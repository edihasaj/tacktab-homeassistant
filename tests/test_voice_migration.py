"""Voice Satellite migration discovery against upstream-shaped registry entities."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.voice_migration import (
    OLD_TIMER_EVENT, async_build_plan, normalize_conversation, normalize_settings, normalize_wake_detection, scan_references,
)
from custom_components.tacktab.voice_migration_refs import _read_yaml

ENTRY = "01JABC"
UPSTREAM_BUILTIN_WAKE_WORD = "ok_nabu"  # the id upstream really registers, not Tacktab's "okay_nabu"


def _old_satellite(hass: HomeAssistant, *, pipeline: str = "Preferred", session: str = "Isolated", **extra: str) -> None:
    """Register what the real integration registers: unique ids from select.py/switch.py."""
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain="voice_satellite", entry_id=ENTRY)
    entry.add_to_hass(hass)
    registry.async_get_or_create("assist_satellite", "voice_satellite", ENTRY, suggested_object_id="kitchen", config_entry=entry)
    states = {
        "select.kitchen_pipeline": ("select", f"{ENTRY}-pipeline", pipeline),
        "select.kitchen_finished": ("select", f"{ENTRY}-vad_sensitivity", "aggressive"),
        "select.kitchen_session": ("select", f"{ENTRY}_session_duration", session),
        "select.kitchen_detect": ("select", f"{ENTRY}_wake_word_detection", extra.get("detection", "On Device (microWakeWord)")),
        "select.kitchen_wake": ("select", f"{ENTRY}_wake_word_model", extra.get("wake", "hey_jarvis")),
        "switch.kitchen_mute_timers": ("switch", f"{ENTRY}_mute_timers", "on"),
    }
    for entity_id, (domain, unique_id, value) in states.items():
        registry.async_get_or_create(domain, "voice_satellite", unique_id, suggested_object_id=entity_id.split(".")[1], config_entry=entry)
        hass.states.async_set(entity_id, value)
    # Real Assist satellites hold the pipeline select's *display* value as an attribute.
    hass.states.async_set("assist_satellite.kitchen", "idle", {"friendly_name": "Kitchen browser", "pipeline": pipeline})


async def test_async_plan_reads_upstream_children_into_one_typed_shape(hass: HomeAssistant) -> None:
    _old_satellite(hass)
    pipelines = [SimpleNamespace(id="01HOME", name="Home Assistant")]
    with patch("homeassistant.components.assist_pipeline.async_get_pipelines", return_value=pipelines):
        plan = await async_build_plan(hass)

    (candidate,) = plan["candidates"]
    settings = candidate["settings"]
    assert candidate["entity_id"] == "assist_satellite.kitchen" and candidate["name"] == "Kitchen browser"
    assert settings["assistant"] == "Preferred" and settings["assistant_preferred"] is True
    assert settings["finished_speaking"] == "aggressive"
    assert settings["conversation"] == "isolated" and settings["conversation_seconds"] is None
    assert settings["wake_word"] == "hey_jarvis"
    assert (settings["wake_detection"], settings["wake_engine"], settings["wake_engine_supported"]) == ("local", "microwakeword", True)
    assert settings["timer_muted"] is True
    assert "timer_visible" not in settings, "upstream has no such entity; unknown values must not be invented"


async def test_async_plan_resolves_a_named_assistant_to_its_stable_id(hass: HomeAssistant) -> None:
    _old_satellite(hass, pipeline="home assistant")
    pipelines = [SimpleNamespace(id="01HOME", name="Home Assistant")]
    with patch("homeassistant.components.assist_pipeline.async_get_pipelines", return_value=pipelines):
        settings = (await async_build_plan(hass))["candidates"][0]["settings"]
    assert settings["assistant_pipeline_id"] == "01HOME" and settings["assistant_preferred"] is False


async def test_async_plan_reports_unsupported_wake_engine_and_ignores_other_integrations(hass: HomeAssistant) -> None:
    _old_satellite(hass, detection="On Device (openWakeWord)")
    other = er.async_get(hass)
    other.async_get_or_create("assist_satellite", "esphome", "other", suggested_object_id="other")
    hass.states.async_set("assist_satellite.other", "idle")
    with patch("homeassistant.components.assist_pipeline.async_get_pipelines", return_value=[]):
        plan = await async_build_plan(hass)
    assert [item["entity_id"] for item in plan["candidates"]] == ["assist_satellite.kitchen"]
    assert plan["candidates"][0]["settings"]["wake_engine_supported"] is False
    assert {"code": "unsupported_wake_engine", "detail": "On Device (openWakeWord)"} in plan["candidates"][0]["warnings"]


async def test_async_plan_finds_references_in_loaded_automations_and_marks_gaps(hass: HomeAssistant) -> None:
    _old_satellite(hass)
    loaded = SimpleNamespace(entities=[
        SimpleNamespace(entity_id="automation.ping", raw_config={"id": "ping", "alias": "Ping", "actions": [{"event_type": OLD_TIMER_EVENT}]}),
        SimpleNamespace(entity_id="automation.broken", raw_config=None),
    ])
    hass.data["automation"] = loaded
    hass.data["script"] = SimpleNamespace(entities=[])
    with patch("homeassistant.components.assist_pipeline.async_get_pipelines", return_value=[]):
        plan = await async_build_plan(hass)
    assert plan["candidates"][0]["references"] == [{"kind": "automation", "id": "ping", "name": "Ping"}]
    assert plan["references_complete"] is False
    assert any("automation.broken" in warning for warning in plan["reference_warnings"])


def test_yaml_with_secret_keeps_the_file_and_include_marks_it_incomplete(tmp_path) -> None:
    (tmp_path / "automations.yaml").write_text(
        "- id: a\n  alias: Uses secret\n  action:\n    - service: notify.x\n      data: !secret token\n"
        "    - event_type: voice_satellite_timer\n- id: b\n  action: !include extra.yaml\n"
    )
    documents, problems = _read_yaml(str(tmp_path))
    assert problems == [] and [item["id"] for item in documents["automation"]] == ["a", "b"]
    assert scan_references(documents, "assist_satellite.kitchen") == [{"kind": "automation", "id": "a", "name": "Uses secret"}]
    from custom_components.tacktab.voice_migration_refs import ReferenceDiscovery, _add_documents
    discovery = ReferenceDiscovery()
    _add_documents(discovery, documents)
    assert discovery.complete is False and "includes content" in discovery.warnings[0]


def test_unreadable_yaml_is_incomplete_not_empty(tmp_path) -> None:
    (tmp_path / "scripts.yaml").write_text("a: [unclosed")
    _documents, problems = _read_yaml(str(tmp_path))
    assert problems == ["scripts.yaml could not be read"]


def test_reference_scan_never_changes_source_documents() -> None:
    documents = {"automation": [{"alias": "Timer", "trigger": {"event_type": OLD_TIMER_EVENT}}], "script": {"hello": {"sequence": [{"service": "assist_satellite.announce"}]}}}
    before = repr(documents)
    assert scan_references(documents, "assist_satellite.kitchen") == [{"kind": "automation", "id": "Timer", "name": "Timer"}]
    assert repr(documents) == before


@pytest.mark.parametrize(("label", "mode", "engine", "supported"), [
    ("On Device (microWakeWord)", "local", "microwakeword", True),
    ("On Device", "local", "microwakeword", True),
    ("On Device (vsWakeWord)", "local", "vswakeword", False),
    ("Home Assistant", "server", None, True),
    ("Disabled", "disabled", None, True),
    ("surprise", None, None, None),
    ("unavailable", None, None, None),
])
def test_wake_detection_labels(label: str, mode: str | None, engine: str | None, supported: bool | None) -> None:
    result = normalize_wake_detection(label)
    assert (result["wake_detection"], result["wake_engine"], result["wake_engine_supported"]) == (mode, engine, supported)


@pytest.mark.parametrize(("value", "mode", "seconds"), [
    ("Isolated", "isolated", None), ("isolated", "isolated", None), ("persistent", "persistent", None),
    ("5_minutes", "timed", 300), ("1_hour", "timed", 3600), ("6_hours", "timed", 21600), (None, None, None), ("what", None, None),
])
def test_conversation_values(value: str | None, mode: str | None, seconds: int | None) -> None:
    result = normalize_conversation(value)
    assert (result["conversation"], result["conversation_seconds"]) == (mode, seconds)


def test_unknown_source_values_stay_none() -> None:
    settings = normalize_settings({"assistant": "unknown", "finished_speaking": "Moderately sensitive", "timer_muted": "unavailable"})
    assert settings["assistant"] is None and settings["finished_speaking"] is None and settings["timer_muted"] is None


async def test_upstream_ok_nabu_becomes_tacktabs_okay_nabu(hass: HomeAssistant) -> None:
    _old_satellite(hass, wake=UPSTREAM_BUILTIN_WAKE_WORD)
    with patch("homeassistant.components.assist_pipeline.async_get_pipelines", return_value=[]):
        settings = (await async_build_plan(hass))["candidates"][0]["settings"]
    assert settings["wake_word"] == "okay_nabu" and settings["wake_word_source"] == "ok_nabu"
    assert normalize_settings({"wake_word": "hey_jarvis"})["wake_word"] == "hey_jarvis"


@pytest.mark.parametrize(("value", "memory", "rounded"), [
    ("Isolated", "isolated", False), ("persistent", "until_restart", False),
    ("5_minutes", "5_minutes", False), ("10_minutes", "15_minutes", True), ("15_minutes", "15_minutes", False),
    ("30_minutes", "until_restart", True), ("6_hours", "until_restart", True), (None, None, False),
])
def test_session_duration_maps_to_conversation_memory(value: str | None, memory: str | None, rounded: bool) -> None:
    result = normalize_conversation(value)
    assert (result["conversation_memory"], result["conversation_memory_rounded"]) == (memory, rounded)
