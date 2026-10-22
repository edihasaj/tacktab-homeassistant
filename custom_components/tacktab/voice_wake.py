"""Central, bounded SNR arbitration for Tacktab-native wake detections."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .voice_bridge import VoiceSession


@dataclass(slots=True)
class WakeClaim:
    """One admissible candidate in a fixed server-receipt window."""

    session: VoiceSession
    claim_id: str
    slot_id: str
    phrase: str
    snr_db: float
    received_at: float


class WakeArbiter:
    """Select one candidate after the frozen 400 ms SNR window."""

    def __init__(self) -> None:
        self._windows: dict[str, list[WakeClaim]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cooldown_until: dict[str, float] = {}

    def submit(self, claim: WakeClaim) -> None:
        phrase = claim.phrase
        now = time.monotonic()
        if now < self._cooldown_until.get(phrase, 0):
            claim.session.emit_claim(claim, granted=False, reason="busy")
            return
        window = self._windows.setdefault(phrase, [])
        if any(candidate.session.install_id == claim.session.install_id and candidate.slot_id == claim.slot_id for candidate in window):
            claim.session.emit_claim(claim, granted=False, reason="busy")
            return
        window.append(claim)
        if phrase not in self._tasks:
            self._tasks[phrase] = claim.session.hass.async_create_task(self._finish(phrase), f"tacktab_voice_claim_{phrase}")

    async def _finish(self, phrase: str) -> None:
        await asyncio.sleep(0.4)
        claims = self._windows.pop(phrase, [])
        self._tasks.pop(phrase, None)
        if not claims:
            return
        eligible = [claim for claim in claims if claim.session.is_claim_eligible(claim)]
        if not eligible:
            for claim in claims:
                claim.session.emit_claim(claim, granted=False, reason="busy")
            return
        winner = min(eligible, key=lambda candidate: (-candidate.snr_db, candidate.received_at, candidate.session.install_id, candidate.slot_id))
        self._cooldown_until[phrase] = time.monotonic() + 2
        for claim in claims:
            if claim not in eligible:
                claim.session.emit_claim(claim, granted=False, reason="busy")
            else:
                claim.session.emit_claim(claim, granted=claim is winner, reason="winner" if claim is winner else "lower_snr")
