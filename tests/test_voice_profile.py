"""Pure fleet voice profile rules: schema, exclusions, unknown keys, merges, normalisation."""

from copy import deepcopy

import pytest

from custom_components.tacktab.voice_profile import (
    EXCLUDED_KEYS,
    GROUPS,
    ProfileError,
    apply_update,
    empty_document,
    normalize_document,
    sanitize_groups,
    sanitize_removals,
    view,
)
from custom_components.tacktab.voice_protocol import VoiceProtocolError, validate_message
from tests.test_voice_protocol import CONNECT

WRITER = {"type": "tablet", "install_id": "a", "name": "Kitchen"}


@pytest.mark.parametrize(
    ("group", "key", "value"),
    [
        ("voice", "wake_sensitivity", 1.5),
        ("voice", "wake_sensitivity", float("nan")),
        ("voice", "wake_fine_tune", 11),
        ("voice", "wake_fine_tune", True),
        ("voice", "wake_engine", "other"),
        ("voice", "follow_up_delay_ms", 3001),
        ("appearance", "theme", "neon"),
        ("appearance", "text_scale", 5),
        ("appearance", "background_dim", 0.9),
        ("appearance", "skin_1", "Red Eye"),
        ("conversation", "retain_answer_seconds", -1),
        ("conversation", "show_response", "yes"),
        ("timers", "timer_scale", 0.1),
        ("chimes", "chime_volume", 2),
        ("chimes", "wake_chime_enabled", 1),
        ("chimes", "done_chime_id", ""),
    ],
)
def test_known_keys_are_validated_like_the_apps_validate_them(group: str, key: str, value: object) -> None:
    with pytest.raises(ProfileError) as err:
        sanitize_groups({group: {key: value}})
    assert err.value.code == "invalid_profile"


def test_every_syncable_group_accepts_a_valid_sample() -> None:
    clean, dropped = sanitize_groups({
        "voice": {"wake_engine": "open_wake_word", "wake_word_1": "okay_nabu", "wake_word_2": "", "wake_sensitivity": 0.4,
                  "wake_fine_tune": -3, "stop_fine_tune": 2, "stop_word_enabled": True, "skip_chime_after_wake": True,
                  "follow_up_enabled": True, "follow_up_delay_ms": 750, "follow_up_chime": False},
        "appearance": {"appearance": "tacktab", "skin_1": "glow_orb", "skin_2": "four_dots", "theme": "dark",
                       "background_dim": 0.3, "text_scale": 1.2, "reactive_activity_bar": True},
        "conversation": {"show_transcript": True, "show_response": True, "show_tool_activity": False,
                         "hide_sentiment_tags": True, "retain_answer_seconds": 30},
        "timers": {"show_timers": True, "show_timer_names": False, "show_finished_timers": True, "timer_scale": 1.25,
                   "timer_alert_muted": False, "speak_timer_finished": True},
        "chimes": {"chimes_enabled": True, "chime_volume": 0.6, "wake_chime_id": "wake", "done_chime_enabled": False,
                   "timer_chime_volume": 1, "announcement_chime_id": "soft"},
    })
    assert set(clean) == set(GROUPS)
    assert dropped == []


@pytest.mark.parametrize("key", [
    "arbitration_offset_db", "microphone_gain", "device_name", "area", "access_token", "home_assistant_url",
    "pipeline_id", "media_player_entity_id", "muted", "wake_api_key", "client_secret",
])
def test_per_device_and_credential_keys_are_dropped_never_stored(key: str) -> None:
    clean, dropped = sanitize_groups({"voice": {key: 5, "wake_sensitivity": 0.5}})
    assert clean == {"voice": {"wake_sensitivity": 0.5}}
    assert dropped == [f"voice.{key}"]
    doc, changed = apply_update(empty_document(), clean, {}, WRITER, "now")
    assert key not in doc["groups"]["voice"] and changed == ["voice"]


def test_excluded_set_covers_the_documented_per_device_keys() -> None:
    assert {"arbitration_offset_db", "microphone_gain", "device_name", "area"} <= EXCLUDED_KEYS


def test_excluded_keys_in_stored_documents_are_stripped_on_load() -> None:
    doc = normalize_document({"revision": 3, "groups": {"voice": {"arbitration_offset_db": 4, "wake_sensitivity": 0.2}}})
    assert doc["groups"]["voice"] == {"wake_sensitivity": 0.2}


def test_unknown_keys_from_newer_apps_are_kept_verbatim() -> None:
    future = {"wake_future_knob": {"nested": [1, 2.5, None, "x"], "on": True}, "wake_next_mode": "turbo"}
    clean, dropped = sanitize_groups({"voice": {**future, "wake_sensitivity": 0.7}})
    doc, changed = apply_update(empty_document(), clean, {}, WRITER, "now")
    assert dropped == [] and changed == ["voice"]
    assert doc["groups"]["voice"] == {**future, "wake_sensitivity": 0.7}
    assert view(doc)["groups"]["voice"]["wake_future_knob"] == future["wake_future_knob"]


def test_an_older_app_update_does_not_erase_a_newer_apps_keys() -> None:
    first, _ = apply_update(empty_document(), {"voice": {"wake_future_knob": 9}}, {}, WRITER, "t1")
    second, changed = apply_update(first, {"voice": {"wake_sensitivity": 0.3}}, {}, WRITER, "t2")
    assert changed == ["voice"]
    assert second["groups"]["voice"] == {"wake_future_knob": 9, "wake_sensitivity": 0.3}


@pytest.mark.parametrize("groups", [
    {"mystery": {"a": 1}},
    {"voice": {"Bad-Key": 1}},
    {"voice": {"wake_future": object()}},
    {"voice": {"wake_future": "x" * 513}},
    {"voice": {"wake_future": [[[[1]]]]}},
    {"voice": {"wake_future": float("inf")}},
    {},
    {"voice": []},
])
def test_unknown_groups_and_malformed_unknown_values_are_rejected(groups: object) -> None:
    with pytest.raises(ProfileError):
        sanitize_groups(groups)


def test_group_size_limit() -> None:
    clean = {"voice": {f"extra_{i:03d}": "x" * 100 for i in range(100)}}
    with pytest.raises(ProfileError, match="too large"):
        apply_update(empty_document(), clean, {}, WRITER, "now")


def test_revision_and_group_revisions_only_move_for_real_changes() -> None:
    doc, changed = apply_update(empty_document(), {"voice": {"wake_sensitivity": 0.5}}, {}, WRITER, "t1")
    assert (doc["revision"], changed, doc["group_revisions"]["voice"], doc["updated_by"]) == (1, ["voice"], 1, WRITER)

    same, changed = apply_update(doc, {"voice": {"wake_sensitivity": 0.5}}, {}, {"type": "user"}, "t2")
    assert changed == [] and same["revision"] == 1 and same["updated_at"] == "t1" and same["updated_by"] == WRITER

    doc2, changed = apply_update(doc, {"timers": {"show_timers": False}}, {}, WRITER, "t3")
    assert changed == ["timers"] and doc2["group_revisions"] == {**doc["group_revisions"], "timers": 2}


def test_removals_delete_keys_and_validate_names() -> None:
    doc, _ = apply_update(empty_document(), {"voice": {"wake_future_knob": 1, "wake_sensitivity": 0.5}}, {}, WRITER, "t1")
    doc2, changed = apply_update(doc, {}, sanitize_removals({"voice": ["wake_future_knob"]}), WRITER, "t2")
    assert changed == ["voice"] and doc2["groups"]["voice"] == {"wake_sensitivity": 0.5}
    for bad in ({"nope": ["a"]}, {"voice": ["Bad Key"]}, {"voice": "a"}, []):
        with pytest.raises(ProfileError):
            sanitize_removals(bad)


def test_sparse_or_damaged_stored_documents_are_repaired() -> None:
    assert normalize_document(None) == empty_document()
    assert normalize_document("garbage") == empty_document()
    # A document without group revisions (older layout) gets them from its revision.
    doc = normalize_document({"revision": 4, "groups": {"voice": {"wake_sensitivity": 0.5}, "timers": {}}})
    assert doc["group_revisions"]["voice"] == 4 and doc["group_revisions"]["timers"] == 0
    damaged = normalize_document({
        "revision": -2, "groups": {"voice": {"wake_sensitivity": 9, "wake_fine_tune": 2}, "ghost": {"a": 1}},
        "group_revisions": {"voice": "x"}, "updated_by": {"name": "A", "bad": {"x": 1}},
    })
    assert damaged["revision"] == 0 and damaged["groups"]["voice"] == {"wake_fine_tune": 2}
    assert "ghost" not in damaged["groups"] and damaged["updated_by"] == {"name": "A"}


def test_view_never_aliases_the_document() -> None:
    doc, _ = apply_update(empty_document(), {"voice": {"wake_future_knob": [1]}}, {}, WRITER, "t")
    snapshot = view(doc)
    snapshot["groups"]["voice"]["wake_future_knob"].append(2)
    assert doc["groups"]["voice"]["wake_future_knob"] == [1]
    assert set(view(doc, ["voice"])["groups"]) == {"voice"}


# -- protocol negotiation -----------------------------------------------------
def _connect(protocol: int, **capabilities: bool) -> dict:
    message = deepcopy(CONNECT)
    message["protocol"] = protocol
    message["capabilities"].update(capabilities)
    return message


def test_voice_profile_capability_is_optional_so_older_apps_connect_unchanged() -> None:
    assert "voice_profile" not in validate_message(_connect(1))["capabilities"]
    assert "voice_profile" not in validate_message(_connect(2))["capabilities"]
    assert validate_message(_connect(2, voice_profile=True))["capabilities"]["voice_profile"] is True
    assert validate_message(_connect(2, voice_profile=False))["capabilities"]["voice_profile"] is False


def test_voice_profile_capability_needs_protocol_two_and_a_boolean() -> None:
    with pytest.raises(VoiceProtocolError) as err:
        validate_message(_connect(1, voice_profile=True))
    assert err.value.code == "unsupported_protocol"
    with pytest.raises(VoiceProtocolError):
        validate_message(_connect(2, voice_profile="yes"))
    with pytest.raises(VoiceProtocolError):
        validate_message(_connect(2, other_capability=True))


EPOCH = "00000000-0000-4000-8000-0000000000aa"
REQUEST = "00000000-0000-4000-8000-0000000000bb"


def test_profile_commands_have_a_strict_shape() -> None:
    get = {"id": 3, "type": "tacktab/voice/voice_profile_get", "epoch": EPOCH}
    assert validate_message(get) == get
    update = {"id": 4, "type": "tacktab/voice/voice_profile_update", "epoch": EPOCH, "request_id": REQUEST,
              "base_revision": 0, "groups": {"voice": {"wake_sensitivity": 0.5}}}
    assert validate_message(update) == update
    for bad in (
        {**update, "extra": 1},
        {**update, "base_revision": -1},
        {**update, "base_revision": True},
        {**update, "request_id": "not-a-uuid"},
        {**update, "groups": []},
        {k: v for k, v in update.items() if k != "groups"},
        {**get, "base_revision": 0},
    ):
        with pytest.raises(VoiceProtocolError):
            validate_message(bad)
