"""Config flow tests."""

from ipaddress import ip_address

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab.const import DOMAIN

from .conftest import BASE, ENTRY_DATA, HOST, PORT, STATUS


async def test_user_flow_creates_entry(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", json=STATUS)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen tablet"
    assert result["result"].unique_id == "abc123"
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer s3cret-pw"


async def test_user_flow_wrong_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", status=401)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_locked_out_after_wrong_passwords(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", status=429, headers={"Retry-After": "30"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "too_many_attempts"}


async def test_user_flow_unreachable(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_old_app_without_id_uses_address(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{BASE}/status", json={k: v for k, v in STATUS.items() if k not in ("id", "name")})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["result"].unique_id == f"{HOST}:{PORT}"
    assert result["title"] == "Tacktab samsung SM-X200"


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
