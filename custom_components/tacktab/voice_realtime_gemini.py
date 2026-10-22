"""Gemini Live WebSocket adapter; API key remains only in HA's transport URL."""
from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote
from aiohttp import ClientWSTimeout

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .voice_realtime_providers import ProviderConfig, ProviderEvent, RealtimeProviderError, ToolSpec, json_message, safe_provider_error


class GeminiRealtimeProvider:
    def __init__(self, hass: Any) -> None:
        self.hass, self.ws, self._handle = hass, None, None
        self._transcripts: dict[str, str] = {}

    async def connect(self, config: ProviderConfig, tools: list[ToolSpec], instructions: str, resume: dict[str, Any] | None = None) -> None:
        url = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=" + quote(config.api_key, safe="")
        declarations = [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]
        setup: dict[str, Any] = {"model": config.model, "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": config.voice}}}}, "systemInstruction": {"parts": [{"text": instructions}]}, "inputAudioTranscription": {}, "outputAudioTranscription": {}, "realtimeInputConfig": {"activityHandling": "START_OF_ACTIVITY_INTERRUPTS"}, "tools": [{"functionDeclarations": declarations}], "sessionResumption": {}, "contextWindowCompression": {"slidingWindow": {}}}
        if resume and resume.get("handle"): setup["sessionResumption"] = {"handle": resume["handle"]}
        try:
            self.ws = await async_get_clientsession(self.hass).ws_connect(url, timeout=ClientWSTimeout(ws_receive=60))
            await self._send({"setup": setup})
        except Exception as exc: raise safe_provider_error(exc) from None

    async def _send(self, data: dict[str, Any]) -> None:
        if self.ws is None: raise RealtimeProviderError("Realtime provider is closed")
        await self.ws.send_json(data)
    async def send_audio(self, pcm16le_16000: bytes) -> None: await self._send({"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm16le_16000).decode()}}})
    async def end_audio(self) -> None: await self._send({"realtimeInput": {"audioStreamEnd": True}})
    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any], is_error: bool) -> None: await self._send({"toolResponse": {"functionResponses": [{"id": call_id, "name": name, "response": {"result": result, "error": is_error}}]}})
    async def continue_after_tools(self) -> None: return
    async def interrupt(self, item_id: str | None, played_frames_24000: int) -> None:
        await self._send({"clientContent": {"turns": [], "turnComplete": False}})

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self.ws is None: return
        async for message in self.ws:
            data = json_message(message.data)
            if data is None: continue
            if "error" in data:
                yield ProviderEvent("error", detail="Gemini Live rejected realtime input")
                continue
            if "setupComplete" in data: yield ProviderEvent("ready"); continue
            update = data.get("sessionResumptionUpdate", {})
            if isinstance(update, dict) and update.get("resumable") is True and isinstance(update.get("newHandle"), str): self._handle = update["newHandle"]; yield ProviderEvent("resume", handle=self._handle)
            activity = data.get("voiceActivity")
            if isinstance(activity, dict):
                if activity.get("type") == "ACTIVITY_START": yield ProviderEvent("speech_started")
                elif activity.get("type") == "ACTIVITY_END": yield ProviderEvent("speech_stopped")
            content = data.get("serverContent", {})
            if isinstance(content, dict):
                if content.get("interrupted"): yield ProviderEvent("interrupted")
                for part in content.get("modelTurn", {}).get("parts", []):
                    inline = part.get("inlineData", {}) if isinstance(part, dict) else {}
                    if inline.get("mimeType", "").startswith("audio/pcm"):
                        try: yield ProviderEvent("audio", audio=base64.b64decode(inline["data"], validate=True))
                        except (KeyError, ValueError): pass
                for field, role in (("inputTranscription", "user"), ("outputTranscription", "assistant")):
                    transcript = content.get(field)
                    if isinstance(transcript, dict):
                        text = str(transcript.get("text", "")); self._transcripts[role] = text
                        yield ProviderEvent("transcript", role=role, text=text, operation="replace", final=False)
                if content.get("turnComplete"):
                    for role, text in tuple(self._transcripts.items()): yield ProviderEvent("transcript", role=role, text=text, operation="replace", final=True)
                    self._transcripts.clear(); yield ProviderEvent("generation_end")
            calls = data.get("toolCall", {}).get("functionCalls", [])
            for call in calls if isinstance(calls, list) else []:
                if isinstance(call, dict) and isinstance(call.get("args"), dict): yield ProviderEvent("tool_call", call_id=call.get("id"), name=call.get("name"), arguments=call["args"])
            cancellation = data.get("toolCallCancellation")
            if isinstance(cancellation, dict) and isinstance(cancellation.get("ids"), list):
                yield ProviderEvent("tool_cancel", cancelled_ids=tuple(str(x) for x in cancellation["ids"]))

    async def close(self) -> dict[str, Any] | None:
        if self.ws is not None: await self.ws.close()
        self.ws = None
        return {"handle": self._handle} if self._handle else None
