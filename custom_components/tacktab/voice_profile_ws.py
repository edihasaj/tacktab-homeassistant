"""Administrator websocket API for the fleet voice profile.

Tablets use the bridge messages instead (see ``voice_profile_bridge``). These
commands are for the Home Assistant frontend, scripts and tests, and require an
administrator.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .voice_profile import ProfileError
from .voice_profile_manager import async_get_profile_manager

GET = "tacktab/voice_profile/get"
UPDATE = "tacktab/voice_profile/update"


@callback
def async_register_voice_profile_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, _handle_get)
    websocket_api.async_register_command(hass, _handle_update)


@websocket_api.websocket_command({vol.Required("type"): GET})
@websocket_api.require_admin
@websocket_api.async_response
async def _handle_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = await async_get_profile_manager(hass)
    connection.send_result(msg["id"], {**manager.get_view(), "devices": manager.devices_view()})


@websocket_api.websocket_command({
    vol.Required("type"): UPDATE,
    vol.Required("base_revision"): vol.All(int, vol.Range(min=0)),
    vol.Required("groups"): dict,
    vol.Optional("remove"): dict,
    vol.Optional("request_id"): vol.All(str, vol.Length(min=1, max=64)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def _handle_update(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = await async_get_profile_manager(hass)
    user = connection.user
    writer = {"type": "user", "user_id": user.id if user else None, "name": user.name if user else None}
    try:
        result = await manager.async_update(
            base_revision=msg["base_revision"], groups=msg["groups"], removals=msg.get("remove"),
            writer=writer, request_id=msg.get("request_id"),
        )
    except ProfileError as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], result)
