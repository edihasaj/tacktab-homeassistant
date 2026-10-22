"""Realtime command controller wired by the voice websocket owner.

The bridge owns authentication, epochs and the physical media lease.  This
controller owns only provider sessions and never exposes provider exceptions.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback

from .voice_realtime_config import async_prepare_provider_store, RealtimeConfigError, get_slot, key_transport_allowed, parse_slot_config, provider_config, update_key, update_slot
from .voice_realtime_gemini import GeminiRealtimeProvider
from .voice_realtime_memory import RetainedContext, append_bounded, item_size
from .voice_realtime_openai import OpenAIRealtimeProvider
from .voice_realtime_protocol import RealtimeProtocolError, parse_command
from .voice_realtime_providers import ProviderEvent, RealtimeProviderError
from .voice_realtime_runtime import MAX_SESSION_SECONDS, RuntimeSession, async_end_audio, async_interrupt, async_playback, async_send_audio
from .voice_realtime_xai import XAIRealtimeProvider
from .voice_realtime_tools import async_bind_tools

REALTIME_DATA = "voice_realtime"
MAX_OUTPUT_CHUNK_BYTES = 32 * 1024
MAX_OUTPUT_FRAMES = 5 * 24_000
class RealtimeController:
    def __init__(self, hass: HomeAssistant, bridge: Any) -> None:
        self.hass, self.bridge, self.sessions, self.pending, self.memory = hass, bridge, {}, {}, {}
        self.memory_tasks: dict[tuple[str, ...], asyncio.Task[None]] = {}
        self.closed_sessions: dict[str, tuple[Any, tuple[str, ...] | None]] = {}
        self.provider_locks: dict[tuple[str, str], asyncio.Lock] = {}
    def _bridge_session(self, connection: Any) -> Any:
        try:
            return self.bridge.session_for_connection(connection)
        except Exception as exc:
            raise RealtimeProtocolError("disconnected", "Realtime connection is not paired") from exc
    async def async_close_for_session(self, bridge_session: Any, reason: str = "disconnected") -> None:
        """Close every provider run tied to a replaced/unloaded bridge epoch."""
        runs = [run for run in (*self.sessions.values(), *self.pending.values()) if run.bridge_session is bridge_session]
        for run in runs:
            await self.close(run, reason, False)
    @staticmethod
    def _entry(session: Any) -> Any:
        entry = getattr(session.device, "entry", None)
        if entry is None: raise RealtimeProtocolError("disconnected", "Realtime device is not ready")
        return entry
    @staticmethod
    def _emit(session: Any, kind: str, **data: Any) -> None:
        session.emit({"event": "realtime", "kind": kind, **data})
    async def handle(self, connection: Any, raw: dict[str, Any]) -> None:
        msg = parse_command(raw)
        bridge_session = self._bridge_session(connection)
        try: bridge_session.validate_epoch(msg)
        except Exception as exc: raise RealtimeProtocolError("stale_epoch", "Stale realtime session") from exc
        entry = self._entry(bridge_session)
        command = msg["type"].rsplit("/", 1)[-1]
        if command == "configure_provider":
            self._admin(connection)
            await async_prepare_provider_store(self.hass, entry)
            if not key_transport_allowed(self.hass, connection, entry):
                raise RealtimeProtocolError("insecure_transport", "Provider keys require a trusted transport")
            async with self._provider_lock(entry, msg["provider"]):
                self._provider_idle(bridge_session.device, msg["provider"])
                result = update_key(self.hass, entry, msg["provider"], msg["key_action"], msg.get("api_key"))
                self._purge_provider_memory(bridge_session.device, msg["provider"])
            connection.send_result(msg["id"], result)
        elif command == "configure_slot":
            self._admin(connection)
            async with bridge_session.device.config_lock:
                self._slot_idle(bridge_session.device, msg["slot_id"])
                if msg["expected_config_revision"] != bridge_session.device.config_revision: raise RealtimeProtocolError("config_conflict", "Realtime configuration changed")
                try:
                    bridge_session.device.slot(msg["slot_id"])
                    candidate = parse_slot_config(msg["config"])
                    if candidate.mode == "realtime":
                        if not bridge_session.device.device_id: raise RealtimeConfigError("Device is not ready")
                        await async_bind_tools(self.hass, connection, bridge_session.device.device_id, candidate.llm_api_ids, candidate.language)
                except Exception as exc:
                    raise RealtimeProtocolError("invalid_format", "Realtime slot configuration is unavailable") from exc
                result = update_slot(self.hass, entry, msg["slot_id"], msg["config"])
                bridge_session.device.config_revision += 1
                revision = bridge_session.device.config_revision
                self._purge_memory(bridge_session.device, msg["slot_id"])
            connection.send_result(msg["id"], {"config_revision": revision, "config": result.public()})
        elif command == "catalog": connection.send_result(msg["id"], await self.catalog(entry, msg["provider"]))
        elif command == "start": await self.start(connection, bridge_session, entry, msg)
        elif command == "audio": await self.audio(connection, msg)
        elif command == "audio_end": await self.audio_end(connection, msg)
        elif command == "playback": await self.playback(connection, msg)
        elif command == "interrupt": await self.interrupt(connection, msg)
        elif command == "stop": await self.stop(connection, msg)
    @staticmethod
    def _admin(connection: Any) -> None:
        if getattr(getattr(connection, "user", None), "is_admin", False) is not True: raise RealtimeProtocolError("unauthorized", "Administrator access is required")
    def _slot_idle(self, device: Any, slot_id: str) -> None:
        if any(run.bridge_session.device is device and run.slot_id == slot_id for run in (*self.sessions.values(), *self.pending.values())):
            raise RealtimeProtocolError("busy", "Realtime session is active")
    def _provider_idle(self, device: Any, provider: str) -> None:
        if any(
            run.setting.provider == provider
            for run in (*self.sessions.values(), *self.pending.values())
        ):
            raise RealtimeProtocolError("busy", "Realtime session is active")
    def _provider_lock(self, entry: Any, provider: str) -> asyncio.Lock:
        key = ("shared", provider)
        return self.provider_locks.setdefault(key, asyncio.Lock())
    async def catalog(self, entry: Any, provider: str) -> dict[str, Any]:
        from .voice_realtime_catalog import async_catalog
        await async_prepare_provider_store(self.hass, entry)
        result = await async_catalog(self.hass, entry, provider)
        device = next((device for device in self.bridge.devices.values() if getattr(device, "entry", None) is entry), None)
        slots = {slot.slot_id: get_slot(entry, slot.slot_id).public() for slot in getattr(device, "slots", ())}
        return {**result, "realtime_slots": slots, "config_revision": getattr(device, "config_revision", 0)}
    def _purge_memory(self, device: Any, slot_id: str | None = None) -> None:
        now = time.monotonic()
        for key, value in tuple(self.memory.items()):
            if value.expires_at <= now or (key[1] == device.install_id and (slot_id is None or key[2] == slot_id)):
                self._forget_memory(key)
    def _purge_provider_memory(self, device: Any, provider: str) -> None:
        for key in tuple(self.memory):
            if key[3] == provider:
                self._forget_memory(key)
    def _purge_other_identities(self, key: tuple[str, ...]) -> None:
        for other in tuple(self.memory):
            if other[1:3] == key[1:3] and other[0] != key[0]:
                self._forget_memory(other)
    def _forget_memory(self, key: tuple[str, ...]) -> None:
        self.memory.pop(key, None)
        task = self.memory_tasks.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
    async def _expire_memory(self, key: tuple[str, ...], expires_at: float) -> None:
        try:
            await asyncio.sleep(max(0, expires_at - time.monotonic()))
            retained = self.memory.get(key)
            if retained is not None and retained.expires_at <= time.monotonic():
                self.memory.pop(key, None)
        except asyncio.CancelledError:
            return
        except Exception:
            if not run.closed:
                await self.close(run, "provider_error", False)
        finally:
            if self.memory_tasks.get(key) is asyncio.current_task():
                self.memory_tasks.pop(key, None)
    def _memory_key(self, connection: Any, bridge_session: Any, setting: Any, slot_id: str) -> tuple[str, ...]:
        user = str(getattr(getattr(connection, "user", None), "id", ""))
        return (user, bridge_session.device.install_id, slot_id, setting.provider, setting.model, setting.voice)
    async def start(self, connection: Any, bridge_session: Any, entry: Any, msg: dict[str, Any]) -> None:
        active = next((runtime for runtime in self.sessions.values() if runtime.bridge_session is bridge_session), None)
        if active is not None:
            if msg["trigger"] != "follow_up" or active.slot_id != msg["slot_id"] or active.idle_task is None:
                raise RealtimeProtocolError("busy", "Realtime session is active")
            self._cancel_idle(active); active.discard_audio = False
            connection.send_result(msg["id"], {"session_id": active.session_id, "input_format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, "output_format": {"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1}})
            return
        setting = get_slot(entry, msg["slot_id"])
        try: bridge_session.device.slot(msg["slot_id"])
        except Exception as exc: raise RealtimeProtocolError("slot_disabled", "Realtime slot is not configured") from exc
        if setting.mode != "realtime": raise RealtimeProtocolError("slot_disabled", "Realtime mode is disabled")
        if not bridge_session.device.status.microphone_ready: raise RealtimeProtocolError("capture_unavailable", "Microphone is unavailable")
        if not isinstance(bridge_session.device.device_id, str) or not bridge_session.device.device_id:
            raise RealtimeProtocolError("disconnected", "Realtime device is not ready")
        await async_prepare_provider_store(self.hass, entry)
        if msg["trigger"] == "wake_word":
            if not self.bridge.consume_wake_grant(bridge_session, msg["slot_id"], msg["grant_id"], msg["wake_word_phrase"]):
                raise RealtimeProtocolError("invalid_grant", "Wake grant is invalid")
        owner = "realtime:" + str(uuid4())
        try: self.bridge.acquire_media_lease(bridge_session, owner)
        except Exception as exc: raise RealtimeProtocolError("busy", "Voice device is busy") from exc
        provider = {"openai": OpenAIRealtimeProvider, "xai": XAIRealtimeProvider, "gemini": GeminiRealtimeProvider}[setting.provider](self.hass)
        runtime = RuntimeSession(str(uuid4()), bridge_session, msg["slot_id"], setting, provider, owner)
        runtime.memory_key = self._memory_key(connection, bridge_session, setting, msg["slot_id"])
        self._purge_other_identities(runtime.memory_key)
        retained = self.memory.get(runtime.memory_key)
        if retained and retained.expires_at <= time.monotonic():
            self._forget_memory(runtime.memory_key)
            retained = None
        if retained:
            runtime.history = list(retained.items)
            runtime.history_bytes = sum(item_size(item) for item in runtime.history)
            runtime.resume = retained.resume
        runtime.setup_task = asyncio.current_task()
        self.pending[runtime.session_id] = runtime
        try:
            self._purge_memory(bridge_session.device)
            async with asyncio.timeout(15):
                runtime.tools = await async_bind_tools(self.hass, connection, bridge_session.device.device_id, setting.llm_api_ids, setting.language)
                if runtime.closed: raise asyncio.CancelledError
                instructions = "\n\n".join(value for value in (runtime.tools.prompt, setting.instructions) if value)
                resume = {**(runtime.resume or {}), "history": runtime.history} if (runtime.resume or runtime.history) else None
                await provider.connect(provider_config(entry, setting, self.hass), runtime.tools.specs, instructions, resume)
                if runtime.closed: raise asyncio.CancelledError
                runtime.task = self.hass.async_create_task(self._events(runtime), f"tacktab_realtime_{runtime.session_id}")
                await runtime.ready.wait()
                if runtime.closed or runtime.setup_failed:
                    raise RealtimeProviderError("Realtime provider rejected setup")
        except Exception:
            if runtime.task is not None: runtime.task.cancel()
            try:
                await provider.close()
            except Exception:
                pass
            finally:
                await self._release(runtime)
                bridge_session.device.realtime_active = False
            raise RealtimeProtocolError("timeout", "Realtime provider could not start") from None
        except asyncio.CancelledError:
            if not runtime.closed:
                await self.close(runtime, "disconnected", False)
            raise
        finally:
            self.pending.pop(runtime.session_id, None)
            runtime.setup_task = None
        self.sessions[runtime.session_id] = runtime; bridge_session.device.realtime_active = True
        runtime.max_task = self.hass.async_create_task(self._close_after(runtime, MAX_SESSION_SECONDS, "session_limit"), f"tacktab_realtime_limit_{runtime.session_id}")
        connection.send_result(msg["id"], {"session_id": runtime.session_id, "input_format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, "output_format": {"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1}})
        self._emit(bridge_session, "ready", session_id=runtime.session_id, slot_id=runtime.slot_id, provider=runtime.setting.provider, model=runtime.setting.model, voice=runtime.setting.voice, input_format={"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, output_format={"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1})
    def _runtime(self, connection: Any, sid: str) -> RuntimeSession:
        result = self.sessions.get(sid)
        if result is None or result.closed or result.bridge_session is not self._bridge_session(connection): raise RealtimeProtocolError("unknown_run", "Realtime session is not active")
        return result
    async def audio(self, connection: Any, msg: dict[str, Any]) -> None:
        await async_send_audio(self, connection, msg)
    async def audio_end(self, connection: Any, msg: dict[str, Any]) -> None:
        await async_end_audio(self, connection, msg)
    async def interrupt(self, connection: Any, msg: dict[str, Any]) -> None:
        await async_interrupt(self, connection, msg)
    async def _interrupt_playback(self, run: RuntimeSession, playback: str, frames: int) -> None:
        """Retire output using the physical frame count, before accepting a new turn."""
        retired = playback in run.retired_playbacks
        if retired and frames <= run.played_frames.get(playback, 0):
            return
        failure: Exception | None = None
        try:
            await run.provider.interrupt(run.playback_items.get(playback), frames)
        except Exception as exc:
            failure = exc
        run.played_frames[playback] = frames
        run.retired_playbacks.add(playback)
        run.output_ended.add(playback)
        run.discard_audio = True
        if not retired:
            run.accept_tool_calls = False
            run.generation += 1
        for task in tuple(run.tool_tasks.values()):
            task.cancel()
        if not retired:
            self._emit(
                run.bridge_session,
                "interrupted",
                session_id=run.session_id,
                slot_id=run.slot_id,
                playback_id=playback,
            )
        if failure is not None:
            raise failure
    async def playback(self, connection: Any, msg: dict[str, Any]) -> None:
        await async_playback(self, connection, msg)
    async def stop(self, connection: Any, msg: dict[str, Any]) -> None:
        try:
            run = self._runtime(connection, msg["session_id"])
        except RealtimeProtocolError:
            closed = self.closed_sessions.get(msg["session_id"])
            if not msg.get("forget") or closed is None or closed[0] is not self._bridge_session(connection):
                raise
            if closed[1] is not None:
                self._forget_memory(closed[1])
            connection.send_result(msg["id"])
            return
        await self.close(run, "stopped", bool(msg.get("forget")))
        connection.send_result(msg["id"])
    async def _events(self, run: RuntimeSession) -> None:
        try:
            async for event in run.provider.events(): await self._event(run, event)
        except (RealtimeProviderError, asyncio.CancelledError):
            if not run.closed: await self.close(run, "provider_error", False)
        except Exception:
            if not run.closed: await self.close(run, "provider_error", False)
        else:
            if not run.closed:
                run.setup_failed = not run.ready.is_set()
                run.ready.set()
                await self.close(run, "provider_closed", False)
    async def _event(self, run: RuntimeSession, event: ProviderEvent) -> None:
        if event.kind == "ready": run.ready.set()
        elif event.kind == "audio" and event.audio is not None:
            if run.discard_audio: return
            self._cancel_idle(run)
            run.generation_had_audio = True
            if len(event.audio) % 2:
                await self.close(run, "provider_error", False)
                return
            frames = len(event.audio) // 2
            playback = run.playback_id if run.playback_id not in run.output_ended else None
            playback = playback or str(uuid4()); run.playback_id = playback
            outstanding = sum(
                sent - run.played_frames.get(item, 0)
                for item, sent in run.sent_frames.items()
                if item not in run.retired_playbacks
            )
            if outstanding + frames > MAX_OUTPUT_FRAMES: await self.close(run, "audio_overflow", False); return
            if event.item_id: run.playback_items[playback] = event.item_id
            import base64
            for offset in range(0, len(event.audio), MAX_OUTPUT_CHUNK_BYTES):
                chunk = event.audio[offset:offset + MAX_OUTPUT_CHUNK_BYTES]
                sequence = run.output_chunks.get(playback, 0)
                run.output_chunks[playback] = sequence + 1
                chunk_frames = len(chunk) // 2
                run.sent_frames[playback] = run.sent_frames.get(playback, 0) + chunk_frames
                self._emit(run.bridge_session, "audio", session_id=run.session_id, slot_id=run.slot_id, playback_id=playback, item_id=event.item_id or "", chunk_seq=sequence, audio=base64.b64encode(chunk).decode(), frames=chunk_frames)
        elif event.kind == "generation_end":
            awaiting_tools = run.pending_continue
            if run.generation_had_audio and run.playback_id:
                playback = run.playback_id
                run.output_ended.add(playback)
                self._emit(run.bridge_session, "audio_end", session_id=run.session_id, slot_id=run.slot_id, playback_id=playback)
            if run.pending_continue: run.response_done = True
            await self._maybe_continue(run)
            # A cancelled response may have late chunks in flight.  It is now
            # safe to accept the next generation, but a follow-up window still
            # waits for a physical playback completion ACK.
            run.discard_audio = False
            run.accept_tool_calls = True
            if not run.generation_had_audio and not awaiting_tools and (
                run.playback_id is None or run.playback_id not in run.retired_playbacks
            ):
                self._schedule_idle_close(run)
            run.generation_had_audio = False
        elif event.kind == "speech_started":
            self._cancel_idle(run)
            if run.playback_id and run.playback_id not in run.retired_playbacks:
                await self._interrupt_playback(
                    run,
                    run.playback_id,
                    run.played_frames.get(run.playback_id, 0),
                )
            self._emit(run.bridge_session, "speech_started", session_id=run.session_id, slot_id=run.slot_id, playback_id=run.playback_id)
        elif event.kind == "speech_stopped":
            # A provider VAD turn boundary starts the next generation.
            run.utterance_bytes = 0
            self._emit(run.bridge_session, "speech_stopped", session_id=run.session_id, slot_id=run.slot_id, playback_id=run.playback_id)
        elif event.kind == "interrupted":
            self._cancel_idle(run)
            if run.playback_id and run.playback_id not in run.retired_playbacks:
                await self._interrupt_playback(
                    run,
                    run.playback_id,
                    run.played_frames.get(run.playback_id, 0),
                )
        elif event.kind == "transcript":
            text = event.text or ""
            self._emit(run.bridge_session, "transcript", session_id=run.session_id, slot_id=run.slot_id, role=event.role, item_id=event.item_id or "", text=text, operation=event.operation, final=event.final)
            if event.final and event.role in {"user", "assistant"}:
                run.history_bytes = append_bounded(
                    run.history, {"role": event.role, "text": text}, run.history_bytes
                )
        elif event.kind == "resume" and event.handle:
            run.resume = {"handle": event.handle}
        elif event.kind == "tool_call" and event.call_id and event.name and event.arguments is not None:
            key = (run.generation, event.call_id)
            if not run.accept_tool_calls or key in run.called or run.tools is None: return
            run.called[key] = True
            run.pending_continue = True
            task = self.hass.async_create_task(self._run_tool(run, event, run.generation), f"tacktab_realtime_tool_{event.call_id}")
            run.tool_tasks[event.call_id] = task
        elif event.kind == "tool_cancel":
            for call_id in event.cancelled_ids:
                task = run.tool_tasks.get(call_id)
                if task is not None: task.cancel()
        elif event.kind == "error":
            run.setup_failed = True
            run.ready.set()
            if run.session_id in self.sessions:
                await self.close(run, "provider_error", False)
    async def _run_tool(self, run: RuntimeSession, event: ProviderEvent, generation: int) -> None:
        assert event.call_id and event.name and event.arguments is not None and run.tools is not None
        self._emit(run.bridge_session, "tool_state", session_id=run.session_id, slot_id=run.slot_id, call_id=event.call_id, state="running")
        try:
            try:
                async with asyncio.timeout(30):
                    result, failed = await run.tools.call(event.name, event.arguments, event.call_id)
            except TimeoutError:
                result, failed = {"error": "Tool timed out"}, True
            if run.closed or generation != run.generation or not run.accept_tool_calls:
                return
            # Preserve the observed HA outcome even if sending it fails. A
            # reopened conversation must not blindly repeat a completed action.
            run.history_bytes = append_bounded(
                run.history,
                {"role": "tool", "call_id": event.call_id, "name": event.name,
                 "arguments": event.arguments, "result": result, "error": failed},
                run.history_bytes,
            )
            await run.provider.send_tool_result(event.call_id, event.name, result, failed)
            self._emit(run.bridge_session, "tool_state", session_id=run.session_id, slot_id=run.slot_id, call_id=event.call_id, state="failed" if failed else "completed")
        except asyncio.CancelledError:
            return
        except Exception:
            await self.close(run, "provider_error", False)
        finally:
            if run.tool_tasks.get(event.call_id) is asyncio.current_task():
                run.tool_tasks.pop(event.call_id, None)
            if generation == run.generation and run.accept_tool_calls:
                await self._maybe_continue(run)
    async def _maybe_continue(self, run: RuntimeSession) -> None:
        """Continue only after every call and physical prior playback complete."""
        if not run.pending_continue or not run.response_done or run.tool_tasks or run.closed or not run.accept_tool_calls:
            return
        if any(run.played_frames.get(item, 0) != sent for item, sent in run.sent_frames.items() if item in run.output_ended and item not in run.retired_playbacks):
            return
        run.pending_continue = False; run.response_done = False
        try:
            await run.provider.continue_after_tools()
        except Exception:
            await self.close(run, "provider_error", False)
    def _schedule_idle_close(self, run: RuntimeSession) -> None:
        if run.closed or run.idle_task is not None: return
        seconds = run.setting.follow_up_timeout_ms / 1000
        run.idle_task = self.hass.async_create_task(self._close_after(run, seconds, "follow_up_expired"), f"tacktab_realtime_idle_{run.session_id}")
    @staticmethod
    def _cancel_idle(run: RuntimeSession) -> None:
        if run.idle_task is not None:
            run.idle_task.cancel()
            run.idle_task = None
    async def _close_after(self, run: RuntimeSession, seconds: float, reason: str) -> None:
        try:
            await asyncio.sleep(seconds)
            if not run.closed: await self.close(run, reason, False)
        except asyncio.CancelledError:
            return
    async def close(self, run: RuntimeSession, reason: str, forget: bool) -> None:
        if run.closed: return
        run.closed = True; self.sessions.pop(run.session_id, None)
        self.pending.pop(run.session_id, None)
        if run.setup_task and run.setup_task is not asyncio.current_task(): run.setup_task.cancel()
        if run.task and run.task is not asyncio.current_task(): run.task.cancel()
        for timer in (run.idle_task, run.max_task):
            if timer and timer is not asyncio.current_task(): timer.cancel()
        tasks = tuple(task for task in run.tool_tasks.values() if task is not asyncio.current_task())
        for task in tasks: task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        run.tool_tasks.clear()
        retained = False
        try:
            try:
                provider_resume = await run.provider.close()
            except Exception:
                # A transport teardown failure must never leak a physical lease
                # or claim that state was retained.
                provider_resume = None
                forget = True
            if provider_resume:
                run.resume = provider_resume
            if not forget and run.memory_key and run.setting.memory_timeout_ms and (run.history or run.resume):
                expires_at = time.monotonic() + run.setting.memory_timeout_ms / 1000
                self._forget_memory(run.memory_key)
                self.memory[run.memory_key] = RetainedContext(expires_at, list(run.history), run.resume)
                self.memory_tasks[run.memory_key] = self.hass.async_create_task(
                    self._expire_memory(run.memory_key, expires_at),
                    f"tacktab_realtime_memory_{run.session_id}",
                )
                retained = True
            elif run.memory_key:
                self._forget_memory(run.memory_key)
        finally:
            await self._release(run)
            run.bridge_session.device.realtime_active = False
            self.closed_sessions[run.session_id] = (run.bridge_session, run.memory_key)
            while len(self.closed_sessions) > 64:
                self.closed_sessions.pop(next(iter(self.closed_sessions)))
            self._emit(run.bridge_session, "closed", session_id=run.session_id, slot_id=run.slot_id, reason=reason, memory_retained=retained, recoverable=False)
    async def _release(self, run: RuntimeSession) -> None:
        if run.lease_released:
            return
        run.lease_released = True
        try:
            self.bridge.release_media_lease(run.bridge_session, run.owner)
        except Exception:
            return
@callback
def async_setup_realtime(hass: HomeAssistant, bridge: Any | None = None) -> RealtimeController:
    """Return the provider controller without initializing a provider socket."""
    if bridge is None:
        from .voice_bridge import async_get_bridge
        bridge = async_get_bridge(hass)
    return hass.data.setdefault(REALTIME_DATA, RealtimeController(hass, bridge))
