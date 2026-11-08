"""Polls a Tacktab tablet for its status."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TacktabAuthError, TacktabClient, TacktabError
from .const import DOMAIN, SCAN_INTERVAL
from .rediscover import async_find_tablet

_LOGGER = logging.getLogger(__name__)

# Wi-Fi tablets miss the occasional poll (power saving, roaming, a slow reply).
# Keep the last known state through short gaps so entities do not flicker to
# unavailable; a tablet that stays silent this long is reported unavailable.
UNAVAILABLE_AFTER = timedelta(seconds=60)
# How often an unreachable tablet is looked up again over mDNS.
REDISCOVER_EVERY = timedelta(seconds=60)

type TacktabConfigEntry = ConfigEntry[TacktabCoordinator]


class TacktabCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the latest status of one tablet."""

    config_entry: TacktabConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TacktabConfigEntry, client: TacktabClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client
        self._failing_since: datetime | None = None
        self._last_lookup: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status()
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            if (status := await self._async_follow_new_address()) is not None:
                self._failing_since = None
                return status
            now = dt_util.utcnow()
            if self._failing_since is None:
                self._failing_since = now
            if self.data is not None and now - self._failing_since < UNAVAILABLE_AFTER:
                _LOGGER.debug("Tacktab missed a status poll; keeping the last state: %s", err)
                return self.data
            raise UpdateFailed(f"Tacktab is not reachable: {err}") from err
        self._failing_since = None
        return status

    async def _async_follow_new_address(self) -> dict[str, Any] | None:
        """If the tablet now announces another address, switch to it and poll there."""
        device_id = self.config_entry.unique_id
        now = dt_util.utcnow()
        if not device_id or (self._last_lookup and now - self._last_lookup < REDISCOVER_EVERY):
            return None
        self._last_lookup = now
        try:
            found = await async_find_tablet(self.hass, device_id)
        except Exception:  # noqa: BLE001 - discovery must never break polling
            _LOGGER.debug("Tacktab mDNS lookup failed", exc_info=True)
            return None
        entry = self.config_entry
        if found is None or found == (entry.data[CONF_HOST], entry.data[CONF_PORT]):
            return None
        host, port = found
        self.client.set_address(host, port)
        try:
            status = await self.client.status()
        except TacktabError:
            self.client.set_address(entry.data[CONF_HOST], entry.data[CONF_PORT])
            return None
        if status.get("id") not in (None, device_id):
            self.client.set_address(entry.data[CONF_HOST], entry.data[CONF_PORT])
            return None
        _LOGGER.info("Tacktab %s moved to %s:%s", entry.title, host, port)
        self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port})
        return status

    async def command(self, call: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """Run a command; its answer is the new status, so no extra poll is needed."""
        try:
            self.async_set_updated_data(await call())
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            raise UpdateFailed(f"Tacktab did not accept the command: {err}") from err
