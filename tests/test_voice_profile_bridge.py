"""Fleet voice profile over the real Home Assistant websocket API and bridge."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN
from custom_components.tacktab.voice_profile import GROUPS
from custom_components.tacktab.voice_profile_manager import (
    OPTION_FOLLOW,
    OPTION_GROUPS,
    async_get_profile_manager,
)
from tests.test_voice_protocol import CONNECT
from tests.test_voice_runtime import VOICE_DATA

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
C = "00000000-0000-4000-8000-00000000000c"


async def _entry(hass: HomeAssistant, install_id: str, name: str, **options) -> MockConfigEntry:
    data = {**deepcopy(VOICE_DATA), "voice_install_id": install_id}
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"voice:{install_id}", title=name, data=data, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


class Tablet:
    """A paired tablet on its own websocket."""

    def __init__(self, client, install_id: str, name: str) -> None:
        self.client, self.install_id, self.name = client, install_id, name
        self.epoch = ""
        self.connect_result: dict = {}

    async def connect(self, *, protocol: int = 2, voice_profile: bool | None = True) -> dict:
        message = deepcopy(CONNECT)
        message.update(protocol=protocol, install_id=self.install_id, name=self.name)
        if voice_profile is not None:
            message["capabilities"]["voice_profile"] = voice_profile
        await self.client.send_json_auto_id(message)
        response = await self.client.receive_json()
        while response["type"] == "event":  # a replaced session announces its closing first
            response = await self.client.receive_json()
        assert response["success"], response
        self.connect_result = response["result"]
        self.epoch = self.connect_result["epoch"]
        return self.connect_result

    async def call(self, command: str, **fields) -> dict:
        await self.client.send_json_auto_id({"type": f"tacktab/voice/{command}", "epoch": self.epoch, **fields})
        return await self.client.receive_json()

    async def get(self) -> dict:
        return await self.call("voice_profile_get")

    async def push(self, base_revision: int, groups: dict) -> dict:
        return await self.call(
            "voice_profile_update", request_id=str(uuid4()), base_revision=base_revision, groups=groups
        )

    async def event(self) -> dict:
        message = await asyncio.wait_for(self.client.receive_json(), 2)
        assert message["type"] == "event", message
        return message["event"]

    async def assert_no_event(self) -> None:
        """A command's own result, then its state event, must be the only traffic."""
        response = await self.call("snapshot")
        assert response["success"]
        assert (await self.event())["event"] == "snapshot"


@pytest.fixture
async def fleet(hass: HomeAssistant, hass_ws_client):
    assert await async_setup_component(hass, "homeassistant", {})
    entries = {
        A: await _entry(hass, A, "Kitchen"),
        B: await _entry(hass, B, "Hallway"),
    }
    tablets = {
        A: Tablet(await hass_ws_client(hass), A, "Kitchen"),
        B: Tablet(await hass_ws_client(hass), B, "Hallway"),
    }
    return entries, tablets


async def _follow(hass: HomeAssistant, entry, enabled: bool = True, groups=GROUPS) -> None:
    await (await async_get_profile_manager(hass)).async_set_follow(entry, enabled, groups)


async def test_connect_negotiates_the_capability_and_reports_the_profile_summary(hass, fleet) -> None:
    entries, tablets = fleet
    result = await tablets[A].connect()
    assert result["voice_profile"] == {
        "revision": 0, "supported_groups": list(GROUPS), "following": {"enabled": False, "groups": list(GROUPS)},
    }


@pytest.mark.parametrize(("protocol", "capability"), [(2, None), (2, False), (1, None)])
async def test_older_clients_never_see_a_profile_message(hass, hass_ws_client, fleet, protocol, capability) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    old, new = tablets[A], tablets[B]
    await _follow(hass, entries[B])
    result = await old.connect(protocol=protocol, voice_profile=capability)
    assert "voice_profile" not in result
    await new.connect()

    # The commands are refused for a client which never negotiated them ...
    response = await old.get()
    assert not response["success"] and response["error"]["code"] == "unsupported_capability"
    response = await old.push(0, {"voice": {"wake_sensitivity": 0.4}})
    assert not response["success"] and response["error"]["code"] == "unsupported_capability"

    # ... and a change made elsewhere is never pushed to it, even when it follows the fleet.
    admin = await hass_ws_client(hass)
    await admin.send_json_auto_id({
        "type": "tacktab/voice_profile/update", "base_revision": 0, "groups": {"voice": {"wake_sensitivity": 0.9}},
    })
    assert (await admin.receive_json())["success"]
    assert (await new.event())["event"] == "voice_profile_changed"
    await old.assert_no_event()


async def test_negotiation_is_per_session_not_per_device(hass, fleet) -> None:
    """Reconnecting without the capability turns the messages back off."""
    entries, tablets = fleet
    await _follow(hass, entries[A])
    tablet = tablets[A]
    await tablet.connect()
    assert (await tablet.get())["success"]
    await tablet.connect(voice_profile=None)
    assert (await tablet.get())["error"]["code"] == "unsupported_capability"


async def test_a_tablet_must_opt_in_before_it_can_push(hass, fleet) -> None:
    entries, tablets = fleet
    tablet = tablets[A]
    await tablet.connect()
    response = await tablet.push(0, {"voice": {"wake_sensitivity": 0.4}})
    assert response["error"]["code"] == "profile_not_following"
    assert (await tablet.get())["result"]["revision"] == 0


async def test_a_tablet_can_push_only_groups_it_follows(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A], True, ["voice"])
    tablet = tablets[A]
    await tablet.connect()
    response = await tablet.push(0, {"voice": {"wake_sensitivity": 0.4}, "timers": {"show_timers": False}})
    assert response["error"]["code"] == "profile_not_following"
    response = await tablet.push(0, {"voice": {"wake_sensitivity": 0.4}})
    assert response["success"] and response["result"]["revision"] == 1


async def test_push_reaches_followers_for_followed_groups_only(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await _follow(hass, entries[B], True, ["voice", "chimes"])
    for tablet in tablets.values():
        await tablet.connect()

    response = await tablets[A].push(0, {
        "voice": {"wake_sensitivity": 0.4}, "timers": {"show_timers": False}, "chimes": {"chime_volume": 0.2},
    })
    assert response["success"]
    assert response["result"]["revision"] == 1 and response["result"]["changed"] is True

    event = await tablets[B].event()
    assert event["event"] == "voice_profile_changed" and event["reason"] == "update"
    assert event["revision"] == 1 and event["seq"] > 0 and event["epoch"] == tablets[B].epoch
    assert event["groups"] == {"voice": {"wake_sensitivity": 0.4}, "chimes": {"chime_volume": 0.2}}
    assert event["following"] == {"enabled": True, "groups": ["voice", "chimes"]}
    assert event["updated_by"] == {"type": "tablet", "install_id": A, "name": "Kitchen"}
    assert event["group_revisions"]["timers"] == 1
    # The writer got its result only; there is no echo of its own change.
    await tablets[A].assert_no_event()


async def test_a_tablet_that_has_not_opted_in_receives_nothing(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    for tablet in tablets.values():
        await tablet.connect()
    assert (await tablets[A].push(0, {"voice": {"wake_sensitivity": 0.4}}))["success"]
    await tablets[B].assert_no_event()


async def test_stale_writes_are_rejected_and_do_not_change_anything(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await _follow(hass, entries[B])
    for tablet in tablets.values():
        await tablet.connect()
    assert (await tablets[A].push(0, {"voice": {"wake_sensitivity": 0.4}}))["success"]
    await tablets[B].event()

    stale = await tablets[B].push(0, {"voice": {"wake_sensitivity": 0.9}})
    assert not stale["success"] and stale["error"]["code"] == "profile_conflict"
    assert "revision 1" in stale["error"]["message"]
    ahead = await tablets[B].push(5, {"voice": {"wake_sensitivity": 0.9}})
    assert ahead["error"]["code"] == "profile_conflict"
    current = (await tablets[B].get())["result"]
    assert current["revision"] == 1 and current["groups"]["voice"] == {"wake_sensitivity": 0.4}

    fresh = await tablets[B].push(1, {"voice": {"wake_sensitivity": 0.9}})
    assert fresh["success"] and fresh["result"]["revision"] == 2


async def test_a_retried_push_with_the_same_request_id_is_idempotent(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    tablet = tablets[A]
    await tablet.connect()
    request = str(uuid4())
    fields = {"request_id": request, "base_revision": 0, "groups": {"voice": {"wake_sensitivity": 0.4}}}
    first = await tablet.call("voice_profile_update", **fields)
    second = await tablet.call("voice_profile_update", **fields)
    assert first["success"] and second["success"]
    assert first["result"] == second["result"] and second["result"]["revision"] == 1
    assert (await tablet.get())["result"]["revision"] == 1


async def test_per_device_keys_are_dropped_and_never_reach_other_tablets(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await _follow(hass, entries[B])
    for tablet in tablets.values():
        await tablet.connect()
    response = await tablets[A].push(0, {
        "voice": {"wake_sensitivity": 0.4, "arbitration_offset_db": 12, "microphone_gain": 3},
        "appearance": {"device_name": "Kitchen", "area": "kitchen", "theme": "dark"},
    })
    assert response["result"]["dropped"] == [
        "appearance.area", "appearance.device_name", "voice.arbitration_offset_db", "voice.microphone_gain",
    ]
    event = await tablets[B].event()
    assert event["groups"]["voice"] == {"wake_sensitivity": 0.4}
    assert event["groups"]["appearance"] == {"theme": "dark"}
    stored = (await tablets[B].get())["result"]
    assert "arbitration_offset_db" not in str(stored["groups"]) and "microphone_gain" not in str(stored["groups"])
    assert stored["groups"]["appearance"] == {"theme": "dark"}


async def test_invalid_pushes_are_rejected_whole(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    tablet = tablets[A]
    await tablet.connect()
    for groups in ({"voice": {"wake_sensitivity": 4}}, {"nope": {"a": 1}}, {"voice": {"Bad Key": 1}}):
        response = await tablet.push(0, groups)
        assert response["error"]["code"] == "invalid_profile", groups
    assert (await tablet.get())["result"]["revision"] == 0


async def test_unknown_keys_round_trip_between_apps_of_different_ages(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await _follow(hass, entries[B])
    for tablet in tablets.values():
        await tablet.connect()
    future = {"wake_future_knob": {"curve": [0.1, 0.9], "mode": "turbo"}}
    assert (await tablets[A].push(0, {"voice": future}))["success"]
    assert (await tablets[B].event())["groups"]["voice"] == future

    # An older app changes something else; the newer app's key must survive.
    assert (await tablets[B].push(1, {"voice": {"wake_sensitivity": 0.3}}))["success"]
    event = await tablets[A].event()
    assert event["groups"]["voice"] == {**future, "wake_sensitivity": 0.3}
    assert (await tablets[B].get())["result"]["groups"]["voice"] == {**future, "wake_sensitivity": 0.3}


async def test_get_returns_the_whole_profile_with_metadata(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A], True, ["voice"])
    await tablets[A].connect()
    await tablets[A].push(0, {"voice": {"wake_sensitivity": 0.4}})
    result = (await tablets[A].get())["result"]
    assert result["schema_version"] == 1 and result["revision"] == 1
    assert result["updated_by"]["install_id"] == A and result["updated_at"]
    assert set(result["groups"]) == set(GROUPS)
    assert result["following"] == {"enabled": True, "groups": ["voice"]}
    assert "arbitration_offset_db" in result["excluded_keys"]


async def test_following_catches_a_tablet_up_and_choosing_groups_filters_it(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await tablets[A].connect()
    await tablets[B].connect()
    await tablets[A].push(0, {"voice": {"wake_sensitivity": 0.4}, "timers": {"timer_scale": 1.2}})

    manager = await async_get_profile_manager(hass)
    await manager.async_set_follow(entries[B], True, ["timers"])
    event = await tablets[B].event()
    assert event["reason"] == "follow_changed" and event["revision"] == 1
    assert event["groups"] == {"timers": {"timer_scale": 1.2}}
    assert event["following"] == {"enabled": True, "groups": ["timers"]}

    await manager.async_set_follow(entries[B], False, ["timers"])
    off = await tablets[B].event()
    assert off["groups"] == {} and off["following"]["enabled"] is False


async def test_the_switches_drive_the_same_opt_in(hass, fleet) -> None:
    entries, tablets = fleet
    await tablets[A].connect()
    follow = next(s for s in hass.states.async_entity_ids("switch") if s.endswith("follow_fleet_voice_profile"))
    await hass.services.async_call("switch", "turn_on", {"entity_id": follow}, blocking=True)
    assert hass.states.get(follow).state == "on"
    assert entries[A].options[OPTION_FOLLOW] is True
    assert (await tablets[A].event())["following"]["enabled"] is True

    group = next(s for s in hass.states.async_entity_ids("switch") if s.endswith("follow_fleet_chimes"))
    await hass.services.async_call("switch", "turn_off", {"entity_id": group}, blocking=True)
    assert hass.states.get(group).state == "off"
    assert "chimes" not in entries[A].options[OPTION_GROUPS]
    assert (await tablets[A].event())["following"]["groups"] == [g for g in GROUPS if g != "chimes"]


async def test_fleet_sensor_shows_revision_and_last_writer(hass, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await tablets[A].connect()
    await tablets[A].push(0, {"voice": {"wake_sensitivity": 0.4}})
    await hass.async_block_till_done()
    sensor = next(s for s in hass.states.async_entity_ids("sensor") if s.endswith("fleet_voice_profile_revision"))
    state = hass.states.get(sensor)
    assert state.state == "1"
    assert state.attributes["updated_by"]["name"] == "Kitchen" and state.attributes["app_supported"] is True


async def test_tablet_options_flow_sets_follow_and_groups(hass, fleet) -> None:
    entries, tablets = fleet
    await tablets[B].connect()
    result = await hass.config_entries.options.async_init(entries[B].entry_id)
    assert result["type"] == "form" and result["step_id"] == "tablet"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPTION_FOLLOW: True, OPTION_GROUPS: ["voice", "appearance"]}
    )
    assert result["type"] == "create_entry"
    assert entries[B].options == {OPTION_FOLLOW: True, OPTION_GROUPS: ["voice", "appearance"]}
    assert (await tablets[B].event())["following"] == {"enabled": True, "groups": ["voice", "appearance"]}


# -- permissions --------------------------------------------------------------
async def test_an_unpaired_non_admin_connection_cannot_use_tablet_messages(
    hass, hass_ws_client, hass_read_only_access_token, fleet
) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[A])
    await tablets[A].connect()
    reader = Tablet(await hass_ws_client(hass, hass_read_only_access_token), A, "Intruder")
    reader.epoch = tablets[A].epoch  # even a leaked epoch must not help
    for command, fields in (
        ("voice_profile_get", {}),
        ("voice_profile_update", {"request_id": str(uuid4()), "base_revision": 0, "groups": {"voice": {"wake_sensitivity": 1}}}),
    ):
        response = await reader.call(command, **fields)
        assert not response["success"] and response["error"]["code"] == "disconnected"
    assert (await tablets[A].get())["result"]["revision"] == 0


async def test_non_admins_cannot_pair_a_tablet_or_use_the_admin_profile_api(
    hass, hass_ws_client, hass_read_only_access_token, fleet
) -> None:
    reader = await hass_ws_client(hass, hass_read_only_access_token)
    await reader.send_json_auto_id(
        {**deepcopy(CONNECT), "protocol": 2, "capabilities": {**CONNECT["capabilities"], "voice_profile": True}}
    )
    assert (await reader.receive_json())["error"]["code"] == "unauthorized"
    await reader.send_json_auto_id({"type": "tacktab/voice_profile/get"})
    assert (await reader.receive_json())["error"]["code"] == "unauthorized"
    await reader.send_json_auto_id({
        "type": "tacktab/voice_profile/update", "base_revision": 0, "groups": {"voice": {"wake_sensitivity": 0.1}},
    })
    assert (await reader.receive_json())["error"]["code"] == "unauthorized"
    assert (await async_get_profile_manager(hass)).doc["revision"] == 0


async def test_an_administrator_edits_the_profile_with_the_same_revision_rules(hass, hass_ws_client, fleet) -> None:
    entries, tablets = fleet
    await _follow(hass, entries[B])
    await tablets[B].connect()
    admin = await hass_ws_client(hass)

    await admin.send_json_auto_id({
        "type": "tacktab/voice_profile/update", "base_revision": 0, "request_id": "r1",
        "groups": {"voice": {"wake_future_knob": 1, "wake_sensitivity": 0.5, "arbitration_offset_db": 3}},
    })
    response = await admin.receive_json()
    assert response["success"] and response["result"]["revision"] == 1
    assert response["result"]["dropped"] == ["voice.arbitration_offset_db"]
    event = await tablets[B].event()
    assert event["updated_by"]["type"] == "user" and event["updated_by"]["user_id"]

    await admin.send_json_auto_id({"type": "tacktab/voice_profile/update", "base_revision": 0, "groups": {"timers": {"show_timers": True}}})
    assert (await admin.receive_json())["error"]["code"] == "profile_conflict"

    await admin.send_json_auto_id({
        "type": "tacktab/voice_profile/update", "base_revision": 1, "groups": {}, "remove": {"voice": ["wake_future_knob"]},
    })
    assert (await admin.receive_json())["result"]["revision"] == 2
    await admin.send_json_auto_id({"type": "tacktab/voice_profile/get"})
    got = (await admin.receive_json())["result"]
    assert got["groups"]["voice"] == {"wake_sensitivity": 0.5}
    assert {d["install_id"] for d in got["devices"]} == {A, B}
    assert next(d for d in got["devices"] if d["install_id"] == B)["negotiated"] is True
