"""Deterministic entity graph compilation and migration diagnostics."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from . import type_registry


ENTITY_ID = re.compile(
    r"^entity\.[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)*$")
ENTITY_KINDS = frozenset(type_registry.ENUMS.get("entityKind", ()))

_RELATIONSHIP_FIELDS = (
    "id", "confidence", "note", "observedAt", "validFrom", "validUntil",
)
_TOPIC_ENTITY_KINDS = {
    "person": "person",
    "people": "person",
    "organization": "organization",
    "organizations": "organization",
    "organisation": "organization",
    "organisations": "organization",
    "institution": "institution",
    "institutions": "institution",
    "party": "party",
    "parties": "party",
    "collective": "collective",
    "collectives": "collective",
    "group": "collective",
    "groups": "collective",
    "place": "place",
    "places": "place",
    "event": "event",
    "events": "event",
}


def valid_entity_id(value):
    """Return whether value is a stable ``entity.*`` identifier."""
    return isinstance(value, str) and ENTITY_ID.fullmatch(value) is not None


def validate_relationships(value):
    """Validate the canonical list-of-objects relationship schema."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        return ["relationships must be a list of objects"]
    errors = []
    for index, relationship in enumerate(value):
        prefix = f"relationships[{index}]"
        if not isinstance(relationship, dict):
            errors.append(f"{prefix} must be an object")
            continue
        predicate = relationship.get("predicate")
        if not isinstance(predicate, str) or not predicate.strip():
            errors.append(f"{prefix}.predicate must be a non-empty string")
        elif "\n" in predicate or "\r" in predicate:
            errors.append(f"{prefix}.predicate must be one line")
        target_id = relationship.get("targetId")
        if not valid_entity_id(target_id):
            errors.append(f"{prefix}.targetId must be a stable entity.* id")
    return errors


def validate_entity_page(page):
    """Validate one compiler page dictionary as an entity node."""
    path = str(page.get("path") or "<unknown>")
    errors = []
    if page.get("kind") != "entity":
        errors.append(f"{path}: type must be entity")
    if not valid_entity_id(page.get("id")):
        errors.append(f"{path}: id must be a stable entity.* id")
    kind = page.get("entityKind")
    if not isinstance(kind, str) or not kind.strip():
        errors.append(f"{path}: entityKind must be a non-empty string")
    elif ENTITY_KINDS and kind not in ENTITY_KINDS:
        errors.append(f"{path}: entityKind must be one of {sorted(ENTITY_KINDS)}")
    errors.extend(f"{path}: {error}" for error in validate_relationships(
        page.get("relationships")))
    return errors


def _relationship_record(page, relationship, index, schema_version):
    record = {
        "schemaVersion": schema_version,
        "sourceId": page["id"],
        "sourcePath": page["path"],
        "predicate": relationship["predicate"].strip(),
        "targetId": relationship["targetId"],
        "relationshipIndex": index,
    }
    for field in _RELATIONSHIP_FIELDS:
        if field in relationship:
            record[field] = relationship[field]
    return record


def analyze_graph(pages, schema_version):
    """Build graph records plus deterministic integrity diagnostics."""
    entity_pages = sorted(
        (page for page in pages if page.get("kind") == "entity"),
        key=lambda page: page.get("path", ""))
    id_paths = defaultdict(list)
    invalid_entities = []
    invalid_relationships = []
    relationships = []

    for page in entity_pages:
        path = str(page.get("path") or "<unknown>")
        if not valid_entity_id(page.get("id")):
            invalid_entities.append(
                f"{path}: id must be a stable entity.* id")
        kind = page.get("entityKind")
        if not isinstance(kind, str) or not kind.strip():
            invalid_entities.append(
                f"{path}: entityKind must be a non-empty string")
        elif ENTITY_KINDS and kind not in ENTITY_KINDS:
            invalid_entities.append(
                f"{path}: entityKind must be one of {sorted(ENTITY_KINDS)}")
        invalid_relationships.extend(
            f"{path}: {error}" for error in validate_relationships(
                page.get("relationships")))
        if valid_entity_id(page.get("id")):
            id_paths[page["id"]].append(page["path"])
        value = page.get("relationships")
        if not valid_entity_id(page.get("id")) or not isinstance(value, list):
            continue
        for index, relationship in enumerate(value):
            if validate_relationships([relationship]):
                continue
            relationships.append(_relationship_record(
                page, relationship, index, schema_version))

    duplicate_ids = {
        entity_id: sorted(paths) for entity_id, paths in id_paths.items()
        if len(paths) > 1
    }
    entity_records = []
    for page in entity_pages:
        entity_id = page.get("id")
        if not valid_entity_id(entity_id):
            continue
        entity_kind = page.get("entityKind")
        record = {
            "schemaVersion": schema_version,
            "id": entity_id,
            "title": page.get("title") or Path(page["path"]).stem,
            "entityKind": entity_kind
            if isinstance(entity_kind, str) and entity_kind in ENTITY_KINDS
            else "other",
            "path": page["path"],
            "aliases": list(page.get("aliases") or []),
            "topics": list(page.get("topics") or []),
            "ambiguous": entity_id in duplicate_ids,
        }
        if page.get("privacyTier"):
            record["privacyTier"] = page["privacyTier"]
        entity_records.append(record)

    orphan_relationships = []
    ambiguous_relationships = []
    for relationship in relationships:
        source_ambiguous = relationship["sourceId"] in duplicate_ids
        target_paths = id_paths.get(relationship["targetId"], [])
        if not target_paths:
            relationship["resolution"] = "orphan"
            orphan_relationships.append(dict(relationship))
        elif source_ambiguous or len(target_paths) > 1:
            relationship["resolution"] = "ambiguous"
            relationship["targetPaths"] = sorted(target_paths)
            ambiguous_relationships.append(dict(relationship))
        else:
            relationship["resolution"] = "resolved"
            relationship["targetPath"] = target_paths[0]

    entity_records.sort(key=lambda item: (item["id"], item["path"]))
    relationships.sort(key=lambda item: (
        item["sourceId"], item["predicate"], item["targetId"],
        item["sourcePath"], item["relationshipIndex"]))
    migration_candidates = find_refs_migration_candidates(pages, id_paths)
    return {
        "entities": entity_records,
        "relationships": relationships,
        "duplicateIds": duplicate_ids,
        "invalidEntities": sorted(set(invalid_entities)),
        "invalidRelationships": sorted(set(invalid_relationships)),
        "orphanRelationships": sorted(
            orphan_relationships, key=_relationship_sort_key),
        "ambiguousRelationships": sorted(
            ambiguous_relationships, key=_relationship_sort_key),
        "migrationCandidates": migration_candidates,
        "summary": {
            "entityCount": len(entity_records),
            "relationshipCount": len(relationships),
            "resolvedRelationshipCount": sum(
                item["resolution"] == "resolved" for item in relationships),
            "orphanRelationshipCount": len(orphan_relationships),
            "ambiguousRelationshipCount": len(ambiguous_relationships),
            "invalidEntityCount": len(set(invalid_entities)),
            "invalidRelationshipCount": len(set(invalid_relationships)),
            "migrationCandidateCount": len(migration_candidates),
        },
    }


def _relationship_sort_key(item):
    return (
        item["sourceId"], item["predicate"], item["targetId"],
        item["sourcePath"], item["relationshipIndex"])


def _slug(value):
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return value or "unnamed"


def find_refs_migration_candidates(pages, entity_paths=None):
    """Report conservative refs/ candidates without moving any page."""
    entity_paths = entity_paths or {}
    candidates = []
    for page in sorted(pages, key=lambda item: item.get("path", "")):
        if page.get("kind") != "ref":
            continue
        reasons = []
        explicit_kind = page.get("entityKind")
        inferred_kind = explicit_kind if (
            isinstance(explicit_kind, str) and explicit_kind in ENTITY_KINDS
        ) else None
        if isinstance(explicit_kind, str) and explicit_kind:
            reasons.append("frontmatter entityKind")
        page_id = page.get("id")
        if valid_entity_id(page_id):
            reasons.append("frontmatter entity id")
        for topic in page.get("topics") or []:
            topic_kind = _TOPIC_ENTITY_KINDS.get(str(topic).strip().lower())
            if topic_kind:
                if not inferred_kind:
                    inferred_kind = topic_kind
                reasons.append(f"topic {topic}")
        if not reasons:
            continue
        stem = Path(page["path"]).stem
        suggested_id = page_id if valid_entity_id(page_id) else (
            f"entity.{_slug(page.get('title') or stem)}")
        candidates.append({
            "path": page["path"],
            "title": page.get("title") or stem.replace("-", " "),
            "suggestedId": suggested_id,
            "suggestedPath": f"entities/{stem}.md",
            "entityKind": inferred_kind or "other",
            "reasons": sorted(set(reasons)),
            "existingEntityPaths": sorted(entity_paths.get(suggested_id, [])),
            "reviewOnly": True,
        })
    return candidates


def jsonl(records):
    """Serialize records as stable, newline-terminated JSONL."""
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))
        for record in records]
    return "".join(line + "\n" for line in sorted(lines))
