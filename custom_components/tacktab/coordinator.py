"""Polls a Tacktab tablet for its status."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TacktabAuthError, TacktabClient, TacktabError
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Wi-Fi tablets miss the occasional poll (power saving, roaming, a slow reply).
# Keep the last known state through short gaps so entities do not flicker to
# unavailable; a tablet that stays silent this long is reported unavailable.
UNAVAILABLE_AFTER = timedelta(seconds=60)

type TacktabConfigEntry = ConfigEntry[TacktabCoordinator]


class TacktabCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the latest status of one tablet."""

    config_entry: TacktabConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TacktabConfigEntry, client: TacktabClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client
        self._failing_since: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.client.status()
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            now = dt_util.utcnow()
            if self._failing_since is None:
                self._failing_since = now
            if self.data is not None and now - self._failing_since < UNAVAILABLE_AFTER:
                _LOGGER.debug("Tacktab missed a status poll; keeping the last state: %s", err)
                return self.data
            raise UpdateFailed(f"Tacktab is not reachable: {err}") from err
        self._failing_since = None
        return status

    async def command(self, call: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """Run a command; its answer is the new status, so no extra poll is needed."""
        try:
            self.async_set_updated_data(await call())
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            raise UpdateFailed(f"Tacktab did not accept the command: {err}") from err
