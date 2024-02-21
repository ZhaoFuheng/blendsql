import logging

from guidance.models import Transformers, Model

from .._llm import LLM

logging.getLogger("guidance").setLevel(logging.CRITICAL)


class TransformersLLM(LLM):
    """Class for Transformers Local LLM."""

    def __init__(self, model_name_or_path: str, **kwargs):
        try:
            import transformers
        except ImportError:
            raise Exception(
                "Please install transformers with `pip install transformers`!"
            ) from None
        super().__init__(
            model_name_or_path=model_name_or_path,
            requires_config=False,
            tokenizer=transformers.AutoTokenizer.from_pretrained(model_name_or_path),
            **kwargs
        )

    def _load_model(self) -> Model:
        return Transformers(self.model_name_or_path, echo=False)
