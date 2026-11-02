from typing import Any

import anthropic
import openai
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings

# Status codes worth retrying: request timeout, conflict, rate limit (5xx handled separately).
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429})


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


def is_transient_llm_error(exc: BaseException) -> bool:
    """True for provider/network failures that may succeed if retried later."""
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return True  # includes timeouts
    if isinstance(exc, (openai.APIStatusError, anthropic.APIStatusError)):
        return exc.status_code in _RETRYABLE_STATUS_CODES or exc.status_code >= 500
    return isinstance(exc, (TimeoutError, ConnectionError))
