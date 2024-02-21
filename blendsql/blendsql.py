import copy
import logging
import time
import uuid
import pandas as pd
import pyparsing
import re
from typing import Dict, Iterable, List, Set, Tuple, Generator, Optional
from sqlite3 import OperationalError
import sqlglot.expressions
from attr import attrs, attrib
from functools import partial
from sqlglot import exp
from colorama import Fore
import string

from .utils import (
    sub_tablename,
    get_temp_session_table,
    get_temp_subquery_table,
    delete_session_tables,
    recover_blendsql,
    get_tablename_colname,
)
from .db.sqlite_db_connector import SQLiteDBConnector
from .db.utils import double_quote_escape, single_quote_escape
from ._sqlglot import (
    MODIFIERS,
    get_singleton_child,
    get_reversed_subqueries,
    replace_join_with_ingredient_single_ingredient,
    replace_join_with_ingredient_multiple_ingredient,
    prune_true_where,
    prune_with,
    replace_subquery_with_direct_alias_call,
    maybe_set_subqueries_to_true,
    SubqueryContextManager,
    remove_ctes,
    is_in_cte,
)
from ._dialect import _parse_one, FTS5SQLite
from ._grammar import grammar
from .ingredients.ingredient import Ingredient
from ._smoothie import Smoothie, SmoothieMeta
from ._constants import (
    IngredientType,
    IngredientKwarg,
)
from .llms._llm import LLM


@attrs
class Kitchen(list):
    """Superset of list. A collection of ingredients."""

    db: SQLiteDBConnector = attrib()
    session_uuid: str = attrib()

    added_ingredient_names: set = attrib(init=False)

    def __attrs_post_init__(self):
        self.added_ingredient_names = set()

    def names(self):
        return [i.name for i in self]

    def get_from_name(self, name: str):
        for f in self:
            if f.name == name.upper():
                return f
        raise pyparsing.ParseException(
            f"Ingredient '{name}' called, but not found in passed `ingredient` arg!"
        )

    def extend(self, functions: Iterable[Ingredient]) -> None:
        assert all(
            issubclass(x, Ingredient) for x in functions
        ), "All arguments passed to `Kitchen` must be ingredients!"
        for function in functions:
            name = function.__name__.upper()
            assert (
                name not in self.added_ingredient_names
            ), f"Duplicate ingredient names passed! These are case insensitive, be careful.\n{name}"
            function = function(name)
            self.added_ingredient_names.add(name)
            # Add db and session_uuid as default kwargs
            # This way, ingredients are able to interact with data
            function.db = self.db
            function.session_uuid = self.session_uuid
            self.append(function)


def autowrap_query(
    query: str, kitchen: Kitchen, ingredient_alias_to_parsed_dict: Dict[str, dict]
) -> Tuple[exp.Expression, str]:
    """
    Check to see if we have some BlendSQL ingredient syntax that needs to be formatted differently
        before passing to sqlglot.parse_one.
    A single `QAIngredient` should be wrapped in `CASE` syntax.
    A `JoinIngredient` needs to include a reference to the left tablename.
    """
    # TODO: i dont think we need to scanString again
    for _parse_results, start, end in [i for i in grammar.scanString(query)][::-1]:
        alias_function_str = query[start:end]
        parse_results_dict = ingredient_alias_to_parsed_dict[alias_function_str]
        _function: Ingredient = kitchen.get_from_name(parse_results_dict["function"])
        if _function.ingredient_type == IngredientType.QA:
            if not query.strip().lower().startswith("select"):
                query = query.replace(
                    alias_function_str,
                    f"""SELECT CASE WHEN FALSE THEN FALSE WHEN TRUE THEN {alias_function_str} END""",
                )
        elif _function.ingredient_type == IngredientType.JOIN:
            kwargs_dict = {x[0]: x[-1] for x in parse_results_dict["kwargs"]}
            left_table, left_column = get_tablename_colname(kwargs_dict["left_on"])
            query = query.replace(
                alias_function_str,
                f'"{left_table}" ON {alias_function_str}',
            )
        else:
            continue
    # Now re-parse with sqlglot
    _query: exp.Expression = _parse_one(query)
    original_query = copy.deepcopy(recover_blendsql(_query.sql(dialect=FTS5SQLite)))
    return (_query, original_query)


def preprocess_blendsql(query: str) -> Tuple[str, dict]:
    ingredient_alias_to_parsed_dict = {}
    ingredient_str_to_alias = {}
    query = re.sub(r"(\s+)", " ", query)
    reversed_scan_res = [scan_res for scan_res in grammar.scanString(query)][::-1]
    for idx, (parse_results, start, end) in enumerate(reversed_scan_res):
        original_ingredient_string = query[start:end]
        # If we're in between parentheses, add a `SELECT`
        # This way it gets picked up as a subquery to parse later
        # TODO: is this safe to do?
        if query[start - 2] == "(" and query[end + 1] == ")":
            inserted_select = " SELECT "
            query = query[:start] + inserted_select + query[start:end] + query[end:]
            start += len(inserted_select)
            end += len(inserted_select)
        if (
            original_ingredient_string in ingredient_str_to_alias
        ):  # If we've already processed this function, no need to do it again
            substituted_ingredient_alias = ingredient_str_to_alias[
                original_ingredient_string
            ]
        else:
            substituted_ingredient_alias = (
                "{{" + f"{string.ascii_uppercase[idx]}()" + "}}"
            )
            ingredient_str_to_alias[
                original_ingredient_string
            ] = substituted_ingredient_alias
            parsed_results_dict = parse_results.as_dict()
            # Remove parentheses at beginning and end of arg/kwarg
            # TODO: this should be handled by pyparsing
            for arg_type in {"args", "kwargs"}:
                for idx in range(len(parsed_results_dict[arg_type])):
                    curr_arg = parsed_results_dict[arg_type][idx]
                    if not isinstance(curr_arg, str):
                        continue
                    parsed_results_dict[arg_type][idx] = re.sub(
                        r"(^\()(.*)(\)$)", r"\2", curr_arg
                    ).strip()
            # Below we track the 'raw' representation, in case we need to pass into
            #   a recursive BlendSQL call later
            ingredient_alias_to_parsed_dict[
                substituted_ingredient_alias
            ] = parsed_results_dict | {"raw": query[start:end]}
        query = query[:start] + substituted_ingredient_alias + query[end:]
    return (query, ingredient_alias_to_parsed_dict)


def set_subquery_to_alias(
    subquery: exp.Expression,
    aliasname: str,
    query: exp.Expression,
    db: SQLiteDBConnector,
    blender: LLM,
    ingredient_alias_to_parsed_dict: Dict[str, dict],
    **kwargs,
) -> exp.Expression:
    str_subquery = recover_blendsql(subquery.sql(dialect=FTS5SQLite))
    disambiguate_and_submit_blend(
        ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
        query=str_subquery,
        db=db,
        blender=blender,
        aliasname=aliasname,
        **kwargs,
    ).df.to_sql(aliasname, db.con, if_exists="replace", index=False)
    # Now, we need to remove subquery and instead insert direct reference to aliasname
    # Example:
    #   `SELECT Symbol FROM (SELECT DISTINCT Symbol FROM portfolio) AS w`
    #   Should become: `SELECT Symbol FROM w`
    return query.transform(
        replace_subquery_with_direct_alias_call,
        subquery=subquery.parent,
        aliasname=aliasname,
    ).transform(prune_with)


def get_sorted_grammar_matches(
    q: str,
    ingredient_alias_to_parsed_dict: dict,
    kitchen: Kitchen,
) -> Generator[Tuple[int, int, dict, Ingredient], None, None]:
    """
    Yields parsed matches from grammar, according to a specified order of operations.

    Arguments:
        q: str, the current BlendSQL query to parse. Should contain ingredient aliases.
        ingredient_alias_to_parsed_dict: Mapping from ingredient alias to their parsed representations.
            Example:
                {"{{A()}}": {"function": "LLMMap", "args": ...}
        kitchen: Contains inventory of ingredients (aka BlendSQL functions)

    Returns:
        Generator containing tuple of:
            - start index of grammar match
            - end index of grammar match
            - parsed_results_dict
            - `Function` object that is matched
    """
    ooo = [IngredientType.MAP, IngredientType.QA, IngredientType.JOIN]
    parse_results = [i for i in grammar.scanString(q)]
    while len(parse_results) > 0:
        curr_ingredient_target = ooo.pop(0)
        remaining_parse_results = []
        for _, start, end in parse_results:
            alias_function_str = q[start:end]
            # Fetch parsed ingredient dict from our cache
            parse_results_dict = ingredient_alias_to_parsed_dict[alias_function_str]
            _function: Ingredient = kitchen.get_from_name(
                parse_results_dict["function"]
            )
            if _function.ingredient_type == curr_ingredient_target:
                yield (start, end, alias_function_str, parse_results_dict, _function)
                continue
            elif _function.ingredient_type not in IngredientType:
                raise ValueError(
                    f"Not sure what to do with ingredient_type '{_function.ingredient_type}' yet"
                )
            remaining_parse_results.append((_, start, end))
        parse_results = remaining_parse_results


def disambiguate_and_submit_blend(
    ingredient_alias_to_parsed_dict: Dict[str, dict],
    query: str,
    aliasname: str,
    **kwargs,
):
    """
    Used to disambiguate anonymized BlendSQL function and execute in a recursive context.
    """
    for alias, d in ingredient_alias_to_parsed_dict.items():
        query = re.sub(re.escape(alias), d["raw"], query)
    logging.debug(
        Fore.CYAN + f"Executing `{query}` and setting to `{aliasname}`..." + Fore.RESET
    )
    return blend(query=query, **kwargs)


def blend(
    query: str,
    db: SQLiteDBConnector,
    blender: Optional[LLM] = None,
    ingredients: Optional[Iterable[Ingredient]] = None,
    verbose: bool = False,
    blender_args: Optional[Dict[str, str]] = None,
    infer_map_constraints: bool = False,
    table_to_title: Optional[Dict[str, str]] = None,
    silence_db_exec_errors: bool = True,
    # User shouldn't interact with below
    _prev_passed_values: int = 0,
    _prev_cleanup_tables: Set[str] = None,
) -> Smoothie:
    """Executes a BlendSQL query on a database given an ingredient context.

    Args:
        query: The BlendSQL query to execute
        db: Database connector object
        ingredients: List of ingredient objects, to use in interpreting BlendSQL query
        verbose: Boolean defining whether to run in logging.debug mode
        blender: Optionally override whatever llm argument we pass to LLM ingredient.
            Useful for research applications, where we don't (necessarily) want the parser to choose endpoints.
        infer_map_constraints: Optionally infer the output format of an `IngredientMap` call, given the predicate context
            For example, in `{{LLMMap('convert to date', 'w::listing date')}} <= '1960-12-31'`
            We can infer the output format should look like '1960-12-31'
                and put this in the `example_outputs` kwarg
        table_to_title: Optional mapping from table name to title of table.
            Useful for datasets like WikiTableQuestions, where relevant info is stored in table title.
        _prev_passed_values: int used to track values passed to nested recursive `blend()` calls
    Returns:
        smoothie: Smoothie dataclass containing pd.DataFrame output and execution metadata
    """
    cleanup_tables = set() if _prev_cleanup_tables is None else _prev_cleanup_tables
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.ERROR)
    try:
        start = time.time()
        example_map_outputs = []
        naive_execution = False
        session_uuid = str(uuid.uuid4())[:4]
        if ingredients is None:
            ingredients = []
        # Create our Kitchen
        kitchen = Kitchen(db=db, session_uuid=session_uuid)
        kitchen.extend(ingredients)
        query, ingredient_alias_to_parsed_dict = preprocess_blendsql(query)
        try:
            _query: exp.Expression = _parse_one(query)
            original_query = copy.deepcopy(
                recover_blendsql(_query.sql(dialect=FTS5SQLite))
            )
            query = recover_blendsql(_query.sql(dialect=FTS5SQLite))
        except sqlglot.errors.ParseError:
            _query, original_query = autowrap_query(
                query=query,
                kitchen=kitchen,
                ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
            )
            query = recover_blendsql(_query.sql(dialect=FTS5SQLite))

        # Preliminary check - we can't have anything that modifies database state
        if _query.find(MODIFIERS):
            raise ValueError("BlendSQL query cannot have `DELETE` clause!")

        # If there's no `SELECT` and just a QAIngredient, wrap it in a `SELECT CASE` query
        if _query.find(exp.Select) is None:
            _query, original_query = autowrap_query(
                query=query,
                kitchen=kitchen,
                ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
            )
            query = recover_blendsql(_query.sql(dialect=FTS5SQLite))

        # If we don't have any ingredient calls, execute as normal SQL
        if len(ingredients) == 0 or grammar.searchString(query).as_list() == []:
            return Smoothie(
                df=db.execute_query(query, silence_errors=silence_db_exec_errors),
                meta=SmoothieMeta(
                    process_time_seconds=time.time() - start,
                    num_values_passed=0,
                    num_prompt_tokens=0,
                    prompts=blender.prompts if blender is not None else [],
                    example_map_outputs=example_map_outputs,
                    ingredients=[],
                    query=original_query,
                    db_path=db.db_path,
                    contains_ingredient=False,
                ),
            )
        _get_temp_session_table = partial(get_temp_session_table, session_uuid)
        # Mapping from {"QA('does this company...', 'constituents::Name')": 'does this company'...})
        function_call_to_res: Dict[str, str] = {}
        session_modified_tables = set()
        # TODO: Currently, as we traverse upwards from deepest subquery,
        #   if any lower subqueries have an ingredient, we deem the current
        #   as inelligible for optimization. Maybe this can be improved in the future.
        prev_subquery_has_ingredient = False
        for subquery_idx, subquery in enumerate(get_reversed_subqueries(_query)):
            # At this point, we should have already handled cte statements and created associated tables
            subquery = subquery.transform(remove_ctes)
            # Only cache executed_ingredients within the same subquery
            # The same ingredient may have different results within a different subquery context
            executed_subquery_ingredients: Set[str] = set()
            prev_subquery_map_columns = set()
            _get_temp_subquery_table = partial(
                get_temp_subquery_table, session_uuid, subquery_idx
            )
            if not isinstance(subquery, exp.Select):
                # We need to create a select query from this subquery
                # So we find the parent select, and grab that table
                parent_select_tablenames = [
                    i.name
                    for i in subquery.find_ancestor(exp.Select).find_all(exp.Table)
                ]
                if len(parent_select_tablenames) == 1:
                    subquery_str = recover_blendsql(
                        f"SELECT * FROM {parent_select_tablenames[0]} WHERE "
                        + get_singleton_child(subquery).sql(dialect=FTS5SQLite)
                    )
                else:
                    logging.debug(
                        Fore.YELLOW
                        + "Encountered subquery without `SELECT`, and more than 1 table!\nCannot optimize yet, skipping this step."
                    )
                    continue
            else:
                subquery_str = recover_blendsql(subquery.sql(dialect=FTS5SQLite))

            in_cte, table_alias_name = is_in_cte(subquery, return_name=True)
            scm = SubqueryContextManager(
                node=_parse_one(
                    subquery_str
                ),  # Need to do this so we don't track parents into construct_abstracted_selects
                prev_subquery_has_ingredient=prev_subquery_has_ingredient,
                alias_to_subquery={table_alias_name: subquery} if in_cte else None,
            )
            for tablename, abstracted_query in scm.abstracted_table_selects():
                aliased_subquery = scm.alias_to_subquery.pop(tablename, None)
                if aliased_subquery is not None:
                    # First, we need to explicitly create the aliased subquery as a table
                    # For example, `SELECT Symbol FROM (SELECT DISTINCT Symbol FROM portfolio) AS w WHERE w...`
                    # We can't assign `abstracted_query` for non-existent `w`
                    #   until we set `w` to `SELECT DISTINCT Symbol FROM portfolio`
                    _query = set_subquery_to_alias(
                        subquery=aliased_subquery,
                        aliasname=tablename,
                        query=_query,
                        blender=blender,
                        db=db,
                        ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
                        # Below are in case we need to call blend() again
                        ingredients=ingredients,
                        blender_args=blender_args,
                        infer_map_constraints=infer_map_constraints,
                        silence_db_exec_errors=silence_db_exec_errors,
                        table_to_title=table_to_title,
                        verbose=verbose,
                        _prev_passed_values=_prev_passed_values,
                        _prev_cleanup_tables=cleanup_tables,
                    )
                    query = recover_blendsql(_query.sql(dialect=FTS5SQLite))
                    cleanup_tables.add(tablename)
                if abstracted_query is not None:
                    logging.debug(
                        Fore.CYAN
                        + f"Executing `{abstracted_query}` and setting to `{_get_temp_subquery_table(tablename)}`..."
                        + Fore.RESET
                    )
                    try:
                        db.execute_query(abstracted_query).to_sql(
                            _get_temp_subquery_table(tablename),
                            db.con,
                            if_exists="replace",
                            index=False,
                        )
                        cleanup_tables.add(_get_temp_subquery_table(tablename))
                    except OperationalError:
                        # Fallback to naive execution
                        naive_execution = True
            # Be sure to handle those remaining aliases, which didn't have abstracted queries
            for aliasname, aliased_subquery in scm.alias_to_subquery.items():
                _query = set_subquery_to_alias(
                    subquery=aliased_subquery,
                    aliasname=aliasname,
                    query=_query,
                    blender=blender,
                    db=db,
                    ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
                    # Below are in case we need to call blend() again
                    ingredients=ingredients,
                    blender_args=blender_args,
                    infer_map_constraints=infer_map_constraints,
                    silence_db_exec_errors=silence_db_exec_errors,
                    table_to_title=table_to_title,
                    verbose=verbose,
                    _prev_passed_values=_prev_passed_values,
                    _prev_cleanup_tables=cleanup_tables,
                )
                query = recover_blendsql(_query.sql(dialect=FTS5SQLite))
                cleanup_tables.add(aliasname)
            if prev_subquery_has_ingredient:
                scm.set_node(scm.node.transform(maybe_set_subqueries_to_true))

            # After above processing of ast, sync back to string repr
            subquery_str = scm.sql()
            # Find all ingredients to execute (e.g. '{{f(a, b, c)}}')
            # Track when we've created a new table from a MapIngredient call
            # only at the end of parsing a subquery, we can merge to the original session_uuid table
            tablename_to_map_out: Dict[str, List[pd.DataFrame]] = {}
            for (
                start,
                end,
                alias_function_str,
                parse_results_dict,
                _function,
            ) in get_sorted_grammar_matches(
                q=subquery_str,
                ingredient_alias_to_parsed_dict=ingredient_alias_to_parsed_dict,
                kitchen=kitchen,
            ):
                prev_subquery_has_ingredient = True
                if alias_function_str in executed_subquery_ingredients:
                    # Don't execute same ingredient twice
                    continue
                executed_subquery_ingredients.add(alias_function_str)
                # kwargs gets returned as ['limit', '=', 10] sort of list
                # So we need to parse by indices in dict expression
                # maybe if I was better at pp.Suppress we wouldn't need this
                kwargs_dict = {x[0]: x[-1] for x in parse_results_dict["kwargs"]}

                # Optionally modify kwargs dict, depending on blend() blender_args
                if blender_args is not None:
                    for k, v in blender_args.items():
                        if k in kwargs_dict:
                            logging.debug(
                                Fore.YELLOW
                                + f"Overriding passed arg for '{k}'!"
                                + Fore.RESET
                            )
                        kwargs_dict[k] = v
                # Handle llm, make sure we initialize it if it's a string
                # llm = kwargs_dict.get("llm", None)
                # llm_type = kwargs_dict.get("llm_type", None)
                # if llm is not None:
                #     if not isinstance(llm, LLM):
                #         kwargs_dict["llm"] = initialize_llm(llm_type, llm)
                # else:
                #     kwargs_dict["llm"] = initialize_llm(
                #         DEFAULT_LLM_TYPE, DEFAULT_LLM_NAME
                #     )
                kwargs_dict["llm"] = blender

                if _function.ingredient_type == IngredientType.MAP:
                    # Latter is the winner.
                    # So if we already define something in kwargs_dict,
                    #   It's not overriden here
                    kwargs_dict = (
                        scm.infer_map_constraints(
                            start=start,
                            end=end,
                        )
                        | kwargs_dict
                        if infer_map_constraints
                        else kwargs_dict
                    )
                if table_to_title is not None:
                    kwargs_dict["table_to_title"] = table_to_title

                if _function.ingredient_type == IngredientType.QA:
                    # Optionally, recursively call blend() again to get subtable
                    context_arg = kwargs_dict.get(
                        IngredientKwarg.CONTEXT,
                        parse_results_dict["args"][1]
                        if len(parse_results_dict["args"]) > 1
                        else parse_results_dict["args"][0]
                        if len(parse_results_dict["args"]) > 0
                        else "",
                    )
                    if context_arg.upper().startswith(("SELECT", "WITH")):
                        _smoothie = blend(
                            query=context_arg,
                            db=db,
                            blender=blender,
                            ingredients=ingredients,
                            blender_args=blender_args,
                            infer_map_constraints=infer_map_constraints,
                            silence_db_exec_errors=silence_db_exec_errors,
                            table_to_title=table_to_title,
                            verbose=verbose,
                            _prev_passed_values=_prev_passed_values,
                            _prev_cleanup_tables=cleanup_tables,
                        )
                        _prev_passed_values = _smoothie.meta.num_values_passed
                        subtable = _smoothie.df
                        kwargs_dict[IngredientKwarg.CONTEXT] = subtable
                        # Below, we can remove the optional `context` arg we passed in args
                        parse_results_dict["args"] = parse_results_dict["args"][:1]
                # Execute our ingredient function
                function_out = _function(
                    *parse_results_dict["args"],
                    **kwargs_dict
                    | {
                        "get_temp_subquery_table": _get_temp_subquery_table,
                        "get_temp_session_table": _get_temp_session_table,
                        "aliases_to_tablenames": scm.alias_to_tablename,
                        "prev_subquery_map_columns": prev_subquery_map_columns,
                    },
                )
                # Check how to handle output, depending on ingredient type
                if _function.ingredient_type == IngredientType.MAP:
                    # Parse so we replace this function in blendsql with 1st arg
                    #   (new_col, which is the question we asked)
                    #  But also update our underlying table, so we can execute correctly at the end
                    (new_col, tablename, colname, new_table) = function_out
                    prev_subquery_map_columns.add(new_col)
                    non_null_subset = new_table[new_table[new_col].notnull()]
                    # These are just for logging + debugging purposes
                    example_map_outputs.append(
                        tuple(zip(non_null_subset[colname], non_null_subset[new_col]))
                    )
                    if tablename in tablename_to_map_out:
                        tablename_to_map_out[tablename].append(new_table)
                    else:
                        tablename_to_map_out[tablename] = [new_table]
                    session_modified_tables.add(tablename)
                    function_call_to_res[
                        alias_function_str
                    ] = f'"{double_quote_escape(tablename)}"."{double_quote_escape(new_col)}"'
                elif _function.ingredient_type in (
                    IngredientType.STRING,
                    IngredientType.QA,
                ):
                    # Here, we can simply insert the function's output
                    function_call_to_res[alias_function_str] = function_out
                elif _function.ingredient_type == IngredientType.JOIN:
                    # 1) Get the `JOIN` clause containing function
                    # 2) Replace with just the function alias
                    # 3) Assign `function_out` to `alias_function_str`
                    (
                        left_tablename,
                        right_tablename,
                        join_clause,
                        temp_join_tablename,
                    ) = function_out
                    cleanup_tables.add(temp_join_tablename)
                    # Special case for when we have more than 1 ingredient in `JOIN` node left at this point
                    num_ingredients_in_join = (
                        len(list(_query.find(exp.Join).find_all(exp.Struct))) // 2
                    )
                    if num_ingredients_in_join > 1:
                        # Case where we have
                        # `SELECT * FROM w0 JOIN w0 ON {{B()}} > 1 AND {{A()}} WHERE TRUE`
                        # Since we haven't executed and saved `{{B()}}` to temp table yet,
                        #   we need to keep. So we get:
                        temp_uuid = str(uuid.uuid4())
                        _query = _query.transform(
                            replace_join_with_ingredient_multiple_ingredient,
                            ingredient_name=re.sub(
                                r"(\{|\}|\(|\))", "", alias_function_str
                            ),
                            ingredient_alias=alias_function_str,
                            temp_uuid=temp_uuid,
                        ).transform(prune_true_where)
                        query = recover_blendsql(
                            _query.sql(dialect=FTS5SQLite)
                        ).replace(f'SELECT "{temp_uuid}", ', "")
                    else:
                        # Case where we have
                        # `SELECT * FROM w0 JOIN w0 ON w0.x > 1 AND {{A()}} WHERE TRUE`
                        # Since we've already applied SQL operation `w0.x > 1` and set to temp table
                        # we can remove this. So below we transform to:
                        # `SELECT * FROM w0 {{A()}}  WHERE TRUE`
                        # This way, `{{A()}}` can get replaced with our new join
                        # TODO: since we're not removing predicates in other areas, probably not best to do it here.
                        #   Should probably modify in the future.
                        _query = _query.transform(
                            replace_join_with_ingredient_single_ingredient,
                            ingredient_name=re.sub(
                                r"(\{|\}|\(|\))", "", alias_function_str
                            ),
                            ingredient_alias=alias_function_str,
                        )
                        query = recover_blendsql(_query.sql(dialect=FTS5SQLite))
                    function_call_to_res[alias_function_str] = join_clause
                else:
                    raise ValueError(
                        f"Not sure what to do with ingredient_type '{_function.ingredient_type}' yet\n(Also, we should have never hit this error....)"
                    )
                if naive_execution:
                    break
            # Combine all the retrieved ingredient outputs
            for tablename, llm_outs in tablename_to_map_out.items():
                if len(llm_outs) > 0:
                    # Once we finish parsing this subquery, write to our session_uuid table
                    # Below, we differ from Binder, which seems to replace the old table
                    # On their left join merge command: https://github.com/HKUNLP/Binder/blob/9eede69186ef3f621d2a50572e1696bc418c0e77/nsql/database.py#L196
                    # We create a new temp table to avoid a potentially self-destructive operation
                    base_tablename = tablename
                    _base_table: pd.DataFrame = db.execute_query(
                        f"SELECT * FROM '{single_quote_escape(base_tablename)}'"
                    )
                    base_table = _base_table
                    if db.has_table(_get_temp_session_table(tablename)):
                        base_tablename = _get_temp_session_table(tablename)
                        base_table: pd.DataFrame = db.execute_query(
                            f"SELECT * FROM '{single_quote_escape(base_tablename)}'"
                        )
                    previously_added_columns = base_table.columns.difference(
                        _base_table.columns
                    )
                    assert len(set([len(x) for x in llm_outs])) == 1
                    llm_out_df = pd.concat(llm_outs, axis=1)
                    llm_out_df = llm_out_df.loc[:, ~llm_out_df.columns.duplicated()]
                    # Handle duplicate columns, e.g. in test_nested_duplicate_ingredient_calls()
                    for column in previously_added_columns:
                        if all(
                            column in x
                            for x in [llm_out_df.columns, base_table.columns]
                        ):
                            # Fill nan in llm_out_df with those values in base_table
                            pd.testing.assert_index_equal(
                                base_table.index, llm_out_df.index
                            )
                            llm_out_df[column] = llm_out_df[column].fillna(
                                base_table[column]
                            )
                            base_table = base_table.drop(columns=column)
                    llm_out_df = llm_out_df[
                        llm_out_df.columns.difference(base_table.columns)
                    ]
                    pd.testing.assert_index_equal(base_table.index, llm_out_df.index)
                    merged = base_table.merge(
                        llm_out_df, how="left", right_index=True, left_index=True
                    )
                    merged.to_sql(
                        name=_get_temp_session_table(tablename),
                        con=db.con,
                        if_exists="replace",
                        index=False,
                    )
                    cleanup_tables.add(_get_temp_session_table(tablename))
                    session_modified_tables.add(tablename)

        # Now insert the function outputs to the original query
        for function_str, res in function_call_to_res.items():
            query = query.replace(function_str, str(res))
        for t in session_modified_tables:
            query = sub_tablename(
                t, f'"{double_quote_escape(_get_temp_session_table(t))}"', query
            )
        for a, t in scm.alias_to_tablename.items():
            if t in session_modified_tables:
                query = sub_tablename(
                    a, f'"{double_quote_escape(_get_temp_session_table(t))}"', query
                )
        logging.debug("")
        logging.debug(
            "**********************************************************************************"
        )
        logging.debug(Fore.LIGHTGREEN_EX + f"Final Query:\n{query}" + Fore.RESET)
        logging.debug(
            "**********************************************************************************"
        )
        logging.debug("")

        df = db.execute_query(query, silence_errors=silence_db_exec_errors)

        return Smoothie(
            df=df,
            meta=SmoothieMeta(
                process_time_seconds=time.time() - start,
                num_values_passed=sum(
                    [
                        i.num_values_passed
                        for i in kitchen
                        if hasattr(i, "num_values_passed")
                    ]
                )
                + _prev_passed_values,
                num_prompt_tokens=blender.num_prompt_tokens
                if blender is not None
                else 0,
                prompts=blender.prompts if blender is not None else [],
                example_map_outputs=example_map_outputs,
                ingredients=ingredients,
                query=original_query,
                db_path=db.db_path,
            ),
        )

    except Exception as error:
        raise error
    finally:
        # In the case of a recursive `blend()` call,
        #   this logic allows temp tables to persist until
        #   the final base case is fulfilled.
        if _prev_cleanup_tables is None:
            delete_session_tables(db=db, cleanup_tables=cleanup_tables)
