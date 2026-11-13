"""Conversation memory: how long a tablet keeps Assist context between activations."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import chat_session

from custom_components.tacktab import voice_migration_ws as ws
from custom_components.tacktab.voice_conversation import (
    CORE_RETENTION, DEFAULT_MEMORY, MEMORY_SECONDS, ConversationMemory, TacktabConversationMemorySelect, retention_seconds,
)

COMPONENT = Path(__file__).parent.parent / "custom_components" / "tacktab"


@pytest.fixture(autouse=True)
def _stop_keepalives(hass: HomeAssistant):
    """A keepalive is a real HA timer; none may outlive its test."""
    clocks: list["_Clock"] = []
    _Clock.created = clocks
    yield
    for clock in clocks:
        clock.memory.cancel()
    cleanup = hass.data.get(chat_session.DATA_CHAT_SESSION_CLEANUP)
    if cleanup is not None and cleanup.unsub:
        cleanup.unsub()
        cleanup.unsub = None


class _Clock:
    created: list["_Clock"] = []

    def __init__(self, hass: HomeAssistant, option: str) -> None:
        self.now = 1000.0
        self.satellite = SimpleNamespace(_conversation_id="01CONVERSATION")
        self.device = SimpleNamespace(conversation_memory=option)
        self.memory = ConversationMemory(hass, lambda: retention_seconds(self.device))
        self.memory._now = lambda: self.now  # type: ignore[method-assign]
        self.created.append(self)

    def run(self, *, idle_for: float = 0) -> str | None:
        """One activation after `idle_for` seconds of silence; returns the conversation id it used."""
        self.now += idle_for
        self.memory.before_run(self.satellite)
        used = self.satellite._conversation_id
        self.memory.after_run(self.satellite)
        return used


def test_default_is_homeassistants_own_five_minute_retention() -> None:
    assert DEFAULT_MEMORY == "5_minutes" and MEMORY_SECONDS[DEFAULT_MEMORY] == CORE_RETENTION == 300


async def test_isolated_starts_a_new_conversation_every_activation(hass: HomeAssistant) -> None:
    clock = _Clock(hass, "isolated")
    assert clock.run() is None and clock.run(idle_for=1) is None


@pytest.mark.parametrize(("option", "inside", "outside"), [
    ("1_minute", 59, 61), ("5_minutes", 299, 301), ("15_minutes", 899, 901),
])
async def test_context_is_kept_until_the_retention_runs_out(hass: HomeAssistant, option: str, inside: float, outside: float) -> None:
    clock = _Clock(hass, option)
    clock.run()
    clock.satellite._conversation_id = "01CONVERSATION"
    assert clock.run(idle_for=inside) == "01CONVERSATION"
    assert clock.run(idle_for=outside) is None


async def test_until_restart_never_expires(hass: HomeAssistant) -> None:
    clock = _Clock(hass, "until_restart")
    clock.run()
    assert clock.run(idle_for=10 * 86400) == "01CONVERSATION"


async def test_long_retention_refreshes_the_chat_session_past_homeassistants_five_minutes(hass: HomeAssistant) -> None:
    with chat_session.async_get_chat_session(hass) as session:
        conversation_id = session.conversation_id
    clock = _Clock(hass, "15_minutes")
    clock.satellite._conversation_id = conversation_id
    clock.memory.after_run(clock.satellite)
    assert clock.memory._keepalive is not None, "a keepalive is needed beyond HA's own five minutes"
    stale = hass.data[chat_session.DATA_CHAT_SESSION][conversation_id].last_updated
    clock.now += 240
    clock.memory.cancel()  # the timer is firing now; stand in for HA consuming it
    clock.memory._refresh(clock.satellite)
    assert hass.data[chat_session.DATA_CHAT_SESSION][conversation_id].last_updated > stale
    assert clock.satellite._conversation_id == conversation_id and clock.memory._keepalive is not None
    clock.now += 900  # retention over: the keepalive stops and the conversation is dropped
    clock.memory.cancel()
    clock.memory._refresh(clock.satellite)
    assert clock.satellite._conversation_id is None and clock.memory._keepalive is None


async def test_short_retention_needs_no_keepalive(hass: HomeAssistant) -> None:
    clock = _Clock(hass, "5_minutes")
    clock.run()
    assert clock.memory._keepalive is None


async def test_changing_the_option_applies_immediately(hass: HomeAssistant) -> None:
    clock = _Clock(hass, "15_minutes")
    clock.run()
    clock.device.conversation_memory = "isolated"
    clock.memory.apply_setting(clock.satellite)
    assert clock.satellite._conversation_id is None and clock.memory._keepalive is None


async def test_select_stores_the_option_and_rejects_unknown_ones(hass: HomeAssistant) -> None:
    device = SimpleNamespace(install_id="abc", satellites={})
    select = TacktabConversationMemorySelect(device)
    select.hass = hass
    select.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    assert select.current_option == DEFAULT_MEMORY and select.options == list(MEMORY_SECONDS)
    await select.async_select_option("1_minute")
    assert device.conversation_memory == "1_minute"
    with pytest.raises(ValueError):
        await select.async_select_option("forever")


@pytest.mark.parametrize("path", ["strings.json", "translations/en.json"])
def test_new_selects_have_tacktab_translations(path: str) -> None:
    selects = json.loads((COMPONENT / path).read_text())["entity"]["select"]
    assert selects["vad_sensitivity"]["name"] == "Finished speaking"
    assert set(selects["vad_sensitivity"]["state"]) == {"default", "relaxed", "aggressive"}
    assert selects["conversation_memory"]["name"] == "Conversation memory"
    assert set(selects["conversation_memory"]["state"]) == set(MEMORY_SECONDS)


async def test_migration_apply_writes_conversation_memory_with_finished_speaking(hass: HomeAssistant) -> None:
    written: list = []

    class _Select:
        async def async_select_option(self, option: str) -> None:
            written.append(option)

    device = SimpleNamespace(select_entities={}, vad_select=_Select(), memory_select=_Select())
    settings = {"finished_speaking": "relaxed", "conversation_memory": "15_minutes"}
    assert await ws._apply_home_assistant_side(hass, device, settings, {"conversation"}) == ["finished_speaking", "conversation_memory"]
    assert written == ["relaxed", "15_minutes"]
    written.clear()
    assert await ws._apply_home_assistant_side(hass, device, settings, {"timers"}) == [] and written == []
