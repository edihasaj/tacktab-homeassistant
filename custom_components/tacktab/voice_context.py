"""Room and tablet context shared by realtime instructions and script tools.

Home Assistant's own conversation agent already resolves the speaking area for
an Assist pipeline from the satellite entity and its device. Realtime sessions
bypass that pipeline, so this module produces the equivalent tablet name and
area text for the provider instructions.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback

DEVICE_ID_FIELD = "tacktab_device_id"
DEVICE_NAME_FIELD = "tacktab_device_name"


@callback
def async_device_identity(hass: HomeAssistant, device_id: str | None, fallback_name: str) -> tuple[str, str | None]:
    """Return the HA device name and area name for one voice tablet."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr

    name = fallback_name
    area_name: str | None = None
    if not device_id:
        return name, area_name
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return name, area_name
    name = device.name_by_user or device.name or fallback_name
    area_id = dr.async_get_effective_area_id(hass, device)
    if area_id is not None and (area := ar.async_get(hass).async_get_area(area_id)) is not None:
        area_name = area.name
    return name, area_name


@callback
def async_room_prompt(hass: HomeAssistant, device_id: str | None, fallback_name: str) -> str:
    """Describe the speaking tablet and its area for a realtime model."""
    name, area_name = async_device_identity(hass, device_id, fallback_name)
    if area_name is None:
        return (
            f"You are speaking through the Tacktab tablet {name!r}. "
            'When the user names no room, act on the entities in that tablet\'s Home Assistant area.'
        )
    return (
        f"You are speaking through the Tacktab tablet {name!r} in the Home Assistant area {area_name!r}. "
        f'When the user says something like "turn on the lights" without naming a room, '
        f"use the {area_name} area. If they name another room, area, floor or device, use that instead."
    )
