"""Shared fixtures."""

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT

HOST = "192.168.1.40"
PORT = 7979
BASE = f"http://{HOST}:{PORT}/api"
STATUS = {
    "app": "Tacktab", "version": "1.0.1", "id": "abc123", "name": "Kitchen tablet",
    "url": "http://ha.local:8123/kitchen", "screen": "on", "brightness": 60,
    "battery": 88, "charging": True, "model": "samsung SM-X200",
}
ENTRY_DATA = {CONF_HOST: HOST, CONF_PORT: PORT, CONF_PASSWORD: "s3cret-pw"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def find_tablet():
    """No tablet announces itself unless a test says so."""
    with patch("custom_components.tacktab.coordinator.async_find_tablet", AsyncMock(return_value=None)) as mock:
        yield mock
