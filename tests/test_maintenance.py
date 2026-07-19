import json
from pathlib import Path

from memory_wiki import compiler, maintenance, schema


def _snapshot(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_maintenance_derives_stable_read_only_tasks(cfg):
    root = Path(cfg.wiki_root)
    digest_path = root / compiler.AGENT_DIGEST_PATH
    claims_path = root / compiler.CLAIMS_DIGEST_PATH
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps({
        "contradictionClusters": [{
            "key": "claim.shared.alpha", "label": "Shared alpha",
            "paths": ["claims/a.md", "claims/b.md"],
        }],
        "pages": [{
            "path": "claims/a.md", "title": "Alpha",
            "questions": ["What would falsify this?"],
            "privacyTier": "local-private",
        }],
    }), encoding="utf-8")
    claims_path.write_text(json.dumps({
        "pagePath": "claims/a.md", "id": "claim.test.alpha",
        "text": "Alpha remains true", "status": "contested",
        "confidence": 0.3, "missingEvidence": True,
        "freshnessLevel": "stale", "privacyTiers": ["private"],
    }) + "\n", encoding="utf-8")
    before = _snapshot(root)

    first = maintenance.build(cfg, limit=1000)
    second = maintenance.build(cfg, limit=1000)

    categories = {item["category"] for item in first["tasks"]}
    assert {"contradiction", "contested_claim", "missing_evidence",
            "low_confidence", "stale_claim", "privacy_review",
            "open_question"}.issubset(categories)
    assert first["tasks"] == second["tasks"]
    assert first["summary"]["by_severity"]["high"] >= 2
    assert _snapshot(root) == before


def test_maintenance_empty_state_is_clear(cfg):
    result = maintenance.build(cfg)
    assert result["compiler_available"] is False
    assert result["tasks"] == []
    assert "missing" in result["issues"][0]


def test_maintenance_rejects_explicit_future_artifacts(cfg):
    root = Path(cfg.wiki_root)
    digest_path = root / compiler.AGENT_DIGEST_PATH
    claims_path = root / compiler.CLAIMS_DIGEST_PATH
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    future = schema.SCHEMA_VERSION + 1
    digest_path.write_text(json.dumps({
        "schemaVersion": future,
        "contradictionClusters": [{"label": "Do not read"}],
    }), encoding="utf-8")
    claims_path.write_text(json.dumps({
        "schemaVersion": future,
        "text": "Do not read",
        "missingEvidence": True,
    }) + "\n", encoding="utf-8")

    result = maintenance.build(cfg)

    assert result["tasks"] == []
    assert any("newer than supported" in issue for issue in result["issues"])
