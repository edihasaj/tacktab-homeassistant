"""Classic OpenAI Realtime GA adapter (no beta/session.start transport)."""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from aiohttp import ClientWSTimeout

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .voice_realtime_providers import (ProviderConfig, ProviderEvent, RealtimeProviderError, StatefulResampler, ToolSpec, json_message, safe_provider_error, tool_payload)


class OpenAIRealtimeProvider:
    input_rate = 24_000

    def __init__(self, hass: Any) -> None:
        self.hass = hass
        self.ws: Any = None
        self.resampler = StatefulResampler()
        self.active_item_id: str | None = None
        self.active_response_id: str | None = None

    async def connect(self, config: ProviderConfig, tools: list[ToolSpec], instructions: str, resume: dict[str, Any] | None = None) -> None:
        url = f"wss://api.openai.com/v1/realtime?model={quote(config.model, safe='')}"
        try:
            self.ws = await async_get_clientsession(self.hass).ws_connect(url, headers={"Authorization": f"Bearer {config.api_key}"}, timeout=ClientWSTimeout(ws_receive=60))
            await self._send({
                "type": "session.update",
                "session": {
                    "type": "realtime", "output_modalities": ["audio"], "instructions": instructions,
                    "audio": {
                        "input": {"format": {"type": "audio/pcm", "rate": 24000}, "transcription": {"model": "gpt-4o-mini-transcribe"}, "turn_detection": {"type": "server_vad", "create_response": True, "interrupt_response": True}},
                        "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": config.voice},
                    },
                    "tools": tool_payload(tools), "tool_choice": "auto",
                },
            })
            for item in (resume or {}).get("history", []):
                role, text = item if isinstance(item, tuple) and len(item) == 2 else (item.get("role"), item.get("text")) if isinstance(item, dict) else (None, None)
                if role == "tool" and isinstance(item, dict) and isinstance(item.get("call_id"), str):
                    import json
                    arguments = item.get("arguments", {})
                    await self._send({"type": "conversation.item.create", "item": {"type": "function_call", "call_id": item["call_id"], "name": item.get("name", "tool"), "arguments": json.dumps(arguments, separators=(",", ":"))}})
                    await self._send({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": item["call_id"], "output": json.dumps({"result": item.get("result", {}), "error": bool(item.get("error", False))}, separators=(",", ":"))}})
                    continue
                if role not in {"user", "assistant"} or not isinstance(text, str): continue
                content_type = "input_text" if role == "user" else "output_text"
                await self._send({"type": "conversation.item.create", "item": {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]}})
        except Exception as exc:
            raise safe_provider_error(exc) from None

    async def _send(self, message: dict[str, Any]) -> None:
        if self.ws is None:
            raise RealtimeProviderError("Realtime provider is closed")
        await self.ws.send_json(message)

    async def send_audio(self, pcm16le_16000: bytes) -> None:
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(self.resampler.convert(pcm16le_16000)).decode()})

    async def end_audio(self) -> None:
        tail = self.resampler.flush()
        if tail:
            await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(tail).decode()})
        await self._send({"type": "input_audio_buffer.commit"})

    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any], is_error: bool) -> None:
        import json
        await self._send({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps({"result": result, "error": is_error}, separators=(",", ":"))}})

    async def continue_after_tools(self) -> None:
        await self._send({"type": "response.create"})

    async def interrupt(self, item_id: str | None, played_frames_24000: int) -> None:
        if self.active_response_id is not None:
            await self._send({"type": "response.cancel", "response_id": self.active_response_id})
        if item_id:
            await self._send({"type": "conversation.item.truncate", "item_id": item_id, "content_index": 0, "audio_end_ms": played_frames_24000 // 24})

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self.ws is None: return
        async for message in self.ws:
            data = json_message(message.data)
            if data is None: continue
            kind = data.get("type")
            if kind == "session.updated": yield ProviderEvent("ready")
            elif kind == "response.created":
                response = data.get("response", {})
                self.active_response_id = response.get("id") if isinstance(response, dict) else data.get("response_id")
            elif kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}: yield ProviderEvent("speech_started" if kind.endswith("started") else "speech_stopped")
            elif kind in {"response.output_audio.delta", "response.audio.delta"}:
                try: audio = base64.b64decode(data["delta"], validate=True)
                except (KeyError, ValueError): continue
                self.active_item_id = data.get("item_id") or self.active_item_id
                yield ProviderEvent("audio", audio=audio, item_id=self.active_item_id)
            elif kind in {"response.output_audio_transcript.delta", "response.output_audio_transcript.done"}:
                done = kind.endswith("done")
                yield ProviderEvent("transcript", role="assistant", text=data.get("delta", data.get("transcript", "")), operation="replace" if done else "append", final=done, item_id=data.get("item_id"))
            elif kind == "conversation.item.input_audio_transcription.completed":
                yield ProviderEvent("transcript", role="user", text=data.get("transcript", ""), operation="replace", final=True, item_id=data.get("item_id"))
            elif kind == "response.function_call_arguments.done":
                try: arguments = __import__("json").loads(data.get("arguments", "{}"))
                except ValueError: yield ProviderEvent("error", detail="Invalid tool arguments"); continue
                if isinstance(arguments, dict): yield ProviderEvent("tool_call", call_id=data.get("call_id"), name=data.get("name"), arguments=arguments)
            elif kind == "response.done":
                self.active_response_id = None
                response = data.get("response", {})
                for item in response.get("output", []) if isinstance(response, dict) else []:
                    if not isinstance(item, dict) or item.get("type") != "function_call": continue
                    try: arguments = __import__("json").loads(item.get("arguments", "{}"))
                    except ValueError: continue
                    if isinstance(arguments, dict): yield ProviderEvent("tool_call", call_id=item.get("call_id"), name=item.get("name"), arguments=arguments)
                yield ProviderEvent("generation_end")
            elif kind == "error":
                error = data.get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
                if code not in {"response_not_found", "response_cancel_not_found", "response_cancel_not_active", "no_active_response"}:
                    yield ProviderEvent("error", detail="Provider rejected realtime input")

    async def close(self) -> dict[str, Any] | None:
        if self.ws is not None: await self.ws.close()
        self.ws = None
        return None
