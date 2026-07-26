import json
from datetime import datetime, timezone
from pathlib import Path

from memory_wiki import claims, compiler, schema, wikifs


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def test_nested_claim_json_roundtrip_preserves_legacy_shapes():
    value = [{
        "id": "claim.alpha.baseline",
        "text": "Alpha is supported.",
        "confidence": 0.8,
        "evidence": [{
            "kind": "source", "sourceId": "source.one",
            "path": "sources/one.md", "lines": "10-12", "weight": 2,
            "confidence": 0.9, "privacyTier": "public", "note": "Direct",
            "updatedAt": "2026-07-10",
        }],
    }]
    fm = {"topics": ["alpha", "beta"],
          "fields": {"author": "Jane", "medium": "post"},
          "claims": value}
    text = wikifs.render_frontmatter(fm)
    parsed, _ = wikifs.parse_frontmatter(text)
    assert parsed == fm
    assert 'topics:\n  - "alpha"\n  - "beta"' in text
    assert 'fields:\n  author: "Jane"' in text
    assert "claims: [{" in text


def test_claim_validation_and_normalization():
    raw = [
        {"id": "claim.example.ok", "text": "  A claim  ", "confidence": 1,
         "evidence": [{"note": "proof", "confidence": 0}]},
        {"text": " ", "evidence": []},
        {"text": "bad", "confidence": 1.1,
         "evidence": [{"note": "x", "confidence": -0.1}]},
    ]
    normalized = claims.normalize_claims(raw)
    assert [item["text"] for item in normalized] == ["A claim", "bad"]
    assert normalized[0]["evidence"][0]["confidence"] == 0.0
    assert "confidence" not in normalized[1]
    errors = claims.validate_claims(raw)
    assert any("claims[1].text" in error for error in errors)
    assert any("claims[2].confidence" in error for error in errors)
    assert any("evidence[0].confidence" in error for error in errors)
    assert claims.normalize_status(None) == "supported"


def test_claim_freshness_prefers_newest_evidence_and_repeated_ids_conflict():
    page = {"path": "concepts/a.md", "title": "A",
            "updatedAt": "2026-01-01", "claims": []}
    claim = {"id": "shared", "text": "same", "evidence": [
        {"updatedAt": "2026-06-01"}, {"updatedAt": "2026-07-13"}]}
    assert claims.assess_claim_freshness(page, claim, NOW)["level"] == "fresh"
    page["claims"] = [claim, dict(claim, text="different")]
    assert claims.contradiction_clusters([page], NOW)[0]["key"] == "shared"
    page["claims"] = [claim]
    other = {"path": "sources/b.md", "title": "B",
             "updatedAt": "2026-07-14", "claims": [dict(claim)]}
    assert claims.contradiction_clusters([page, other], NOW) == []
    other["claims"][0]["status"] = "refuted"
    assert claims.contradiction_clusters([page, other], NOW)[0]["key"] == "shared"


def _write_structured(root, relpath, ntype, title, claim, extras=None):
    fm = schema.page_frontmatter(
        ntype, title, topics=["testing"], created="2026-07-10", extras=extras)
    fm["id"] = relpath.removesuffix(".md").replace("/", ".")
    fm["updatedAt"] = "2026-07-10"
    fm["claims"] = [claim]
    wikifs.atomic_write(root / relpath, wikifs.render_page(fm, "Body.\n"))


def test_compile_artifacts_reports_legacy_fallback_and_write_if_changed(fixture_wiki):
    root = Path(fixture_wiki)
    _write_structured(root, "concepts/alpha.md", "concept", "Alpha", {
        "id": "claim.shared.alpha", "text": "Alpha is true", "confidence": 0.2,
        "evidence": [{"kind": "paper", "sourceId": "source.paper",
                      "updatedAt": "2026-07-12"}],
    })
    alpha_path = root / "concepts" / "alpha.md"
    alpha_fm, alpha_body = wikifs.parse_frontmatter(alpha_path.read_text())
    alpha_fm["questions"] = ["Which source resolves the disagreement?"]
    alpha_fm["privacyTier"] = "local-private"
    alpha_path.write_text(wikifs.render_page(alpha_fm, alpha_body),
                          encoding="utf-8")
    _write_structured(
        root, "sources/alpha-source.md", "source", "Alpha source", {
            "id": "claim.shared.alpha", "text": "Alpha is false", "status": "refuted",
            "confidence": 0.8, "evidence": [],
        }, extras={"author": "Jane", "medium": "paper"})
    claim_path = root / "claims" / "small-tools-beat-frameworks.md"
    fm, body = wikifs.parse_frontmatter(claim_path.read_text())
    fm["contradicts"] = ["concepts/alpha.md"]
    claim_path.write_text(wikifs.render_page(fm, body), encoding="utf-8")

    result = compiler.compile_wiki(root, now=NOW)
    assert result["claimCount"] == 3
    assert result["pages"] == sorted(result["pages"], key=lambda page: page["path"])
    assert set(result["updatedFiles"]) == {
        compiler.AGENT_DIGEST_PATH, compiler.CLAIMS_DIGEST_PATH,
        compiler.ENTITIES_DIGEST_PATH, compiler.RELATIONSHIPS_DIGEST_PATH,
        *(f"reports/{slug}.md" for slug, _ in compiler.REPORT_DEFINITIONS),
    }

    digest = json.loads((root / compiler.AGENT_DIGEST_PATH).read_text())
    assert digest["schemaVersion"] == schema.SCHEMA_VERSION
    assert digest["claimHealth"]["lowConfidence"] == 1
    assert digest["claimHealth"]["missingEvidence"] == 2
    assert {item["kind"] for item in digest["contradictionClusters"]} == {
        "claim-id", "explicit"}
    legacy = next(page for page in digest["pages"] if page["kind"] == "claim")
    assert legacy["claimCount"] == 1
    assert legacy["topClaims"][0]["text"] == "small tools beat frameworks"

    jsonl = (root / compiler.CLAIMS_DIGEST_PATH).read_text().splitlines()
    assert jsonl == sorted(jsonl)
    assert len(jsonl) == 3
    assert all(json.loads(line)["status"] for line in jsonl)
    contradictions = (root / "reports" / "contradictions.md").read_text()
    assert "Explicit contradiction links: 1" in contradictions
    assert "Competing claim clusters: 1" in contradictions
    low = (root / "reports" / "low-confidence.md").read_text()
    assert "Low-confidence claims: 1" in low
    provenance = (root / "reports" / "provenance-coverage.md").read_text()
    assert "paper: 1" in provenance
    assert "Which source resolves the disagreement?" in (
        root / "reports" / "open-questions.md").read_text()
    assert "page tier `local-private`" in (
        root / "reports" / "privacy-review.md").read_text()

    assert compiler.compile_wiki(root, now=NOW)["updatedFiles"] == []


def test_report_managed_block_preserves_human_content(fixture_wiki):
    root = Path(fixture_wiki)
    compiler.compile_wiki(root, now=NOW)
    path = root / "reports" / "claim-health.md"
    original = path.read_text()
    path.write_text(original + "\n## Human notes\n\nKeep me.\n", encoding="utf-8")
    compiler.compile_wiki(root, now=NOW)
    rendered = path.read_text()
    assert "## Human notes\n\nKeep me." in rendered
    assert rendered.count("<!-- memory-wiki:report:claim-health:start -->") == 1
    fm, _ = wikifs.parse_frontmatter(rendered)
    assert fm["type"] == "report"
    assert schema.validate_page("reports/claim-health.md", rendered) == []


def test_raw_archives_are_counted_private_but_not_stale(
        fixture_wiki, make_page):
    root = Path(fixture_wiki)
    path = make_page(
        root, "raw/archived-conversation.md", "raw", "Archived conversation",
        topics=("conversation",), body="Private historical dialogue.\n")
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm is not None
    fm["createdAt"] = "2020-01-01"
    fm["privacyTier"] = "private"
    path.write_text(wikifs.render_page(fm, body), encoding="utf-8")

    compiler.compile_wiki(root, now=NOW)

    digest = json.loads((root / compiler.AGENT_DIGEST_PATH).read_text())
    assert digest["pageCounts"]["raw"] == 1
    stale = (root / "reports" / "stale-pages.md").read_text()
    assert "Archived conversation" not in stale
    privacy = (root / "reports" / "privacy-review.md").read_text()
    assert "Archived conversation" in privacy


def test_report_repairs_managed_frontmatter_without_losing_human_fields(
        fixture_wiki):
    root = Path(fixture_wiki)
    compiler.compile_wiki(root, now=NOW)
    path = root / "reports" / "claim-health.md"
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    fm.update(type="concept", title="Changed", status="draft", owner="human")
    path.write_text(wikifs.render_page(fm, body + "\n## Human notes\n\nKeep.\n"),
                    encoding="utf-8")

    compiler.compile_wiki(root, now=NOW)

    repaired = path.read_text(encoding="utf-8")
    repaired_fm, _ = wikifs.parse_frontmatter(repaired)
    assert repaired_fm["type"] == "report"
    assert repaired_fm["title"] == "Claim Health"
    assert repaired_fm["status"] == "published"
    assert repaired_fm["owner"] == "human"
    assert "## Human notes\n\nKeep." in repaired
    assert schema.validate_page("reports/claim-health.md", repaired) == []


def test_concurrent_compiles_serialize_and_converge(fixture_wiki):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _unused: compiler.compile_wiki(fixture_wiki, now=NOW),
            range(2)))
    changed_counts = sorted(len(result["updatedFiles"]) for result in results)
    assert changed_counts == [0, 4 + len(compiler.REPORT_DEFINITIONS)]
    assert compiler.compile_wiki(fixture_wiki, now=NOW)["updatedFiles"] == []


def test_compile_does_not_follow_page_symlinks(fixture_wiki, tmp_path):
    root = Path(fixture_wiki)
    outside = tmp_path / "outside.md"
    outside.write_text(
        wikifs.render_page({
            "type": "concept", "title": "Private", "topics": [],
            "createdAt": "2026-07-14", "claims": [{
                "id": "private.claim", "text": "Do not ingest me",
                "evidence": [],
            }],
        }, "Private content.\n"),
        encoding="utf-8",
    )
    (root / "concepts" / "private.md").symlink_to(outside)

    compiler.compile_wiki(root, now=NOW)

    digest = json.loads((root / compiler.AGENT_DIGEST_PATH).read_text())
    assert all(page["path"] != "concepts/private.md" for page in digest["pages"])
    assert "private.claim" not in (root / compiler.CLAIMS_DIGEST_PATH).read_text()
