"""Build the configured LLM provider. Switching provider is a configuration change."""

from __future__ import annotations

from opspilot.llm.openai_compat import OpenAICompatibleProvider
from opspilot.llm.types import LLMProvider
from opspilot.settings import OpsPilotSettings

# OpenAI-compatible endpoints. Check each provider's docs if a call fails: these change.
PROVIDER_BASE_URLS: dict[str, str | None] = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openai": None,  # SDK default
    "ollama": "http://localhost:11434/v1",
}


def build_llm(settings: OpsPilotSettings) -> LLMProvider:
    if settings.llm_provider == "custom":
        if not settings.llm_base_url:
            raise ValueError("OPSPILOT_LLM_BASE_URL is required when provider=custom")
        base_url: str | None = settings.llm_base_url
    else:
        base_url = PROVIDER_BASE_URLS[settings.llm_provider]
    api_key = settings.llm_api_key.get_secret_value()
    if not api_key and settings.llm_provider not in ("ollama", "custom"):
        raise ValueError("OPSPILOT_LLM_API_KEY is not set")
    return OpenAICompatibleProvider(
        base_url=base_url,
        api_key=api_key,
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )
