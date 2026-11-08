"""Find a paired tablet again after its Wi-Fi address changed.

Tablets advertise ``_tacktab._tcp`` with their device ID in the TXT record.
Home Assistant's zeroconf browser already watches that type (manifest), so the
shared cache holds every tablet currently announcing itself. Matching on the
ID rather than the address keeps this safe when routers reshuffle leases.
"""

from __future__ import annotations

import logging

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo
from zeroconf.const import _CLASS_IN, _TYPE_PTR

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_tacktab._tcp.local."
LOOKUP_TIMEOUT_MS = 3000


async def async_find_tablet(hass: HomeAssistant, device_id: str) -> tuple[str, int] | None:
    """Return the advertised IPv4 address and port for this device ID, if announced."""
    aiozc = await zeroconf.async_get_async_instance(hass)
    zc = aiozc.zeroconf
    names = {record.alias for record in zc.cache.async_all_by_details(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN)}
    for name in sorted(names):
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        if not await info.async_request(zc, LOOKUP_TIMEOUT_MS):
            continue
        if (info.properties.get(b"id") or b"").decode(errors="replace") != device_id:
            continue
        addresses = info.parsed_scoped_addresses(IPVersion.V4Only)
        if addresses and info.port:
            return addresses[0], info.port
    return None
