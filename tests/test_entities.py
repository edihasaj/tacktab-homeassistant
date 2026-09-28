"""Entity tests against a mocked tablet."""

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN

from .conftest import BASE, ENTRY_DATA, STATUS


async def _setup(hass: HomeAssistant, aioclient_mock) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc123", title="Kitchen tablet", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_states(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    assert hass.states.get("switch.kitchen_tablet_screen").state == STATE_ON
    assert hass.states.get("number.kitchen_tablet_brightness").state == "60"
    assert hass.states.get("text.kitchen_tablet_page").state == "http://ha.local:8123/kitchen"
    assert hass.states.get("sensor.kitchen_tablet_battery").state == "88"
    assert hass.states.get("binary_sensor.kitchen_tablet_charging").state == STATE_ON
    assert hass.states.get("sensor.kitchen_tablet_app_version").state == "1.0.1"


async def test_screen_off_sleeps_and_uses_the_answer(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{BASE}/sleep", json={**STATUS, "screen": "sleep"})
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.kitchen_tablet_screen"}, blocking=True)
    assert hass.states.get("switch.kitchen_tablet_screen").state == STATE_OFF


async def test_commands_hit_the_right_endpoints(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.post(f"{BASE}/wake", json=STATUS)
    aioclient_mock.post(f"{BASE}/brightness?percent=25", json={**STATUS, "brightness": 25})
    aioclient_mock.post(f"{BASE}/reload", json=STATUS)
    aioclient_mock.post(f"{BASE}/load?url=http%3A%2F%2Fha.local%3A8123%2Fnight", json={**STATUS, "url": "http://ha.local:8123/night"})

    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: "switch.kitchen_tablet_screen"}, blocking=True)
    await hass.services.async_call("number", "set_value", {ATTR_ENTITY_ID: "number.kitchen_tablet_brightness", "value": 25}, blocking=True)
    assert hass.states.get("number.kitchen_tablet_brightness").state == "25"
    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: "button.kitchen_tablet_reload_page"}, blocking=True)
    await hass.services.async_call("text", "set_value", {ATTR_ENTITY_ID: "text.kitchen_tablet_page", "value": "http://ha.local:8123/night"}, blocking=True)

    assert hass.states.get("text.kitchen_tablet_page").state == "http://ha.local:8123/night"
    posted = [str(call[1]) for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posted == [f"{BASE}/wake", f"{BASE}/brightness?percent=25", f"{BASE}/reload",
                      f"{BASE}/load?url=http%3A%2F%2Fha.local%3A8123%2Fnight"]


async def test_wrong_password_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", status=401)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc123", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_page_url_with_its_own_query_stays_one_value(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    page = "http://ha.local:8123/lovelace/0?kiosk&theme=dark"
    encoded = "http%3A%2F%2Fha.local%3A8123%2Flovelace%2F0%3Fkiosk%26theme%3Ddark"
    aioclient_mock.post(f"{BASE}/load?url={encoded}", json={**STATUS, "url": page})
    await hass.services.async_call("text", "set_value", {ATTR_ENTITY_ID: "text.kitchen_tablet_page", "value": page}, blocking=True)
    sent = [call[1] for call in aioclient_mock.mock_calls if call[0] == "POST"][-1]
    assert sent.raw_query_string == f"url={encoded}"
    assert sent.query["url"] == page
