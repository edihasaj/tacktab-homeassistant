"""Regression coverage for realtime session ownership and teardown."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from custom_components.tacktab.voice_realtime import RealtimeController, RuntimeSession
from custom_components.tacktab.voice_realtime_config import TRUSTED_LOCAL_HTTP, key_transport_allowed
from custom_components.tacktab.voice_realtime_protocol import RealtimeProtocolError
from custom_components.tacktab.voice_realtime_providers import ProviderEvent


class _Hass:
    def async_create_task(self, coroutine, _name: str | None = None):
        return asyncio.create_task(coroutine)


class _Bridge:
    def __init__(self, session) -> None:
        self.session = session
        self.devices = {"device": session.device}
        self.acquired = 0
        self.released = 0

    def session_for_connection(self, _connection):
        return self.session

    def acquire_media_lease(self, _session, _owner) -> None:
        self.acquired += 1

    def release_media_lease(self, _session, _owner) -> None:
        self.released += 1


class _Connection:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id="user", is_admin=True)
        self.results: list[tuple[int, object]] = []

    def send_result(self, request_id: int, result=None) -> None:
        self.results.append((request_id, result))


class _Provider:
    def __init__(self, close_error: bool = False) -> None:
        self.interrupts: list[tuple[str | None, int]] = []
        self.tool_results: list[tuple[str, str, dict, bool]] = []
        self.continued = 0
        self.close_error = close_error

    async def interrupt(self, item_id: str | None, frames: int) -> None:
        self.interrupts.append((item_id, frames))

    async def send_audio(self, _pcm: bytes) -> None:
        return None

    async def end_audio(self) -> None:
        return None

    async def send_tool_result(self, call_id: str, name: str, result: dict, failed: bool) -> None:
        self.tool_results.append((call_id, name, result, failed))

    async def continue_after_tools(self) -> None:
        self.continued += 1

    async def close(self):
        if self.close_error:
            raise RuntimeError("socket close failed")
        return None


def _runtime(provider: _Provider | None = None) -> tuple[RealtimeController, RuntimeSession, _Connection, _Bridge]:
    device = SimpleNamespace(install_id="install", realtime_active=True)
    events: list[dict] = []
    session = SimpleNamespace(device=device, emit=events.append)
    bridge = _Bridge(session)
    controller = RealtimeController(_Hass(), bridge)
    run = RuntimeSession(
        "run",
        session,
        "primary",
        SimpleNamespace(follow_up_timeout_ms=15_000, memory_timeout_ms=1_800_000),
        provider or _Provider(),
        "lease",
    )
    controller.sessions[run.session_id] = run
    return controller, run, _Connection(), bridge


@pytest.mark.parametrize("tool_timeout", [False, True])
async def test_tool_result_transport_failure_closes_from_current_tool_task(tool_timeout) -> None:
    class FailingResultProvider(_Provider):
        async def send_tool_result(self, *_args):
            raise ConnectionResetError("disconnected")

    async def call(*_args):
        if tool_timeout:
            raise TimeoutError
        return {"success": True}, False

    controller, run, _connection, bridge = _runtime(FailingResultProvider())
    run.tools = SimpleNamespace(call=call)
    await controller._event(run, ProviderEvent("tool_call", call_id="call", name="light", arguments={}))
    tasks = tuple(run.tool_tasks.values())
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
    assert results == [None]
    assert run.closed and bridge.released == 1
    assert run.history[-1]["role"] == "tool"
    assert run.history[-1]["error"] is tool_timeout


async def test_vad_interruption_truncates_actual_playback_and_allows_next_generation() -> None:
    controller, run, connection, _bridge = _runtime()
    run.sent_frames["playback"] = 100
    run.played_frames["playback"] = 37
    run.playback_items["playback"] = "provider-item"
    run.playback_id = "playback"
    executed = False

    async def queued_tool() -> None:
        nonlocal executed
        await asyncio.sleep(0)
        executed = True

    run.tool_tasks["call"] = asyncio.create_task(queued_tool())
    await controller._event(run, ProviderEvent("speech_started"))
    await asyncio.sleep(0)

    assert run.provider.interrupts == [("provider-item", 37)]
    assert "playback" in run.retired_playbacks
    assert not executed

    # Late chunks from the cancelled response are discarded, while the next
    # completed generation gets a fresh playback stream.
    await controller.audio_end(connection, {"id": 1, "session_id": run.session_id})
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00", item_id="late"))
    await controller._event(run, ProviderEvent("generation_end"))
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00", item_id="next"))
    events = run.bridge_session.emit.__self__
    audio = [event for event in events if event["kind"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["playback_id"] != "playback"
    assert any(event["kind"] == "interrupted" for event in events)


async def test_close_failure_always_releases_lease_and_does_not_claim_memory() -> None:
    controller, run, _connection, bridge = _runtime(_Provider(close_error=True))
    run.history = [{"role": "user", "text": "hello"}]
    run.history_bytes = 32
    run.memory_key = ("user", "install", "primary", "xai", "model", "voice")

    await controller.close(run, "stopped", False)

    assert run.closed
    assert bridge.released == 1
    assert run.bridge_session.device.realtime_active is False
    assert run.memory_key not in controller.memory
    assert run.bridge_session.emit.__self__[-1]["memory_retained"] is False


async def test_follow_up_deadline_survives_silent_pcm_until_vad_confirms_speech() -> None:
    controller, run, connection, _bridge = _runtime()
    run.setting.follow_up_timeout_ms = 1
    await controller._event(run, ProviderEvent("generation_end"))
    assert run.idle_task is not None
    await controller.audio(connection, {"id": 2, "session_id": run.session_id, "chunk_seq": 0, "pcm": b"\x00\x00"})
    await controller.audio(connection, {"id": 3, "session_id": run.session_id, "chunk_seq": 1, "pcm": b"\x00\x00"})
    assert run.idle_task is not None
    await asyncio.sleep(0.02)
    assert run.closed

    controller, run, _connection, _bridge = _runtime()
    await controller._event(run, ProviderEvent("generation_end"))
    await controller._event(run, ProviderEvent("speech_started"))
    assert run.idle_task is None


async def test_gemini_interruption_retires_output_and_discards_late_audio() -> None:
    controller, run, _connection, _bridge = _runtime()
    run.sent_frames["playback"] = 100
    run.played_frames["playback"] = 37
    run.playback_items["playback"] = "provider-item"
    run.playback_id = "playback"

    await controller._event(run, ProviderEvent("interrupted"))
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00", item_id="late"))

    assert run.provider.interrupts == [("provider-item", 37)]
    assert "playback" in run.retired_playbacks
    assert not [event for event in run.bridge_session.emit.__self__ if event["kind"] == "audio"]


async def test_final_interrupted_ack_retruncates_to_final_heard_frame() -> None:
    controller, run, connection, _bridge = _runtime()
    run.sent_frames["playback"] = 100
    run.played_frames["playback"] = 37
    run.playback_items["playback"] = "provider-item"
    run.playback_id = "playback"

    await controller._event(run, ProviderEvent("speech_started"))
    await controller.playback(
        connection,
        {"id": 4, "session_id": run.session_id, "playback_id": "playback", "played_frames": 50, "status": "interrupted"},
    )

    assert run.provider.interrupts == [("provider-item", 37), ("provider-item", 50)]
    assert run.played_frames["playback"] == 50


async def test_audio_end_resets_utterance_without_accepting_stale_generation() -> None:
    controller, run, connection, _bridge = _runtime()
    run.utterance_bytes = 60 * 16_000 * 2
    run.discard_audio = True

    await controller.audio_end(connection, {"id": 5, "session_id": run.session_id})
    await controller.audio(connection, {"id": 6, "session_id": run.session_id, "chunk_seq": 0, "pcm": b"\x00\x00"})

    assert run.utterance_bytes == 2
    assert run.discard_audio is True


async def test_provider_send_failure_closes_session_and_releases_lease() -> None:
    class BrokenProvider(_Provider):
        async def send_audio(self, _pcm: bytes) -> None:
            raise ConnectionResetError

    controller, run, connection, bridge = _runtime(BrokenProvider())
    with pytest.raises(RealtimeProtocolError) as error:
        await controller.audio(connection, {"id": 7, "session_id": run.session_id, "chunk_seq": 0, "pcm": b"\x00\x00"})

    assert error.value.code == "provider_error"
    assert run.closed
    assert bridge.released == 1


async def test_finished_playback_closes_when_tool_continuation_disconnects() -> None:
    class BrokenProvider(_Provider):
        async def continue_after_tools(self) -> None:
            raise ConnectionResetError

    controller, run, connection, bridge = _runtime(BrokenProvider())
    run.pending_continue = True
    run.response_done = True
    run.sent_frames["playback"] = 4
    run.output_ended.add("playback")

    await controller.playback(
        connection,
        {"id": 8, "session_id": run.session_id, "playback_id": "playback", "played_frames": 4, "status": "finished"},
    )

    assert run.closed
    assert bridge.released == 1


async def test_output_cap_counts_undrained_frames_across_playback_ids() -> None:
    controller, run, _connection, _bridge = _runtime()
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00" * 100_000, item_id="first"))
    await controller._event(run, ProviderEvent("generation_end"))
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00" * 30_000, item_id="second"))

    assert run.closed


async def test_provider_audio_cancels_an_existing_follow_up_deadline() -> None:
    controller, run, _connection, _bridge = _runtime()
    controller._schedule_idle_close(run)

    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00", item_id="response"))

    assert run.idle_task is None


async def test_provider_audio_is_split_to_native_pcm_frame_limit() -> None:
    controller, run, _connection, _bridge = _runtime()
    await controller._event(run, ProviderEvent("audio", audio=b"\x00\x00" * 20_000, item_id="response"))

    audio = [event for event in run.bridge_session.emit.__self__ if event["kind"] == "audio"]
    assert [event["chunk_seq"] for event in audio] == [0, 1]
    assert [event["frames"] for event in audio] == [16_384, 3_616]
    assert all(len(base64.b64decode(event["audio"])) <= 32 * 1024 for event in audio)


async def test_timed_out_tool_returns_one_safe_error_before_continuing() -> None:
    controller, run, _connection, _bridge = _runtime()

    async def timeout(*_args):
        raise TimeoutError

    run.tools = SimpleNamespace(call=timeout)
    run.pending_continue = True
    run.response_done = True
    event = ProviderEvent("tool_call", call_id="call", name="switch", arguments={})
    await controller._run_tool(run, event, run.generation)

    assert run.provider.tool_results == [("call", "switch", {"error": "Tool timed out"}, True)]
    assert run.provider.continued == 1


async def test_slot_configuration_cas_locks_out_concurrent_start_and_update(monkeypatch) -> None:
    import custom_components.tacktab.voice_realtime as realtime

    class Entries:
        @staticmethod
        def async_update_entry(entry, *, data) -> None:
            entry.data = data

        @staticmethod
        def async_entries(_domain):
            return []

    class Hass(_Hass):
        config_entries = Entries()

    class LockAwareBridge(_Bridge):
        def acquire_media_lease(self, session, owner) -> None:
            if session.device.config_lock.locked():
                raise RuntimeError("topology changing")
            super().acquire_media_lease(session, owner)

    bind_started = asyncio.Event()
    allow_bind = asyncio.Event()

    async def blocked_bind(*_args, **_kwargs):
        bind_started.set()
        await allow_bind.wait()
        return SimpleNamespace(prompt="", specs=[])

    async def prepared_store(*_args):
        return None

    entry = SimpleNamespace(entry_id="entry", data={"realtime_slots": {"primary": {"mode": "realtime", "provider": "xai"}}})
    device = SimpleNamespace(
        install_id="install",
        entry=entry,
        config_revision=0,
        config_lock=asyncio.Lock(),
        realtime_active=False,
        status=SimpleNamespace(microphone_ready=True),
        device_id="ha-device",
        slot=lambda _slot: SimpleNamespace(),
    )
    session = SimpleNamespace(device=device, emit=lambda _event: None, validate_epoch=lambda _msg: None)
    bridge = LockAwareBridge(session)
    controller = RealtimeController(Hass(), bridge)
    connection = _Connection()
    configure = {
        "id": 1,
        "type": "tacktab/realtime/configure_slot",
        "epoch": "epoch",
        "slot_id": "primary",
        "expected_config_revision": 0,
        "config": {"mode": "realtime"},
    }
    monkeypatch.setattr(realtime, "async_bind_tools", blocked_bind)
    monkeypatch.setattr(realtime, "async_prepare_provider_store", prepared_store)
    first = asyncio.create_task(controller.handle(connection, configure))
    await bind_started.wait()
    second = asyncio.create_task(controller.handle(connection, {**configure, "id": 2}))
    await asyncio.sleep(0)

    with pytest.raises(RealtimeProtocolError) as start_error:
        await controller.start(connection, session, entry, {"slot_id": "primary", "trigger": "button", "id": 3})
    assert start_error.value.code == "busy"

    allow_bind.set()
    await first
    with pytest.raises(RealtimeProtocolError) as conflict:
        await second
    assert conflict.value.code == "config_conflict"
    assert device.config_revision == 1


async def test_real_paired_websocket_prepares_store_catalogs_and_binds_assist_tools(hass, hass_ws_client) -> None:
    """Exercise canonical key storage from a real HA websocket connection."""
    from homeassistant.setup import async_setup_component

    from custom_components.tacktab.voice_bridge import async_get_bridge
    from custom_components.tacktab.voice_realtime import async_setup_realtime
    from custom_components.tacktab.voice_realtime_tools import async_bind_tools
    from custom_components.tacktab.voice_setup import async_ensure_voice_bootstrap_entry

    assert await async_setup_component(hass, "homeassistant", {})
    bootstrap = await async_ensure_voice_bootstrap_entry(hass)
    await hass.async_block_till_done()
    websocket = await hass_ws_client()
    await websocket.send_json(
        {
            "id": 1,
            "type": "tacktab/voice/connect",
            "protocol": 1,
            "install_id": "00000000-0000-4000-8000-000000000102",
            "name": "Realtime tablet",
            "platform": "ios",
            "app_version": "2.0",
            "capabilities": {"announcements": True, "start_conversation": True, "timers": True, "local_wake_word": True, "pcm16_16khz_mono": True},
            "status": {"local_enabled": True, "mic_permission": "granted", "app_state": "foreground", "capture_ready": True, "playback_ready": True},
            "slots": [{"slot_id": "primary", "enabled": True, "pipeline_id": None, "wake_word": {"id": "okay", "phrase": "Okay Tacktab"}, "output": "local", "media_player_entity_id": None}],
        }
    )
    while True:
        response = await websocket.receive_json()
        if response.get("id") == 1:
            break
    assert response["success"] is True

    bridge = async_get_bridge(hass)
    device = bridge.devices["00000000-0000-4000-8000-000000000102"]
    connection = device.session.connection
    controller = async_setup_realtime(hass, bridge)
    catalog = await controller.catalog(device.entry, "xai")
    assert catalog["key_configured"] is False
    assert catalog["source"] == "bundled"
    assert not key_transport_allowed(hass, connection, device.entry)
    provider_request = {
        "id": 2,
        "type": "tacktab/realtime/configure_provider",
        "epoch": device.session.epoch,
        "provider": "xai",
        "key_action": "replace",
        "api_key": "test-key",
    }
    with pytest.raises(RealtimeProtocolError) as insecure:
        await controller.handle(connection, provider_request)
    assert insecure.value.code == "insecure_transport"

    hass.config_entries.async_update_entry(bootstrap, options={TRUSTED_LOCAL_HTTP: True})
    assert key_transport_allowed(hass, connection, device.entry)
    await controller.handle(connection, provider_request)
    while True:
        response = await websocket.receive_json()
        if response.get("id") == 2:
            break
    assert response["success"] is True
    assert response["result"] == {"provider": "xai", "key_configured": True}

    tools = await async_bind_tools(hass, connection, device.device_id, ("assist",), None)
    assert tools.api is not None


async def test_startup_bind_failure_closes_provider_and_releases_lease(monkeypatch) -> None:
    import custom_components.tacktab.voice_realtime as realtime

    class ClosingProvider(_Provider):
        def __init__(self, _hass) -> None:
            super().__init__(close_error=True)

    async def bind_failure(*_args, **_kwargs):
        raise RuntimeError("LLM unavailable")

    async def prepared_store(*_args):
        return None

    device = SimpleNamespace(
        install_id="install",
        realtime_active=False,
        status=SimpleNamespace(microphone_ready=True),
        device_id="ha-device",
        slot=lambda _slot: None,
    )
    session = SimpleNamespace(device=device, emit=lambda _event: None)
    bridge = _Bridge(session)
    controller = RealtimeController(_Hass(), bridge)
    connection = _Connection()
    entry = SimpleNamespace(data={"realtime_provider_keys": {"xai": "key"}, "realtime_slots": {"primary": {"mode": "realtime", "provider": "xai"}}})
    monkeypatch.setattr(realtime, "XAIRealtimeProvider", ClosingProvider)
    monkeypatch.setattr(realtime, "async_bind_tools", bind_failure)
    monkeypatch.setattr(realtime, "async_prepare_provider_store", prepared_store)

    with pytest.raises(RealtimeProtocolError) as error:
        await controller.start(connection, session, entry, {"slot_id": "primary", "trigger": "button", "id": 1})

    assert error.value.code == "timeout"
    assert bridge.acquired == bridge.released == 1
    assert not controller.pending
    assert device.realtime_active is False


async def test_retained_memory_is_timer_evicted_and_closed_forget_is_idempotent() -> None:
    controller, run, connection, _bridge = _runtime()
    run.setting.memory_timeout_ms = 1
    run.memory_key = ("user", "install", "primary", "xai", "model", "voice")
    run.history = [{"role": "user", "text": "remember"}]
    run.history_bytes = 40
    await controller.close(run, "stopped", False)
    assert run.memory_key in controller.memory
    await asyncio.sleep(0.02)
    assert run.memory_key not in controller.memory

    await controller.stop(connection, {"id": 2, "session_id": run.session_id, "forget": True})
    assert connection.results[-1][0] == 2
