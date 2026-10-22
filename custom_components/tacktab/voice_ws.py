"""Home Assistant websocket registration for the Tacktab native voice bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .voice_bridge import VoiceBridgeError, async_get_bridge
from .voice_protocol import PREFIX, VoiceProtocolError, validate_message
from .voice_setup import async_ensure_voice_entry
from .const import REALTIME_ENABLED

_COMMANDS = (
    "connect", "configure", "status", "snapshot", "claim", "run", "audio_end", "cancel", "cancel_output", "ack", "timer_action"
)
_REALTIME_COMMANDS = (
    "configure_provider", "configure_slot", "catalog", "start", "audio", "audio_end", "playback", "interrupt", "stop"
)
_REALTIME_PREFIX = "tacktab/realtime/"


@callback
def async_register_voice_websocket_api(hass: HomeAssistant) -> None:
    """Register strict custom commands without changing HA's standard envelopes."""
    for command in _COMMANDS:
        websocket_api.async_register_command(
            hass,
            f"{PREFIX}{command}",
            _handle_voice_command,
            schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
        )
    if REALTIME_ENABLED:
        for command in _REALTIME_COMMANDS:
            websocket_api.async_register_command(
                hass,
                f"{_REALTIME_PREFIX}{command}",
                _handle_realtime_command,
                schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
            )


@websocket_api.async_response
async def _handle_voice_command(
    hass: HomeAssistant, connection: websocket_api.connection.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate then dispatch one bridge command, always keeping errors generic."""
    if msg.get("type") == f"{PREFIX}connect" and (connection.user is None or not connection.user.is_admin):
        connection.send_error(msg["id"], "unauthorized", "Administrator access is required to pair voice")
        return
    try:
        # Validate before the first pairing creates a config entry.
        validated = validate_message(msg)
        if msg.get("type") == f"{PREFIX}connect":
            await async_ensure_voice_entry(hass, validated)
        await async_get_bridge(hass).handle(connection, validated)
    except VoiceProtocolError as err:
        connection.send_error(msg["id"], err.code, err.message)
    except Exception:  # bridge input must never disclose media, credentials, or transcripts
        connection.send_error(msg["id"], "internal_error", "Voice bridge command failed")


@websocket_api.async_response
async def _handle_realtime_command(
    hass: HomeAssistant, connection: websocket_api.connection.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Route provider transport through the bridge-bound realtime controller."""
    from .voice_realtime import async_setup_realtime
    from .voice_realtime_protocol import RealtimeProtocolError

    try:
        await async_setup_realtime(hass, async_get_bridge(hass)).handle(connection, msg)
    except RealtimeProtocolError as err:
        connection.send_error(msg["id"], err.code, err.message)
    except Exception:
        connection.send_error(msg["id"], "internal_error", "Realtime voice command failed")
