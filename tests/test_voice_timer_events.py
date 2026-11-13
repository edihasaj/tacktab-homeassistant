"""The documented ``tacktab_voice_timer`` bus event and its dedupe rules."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN
from custom_components.tacktab.voice_setup import VOICE_ENTRY_KEY, VOICE_ENTRY_PLATFORM, VOICE_ENTRY_SLOTS
from custom_components.tacktab.voice_timers import TIMER_EVENT, VoiceTimerAdapter

INSTALL_ID = "00000000-0000-4000-8000-000000000301"
VOICE_DATA = {
    VOICE_ENTRY_KEY: INSTALL_ID,
    VOICE_ENTRY_PLATFORM: "ios",
    VOICE_ENTRY_SLOTS: [
        {
            "slot_id": "primary",
            "enabled": True,
            "pipeline_id": None,
            "wake_word": {"id": "okay_tacktab", "phrase": "Okay Tacktab"},
            "output": "local",
            "media_player_entity_id": None,
        }
    ],
}
TIMER_FIELDS = {
    "event_type",
    "timer_id",
    "name",
    "total_seconds",
    "seconds_left",
    "is_active",
    "device_id",
    "device_name",
}


async def _device(hass: HomeAssistant):
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"voice:{INSTALL_ID}", title="Hallway tablet", data=VOICE_DATA
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


@callback
def _recorder(events: list[dict]):
    def record(event) -> None:
        events.append(dict(event.data))

    return record


async def _start_timer(hass: HomeAssistant, device, *, seconds: int = 120, name: str = "Tea") -> str:
    await intent.async_handle(
        hass,
        "test",
        intent.INTENT_START_TIMER,
        slots={"seconds": {"value": seconds}, "name": {"value": name}},
        device_id=device.device_id,
    )
    return next(iter(hass.data["intent.timer"].timers))


async def test_timer_events_cover_the_full_lifecycle(hass: HomeAssistant) -> None:
    """Started, paused, resumed, cancelled, finished and dismissed each fire once."""
    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    adapter: VoiceTimerAdapter = device.timer_adapter
    manager = hass.data["intent.timer"]

    timer_id = await _start_timer(hass, device)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started"]
    started = events[0]
    assert set(started) == TIMER_FIELDS
    assert started["timer_id"] == timer_id
    assert started["name"] == "Tea"
    assert started["total_seconds"] == 120
    assert started["is_active"] is True
    assert started["device_id"] == device.device_id
    assert started["device_name"] == "Hallway tablet"
    assert isinstance(started["seconds_left"], int)

    adapter.action("pause-1", timer_id, "pause", None)
    await hass.async_block_till_done()
    adapter.action("resume-1", timer_id, "resume", None)
    await hass.async_block_till_done()
    adapter.action("add-1", timer_id, "add", 30)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "paused", "resumed", "updated"]
    assert events[1]["is_active"] is False
    assert events[2]["is_active"] is True
    assert events[3]["total_seconds"] == 120

    manager._timer_finished(timer_id)
    await hass.async_block_till_done()
    finished = events[-1]
    assert finished["event_type"] == "finished"
    assert finished["seconds_left"] == 0
    assert finished["is_active"] is False
    assert finished["total_seconds"] == 120

    adapter.action("dismiss-1", timer_id, "dismiss", None)
    await hass.async_block_till_done()
    assert events[-1]["event_type"] == "dismissed"
    assert events[-1]["timer_id"] == timer_id

    second = await _start_timer(hass, device, seconds=60, name="Bread")
    await hass.async_block_till_done()
    adapter.action("cancel-1", second, "cancel", None)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events[-2:]] == ["started", "cancelled"]
    assert events[-1]["timer_id"] == second
    assert len(events) == 8


async def test_repeated_and_replayed_handler_calls_do_not_duplicate_events(hass: HomeAssistant) -> None:
    """A reconnect snapshot or a replayed handler call must not re-fire finished."""
    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    adapter: VoiceTimerAdapter = device.timer_adapter
    manager = hass.data["intent.timer"]
    timer_id = await _start_timer(hass, device)
    manager._timer_finished(timer_id)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "finished"]
    finished = manager.timers.get(timer_id)
    assert finished is None

    # Reconnect: the adapter snapshot and a replay of the finish callback for a
    # timer the manager no longer holds must both stay silent.
    adapter.snapshot()
    adapter._snapshot_live()
    stale = adapter.ringing[timer_id]
    adapter._fire("finished", dict(stale))
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "finished"]

    # Dismiss is the one later transition for the same timer, and is idempotent.
    adapter.action("dismiss-1", timer_id, "dismiss", None)
    with_dismiss = list(events)
    assert with_dismiss[-1]["event_type"] == "dismissed"
    assert adapter.action("dismiss-1", timer_id, "dismiss", None) == adapter._request_results["dismiss-1"]
    await hass.async_block_till_done()
    assert events == with_dismiss


async def test_repeated_pause_and_resume_noops_do_not_duplicate_events(hass: HomeAssistant) -> None:
    """Core ignores an already-paused pause; the adapter must not invent an event."""
    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    adapter: VoiceTimerAdapter = device.timer_adapter
    timer_id = await _start_timer(hass, device)
    adapter.action("pause-1", timer_id, "pause", None)
    adapter.action("pause-2", timer_id, "pause", None)
    adapter.action("resume-1", timer_id, "resume", None)
    adapter.action("resume-2", timer_id, "resume", None)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "paused", "resumed"]


async def test_each_tablet_reports_its_own_device_identity(hass: HomeAssistant) -> None:
    """Two tablets share the bus event but never each other's identity."""
    first = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    from custom_components.tacktab.voice_bridge import VoiceDevice

    # A device whose registry ID is unknown (for example a removed entry) still
    # reports the tablet's own name rather than another tablet's identity.
    other = VoiceDevice(hass, "install-2", "Garage tablet", "android", ())
    second = VoiceTimerAdapter(hass, other, "ha-device-unregistered")
    second._fire(
        "started",
        {"timer_id": "t2", "name": "Car", "total_seconds": 30, "remaining_seconds": 30, "is_active": True},
    )
    first.timer_adapter._fire(
        "started",
        {"timer_id": "t1", "name": "Tea", "total_seconds": 60, "remaining_seconds": 60, "is_active": True},
    )
    assert [event["device_id"] for event in events] == ["ha-device-unregistered", first.device_id]
    assert events[0]["device_name"] == "Garage tablet"
    assert events[1]["device_name"] == "Hallway tablet"


async def test_reload_mid_timer_seeds_active_state_and_original_duration(hass: HomeAssistant) -> None:
    """A new adapter adopts existing Core timers instead of guessing their state."""
    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    timer_id = await _start_timer(hass, device)
    manager = hass.data["intent.timer"]
    old_adapter: VoiceTimerAdapter = device.timer_adapter

    # Simulate an integration reload while the timer is still running.
    old_adapter.close()
    reloaded = VoiceTimerAdapter(hass, device, device.device_id)
    reloaded.async_start()
    device.timer_adapter = reloaded
    try:
        # A Core pause outside the adapter (a voice intent or the UI) has no
        # adapter override; only the seeded prior state can label it "paused".
        manager.pause_timer(timer_id)
        await hass.async_block_till_done()
        assert manager.timers[timer_id].is_active is False
        assert [event["event_type"] for event in events] == ["started", "paused"]

        reloaded.action("resume-after-reload", timer_id, "resume", None)
        reloaded.action("add-after-reload", timer_id, "add", 30)
        await hass.async_block_till_done()
        assert [event["event_type"] for event in events] == ["started", "paused", "resumed", "updated"]
        assert manager.timers[timer_id].created_seconds == 150
        assert events[-1]["total_seconds"] == 120
        assert events[-1]["seconds_left"] > 120
    finally:
        reloaded.close()


async def test_unnamed_timer_publishes_an_empty_name(hass: HomeAssistant) -> None:
    """Docs promise a string; Core stores None for unnamed timers."""
    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    timer_id = await _start_timer(hass, device, name="")
    await hass.async_block_till_done()
    assert events[-1]["timer_id"] == timer_id
    assert events[-1]["name"] == ""


async def test_replayed_updated_after_pause_is_deduplicated(hass: HomeAssistant) -> None:
    """A replayed Core UPDATED keeps the pause state and must not re-fire."""
    from homeassistant.components.intent.timers import TimerEventType

    device = await _device(hass)
    events: list[dict] = []
    hass.bus.async_listen(TIMER_EVENT, _recorder(events))
    adapter: VoiceTimerAdapter = device.timer_adapter
    timer_id = await _start_timer(hass, device)
    adapter.action("pause-1", timer_id, "pause", None)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "paused"]

    timer = hass.data["intent.timer"].timers[timer_id]
    adapter._on_timer_event(TimerEventType.UPDATED, timer)
    adapter._on_timer_event(TimerEventType.UPDATED, timer)
    await hass.async_block_till_done()
    assert [event["event_type"] for event in events] == ["started", "paused"]
