from types import SimpleNamespace

from custom_components.tacktab.voice_migration import OLD_TIMER_EVENT, build_plan, scan_references


def test_plan_detects_old_satellite_and_reports_settings_and_references() -> None:
    state = SimpleNamespace(
        state="idle", config_entry_id="old", attributes={
            "friendly_name": "Kitchen browser", "pipeline": "Home Assistant", "wake_word_model": "ok_nabu",
            "wake_word_detection": "On Device (microWakeWord)", "vad_sensitivity": "Moderately sensitive", "mute_timers": False,
        },
    )
    docs = {"automation": [{"id": "notify", "trigger": {"event_type": OLD_TIMER_EVENT}, "action": {"target": {"entity_id": "assist_satellite.kitchen_browser"}}}], "script": {"other": {"sequence": []}}}
    plan = build_plan({"assist_satellite.kitchen_browser": state}, [SimpleNamespace(entry_id="old", domain="voice_satellite")], docs)

    candidate = plan["candidates"][0]
    assert candidate["name"] == "Kitchen browser"
    assert candidate["settings"]["assistant"] == "Home Assistant"
    assert candidate["settings"]["wake_word"] == "ok_nabu"
    assert candidate["references"] == [{"kind": "automation", "id": "notify", "name": "notify"}]
    assert plan["timer_event_mapping"]["from"] == OLD_TIMER_EVENT


def test_reference_scan_never_changes_source_documents() -> None:
    documents = {"automation": [{"alias": "Timer", "trigger": {"event_type": OLD_TIMER_EVENT}}], "script": {"hello": {"sequence": [{"service": "assist_satellite.announce"}]}}}
    before = repr(documents)
    assert scan_references(documents, "assist_satellite.kitchen") == [{"kind": "automation", "id": "Timer", "name": "Timer"}]
    assert repr(documents) == before
