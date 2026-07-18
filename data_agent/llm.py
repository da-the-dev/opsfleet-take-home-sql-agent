"""Model-provider abstraction (design §2 "LLM" / §4.5 fallbacks).

Everything that talks to a model goes through the two factories here; the
rest of the codebase never imports a provider SDK. Select with
``LLM_PROVIDER`` = ``gemini`` (default) | ``ollama`` | ``openrouter``.

Whatever the primary, an OpenRouter fallback is attached when
``OPENROUTER_API_KEY`` is set (unless OpenRouter *is* the primary), so a
provider outage degrades to a slower answer instead of no answer.
"""

import logging
from typing import Optional, Sequence

from langchain_core.embeddings import Embeddings
from langchain_core.runnables import Runnable

from . import config as cfg

logger = logging.getLogger(__name__)


class ProviderConfigError(Exception):
    """Provider selected but its configuration is incomplete; message says what to set."""


def _gemini():
    if not cfg.GOOGLE_API_KEY:
        raise ProviderConfigError("LLM_PROVIDER=gemini needs GOOGLE_API_KEY (see .env.example).")
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=cfg.GEMINI_MODEL, temperature=0.1)


def _ollama():
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=cfg.OLLAMA_MODEL, base_url=cfg.OLLAMA_BASE_URL, temperature=0.1
    )


def _openrouter():
    if not cfg.OPENROUTER_API_KEY:
        raise ProviderConfigError(
            "LLM_PROVIDER=openrouter needs OPENROUTER_API_KEY (see .env.example)."
        )
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    return ChatOpenAI(
        model=cfg.OPENROUTER_MODEL,
        api_key=SecretStr(cfg.OPENROUTER_API_KEY),
        base_url="https://openrouter.ai/api/v1",
        temperature=0.1,
    )


_CHAT_FACTORIES = {"gemini": _gemini, "ollama": _ollama, "openrouter": _openrouter}


def build_chat_model(tools: Sequence) -> Runnable:
    """Tool-bound chat model for the configured provider, with retry + fallback."""
    provider = cfg.LLM_PROVIDER
    if provider not in _CHAT_FACTORIES:
        raise ProviderConfigError(
            f"Unknown LLM_PROVIDER '{provider}'; use one of {sorted(_CHAT_FACTORIES)}."
        )
    logger.info("LLM provider: %s", provider)
    primary = (
        _CHAT_FACTORIES[provider]()
        .bind_tools(tools)
        .with_retry(stop_after_attempt=3, wait_exponential_jitter=True)
    )
    if provider != "openrouter" and cfg.OPENROUTER_API_KEY:
        return primary.with_fallbacks([_openrouter().bind_tools(tools)])
    return primary


def build_embeddings() -> Optional[Embeddings]:
    """Embeddings for golden-trio retrieval.

    ``EMBEDDING_PROVIDER=auto`` follows ``LLM_PROVIDER``; set it explicitly to
    mix (e.g. OpenRouter chat + local Ollama embeddings). Returns None when no
    provider is usable — retrieval then falls back to keyword matching
    (degrade, don't die).
    """
    provider = cfg.EMBEDDING_PROVIDER
    if provider == "auto":
        provider = cfg.LLM_PROVIDER
    try:
        if provider == "ollama":
            from langchain_ollama import OllamaEmbeddings

            return OllamaEmbeddings(
                model=cfg.OLLAMA_EMBEDDING_MODEL, base_url=cfg.OLLAMA_BASE_URL
            )
        if provider == "openrouter" and cfg.OPENROUTER_API_KEY:
            from langchain_openai import OpenAIEmbeddings
            from pydantic import SecretStr

            return OpenAIEmbeddings(
                model=cfg.OPENROUTER_EMBEDDING_MODEL,
                api_key=SecretStr(cfg.OPENROUTER_API_KEY),
                base_url="https://openrouter.ai/api/v1",
                # Send raw strings: OpenRouter rejects tiktoken token arrays.
                check_embedding_ctx_length=False,
            )
        if provider == "gemini" and cfg.GOOGLE_API_KEY:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings

            return GoogleGenerativeAIEmbeddings(model=cfg.EMBEDDING_MODEL)
        logger.info("No embedding provider for %r; keyword retrieval fallback", provider)
    except Exception:  # noqa: BLE001
        logger.exception("Embedding provider failed to initialize")
    return None
