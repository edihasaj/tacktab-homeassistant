"""Narrow Home Assistant 2026.10 timer compatibility adapter for Tacktab voice."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .voice_bridge import VoiceBridgeError, VoiceDevice


class VoiceTimerAdapter:
    """Mirrors device timers and isolates the non-public TimerManager boundary."""

    def __init__(self, hass: HomeAssistant, device: VoiceDevice, device_id: str) -> None:
        self.hass = hass
        self.device = device
        self.device_id = device_id
        self.ringing: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._revisions: dict[str, int] = {}
        self._request_results: OrderedDict[str, dict[str, int]] = OrderedDict()
        self._unsub = None

    def async_start(self) -> None:
        """Register exactly once and snapshot synchronously to avoid a race."""
        from homeassistant.components.intent import async_register_timer_handler

        self._unsub = async_register_timer_handler(self.hass, self.device_id, self._on_timer_event)
        self._snapshot_live()  # no await between registration and mirror snapshot

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
        if session := self.device.session:
            session.emit({"event": "timer", "change": change, "timer": timer_data, "timer_revision": self.device.timer_revision})

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
            if session := self.device.session:
                session.emit({"event": "timer", "change": "dismissed", "timer": timer, "timer_revision": self.device.timer_revision})
        else:
            manager = self._manager()
            timer = manager.timers.get(timer_id)
            if timer is None or timer.device_id != self.device_id or timer.conversation_command:
                raise VoiceBridgeError("timer_not_found", "Timer was not found")
            try:
                if action == "pause":
                    manager.pause_timer(timer_id)
                elif action == "resume":
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
        result = {"config_revision": self.device.config_revision, "timer_revision": self.device.timer_revision}
        self._request_results[request_id] = result
        while len(self._request_results) > 128:
            self._request_results.popitem(last=False)
        return result
