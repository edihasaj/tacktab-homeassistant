"""Bind provider function calls to Home Assistant's contextual LLM API."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .voice_context import DEVICE_ID_FIELD, DEVICE_NAME_FIELD, async_device_identity, async_room_prompt


@dataclass(slots=True)
class BoundTools:
    api: Any
    specs: list[Any]
    aliases: dict[str, str]
    prompt: str
    room_prompt: str = ""
    device_id: str | None = None
    device_name: str = ""
    # Provider aliases of HA script tools. Only these receive the tablet identity.
    script_tools: frozenset[str] = field(default_factory=frozenset)

    async def call(self, provider_name: str, arguments: dict[str, Any], call_id: str) -> tuple[dict[str, Any], bool]:
        from homeassistant.helpers import llm
        original = self.aliases.get(provider_name)
        if original is None:
            return {"error": "Unknown tool"}, True
        tool_args = dict(arguments)
        if provider_name in self.script_tools:
            # The model cannot choose or override these; the bridge owns them.
            tool_args[DEVICE_ID_FIELD] = self.device_id
            tool_args[DEVICE_NAME_FIELD] = self.device_name
        try:
            result = await self.api.async_call_tool(llm.ToolInput(tool_name=original, tool_args=tool_args, id=call_id))
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


def _hidden_identity_fields(parameters: dict[str, Any]) -> dict[str, Any]:
    """Strip tablet identity fields a script declared from the provider schema."""
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return parameters
    hidden = {DEVICE_ID_FIELD, DEVICE_NAME_FIELD}
    if not hidden & properties.keys():
        return parameters
    result = dict(parameters)
    result["properties"] = {name: value for name, value in properties.items() if name not in hidden}
    if isinstance(required := parameters.get("required"), list):
        result["required"] = [name for name in required if name not in hidden]
        if not result["required"]:
            result.pop("required")
    return result


async def async_bind_tools(
    hass: Any,
    connection: Any,
    device_id: str,
    api_ids: tuple[str, ...],
    language: str | None,
    fallback_name: str = "",
) -> BoundTools:
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
    script_tools: set[str] = set()
    name, _area = async_device_identity(hass, device_id, fallback_name)
    import probatio
    serializer = getattr(api, "custom_serializer", None)
    for tool in api.tools:
        original = tool.name; alias = _alias(original, used)
        try:
            parameters = probatio.to_openapi(tool.parameters, custom_serializer=serializer)
        except (TypeError, ValueError):
            continue
        is_script = getattr(tool, "integration", None) == "script" or original.startswith(("script__", "assist__script__"))
        if is_script:
            parameters = _hidden_identity_fields(parameters)
            script_tools.add(alias)
        aliases[alias] = original
        specs.append(ToolSpec(alias, tool.description, parameters, original))
    return BoundTools(
        api,
        specs,
        aliases,
        str(getattr(api, "api_prompt", "")),
        async_room_prompt(hass, device_id, name),
        device_id,
        name,
        frozenset(script_tools),
    )
