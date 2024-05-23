from typing import Tuple
import inspect
import ast
import textwrap


class Program:
    """ """

    def __new__(
        self,
        model: "Model",
        **kwargs,
    ):
        self.model = model
        return self.__call__(self, **kwargs)

    def __call__(self, *args, **kwargs) -> Tuple[str, str]:
        """Logic for formatting prompt and calling the underlying model.
        Should return tuple of (response, prompt).
        """
        ...


def program_to_str(program: Program):
    """Create a string representation of a program.
    It is slightly tricky, since in addition to getting the code content, we need to
        1) identify all global variables referenced within a function, and then
        2) evaluate the variable value
    This is required, since if we have some global constant `PROMPT` called,
    we don't want to fetch from a previously created cache if the value of `PROMPT` changes.

    To avoid extreme messiness, we don't traverse into globals pointing at functions.

    Example:
        >>> PROMPT = "Here is my question: {question}"
        >>> class CorrectionProgram(Program):
        >>>     def __call__(self, question: str, **kwargs):
        >>>         return PROMPT.format(question)

    Some helpful refs:
        - https://github.com/universe-proton/universe-topology/issues/15
    """
    source_func = program.__call__
    call_content = textwrap.dedent(inspect.getsource(source_func))
    root = ast.parse(call_content)
    root_names = {node.id for node in ast.walk(root) if isinstance(node, ast.Name)}
    co_varnames = set(source_func.__code__.co_varnames)
    names_to_resolve = sorted(root_names.difference(co_varnames))
    resolved_names = ""
    if len(names_to_resolve) > 0:
        globals_as_dict = dict(inspect.getmembers(source_func))["__globals__"]
        for name in names_to_resolve:
            if name in globals_as_dict:
                if name.startswith("__"):
                    continue
                val = globals_as_dict[name]
                # Ignore functions - we really only want scalars here
                if any(x for x in [callable(val), hasattr(val, "__module__")]):
                    continue
                resolved_names += f"{val}\n"
    return f"{call_content}{resolved_names}"
