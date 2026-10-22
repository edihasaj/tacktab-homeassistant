"""Stateful websocket transport for native Tacktab Assist satellites.

The transport owns identity, epochs, sequencing and physical-device ownership.
Assist entities live in :mod:`voice_entities`; keeping this module independent
from their imports lets the regular HTTP integration run on older HA releases.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.core import HomeAssistant, callback

from .voice_models import DEFAULT_VOICE_STATUS, SlotConfig, VoiceCapabilities, VoiceStatus, normalized_phrase
from .voice_protocol import VoiceProtocolError, parse_slots, parse_status, validate_message
from .voice_wake import WakeArbiter, WakeClaim

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection


VOICE_DATA = "voice_bridge"
MAX_PCM_FRAME_BYTES = 32 * 1024
MAX_UTTERANCE_BYTES = 60 * 16_000 * 2
MAX_AUDIO_QUEUE_BYTES = 2 * 16_000 * 2
ACTION_TIMEOUT_SECONDS = 300


class VoiceBridgeError(VoiceProtocolError):
    """A known bridge failure suitable for a websocket result error."""


@dataclass(slots=True)
class PendingAction:
    future: asyncio.Future[None]
    slot_id: str
    kind: str
    completed: str | None = None


@dataclass(slots=True)
class VoiceRun:
    run_id: str
    slot_id: str
    pipeline_id: str | None
    audio: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    queued_audio_bytes: int = 0
    utterance_audio_bytes: int = 0
    closed: bool = False
    task: asyncio.Task[None] | None = None
    context_message: dict[str, Any] | None = None

    def accept_audio(self, payload: bytes) -> None:
        if self.closed:
            return
        if not payload:
            self.finish_audio()
            return
        if len(payload) > MAX_PCM_FRAME_BYTES or len(payload) % 2:
            raise VoiceBridgeError("invalid_format", "Invalid PCM frame")
        if self.queued_audio_bytes + len(payload) > MAX_AUDIO_QUEUE_BYTES:
            self.finish_audio()
            raise VoiceBridgeError("audio_overflow", "Audio queue is full")
        if self.utterance_audio_bytes + len(payload) > MAX_UTTERANCE_BYTES:
            self.finish_audio()
            raise VoiceBridgeError("audio_overflow", "Utterance is too long")
        self.queued_audio_bytes += len(payload)
        self.utterance_audio_bytes += len(payload)
        self.audio.put_nowait(payload)

    def finish_audio(self) -> None:
        if not self.closed:
            self.closed = True
            self.audio.put_nowait(None)

    async def stream(self):
        while True:
            chunk = await self.audio.get()
            if chunk is None:
                return
            self.queued_audio_bytes -= len(chunk)
            yield chunk

    async def cancel(self) -> None:
        self.finish_audio()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class VoiceSession:
    """One authenticated connection for one install and a single epoch."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: "VoiceDevice",
        connection: ActiveConnection,
        subscription_id: int,
        binary_handler_id: int,
        unsub_binary: Callable[[], None],
    ) -> None:
        self.hass = hass
        self.device = device
        self.connection = connection
        self.subscription_id = subscription_id
        self.binary_handler_id = binary_handler_id
        self._unsub_binary = unsub_binary
        self.epoch = str(uuid4())
        self.seq = 0
        self.runs: dict[str, VoiceRun] = {}
        self.actions: dict[str, PendingAction] = {}
        self.completed_actions: OrderedDict[str, str] = OrderedDict()
        self.follow_up_tasks: dict[str, asyncio.Task[None]] = {}
        self.closed = False

    @property
    def install_id(self) -> str:
        return self.device.install_id

    def emit(self, event: dict[str, Any]) -> None:
        if self.closed:
            return
        self.seq += 1
        self.connection.send_event(self.subscription_id, {"epoch": self.epoch, "seq": self.seq, **event})

    def emit_claim(self, claim: WakeClaim, *, granted: bool, reason: str) -> None:
        grant_id = str(uuid4()) if granted else None
        if granted:
            self.device.grants[grant_id] = (self.epoch, claim.slot_id, claim.phrase, time.monotonic() + 2)
        self.emit({
            "event": "claim_result", "claim_id": claim.claim_id, "slot_id": claim.slot_id,
            "decision": "granted" if granted else "denied", "grant_id": grant_id,
            "grant_expires_ms": 2000 if granted else 0, "reason": reason,
        })

    async def close(self, reason: str, *, emit: bool = True) -> None:
        if self.closed:
            return
        if emit:
            self.emit({"event": "session_closed", "reason": reason})
        self.closed = True
        self._unsub_binary()
        if controller := self.hass.data.get("voice_realtime"):
            await controller.async_close_for_session(self, reason)
        for action in self.actions.values():
            if not action.future.done():
                action.future.set_exception(VoiceBridgeError("disconnected", "Voice connection closed"))
        for run in tuple(self.runs.values()):
            satellite = getattr(self.device, "satellites", {}).get(run.slot_id)
            if satellite is not None:
                await satellite.async_cancel_response(self, run.run_id)
            await run.cancel()
        self.runs.clear()
        for satellite in getattr(self.device, "satellites", {}).values():
            satellite.async_fail_pending_question("Voice tablet disconnected")
        for task in self.follow_up_tasks.values():
            task.cancel()
        self.follow_up_tasks.clear()
        self.device.grants = {key: grant for key, grant in self.device.grants.items() if grant[0] != self.epoch}
        self.device.follow_up_until.clear()
        if self.device.media_lease and self.device.media_lease[0] == self.epoch:
            self.device.media_lease = None
        if self.device.session is self:
            self.device.session = None

    def validate_epoch(self, msg: dict[str, Any]) -> None:
        if self.closed or self.device.session is not self or msg["epoch"] != self.epoch:
            raise VoiceBridgeError("stale_epoch", "Stale voice session")

    def is_claim_eligible(self, claim: WakeClaim) -> bool:
        """Revalidate a candidate at the fixed SNR-window close."""
        try:
            slot = self.device.slot(claim.slot_id)
        except VoiceBridgeError:
            return False
        return (
            not self.closed
            and self.device.session is self
            and slot.enabled
            and self.device.status.microphone_ready
            and not self.device.config_lock.locked()
            and not self.runs
            and not self.actions
            and self.device.media_lease is None
            and not getattr(self.device, "realtime_active", False)
        )

    def on_binary(self, _hass: HomeAssistant, _connection: ActiveConnection, payload: bytes) -> None:
        if self.closed or not self.runs:
            return
        # A binary handler is exclusive to one physical microphone run.
        run = next(iter(self.runs.values()))
        try:
            run.accept_audio(payload)
        except VoiceBridgeError:
            self.hass.async_create_task(self._fail_run(run, "audio_overflow"), "tacktab_voice_audio_overflow")

    async def _fail_run(self, run: VoiceRun, reason: str) -> None:
        satellite = getattr(self.device, "satellites", {}).get(run.slot_id)
        if satellite is not None:
            await satellite.async_cancel_response(self, run.run_id)
        await run.cancel()
        self.runs.pop(run.run_id, None)
        async_get_bridge(self.hass).release_media_lease(self, run.run_id)
        self.emit({"event": "cancel_action", "action_id": run.run_id, "reason": reason})

    async def play(
        self, *, slot_id: str, kind: str, message: str, media_id: str, preannounce_media_id: str | None,
        listen_after: bool, run_id: str | None = None, timeout: int = ACTION_TIMEOUT_SECONDS,
    ) -> None:
        if self.closed or not self.device.status.playback_ready:
            raise VoiceBridgeError("playback_unavailable", "Playback is unavailable")
        if self.device.capabilities is None:
            raise VoiceBridgeError("playback_unavailable", "Voice capabilities are unavailable")
        if self.device.slot(slot_id).output == "ha_media_player":
            raise VoiceBridgeError("unsupported_output", "Remote speaker completion is unavailable")
        if listen_after and (not self.device.slot(slot_id).enabled or not self.device.status.microphone_ready):
            raise VoiceBridgeError("mic_permission", "Microphone is unavailable for a follow-up")
        if kind == "announcement" and not self.device.capabilities.announcements:
            raise VoiceBridgeError("playback_unavailable", "Announcements are unavailable")
        if kind in ("start_conversation", "question") and not self.device.capabilities.start_conversation:
            raise VoiceBridgeError("playback_unavailable", "Conversation prompts are unavailable")
        action_id = str(uuid4())
        lease_owner = run_id or action_id
        bridge = async_get_bridge(self.hass)
        bridge.acquire_media_lease(self, lease_owner)
        future: asyncio.Future[None] = self.hass.loop.create_future()
        self.actions[action_id] = PendingAction(future, slot_id, kind)
        self.emit({
            "event": "play", "action_id": action_id, "run_id": run_id, "slot_id": slot_id,
            "kind": kind, "message": message, "media_id": media_id,
            "preannounce_media_id": preannounce_media_id, "listen_after": listen_after,
            "timeout_ms": timeout * 1000,
        })
        try:
            async with asyncio.timeout(timeout):
                await future
            if listen_after:
                self.device.follow_up_until[slot_id] = time.monotonic() + 15
                prior = self.follow_up_tasks.pop(slot_id, None)
                if prior is not None:
                    prior.cancel()
                self.follow_up_tasks[slot_id] = self.hass.async_create_task(
                    self._expire_follow_up(slot_id), f"tacktab_voice_follow_up_{slot_id}"
                )
        except TimeoutError as err:
            self.emit({"event": "cancel_action", "action_id": action_id, "reason": "timeout"})
            raise VoiceBridgeError("timeout", "Playback acknowledgement timed out") from err
        finally:
            self.actions.pop(action_id, None)
            self.completed_actions[action_id] = "finished" if future.done() and not future.cancelled() and future.exception() is None else "failed"
            while len(self.completed_actions) > 128:
                self.completed_actions.popitem(last=False)
            if run_id is None:
                bridge.release_media_lease(self, lease_owner)

    async def _expire_follow_up(self, slot_id: str) -> None:
        await asyncio.sleep(15)
        if self.device.follow_up_until.pop(slot_id, 0) <= time.monotonic():
            satellite = getattr(self.device, "satellites", {}).get(slot_id)
            if satellite is not None:
                satellite.async_fail_pending_question("Voice follow-up timed out")


class VoiceDevice:
    """Persistent per-install bridge state. HA entity wiring is added separately."""

    def __init__(self, hass: HomeAssistant, install_id: str, name: str, platform: str, slots: tuple[SlotConfig, ...]) -> None:
        self.hass = hass
        self.install_id = install_id
        # Assigned from Home Assistant's device registry during voice-entry setup.
        # It is deliberately distinct from the app-generated install_id.
        self.device_id: str | None = None
        self.name = name
        self.platform = platform
        self.slots = slots
        self.status = DEFAULT_VOICE_STATUS
        self.capabilities: VoiceCapabilities | None = None
        self.config_revision = 0
        self.timer_revision = 0
        self.session: VoiceSession | None = None
        self.grants: dict[str, tuple[str, str, str, float]] = {}
        self.follow_up_until: dict[str, float] = {}
        self.media_lease: tuple[str, str] | None = None
        self.config_lock = asyncio.Lock()
        self.start_run: Callable[[VoiceRun, dict[str, Any]], Awaitable[None]] | None = None

    def snapshot(self) -> dict[str, Any]:
        timers, ringing = getattr(self, "timer_adapter", None).snapshot() if hasattr(self, "timer_adapter") else ([], [])
        pipelines = self._pipelines()
        preferred = next((item["pipeline_id"] for item in pipelines if item.get("preferred")), None)
        return {
            "device_id": self.device_id or self.install_id,
            "slots": [
                {
                    "slot_id": slot.slot_id,
                    "satellite_id": getattr(getattr(self, "satellites", {}).get(slot.slot_id), "entity_id", ""),
                    "pipeline_select_entity_id": getattr(self, "pipeline_entities", {}).get(slot.slot_id, ""),
                    "config": slot.as_dict(), "resolved_pipeline_id": slot.pipeline_id or preferred,
                    "state": "unavailable" if not slot.enabled or self.session is None else getattr(getattr(self, "satellites", {}).get(slot.slot_id), "state", "unavailable"),
                }
                for slot in self.slots
            ],
            "pipelines": [{key: value for key, value in pipeline.items() if key != "preferred"} for pipeline in pipelines],
            "status": self.status.as_dict(), "timers": timers, "ringing_timer_ids": ringing,
            "config_revision": self.config_revision, "timer_revision": self.timer_revision,
        }

    def _pipelines(self) -> list[dict[str, Any]]:
        """Read Core's pipeline collection only once Assist is loaded."""
        try:
            from homeassistant.components.assist_pipeline import async_get_pipelines

            return [
                {
                    "pipeline_id": pipeline.id, "name": pipeline.name, "language": pipeline.language,
                    "conversation_engine": pipeline.conversation_engine, "tts_engine": pipeline.tts_engine,
                    "wake_word_entity": pipeline.wake_word_entity, "preferred": pipeline.id == self._preferred_pipeline_id(),
                }
                for pipeline in async_get_pipelines(self.hass)
            ]
        except Exception:
            return []

    def _preferred_pipeline_id(self) -> str | None:
        try:
            from homeassistant.components.assist_pipeline import KEY_ASSIST_PIPELINE

            return self.hass.data[KEY_ASSIST_PIPELINE].pipeline_store.async_get_preferred_item()
        except Exception:
            return None

    def slot(self, slot_id: str) -> SlotConfig:
        for slot in self.slots:
            if slot.slot_id == slot_id:
                return slot
        raise VoiceBridgeError("slot_disabled", "Voice slot is not configured")


class VoiceBridge:
    """Global coordinator. It is intentionally separate from Tacktab's HTTP API."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.devices: dict[str, VoiceDevice] = {}
        self.arbiter = WakeArbiter()

    def get_or_create(self, msg: dict[str, Any]) -> VoiceDevice:
        install_id = msg["install_id"]
        device = self.devices.get(install_id)
        if device is None:
            slots = parse_slots(msg["slots"])
            device = VoiceDevice(self.hass, install_id, msg["name"], msg["platform"], slots)
            self.devices[install_id] = device
        self._validate_pipelines(device, device.slots)
        return device

    async def connect(self, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        device = self.get_or_create(msg)
        if device.session is not None:
            await device.session.close("replaced")
        device.status = parse_status(msg["status"])
        device.capabilities = VoiceCapabilities(**msg["capabilities"])
        binary_id, unsub_binary = connection.async_register_binary_handler(lambda h, c, payload: session.on_binary(h, c, payload))
        session = VoiceSession(self.hass, device, connection, msg["id"], binary_id, unsub_binary)
        device.session = session
        connection.subscriptions[msg["id"]] = lambda: self.hass.async_create_task(session.close("unloaded", emit=False), "tacktab_voice_disconnect")
        connection.send_result(msg["id"], {"protocol": 1, "epoch": session.epoch, "binary_handler_id": binary_id, "snapshot_seq": 0, "snapshot": device.snapshot()})

    async def handle(self, connection: ActiveConnection, raw: dict[str, Any]) -> None:
        msg = validate_message(raw)
        if msg["type"] == "tacktab/voice/connect":
            await self.connect(connection, msg)
            return
        session = self._session_for(connection, msg)
        session.validate_epoch(msg)
        command = msg["type"].removeprefix("tacktab/voice/")
        if command == "configure":
            await self._configure(session, msg)
            connection.send_result(msg["id"], self._mutation_result(session.device))
            session.emit({"event": "state", "snapshot": session.device.snapshot()})
        elif command == "status":
            session.device.status = parse_status(msg["status"])
            if not session.device.status.microphone_ready:
                for run in tuple(session.runs.values()):
                    await run.cancel()
                session.runs.clear()
                for action in session.actions.values():
                    if action.kind != "announcement" and not action.future.done():
                        action.future.set_exception(VoiceBridgeError("local_disabled", "Local voice is disabled"))
                if controller := self.hass.data.get("voice_realtime"):
                    await controller.async_close_for_session(session, "local_disabled")
            connection.send_result(msg["id"], self._mutation_result(session.device))
            session.emit({"event": "state", "snapshot": session.device.snapshot()})
        elif command == "snapshot":
            connection.send_result(msg["id"])
            session.emit({"event": "snapshot", "snapshot": session.device.snapshot()})
        elif command == "claim":
            self._claim(session, msg)
            connection.send_result(msg["id"], {"claim_id": msg["claim_id"], "window_ms": 400})
        elif command == "ack":
            self._ack(session, msg)
            connection.send_result(msg["id"], self._mutation_result(session.device))
        elif command == "audio_end":
            run = self._run(session, msg["run_id"])
            run.finish_audio()
            connection.send_result(msg["id"], self._mutation_result(session.device))
        elif command == "cancel":
            run = session.runs.get(msg["run_id"])
            if run is not None:
                satellite = getattr(session.device, "satellites", {}).get(run.slot_id)
                if satellite is not None:
                    await satellite.async_cancel_response(session, run.run_id)
                await run.cancel()
                session.runs.pop(run.run_id, None)
                self.release_media_lease(session, run.run_id)
            connection.send_result(msg["id"], self._mutation_result(session.device))
        elif command == "run":
            resolved_pipeline = await self._run_command(session, msg)
            connection.send_result(msg["id"], {"run_id": msg["run_id"], "slot_id": msg["slot_id"], "pipeline_id": resolved_pipeline})
        elif command == "timer_action":
            adapter = getattr(session.device, "timer_adapter", None)
            if adapter is None:
                raise VoiceBridgeError("timer_not_found", "Timer support is unavailable")
            connection.send_result(msg["id"], adapter.action(msg["request_id"], msg["timer_id"], msg["action"], msg.get("seconds")))
        else:
            raise VoiceBridgeError("invalid_format", "Unsupported voice command")

    async def _configure(self, session: VoiceSession, msg: dict[str, Any]) -> None:
        """Atomically compare, stage, reconcile, and persist one slot topology."""
        async with session.device.config_lock:
            if msg["expected_config_revision"] != session.device.config_revision:
                raise VoiceBridgeError("config_conflict", "Voice configuration changed")
            if session.runs or session.actions or session.device.media_lease is not None or getattr(session.device, "realtime_active", False):
                raise VoiceBridgeError("busy", "Voice device is busy")
            slots = parse_slots(msg["slots"])
            self._validate_pipelines(session.device, slots)
            from .assist_satellite import async_reconcile_slots as reconcile_satellites
            from .select import async_reconcile_slots as reconcile_selects

            previous_slots = session.device.slots
            session.device.slots = slots
            try:
                await reconcile_satellites(session.device, slots)
                await reconcile_selects(session.device, slots)
            except Exception:
                session.device.slots = previous_slots
                raise
            self._sync_satellite_slots(session.device)
            session.device.config_revision += 1
            self._persist_slots(session.device)

    def _session_for(self, connection: ActiveConnection, msg: dict[str, Any]) -> VoiceSession:
        for device in self.devices.values():
            if device.session and not device.session.closed and device.session.connection is connection:
                return device.session
        raise VoiceBridgeError("disconnected", "Voice connection is not paired")

    def _claim(self, session: VoiceSession, msg: dict[str, Any]) -> None:
        slot = session.device.slot(msg["slot_id"])
        phrase = normalized_phrase(msg["wake_word_phrase"])
        if not slot.enabled:
            session.emit_claim(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()), granted=False, reason="slot_disabled")
            return
        if slot.wake_word is None or normalized_phrase(slot.wake_word.phrase) != phrase:
            session.emit_claim(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()), granted=False, reason="invalid_wake_word")
            return
        if not session.device.status.local_enabled:
            session.emit_claim(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()), granted=False, reason="local_disabled")
            return
        if session.device.status.mic_permission != "granted":
            session.emit_claim(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()), granted=False, reason="mic_permission")
            return
        if not session.is_claim_eligible(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic())):
            session.emit_claim(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()), granted=False, reason="busy")
            return
        self.arbiter.submit(WakeClaim(session, msg["claim_id"], slot.slot_id, phrase, msg["snr_db"], time.monotonic()))

    async def _run_command(self, session: VoiceSession, msg: dict[str, Any]) -> str:
        slot = session.device.slot(msg["slot_id"])
        if session.device.config_lock.locked():
            raise VoiceBridgeError("busy", "Voice configuration is changing")
        if not slot.enabled:
            raise VoiceBridgeError("slot_disabled", "Voice slot is disabled")
        if msg["pipeline_id"] != slot.pipeline_id:
            raise VoiceBridgeError("pipeline_mismatch", "Voice pipeline changed")
        resolved_pipeline = self._resolve_pipeline(session.device, slot.pipeline_id)
        if not session.device.status.microphone_ready:
            code = "mic_permission" if session.device.status.mic_permission != "granted" else "capture_unavailable"
            raise VoiceBridgeError(code, "Microphone is unavailable")
        if session.runs or session.actions:
            raise VoiceBridgeError("busy", "Voice device is busy")
        if msg["trigger"] == "wake_word":
            if not self.consume_wake_grant(session, slot.slot_id, msg["grant_id"], msg["wake_word_phrase"]):
                raise VoiceBridgeError("invalid_grant", "Wake grant is invalid")
        if msg["trigger"] == "follow_up" and session.device.follow_up_until.pop(slot.slot_id, 0) < time.monotonic():
            raise VoiceBridgeError("follow_up_expired", "Follow-up is no longer available")
        self.acquire_media_lease(session, msg["run_id"])
        run = VoiceRun(msg["run_id"], slot.slot_id, resolved_pipeline, context_message=msg)
        session.runs[run.run_id] = run
        if session.device.start_run is not None:
            run.task = self.hass.async_create_task(session.device.start_run(run, msg), f"tacktab_voice_run_{run.run_id}")
        else:
            self.release_media_lease(session, run.run_id)
        return resolved_pipeline

    @staticmethod
    def _resolve_pipeline(device: VoiceDevice, pipeline_id: str | None) -> str:
        resolved = pipeline_id or device._preferred_pipeline_id()
        if resolved is None or not any(item["pipeline_id"] == resolved for item in device._pipelines()):
            raise VoiceBridgeError("pipeline_not_found", "Configured pipeline was not found")
        return resolved

    def _validate_pipelines(self, device: VoiceDevice, slots: tuple[SlotConfig, ...]) -> None:
        for slot in slots:
            if slot.pipeline_id is not None:
                self._resolve_pipeline(device, slot.pipeline_id)

    @staticmethod
    def _run(session: VoiceSession, run_id: str) -> VoiceRun:
        try:
            return session.runs[run_id]
        except KeyError as err:
            raise VoiceBridgeError("unknown_run", "Voice run is not active") from err

    @staticmethod
    def _mutation_result(device: VoiceDevice) -> dict[str, int]:
        return {"config_revision": device.config_revision, "timer_revision": device.timer_revision}

    @staticmethod
    def _persist_slots(device: VoiceDevice) -> None:
        """Persist only voice configuration; it never touches the Pro HTTP entry."""
        entry = getattr(device, "entry", None)
        if entry is None:
            return
        from .voice_setup import VOICE_ENTRY_SLOTS

        device.hass.config_entries.async_update_entry(
            entry, data={**entry.data, VOICE_ENTRY_SLOTS: [slot.as_dict() for slot in device.slots]}
        )

    @staticmethod
    def _sync_satellite_slots(device: VoiceDevice) -> None:
        for satellite in getattr(device, "satellites", {}).values():
            satellite.slot = device.slot(satellite.slot.slot_id)

    @staticmethod
    def _ack(session: VoiceSession, msg: dict[str, Any]) -> None:
        action = session.actions.get(msg["action_id"])
        if action is None:
            if session.completed_actions.get(msg["action_id"]) == msg["status"]:
                return
            raise VoiceBridgeError("unknown_action", "Playback action is not active")
        if action.future.done():
            return
        if msg["status"] == "finished":
            action.future.set_result(None)
        else:
            action.future.set_exception(VoiceBridgeError(msg.get("error", "disconnected"), "Playback did not finish"))

    def session_for_connection(self, connection: ActiveConnection) -> VoiceSession:
        """Resolve the only paired session for an authenticated HA connection."""
        return self._session_for(connection, {})

    def acquire_media_lease(self, session: VoiceSession, owner: str) -> None:
        """Acquire the per-tablet mic/output lease shared with realtime providers."""
        if session.device.config_lock.locked():
            raise VoiceBridgeError("busy", "Voice configuration is changing")
        lease = session.device.media_lease
        token = (session.epoch, owner)
        if lease is not None and lease != token:
            raise VoiceBridgeError("busy", "Voice device is busy")
        session.device.media_lease = token

    def release_media_lease(self, session: VoiceSession, owner: str) -> None:
        """Release a lease only when its epoch and owner still match."""
        if session.device.media_lease == (session.epoch, owner):
            session.device.media_lease = None

    def consume_wake_grant(self, session: VoiceSession, slot_id: str, grant_id: str, phrase: str) -> bool:
        """Validate and single-use a central wake grant for any bridge consumer."""
        grant = session.device.grants.pop(grant_id, None)
        return grant is not None and grant[0] == session.epoch and grant[1] == slot_id and grant[2] == normalized_phrase(phrase) and grant[3] >= time.monotonic()


@callback
def async_get_bridge(hass: HomeAssistant) -> VoiceBridge:
    """Return the singleton bridge without touching Tacktab's Pro HTTP path."""
    return hass.data.setdefault(VOICE_DATA, VoiceBridge(hass))
