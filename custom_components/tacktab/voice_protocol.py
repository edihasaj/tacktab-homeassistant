"""Strict validation for the Tacktab voice websocket protocol versions 1 and 2."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .voice_models import SlotConfig, VoiceCapabilities, VoiceStatus, WakeWord

PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOLS = frozenset((1, 2))
PREFIX = "tacktab/voice/"
SLOT_IDS = frozenset(("primary", "secondary"))
MIC_PERMISSIONS = frozenset(("granted", "denied", "undetermined", "restricted"))
APP_STATES = frozenset(("foreground", "background"))


class VoiceProtocolError(ValueError):
    """A safe protocol error exposed through the standard websocket envelope."""

    def __init__(self, code: str = "invalid_format", message: str = "Invalid voice message") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(message: str = "Invalid voice message", code: str = "invalid_format") -> None:
    raise VoiceProtocolError(code, message)


def _strict_object(value: Any, required: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) - required - optional or required - set(value):
        _fail()
    return value


def _string(value: Any, *, minimum: int = 1, maximum: int = 128) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        _fail()
    return value


def _uuid(value: Any) -> str:
    value = _string(value, maximum=36)
    try:
        parsed = UUID(value)
    except (TypeError, ValueError):
        _fail()
    if str(parsed) != value.lower():
        _fail()
    return value


def _int(value: Any, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _fail()
    return value


def _number(value: Any, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        _fail()
    return float(value)


def parse_status(value: Any) -> VoiceStatus:
    obj = _strict_object(value, {"local_enabled", "mic_permission", "app_state", "capture_ready", "playback_ready"})
    if type(obj["local_enabled"]) is not bool or type(obj["capture_ready"]) is not bool or type(obj["playback_ready"]) is not bool:
        _fail()
    if obj["mic_permission"] not in MIC_PERMISSIONS or obj["app_state"] not in APP_STATES:
        _fail()
    return VoiceStatus(**obj)  # type: ignore[arg-type]


def parse_capabilities(value: Any) -> VoiceCapabilities:
    obj = _strict_object(value, {"announcements", "start_conversation", "timers", "local_wake_word", "pcm16_16khz_mono"})
    if any(type(obj[name]) is not bool for name in obj) or obj["pcm16_16khz_mono"] is not True:
        _fail()
    return VoiceCapabilities(**obj)  # type: ignore[arg-type]


def parse_slots(value: Any, *, allow_remote_output: bool = True) -> tuple[SlotConfig, ...]:
    if type(value) is not list or not 1 <= len(value) <= 2:
        _fail()
    slots: list[SlotConfig] = []
    wake_ids: set[str] = set()
    for raw in value:
        obj = _strict_object(raw, {"slot_id", "enabled", "pipeline_id", "wake_word", "output", "media_player_entity_id"})
        slot_id = obj["slot_id"]
        if slot_id not in SLOT_IDS or type(obj["enabled"]) is not bool:
            _fail()
        pipeline_id = obj["pipeline_id"]
        if pipeline_id is not None:
            _string(pipeline_id)
        output = obj["output"]
        media_player = obj["media_player_entity_id"]
        if output == "local" and media_player is not None:
            _fail()
        if output == "ha_media_player":
            if not allow_remote_output:
                _fail("Remote speaker output requires voice protocol 2", "unsupported_protocol")
            _string(media_player)
        if output not in ("local", "ha_media_player"):
            _fail()
        wake_word = obj["wake_word"]
        parsed_wake: WakeWord | None = None
        if wake_word is not None:
            parsed_wake = WakeWord(**_strict_object(wake_word, {"id", "phrase"}))
            _string(parsed_wake.id)
            _string(parsed_wake.phrase)
            if parsed_wake.id in wake_ids:
                _fail()
            wake_ids.add(parsed_wake.id)
        slots.append(SlotConfig(slot_id, obj["enabled"], pipeline_id, parsed_wake, output, media_player))
    ids = {slot.slot_id for slot in slots}
    if len(ids) != len(slots) or "primary" not in ids:
        _fail()
    return tuple(slots)


def validate_message(msg: Any) -> dict[str, Any]:
    """Validate a custom command and return a plain safe copy.

    The HA dispatcher validates its own outer message shape. This validator
    intentionally repeats it because all custom fields must reject unknown keys.
    """
    if type(msg) is not dict or type(msg.get("id")) is not int or msg["id"] < 1:
        _fail()
    kind = msg.get("type")
    if type(kind) is not str or not kind.startswith(PREFIX):
        _fail()
    command = kind.removeprefix(PREFIX)
    common = {"id", "type"}
    specs: dict[str, tuple[set[str], set[str]]] = {
        "connect": (common | {"protocol", "install_id", "name", "platform", "app_version", "capabilities", "status", "slots"}, set()),
        "configure": (common | {"epoch", "expected_config_revision", "slots"}, set()),
        "status": (common | {"epoch", "status"}, set()),
        "snapshot": (common | {"epoch"}, set()),
        "claim": (common | {"epoch", "claim_id", "slot_id", "wake_word_phrase", "snr_db", "confidence"}, set()),
        "run": (common | {"epoch", "run_id", "slot_id", "trigger", "pipeline_id"}, {"grant_id", "wake_word_phrase"}),
        "audio_end": (common | {"epoch", "run_id"}, set()),
        "cancel": (common | {"epoch", "run_id"}, set()),
        "cancel_output": (common | {"epoch", "action_id"}, set()),
        "ack": (common | {"epoch", "action_id", "status"}, {"error"}),
        "timer_action": (common | {"epoch", "request_id", "timer_id", "action"}, {"seconds"}),
    }
    if command not in specs:
        _fail()
    obj = dict(_strict_object(msg, *specs[command]))
    if command == "connect":
        if obj["protocol"] not in SUPPORTED_PROTOCOLS:
            _fail("Unsupported voice protocol", "unsupported_protocol")
        _uuid(obj["install_id"])
        _string(obj["name"], maximum=100)
        _string(obj["app_version"])
        if obj["platform"] not in ("android", "ios"):
            _fail()
        parse_capabilities(obj["capabilities"])
        parse_status(obj["status"])
        parse_slots(obj["slots"], allow_remote_output=obj["protocol"] >= 2)
        return obj
    _uuid(obj["epoch"])
    if command == "configure":
        _int(obj["expected_config_revision"])
        parse_slots(obj["slots"])
    elif command == "status":
        parse_status(obj["status"])
    elif command in ("audio_end", "cancel"):
        _uuid(obj["run_id"])
    elif command == "cancel_output":
        _uuid(obj["action_id"])
    elif command == "claim":
        _uuid(obj["claim_id"])
        if obj["slot_id"] not in SLOT_IDS:
            _fail()
        _string(obj["wake_word_phrase"])
        _number(obj["snr_db"], minimum=-20, maximum=80)
        if obj["confidence"] is not None:
            _number(obj["confidence"], minimum=0, maximum=1)
    elif command == "run":
        _uuid(obj["run_id"])
        if obj["slot_id"] not in SLOT_IDS or obj["trigger"] not in ("button", "wake_word", "follow_up"):
            _fail()
        if obj["pipeline_id"] is not None:
            _string(obj["pipeline_id"])
        wake = obj["trigger"] == "wake_word"
        if wake != ("grant_id" in obj and "wake_word_phrase" in obj):
            _fail()
        if wake:
            _uuid(obj["grant_id"])
            _string(obj["wake_word_phrase"])
    elif command == "ack":
        _uuid(obj["action_id"])
        failed = obj["status"] == "failed"
        if obj["status"] not in ("finished", "failed", "cancelled") or failed != ("error" in obj):
            _fail()
        if failed and obj["error"] not in {"playback_failed", "unsupported_media", "local_disabled", "mic_permission", "timeout"}:
            _fail()
    elif command == "timer_action":
        _uuid(obj["request_id"])
        _string(obj["timer_id"])
        add = obj["action"] in ("add", "subtract")
        if obj["action"] not in {"pause", "resume", "cancel", "dismiss", "add", "subtract"} or add != ("seconds" in obj):
            _fail()
        if add:
            _int(obj["seconds"], minimum=1, maximum=604800)
    return obj
