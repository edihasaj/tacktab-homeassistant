"""Migration websocket: admin/pairing scope, pipeline normalisation, finished-speaking apply."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.tacktab import voice_migration_ws as ws
from custom_components.tacktab.select import resolve_pipeline_option
from custom_components.tacktab.voice_bridge import VoiceBridgeError

PIPELINES = [SimpleNamespace(id="01HOME", name="Home Assistant"), SimpleNamespace(id="01CLOUD", name="Cloud")]
SETTINGS = {"assistant": "Preferred", "assistant_preferred": True, "finished_speaking": "relaxed"}


class _Connection:
    def __init__(self, *, admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=admin)
        self.results: list = []
        self.errors: list = []

    def send_result(self, _id, result) -> None:
        self.results.append(result)

    def send_error(self, _id, code, message) -> None:
        self.errors.append((code, message))


class _Pipeline:
    def __init__(self) -> None:
        self.selected: list = []

    def async_select_pipeline_id(self, pipeline_id) -> None:
        self.selected.append(pipeline_id)


class _Vad:
    def __init__(self) -> None:
        self.selected: list = []

    async def async_select_option(self, option: str) -> None:
        self.selected.append(option)


def _device(connection, *, busy: str | None = None):
    """`busy` names what keeps the tablet busy: run, lease, realtime or config."""
    return SimpleNamespace(
        session=SimpleNamespace(connection=connection, epoch=7, runs={"r": object()} if busy == "run" else {}, actions={}),
        config_lock=SimpleNamespace(locked=lambda: busy == "config"), media_lease=object() if busy == "lease" else None,
        realtime_active=busy == "realtime", select_entities={"primary": _Pipeline()}, vad_select=_Vad(), memory_select=_Vad(),
    )


def _plan(settings=SETTINGS):
    candidate = {"entity_id": "assist_satellite.kitchen", "settings": dict(settings), "warnings": [], "references": []}
    return {"candidates": [candidate], "references_complete": True, "reference_warnings": [], "timer_event_mapping": {}}


async def _run(handler, hass, connection, device, msg, plan=None):
    bridge = SimpleNamespace(devices={"d": device} if device else {})
    with patch.object(ws, "async_get_bridge", return_value=bridge), patch.object(ws, "async_build_plan", return_value=plan or _plan()):
        handler(hass, connection, msg)  # HA schedules async handlers as a task
        await hass.async_block_till_done()


APPLY = {"id": 3, "type": ws.APPLY_COMMAND, "epoch": 7, "candidate_entity_id": "assist_satellite.kitchen", "carry_over": ["assistant", "conversation"]}


@pytest.mark.parametrize("handler", [ws.handle_migration_plan, ws.handle_migration_apply])
async def test_non_admin_cannot_read_or_apply(hass: HomeAssistant, handler) -> None:
    connection = _Connection(admin=False)
    msg = {"id": 3, "type": "x", "epoch": 7} | ({"candidate_entity_id": "assist_satellite.kitchen", "carry_over": []} if handler is ws.handle_migration_apply else {})
    await _run(handler, hass, connection, _device(connection), msg)
    assert connection.results == [] and connection.errors[0][0] == "unauthorized"


@pytest.mark.parametrize("handler", [ws.handle_migration_plan, ws.handle_migration_apply])
async def test_unpaired_or_stale_epoch_gets_nothing(hass: HomeAssistant, handler) -> None:
    connection = _Connection()
    msg = {"id": 3, "type": "x", "epoch": 7} | ({"candidate_entity_id": "assist_satellite.kitchen", "carry_over": []} if handler is ws.handle_migration_apply else {})
    await _run(handler, hass, connection, None, msg)  # admin socket that never paired
    await _run(handler, hass, connection, _device(connection), {**msg, "epoch": 6})
    assert connection.results == [] and [code for code, _ in connection.errors] == ["disconnected", "disconnected"]


async def test_paired_admin_reads_the_plan(hass: HomeAssistant) -> None:
    connection = _Connection()
    await _run(ws.handle_migration_plan, hass, connection, _device(connection), {"id": 1, "type": ws.PLAN_COMMAND, "epoch": 7})
    assert connection.errors == [] and connection.results[0]["candidates"][0]["entity_id"] == "assist_satellite.kitchen"


async def test_apply_writes_preferred_and_finished_speaking_independently_of_conversation(hass: HomeAssistant) -> None:
    connection = _Connection()
    device = _device(connection)
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        await _run(ws.handle_migration_apply, hass, connection, device, APPLY)
    assert connection.errors == []
    assert device.select_entities["primary"].selected == [None]  # "Preferred" -> follows HA's preferred pipeline
    assert device.vad_select.selected == ["relaxed"]
    assert connection.results[0]["applied"] == ["assistant", "finished_speaking"]
    assert connection.results[0]["candidate_entity_id"] == "assist_satellite.kitchen"


async def test_apply_only_writes_selected_carry_over(hass: HomeAssistant) -> None:
    connection = _Connection()
    device = _device(connection)
    await _run(ws.handle_migration_apply, hass, connection, device, {**APPLY, "carry_over": ["timers"]})
    assert connection.results[0]["applied"] == [] and device.vad_select.selected == []


async def test_apply_a_named_assistant_stores_its_stable_id(hass: HomeAssistant) -> None:
    connection = _Connection()
    device = _device(connection)
    plan = _plan({"assistant": "cloud", "assistant_preferred": False, "finished_speaking": None})
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        await _run(ws.handle_migration_apply, hass, connection, device, APPLY, plan)
    assert device.select_entities["primary"].selected == ["01CLOUD"] and device.vad_select.selected == []


async def test_busy_tablet_changes_nothing_and_says_why(hass: HomeAssistant) -> None:
    connection = _Connection()
    device = _device(connection, busy="run")
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        await _run(ws.handle_migration_apply, hass, connection, device, APPLY)
    assert connection.results == [] and connection.errors == [("busy", "Voice device is busy")]
    assert device.vad_select.selected == [] and device.memory_select.selected == [] and device.select_entities["primary"].selected == []


async def test_unknown_assistant_changes_nothing(hass: HomeAssistant) -> None:
    connection = _Connection()
    device = _device(connection)
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        await _run(ws.handle_migration_apply, hass, connection, device, APPLY, _plan({"assistant": "Gone", "finished_speaking": "relaxed"}))
    assert connection.errors[0][0] == "pipeline_not_found" and device.vad_select.selected == []


async def test_resolver_accepts_every_spelling(hass: HomeAssistant) -> None:
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        assert resolve_pipeline_option(hass, "Preferred") == (None, "preferred")
        assert resolve_pipeline_option(hass, "preferred") == (None, "preferred")
        assert resolve_pipeline_option(hass, "home assistant") == ("01HOME", "Home Assistant")
        assert resolve_pipeline_option(hass, "01CLOUD") == ("01CLOUD", "Cloud")
        with pytest.raises(VoiceBridgeError):
            resolve_pipeline_option(hass, "nope")


async def test_invalid_shapes_are_rejected_before_any_lookup(hass: HomeAssistant) -> None:
    connection = _Connection()
    await _run(ws.handle_migration_apply, hass, connection, _device(connection), {**APPLY, "carry_over": ["everything"]})
    assert connection.errors == [("invalid_format", "Invalid migration request")]


async def test_finished_speaking_select_drives_the_silence_ha_waits_for(hass: HomeAssistant) -> None:
    """The migrated choice must change what HA's Assist satellite base class uses."""
    from homeassistant.components.assist_pipeline.vad import VadSensitivity

    from custom_components.tacktab.assist_satellite import TacktabAssistSatellite
    from custom_components.tacktab.select import TacktabFinishedSpeakingSelect

    device = SimpleNamespace(hass=hass, install_id="abc", vad_entity_id=None)
    select = TacktabFinishedSpeakingSelect(device)
    assert select.options == ["default", "relaxed", "aggressive"] and select.unique_id == "voice:abc-vad_sensitivity"

    satellite = TacktabAssistSatellite.__new__(TacktabAssistSatellite)
    satellite.device, satellite.hass = device, hass
    assert satellite.vad_sensitivity_entity_id is None and satellite._resolve_vad_sensitivity() == VadSensitivity.to_seconds("default")

    device.vad_entity_id = "select.kitchen_finished_speaking"
    assert satellite.vad_sensitivity_entity_id is None  # not registered yet: never name a missing entity
    hass.states.async_set("select.kitchen_finished_speaking", "relaxed")
    assert satellite._resolve_vad_sensitivity() == 1.25
    hass.states.async_set("select.kitchen_finished_speaking", "aggressive")
    assert satellite._resolve_vad_sensitivity() == 0.25


@pytest.mark.parametrize("busy", ["run", "lease", "realtime", "config"])
@pytest.mark.parametrize("carry", [["conversation"], ["timers"], ["assistant", "conversation"]])
async def test_a_busy_tablet_changes_nothing_whatever_is_carried(hass: HomeAssistant, busy: str, carry: list[str]) -> None:
    """A conversation-only migration (or one with no resolvable assistant) must not clear an active conversation."""
    connection = _Connection()
    device = _device(connection, busy=busy)
    plan = _plan({"assistant": None, "assistant_preferred": None, "finished_speaking": "relaxed", "conversation_memory": "isolated"})
    with patch("custom_components.tacktab.select.async_get_pipelines", return_value=PIPELINES):
        await _run(ws.handle_migration_apply, hass, connection, device, {**APPLY, "carry_over": carry}, plan)
    assert connection.results == [] and connection.errors == [("busy", "Voice device is busy")]
    assert device.vad_select.selected == [] and device.memory_select.selected == [] and device.select_entities["primary"].selected == []
