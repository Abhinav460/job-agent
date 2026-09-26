"""Chat model factories. Keys come from Settings and are passed explicitly."""

from __future__ import annotations

from typing import Any

from jobagent.config import Settings


class ConfigError(RuntimeError):
    pass


def _anthropic(settings: Settings, model: str, max_tokens: int) -> Any:
    from langchain_anthropic import ChatAnthropic

    if settings.anthropic_api_key is None:
        raise ConfigError("ANTHROPIC_API_KEY is not set in .env")
    return ChatAnthropic(
        model=model,
        api_key=settings.anthropic_api_key,
        max_tokens=max_tokens,
        max_retries=2,
    )


def coordinator_llm(settings: Settings) -> Any:
    """Parsing and routing. Gemini replaces Sonnet here when GEMINI_ENABLED=true."""
    if settings.gemini_enabled:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if settings.google_api_key is None:
            raise ConfigError("GEMINI_ENABLED=true but GOOGLE_API_KEY is not set in .env")
        return ChatGoogleGenerativeAI(model=settings.gemini_model, google_api_key=settings.google_api_key)
    return _anthropic(settings, settings.coordinator_model, max_tokens=4096)


def writer_llm(settings: Settings) -> Any:
    """Resume writing. Always Claude."""
    return _anthropic(settings, settings.writer_model, max_tokens=8192)
