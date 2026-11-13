"""Narrow Home Assistant 2026.10 timer compatibility adapter for Tacktab voice."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .voice_bridge import VoiceBridgeError, VoiceDevice

TIMER_EVENT = "tacktab_voice_timer"


class VoiceTimerAdapter:
    """Mirrors device timers and isolates the non-public TimerManager boundary."""

    def __init__(self, hass: HomeAssistant, device: VoiceDevice, device_id: str) -> None:
        self.hass = hass
        self.device = device
        self.device_id = device_id
        self.ringing: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._revisions: dict[str, int] = {}
        self._active: dict[str, bool] = {}
        self._fired: dict[str, tuple[Any, ...]] = {}
        self._totals: dict[str, int] = {}
        self._override: str | None = None
        self._request_results: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._unsub = None

    def async_start(self) -> None:
        """Register exactly once and snapshot synchronously to avoid a race."""
        from homeassistant.components.intent import async_register_timer_handler

        self._unsub = async_register_timer_handler(self.hass, self.device_id, self._on_timer_event)
        self._seed_live_state()
        self._snapshot_live()  # no await between registration and mirror snapshot

    def _seed_live_state(self) -> None:
        """Adopt timers that already exist, for example after an integration reload.

        Without this, a real Core pause would be labelled ``updated`` and a
        timer's original duration would be taken from the already-extended
        ``created_seconds``. Core only exposes the current value, so a reload
        after an add-time can no longer recover the original start duration.
        """
        for timer in self._manager().timers.values():
            if timer.device_id != self.device_id or timer.conversation_command:
                continue
            timer_id = str(timer.id)
            self._active.setdefault(timer_id, bool(timer.is_active))
            self._totals.setdefault(timer_id, _started_seconds(timer))

    def close(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def snapshot(self) -> tuple[list[dict[str, Any]], list[str]]:
        timers = self._snapshot_live() + list(self.ringing.values())
        return (timers[-64:], list(self.ringing)[-64:])

    def _manager(self):
        from homeassistant.components.intent.const import TIMER_DATA

        manager = self.hass.data.get(TIMER_DATA)
        if manager is None:
            raise VoiceBridgeError("internal_error", "Timer manager is unavailable")
        return manager

    def _snapshot_live(self) -> list[dict[str, Any]]:
        timers = []
        for timer in self._manager().timers.values():
            if timer.device_id != self.device_id or timer.conversation_command:
                continue
            timers.append(self._serialize(timer, self._revisions.get(str(timer.id), 1)))
        return timers

    @staticmethod
    def _serialize(timer: Any, revision: int = 1) -> dict[str, Any]:
        """Copy mutable TimerInfo values before emitting them to the native app."""
        return {
            "timer_id": str(timer.id), "name": timer.name, "total_seconds": int(timer.created_seconds),
            "remaining_seconds": max(0, int(timer.seconds_left)), "is_active": bool(timer.is_active), "revision": revision,
        }

    @callback
    def _on_timer_event(self, event_type: Any, timer: Any) -> None:
        change = str(event_type).split(".")[-1].lower()
        override, self._override = self._override, None
        timer_id = str(timer.id)
        previous_active = self._active.get(timer_id)
        self._active[timer_id] = bool(timer.is_active)
        self.device.timer_revision += 1
        timer_data = self._serialize(timer, self.device.timer_revision)
        self._revisions[timer_data["timer_id"]] = timer_data["revision"]
        if change == "finished":
            timer_data["remaining_seconds"] = 0
            timer_data["is_active"] = False
            self.ringing[timer_data["timer_id"]] = timer_data
            self.ringing.move_to_end(timer_data["timer_id"])
            while len(self.ringing) > 64:
                self.ringing.popitem(last=False)
        self._fire(
            self._event_type(change, override, previous_active, timer_data),
            timer_data,
            raw_change=change,
        )
        if session := self.device.session:
            session.emit({"event": "timer", "change": change, "timer": timer_data, "timer_revision": self.device.timer_revision})

    @staticmethod
    def _event_type(change: str, override: str | None, previous_active: bool | None, timer_data: dict[str, Any]) -> str:
        """Name the transition, including pause and resume from Core's ``updated``."""
        if override is not None:
            return override
        if change == "updated" and previous_active is not None:
            if previous_active and not timer_data["is_active"]:
                return "paused"
            if not previous_active and timer_data["is_active"]:
                return "resumed"
        return change

    def _fire(self, event_type: str, timer_data: dict[str, Any], *, raw_change: str | None = None) -> None:
        """Publish one deduplicated automation event for this exact timer state.

        The adapter is the only authority: a repeated handler call, a snapshot
        read or an unload/reload must not replay a terminal ``finished`` event.
        Dedupe keys on Core's raw change plus the underlying state, never on the
        derived label: replaying a Core ``updated`` callback after a pause would
        otherwise be relabelled ``updated`` and slip past the check.
        """
        timer_id = timer_data["timer_id"]
        # Home Assistant's ``created_seconds`` grows when time is added. The
        # documented event keeps the duration the timer was started with, even
        # though the native mirror continues to report the extended value.
        total_seconds = self._totals.setdefault(timer_id, timer_data["total_seconds"])
        timer_data = {**timer_data, "total_seconds": total_seconds}
        signature = (
            raw_change or event_type,
            total_seconds,
            timer_data["remaining_seconds"],
            timer_data["is_active"],
        )
        if self._fired.get(timer_id) == signature:
            return
        self._fired[timer_id] = signature
        while len(self._fired) > 256:
            self._fired.pop(next(iter(self._fired)))
        while len(self._totals) > 256:
            self._totals.pop(next(iter(self._totals)))
        device_name, _area = _device_identity(self.hass, self.device_id, self.device.name)
        self.hass.bus.async_fire(
            TIMER_EVENT,
            {
                "event_type": event_type,
                "timer_id": timer_id,
                "name": timer_data["name"] or "",
                "total_seconds": timer_data["total_seconds"],
                "seconds_left": timer_data["remaining_seconds"],
                "is_active": timer_data["is_active"],
                "device_id": self.device_id,
                "device_name": device_name,
            },
        )

    def action(self, request_id: str, timer_id: str, action: str, seconds: int | None) -> dict[str, int]:
        """Perform one exact-ID mutation; retries reuse the cached result."""
        if request_id in self._request_results:
            return self._request_results[request_id]
        if action == "dismiss":
            timer = self.ringing.pop(timer_id, None)
            if timer is None:
                raise VoiceBridgeError("timer_not_found", "Timer was not found")
            self.device.timer_revision += 1
            timer["revision"] = self.device.timer_revision
            self._active.pop(timer_id, None)
            self._fire("dismissed", timer)
            if session := self.device.session:
                session.emit({"event": "timer", "change": "dismissed", "timer": timer, "timer_revision": self.device.timer_revision})
        else:
            manager = self._manager()
            timer = manager.timers.get(timer_id)
            if timer is None or timer.device_id != self.device_id or timer.conversation_command:
                raise VoiceBridgeError("timer_not_found", "Timer was not found")
            try:
                if action == "pause":
                    self._override = "paused"
                    manager.pause_timer(timer_id)
                elif action == "resume":
                    self._override = "resumed"
                    manager.unpause_timer(timer_id)
                elif action == "cancel":
                    manager.cancel_timer(timer_id)
                elif action == "add":
                    manager.add_time(timer_id, seconds)
                elif action == "subtract":
                    manager.remove_time(timer_id, seconds)
                else:
                    raise VoiceBridgeError("invalid_timer_action", "Invalid timer action")
            except VoiceBridgeError:
                raise
            except Exception as err:
                raise VoiceBridgeError("timer_not_found", "Timer was not found") from err
            finally:
                # A manager no-op (already paused) fires no handler event.
                self._override = None
            if action == "cancel":
                self._active.pop(timer_id, None)
        result = {"config_revision": self.device.config_revision, "timer_revision": self.device.timer_revision}
        self._request_results[request_id] = result
        while len(self._request_results) > 128:
            self._request_results.popitem(last=False)
        return result


def _device_identity(hass: HomeAssistant, device_id: str, fallback: str) -> tuple[str, str | None]:
    """Return the registry device name and its area name without raising."""
    try:
        from .voice_context import async_device_identity

        return async_device_identity(hass, device_id, fallback)
    except Exception:
        return fallback, None


def _started_seconds(timer: Any) -> int:
    """Return the duration the timer was started with.

    ``created_seconds`` grows when time is added, but Core keeps the original
    ``start_hours``/``start_minutes``/``start_seconds`` unchanged. Fall back to
    ``created_seconds`` only when no start component is present.
    """
    parts = (timer.start_hours, timer.start_minutes, timer.start_seconds)
    if all(part is None for part in parts):
        return int(timer.created_seconds)
    hours, minutes, seconds = (part or 0 for part in parts)
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
