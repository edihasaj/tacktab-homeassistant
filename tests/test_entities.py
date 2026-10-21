"""Entity tests against a mocked tablet."""

import traceback
from urllib.parse import quote

import aiohttp
import pytest

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.api import TacktabClient, TacktabError
from custom_components.tacktab.const import DOMAIN

from .conftest import BASE, ENTRY_DATA, HOST, PORT, STATUS


async def _setup(hass: HomeAssistant, aioclient_mock, status: dict | None = None) -> MockConfigEntry:
    aioclient_mock.get(f"{BASE}/status", json=status or STATUS)
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


async def test_display_off_reports_off_and_its_mode(hass: HomeAssistant, aioclient_mock) -> None:
    """Tacktab 1.0.2+ switches the display off ("off") when it may, and says how."""
    await _setup(hass, aioclient_mock, {**STATUS, "screen_off": "display"})
    aioclient_mock.post(f"{BASE}/sleep", json={**STATUS, "screen": "off", "screen_off": "display"})
    await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: "switch.kitchen_tablet_screen"}, blocking=True)
    state = hass.states.get("switch.kitchen_tablet_screen")
    assert state.state == STATE_OFF
    assert state.attributes["off_mode"] == "display"


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


async def test_old_or_ios_tablet_without_announcement_capability_has_no_notifier(
    hass: HomeAssistant, aioclient_mock
) -> None:
    await _setup(hass, aioclient_mock)
    assert hass.states.get("notify.kitchen_tablet_announcements") is None


async def test_announcement_entity_appears_after_android_upgrade_and_tracks_opt_in(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await _setup(hass, aioclient_mock)

    # The capability appears in status only after the tablet is upgraded. A
    # false value means Android supports it but its owner has not enabled it.
    entry.runtime_data.async_set_updated_data({**STATUS, "announcements_enabled": False})
    await hass.async_block_till_done()
    assert hass.states.get("notify.kitchen_tablet_announcements").state == "unavailable"

    entry.runtime_data.async_set_updated_data({**STATUS, "announcements_enabled": True})
    aioclient_mock.post(f"{BASE}/announce?text=The%20laundry%20is%20ready.", json={**STATUS, "announcements_enabled": True})
    await hass.services.async_call(
        "notify",
        "send_message",
        {ATTR_ENTITY_ID: "notify.kitchen_tablet_announcements", "message": "The laundry is ready."},
        blocking=True,
    )

    sent = [call[1] for call in aioclient_mock.mock_calls if call[0] == "POST"][-1]
    assert sent.raw_query_string == "text=The%20laundry%20is%20ready."


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("   ", "must not be empty"),
        ("a" * 501, "at most 500"),
        ("do not\x00speak", "ISO control"),
        ("do not\x9fspeak", "ISO control"),
        ("\ntrimmed text", "ISO control"),
    ],
)
async def test_announcement_validation_rejects_unsafe_text(
    hass: HomeAssistant, aioclient_mock, message: str, error: str
) -> None:
    client = TacktabClient(async_get_clientsession(hass), HOST, PORT, ENTRY_DATA["password"])
    with pytest.raises(TacktabError, match=error):
        await client.announce(message)
    assert not aioclient_mock.mock_calls


async def test_announcement_errors_do_not_expose_message_or_password(hass: HomeAssistant, aioclient_mock) -> None:
    secret_message = "tell nobody this private message"
    secret_password = "private-api-password"
    aioclient_mock.post(
        f"{BASE}/announce?text={quote(secret_message, safe='')}",
        exc=aiohttp.ClientConnectionError(f"failed for text={secret_message}; password={secret_password}"),
    )
    client = TacktabClient(async_get_clientsession(hass), HOST, PORT, secret_password)

    with pytest.raises(TacktabError) as error:
        await client.announce(secret_message)

    formatted_error = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert str(error.value) == "request failed (ClientConnectionError)"
    assert secret_message not in formatted_error
    assert secret_password not in formatted_error
    assert f"text={quote(secret_message, safe='')}" not in formatted_error


async def test_api_errors_do_not_expose_page_url_query_parameters(hass: HomeAssistant, aioclient_mock) -> None:
    private_page = "https://dashboard.example/?token=private-page-token"
    encoded_page = quote(private_page, safe="")
    aioclient_mock.post(f"{BASE}/load?url={encoded_page}", status=500)
    client = TacktabClient(async_get_clientsession(hass), HOST, PORT, ENTRY_DATA["password"])

    with pytest.raises(TacktabError) as error:
        await client.load(private_page)

    formatted_error = "".join(traceback.format_exception(error.type, error.value, error.tb))
    assert str(error.value) == "request returned HTTP 500"
    assert private_page not in formatted_error
    assert encoded_page not in formatted_error
