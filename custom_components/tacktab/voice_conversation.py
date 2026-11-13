"""Per-tablet "Conversation memory": how long Assist keeps context between activations.

Home Assistant's Assist satellite base class keeps one conversation id per
satellite and its chat session expires after five minutes of inactivity.  That
built-in behaviour is the default here (`5_minutes`).  This module lets a user
shorten it (`isolated` starts a new conversation every activation, `1_minute`)
or lengthen it (`15_minutes`, `until_restart`) by refreshing the chat session
through Home Assistant's own helper while the chosen retention has not elapsed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import chat_session
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

if TYPE_CHECKING:
    from .voice_bridge import VoiceDevice

DEFAULT_MEMORY = "5_minutes"
# Option -> seconds of inactivity after which the next activation starts fresh; None never expires.
MEMORY_SECONDS: dict[str, int | None] = {
    "isolated": 0, "1_minute": 60, "5_minutes": 300, "15_minutes": 900, "until_restart": None,
}
CORE_RETENTION = int(chat_session.CONVERSATION_TIMEOUT.total_seconds())
_KEEPALIVE_SECONDS = CORE_RETENTION - 60


def memory_option_for(conversation: str | None, seconds: int | None) -> tuple[str | None, bool]:
    """Map a Voice Satellite session duration to (option, rounded_up).

    `persistent` -> until_restart, `isolated` -> isolated, a timed value -> the
    smallest option that keeps context at least that long.  Unknown -> (None, False).
    """
    if conversation == "isolated":
        return "isolated", False
    if conversation == "persistent":
        return "until_restart", False
    if conversation == "timed" and seconds:
        for option, limit in MEMORY_SECONDS.items():
            if limit and limit >= seconds:
                return option, limit != seconds
        return "until_restart", True
    return None, False


def retention_seconds(device: VoiceDevice) -> int | None:
    """Seconds this tablet keeps context; `None` means until restart."""
    return MEMORY_SECONDS.get(getattr(device, "conversation_memory", DEFAULT_MEMORY), CORE_RETENTION)


class ConversationMemory:
    """Applies the retention to one satellite's HA conversation id."""

    def __init__(self, hass: HomeAssistant, retention: Callable[[], int | None]) -> None:
        self._hass = hass
        self._retention = retention
        self._touched: float | None = None
        self._keepalive: CALLBACK_TYPE | None = None

    def _now(self) -> float:
        return self._hass.loop.time()

    def expired(self) -> bool:
        retention = self._retention()
        if retention == 0:
            return True
        return retention is not None and self._touched is not None and self._now() - self._touched > retention

    def before_run(self, satellite: Any, *, follow_up: bool = False) -> None:
        """Start a new conversation when the retention has run out (or is zero).

        A server-authorized follow-up continues the activation that asked for it, so
        it keeps its context even when the retention is `isolated`.
        """
        self.cancel()
        if not follow_up and self.expired():
            satellite._conversation_id = None

    def after_run(self, satellite: Any) -> None:
        self._touched = self._now()
        self._schedule(satellite)

    def apply_setting(self, satellite: Any) -> None:
        """A changed option takes effect now, without waiting for the next run."""
        self.cancel()
        if self.expired():
            satellite._conversation_id = None
        else:
            self._schedule(satellite)

    def cancel(self) -> None:
        if self._keepalive is not None:
            self._keepalive()
            self._keepalive = None

    def _schedule(self, satellite: Any) -> None:
        retention = self._retention()
        # Up to Home Assistant's own five minutes it keeps the session itself.
        if satellite._conversation_id is None or (retention is not None and retention <= CORE_RETENTION):
            return

        @callback
        def fire(_now: Any) -> None:
            self._refresh(satellite)

        self._keepalive = async_call_later(self._hass, _KEEPALIVE_SECONDS, fire)

    @callback
    def _refresh(self, satellite: Any) -> None:
        self._keepalive = None
        conversation_id = satellite._conversation_id
        if conversation_id is None or self.expired():
            if conversation_id is not None and self.expired():
                satellite._conversation_id = None
            return
        if conversation_id in self._hass.data.get(chat_session.DATA_CHAT_SESSION, {}):
            with chat_session.async_get_chat_session(self._hass, conversation_id):
                pass  # leaving the helper stamps the session as just used
            self._schedule(satellite)
        else:
            satellite._conversation_id = None  # Home Assistant already dropped it


class TacktabConversationMemorySelect(SelectEntity, RestoreEntity):
    """How long this tablet's Assist conversations keep their context."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "conversation_memory"
    _attr_options = list(MEMORY_SECONDS)

    def __init__(self, device: VoiceDevice) -> None:
        self.device = device
        self._attr_unique_id = f"voice:{device.install_id}:conversation_memory"
        device.conversation_memory = DEFAULT_MEMORY  # type: ignore[attr-defined]

    @property
    def device_info(self) -> dict:
        return {"identifiers": {("tacktab", f"voice:{self.device.install_id}")}}

    @property
    def current_option(self) -> str:
        return self.device.conversation_memory  # type: ignore[attr-defined]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is not None and state.state in MEMORY_SECONDS:
            self.device.conversation_memory = state.state  # type: ignore[attr-defined]

    async def async_select_option(self, option: str) -> None:
        if option not in MEMORY_SECONDS:
            raise ValueError(f"Unknown conversation memory {option!r}")
        self.device.conversation_memory = option  # type: ignore[attr-defined]
        self.async_write_ha_state()
        for satellite in getattr(self.device, "satellites", {}).values():
            satellite.conversation_memory.apply_setting(satellite)


__all__ = ["ConversationMemory", "TacktabConversationMemorySelect", "memory_option_for", "retention_seconds", "MEMORY_SECONDS", "DEFAULT_MEMORY"]
