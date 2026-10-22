"""Typed, dependency-free data for the Tacktab native voice bridge.

This module deliberately does not import Assist.  Older Core versions can load
the established Tacktab HTTP integration while the voice bridge is gated off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

SlotId = Literal["primary", "secondary"]
Output = Literal["local", "ha_media_player"]


@dataclass(frozen=True, slots=True)
class WakeWord:
    """A locally detected wake-word model and its configured phrase."""

    id: str
    phrase: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "phrase": self.phrase}


@dataclass(frozen=True, slots=True)
class SlotConfig:
    """Persisted configuration for one logical satellite on a tablet."""

    slot_id: SlotId
    enabled: bool
    pipeline_id: str | None
    wake_word: WakeWord | None
    output: Output
    media_player_entity_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "enabled": self.enabled,
            "pipeline_id": self.pipeline_id,
            "wake_word": None if self.wake_word is None else self.wake_word.as_dict(),
            "output": self.output,
            "media_player_entity_id": self.media_player_entity_id,
        }


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    """Native gates reported by the tablet; none grant a missing permission."""

    local_enabled: bool
    mic_permission: Literal["granted", "denied", "undetermined", "restricted"]
    app_state: Literal["foreground", "background"]
    capture_ready: bool
    playback_ready: bool

    @property
    def microphone_ready(self) -> bool:
        return self.local_enabled and self.mic_permission == "granted" and self.capture_ready

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_enabled": self.local_enabled,
            "mic_permission": self.mic_permission,
            "app_state": self.app_state,
            "capture_ready": self.capture_ready,
            "playback_ready": self.playback_ready,
        }


DEFAULT_VOICE_STATUS = VoiceStatus(False, "undetermined", "foreground", False, False)


@dataclass(frozen=True, slots=True)
class VoiceCapabilities:
    """Capabilities declared once by a native client during connect."""

    announcements: bool
    start_conversation: bool
    timers: bool
    local_wake_word: bool
    pcm16_16khz_mono: Literal[True]

    def as_dict(self) -> dict[str, bool]:
        return {
            "announcements": self.announcements,
            "start_conversation": self.start_conversation,
            "timers": self.timers,
            "local_wake_word": self.local_wake_word,
            "pcm16_16khz_mono": self.pcm16_16khz_mono,
        }


def normalized_phrase(phrase: str) -> str:
    """Return the protocol's canonical wake phrase form."""
    return " ".join(phrase.lower().split())
