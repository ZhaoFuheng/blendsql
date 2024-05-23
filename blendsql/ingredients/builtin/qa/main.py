import copy
from typing import Dict, Union, Optional, Set, List
import pandas as pd
import outlines

from blendsql.models._model import Model
from blendsql._program import Program
from blendsql.ingredients.ingredient import QAIngredient
from blendsql.db.utils import single_quote_escape


class QAProgram(Program):
    def __call__(
        self,
        question: str,
        context: Optional[pd.DataFrame] = None,
        options: Optional[List[str]] = None,
        long_answer: Optional[bool] = False,
        table_title: Optional[str] = None,
        max_tokens: int = None,
        **kwargs,
    ) -> str:
        prompt = ""
        serialized_db = context.to_string() if context is not None else ""
        prompt += "Answer the question for the table. "
        if long_answer:
            prompt += "Make the answer as concrete as possible, providing more context and reasoning using the entire table.\n"
        else:
            prompt += "Keep the answers as short as possible, without leading context. For example, do not say 'The answer is 2', simply say '2'.\n"
        if options is not None:
            # prompt += "Your answer should be a selection from one of the following options:\n"
            # int_prefix_options = []
            # for _idx, option in enumerate(options):
            #     int_prefix_option = f"{option}"
            #     int_prefix_options.append(int_prefix_option)
            #     prompt += f"{int_prefix_option}\n"
            # Add in title case, since this helps with selection
            modified_option_to_original = {}
            _options = copy.deepcopy(options)
            # Below we check to see if our options have a unique first word
            # sometimes, the model will generate 'Frank' instead of 'Frank Smith'
            # We still want to align that, in this case
            add_first_word = False
            if len(set([i.split(" ")[0] for i in options])) == len(options):
                add_first_word = True
            for option in options:
                option = str(option)
                for modified_option in [option.title(), option.upper()]:
                    _options.add(modified_option)
                    modified_option_to_original[modified_option] = option
                if add_first_word:
                    modified_option_to_original[option.split(" ")[0]] = option
            options = _options
        prompt += f"\n\nQuestion: {question}"
        if table_title is not None:
            prompt += (
                f"\n\nContext: \n Table Description: {table_title} \n {serialized_db}"
            )
        else:
            prompt += f"\n\nContext: \n {serialized_db}"
        if options is not None:
            generator = outlines.generate.choice(
                self.model.logits_generator, [str(i) for i in options]
            )
        else:
            generator = outlines.generate.text(self.model.logits_generator)
        result = generator(prompt, max_tokens=max_tokens)
        if options:
            result = modified_option_to_original.get(result, result)
        return result


class LLMQA(QAIngredient):
    DESCRIPTION = """
    If mapping to a new column still cannot answer the question with valid SQL, turn to an end-to-end solution using the aggregate function:
        `{{LLMQA('question', (blendsql))}}`
        Optionally, this function can take an `options` argument to restrict its output to an existing SQL column.
        For example: `... WHERE column = {{LLMQA('question', (blendsql), options='table::column)}}`
    """

    def run(
        self,
        question: str,
        model: Model,
        options: Optional[Set[str]] = None,
        context: Optional[pd.DataFrame] = None,
        value_limit: Optional[int] = None,
        table_to_title: Optional[Dict[str, str]] = None,
        long_answer: bool = False,
        **kwargs,
    ) -> Union[str, int, float]:
        if context is not None:
            if value_limit is not None:
                context = context.iloc[:value_limit]
        result = model.predict(
            program=QAProgram,
            options=options,
            question=question,
            context=context,
            long_answer=long_answer,
            table_title=None,
            **kwargs,
        )
        return "'{}'".format(single_quote_escape(result.strip().lower()))
