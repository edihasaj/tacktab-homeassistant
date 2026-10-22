"""Frozen wire-contract checks that do not require an Assist-enabled Core."""

from copy import deepcopy

import pytest

from custom_components.tacktab.voice_protocol import VoiceProtocolError, validate_message


CONNECT = {
    "id": 1,
    "type": "tacktab/voice/connect",
    "protocol": 1,
    "install_id": "00000000-0000-4000-8000-000000000001",
    "name": "Kitchen tablet",
    "platform": "ios",
    "app_version": "2.0",
    "capabilities": {
        "announcements": True,
        "start_conversation": True,
        "timers": True,
        "local_wake_word": True,
        "pcm16_16khz_mono": True,
    },
    "status": {
        "local_enabled": True,
        "mic_permission": "granted",
        "app_state": "foreground",
        "capture_ready": True,
        "playback_ready": True,
    },
    "slots": [
        {
            "slot_id": "primary",
            "enabled": True,
            "pipeline_id": None,
            "wake_word": {"id": "okay_tacktab", "phrase": "Okay Tacktab"},
            "output": "local",
            "media_player_entity_id": None,
        }
    ],
}


def test_connect_contract_accepts_primary_slot() -> None:
    assert validate_message(CONNECT)["install_id"] == CONNECT["install_id"]


def test_protocol_two_accepts_remote_speaker_but_v1_rejects_it() -> None:
    remote = deepcopy(CONNECT)
    remote["protocol"] = 2
    remote["slots"][0]["output"] = "ha_media_player"
    remote["slots"][0]["media_player_entity_id"] = "media_player.kitchen"
    assert validate_message(remote)["protocol"] == 2

    remote["protocol"] = 1
    with pytest.raises(VoiceProtocolError, match="Remote speaker output requires voice protocol 2"):
        validate_message(remote)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extra",), True),
        (("capabilities", "extra"), True),
        (("slots", 0, "media_player_entity_id"), "media_player.kitchen"),
        (("slots", 0, "slot_id"), "secondary"),
    ],
)
def test_connect_rejects_unknown_or_invalid_slot_configuration(path: tuple, value: object) -> None:
    message = deepcopy(CONNECT)
    target = message
    for item in path[:-1]:
        target = target[item]
    target[path[-1]] = value
    with pytest.raises(VoiceProtocolError, match="Invalid voice message"):
        validate_message(message)


def test_wake_run_requires_a_matching_grant_pair() -> None:
    message = {
        "id": 4,
        "type": "tacktab/voice/run",
        "epoch": "00000000-0000-4000-8000-000000000002",
        "run_id": "00000000-0000-4000-8000-000000000003",
        "slot_id": "primary",
        "trigger": "wake_word",
        "pipeline_id": None,
        "grant_id": "00000000-0000-4000-8000-000000000004",
    }
    with pytest.raises(VoiceProtocolError):
        validate_message(message)


def test_timer_action_requires_seconds_only_for_time_mutations() -> None:
    message = {
        "id": 5,
        "type": "tacktab/voice/timer_action",
        "epoch": "00000000-0000-4000-8000-000000000002",
        "request_id": "00000000-0000-4000-8000-000000000004",
        "timer_id": "timer-1",
        "action": "pause",
        "seconds": 10,
    }
    with pytest.raises(VoiceProtocolError):
        validate_message(message)
