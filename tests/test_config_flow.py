"""Config flow tests."""

import asyncio

from ipaddress import ip_address

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN
from custom_components.tacktab.voice_setup import VOICE_BOOTSTRAP_KEY

from .conftest import BASE, ENTRY_DATA, HOST, PORT, STATUS


async def _start_http_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "http"})


async def test_user_flow_creates_entry(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    result = await _start_http_flow(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen tablet"
    assert result["result"].unique_id == "abc123"
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer s3cret-pw"


async def test_user_flow_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", status=401)
    result = await _start_http_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_locked_out_after_wrong_passwords(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", status=429, headers={"Retry-After": "30"})
    result = await _start_http_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "too_many_attempts"}


async def test_user_flow_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", exc=TimeoutError())
    result = await _start_http_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_old_app_without_id_uses_address(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", json={k: v for k, v in STATUS.items() if k not in ("id", "name")})
    result = await _start_http_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["result"].unique_id == f"{HOST}:{PORT}"
    assert result["title"] == "Tacktab samsung SM-X200"


async def test_voice_bridge_mode_creates_free_bootstrap_entry(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "voice_bridge"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Voice satellite bridge"
    assert result["data"] == {VOICE_BOOTSTRAP_KEY: True}
    await hass.async_block_till_done()
    assert result["result"].state is config_entries.ConfigEntryState.LOADED
    assert "tacktab/voice/connect" in hass.data["websocket_api"]
    assert "tacktab/realtime/start" in hass.data["websocket_api"]


async def test_clean_voice_bootstrap_accepts_authenticated_native_pairing(hass: HomeAssistant, hass_ws_client) -> None:
    """A clean install can load the bridge before any Pro HTTP entry exists."""
    assert await async_setup_component(hass, "homeassistant", {})
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": "voice_bridge"})
    await hass.async_block_till_done()
    assert result["result"].state is config_entries.ConfigEntryState.LOADED

    websocket = await hass_ws_client()

    async def receive_result(message_id: int) -> dict:
        while True:
            message = await websocket.receive_json()
            if message.get("id") == message_id and message.get("type") == "result":
                return message

    await websocket.send_json(
        {
            "id": 1,
            "type": "tacktab/voice/connect",
            "protocol": 1,
            "install_id": "00000000-0000-4000-8000-000000000101",
            "name": "Kitchen tablet",
            "platform": "ios",
            "app_version": "2.0",
            "capabilities": {"announcements": True, "start_conversation": True, "timers": True, "local_wake_word": True, "pcm16_16khz_mono": True},
            "status": {"local_enabled": True, "mic_permission": "granted", "app_state": "foreground", "capture_ready": True, "playback_ready": True},
            "slots": [{"slot_id": "primary", "enabled": True, "pipeline_id": None, "wake_word": {"id": "okay", "phrase": "Okay Tacktab"}, "output": "local", "media_player_entity_id": None}],
        }
    )
    response = await receive_result(1)
    assert response["success"] is True
    assert response["result"]["protocol"] == 1

    config = {"slot_id": "primary", "enabled": True, "pipeline_id": None, "wake_word": {"id": "okay", "phrase": "Okay Tacktab"}, "output": "local", "media_player_entity_id": None}
    unrelated = hass.async_create_task(asyncio.Event().wait(), "unrelated_test_task")
    await websocket.send_json({"id": 2, "type": "tacktab/voice/configure", "epoch": response["result"]["epoch"], "expected_config_revision": 0, "slots": [config]})
    immediate = await asyncio.wait_for(receive_result(2), 1)
    assert immediate["success"] is True
    assert not unrelated.done()
    unrelated.cancel()

    await websocket.send_json({"id": 3, "type": "tacktab/voice/configure", "epoch": response["result"]["epoch"], "expected_config_revision": 1, "slots": [config]})
    await websocket.send_json({"id": 4, "type": "tacktab/voice/configure", "epoch": response["result"]["epoch"], "expected_config_revision": 1, "slots": [config]})
    replies = {}
    while len(replies) < 2:
        message = await websocket.receive_json()
        if message.get("id") in {3, 4} and message.get("type") == "result":
            replies[message["id"]] = message
    first, second = replies[3], replies[4]
    outcomes = sorted(result["success"] for result in (first, second))
    assert outcomes == [False, True]
    assert {result["error"]["code"] for result in (first, second) if not result["success"]} == {"config_conflict"}


def _discovery(host: str = HOST) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host), ip_addresses=[ip_address(host)], port=PORT, hostname="tacktab.local.",
        type="_tacktab._tcp.local.", name="Kitchen tablet._tacktab._tcp.local.",
        properties={"id": "abc123", "name": "Kitchen tablet", "version": "1.0.1"},
    )


async def test_zeroconf_flow_asks_only_for_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_discovery()
    )
    assert result["step_id"] == "discovery_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "s3cret-pw"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == ENTRY_DATA


async def test_zeroconf_updates_moved_tablet(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc123", data={**ENTRY_DATA, CONF_HOST: "192.168.1.9"})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_discovery()
    )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == HOST


async def test_reauth_updates_password(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="abc123", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-password"})
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_PORT] == PORT
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
