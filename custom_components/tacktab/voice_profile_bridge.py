"""Bridge commands for the fleet voice profile (`voice_profile_get` / `voice_profile_update`).

These commands exist only on sessions that negotiated the ``voice_profile``
capability on protocol 2. A tablet may read any time and may update only when
it has opted in and only for groups it follows; the pairing check is the bridge's
own (an admin paired the tablet, and the update arrives on its paired session).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .voice_profile import ProfileError
from .voice_profile_manager import async_get_profile_manager

if TYPE_CHECKING:
    from homeassistant.components.websocket_api.connection import ActiveConnection

    from .voice_bridge import VoiceSession

PROFILE_COMMANDS = frozenset({"voice_profile_get", "voice_profile_update"})


def require_negotiated(session: "VoiceSession") -> None:
    """Refuse the command unless this session agreed to the profile messages."""
    if not session.profile_sync:
        raise ProfileError("unsupported_capability", "Voice profile sync was not negotiated")


async def async_connect_summary(session: "VoiceSession") -> dict[str, Any]:
    """The small block added to a negotiated connect result: revision and follow state."""
    manager = await async_get_profile_manager(session.hass)
    return {
        "revision": manager.doc["revision"],
        "supported_groups": manager.get_view()["supported_groups"],
        "following": manager.following_view(session.device),
    }


async def async_handle_profile_command(session: "VoiceSession", connection: "ActiveConnection", msg: dict[str, Any]) -> None:
    command = msg["type"].removeprefix("tacktab/voice/")
    require_negotiated(session)
    manager = await async_get_profile_manager(session.hass)
    if command == "voice_profile_get":
        connection.send_result(msg["id"], manager.get_view(session.device))
        return
    enabled, followed = manager.follow_state(session.device)
    if not enabled:
        raise ProfileError("profile_not_following", "Opt this tablet in to the fleet profile first")
    writer = {"type": "tablet", "install_id": session.device.install_id, "name": session.device.name}
    result = await manager.async_update(
        base_revision=msg["base_revision"],
        groups=msg["groups"],
        writer=writer,
        request_id=msg["request_id"],
        origin=session.device,
        allowed_groups=followed,
    )
    connection.send_result(msg["id"], result)
