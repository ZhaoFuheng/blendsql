# Set up logging
import logging
import sys
from typing import Tuple

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from ..database import to_serialized
from ..dataset import DataTrainingArguments
from ...utils.args import ModelArguments
from ...constants import DOCS_TABLE_NAME, EvalField
from ...prompts.few_shot.ottqa import blendsql_examples
from ...utils.bridge_content_encoder import (
    get_database_matches,
)

from blendsql.db import SQLiteDBConnector
from pathlib import Path
import json

ottqa_db_path = "./research/db/ottqa/ottqa.db"
db = SQLiteDBConnector(ottqa_db_path)
DOCS_TABLE_SIZE = db.execute_query(
    f"SELECT COUNT(*) FROM {CONST.DOCS_TABLE_NAME}"
).values[0][0]

ottqa_question_id_to_retriever_results = {}
for filename in Path("./research/utils/ottqa/OTT-QA").iterdir():
    if filename.suffix != ".json":
        continue
    with open(filename, "r") as f:
        d = json.load(f)
    for item in d:
        id_field = "question_id" if "question_id" in item else "id"
        ottqa_question_id_to_retriever_results[item[id_field]] = item["results"]

with open("./research/utils/ottqa/table_id_to_tablename.json", "r") as f:
    table_id_to_tablename = json.load(f)


def ottqa_metric_format_func(item: dict) -> dict:
    prediction = item.get(EvalField.PREDICTION, None)
    if prediction is not None:
        if len(prediction) < 1:
            pred = ""
        else:
            pred = prediction[0]
    else:
        pred = ""
    return {
        "prediction": str(pred),
        "reference": {"answer_text": item["answer_text"], "id": item["id"]},
    }


def ottqa_get_input(
    question: str,
    question_id: str,
    db_path: str,
    data_training_args: DataTrainingArguments,
    model_args: ModelArguments,
) -> Tuple[str, dict]:
    chosen_tables = ottqa_question_id_to_retriever_results[question_id]
    chosen_tables = [
        table_id_to_tablename["_".join(item["title"].split("_")[:-1])]
        for item in chosen_tables
    ]
    chosen_tables = [f"./{i}" for i in chosen_tables]

    # filter unique and select top-n
    seen_tables = set()
    final_chosen_tables = []
    for t in chosen_tables:
        if t in seen_tables:
            continue
        final_chosen_tables.append(t)
        seen_tables.add(t)
    chosen_tables = final_chosen_tables[:3] + [DOCS_TABLE_NAME]

    serialized_db = to_serialized(
        db=db,
        num_rows=data_training_args.num_serialized_rows,
        use_tables=chosen_tables,
        truncate_content=500,
    )
    bridge_hints = None
    if data_training_args.use_bridge_encoder:
        bridge_hints = []
        column_str_with_values = "{table}.{column} ( {values} )"
        value_sep = " , "
        for table_name in chosen_tables:
            if table_name == DOCS_TABLE_NAME:
                continue
            for column_name in db.iter_columns(table_name):
                matches = get_database_matches(
                    question=question,
                    table_name=table_name,
                    column_name=column_name,
                    db=db,
                )
                if matches:
                    bridge_hints.append(
                        column_str_with_values.format(
                            table=table_name,
                            column=column_name,
                            values=value_sep.join(matches),
                        )
                    )
        bridge_hints = "\n".join(bridge_hints)
    return (
        db_path,
        {
            "examples": blendsql_examples
            if model_args.blender_model_name_or_path is not None
            else None,
            "question": question,
            "serialized_db": serialized_db,
            "entire_serialized_db": None,
            "bridge_hints": bridge_hints,
            "use_tables": chosen_tables,
            "extra_task_description": f"Additionally, we have the table `{DOCS_TABLE_NAME}` at our disposal, which contains {DOCS_TABLE_SIZE} Wikipedia articles providing more details about the values in our table.",
        },
    )


def ottqa_pre_process_function(
    batch: dict, data_training_args: DataTrainingArguments, model_args: ModelArguments
) -> dict:
    db_path, input_program_args = zip(
        *[
            ottqa_get_input(
                question=question,
                question_id=question_id,
                db_path=db_path,
                data_training_args=data_training_args,
                model_args=model_args,
            )
            for question, db_path, question_id in zip(
                batch["question"], batch["db_path"], batch["id"]
            )
        ]
    )
    return {"input_program_args": list(input_program_args), "db_path": list(db_path)}
