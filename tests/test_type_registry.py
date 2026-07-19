import json
from pathlib import Path

from memory_wiki import schema, type_registry


def test_registry_is_versioned_and_bijective():
    assert type_registry.SCHEMA_VERSION >= 2
    assert len(type_registry.BY_DIRECTORY) == len(type_registry.PAGE_TYPES)
    assert len(type_registry.BY_TYPE) == len(type_registry.PAGE_TYPES)
    assert schema.DIR_TYPE == {
        spec.directory: spec.ntype for spec in type_registry.PAGE_TYPES}
    assert schema.TYPE_DIR == {
        spec.ntype: spec.directory for spec in type_registry.PAGE_TYPES}


def test_registry_drives_layers_and_candidate_fields():
    assert "entity" in schema.KNOWLEDGE_TYPES
    assert "report" not in schema.KNOWLEDGE_TYPES
    assert schema.DERIVED_TYPES == ("report",)
    assert schema.ARCHIVE_TYPES == ("raw",)
    assert set(schema.MEMORY_TYPES) == {"profile", "journal"}
    assert {"id", "entityKind", "relationships", "validFrom", "validUntil"} \
        <= set(schema.FIELDS_ALLOWLIST)
    assert {spec.ntype for spec in type_registry.external_types()} == {
        "entity", "taste", "profile", "journal", "raw", "report"}


def test_registry_json_is_packaged_next_to_module():
    path = Path(type_registry.__file__).with_name("page-types.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schemaVersion"] == type_registry.SCHEMA_VERSION
    assert any(item["directory"] == "entities" for item in raw["types"])


def test_future_artifact_versions_are_rejected():
    assert type_registry.require_supported_version(
        type_registry.SCHEMA_VERSION, "digest") == type_registry.SCHEMA_VERSION
    try:
        type_registry.require_supported_version(
            type_registry.SCHEMA_VERSION + 1, "digest")
    except ValueError as exc:
        assert "newer than supported" in str(exc)
    else:
        raise AssertionError("future artifact version was accepted")
