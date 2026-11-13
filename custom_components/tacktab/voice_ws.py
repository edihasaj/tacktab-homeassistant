"""Home Assistant websocket registration for the Tacktab native voice bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .voice_bridge import VoiceBridgeError, async_get_bridge
from .voice_migration import async_build_plan
from .voice_protocol import PREFIX, VoiceProtocolError, validate_message
from .voice_setup import async_ensure_voice_entry
from .const import REALTIME_ENABLED

_COMMANDS = (
    "connect", "configure", "status", "snapshot", "claim", "run", "audio_end", "cancel", "cancel_output", "ack", "timer_action"
)
_MIGRATION_COMMAND = f"{PREFIX}migration_plan"
_MIGRATION_APPLY_COMMAND = f"{PREFIX}migration_apply"
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
    websocket_api.async_register_command(hass, _MIGRATION_COMMAND, _handle_migration_plan, schema=vol.Schema({}, extra=vol.ALLOW_EXTRA))
    websocket_api.async_register_command(hass, _MIGRATION_APPLY_COMMAND, _handle_migration_apply, schema=vol.Schema({}, extra=vol.ALLOW_EXTRA))
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
async def _handle_migration_plan(
    hass: HomeAssistant, connection: websocket_api.connection.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Expose a read-only old-satellite inventory on HA's authenticated socket."""
    if set(msg) - {"id", "type", "epoch"} or not isinstance(msg.get("id"), int):
        connection.send_error(msg.get("id", 0), "invalid_format", "Invalid migration request")
        return
    try:
        connection.send_result(msg["id"], await async_build_plan(hass))
    except Exception:
        connection.send_error(msg["id"], "internal_error", "Migration discovery failed")


@websocket_api.async_response
async def _handle_migration_apply(
    hass: HomeAssistant, connection: websocket_api.connection.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Apply only Tacktab's primary pipeline select; old integration remains untouched."""
    allowed = {"id", "type", "epoch", "candidate_entity_id", "carry_over"}
    if set(msg) - allowed or not isinstance(msg.get("id"), int) or not isinstance(msg.get("candidate_entity_id"), str) or not isinstance(msg.get("carry_over"), list):
        connection.send_error(msg.get("id", 0), "invalid_format", "Invalid migration request")
        return
    if any(item not in {"voice", "conversation", "assistant", "timers", "appearance"} for item in msg["carry_over"]):
        connection.send_error(msg["id"], "invalid_format", "Invalid migration request")
        return
    try:
        device = next((item for item in async_get_bridge(hass).devices.values() if item.session and item.session.connection is connection), None)
        if device is None or msg.get("epoch") != device.session.epoch:
            connection.send_error(msg["id"], "disconnected", "Tacktab satellite is not paired")
            return
        plan = await async_build_plan(hass)
        candidate = next((item for item in plan["candidates"] if item["entity_id"] == msg["candidate_entity_id"]), None)
        if candidate is None:
            connection.send_error(msg["id"], "not_found", "Voice Satellite candidate was not found")
            return
        applied: list[str] = []
        if "assistant" in msg["carry_over"] and candidate["settings"].get("assistant"):
            select = getattr(device, "select_entities", {}).get("primary")
            if select is None:
                connection.send_error(msg["id"], "disconnected", "Tacktab pipeline select is unavailable")
                return
            await select.async_select_option(candidate["settings"]["assistant"])
            applied.append("assistant")
        # Remaining selections are returned to the native client. It owns
        # microphone, VAD and timer rendering, so HA must not pretend to set them.
        connection.send_result(msg["id"], {"applied": applied, "settings": candidate["settings"], "references": candidate["references"], "timer_event_mapping": plan["timer_event_mapping"]})
    except VoiceBridgeError as err:
        connection.send_error(msg["id"], err.code, err.message)
    except Exception:
        connection.send_error(msg["id"], "internal_error", "Migration switch failed")


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
