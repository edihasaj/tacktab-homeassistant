"""Pure fleet voice profile rules: schema, exclusions, unknown keys, merges, normalisation."""

from copy import deepcopy
import unicodedata

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


# -- recursive exclusion ------------------------------------------------------
NESTED = {
    "future_provider": {
        "api_key": "x", "arbitration_offset_db": 9, "label": "keep",
        "inner": {"Token": "no", "access_token": "no", "keep_me": [1, 2], "deep": {"password": "p", "ok": True}},
        "devices": [{"device_name": "Kitchen", "area": "k", "mic_gain": 2, "name": "fine", "enabled": True}],
    },
    "future_list": [1, {"password": "p", "ok": True}],
}


def test_excluded_and_secret_keys_are_stripped_at_any_depth_with_paths() -> None:
    clean, dropped = sanitize_groups({"voice": dict(NESTED)})
    assert clean["voice"]["future_provider"] == {
        "label": "keep",
        "inner": {"keep_me": [1, 2], "deep": {"ok": True}},
        "devices": [{"name": "fine", "enabled": True}],
    }
    assert clean["voice"]["future_list"] == [1, {"ok": True}]
    assert dropped == sorted([
        "voice.future_provider.api_key",
        "voice.future_provider.arbitration_offset_db",
        "voice.future_provider.inner.Token",
        "voice.future_provider.inner.access_token",
        "voice.future_provider.inner.deep.password",
        "voice.future_list[1].password",
        "voice.future_provider.devices[0].device_name",
        "voice.future_provider.devices[0].area",
        "voice.future_provider.devices[0].mic_gain",
    ])


@pytest.mark.parametrize("key", [
    "client_secret", "refresh_token", "db_password", "service_credentials", "pipelines", "pipeline_id",
    "outputs", "output_target", "media_player_entity_id", "arbitration_offset_db", "mic_gain",
    "microphone_gain", "device_name", "area", "area_id",
])
def test_each_sensitive_family_is_stripped_from_nested_values(key: str) -> None:
    clean, dropped = sanitize_groups({"timers": {"future": [{key: "x", "kept": 1}]}})
    assert clean["timers"]["future"] == [{"kept": 1}]
    assert dropped == [f"timers.future[0].{key}"]


def test_stripped_nested_values_are_never_stored_or_viewed() -> None:
    clean, _ = sanitize_groups({"voice": dict(NESTED)})
    doc, _ = apply_update(empty_document(), clean, {}, WRITER, "now")
    text = str(view(doc))
    for needle in ("api_key", "arbitration", "access_token", "password", "device_name", "mic_gain"):
        assert needle not in text


def test_stored_documents_are_scrubbed_recursively_on_load() -> None:
    doc = normalize_document({"revision": 2, "groups": {"voice": dict(NESTED)}})
    assert "api_key" not in str(doc) and "arbitration" not in str(doc) and "password" not in str(doc)
    assert doc["groups"]["voice"]["future_provider"]["label"] == "keep"


def test_no_known_profile_key_is_itself_excluded() -> None:
    from custom_components.tacktab.voice_profile import KNOWN_KEYS, is_excluded

    assert [(g, k) for g, keys in KNOWN_KEYS.items() for k in keys if is_excluded(k)] == []


# -- key syntax allowlist and spelling variants -------------------------------
VARIANTS = [
    "api-key", "private-key", "deviceId", "device-id", "home-assistant-url", "apiKey", "API_KEY", "Api_Key",
    "pаssword",  # Cyrillic a
    "ａｐｉ＿ｋｅｙ",  # fullwidth "api_key"
    "ｔｏｋｅｎ",  # fullwidth "token"
    "access-token", "clientSecret", "mic-gain", "micGain", "install-id", "outputTarget", "area-id", "pipelineId",
    "Kitchen Area", "tok​en", "x" * 65, "1abc", "_abc", "has.dot", "has space", "bad\x00key", "",
]


@pytest.mark.parametrize("variant", VARIANTS)
def test_nested_spelling_variants_are_dropped_and_reported(variant: str) -> None:
    clean, dropped = sanitize_groups({"voice": {"future": {variant: "secret", "keep": 1, "list": [{variant: 1, "ok": 2}]}}})
    assert clean["voice"]["future"] == {"keep": 1, "list": [{"ok": 2}]}
    assert len(dropped) == 2 and all(path.startswith("voice.future") for path in dropped)
    assert "secret" not in str(clean)
    assert all(char.isprintable() for path in dropped for char in path)


@pytest.mark.parametrize("variant", VARIANTS)
def test_top_level_spelling_variants_are_never_stored(variant: str) -> None:
    try:
        clean, dropped = sanitize_groups({"voice": {variant: "secret", "wake_sensitivity": 0.5}})
    except ProfileError as err:
        assert err.code == "invalid_profile"  # a malformed name rejects the whole update
        return
    assert clean == {"voice": {"wake_sensitivity": 0.5}} and len(dropped) == 1
    doc, _ = apply_update(empty_document(), clean, {}, WRITER, "now")
    assert "secret" not in str(doc)


@pytest.mark.parametrize("variant", ["api-key", "deviceId", "device-id", "home-assistant-url", "private-key", "ａｐｉ＿ｋｅｙ"])
def test_denied_spellings_are_reported_not_just_rejected_at_the_top_level(variant: str) -> None:
    clean, dropped = sanitize_groups({"voice": {variant: 1, "wake_sensitivity": 0.5}})
    assert clean == {"voice": {"wake_sensitivity": 0.5}} and dropped == [f"voice.{variant}"]


def test_canonical_form_folds_case_width_and_separators() -> None:
    from custom_components.tacktab.voice_profile import canonical_key

    assert canonical_key("deviceId") == "device_id"
    assert canonical_key("home-assistant-url") == "home_assistant_url"
    assert canonical_key("ＡＰＩ＿Ｋｅｙ") == "api_key"
    assert canonical_key("APIKey") == "apikey"


def test_stored_documents_drop_nested_variants_on_load() -> None:
    doc = normalize_document({"revision": 1, "groups": {"voice": {"future": {"api-key": "x", "deviceId": "d", "keep": 1}}}})
    assert doc["groups"]["voice"] == {"future": {"keep": 1}}


# -- bounded traversal --------------------------------------------------------
def _deep(levels: int) -> dict:
    value: dict = {}
    for _ in range(levels):
        value = {"a": value}
    return value


@pytest.mark.parametrize("levels", [4, 50, 1000, 100_000])
def test_a_deep_value_is_a_structured_error_on_update(levels: int) -> None:
    with pytest.raises(ProfileError) as err:
        sanitize_groups({"voice": {"future": _deep(levels)}})
    assert err.value.code == "invalid_profile"


def test_deep_lists_and_wide_values_are_bounded_too() -> None:
    nested_list: list = []
    for _ in range(5000):
        nested_list = [nested_list]
    for value in (nested_list, [[0] * 32] * 32, {f"k{i}": 1 for i in range(33)}):
        with pytest.raises(ProfileError) as err:
            sanitize_groups({"voice": {"future": value}})
        assert err.value.code == "invalid_profile"


def test_values_at_the_limit_are_still_accepted() -> None:
    clean, dropped = sanitize_groups({"voice": {"future": {"a": {"b": [1, 2, 3]}}, "wide": {f"k{i}": i for i in range(32)}}})
    assert dropped == [] and clean["voice"]["future"] == {"a": {"b": [1, 2, 3]}}


def test_load_drops_a_malformed_deep_value_and_keeps_the_rest() -> None:
    doc = normalize_document({
        "revision": 3,
        "groups": {"voice": {"wake_sensitivity": 0.2, "future_deep": _deep(1000), "future_ok": {"a": 1}}, "timers": {"timer_scale": 1.1}},
    })
    assert doc["groups"]["voice"] == {"wake_sensitivity": 0.2, "future_ok": {"a": 1}}
    assert doc["groups"]["timers"] == {"timer_scale": 1.1} and doc["revision"] == 3


def test_a_cyrillic_lookalike_password_key_is_dropped_not_rewritten() -> None:
    """NFKC leaves Cyrillic "а" alone, and replacing it with "_" would give p_ssword."""
    key = "p\u0430ssword"
    assert key != "password" and unicodedata.normalize("NFKC", key) == key
    clean, dropped = sanitize_groups({"voice": {"future": {key: "hunter2", "keep": 1, "p_ssword": "x"}}})
    assert clean["voice"]["future"] == {"keep": 1, "p_ssword": "x"}  # no rewrite into another key
    assert dropped == [f"voice.future.{key}"]
    assert "hunter2" not in str(clean)


# -- trailing newline and huge integers ---------------------------------------
@pytest.mark.parametrize("key", ["safe\n", "future\n", "safe\r\n", "safe\n\n"])
def test_a_trailing_newline_does_not_pass_key_validation(key: str) -> None:
    clean, dropped = sanitize_groups({"voice": {"future": {key: 1, "keep": 2}}})
    assert clean["voice"]["future"] == {"keep": 2}
    assert len(dropped) == 1 and all(char.isprintable() for char in dropped[0])
    with pytest.raises(ProfileError):
        sanitize_groups({"voice": {key: 1}})
    assert normalize_document({"revision": 1, "groups": {"voice": {key: 1, "future": {key: 1, "keep": 2}}}})["groups"]["voice"] == {
        "future": {"keep": 2}
    }
    with pytest.raises(ProfileError):
        sanitize_removals({"voice": [key]})


@pytest.mark.parametrize("skin", ["glow_orb\n", "glow orb", "Glow_Orb"])
def test_identifier_values_reject_a_trailing_newline_too(skin: str) -> None:
    with pytest.raises(ProfileError):
        sanitize_groups({"appearance": {"skin_1": skin}})


@pytest.mark.parametrize(("group", "key"), [
    ("voice", "wake_sensitivity"), ("appearance", "text_scale"), ("timers", "timer_scale"), ("chimes", "chime_volume"),
])
@pytest.mark.parametrize("huge", [10**1000, -(10**1000)])
def test_huge_integers_are_invalid_profile_not_overflow(group: str, key: str, huge: int) -> None:
    with pytest.raises(ProfileError) as err:
        sanitize_groups({group: {key: huge}})
    assert err.value.code == "invalid_profile"


@pytest.mark.parametrize("huge", [10**1000, 2**53 + 1])
def test_huge_integers_in_unknown_values_are_invalid_profile(huge: int) -> None:
    for value in (huge, [huge], {"a": huge}):
        with pytest.raises(ProfileError) as err:
            sanitize_groups({"voice": {"future": value}})
        assert err.value.code == "invalid_profile"


def test_load_drops_only_the_huge_integer_and_keeps_healthy_data() -> None:
    doc = normalize_document({
        "revision": 4,
        "groups": {
            "voice": {"wake_sensitivity": 10**1000, "wake_fine_tune": 2, "future_big": 10**1000, "future_ok": {"a": 1}},
            "timers": {"timer_scale": 1.25},
        },
    })
    assert doc["groups"]["voice"] == {"wake_fine_tune": 2, "future_ok": {"a": 1}}
    assert doc["groups"]["timers"] == {"timer_scale": 1.25} and doc["revision"] == 4


def test_a_huge_integer_in_a_bridge_number_is_a_protocol_error() -> None:
    message = {
        "id": 5, "type": "tacktab/voice/claim", "epoch": EPOCH, "claim_id": REQUEST, "slot_id": "primary",
        "wake_word_phrase": "okay tacktab", "snr_db": 10**1000, "confidence": None,
    }
    with pytest.raises(VoiceProtocolError) as err:
        validate_message(message)
    assert err.value.code == "invalid_format"
    assert validate_message({**message, "snr_db": 12})["snr_db"] == 12
