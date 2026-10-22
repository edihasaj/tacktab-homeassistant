"""Strict, secret-safe validation of tacktab/realtime websocket commands."""
from __future__ import annotations

import base64
from typing import Any

MAX_PCM = 32 * 1024
PREFIX = "tacktab/realtime/"


class RealtimeProtocolError(ValueError):
    def __init__(self, code: str = "invalid_format", message: str = "Invalid realtime message") -> None: self.code, self.message = code, message


def _fail() -> None: raise RealtimeProtocolError()
def _keys(message: dict[str, Any], required: set[str], optional: set[str] = set()) -> None:
    if not isinstance(message, dict) or set(message) - required - optional or not required <= set(message): _fail()
def _epoch(message: dict[str, Any]) -> None:
    if not isinstance(message.get("epoch"), str) or not message["epoch"]: _fail()


def parse_command(message: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(message.get("type"), str) or not message["type"].startswith(PREFIX) or not isinstance(message.get("id"), int): _fail()
    command = message["type"][len(PREFIX):]; _epoch(message)
    common = {"id", "type", "epoch"}
    if command == "configure_provider":
        _keys(message, common | {"provider", "key_action"}, {"api_key"})
        if message["provider"] not in {"openai", "xai", "gemini"} or message["key_action"] not in {"keep", "replace", "clear"}: _fail()
        if (message["key_action"] == "replace") != (isinstance(message.get("api_key"), str) and bool(message.get("api_key").strip())): _fail()
    elif command == "configure_slot":
        _keys(message, common | {"slot_id", "expected_config_revision", "config"})
        if message["slot_id"] not in {"primary", "secondary"} or not isinstance(message["expected_config_revision"], int) or not isinstance(message["config"], dict): _fail()
    elif command == "catalog":
        _keys(message, common | {"provider"})
        if message["provider"] not in {"openai", "xai", "gemini"}: _fail()
    elif command == "start":
        _keys(message, common | {"slot_id", "trigger"}, {"grant_id", "wake_word_phrase"})
        if message["slot_id"] not in {"primary", "secondary"} or message["trigger"] not in {"button", "follow_up", "wake_word"}: _fail()
        wake = message["trigger"] == "wake_word"
        if wake != (isinstance(message.get("grant_id"), str) and isinstance(message.get("wake_word_phrase"), str)): _fail()
    elif command == "audio":
        _keys(message, common | {"session_id", "chunk_seq", "audio"})
        if not isinstance(message["session_id"], str) or not isinstance(message["chunk_seq"], int) or message["chunk_seq"] < 0 or not isinstance(message["audio"], str): _fail()
        try: pcm = base64.b64decode(message["audio"], validate=True)
        except ValueError: _fail()
        if not pcm or len(pcm) > MAX_PCM or len(pcm) % 2: _fail()
        message["pcm"] = pcm
    elif command == "audio_end":
        _keys(message, common | {"session_id"})
        if not isinstance(message["session_id"], str): _fail()
    elif command == "stop":
        _keys(message, common | {"session_id"}, {"forget"})
        if not isinstance(message["session_id"], str) or ("forget" in message and not isinstance(message["forget"], bool)): _fail()
    elif command in {"playback", "interrupt"}:
        _keys(message, common | {"session_id", "playback_id", "played_frames"}, {"status"})
        if not all(isinstance(message[x], str) for x in ("session_id", "playback_id")) or not isinstance(message["played_frames"], int) or message["played_frames"] < 0: _fail()
        if command == "playback" and message.get("status") not in {"progress", "finished", "interrupted", "failed"}: _fail()
    else: _fail()
    return message
