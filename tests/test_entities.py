import json
from pathlib import Path

from memory_wiki import compiler, entities, schema, wikifs


def _entity(root, name, entity_id, relationships=None, kind="person"):
    fm = schema.page_frontmatter(
        "entity", name, topics=["people"], created="2026-07-01",
        extras={
            "id": entity_id,
            "entityKind": kind,
            "relationships": relationships or [],
        })
    path = root / "entities" / f"{name.lower().replace(' ', '-')}.md"
    wikifs.atomic_write(path, wikifs.render_page(fm, "Entity notes.\n"))
    return path


def test_entity_and_relationship_schema_is_explicit():
    page = {
        "kind": "entity",
        "path": "entities/ada-lovelace.md",
        "id": "entity.ada-lovelace",
        "entityKind": "person",
        "relationships": [{
            "predicate": "workedWith",
            "targetId": "entity.charles-babbage",
        }],
    }
    assert entities.validate_entity_page(page) == []
    assert entities.valid_entity_id("entity.person.ada-lovelace")
    assert not entities.valid_entity_id("Ada Lovelace")

    page["id"] = "ada"
    page["relationships"] = [{"type": "workedWith", "target": "babbage"}]
    errors = entities.validate_entity_page(page)
    assert any("stable entity.* id" in error for error in errors)
    assert any(".predicate" in error for error in errors)
    assert any(".targetId" in error for error in errors)


def test_graph_marks_resolved_orphan_and_ambiguous_relationships():
    pages = [
        {
            "kind": "entity", "path": "entities/ada.md", "title": "Ada",
            "id": "entity.ada", "entityKind": "person", "aliases": [],
            "topics": [], "relationships": [
                {"predicate": "workedWith", "targetId": "entity.babbage"},
                {"predicate": "knew", "targetId": "entity.missing"},
                {"predicate": "visited", "targetId": "entity.london"},
            ],
        },
        {
            "kind": "entity", "path": "entities/babbage.md",
            "title": "Babbage", "id": "entity.babbage",
            "entityKind": "person", "aliases": [], "topics": [],
        },
        {
            "kind": "entity", "path": "entities/london.md",
            "title": "London", "id": "entity.london",
            "entityKind": "place", "aliases": [], "topics": [],
        },
        {
            "kind": "entity", "path": "entities/london-city.md",
            "title": "London city", "id": "entity.london",
            "entityKind": "place", "aliases": [], "topics": [],
        },
    ]

    graph = entities.analyze_graph(pages, schema.SCHEMA_VERSION)

    assert [item["resolution"] for item in graph["relationships"]] == [
        "orphan", "ambiguous", "resolved"]
    assert graph["duplicateIds"] == {
        "entity.london": [
            "entities/london-city.md", "entities/london.md"]}
    assert all(item["schemaVersion"] == schema.SCHEMA_VERSION
               for item in graph["entities"] + graph["relationships"])


def test_compile_emits_graph_artifacts_and_review_only_migration_report(
        fixture_wiki):
    root = Path(fixture_wiki)
    _entity(root, "Charles Babbage", "entity.charles-babbage")
    _entity(root, "Ada Lovelace", "entity.ada-lovelace", [
        {"predicate": "workedWith", "targetId": "entity.charles-babbage"},
        {"predicate": "correspondedWith", "targetId": "entity.missing"},
    ])
    ref = root / "refs" / "analytical-society.md"
    fm = schema.page_frontmatter(
        "ref", "Analytical Society", topics=["organization"],
        created="2026-07-01")
    wikifs.atomic_write(ref, wikifs.render_page(fm, "Historical notes.\n"))

    result = compiler.compile_wiki(root)

    entity_lines = (root / compiler.ENTITIES_DIGEST_PATH).read_text().splitlines()
    relationship_lines = (
        root / compiler.RELATIONSHIPS_DIGEST_PATH).read_text().splitlines()
    entity_records = [json.loads(line) for line in entity_lines]
    relationship_records = [json.loads(line) for line in relationship_lines]
    assert [item["id"] for item in entity_records] == [
        "entity.ada-lovelace", "entity.charles-babbage"]
    assert {item["resolution"] for item in relationship_records} == {
        "resolved", "orphan"}
    assert result["entityGraph"]["migrationCandidateCount"] == 1
    digest = json.loads((root / compiler.AGENT_DIGEST_PATH).read_text())
    assert digest["entityGraph"]["entityCount"] == 2

    health = (root / "reports" / "entity-relationships.md").read_text()
    assert "Orphan relationships: 1" in health
    assert "`entity.missing`" in health
    migration = (
        root / "reports" / "entity-migration-candidates.md").read_text()
    assert "Review only: this report never moves or rewrites pages." in migration
    assert "refs/analytical-society.md" in migration
    assert "`entity.analytical-society`" in migration
    assert ref.is_file()
    assert not (root / "entities" / "analytical-society.md").exists()
    assert compiler.compile_wiki(root)["updatedFiles"] == []
