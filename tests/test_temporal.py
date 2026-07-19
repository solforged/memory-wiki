from datetime import datetime, timezone

import pytest

from memory_wiki import claims, compiler, schema, temporal, wikifs


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


@pytest.mark.parametrize(("claim", "state"), [
    ({}, "unknown"),
    ({"validFrom": "2026-07-01"}, "current"),
    ({"validFrom": "2026-08-01"}, "future"),
    ({"validUntil": "2026-07-19"}, "expired"),
    ({"status": "superseded"}, "superseded"),
    ({"validFrom": "not-a-date"}, "invalid"),
])
def test_claim_state_and_default_recall_policy(claim, state):
    assert temporal.claim_state(claim, NOW) == state
    assert temporal.is_recallable(claim, NOW) == (
        state in ("current", "unknown"))
    assert temporal.is_recallable(claim, NOW, include_historical=True) == (
        state != "invalid")


def test_interval_validation_and_as_of_contract():
    errors = temporal.interval_errors({
        "validFrom": "2026-08-01", "validUntil": "2026-07-01",
    }, "claims[0]")
    assert errors == [
        "claims[0].validFrom must not be after claims[0].validUntil"]
    with pytest.raises(ValueError, match="as_of must be an ISO"):
        temporal.coerce_as_of("tomorrow")


def test_exact_excerpt_hash_is_content_bound():
    excerpt = "The exact source sentence."
    digest = claims.excerpt_hash(excerpt)
    value = [{
        "id": "claim.alpha.current",
        "text": "Alpha is current.",
        "validFrom": "2026-07-01",
        "evidence": [{
            "kind": "source", "observedAt": "2026-07-10T12:00:00Z",
            "excerpt": excerpt, "excerptHash": digest,
        }],
    }]
    assert claims.validate_claims(value) == []
    value[0]["evidence"][0]["excerpt"] += " Changed."
    assert any("does not match excerpt" in error
               for error in claims.validate_claims(value))


def test_semantic_lineage_validation_and_graph_issues():
    pages = [{
        "path": "claims/alpha.md", "title": "Alpha", "claims": [{
            "id": "claim.alpha.current", "text": "Current",
            "supersedes": "claim.alpha.previous", "evidence": [],
        }],
    }]
    assert claims.lineage_issues(pages) == [{
        "kind": "missing-predecessor",
        "claimId": "claim.alpha.current",
        "predecessorId": "claim.alpha.previous",
        "paths": ["claims/alpha.md"],
    }]
    invalid = [{
        "id": "claim.alpha.current", "text": "Bad", "evidence": [],
        "supersedes": "claim.alpha.current",
    }]
    assert any("must not reference itself" in error
               for error in claims.validate_claims(invalid))


def test_compiler_keeps_history_but_prefers_current_claims(fixture_wiki):
    root = fixture_wiki
    path = root / "concepts" / "versioned.md"
    fm = schema.page_frontmatter(
        "concept", "Versioned", topics=["testing"], created="2026-01-01")
    fm["claims"] = [
        {
            "id": "claim.versioned.previous", "text": "Old value",
            "status": "superseded", "validUntil": "2026-07-01",
            "evidence": [], "updatedAt": "2026-07-01",
        },
        {
            "id": "claim.versioned.current", "text": "Current value",
            "validFrom": "2026-07-01",
            "supersedes": "claim.versioned.previous",
            "evidence": [{
                "kind": "source", "sourceId": "source.current",
                "observedAt": "2026-07-02",
            }],
            "updatedAt": "2026-07-02",
        },
    ]
    path.write_text(wikifs.render_page(fm, "Version history.\n"),
                    encoding="utf-8")

    compiler.compile_wiki(root, now=NOW)

    import json
    digest = json.loads((root / compiler.AGENT_DIGEST_PATH).read_text())
    page = next(item for item in digest["pages"]
                if item["path"] == "concepts/versioned.md")
    assert [item["text"] for item in page["topClaims"]] == ["Current value"]
    rows = [json.loads(line) for line in (
        root / compiler.CLAIMS_DIGEST_PATH).read_text().splitlines()]
    versioned = [row for row in rows if row["pagePath"] == "concepts/versioned.md"]
    assert {row["temporalState"] for row in versioned} == {
        "current", "superseded"}
    assert all(row["schemaVersion"] == schema.SCHEMA_VERSION
               for row in versioned)
    report = (root / "reports" / "temporal-integrity.md").read_text()
    assert "superseded: 1" in report
    assert "Lineage issues: 0" in report
