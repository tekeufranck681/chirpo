"""LLM providers, one module each, chosen in settings (D9, D45)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

    from chirpo.config import Settings
    from chirpo.core.ports import LLMProvider


def llm_from_settings(
    settings: "Settings", *, client: "httpx.AsyncClient | None" = None
) -> "LLMProvider | None":
    """The configured provider, or None when no API key is set. A worker passes its `client`."""
    key = settings.llm_api_key
    if key is None or not key.get_secret_value().strip():
        return None
    from chirpo.adapters.llm.gemini import DEFAULT_MODEL, GeminiProvider

    return GeminiProvider(key, settings.llm_model or DEFAULT_MODEL, client=client)
