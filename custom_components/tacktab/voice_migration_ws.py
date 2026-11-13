"""Websocket handlers for Voice Satellite migration: admin-only and pairing-scoped."""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .voice_bridge import VoiceBridgeError, VoiceDevice, async_get_bridge
from .voice_migration import async_build_plan
from .voice_protocol import PREFIX

PLAN_COMMAND = f"{PREFIX}migration_plan"
APPLY_COMMAND = f"{PREFIX}migration_apply"
CARRY_OVER = {"voice", "conversation", "assistant", "timers", "appearance"}


def _authorized_device(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> VoiceDevice | None:
    """Return the tablet this socket is paired as, or send the error and None.

    The plan exposes automation/script contents, so a non-admin user or a socket
    that has not completed Tacktab pairing (matching epoch) gets nothing.
    """
    if connection.user is None or not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator access is required to migrate voice")
        return None
    device = next((item for item in async_get_bridge(hass).devices.values() if item.session and item.session.connection is connection), None)
    if device is None or msg.get("epoch") != device.session.epoch:
        connection.send_error(msg["id"], "disconnected", "Tacktab satellite is not paired")
        return None
    return device


@websocket_api.async_response
async def handle_migration_plan(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Expose a read-only old-satellite inventory on HA's authenticated socket."""
    if set(msg) - {"id", "type", "epoch"} or not isinstance(msg.get("id"), int):
        connection.send_error(msg.get("id", 0), "invalid_format", "Invalid migration request")
        return
    if _authorized_device(hass, connection, msg) is None:
        return
    try:
        connection.send_result(msg["id"], await async_build_plan(hass))
    except Exception:
        connection.send_error(msg["id"], "internal_error", "Migration discovery failed")


@websocket_api.async_response
async def handle_migration_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Apply only Tacktab's own HA-side selects; the old integration stays untouched."""
    allowed = {"id", "type", "epoch", "candidate_entity_id", "carry_over"}
    carry = msg.get("carry_over")
    if (
        set(msg) - allowed or not isinstance(msg.get("id"), int) or not isinstance(msg.get("candidate_entity_id"), str)
        or not isinstance(carry, list) or any(item not in CARRY_OVER for item in carry)
    ):
        connection.send_error(msg.get("id", 0), "invalid_format", "Invalid migration request")
        return
    device = _authorized_device(hass, connection, msg)
    if device is None:
        return
    try:
        plan = await async_build_plan(hass)
        candidate = next((item for item in plan["candidates"] if item["entity_id"] == msg["candidate_entity_id"]), None)
        if candidate is None:
            connection.send_error(msg["id"], "not_found", "Voice Satellite candidate was not found")
            return
        settings = candidate["settings"]
        applied = await _apply_home_assistant_side(hass, device, settings, set(carry))
        connection.send_result(msg["id"], {
            "candidate_entity_id": candidate["entity_id"], "applied": applied, "settings": settings,
            "warnings": candidate["warnings"], "references": candidate["references"],
            "references_complete": plan["references_complete"], "reference_warnings": plan["reference_warnings"],
            "timer_event_mapping": plan["timer_event_mapping"],
        })
    except VoiceBridgeError as err:
        connection.send_error(msg["id"], err.code, err.message)
    except Exception:
        connection.send_error(msg["id"], "internal_error", "Migration switch failed")


async def _apply_home_assistant_side(hass: HomeAssistant, device: VoiceDevice, settings: dict[str, Any], carry: set[str]) -> list[str]:
    """Validate everything first, then write; raises before any change on a bad input."""
    from .select import resolve_pipeline_option  # imports Assist; keep the voice bridge gate lazy

    pipeline_select = getattr(device, "select_entities", {}).get("primary")
    vad_select = getattr(device, "vad_select", None)
    pipeline: tuple[str | None, str] | None = None
    if "assistant" in carry and settings.get("assistant"):
        if pipeline_select is None:
            raise VoiceBridgeError("disconnected", "Tacktab pipeline select is unavailable")
        pipeline = resolve_pipeline_option(hass, settings["assistant"])
        pipeline_select.ensure_idle()
    vad = settings.get("finished_speaking") if "conversation" in carry else None
    if vad and vad_select is None:
        raise VoiceBridgeError("disconnected", "Tacktab finished-speaking select is unavailable")
    applied: list[str] = []
    if pipeline is not None:
        pipeline_select.async_select_pipeline_id(pipeline[0])
        applied.append("assistant")
    if vad:
        await vad_select.async_select_option(vad)
        applied.append("finished_speaking")
    return applied
