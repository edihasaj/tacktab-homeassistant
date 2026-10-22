"""Observable Home Assistant media-player output for voice satellites.

The media-player service returning only means that the request was accepted.  A
remote voice turn is complete only after this module has observed the selected
player start *and* return to an idle/off state while it is still reporting the
same media.  This deliberately supports the conservative subset of media
players which exposes that lifecycle; guessing completion would release the
tablet's microphone lease too early.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.components.media_player.const import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DATA_COMPONENT as MEDIA_PLAYER_COMPONENT,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_PLAY_MEDIA,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import SERVICE_MEDIA_STOP
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event


REMOTE_START_TIMEOUT_SECONDS = 15
REMOTE_OUTPUT_TIMEOUT_SECONDS = 300


class RemoteOutputError(Exception):
    """A safe remote-output failure with a protocol-level reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RemotePlayback:
    """One media-player call and the observer which proves physical playback."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        media_id: str,
        *,
        context: Context | None,
        on_started: Callable[[], None],
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.media_id = media_id
        self.context = context
        self.on_started = on_started
        self._future: asyncio.Future[None] = hass.loop.create_future()
        self._started_future: asyncio.Future[None] = hass.loop.create_future()
        self._unsub: Callable[[], None] | None = None
        self._started = False
        self._cancelled = False
        self._expected_url = ""

    async def async_play(self) -> None:
        """Call the official service and wait for an observed terminal state."""
        self._validate_player()
        self._expected_url, content_type = await self._async_resolve_media()
        self._unsub = async_track_state_change_event(
            self.hass, [self.entity_id], self._state_changed
        )
        try:
            data = {
                "entity_id": self.entity_id,
                ATTR_MEDIA_CONTENT_ID: self._expected_url,
                ATTR_MEDIA_CONTENT_TYPE: content_type,
            }
            try:
                # A service return is not a physical start.  The same bounded
                # start window covers service dispatch and a *new* matching
                # state transition, so a no-op request cannot inherit an old
                # playback of the same URL.
                async with asyncio.timeout(REMOTE_START_TIMEOUT_SECONDS):
                    await self.hass.services.async_call(
                        MEDIA_PLAYER_DOMAIN,
                        SERVICE_PLAY_MEDIA,
                        data,
                        blocking=True,
                        context=self.context,
                    )
                    await asyncio.shield(self._started_future)
                async with asyncio.timeout(REMOTE_OUTPUT_TIMEOUT_SECONDS):
                    await asyncio.shield(self._future)
            except TimeoutError as err:
                raise RemoteOutputError("timeout", "Remote speaker did not report completion") from err
            except RemoteOutputError:
                raise
            except Exception as err:
                raise RemoteOutputError("remote_output_failed", "Remote speaker could not start media") from err
        finally:
            self._unsubscribe()

    def cancel(self) -> None:
        """Stop observing a cancelled/disconnected voice turn.

        We must not call media_player.stop here: another HA automation may have
        replaced this media by the time a websocket disconnects.
        """
        self._cancelled = True
        self._unsubscribe()
        if self._started and not self._future.done():
            self._future.set_exception(RemoteOutputError("remote_output_failed", "Remote output cancelled"))
        elif not self._started_future.done():
            self._started_future.set_exception(RemoteOutputError("remote_output_failed", "Remote output cancelled"))

    async def async_cancel_owned_media(self) -> None:
        """Cancel observation and stop only the media this action still owns."""
        self._cancelled = True
        state = self.hass.states.get(self.entity_id)
        if (
            self._started
            and state is not None
            and state.state in (MediaPlayerState.PLAYING, MediaPlayerState.BUFFERING, MediaPlayerState.PAUSED)
            and state.attributes.get(ATTR_MEDIA_CONTENT_ID) == self._expected_url
        ):
            try:
                await self.hass.services.async_call(
                    MEDIA_PLAYER_DOMAIN,
                    SERVICE_MEDIA_STOP,
                    {"entity_id": self.entity_id},
                    blocking=True,
                    context=self.context,
                )
            except Exception:
                # A player can lack STOP or disappear after the matching check.
                # Its observer is still cancelled, and no replacement is touched.
                pass
        self.cancel()

    def _validate_player(self) -> None:
        state = self.hass.states.get(self.entity_id)
        component = self.hass.data.get(MEDIA_PLAYER_COMPONENT)
        if (
            not self.entity_id.startswith(f"{MEDIA_PLAYER_DOMAIN}.")
            or state is None
            or state.state == "unavailable"
            or component is None
            or component.get_entity(self.entity_id) is None
        ):
            raise RemoteOutputError("unsupported_output", "Configured remote speaker is unavailable")
        features = state.attributes.get("supported_features", 0)
        if not isinstance(features, int) or not features & MediaPlayerEntityFeature.PLAY_MEDIA:
            raise RemoteOutputError("unsupported_output", "Configured remote speaker cannot play media")

    async def _async_resolve_media(self) -> tuple[str, str]:
        """Use HA's media-source and signed-URL helpers, never an HA token."""
        try:
            if media_source.is_media_source_id(self.media_id):
                resolved = await media_source.async_resolve_media(
                    self.hass, self.media_id, self.entity_id
                )
                url = async_process_play_media_url(self.hass, resolved.url)
                return url, resolved.mime_type
            return async_process_play_media_url(self.hass, self.media_id), "music"
        except Exception as err:
            raise RemoteOutputError("remote_output_failed", "Remote media could not be resolved") from err

    @callback
    def _state_changed(self, event: Event) -> None:
        self._observe(event.data.get("new_state"), event.data.get("old_state"))

    @callback
    def _observe(self, state: State | None, old_state: State | None = None) -> None:
        if self._cancelled or self._future.done():
            return
        if state is None:
            self._fail("Remote speaker was removed")
            return
        if state.state == "unavailable":
            self._fail("Remote speaker became unavailable")
            return
        media_id = state.attributes.get(ATTR_MEDIA_CONTENT_ID)
        if not self._started:
            was_matching = (
                old_state is not None
                and old_state.state in (MediaPlayerState.PLAYING, MediaPlayerState.BUFFERING)
                and old_state.attributes.get(ATTR_MEDIA_CONTENT_ID) == self._expected_url
            )
            if (
                state.state in (MediaPlayerState.PLAYING, MediaPlayerState.BUFFERING)
                and media_id == self._expected_url
                and not was_matching
            ):
                self._started = True
                self.on_started()
                self._started_future.set_result(None)
            return
        # Once our media has started, another reported item is an explicit
        # replacement.  Do not credit its terminal state to this voice action.
        if media_id != self._expected_url:
            self._future.set_exception(RemoteOutputError("remote_output_failed", "Remote media was replaced"))
        elif state.state in (MediaPlayerState.IDLE, MediaPlayerState.OFF):
            self._future.set_result(None)

    def _fail(self, message: str) -> None:
        error = RemoteOutputError("remote_output_failed", message)
        if self._started:
            self._future.set_exception(error)
        elif not self._started_future.done():
            self._started_future.set_exception(error)

    def _unsubscribe(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
