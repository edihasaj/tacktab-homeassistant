"""Remote HA speaker output uses state observation, never a native ACK."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.components.media_player.const import DATA_COMPONENT as MEDIA_PLAYER_DATA
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.tacktab.voice_bridge import VoiceBridgeError, VoiceDevice, VoiceRun, VoiceSession, async_get_bridge
from custom_components.tacktab.voice_models import SlotConfig, VoiceCapabilities, VoiceStatus


class _Speaker(MediaPlayerEntity):
    """A representative entity whose service implementation updates HA state."""

    _attr_name = "Voice test speaker"
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.STOP

    def __init__(self, *, starts: bool = True) -> None:
        self.starts = starts
        self._attr_state = MediaPlayerState.IDLE
        self._attr_media_content_id: str | None = None
        self.calls: list[tuple[str, str]] = []
        self.stops = 0
        self.block_service = False

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        self.calls.append((media_type, media_id))
        if self.block_service:
            await asyncio.Event().wait()
        if self.starts:
            self._attr_media_content_id = media_id
            self._attr_state = MediaPlayerState.PLAYING
            self.async_write_ha_state()

    def finish(self) -> None:
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    def replace(self, media_id: str) -> None:
        self._attr_media_content_id = media_id
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        self.stops += 1
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()


class Connection:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.results: list[tuple[int, dict | None]] = []
        self.subscriptions: dict = {}

    def async_register_binary_handler(self, _handler):
        return 1, lambda: None

    def send_event(self, _subscription_id: int, event: dict) -> None:
        self.events.append(event)

    def send_result(self, msg_id: int, result: dict | None = None) -> None:
        self.results.append((msg_id, result))

    def context(self, _msg: dict) -> Context:
        return Context()


async def _speaker(hass: HomeAssistant, *, starts: bool = True) -> _Speaker:
    assert await async_setup_component(hass, "media_player", {})
    speaker = _Speaker(starts=starts)
    await hass.data[MEDIA_PLAYER_DATA].async_add_entities([speaker])
    await hass.async_block_till_done()
    return speaker


def _session(hass: HomeAssistant, entity_id: str) -> tuple[VoiceSession, VoiceDevice, Connection]:
    slot = SlotConfig("primary", True, None, None, "ha_media_player", entity_id)
    device = VoiceDevice(hass, "00000000-0000-4000-8000-000000000001", "Tablet", "ios", (slot,))
    device.status = VoiceStatus(True, "granted", "foreground", True, False)
    device.capabilities = VoiceCapabilities(True, True, True, True, True)
    connection = Connection()
    session = VoiceSession(hass, device, connection, 9, 1, lambda: None, 2)
    device.session = session
    return session, device, connection


async def _until_started(connection: Connection) -> None:
    for _ in range(20):
        if any(event.get("phase") == "started" for event in connection.events):
            return
        await asyncio.sleep(0)
    pytest.fail("remote speaker never emitted a started event")


async def _until_calls(speaker: _Speaker, count: int) -> None:
    for _ in range(20):
        if len(speaker.calls) >= count:
            return
        await asyncio.sleep(0)
    pytest.fail(f"speaker did not receive {count} media calls")


async def test_remote_player_service_reports_observed_lifecycle_and_releases_before_finished(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, device, connection = _session(hass, speaker.entity_id)
    run_id = "00000000-0000-4000-8000-000000000002"
    session.runs[run_id] = VoiceRun(run_id, "primary", None)

    task = hass.async_create_task(session.play(
        slot_id="primary", kind="response", message="Hello", media_id="https://example.invalid/tts.mp3",
        preannounce_media_id=None, listen_after=True, run_id=run_id,
    ))
    await _until_started(connection)
    assert speaker.calls == [("music", "https://example.invalid/tts.mp3")]
    assert connection.events[-1]["event"] == "remote_playback"
    assert connection.events[-1]["phase"] == "started"
    assert connection.events[-1]["message"] == "Hello"
    assert device.media_lease is not None

    speaker.finish()
    await task

    finished = connection.events[-1]
    assert finished["phase"] == "finished"
    assert finished["listen_after"] is True
    assert finished["error"] is None
    assert device.media_lease is None
    assert "primary" in device.follow_up_until
    assert run_id not in session.runs
    # A native client can submit its authorized follow-up as soon as it sees
    # `finished`; the completed remote run must no longer make it busy.
    device._preferred_pipeline_id = lambda: "pipeline"  # type: ignore[method-assign]
    device._pipelines = lambda: [{"pipeline_id": "pipeline"}]  # type: ignore[method-assign]
    bridge = async_get_bridge(hass)
    next_run_id = "00000000-0000-4000-8000-000000000003"
    assert await bridge._run_command(session, {
        "run_id": next_run_id, "slot_id": "primary", "trigger": "follow_up", "pipeline_id": None,
    }) == "pipeline"
    assert next_run_id in session.runs
    session.runs.pop(next_run_id)
    schema = json.loads((Path(__file__).parents[1] / "docs" / "remote-output.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for event in connection.events:
        assert not list(validator.iter_errors(event))


async def test_remote_player_start_timeout_fails_without_success(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.tacktab.voice_output.REMOTE_START_TIMEOUT_SECONDS", 0.01)
    speaker = await _speaker(hass, starts=False)
    session, device, connection = _session(hass, speaker.entity_id)

    with pytest.raises(VoiceBridgeError, match="did not report completion") as error:
        await session.play(slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False)

    assert error.value.code == "timeout"
    assert connection.events[-1]["phase"] == "failed"
    assert connection.events[-1]["error"] == "timeout"
    assert device.media_lease is None


async def test_remote_player_rejects_stale_same_url_playback(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.tacktab.voice_output.REMOTE_START_TIMEOUT_SECONDS", 0.01)
    speaker = await _speaker(hass, starts=False)
    speaker._attr_media_content_id = "https://example.invalid/a.mp3"
    speaker._attr_state = MediaPlayerState.PLAYING
    speaker.async_write_ha_state()
    await hass.async_block_till_done()
    session, _device, connection = _session(hass, speaker.entity_id)

    with pytest.raises(VoiceBridgeError) as error:
        await session.play(slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False)
    assert error.value.code == "timeout"
    assert not any(event.get("phase") == "started" for event in connection.events)
    assert connection.events[-1]["phase"] == "failed"


async def test_remote_player_bounds_a_hanging_service_dispatch(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.tacktab.voice_output.REMOTE_START_TIMEOUT_SECONDS", 0.01)
    speaker = await _speaker(hass)
    speaker.block_service = True
    session, _device, connection = _session(hass, speaker.entity_id)

    with pytest.raises(VoiceBridgeError) as error:
        await session.play(slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False)
    assert error.value.code == "timeout"
    assert connection.events[-1]["phase"] == "failed"


async def test_remote_player_removal_fails_without_waiting_for_timeout(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, _device, connection = _session(hass, speaker.entity_id)
    task = hass.async_create_task(session.play(slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False))
    await _until_started(connection)
    hass.states.async_remove(speaker.entity_id)

    with pytest.raises(VoiceBridgeError, match="removed") as error:
        await task
    assert error.value.code == "remote_output_failed"
    assert connection.events[-1]["phase"] == "failed"


async def test_remote_player_observes_preannounce_before_prompt(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, _device, connection = _session(hass, speaker.entity_id)
    task = hass.async_create_task(session.play(
        slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/prompt.mp3",
        preannounce_media_id="https://example.invalid/chime.mp3", listen_after=False,
    ))
    await _until_started(connection)
    assert speaker.calls == [("music", "https://example.invalid/chime.mp3")]
    speaker.finish()
    await _until_calls(speaker, 2)
    assert speaker.calls[-1] == ("music", "https://example.invalid/prompt.mp3")
    speaker.finish()
    await task
    assert connection.events[-1]["phase"] == "finished"


async def test_remote_player_replacement_is_not_credited_as_completion(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, _device, connection = _session(hass, speaker.entity_id)
    task = hass.async_create_task(session.play(slot_id="primary", kind="response", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False))
    await _until_started(connection)
    speaker.replace("https://example.invalid/unrelated.mp3")

    with pytest.raises(VoiceBridgeError, match="replaced") as error:
        await task
    assert error.value.code == "remote_output_failed"
    assert connection.events[-1]["phase"] == "failed"


async def test_remote_player_disconnect_cleans_observer_without_stopping_audio(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, device, connection = _session(hass, speaker.entity_id)
    task = hass.async_create_task(session.play(slot_id="primary", kind="response", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False))
    await _until_started(connection)

    await session.close("replaced")
    with pytest.raises(VoiceBridgeError):
        await task
    assert speaker.state == MediaPlayerState.PLAYING
    assert device.media_lease is None
    assert not any(event.get("phase") == "finished" for event in connection.events)


async def test_remote_player_requires_play_media_capability(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    speaker._attr_supported_features = MediaPlayerEntityFeature(0)
    speaker.async_write_ha_state()
    await hass.async_block_till_done()
    session, _device, connection = _session(hass, speaker.entity_id)

    with pytest.raises(VoiceBridgeError) as error:
        await session.play(slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=False)
    assert error.value.code == "unsupported_output"
    assert connection.events[-1]["phase"] == "failed"


async def test_remote_follow_up_still_requires_native_microphone_permission(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, device, connection = _session(hass, speaker.entity_id)
    device.status = VoiceStatus(True, "denied", "foreground", False, False)

    with pytest.raises(VoiceBridgeError) as error:
        await session.play(slot_id="primary", kind="start_conversation", message="", media_id="https://example.invalid/a.mp3", preannounce_media_id=None, listen_after=True)
    assert error.value.code == "mic_permission"
    assert connection.events == []


async def test_v2_cancel_output_stops_only_owned_media_and_fails_canonical_action(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, device, connection = _session(hass, speaker.entity_id)
    bridge = async_get_bridge(hass)
    bridge.devices[device.install_id] = device
    task = hass.async_create_task(session.play(
        slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3",
        preannounce_media_id=None, listen_after=False,
    ))
    await _until_started(connection)
    action_id = connection.events[-1]["action_id"]

    await bridge.handle(connection, {
        "id": 4, "type": "tacktab/voice/cancel_output", "epoch": session.epoch,
        "action_id": action_id,
    })
    assert connection.results[-1][0] == 4
    assert speaker.stops == 1
    with pytest.raises(VoiceBridgeError, match="cancelled"):
        await task
    assert connection.events[-1]["phase"] == "failed"
    assert not any(event.get("phase") == "finished" for event in connection.events)


async def test_v2_cancel_output_never_stops_replacement_media(hass: HomeAssistant) -> None:
    speaker = await _speaker(hass)
    session, device, connection = _session(hass, speaker.entity_id)
    bridge = async_get_bridge(hass)
    bridge.devices[device.install_id] = device
    task = hass.async_create_task(session.play(
        slot_id="primary", kind="announcement", message="", media_id="https://example.invalid/a.mp3",
        preannounce_media_id=None, listen_after=False,
    ))
    await _until_started(connection)
    action_id = connection.events[-1]["action_id"]
    output = session.remote_actions[action_id].output
    assert output is not None
    output._unsubscribe()  # Verify the stop guard against a replacement state directly.
    speaker.replace("https://example.invalid/replacement.mp3")

    await bridge.handle(connection, {
        "id": 5, "type": "tacktab/voice/cancel_output", "epoch": session.epoch,
        "action_id": action_id,
    })
    assert speaker.stops == 0
    with pytest.raises(VoiceBridgeError, match="cancelled"):
        await task
    assert connection.events[-1]["phase"] == "failed"
