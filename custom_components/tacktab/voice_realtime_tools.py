"""Bind provider function calls to Home Assistant's contextual LLM API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class BoundTools:
    api: Any
    specs: list[Any]
    aliases: dict[str, str]
    prompt: str

    async def call(self, provider_name: str, arguments: dict[str, Any], call_id: str) -> tuple[dict[str, Any], bool]:
        from homeassistant.helpers import llm
        original = self.aliases.get(provider_name)
        if original is None:
            return {"error": "Unknown tool"}, True
        try:
            result = await self.api.async_call_tool(llm.ToolInput(tool_name=original, tool_args=arguments, id=call_id))
        except Exception:
            return {"error": "Tool failed"}, True
        data = getattr(result, "data", None)
        error = getattr(result, "error", None)
        if error:
            # HA tool handlers put safe, user-actionable details in ``data``.
            # Preserve that structured value without reflecting exception text.
            return data if isinstance(data, dict) else {"error": "Tool failed"}, True
        return data if isinstance(data, dict) else {"result": data}, False


def _alias(name: str, used: set[str]) -> str:
    """Provider-safe, deterministic and reversible names for HA tools."""
    base = "".join(char if char.isalnum() or char == "_" else "_" for char in name)[:64] or "tool"
    candidate, suffix = base, 2
    while candidate in used:
        candidate = f"{base[:58]}_{suffix}"; suffix += 1
    used.add(candidate)
    return candidate


async def async_bind_tools(hass: Any, connection: Any, device_id: str, api_ids: tuple[str, ...], language: str | None) -> BoundTools:
    """Resolve only HA-provided tools for this paired device and user context."""
    from homeassistant.helpers import llm
    from homeassistant.setup import async_setup_component
    if not await async_setup_component(hass, "llm", {}):
        raise RuntimeError("Home Assistant LLM component is unavailable")
    context_factory = getattr(connection, "context", None)
    if context_factory is None:
        raise RuntimeError("Paired websocket context is unavailable")
    context = llm.LLMContext(platform="tacktab", context=context_factory({}), language=language, assistant="conversation", device_id=device_id)
    api = await llm.async_get_api(hass, list(api_ids), context)
    from .voice_realtime_providers import ToolSpec
    used: set[str] = set(); aliases: dict[str, str] = {}; specs: list[ToolSpec] = []
    import probatio
    serializer = getattr(api, "custom_serializer", None)
    for tool in api.tools:
        original = tool.name; alias = _alias(original, used)
        try:
            parameters = probatio.to_openapi(tool.parameters, custom_serializer=serializer)
        except (TypeError, ValueError):
            continue
        aliases[alias] = original
        specs.append(ToolSpec(alias, tool.description, parameters, original))
    return BoundTools(api, specs, aliases, str(getattr(api, "api_prompt", "")))
