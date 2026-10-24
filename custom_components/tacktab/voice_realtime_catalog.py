"""No-cost provider metadata discovery with an honest bundled fallback."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .voice_realtime_config import RealtimeConfigError, api_key

_FALLBACK = {
    "openai": {"models": ("gpt-realtime", "gpt-realtime-2.1"), "voices": ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")},
    "xai": {"models": ("grok-voice-3.0", "grok-voice-latest"), "voices": ("eve",)},
    "gemini": {"models": ("models/gemini-3.8-live",), "voices": ("Kore",)},
}


def _checked_at() -> str:
    return datetime.now(UTC).isoformat()


async def _get_json(hass: Any, url: str, headers: dict[str, str] | None = None, params: dict[str, str] | None = None) -> Any:
    try:
        async with async_get_clientsession(hass).get(url, headers=headers, params=params, timeout=10) as response:
            if response.status != 200: return None
            return await response.json(content_type=None)
    except Exception:
        return None


async def async_catalog(hass: Any, entry: Any, provider: str) -> dict[str, Any]:
    """Discover metadata only; it never opens an audio/live-session endpoint."""
    if provider not in _FALLBACK:
        raise RealtimeConfigError("Invalid realtime provider")
    fallback = _FALLBACK[provider]
    key = api_key(entry, provider, hass)
    checked_at = _checked_at()
    if not key:
        return {"provider": provider, "key_configured": False, "models": list(fallback["models"]), "voices": list(fallback["voices"]), "source": "bundled", "checked_at": checked_at, "validated": False}
    auth = {"Authorization": f"Bearer {key}"}
    models: list[str] | None = None; voices: list[str] | None = None; live_success = False
    if provider == "openai":
        payload = await _get_json(hass, "https://api.openai.com/v1/models", auth)
        if isinstance(payload, dict):
            live_success = True
            available = {item.get("id") for item in payload.get("data", []) if isinstance(item, dict)}
            models = [model for model in fallback["models"] if model in available]
        # OpenAI Realtime voices are a documented curated list, not a models API.
        voices = list(fallback["voices"])
    elif provider == "xai":
        model_payload = await _get_json(hass, "https://api.x.ai/v1/models", auth)
        voice_payload = await _get_json(hass, "https://api.x.ai/v1/tts/voices", auth)
        if isinstance(model_payload, dict):
            live_success = True
            available = {item.get("id") for item in model_payload.get("data", []) if isinstance(item, dict)}
            models = [model for model in fallback["models"] if model in available]
        if isinstance(voice_payload, dict):
            live_success = True
            values = [item.get("voice_id") for item in voice_payload.get("voices", []) if isinstance(item, dict)]
            voices = [value for value in values if isinstance(value, str) and value.islower()]
    else:
        payload = await _get_json(hass, "https://generativelanguage.googleapis.com/v1beta/models", params={"key": key})
        if isinstance(payload, dict):
            live_success = True
            values = payload.get("models", [])
            models = [item.get("name") for item in values if isinstance(item, dict) and "bidiGenerateContent" in item.get("supportedGenerationMethods", [])]
        # Gemini exposes no Live-only voice catalog endpoint. Keep the explicit fallback.
        voices = list(fallback["voices"])
    model_values = models if models else list(fallback["models"])
    voice_values = voices if voices else list(fallback["voices"])
    return {"provider": provider, "key_configured": True, "models": model_values, "voices": voice_values, "source": "live" if live_success else "bundled", "checked_at": checked_at, "validated": live_success}
