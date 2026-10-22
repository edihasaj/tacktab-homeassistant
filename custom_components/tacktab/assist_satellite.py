"""One official AssistSatelliteEntity per configured Tacktab voice slot."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineStage
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityDescription,
    AssistSatelliteWakeWord,
)
from homeassistant.components.assist_satellite.const import AssistSatelliteEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .voice_bridge import VoiceBridgeError, VoiceDevice, VoiceRun
from .voice_models import SlotConfig


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    """Add actual HA satellites for all enabled logical slots on one device."""
    device: VoiceDevice = entry.runtime_data
    satellites = [TacktabAssistSatellite(device, slot) for slot in device.slots]
    device.satellites = {satellite.slot.slot_id: satellite for satellite in satellites}  # type: ignore[attr-defined]
    device.add_satellite_entities = add  # type: ignore[attr-defined]
    async def start_run(run: VoiceRun, msg: dict[str, Any]) -> None:
        await _start_run(device, run, msg)

    device.start_run = start_run
    add(satellites)


async def async_reconcile_slots(device: VoiceDevice, slots: tuple[SlotConfig, ...]) -> None:
    """Reconcile HA satellite entities before accepting a new slot topology."""
    existing: dict[str, TacktabAssistSatellite] = getattr(device, "satellites", {})
    wanted = {slot.slot_id for slot in slots}
    for slot_id, entity in tuple(existing.items()):
        if slot_id not in wanted:
            await entity.platform.async_remove_entity(entity.entity_id)
            existing.pop(slot_id, None)
    additions = [TacktabAssistSatellite(device, slot) for slot in slots if slot.slot_id not in existing]
    if additions:
        existing.update({entity.slot.slot_id: entity for entity in additions})
        device.add_satellite_entities(additions)  # type: ignore[attr-defined]


async def _start_run(device: VoiceDevice, run: VoiceRun, msg: dict[str, Any]) -> None:
    """Run a selected slot through the base entity's supported pipeline entry point."""
    satellite: TacktabAssistSatellite = device.satellites[run.slot_id]  # type: ignore[attr-defined]
    session = device.session
    assert session is not None and run.context_message is not None
    try:
        async with asyncio.timeout(300):
            await satellite.async_accept_pipeline_from_satellite(
                run.stream(), start_stage=PipelineStage.STT, end_stage=PipelineStage.TTS,
                wake_word_phrase=msg.get("wake_word_phrase"), context=session.connection.context(run.context_message),
            )
        await satellite.async_wait_response(session, run.run_id)
    except TimeoutError:
        session.emit({"event": "pipeline", "run_id": run.run_id, "slot_id": run.slot_id, "core": {"type": "error", "timestamp": "", "data": {"code": "timeout"}}})
    finally:
        if session.runs.get(run.run_id) is run:
            session.runs.pop(run.run_id, None)
        from .voice_bridge import async_get_bridge

        async_get_bridge(device.hass).release_media_lease(session, run.run_id)


class TacktabAssistSatellite(AssistSatelliteEntity):
    """The supported HA satellite facade for one primary or secondary slot."""

    entity_description = AssistSatelliteEntityDescription(key="tacktab_assist_satellite", translation_key="assist_satellite")
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, device: VoiceDevice, slot: SlotConfig) -> None:
        self.device = device
        self.slot = slot
        self._attr_unique_id = f"voice:{device.install_id}:{slot.slot_id}"
        self._attr_translation_key = "assist_satellite"
        self._response_tasks: dict[str, tuple[Any, asyncio.Task[None]]] = {}

    @property
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, f"voice:{self.device.install_id}")}, "name": self.device.name, "manufacturer": "Applifyer", "model": "Tacktab"}

    @property
    def pipeline_entity_id(self) -> str | None:
        return getattr(self.device, "pipeline_entities", {}).get(self.slot.slot_id)

    @property
    def supported_features(self) -> AssistSatelliteEntityFeature:
        if (
            not self.slot.enabled
            or self.device.capabilities is None
            or (self.slot.output == "local" and not self.device.status.playback_ready)
        ):
            return AssistSatelliteEntityFeature(0)
        features = AssistSatelliteEntityFeature(0)
        if self.device.capabilities.announcements:
            features |= AssistSatelliteEntityFeature.ANNOUNCE
        if self.device.capabilities.start_conversation and self.device.status.microphone_ready:
            features |= AssistSatelliteEntityFeature.START_CONVERSATION
        return features

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        wake_words = [] if self.slot.wake_word is None else [AssistSatelliteWakeWord(self.slot.wake_word.id, self.slot.wake_word.phrase, [])]
        return AssistSatelliteConfiguration(wake_words, [word.id for word in wake_words], 1)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        current = self.async_get_configuration().active_wake_words
        if config.active_wake_words != current:
            # Native wake-model configuration is owned by the tablet protocol;
            # accepting an HA request without applying it would be misleading.
            raise VoiceBridgeError("invalid_format", "Wake-word changes must be configured on Tacktab")

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        session = self._session()
        await session.play(
            slot_id=self.slot.slot_id, kind="announcement", message=announcement.message,
            media_id=announcement.media_id, preannounce_media_id=announcement.preannounce_media_id,
            listen_after=False, before_finished=self.tts_response_finished,
        )

    async def async_start_conversation(self, announcement: AssistSatelliteAnnouncement) -> None:
        session = self._session()
        kind = "question" if self._ask_question_future is not None else "start_conversation"
        await session.play(
            slot_id=self.slot.slot_id, kind=kind, message=announcement.message,
            media_id=announcement.media_id, preannounce_media_id=announcement.preannounce_media_id,
            listen_after=True, before_finished=self.tts_response_finished,
        )

    @callback
    def on_pipeline_event(self, event: PipelineEvent) -> None:
        session = self.device.session
        if session is None or not session.runs:
            return
        run = next((item for item in session.runs.values() if item.slot_id == self.slot.slot_id), None)
        if run is None:
            return
        core = {"type": event.type, "timestamp": event.timestamp, "data": event.data}
        session.emit({"event": "pipeline", "run_id": run.run_id, "slot_id": self.slot.slot_id, "core": core})
        if event.type == "tts-end" and event.data:
            output = event.data.get("tts_output", {})
            media_id = output.get("url")
            if isinstance(media_id, str):
                task = self.hass.async_create_task(self._play_response(session, run.run_id, media_id), f"tacktab_voice_response_{run.run_id}")
                self._response_tasks[run.run_id] = (session, task)

    async def _play_response(self, session, run_id: str, media_id: str) -> None:
        try:
            if not session.closed:
                capabilities = self.device.capabilities
                listen_after = bool(
                    self.slot.enabled
                    and capabilities is not None
                    and capabilities.start_conversation
                    and self.device.status.microphone_ready
                )
                await session.play(
                    slot_id=self.slot.slot_id,
                    kind="response",
                    message="",
                    media_id=media_id,
                    preannounce_media_id=None,
                    listen_after=listen_after,
                    run_id=run_id,
                    before_finished=self.tts_response_finished,
                )
        finally:
            self._response_tasks.pop(run_id, None)
            # Local playback completes through the native ACK path; remote
            # output calls this before its terminal protocol event.
            self.tts_response_finished()

    async def async_wait_response(self, session, run_id: str) -> None:
        """Keep the physical device lease until native playback ACK completes."""
        response = self._response_tasks.get(run_id)
        if response is not None and response[0] is session:
            await response[1]

    async def async_cancel_response(self, session, run_id: str) -> None:
        response = self._response_tasks.get(run_id)
        if response is not None and response[0] is session:
            response[1].cancel()
            try:
                await response[1]
            except asyncio.CancelledError:
                pass

    def async_fail_pending_question(self, message: str) -> None:
        """Unblock HA's ask_question caller when native cannot collect speech."""
        future = self._ask_question_future
        if future is not None and not future.done():
            future.set_exception(HomeAssistantError(message))

    def _session(self):
        if self.device.session is None:
            raise VoiceBridgeError("disconnected", "Voice tablet is disconnected")
        return self.device.session
