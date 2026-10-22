"""Per-slot pipeline selectors for the official Tacktab Assist satellites."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .voice_bridge import VoiceBridgeError, VoiceDevice, async_get_bridge

PREFERRED = "preferred"
UNAVAILABLE = "unavailable"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    """Add generic selects; each satellite resolves its own configured selection."""
    device: VoiceDevice = entry.runtime_data
    entities = [TacktabPipelineSelect(device, slot.slot_id) for slot in device.slots]
    device.select_entities = {entity.slot_id: entity for entity in entities}  # type: ignore[attr-defined]
    device.add_select_entities = add  # type: ignore[attr-defined]
    add(entities)


async def async_reconcile_slots(device: VoiceDevice, slots) -> None:
    """Keep generic pipeline selects in lockstep with slot entities."""
    existing: dict[str, TacktabPipelineSelect] = getattr(device, "select_entities", {})
    wanted = {slot.slot_id for slot in slots}
    for slot_id, entity in tuple(existing.items()):
        if slot_id not in wanted:
            await entity.platform.async_remove_entity(entity.entity_id)
            existing.pop(slot_id, None)
            getattr(device, "pipeline_entities", {}).pop(slot_id, None)
    additions = [TacktabPipelineSelect(device, slot.slot_id) for slot in slots if slot.slot_id not in existing]
    if additions:
        existing.update({entity.slot_id: entity for entity in additions})
        device.add_select_entities(additions)  # type: ignore[attr-defined]


class TacktabPipelineSelect(SelectEntity):
    """Maps a stable saved pipeline id to HA's user-facing pipeline name."""

    _attr_has_entity_name = True

    def __init__(self, device: VoiceDevice, slot_id: str) -> None:
        self.device = device
        self.slot_id = slot_id
        self._attr_unique_id = f"voice:{device.install_id}:{slot_id}:pipeline"
        self._attr_name = f"{slot_id.title()} pipeline"

    @property
    def device_info(self) -> dict:
        return {"identifiers": {("tacktab", f"voice:{self.device.install_id}")}}

    @property
    def options(self) -> list[str]:
        return [PREFERRED, *(pipeline.name for pipeline in async_get_pipelines(self.hass))]

    @property
    def current_option(self) -> str:
        pipeline_id = self.device.slot(self.slot_id).pipeline_id
        if pipeline_id is None:
            return PREFERRED
        for pipeline in async_get_pipelines(self.hass):
            if pipeline.id == pipeline_id:
                return pipeline.name
        # This intentionally is not `preferred`: an absent configured pipeline
        # must fail at run time instead of silently changing user intent.
        return UNAVAILABLE

    async def async_added_to_hass(self) -> None:
        """Publish the real entity id after HA has assigned it."""
        await super().async_added_to_hass()
        if not hasattr(self.device, "pipeline_entities"):
            self.device.pipeline_entities = {}  # type: ignore[attr-defined]
        self.device.pipeline_entities[self.slot_id] = self.entity_id  # type: ignore[attr-defined]

    async def async_select_option(self, option: str) -> None:
        if (
            self.device.config_lock.locked()
            or
            self.device.media_lease is not None
            or getattr(self.device, "realtime_active", False)
            or (self.device.session is not None and (self.device.session.runs or self.device.session.actions))
        ):
            raise VoiceBridgeError("busy", "Voice device is busy")
        pipeline_id = None
        if option != PREFERRED:
            pipeline_id = next((pipeline.id for pipeline in async_get_pipelines(self.hass) if pipeline.name == option), None)
            if pipeline_id is None:
                raise VoiceBridgeError("pipeline_not_found", "Configured pipeline no longer exists")
        slots = list(self.device.slots)
        index = next(index for index, slot in enumerate(slots) if slot.slot_id == self.slot_id)
        slots[index] = replace(slots[index], pipeline_id=pipeline_id)
        self.device.slots = tuple(slots)
        self.device.config_revision += 1
        async_get_bridge(self.hass)._persist_slots(self.device)
        async_get_bridge(self.hass)._sync_satellite_slots(self.device)
        self.async_write_ha_state()
        if self.device.session is not None:
            self.device.session.emit({"event": "state", "snapshot": self.device.snapshot()})
