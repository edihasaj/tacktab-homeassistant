"""Safe, versioned conversation result cards for native Tacktab clients."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import re
import unicodedata


RESULT_VERSION = 1
MAX_TEXT = 512
MAX_URL = 2_048
_ABSOLUTE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)$", re.DOTALL)
_AUTHORITY = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[^\[\]:@\s/?#\\]+)(?::([0-9]{1,5}))?$")
_KINDS = frozenset(("text", "tool", "weather", "entity", "image", "video", "number"))


def result_from_intent(intent_output: Any) -> dict[str, Any] | None:
    """Extract one display-only card from a Core intent response.

    Core integrations may add arbitrary response data.  Only a small explicit
    vocabulary crosses the native boundary, and unknown values fall back to a
    plain text card rather than being reflected as provider markup.
    """
    if not isinstance(intent_output, Mapping):
        return None
    response = intent_output.get("response")
    data = response.get("data") if isinstance(response, Mapping) else None
    data = data if isinstance(data, Mapping) else intent_output.get("data")
    data = data if isinstance(data, Mapping) else {}
    speech = _speech(response)
    kind = _string(data.get("result_type") or data.get("type"))
    if kind == "weather" or isinstance(data.get("weather"), Mapping):
        weather = data.get("weather") if isinstance(data.get("weather"), Mapping) else data
        return _card("weather", title=_string(weather.get("title")) or "Weather", text=speech or _string(weather.get("forecast")),
                     condition=_string(weather.get("condition")), temperature=_first_number(weather, "temperature", "temp"),
                     high=_number(weather.get("high")), low=_number(weather.get("low")), forecast=_string(weather.get("forecast")))
    media_kind = "video" if kind == "video" else "image" if kind == "image" else None
    media_url = sanitize_media_url(data.get("url") or data.get("image_url") or data.get("video_url"))
    if media_kind and media_url:
        return _card(media_kind, title=_string(data.get("title")) or media_kind.title(), text=speech, url=media_url)
    if kind in {"stock", "number"} or _number(data.get("value")) is not None:
        return _card("number", title=_string(data.get("title")) or _string(data.get("symbol")) or "Result", text=speech,
                     value=_number(data.get("value")), unit=_string(data.get("unit")), change=_number(data.get("change")))
    if kind == "entity" or _string(data.get("state")):
        return _card("entity", title=_string(data.get("name")) or _string(data.get("entity_id")) or "Home Assistant", text=speech,
                     state=_string(data.get("state")), attributes=_safe_attributes(data.get("attributes")))
    return _card("text", title="Assistant", text=speech) if speech else None


def tool_use_result(name: str, result: Any, failed: bool) -> dict[str, Any]:
    """Return a concise, safe tool-use line for a realtime call."""
    title = _human_name(name)
    detail = _find_text(result) if isinstance(result, Mapping) else None
    text = detail or ("Couldn't run " if failed else "Ran ") + title
    return _card("tool", title=title, text=text, status="failed" if failed else "completed")


def sanitize_media_url(value: Any) -> str | None:
    """Apply the one Tacktab card URL policy shared with the Android and iOS apps.

    Allowed: ``https://host/...`` (scheme matched case-insensitively and
    normalized to lowercase) or a root-relative path such as ``/api/image_proxy/x``.
    Rejected: any other scheme (http, file, data), protocol-relative ``//host``,
    userinfo, whitespace/control characters, backslashes, malformed hosts or ports
    (``https://[``), and values longer than ``MAX_URL`` (never truncated, because a
    clipped URL is a different URL).  A rejected URL makes the caller fall back to
    the spoken text.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_URL or any(char.isspace() or char == "\\" or unicodedata.category(char) == "Cc" for char in text):
        return None
    if text.startswith("/"):
        return None if text.startswith("//") else text
    match = _ABSOLUTE.match(text)
    if match is None:
        return None
    scheme, authority, rest = match.groups()
    host = _AUTHORITY.match(authority)
    if scheme.lower() != "https" or host is None or (host.group(1) and int(host.group(1)) > 65535):
        return None
    return f"https://{authority}{rest}"


def _card(kind: str, *, title: str, text: str | None = None, **extra: Any) -> dict[str, Any]:
    card = {"version": RESULT_VERSION, "kind": kind if kind in _KINDS else "text", "title": title[:MAX_TEXT], "text": (text or "")[:MAX_TEXT]}
    card.update({key: value for key, value in extra.items() if value is not None})
    return card


def _speech(response: Any) -> str | None:
    if not isinstance(response, Mapping):
        return None
    speech = response.get("speech")
    plain = speech.get("plain") if isinstance(speech, Mapping) else None
    return _string(plain.get("speech")) if isinstance(plain, Mapping) else None


def _find_text(data: Mapping[str, Any]) -> str | None:
    for key in ("speech", "message", "response", "result", "summary"):
        text = _string(data.get(key))
        if text:
            return text
    return None


def _safe_attributes(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    output = {str(key)[:64]: text for key, raw in value.items() if (text := _string(raw, 160))}
    return dict(list(output.items())[:8]) or None


def _human_name(value: str) -> str:
    return " ".join(part for part in value.replace("__", " ").replace("_", " ").split() if part)[:MAX_TEXT] or "Home Assistant action"


def _string(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text and not any(ord(char) < 32 for char in text) else None


def _first_number(data: Mapping[str, Any], *keys: str) -> int | float | None:
    """Return the first valid number, keeping a legitimate zero (0 °C)."""
    for key in keys:
        number = _number(data.get(key))
        if number is not None:
            return number
    return None


def _number(value: Any) -> int | float | None:
    if type(value) in (int, float) and not isinstance(value, bool):
        return value if abs(value) < 1_000_000_000 else None
    return None
