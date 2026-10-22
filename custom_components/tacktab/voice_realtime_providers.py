"""Provider-neutral realtime voice primitives.

This module intentionally has no bridge imports: adapters can be fixture-tested
without opening a paid provider session, and provider credentials stay on HA.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
import struct
from typing import Any, Protocol


class RealtimeProviderError(Exception):
    """A safe provider failure.  Its text must be suitable for a client."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: str
    model: str
    voice: str
    api_key: str
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    original_name: str | None = None


@dataclass(slots=True)
class ProviderEvent:
    kind: str
    audio: bytes | None = None
    item_id: str | None = None
    text: str | None = None
    role: str | None = None
    operation: str = "append"
    final: bool = False
    call_id: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    recoverable: bool = False
    detail: str | None = None
    cancelled_ids: tuple[str, ...] = ()
    handle: str | None = None


class StatefulResampler:
    """Dependency-free PCM16 16→24 kHz resampler retaining chunk phase.

    Output sample ``j`` is interpolated at input time ``2j/3``. Samples that
    need the next input endpoint wait for a later chunk, so WebSocket framing
    cannot change the waveform. At ``flush`` the one unresolved endpoint is
    held at the final input value; this produces ``floor(1.5 * input_frames)``
    output frames and makes a one-frame turn audible.
    """

    def __init__(self, from_rate: int = 16_000, to_rate: int = 24_000) -> None:
        self.from_rate = from_rate
        self.to_rate = to_rate
        if (from_rate, to_rate) != (16_000, 24_000):
            raise ValueError("Only Tacktab's 16 kHz to 24 kHz conversion is supported")
        self._input_frames = 0
        self._next_output = 0
        self._previous: int | None = None
        self._last: int | None = None

    def convert(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise RealtimeProviderError("Invalid PCM frame")
        output: list[int] = []
        for sample in struct.unpack(f"<{len(pcm) // 2}h", pcm):
            index = self._input_frames
            self._last = sample
            while (2 * self._next_output + 2) // 3 <= index:
                numerator = 2 * self._next_output
                left, remainder = divmod(numerator, 3)
                if remainder == 0:
                    value = sample
                else:
                    # The only unresolved interpolation has left=index-1 and
                    # right=index; previous therefore spans arbitrary chunks.
                    assert self._previous is not None and left == index - 1
                    value = (self._previous * (3 - remainder) + sample * remainder) // 3
                output.append(value)
                self._next_output += 1
            self._previous = sample
            self._input_frames += 1
        return struct.pack(f"<{len(output)}h", *output) if output else b""

    def flush(self) -> bytes:
        """Finish a turn with held-tail interpolation; subsequent calls are empty."""
        if self._last is None:
            return b""
        target = (self._input_frames * 3) // 2
        output = [self._last] * (target - self._next_output)
        self._input_frames = 0
        self._next_output = 0
        self._previous = None
        self._last = None
        return struct.pack(f"<{len(output)}h", *output) if output else b""


class RealtimeProvider(Protocol):
    async def connect(self, config: ProviderConfig, tools: list[ToolSpec], instructions: str, resume: dict[str, Any] | None = None) -> None: ...
    async def send_audio(self, pcm16le_16000: bytes) -> None: ...
    async def end_audio(self) -> None: ...
    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any], is_error: bool) -> None: ...
    async def continue_after_tools(self) -> None: ...
    async def interrupt(self, item_id: str | None, played_frames_24000: int) -> None: ...
    async def events(self) -> AsyncIterator[ProviderEvent]: ...
    async def close(self) -> dict[str, Any] | None: ...


def json_message(raw: str | bytes) -> dict[str, Any] | None:
    """Decode a provider message without ever reflecting raw provider data."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def safe_provider_error(_exc: BaseException) -> RealtimeProviderError:
    """Provider HTTP/websocket errors often include a URL or request headers."""
    return RealtimeProviderError("Realtime provider connection failed")


def tool_payload(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [{"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.parameters} for tool in tools]
