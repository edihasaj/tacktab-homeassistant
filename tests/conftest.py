"""Shared fixtures."""

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
