"""A tablet whose Wi-Fi address changed is followed through its mDNS announcement."""

from datetime import timedelta

from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN

from .conftest import BASE, ENTRY_DATA, PORT, STATUS

NEW_HOST = "192.168.1.44"
NEW_BASE = f"http://{NEW_HOST}:{PORT}/api"


async def _setup(hass: HomeAssistant, aioclient_mock) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc123", title="Kitchen tablet", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/status", exc=TimeoutError())
    return entry


async def test_moved_tablet_is_followed_and_saved(hass: HomeAssistant, aioclient_mock, find_tablet) -> None:
    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.get(f"{NEW_BASE}/status", json=STATUS)
    find_tablet.return_value = (NEW_HOST, PORT)
    await entry.runtime_data.async_refresh()
    find_tablet.assert_awaited_with(hass, "abc123")
    assert entry.data[CONF_HOST] == NEW_HOST
    assert hass.states.get("switch.kitchen_tablet_screen").state == "on"


async def test_other_device_at_the_announced_address_is_ignored(
    hass: HomeAssistant, aioclient_mock, find_tablet
) -> None:
    entry = await _setup(hass, aioclient_mock)
    aioclient_mock.get(f"{NEW_BASE}/status", json={**STATUS, "id": "someone-else"})
    find_tablet.return_value = (NEW_HOST, PORT)
    await entry.runtime_data.async_refresh()
    assert entry.data[CONF_HOST] == ENTRY_DATA[CONF_HOST]


async def test_lookups_are_throttled(hass: HomeAssistant, aioclient_mock, find_tablet, freezer) -> None:
    entry = await _setup(hass, aioclient_mock)
    coordinator = entry.runtime_data
    for _ in range(3):
        freezer.tick(timedelta(seconds=10))
        await coordinator.async_refresh()
    assert find_tablet.await_count == 1
    freezer.tick(timedelta(seconds=60))
    await coordinator.async_refresh()
    assert find_tablet.await_count == 2
