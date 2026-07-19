"""Declarative page-type registry shared by schema and runtime consumers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


_LAYERS = frozenset(("knowledge", "memory", "archive", "derived"))
_WRITERS = frozenset(("review", "direct", "compiler"))


@dataclass(frozen=True)
class PageTypeSpec:
    directory: str
    ntype: str
    layer: str
    writer: str
    required: tuple[str, ...]
    manage_external: bool


def _load_registry():
    path = Path(__file__).with_name("page-types.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schemaVersion")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError("page-types.json needs a positive schemaVersion")
    specs = []
    directories = set()
    names = set()
    for item in raw.get("types", []):
        if not isinstance(item, dict):
            raise RuntimeError("page-types.json type entries must be objects")
        directory = str(item.get("directory") or "")
        ntype = str(item.get("type") or "")
        layer = str(item.get("layer") or "")
        writer = str(item.get("writer") or "")
        required = tuple(str(value) for value in item.get("required", []))
        if not directory or not ntype or directory in directories or ntype in names:
            raise RuntimeError("page-types.json has missing or duplicate types")
        if layer not in _LAYERS or writer not in _WRITERS:
            raise RuntimeError(f"invalid page type policy for {ntype}")
        directories.add(directory)
        names.add(ntype)
        manage_external = item.get("manageExternal", False)
        if not isinstance(manage_external, bool):
            raise RuntimeError(
                f"manageExternal for {ntype} must be a boolean")
        specs.append(PageTypeSpec(
            directory, ntype, layer, writer, required, manage_external))
    if not specs:
        raise RuntimeError("page-types.json contains no page types")
    enums = {
        str(field): frozenset(str(value) for value in values)
        for field, values in (raw.get("enums") or {}).items()
        if isinstance(values, list)
    }
    candidate_fields = tuple(
        str(value) for value in raw.get("candidateFields", []))
    return version, tuple(specs), enums, candidate_fields


SCHEMA_VERSION, PAGE_TYPES, ENUMS, CANDIDATE_FIELDS = _load_registry()
BY_DIRECTORY = {spec.directory: spec for spec in PAGE_TYPES}
BY_TYPE = {spec.ntype: spec for spec in PAGE_TYPES}


def types_for_layer(layer):
    return tuple(spec.ntype for spec in PAGE_TYPES if spec.layer == layer)


def directories_for_writer(writer):
    return tuple(spec.directory for spec in PAGE_TYPES if spec.writer == writer)


def external_types():
    """Types inserted into a host wiki validator's managed block."""
    return tuple(spec for spec in PAGE_TYPES if spec.manage_external)


def require_supported_version(version, artifact="artifact"):
    """Reject unreadable future artifacts instead of guessing their shape."""
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"{artifact} has no supported schemaVersion")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"{artifact} schemaVersion {version} is newer than supported "
            f"version {SCHEMA_VERSION}")
    return version
