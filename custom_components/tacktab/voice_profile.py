"""Fleet voice profile: schema, validation and pure document logic.

The profile is the part of a tablet's voice settings that is meant to be the
same across a fleet of tablets. It is deliberately dependency-free (no Home
Assistant imports) so the wire contract can be tested on its own.

Rules the rest of the integration relies on:

* A document is ``{schema_version, revision, updated_at, updated_by,
  group_revisions, groups}``. ``revision`` rises by one on every accepted change.
* Only the groups in :data:`GROUPS` exist. Inside a group, keys this release
  knows are validated; any other key from a newer app is kept verbatim and
  returned unchanged (forward compatibility), provided it is plain bounded JSON.
* Per-device keys (:data:`EXCLUDED_KEYS`) and anything that looks like a
  credential are never stored, even when an app sends them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import json
import math
import re
from typing import Any

from .voice_protocol import VoiceProtocolError

SCHEMA_VERSION = 1

GROUP_VOICE = "voice"
GROUP_APPEARANCE = "appearance"
GROUP_CONVERSATION = "conversation"
GROUP_TIMERS = "timers"
GROUP_CHIMES = "chimes"
GROUPS = (GROUP_VOICE, GROUP_APPEARANCE, GROUP_CONVERSATION, GROUP_TIMERS, GROUP_CHIMES)

MAX_KEY_LENGTH = 64
MAX_KEYS_PER_GROUP = 128
MAX_GROUP_BYTES = 8 * 1024
MAX_DOCUMENT_BYTES = 48 * 1024
MAX_STRING_LENGTH = 512
MAX_DEPTH = 3
MAX_CONTAINER_ITEMS = 32

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Names that are never shared, at any depth: credentials, routing and per-device
# hardware or identity. Matched on word boundaries so ordinary names survive.
_SECRET_RE = re.compile(
    r"(token|secret|password|passwd|credential|api_?key|private_?key"
    r"|(^|_)(pipelines?|outputs?|media_players?|arbitration|areas?)(_|$)"
    r"|mic(rophone)?_?gain|input_gain|device_name)",
    re.IGNORECASE,
)
_CHIME_EVENTS = ("wake", "done", "error", "timer", "announcement")

# Kiosk Satellite keeps hardware-dependent settings per device; so do we. The
# second half are identity, routing and credential fields that must never be
# copied between tablets or stored in Home Assistant by the profile.
EXCLUDED_KEYS = frozenset({
    "arbitration_offset_db", "arbitration_offset", "microphone_gain", "mic_gain", "input_gain",
    "device_name", "name", "area", "area_id", "device_id", "install_id",
    "access_token", "home_assistant_url", "pipeline_id", "pipeline_id_2",
    "output_target", "output_target_2", "media_player_entity_id", "media_player_entity_id_2",
    "enabled", "muted", "hands_free", "announcements_enabled", "background_listening",
})


# Inside a value, generic names ("name", "enabled", "muted", ...) are ordinary
# data, so only the identity and routing names are excluded there.
NESTED_EXCLUDED_KEYS = EXCLUDED_KEYS - {"name", "enabled", "muted", "hands_free", "announcements_enabled", "background_listening"}


class ProfileError(VoiceProtocolError):
    """A safe, structured profile failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _fail(message: str, code: str = "invalid_profile") -> None:
    raise ProfileError(code, message)


Validator = Callable[[Any], None]


def _flag(value: Any) -> None:
    if type(value) is not bool:
        _fail("Expected a boolean")


def _int_range(low: int, high: int) -> Validator:
    def check(value: Any) -> None:
        if type(value) is not int or not low <= value <= high:
            _fail(f"Expected an integer from {low} to {high}")

    return check


def _float_range(low: float, high: float) -> Validator:
    def check(value: Any) -> None:
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            _fail(f"Expected a number from {low} to {high}")

    return check


def _choice(*options: str) -> Validator:
    def check(value: Any) -> None:
        if value not in options:
            _fail("Unsupported option")

    return check


def _text(maximum: int = 128, *, allow_empty: bool = False) -> Validator:
    def check(value: Any) -> None:
        if type(value) is not str or len(value) > maximum or (not value and not allow_empty):
            _fail("Expected a short string")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            _fail("Control characters are not allowed")

    return check


_SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _slug(value: Any) -> None:
    if type(value) is not str or not _SLUG.match(value):
        _fail("Expected a lowercase identifier")


def _chime_specs() -> dict[str, Validator]:
    specs: dict[str, Validator] = {"chimes_enabled": _flag, "chime_volume": _float_range(0, 1)}
    for event in _CHIME_EVENTS:
        specs[f"{event}_chime_id"] = _text()
        specs[f"{event}_chime_enabled"] = _flag
        specs[f"{event}_chime_volume"] = _float_range(0, 1)
    return specs


# Keys this release validates, with the same ranges the apps enforce locally.
KNOWN_KEYS: dict[str, dict[str, Validator]] = {
    GROUP_VOICE: {
        "wake_engine": _choice("micro_wake_word", "open_wake_word"),
        "wake_word_1": _text(),
        "wake_word_2": _text(allow_empty=True),
        "wake_sensitivity": _float_range(0, 1),
        "wake_fine_tune": _int_range(-10, 10),
        "stop_fine_tune": _int_range(-10, 10),
        "stop_word_enabled": _flag,
        "skip_chime_after_wake": _flag,
        "follow_up_enabled": _flag,
        "follow_up_delay_ms": _int_range(0, 3000),
        "follow_up_chime": _flag,
    },
    GROUP_APPEARANCE: {
        "appearance": _text(),
        "skin_1": _slug,
        "skin_2": _slug,
        "theme": _choice("system", "light", "dark"),
        "background_dim": _float_range(0, 0.85),
        "text_scale": _float_range(0.8, 1.5),
        "reactive_activity_bar": _flag,
    },
    GROUP_CONVERSATION: {
        "show_transcript": _flag,
        "show_response": _flag,
        "show_tool_activity": _flag,
        "hide_sentiment_tags": _flag,
        "retain_answer_seconds": _int_range(0, 300),
    },
    GROUP_TIMERS: {
        "show_timers": _flag,
        "show_timer_names": _flag,
        "show_finished_timers": _flag,
        "timer_scale": _float_range(0.75, 1.5),
        "timer_alert_muted": _flag,
        "speak_timer_finished": _flag,
    },
    GROUP_CHIMES: _chime_specs(),
}


def is_excluded(key: str) -> bool:
    """True for per-device, identity and credential keys."""
    return key.lower() in EXCLUDED_KEYS or _SECRET_RE.search(key) is not None


def strip_excluded(value: Any, path: str, dropped: list[str]) -> Any:
    """Return ``value`` without excluded or secret-like keys at any depth.

    Walks dicts and the dicts inside lists. Each removed entry is recorded in
    ``dropped`` as its path, e.g. ``voice.future_provider.api_key`` or
    ``voice.future[2].token``. Generic names such as ``name`` are excluded only at
    the top of a group, where they would be a device field, not inside a value.
    """
    if type(value) is dict:
        kept: dict[Any, Any] = {}
        for key, item in value.items():
            if type(key) is str and (key.lower() in NESTED_EXCLUDED_KEYS or _SECRET_RE.search(key) is not None):
                dropped.append(f"{path}.{key}")
                continue
            kept[key] = strip_excluded(item, f"{path}.{key}", dropped)
        return kept
    if type(value) is list:
        return [strip_excluded(item, f"{path}[{index}]", dropped) for index, item in enumerate(value)]
    return value


def _json_value(value: Any, depth: int = 0) -> None:
    """Accept only bounded plain JSON for keys this release does not know."""
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > 2**53:
            _fail("Number is too large")
    elif type(value) is float:
        if not math.isfinite(value):
            _fail("Number must be finite")
    elif type(value) is str:
        if len(value) > MAX_STRING_LENGTH:
            _fail("String is too long")
    elif type(value) is list:
        if depth >= MAX_DEPTH or len(value) > MAX_CONTAINER_ITEMS:
            _fail("List is too large")
        for item in value:
            _json_value(item, depth + 1)
    elif type(value) is dict:
        if depth >= MAX_DEPTH or len(value) > MAX_CONTAINER_ITEMS:
            _fail("Object is too large")
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_KEY_LENGTH:
                _fail("Invalid object key")
            _json_value(item, depth + 1)
    else:
        _fail("Unsupported value")


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True))


def check_known_or_unknown(group: str, key: str, value: Any) -> None:
    """Validate one value: strictly when the key is known, shape-only otherwise."""
    validator = KNOWN_KEYS[group].get(key)
    if validator is not None:
        validator(value)
        return
    if not _KEY_RE.match(key):
        _fail("Invalid key name")
    _json_value(value)


def sanitize_groups(groups: Any, *, allow_empty: bool = False) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Validate a partial update and return ``(clean, dropped)``.

    ``dropped`` lists ``group.key`` entries removed because they are per-device
    or credential-like. Everything else invalid rejects the whole update.
    """
    if type(groups) is not dict or (not groups and not allow_empty):
        _fail("Expected at least one group")
    clean: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for group, values in groups.items():
        if group not in GROUPS:
            _fail("Unknown voice profile group")
        if type(values) is not dict or len(values) > MAX_KEYS_PER_GROUP:
            _fail("Invalid group")
        kept: dict[str, Any] = {}
        for key, value in values.items():
            if type(key) is not str:
                _fail("Invalid key name")
            if is_excluded(key):
                dropped.append(f"{group}.{key}")
                continue
            if key not in KNOWN_KEYS[group]:
                value = strip_excluded(value, f"{group}.{key}", dropped)
            check_known_or_unknown(group, key, value)
            kept[key] = deepcopy(value)
        clean[group] = kept
    return clean, sorted(dropped)


def sanitize_removals(remove: Any) -> dict[str, list[str]]:
    """Validate ``{group: [key, ...]}`` removals (administrator only)."""
    if remove is None:
        return {}
    if type(remove) is not dict:
        _fail("Invalid removal")
    clean: dict[str, list[str]] = {}
    for group, keys in remove.items():
        if group not in GROUPS or type(keys) is not list or len(keys) > MAX_KEYS_PER_GROUP:
            _fail("Invalid removal")
        for key in keys:
            if type(key) is not str or not _KEY_RE.match(key):
                _fail("Invalid removal")
        clean[group] = list(dict.fromkeys(keys))
    return clean


def empty_document() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "updated_at": None,
        "updated_by": None,
        "group_revisions": {group: 0 for group in GROUPS},
        "groups": {group: {} for group in GROUPS},
    }


def normalize_document(raw: Any) -> dict[str, Any]:
    """Repair a stored document. Invalid entries are dropped, not trusted."""
    doc = empty_document()
    if not isinstance(raw, Mapping):
        return doc
    revision = raw.get("revision")
    doc["revision"] = revision if type(revision) is int and revision >= 0 else 0
    if isinstance(raw.get("updated_at"), str):
        doc["updated_at"] = raw["updated_at"]
    if isinstance(raw.get("updated_by"), Mapping):
        doc["updated_by"] = {str(k): v for k, v in raw["updated_by"].items() if isinstance(v, (str, type(None)))}
    stored_groups = raw.get("groups") if isinstance(raw.get("groups"), Mapping) else {}
    stored_revisions = raw.get("group_revisions") if isinstance(raw.get("group_revisions"), Mapping) else {}
    for group in GROUPS:
        values = stored_groups.get(group)
        if isinstance(values, Mapping):
            for key, value in values.items():
                try:
                    if type(key) is str and not is_excluded(key):
                        if key not in KNOWN_KEYS[group]:
                            value = strip_excluded(value, f"{group}.{key}", [])
                        check_known_or_unknown(group, key, value)
                        doc["groups"][group][key] = deepcopy(value)
                except ProfileError:
                    continue
        group_revision = stored_revisions.get(group)
        if type(group_revision) is int and 0 <= group_revision <= doc["revision"]:
            doc["group_revisions"][group] = group_revision
        elif doc["groups"][group]:
            # Documents written before per-group revisions existed.
            doc["group_revisions"][group] = doc["revision"]
    return doc


def apply_update(
    doc: Mapping[str, Any],
    groups: Mapping[str, Mapping[str, Any]],
    removals: Mapping[str, list[str]],
    writer: dict[str, Any],
    now: str,
) -> tuple[dict[str, Any], list[str]]:
    """Merge a validated update. Return ``(new_doc, changed_groups)``.

    Keys are merged one by one, so a key this app does not know (written by a
    newer app) survives an update from an older app. No change means no
    revision bump.
    """
    new = deepcopy(dict(doc))
    changed: list[str] = []
    for group in GROUPS:
        current = new["groups"][group]
        before = deepcopy(current)
        current.update(deepcopy(groups.get(group, {})))
        for key in removals.get(group, ()):
            current.pop(key, None)
        if current != before:
            if len(current) > MAX_KEYS_PER_GROUP or _size(current) > MAX_GROUP_BYTES:
                _fail("Group is too large")
            changed.append(group)
    if not changed:
        return new, []
    if _size(new["groups"]) > MAX_DOCUMENT_BYTES:
        _fail("Profile is too large")
    new["revision"] = doc["revision"] + 1
    new["updated_at"] = now
    new["updated_by"] = writer
    for group in changed:
        new["group_revisions"][group] = new["revision"]
    return new, changed


def view(doc: Mapping[str, Any], groups: tuple[str, ...] | list[str] | None = None) -> dict[str, Any]:
    """The wire representation: metadata plus the requested group contents."""
    selected = GROUPS if groups is None else tuple(group for group in GROUPS if group in groups)
    return {
        "schema_version": doc["schema_version"],
        "revision": doc["revision"],
        "updated_at": doc["updated_at"],
        "updated_by": deepcopy(doc["updated_by"]),
        "group_revisions": dict(doc["group_revisions"]),
        "groups": {group: deepcopy(doc["groups"][group]) for group in selected},
    }
