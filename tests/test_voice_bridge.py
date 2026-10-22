"""Bounded transport behavior that is independent of an installed Assist pipeline."""

import asyncio

import pytest

from custom_components.tacktab.voice_bridge import MAX_AUDIO_QUEUE_BYTES, VoiceBridgeError, VoiceRun
from custom_components.tacktab.voice_wake import WakeArbiter, WakeClaim


class _Hass:
    def async_create_task(self, coroutine, _name: str):
        return asyncio.create_task(coroutine)


class _Session:
    def __init__(self, install_id: str) -> None:
        self.hass = _Hass()
        self.install_id = install_id
        self.events: list[tuple[str, bool, str]] = []

    def emit_claim(self, claim: WakeClaim, *, granted: bool, reason: str) -> None:
        self.events.append((claim.claim_id, granted, reason))

    def is_claim_eligible(self, _claim: WakeClaim) -> bool:
        return True


def test_pcm_queue_refuses_oversize_and_two_second_backlog() -> None:
    run = VoiceRun("run", "primary", "pipeline")
    with pytest.raises(VoiceBridgeError, match="Invalid PCM"):
        run.accept_audio(b"x" * (32 * 1024 + 2))

    run = VoiceRun("run", "primary", "pipeline")
    run.accept_audio(b"x" * (MAX_AUDIO_QUEUE_BYTES // 2))
    run.accept_audio(b"x" * (MAX_AUDIO_QUEUE_BYTES // 2))
    with pytest.raises(VoiceBridgeError, match="queue is full"):
        run.accept_audio(b"xx")


async def test_wake_arbiter_emits_one_grant_and_lower_snr_denial() -> None:
    low = _Session("aaa")
    high = _Session("bbb")
    arbiter = WakeArbiter()
    arbiter.submit(WakeClaim(low, "low", "primary", "okay tacktab", 8, 10.0))
    arbiter.submit(WakeClaim(high, "high", "primary", "okay tacktab", 12, 11.0))

    await asyncio.sleep(0.45)

    assert low.events == [("low", False, "lower_snr")]
    assert high.events == [("high", True, "winner")]


async def test_pcm_queue_drains_while_a_longer_utterance_continues() -> None:
    """Queue capacity protects memory without imposing a two-second utterance cap."""
    run = VoiceRun("run", "primary", "pipeline")

    async def consume() -> list[bytes]:
        return [chunk async for chunk in run.stream()]

    consumer = asyncio.create_task(consume())
    for _ in range(3):
        run.accept_audio(b"x" * (MAX_AUDIO_QUEUE_BYTES // 2))
        await asyncio.sleep(0)
    run.finish_audio()

    assert len(await consumer) == 3
    assert run.utterance_audio_bytes > MAX_AUDIO_QUEUE_BYTES
    assert run.queued_audio_bytes == 0
