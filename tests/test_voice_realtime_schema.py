from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


@pytest.fixture(name="validator")
def validator_fixture() -> Draft202012Validator:
    schema = json.loads((Path(__file__).parents[1] / "docs" / "realtime-events.schema.json").read_text())
    return Draft202012Validator(schema)


def event(kind: str, **fields: object) -> dict[str, object]:
    return {"event": "realtime", "epoch": "epoch-1", "seq": 7, "session_id": "session-1", "slot_id": "primary", "kind": kind, **fields}


@pytest.mark.parametrize("payload", [
    event("ready", provider="xai", model="grok-voice-3.0", voice="eve", input_format={"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, output_format={"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1}),
    event("audio", playback_id="playback-1", item_id="item-1", chunk_seq=0, audio="AAE=", frames=1),
    event("transcript", role="assistant", item_id="item-1", text="Done", operation="append", final=True),
    event("tool_state", call_id="call-1", state="completed"),
    event("interrupted", playback_id="playback-1"),
    event("closed", reason="stopped", memory_retained=False, recoverable=False),
])
def test_actual_realtime_event_shapes_validate_with_global_epoch_and_sequence(validator: Draft202012Validator, payload: dict[str, object]) -> None:
    assert list(validator.iter_errors(payload)) == []


@pytest.mark.parametrize("field", ["event", "epoch", "seq", "session_id", "slot_id", "kind"])
def test_missing_base_fields_are_rejected(validator: Draft202012Validator, field: str) -> None:
    payload = event("closed", reason="stopped", memory_retained=False, recoverable=False)
    payload.pop(field)
    assert list(validator.iter_errors(payload))


def test_subtype_rejects_unknown_fields_and_invalid_nested_format(validator: Draft202012Validator) -> None:
    payload = event("ready", provider="xai", model="grok-voice-3.0", voice="eve", input_format={"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, output_format={"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1})
    unknown = copy.deepcopy(payload); unknown["unexpected"] = True
    assert list(validator.iter_errors(unknown))
    malformed = copy.deepcopy(payload); malformed["output_format"]["sample_rate"] = 16_001
    assert list(validator.iter_errors(malformed))


def test_ready_event_rejects_retired_provider(validator: Draft202012Validator) -> None:
    payload = event("ready", provider="openai", model="gpt-realtime", voice="marin", input_format={"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1}, output_format={"codec": "pcm_s16le", "sample_rate": 24000, "channels": 1})
    assert list(validator.iter_errors(payload))
