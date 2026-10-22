"""Real Home Assistant 2026.10 runtime coverage for the native voice entry."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN
from custom_components.tacktab.voice_setup import VOICE_ENTRY_KEY, VOICE_ENTRY_PLATFORM, VOICE_ENTRY_SLOTS
from custom_components.tacktab.voice_models import VoiceCapabilities, VoiceStatus


VOICE_INSTALL_ID = "00000000-0000-4000-8000-000000000001"
VOICE_DATA = {
    VOICE_ENTRY_KEY: VOICE_INSTALL_ID,
    VOICE_ENTRY_PLATFORM: "ios",
    VOICE_ENTRY_SLOTS: [
        {
            "slot_id": "primary",
            "enabled": True,
            "pipeline_id": None,
            "wake_word": {"id": "okay_tacktab", "phrase": "Okay Tacktab"},
            "output": "local",
            "media_player_entity_id": None,
        },
        {
            "slot_id": "secondary",
            "enabled": True,
            "pipeline_id": None,
            "wake_word": {"id": "hey_tacktab", "phrase": "Hey Tacktab"},
            "output": "local",
            "media_player_entity_id": None,
        },
    ],
}


async def test_voice_entry_sets_up_real_satellites_and_pipeline_selects(hass: HomeAssistant) -> None:
    """A separate free voice entry creates two actual Assist satellites on one device."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"voice:{VOICE_INSTALL_ID}", title="Kitchen tablet", data=VOICE_DATA)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    satellites = hass.states.async_entity_ids("assist_satellite")
    selects = hass.states.async_entity_ids("select")
    assert len(satellites) == 2
    assert len([entity_id for entity_id in selects if "pipeline" in entity_id]) == 2
    assert entry.runtime_data.device_id
    assert entry.runtime_data.timer_adapter is not None


class _NativeSession:
    """Captures the same authoritative events a connected tablet receives."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)

    async def close(self, _reason: str) -> None:
        return None

    async def play(self, **event) -> None:
        self.events.append(event)


async def test_voice_timer_adapter_uses_real_hass_start_timer_and_exact_id_actions(hass: HomeAssistant) -> None:
    """Intent-created timers drive the one registered device handler and adapter mutations."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"voice:{VOICE_INSTALL_ID}", title="Kitchen tablet", data=VOICE_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = entry.runtime_data
    native = _NativeSession()
    device.session = native
    await intent.async_handle(
        hass,
        "test",
        intent.INTENT_START_TIMER,
        slots={"seconds": {"value": 120}, "name": {"value": "Tea"}},
        device_id=device.device_id,
    )
    manager = hass.data["intent.timer"]
    timer_id = next(iter(manager.timers))
    adapter = device.timer_adapter
    assert native.events[-1]["change"] == "started"

    adapter.action("pause", timer_id, "pause", None)
    assert manager.timers[timer_id].is_active is False
    adapter.action("resume", timer_id, "resume", None)
    assert manager.timers[timer_id].is_active is True
    adapter.action("add", timer_id, "add", 30)
    adapter.action("subtract", timer_id, "subtract", 10)
    assert manager.timers[timer_id].created_seconds == 150
    assert manager.timers[timer_id].seconds == 140

    manager._timer_finished(timer_id)
    assert native.events[-1]["change"] == "finished"
    timers, ringing = adapter.snapshot()
    assert timers[-1]["timer_id"] == timer_id
    assert ringing == [timer_id]
    adapter.action("dismiss", timer_id, "dismiss", None)
    assert native.events[-1]["change"] == "dismissed"


async def test_real_assist_announcement_service_waits_for_native_ack_path(hass: HomeAssistant) -> None:
    """HA service routing reaches the actual satellite feature implementation."""
    assert await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"voice:{VOICE_INSTALL_ID}", title="Kitchen tablet", data=VOICE_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = entry.runtime_data
    native = _NativeSession()
    device.session = native
    device.status = VoiceStatus(False, "denied", "foreground", False, True)
    device.capabilities = VoiceCapabilities(True, False, True, True, True)
    satellite = next(iter(device.satellites.values()))
    satellite.async_write_ha_state()

    await hass.services.async_call(
        "assist_satellite",
        "announce",
        {"entity_id": satellite.entity_id, "media_id": "https://example.invalid/announcement.mp3", "preannounce": False},
        blocking=True,
    )
    assert native.events[-1]["kind"] == "announcement"
