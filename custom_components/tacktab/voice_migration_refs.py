"""Find automations and scripts that still use Voice Satellite, without changing them.

Discovery prefers what Home Assistant has actually loaded: that covers
`!include` trees, packages, blueprints' inputs and UI-created items.  Only when
an integration is not loaded do we read its YAML file, with tagged nodes kept
rather than dropped.  Anything that could not be inspected is reported through
`complete: False` so the wizard never presents "nothing found" as fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant

OLD_TIMER_EVENT = "voice_satellite_timer"
_FILES = (("automation", "automations.yaml"), ("script", "scripts.yaml"))
_INCLUDE_TAGS = ("!include",)


class Tagged:
    """A YAML node with a custom tag (`!secret`, `!include ...`) kept verbatim."""

    __slots__ = ("tag", "value")

    def __init__(self, tag: str, value: Any) -> None:
        self.tag, self.value = tag, value

    @property
    def opaque(self) -> bool:
        """An include pulls in content this parser cannot see."""
        return self.tag.startswith(_INCLUDE_TAGS)


class _TaggedLoader(yaml.SafeLoader):
    """SafeLoader that never executes or discards HA's custom tags."""


def _construct_tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Tagged:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    return Tagged("!" + suffix, value)


_TaggedLoader.add_multi_constructor("!", _construct_tagged)


@dataclass(slots=True)
class ReferenceDiscovery:
    """References found plus the honest scope of the search."""

    items: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    warnings: list[str] = field(default_factory=list)

    def incomplete(self, reason: str) -> None:
        self.complete = False
        if reason not in self.warnings:
            self.warnings.append(reason)

    def for_satellite(self, satellite_id: str) -> list[dict[str, str]]:
        return [_public(item) for item in self.items if item["config"] is not None and _contains(item["config"], satellite_id)]


def _public(item: Mapping[str, Any]) -> dict[str, str]:
    return {"kind": item["kind"], "id": item["id"], "name": item["name"]}


def _opaque(value: Any) -> bool:
    if isinstance(value, Tagged):
        return value.opaque or _opaque(value.value)
    if isinstance(value, Mapping):
        return any(_opaque(key) or _opaque(item) for key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_opaque(item) for item in value)
    return False


def _contains(value: Any, satellite_id: str) -> bool:
    """Match literal old entity ids or the old timer event inside a config."""
    if isinstance(value, str):
        return satellite_id in value or OLD_TIMER_EVENT in value
    if isinstance(value, Tagged):
        return _contains(value.value, satellite_id)
    if isinstance(value, Mapping):
        return any(_contains(key, satellite_id) or _contains(item, satellite_id) for key, item in value.items())
    if isinstance(value, Iterable):
        return any(_contains(item, satellite_id) for item in value)
    return False


def _item(kind: str, config: Any, fallback: str, entity_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(config, Mapping):
        return None
    ident = str(config.get("id") or entity_id or config.get("alias") or config.get("name") or fallback)
    name = str(config.get("alias") or config.get("name") or ident)
    return {"kind": kind, "id": ident, "name": name, "entity_id": entity_id, "config": config}


def scan_references(documents: Mapping[str, Any], satellite_id: str) -> list[dict[str, str]]:
    """Return, never mutate, configured automations and scripts using old APIs."""
    discovery = ReferenceDiscovery()
    _add_documents(discovery, documents)
    return discovery.for_satellite(satellite_id)


def _add_documents(discovery: ReferenceDiscovery, documents: Mapping[str, Any]) -> None:
    for kind in ("automation", "script"):
        source = documents.get(kind, {})
        if isinstance(source, Tagged):
            discovery.incomplete(f"{kind}: content comes from {source.tag} and could not be inspected")
            continue
        entries = list(enumerate(source)) if isinstance(source, list) else list(source.items()) if isinstance(source, Mapping) else []
        for index, config in entries:
            if _opaque(config):
                discovery.incomplete(f"{kind} {index}: includes content that could not be inspected")
            if (item := _item(kind, config, str(index))) is not None:
                discovery.items.append(item)


def _read_yaml(config_dir: str) -> tuple[dict[str, Any], list[str]]:
    documents: dict[str, Any] = {}
    problems: list[str] = []
    for kind, filename in _FILES:
        path = Path(config_dir, filename)
        try:
            with path.open(encoding="utf-8") as handle:
                documents[kind] = yaml.load(handle, Loader=_TaggedLoader) or {}  # noqa: S506 - SafeLoader subclass
        except FileNotFoundError:
            documents[kind] = {}
        except (OSError, yaml.YAMLError):
            problems.append(f"{filename} could not be read")
    return documents, problems


def _loaded(hass: HomeAssistant, domain: str, discovery: ReferenceDiscovery) -> bool:
    """Add what HA itself loaded for `domain`; False when it is not loaded."""
    component = hass.data.get(domain)
    entities = getattr(component, "entities", None)
    if entities is None:
        return False
    for entity in entities:
        config = getattr(entity, "raw_config", None)
        if config is None:
            discovery.incomplete(f"{domain} {getattr(entity, 'entity_id', '?')} has no readable configuration")
            continue
        if (item := _item(domain, config, str(entity.entity_id), getattr(entity, "entity_id", None))) is not None:
            discovery.items.append(item)
    return True


async def async_discover_references(hass: HomeAssistant) -> ReferenceDiscovery:
    """Inventory loaded automations/scripts, falling back to tagged-aware YAML."""
    discovery = ReferenceDiscovery()
    unloaded = [kind for kind, _ in _FILES if not _loaded(hass, kind, discovery)]
    if unloaded:
        documents, problems = await hass.async_add_executor_job(_read_yaml, hass.config.config_dir)
        _add_documents(discovery, {kind: documents.get(kind, {}) for kind in unloaded})
        for problem in problems:
            if any(problem.startswith(name) for kind, name in _FILES if kind in unloaded):
                discovery.incomplete(problem)
    return discovery
