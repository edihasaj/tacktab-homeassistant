"""Fleet voice profile persistence, migration and revision conflicts (no websocket)."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.tacktab.voice_profile import ProfileError
from custom_components.tacktab.voice_profile_manager import STORAGE_KEY, VoiceProfileManager

WRITER = {"type": "user", "user_id": "u1", "name": "Edi"}


async def _manager(hass: HomeAssistant) -> VoiceProfileManager:
    manager = VoiceProfileManager(hass)
    await manager.async_load()
    return manager


async def test_updates_persist_and_survive_a_restart(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    result = await manager.async_update(
        base_revision=0, writer=WRITER,
        groups={"voice": {"wake_sensitivity": 0.4, "wake_future_knob": {"a": [1]}}, "chimes": {"chime_volume": 0.3}},
    )
    assert result["revision"] == 1 and result["changed"] is True
    stored = hass_storage[STORAGE_KEY]
    assert stored["version"] == 1 and stored["minor_version"] == 1
    assert stored["data"]["revision"] == 1 and stored["data"]["updated_by"] == WRITER

    reloaded = await _manager(hass)
    assert reloaded.doc == manager.doc
    assert reloaded.doc["groups"]["voice"]["wake_future_knob"] == {"a": [1]}
    assert reloaded.doc["group_revisions"] == {
        "voice": 1, "appearance": 0, "conversation": 0, "timers": 0, "chimes": 1,
    }


async def test_an_empty_store_starts_at_revision_zero(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    assert manager.doc["revision"] == 0 and not manager.read_only
    assert STORAGE_KEY not in hass_storage  # nothing is written until the first change


async def test_older_sparse_documents_migrate_forward(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": 1, "minor_version": 0, "key": STORAGE_KEY,
        "data": {
            "revision": 7,
            # No group_revisions, no schema_version, an excluded key and a bad value.
            "groups": {"voice": {"wake_sensitivity": 0.2, "arbitration_offset_db": 9}, "timers": {"timer_scale": 9}},
        },
    }
    manager = await _manager(hass)
    assert not manager.read_only
    assert manager.doc["schema_version"] == 1 and manager.doc["revision"] == 7
    assert manager.doc["groups"]["voice"] == {"wake_sensitivity": 0.2}
    assert manager.doc["groups"]["timers"] == {}
    assert manager.doc["group_revisions"]["voice"] == 7
    result = await manager.async_update(base_revision=7, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.3}})
    assert result["revision"] == 8
    assert hass_storage[STORAGE_KEY]["minor_version"] == 1


async def test_storage_from_a_newer_release_is_never_overwritten(hass: HomeAssistant, hass_storage) -> None:
    newer = {"version": 2, "minor_version": 1, "key": STORAGE_KEY, "data": {"revision": 3, "future": True}}
    hass_storage[STORAGE_KEY] = dict(newer)
    manager = await _manager(hass)
    assert manager.read_only
    with pytest.raises(ProfileError) as err:
        await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.3}})
    assert err.value.code == "profile_read_only"
    assert hass_storage[STORAGE_KEY] == newer


async def test_a_damaged_document_loads_as_empty_instead_of_crashing(hass: HomeAssistant, hass_storage) -> None:
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": ["not", "a", "doc"]}
    manager = await _manager(hass)
    assert manager.doc["revision"] == 0 and not manager.read_only


async def test_stale_base_revisions_are_rejected(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.4}})
    for stale in (0, 2, 99):
        with pytest.raises(ProfileError) as err:
            await manager.async_update(base_revision=stale, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.9}})
        assert err.value.code == "profile_conflict"
    assert manager.doc["groups"]["voice"] == {"wake_sensitivity": 0.4}
    assert hass_storage[STORAGE_KEY]["data"]["revision"] == 1


async def test_a_no_op_update_does_not_bump_the_revision_or_write(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.4}})
    with patch.object(manager._store, "async_save") as save:
        result = await manager.async_update(base_revision=1, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.4}})
    assert result["changed"] is False and result["revision"] == 1
    save.assert_not_called()


async def test_racing_writers_with_the_same_base_revision_cannot_both_win(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)

    async def write(value: float):
        return await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": value}})

    results = await asyncio.gather(write(0.1), write(0.2), write(0.3), return_exceptions=True)
    winners = [item for item in results if isinstance(item, dict)]
    losers = [item for item in results if isinstance(item, ProfileError)]
    assert len(winners) == 1 and len(losers) == 2
    assert all(loser.code == "profile_conflict" for loser in losers)
    assert manager.doc["revision"] == 1


async def test_a_failed_save_leaves_the_document_untouched(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    with patch.object(manager._store, "async_save", side_effect=OSError("disk full")):
        with pytest.raises(ProfileError) as err:
            await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.4}})
    assert err.value.code == "profile_unavailable"
    assert manager.doc["revision"] == 0 and manager.doc["groups"]["voice"] == {}
    # The same base revision is still usable afterwards.
    result = await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.4}})
    assert result["revision"] == 1


async def test_invalid_input_is_rejected_before_any_revision_check_or_write(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    with pytest.raises(ProfileError) as err:
        await manager.async_update(base_revision=5, writer=WRITER, groups={"voice": {"wake_sensitivity": 9}})
    assert err.value.code == "invalid_profile"
    assert STORAGE_KEY not in hass_storage


async def test_a_newer_minor_version_is_read_only_and_keeps_unknown_content(hass: HomeAssistant, hass_storage) -> None:
    """Version 1.2 may hold groups and metadata this release cannot represent."""
    newer = {
        "version": 1, "minor_version": 2, "key": STORAGE_KEY,
        "data": {
            "schema_version": 2, "revision": 4, "groups": {"voice": {"wake_sensitivity": 0.2}, "gestures": {"swipe": True}},
            "sync": {"cursor": "abc"},
        },
    }
    hass_storage[STORAGE_KEY] = deepcopy(newer)
    manager = await _manager(hass)
    assert manager.read_only
    with pytest.raises(ProfileError) as err:
        await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.3}})
    assert err.value.code == "profile_read_only"
    # Nothing was rewritten: unknown group, metadata and the 1.2 version survive.
    assert hass_storage[STORAGE_KEY] == newer


def _deep(levels: int) -> dict:
    value: dict = {}
    for _ in range(levels):
        value = {"a": value}
    return value


async def test_loading_a_deep_stored_value_repairs_it_and_does_not_block_setup(hass: HomeAssistant, hass_storage) -> None:
    # The storage fixture deep-copies documents (and would itself recurse), so hand
    # the manager what Home Assistant's JSON loader returns from disk directly.
    data = {"revision": 5, "groups": {"voice": {"wake_sensitivity": 0.2, "future_deep": _deep(1000)}}}
    manager = VoiceProfileManager(hass)
    with patch.object(manager._store, "async_load", AsyncMock(return_value=data)):
        await manager.async_load()
    assert not manager.read_only
    assert manager.doc["revision"] == 5 and manager.doc["groups"]["voice"] == {"wake_sensitivity": 0.2}
    result = await manager.async_update(base_revision=5, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.3}})
    assert result["revision"] == 6


async def test_an_unreadable_document_never_raises_from_load_and_is_not_overwritten(hass: HomeAssistant, hass_storage) -> None:
    stored = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": {"revision": 5, "groups": {}}}
    hass_storage[STORAGE_KEY] = deepcopy(stored)
    with patch("custom_components.tacktab.voice_profile_manager.normalize_document", side_effect=RecursionError):
        manager = await _manager(hass)  # must not raise
    assert manager.read_only and manager.doc["revision"] == 0
    with pytest.raises(ProfileError) as err:
        await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"wake_sensitivity": 0.3}})
    assert err.value.code == "profile_read_only"
    assert hass_storage[STORAGE_KEY] == stored


async def test_a_deep_update_is_invalid_profile_and_changes_nothing(hass: HomeAssistant, hass_storage) -> None:
    manager = await _manager(hass)
    with pytest.raises(ProfileError) as err:
        await manager.async_update(base_revision=0, writer=WRITER, groups={"voice": {"future": _deep(1000)}})
    assert err.value.code == "invalid_profile"
    assert manager.doc["revision"] == 0 and STORAGE_KEY not in hass_storage
