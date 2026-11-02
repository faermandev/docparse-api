from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Create the chat model for the configured provider (LLM_PROVIDER / LLM_MODEL)."""
    kwargs: dict[str, Any] = {
        "max_tokens": settings.llm_max_tokens,
        "timeout": settings.llm_timeout_seconds,
    }
    if settings.llm_api_key is not None:
        kwargs["api_key"] = settings.llm_api_key.get_secret_value()

    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider, **kwargs)
    assert isinstance(model, BaseChatModel)  # a concrete model is returned when one is named
    return model
