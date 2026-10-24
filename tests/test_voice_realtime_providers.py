"""Protocol fixtures only: these tests never open a paid provider connection."""
from __future__ import annotations

import base64
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.tacktab.voice_realtime_config import SlotRealtimeConfig, parse_slot_config
from custom_components.tacktab.voice_realtime_gemini import GeminiRealtimeProvider
from custom_components.tacktab.voice_realtime_openai import OpenAIRealtimeProvider
from custom_components.tacktab.voice_realtime_providers import ProviderConfig, StatefulResampler, ToolSpec
from custom_components.tacktab.voice_realtime_protocol import RealtimeProtocolError, parse_command
from custom_components.tacktab.voice_realtime_catalog import async_catalog
from custom_components.tacktab.voice_realtime import RealtimeController, RuntimeSession
from custom_components.tacktab.voice_realtime_xai import XAIRealtimeProvider


class Message:
    def __init__(self, data): self.data = json.dumps(data) if isinstance(data, dict) else data


class Socket:
    def __init__(self, events=()): self.sent = []; self.events = [Message(x) for x in events]; self.closed = False
    async def send_json(self, data): self.sent.append(data)
    async def close(self): self.closed = True
    def __aiter__(self):
        async def stream():
            for event in self.events: yield event
        return stream()


class Client:
    def __init__(self, socket): self.socket = socket; self.calls = []
    async def ws_connect(self, *args, **kwargs): self.calls.append((args, kwargs)); return self.socket


@pytest.mark.parametrize(("adapter", "provider", "model", "voice", "expected_rate"), [
    (OpenAIRealtimeProvider, "openai", "gpt-realtime", "marin", 24000),
    (XAIRealtimeProvider, "xai", "grok-voice-3.0", "eve", 16000),
])
async def test_openai_shape_and_xai_shape(monkeypatch, adapter, provider, model, voice, expected_rate):
    socket, client = Socket(), Client(Socket())
    client.socket = socket
    module = "custom_components.tacktab.voice_realtime_openai" if provider == "openai" else "custom_components.tacktab.voice_realtime_xai"
    monkeypatch.setattr(f"{module}.async_get_clientsession", lambda hass: client)
    instance = adapter(object())
    await instance.connect(ProviderConfig(provider, model, voice, "never-log-this"), [ToolSpec("intent__Timer", "timer", {"type": "object"})], "home prompt")
    setup = socket.sent[0]["session"]
    assert setup["audio"]["input"]["format"]["rate"] == expected_rate
    assert setup["audio"]["output"]["format"]["rate"] == 24000
    assert setup["tools"][0]["name"] == "intent__Timer"
    await instance.send_audio(b"\x00\x00" * 100)
    assert socket.sent[-1]["type"] == "input_audio_buffer.append"


async def test_gemini_setup_and_event_fixture(monkeypatch):
    pcm = base64.b64encode(b"\x01\x00" * 8).decode()
    socket = Socket([{"setupComplete": {}}, {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": pcm}}]}, "turnComplete": True}}, {"toolCall": {"functionCalls": [{"id": "call-1", "name": "timer", "args": {"minutes": 2}}]}}])
    client = Client(socket)
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_gemini.async_get_clientsession", lambda hass: client)
    instance = GeminiRealtimeProvider(object())
    await instance.connect(ProviderConfig("gemini", "models/gemini-3.8-live", "Kore", "never-log-this"), [ToolSpec("timer", "timer", {"type": "object"})], "home prompt")
    assert socket.sent[0]["setup"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    kinds = [event.kind async for event in instance.events()]
    assert kinds == ["ready", "audio", "generation_end", "tool_call"]
    await instance.send_audio(b"\x00\x00" * 16)
    await instance.end_audio()
    await instance.send_tool_result("call-1", "timer", {"ok": True}, False)
    assert socket.sent[-1]["toolResponse"]["functionResponses"][0]["id"] == "call-1"
    await instance.interrupt(None, 0)
    await instance.close()
    assert socket.closed


@pytest.mark.parametrize(("adapter", "module", "provider", "model", "voice"), [
    (OpenAIRealtimeProvider, "custom_components.tacktab.voice_realtime_openai", "openai", "gpt-realtime", "marin"),
    (XAIRealtimeProvider, "custom_components.tacktab.voice_realtime_xai", "xai", "grok-voice-3.0", "eve"),
])
async def test_openai_shape_tool_interrupt_and_cleanup_fixture(monkeypatch, adapter, module, provider, model, voice):
    audio = base64.b64encode(b"\x01\x00" * 16).decode()
    socket = Socket([
        {"type": "session.updated"},
        {"type": "response.output_audio.delta", "item_id": "item-1", "delta": audio},
        {"type": "response.function_call_arguments.done", "call_id": "call-1", "name": "timer", "arguments": '{"minutes":2}'},
        {"type": "response.done"},
    ])
    monkeypatch.setattr(f"{module}.async_get_clientsession", lambda hass: Client(socket))
    instance = adapter(object())
    await instance.connect(ProviderConfig(provider, model, voice, "never-log-this"), [], "home prompt")
    events = [event async for event in instance.events()]
    assert [event.kind for event in events] == ["ready", "audio", "tool_call", "generation_end"]
    await instance.send_tool_result("call-1", "timer", {"ok": True}, False)
    await instance.continue_after_tools()
    await instance.interrupt("item-1", 480)
    expected = ["conversation.item.create", "response.create", "conversation.item.truncate"] if provider == "openai" else ["conversation.item.create", "response.create", "response.cancel", "conversation.item.truncate"]
    assert [message["type"] for message in socket.sent[-len(expected):]] == expected
    assert socket.sent[-1]["audio_end_ms"] == 20
    await instance.close()
    assert socket.closed


async def test_openai_completed_response_truncates_without_cancel_and_finalizes_user_transcript(monkeypatch):
    socket = Socket([
        {"type": "session.updated"}, {"type": "response.created", "response": {"id": "resp-1"}},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "user-1", "transcript": "turn on lights"},
        {"type": "response.done", "response": {"id": "resp-1"}},
    ])
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_openai.async_get_clientsession", lambda hass: Client(socket))
    instance = OpenAIRealtimeProvider(object())
    await instance.connect(ProviderConfig("openai", "gpt-realtime", "marin", "key"), [], "prompt")
    events = [event async for event in instance.events()]
    assert events[1].role == "user" and events[1].final and events[1].operation == "replace"
    assert socket.sent[0]["session"]["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
    await instance.interrupt("assistant-item", 240)
    assert socket.sent[-1]["type"] == "conversation.item.truncate"


async def test_gemini_activity_and_late_turn_complete_finalizes_buffered_transcripts(monkeypatch):
    socket = Socket([
        {"setupComplete": {}},
        {"voiceActivity": {"type": "ACTIVITY_START"}},
        {"serverContent": {"inputTranscription": {"text": "hello"}}},
        {"voiceActivity": {"type": "ACTIVITY_END"}},
        {"serverContent": {"outputTranscription": {"text": "hi"}}},
        {"serverContent": {"turnComplete": True}},
    ])
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_gemini.async_get_clientsession", lambda hass: Client(socket))
    instance = GeminiRealtimeProvider(object())
    await instance.connect(ProviderConfig("gemini", "models/gemini-3.8-live", "Kore", "key"), [], "prompt")
    events = [event async for event in instance.events()]
    assert [event.kind for event in events] == ["ready", "speech_started", "transcript", "speech_stopped", "transcript", "transcript", "transcript", "generation_end"]
    assert [event.final for event in events if event.kind == "transcript"] == [False, False, True, True]
    await instance.interrupt(None, 0)
    assert socket.sent[-1] == {"clientContent": {"turns": [], "turnComplete": False}}


async def test_xai_completed_transcription_replaces_partial(monkeypatch):
    socket = Socket([
        {"type": "conversation.item.input_audio_transcription.updated", "item_id": "u", "transcript": "hel"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "u", "transcript": "hello"},
    ])
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_xai.async_get_clientsession", lambda hass: Client(socket))
    instance = XAIRealtimeProvider(object())
    await instance.connect(ProviderConfig("xai", "grok-voice-3.0", "eve", "key"), [], "prompt")
    events = [event async for event in instance.events()]
    assert [(event.operation, event.final, event.text) for event in events] == [("replace", False, "hel"), ("replace", True, "hello")]


@pytest.mark.parametrize(("adapter", "module", "provider", "model", "voice"), [
    (OpenAIRealtimeProvider, "custom_components.tacktab.voice_realtime_openai", "openai", "gpt-realtime", "marin"),
    (XAIRealtimeProvider, "custom_components.tacktab.voice_realtime_xai", "xai", "grok-voice-3.0", "eve"),
])
async def test_resume_replays_complete_tool_call_and_output_without_execution(monkeypatch, adapter, module, provider, model, voice):
    socket = Socket()
    monkeypatch.setattr(f"{module}.async_get_clientsession", lambda hass: Client(socket))
    instance = adapter(object())
    history = [{"role": "tool", "call_id": "call-1", "name": "intent__HassStartTimer", "arguments": {"minutes": 2}, "result": {"timer": "tea"}, "error": True}]
    await instance.connect(ProviderConfig(provider, model, voice, "key"), [], "prompt", {"history": history})
    call, output = socket.sent[-2:]
    assert call["item"]["type"] == "function_call" and call["item"]["name"] == "intent__HassStartTimer"
    assert json.loads(call["item"]["arguments"]) == {"minutes": 2}
    assert output["item"]["type"] == "function_call_output"
    assert json.loads(output["item"]["output"]) == {"result": {"timer": "tea"}, "error": True}


async def test_openai_no_active_response_cancel_race_keeps_session_usable(monkeypatch):
    socket = Socket([
        {"type": "error", "error": {"code": "response_cancel_not_active"}},
        {"type": "input_audio_buffer.speech_started"},
    ])
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_openai.async_get_clientsession", lambda hass: Client(socket))
    instance = OpenAIRealtimeProvider(object())
    await instance.connect(ProviderConfig("openai", "gpt-realtime", "marin", "key"), [], "prompt")
    assert [event.kind async for event in instance.events()] == ["speech_started"]


def test_stateful_resampler_preserves_duration_across_uneven_chunks():
    one = StatefulResampler(); split = StatefulResampler()
    source = b"\x01\x00" * 1600
    whole = one.convert(source) + one.flush()
    chunks = split.convert(source[:602]) + split.convert(source[602:]) + split.flush()
    assert whole == chunks
    assert len(whole) == 2400 * 2


def test_stateful_resampler_flushes_a_single_odd_sample():
    resampler = StatefulResampler()
    assert resampler.convert(b"\x02\x00") == b"\x02\x00"
    assert resampler.flush() == b""
    assert resampler.flush() == b""


def test_slot_configuration_bounds_and_has_no_secret_field():
    value = parse_slot_config({"mode": "realtime", "provider": "gemini", "model": "models/gemini-3.8-live", "voice": "Kore", "memory_timeout_ms": 1_800_000})
    assert value.public()["memory_timeout_ms"] == 1_800_000
    with pytest.raises(ValueError): parse_slot_config({"api_key": "must-not-persist-here"})


def test_audio_end_rejects_stop_only_forget_flag():
    message = {"id": 1, "type": "tacktab/realtime/audio_end", "epoch": "epoch", "session_id": "session", "forget": True}
    with pytest.raises(RealtimeProtocolError): parse_command(message)


def test_realtime_event_schema_is_versioned_and_declares_slot_bound_ready_formats():
    schema = json.loads((Path(__file__).parents[1] / "docs" / "realtime-events.schema.json").read_text())
    assert schema["$id"] == "urn:tacktab:realtime-events:v1"
    ready = schema["$defs"]["ready"]
    assert "input_format" in json.dumps(ready)
    assert "slot_id" in json.dumps(schema["$defs"]["base"])


async def test_catalog_uses_independent_live_metadata_and_sanitized_fallback(monkeypatch):
    entry = type("Entry", (), {"data": {"realtime_provider_keys": {"xai": "key-never-returned"}}})()
    calls = []
    async def get_json(hass, url, headers=None, params=None):
        calls.append(url)
        if url.endswith("/models"): return {"data": [{"id": "grok-voice-3.0"}]}
        return None
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_catalog._get_json", get_json)
    result = await async_catalog(object(), entry, "xai")
    assert result["models"] == ["grok-voice-3.0"]
    assert result["voices"] == ["eve"]
    assert result["validated"] is True and "key-never-returned" not in json.dumps(result)
    assert len(calls) == 2


async def test_catalog_failed_metadata_is_bundled_and_never_validates_a_saved_key(monkeypatch):
    entry = type("Entry", (), {"data": {"realtime_provider_keys": {"openai": "key-never-returned"}}})()
    async def get_json(*args, **kwargs): return None
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_catalog._get_json", get_json)
    result = await async_catalog(object(), entry, "openai")
    assert result["source"] == "bundled" and result["validated"] is False


async def test_bridge_disconnect_cancels_provider_and_running_tool_before_lease_release():
    order: list[str] = []
    class Provider:
        async def close(self): order.append("provider")
    class Bridge:
        def release_media_lease(self, session, owner): order.append("lease")
    session = SimpleNamespace(device=SimpleNamespace(realtime_active=True), emit=lambda event: None)
    run = RuntimeSession("run", session, "primary", SimpleNamespace(), Provider(), "owner")
    async def unfinished():
        try: await asyncio.sleep(60)
        finally: order.append("tool-cancelled")
    run.tool_tasks["call"] = __import__("asyncio").create_task(unfinished())
    await asyncio.sleep(0)
    controller = RealtimeController(SimpleNamespace(), Bridge())
    controller.sessions[run.session_id] = run
    await controller.async_close_for_session(session, "replaced")
    assert run.closed and controller.sessions == {}
    assert order == ["tool-cancelled", "provider", "lease"]
