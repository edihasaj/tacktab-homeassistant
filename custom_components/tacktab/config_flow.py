"""Config flow for Tacktab: manual setup, zeroconf discovery and re-auth."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import TacktabAuthError, TacktabClient, TacktabError, TacktabRateLimitError
from .const import DEFAULT_PORT, DOMAIN


class TacktabConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Tacktab tablet."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port = DEFAULT_PORT
        self._name: str | None = None

    async def _check(self, host: str, port: int, password: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return (status, error key)."""
        client = TacktabClient(async_get_clientsession(self.hass), host, port, password)
        try:
            return await client.status(), None
        except TacktabAuthError:
            return None, "invalid_auth"
        except TacktabRateLimitError:
            return None, "too_many_attempts"
        except TacktabError:
            return None, "cannot_connect"

    @staticmethod
    def _unique_id(status: dict[str, Any], host: str, port: int) -> str:
        # Tacktab 1.0.1+ reports a stable install id; 1.0.0 falls back to the address.
        return status.get("id") or f"{host}:{port}"

    @staticmethod
    def _title(status: dict[str, Any]) -> str:
        return status.get("name") or f"Tacktab {status.get('model', '')}".strip()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            status, error = await self._check(host, port, user_input[CONF_PASSWORD])
            if status is not None:
                await self.async_set_unique_id(self._unique_id(status, host, port))
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self.async_create_entry(title=self._title(status), data={**user_input, CONF_HOST: host})
            errors["base"] = error or "unknown"
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): int,
            vol.Required(CONF_PASSWORD): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A tablet with the remote control API on advertised itself on the network."""
        device_id = discovery_info.properties.get("id")
        if not device_id:
            return self.async_abort(reason="not_supported")
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        self._name = discovery_info.properties.get("name") or discovery_info.name.split(".")[0]
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._host is not None
        if user_input is not None:
            status, error = await self._check(self._host, self._port, user_input[CONF_PASSWORD])
            if status is not None:
                return self.async_create_entry(
                    title=self._title(status) if status.get("name") else self._name or "Tacktab",
                    data={CONF_HOST: self._host, CONF_PORT: self._port, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"name": self._name or "Tacktab", "host": self._host},
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            _, error = await self._check(entry.data[CONF_HOST], entry.data[CONF_PORT], user_input[CONF_PASSWORD])
            if error is None:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}), errors=errors
        )
