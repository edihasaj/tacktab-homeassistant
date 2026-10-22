"""Mutable state for one realtime provider session."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any

from .voice_realtime_providers import RealtimeProvider
from .voice_realtime_protocol import RealtimeProtocolError
from .voice_realtime_tools import BoundTools

MAX_INPUT_BYTES = 2 * 16_000 * 2
MAX_UTTERANCE_BYTES = 60 * 16_000 * 2
MAX_SESSION_SECONDS = 9 * 60


@dataclass(slots=True)
class RuntimeSession:
    session_id: str
    bridge_session: Any
    slot_id: str
    setting: Any
    provider: RealtimeProvider
    owner: str
    next_chunk: int = 0
    queued_input: int = 0
    utterance_bytes: int = 0
    sent_frames: dict[str, int] = field(default_factory=dict)
    output_chunks: dict[str, int] = field(default_factory=dict)
    played_frames: dict[str, int] = field(default_factory=dict)
    playback_items: dict[str, str] = field(default_factory=dict)
    output_ended: set[str] = field(default_factory=set)
    retired_playbacks: set[str] = field(default_factory=set)
    discard_audio: bool = False
    playback_id: str | None = None
    task: asyncio.Task[None] | None = None
    setup_task: asyncio.Task[Any] | None = None
    started: float = field(default_factory=time.monotonic)
    closed: bool = False
    called: dict[tuple[int, str], bool] = field(default_factory=dict)
    tools: BoundTools | None = None
    tool_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    setup_failed: bool = False
    pending_continue: bool = False
    response_done: bool = False
    idle_task: asyncio.Task[None] | None = None
    max_task: asyncio.Task[None] | None = None
    memory_key: tuple[str, ...] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    history_bytes: int = 0
    resume: dict[str, Any] | None = None
    lease_released: bool = False
    generation: int = 0
    accept_tool_calls: bool = True
    generation_had_audio: bool = False


async def async_send_audio(controller: Any, connection: Any, msg: dict[str, Any]) -> None:
    run = controller._runtime(connection, msg["session_id"])
    if msg["chunk_seq"] != run.next_chunk:
        raise RealtimeProtocolError()
    if (
        run.queued_input + len(msg["pcm"]) > MAX_INPUT_BYTES
        or run.utterance_bytes + len(msg["pcm"]) > MAX_UTTERANCE_BYTES
        or time.monotonic() - run.started > MAX_SESSION_SECONDS
    ):
        await controller.close(run, "audio_overflow", False)
        raise RealtimeProtocolError("audio_overflow", "Realtime audio limit reached")
    run.next_chunk += 1; run.queued_input += len(msg["pcm"]); run.utterance_bytes += len(msg["pcm"])
    try:
        await run.provider.send_audio(msg["pcm"])
    except Exception:
        await controller.close(run, "provider_error", False)
        raise RealtimeProtocolError("provider_error", "Realtime provider disconnected") from None
    finally:
        run.queued_input -= len(msg["pcm"])
    connection.send_result(msg["id"])


async def async_end_audio(controller: Any, connection: Any, msg: dict[str, Any]) -> None:
    run = controller._runtime(connection, msg["session_id"])
    try:
        await run.provider.end_audio()
    except Exception:
        await controller.close(run, "provider_error", False)
        raise RealtimeProtocolError("provider_error", "Realtime provider disconnected") from None
    run.utterance_bytes = 0
    connection.send_result(msg["id"])


async def async_interrupt(controller: Any, connection: Any, msg: dict[str, Any]) -> None:
    run = controller._runtime(connection, msg["session_id"])
    playback = msg["playback_id"]
    if playback not in run.sent_frames or msg["played_frames"] < run.played_frames.get(playback, 0) or msg["played_frames"] > run.sent_frames.get(playback, 0):
        raise RealtimeProtocolError()
    try:
        await controller._interrupt_playback(run, playback, msg["played_frames"])
    except Exception:
        await controller.close(run, "provider_error", False)
        raise RealtimeProtocolError("provider_error", "Realtime provider disconnected") from None
    connection.send_result(msg["id"])


async def async_playback(controller: Any, connection: Any, msg: dict[str, Any]) -> None:
    run = controller._runtime(connection, msg["session_id"])
    playback = msg["playback_id"]
    sent, previous = run.sent_frames.get(playback, 0), run.played_frames.get(playback, 0)
    if playback not in run.sent_frames or msg["played_frames"] < previous or msg["played_frames"] > sent:
        raise RealtimeProtocolError()
    if msg["status"] == "finished" and (playback not in run.output_ended or msg["played_frames"] != sent):
        raise RealtimeProtocolError()
    if msg["status"] == "finished":
        run.played_frames[playback] = msg["played_frames"]
        run.retired_playbacks.add(playback)
        await controller._maybe_continue(run)
        controller._schedule_idle_close(run)
    elif msg["status"] in {"interrupted", "failed"}:
        try:
            await controller._interrupt_playback(run, playback, msg["played_frames"])
        except Exception:
            await controller.close(run, "provider_error", False)
            raise RealtimeProtocolError("provider_error", "Realtime provider disconnected") from None
    else:
        run.played_frames[playback] = msg["played_frames"]
    connection.send_result(msg["id"])
