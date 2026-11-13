"""Home Assistant storage, follow settings and fan-out for the fleet voice profile.

One manager per Home Assistant instance owns the profile document. Every write
goes through :meth:`VoiceProfileManager.async_update`, which checks the base
revision, persists, and only then tells the following tablets.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .voice_profile import (
    EXCLUDED_KEYS,
    GROUPS,
    ProfileError,
    apply_update,
    empty_document,
    normalize_document,
    sanitize_groups,
    sanitize_removals,
    view,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .voice_bridge import VoiceDevice

PROFILE_DATA = "tacktab_voice_profile"
PROFILE_LOCK = "tacktab_voice_profile_lock"
STORAGE_KEY = "tacktab.voice_profile"
STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 1
OPTION_FOLLOW = "fleet_profile_follow"
OPTION_GROUPS = "fleet_profile_groups"
RECENT_REQUESTS = 32


class StorageTooNewError(Exception):
    """Storage was written by a newer release. Not NotImplementedError: Home Assistant
    swallows that for a same-major version and would save the document back."""


class _ProfileStore(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]) -> dict[str, Any]:
        """Bring an older or sparse document forward; refuse to guess about newer ones.

        A newer major *or minor* version may hold groups and metadata this release
        cannot represent, and saving would drop them, so it is never migrated.
        """
        if (old_major_version, old_minor_version) > (STORAGE_VERSION, STORAGE_MINOR_VERSION):
            raise StorageTooNewError("Voice profile storage was written by a newer Tacktab")
        return normalize_document(old_data)


class VoiceProfileManager:
    """The fleet profile document plus per-tablet follow state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = _ProfileStore(hass, STORAGE_VERSION, STORAGE_KEY, minor_version=STORAGE_MINOR_VERSION)
        self.doc: dict[str, Any] = empty_document()
        # Set when storage is unreadable or newer than this release; writes are
        # refused so a downgrade can never overwrite a newer document.
        self.read_only = False
        self._lock = asyncio.Lock()
        self._recent: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._listeners: set[Callable[[], None]] = set()

    async def async_load(self) -> None:
        try:
            stored = await self._store.async_load()
        except Exception:
            self.read_only = True
            return
        self.doc = normalize_document(stored)

    # -- listeners -----------------------------------------------------------
    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    # -- follow state --------------------------------------------------------
    @staticmethod
    def follow_state(device: "VoiceDevice") -> tuple[bool, tuple[str, ...]]:
        """Return ``(opted_in, followed_groups)`` read from the entry options."""
        entry = getattr(device, "entry", None)
        options = entry.options if entry is not None else {}
        groups = options.get(OPTION_GROUPS)
        selected = tuple(group for group in GROUPS if group in groups) if isinstance(groups, (list, tuple)) else GROUPS
        return options.get(OPTION_FOLLOW) is True, selected

    def following_view(self, device: "VoiceDevice") -> dict[str, Any]:
        enabled, groups = self.follow_state(device)
        return {"enabled": enabled, "groups": list(groups)}

    async def async_set_follow(self, entry: "ConfigEntry", enabled: bool, groups: tuple[str, ...] | list[str]) -> None:
        """Persist a tablet's opt-in, then catch a negotiated tablet up."""
        wanted = [group for group in GROUPS if group in groups]
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPTION_FOLLOW: bool(enabled), OPTION_GROUPS: wanted}
        )
        from .voice_bridge import async_get_bridge
        from .voice_setup import VOICE_ENTRY_KEY

        device = async_get_bridge(self.hass).devices.get(entry.data.get(VOICE_ENTRY_KEY, ""))
        if device is not None:
            self._emit_changed(device, None, reason="follow_changed")
        self._notify_listeners()

    # -- reads ---------------------------------------------------------------
    def get_view(self, device: "VoiceDevice | None" = None) -> dict[str, Any]:
        result = view(self.doc)
        result["supported_groups"] = list(GROUPS)
        result["excluded_keys"] = sorted(EXCLUDED_KEYS)
        if device is not None:
            result["following"] = self.following_view(device)
        return result

    def devices_view(self) -> list[dict[str, Any]]:
        from .voice_bridge import async_get_bridge

        items = []
        for device in async_get_bridge(self.hass).devices.values():
            session = device.session
            items.append({
                "install_id": device.install_id, "name": device.name,
                "connected": session is not None and not session.closed,
                "negotiated": bool(session is not None and not session.closed and getattr(session, "profile_sync", False)),
                "following": self.following_view(device),
            })
        return items

    # -- writes --------------------------------------------------------------
    async def async_update(
        self,
        *,
        base_revision: int,
        groups: Any,
        removals: Any = None,
        writer: dict[str, Any],
        request_id: str | None = None,
        origin: "VoiceDevice | None" = None,
        allowed_groups: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Validate, compare revisions, persist, then fan out. Raises ProfileError."""
        clean_removals = sanitize_removals(removals)
        clean, dropped = sanitize_groups(groups, allow_empty=any(clean_removals.values()))
        if allowed_groups is not None and any(group not in allowed_groups for group in (*clean, *clean_removals)):
            raise ProfileError("profile_not_following", "Follow a group before pushing it")
        replay_key = (writer.get("install_id") or writer.get("user_id") or "", request_id or "")
        async with self._lock:
            if self.read_only:
                raise ProfileError("profile_read_only", "Voice profile storage is unavailable")
            if request_id is not None and replay_key in self._recent:
                return dict(self._recent[replay_key])
            if base_revision != self.doc["revision"]:
                raise ProfileError(
                    "profile_conflict", f"Voice profile is at revision {self.doc['revision']}; fetch it and retry"
                )
            new_doc, changed = apply_update(self.doc, clean, clean_removals, writer, dt_util.utcnow().isoformat())
            if changed:
                try:
                    await self._store.async_save(new_doc)
                except Exception as err:
                    raise ProfileError("profile_unavailable", "Voice profile could not be saved") from err
                self.doc = new_doc
            result = {
                "revision": self.doc["revision"], "changed": bool(changed), "updated_at": self.doc["updated_at"],
                "group_revisions": dict(self.doc["group_revisions"]), "dropped": dropped,
            }
            if request_id is not None:
                self._recent[replay_key] = result
                while len(self._recent) > RECENT_REQUESTS:
                    self._recent.popitem(last=False)
        if changed:
            self._fan_out(changed, origin)
            self._notify_listeners()
        return result

    # -- fan-out -------------------------------------------------------------
    @callback
    def _fan_out(self, changed: list[str], origin: "VoiceDevice | None") -> None:
        from .voice_bridge import async_get_bridge

        for device in tuple(async_get_bridge(self.hass).devices.values()):
            if device is not origin:
                self._emit_changed(device, changed, reason="update")

    @callback
    def _emit_changed(self, device: "VoiceDevice", changed: list[str] | None, *, reason: str) -> None:
        """Send ``voice_profile_changed`` to one tablet, only if it negotiated it."""
        session = device.session
        if session is None or session.closed or not getattr(session, "profile_sync", False):
            return
        enabled, followed = self.follow_state(device)
        if reason == "update" and not enabled:
            return
        groups = [g for g in followed if enabled and (changed is None or g in changed)]
        if reason == "update" and not groups:
            return
        session.emit({"event": "voice_profile_changed", "reason": reason, **view(self.doc, groups), "following": {"enabled": enabled, "groups": list(followed)}})


@callback
def async_peek_profile_manager(hass: HomeAssistant) -> VoiceProfileManager | None:
    return hass.data.get(PROFILE_DATA)


async def async_get_profile_manager(hass: HomeAssistant) -> VoiceProfileManager:
    """Return the loaded singleton manager, loading storage once."""
    lock = hass.data.setdefault(PROFILE_LOCK, asyncio.Lock())
    async with lock:
        manager = hass.data.get(PROFILE_DATA)
        if manager is None:
            manager = VoiceProfileManager(hass)
            await manager.async_load()
            hass.data[PROFILE_DATA] = manager
        return manager
