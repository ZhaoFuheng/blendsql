import os
from outlines.models import openai, azure_openai, LogitsGenerator
from outlines.models.openai import OpenAIConfig
import tiktoken

from .._model import RemoteModel

DEFAULT_CONFIG = OpenAIConfig(temperature=0.0)


def openai_setup() -> None:
    """Setup helper for AzureOpenAI and OpenAI models."""
    if all(
        x is not None
        for x in {
            os.getenv("TENANT_ID"),
            os.getenv("CLIENT_ID"),
            os.getenv("CLIENT_SECRET"),
        }
    ):
        try:
            from azure.identity import ClientSecretCredential
        except ImportError:
            raise ValueError(
                "Found ['TENANT_ID', 'CLIENT_ID', 'CLIENT_SECRET'] in .env file, using Azure OpenAI\nIn order to use Azure OpenAI, run `pip install azure-identity`!"
            ) from None
        credential = ClientSecretCredential(
            tenant_id=os.environ["TENANT_ID"],
            client_id=os.environ["CLIENT_ID"],
            client_secret=os.environ["CLIENT_SECRET"],
            disable_instance_discovery=True,
        )
        access_token = credential.get_token(
            os.environ["TOKEN_SCOPE"],
            tenant_id=os.environ["TENANT_ID"],
        )
        os.environ["OPENAI_API_KEY"] = access_token.token
    elif os.getenv("OPENAI_API_KEY") is not None:
        pass
    else:
        raise ValueError(
            "Error authenticating with OpenAI\n Without explicit `OPENAI_API_KEY`, you need to provide ['TENANT_ID', 'CLIENT_ID', 'CLIENT_SECRET']"
        ) from None


class AzureOpenaiLLM(RemoteModel):
    """Class for Azure OpenAI Model API.

    Args:
        model_name_or_path: Name of the Azure deployment to use
        env: Path to directory of .env file, or to the file itself to load as a dotfile.
            Should either contain the variable `OPENAI_API_KEY`,
                or all of `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`
    """

    def __init__(
        self,
        model_name_or_path: str,
        env: str = None,
        caching: bool = True,
        config: OpenAIConfig = None,
        **kwargs
    ):
        super().__init__(
            model_name_or_path=model_name_or_path,
            tokenizer=tiktoken.encoding_for_model(model_name_or_path),
            requires_config=True,
            refresh_interval_min=30,
            load_model_kwargs=kwargs | {"config": config or DEFAULT_CONFIG},
            env=env,
            caching=caching,
            **kwargs
        )

    def _load_model(self, config: OpenAIConfig, **kwargs) -> LogitsGenerator:
        return azure_openai(
            self.model_name_or_path,
            config=config,
            azure_endpoint=os.getenv("OPENAI_API_BASE"),
            api_version=os.getenv("OPENAI_API_VERSION"),
            api_key=os.getenv("OPENAI_API_KEY"),
            **kwargs
        )

    def _setup(self, **kwargs) -> None:
        openai_setup()


class OpenaiLLM(RemoteModel):
    """Class for OpenAI Model API.

    Args:
        model_name_or_path: Name of the OpenAI model to use
        env: Path to directory of .env file, or to the file itself to load as a dotfile.
            Should contain the variable `OPENAI_API_KEY`
    """

    def __init__(
        self,
        model_name_or_path: str,
        env: str = None,
        caching: bool = True,
        config: OpenAIConfig = None,
        **kwargs
    ):
        super().__init__(
            model_name_or_path=model_name_or_path,
            tokenizer=tiktoken.encoding_for_model(model_name_or_path),
            requires_config=True,
            refresh_interval_min=30,
            load_model_kwargs={"config": config or DEFAULT_CONFIG},
            env=env,
            caching=caching,
            **kwargs
        )

    def _load_model(self, config: OpenAIConfig, **kwargs) -> LogitsGenerator:
        return openai(
            self.model_name_or_path,
            config=config,
            api_key=os.getenv("OPENAI_API_KEY"),
            **kwargs
        )

    def _setup(self, **kwargs) -> None:
        openai_setup()
