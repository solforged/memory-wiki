from __future__ import annotations

import json

import memory_wiki
from memory_wiki import chunks, claims, evaluation, search, tools, wikifs
from memory_wiki.index import WikiIndex


def _unit_page(root, make_page):
    path = make_page(
        root, "concepts/release-units.md", "concept", "Release units",
        topics=("operations",),
        body=(
            "A concise release overview.\n\n"
            "## Operations\n\n"
            "Operators use a heliotrope deployment checklist.\n\n"
            "### Rollback\n\n"
            "Restore the last stable artifact.\n\n"
            "## Ownership\n\n"
            "The release captain owns the decision.\n"
        ),
    )
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    excerpt = "The runbook requires two operators for production releases."
    fm["claims"] = [
        {
            "id": "claim.release.current",
            "text": "Starlight deployments require a release quorum.",
            "validFrom": "2025-01-01",
            "evidence": [
                {
                    "kind": "runbook",
                    "sourceId": "source.release-runbook",
                    "path": "sources/release-runbook.md",
                    "lines": "10-12",
                    "observedAt": "2026-07-01",
                    "excerpt": excerpt,
                    "excerptHash": claims.excerpt_hash(excerpt),
                },
                {
                    "kind": "incident",
                    "sourceId": "source.release-incident",
                    "note": "A second exact support record.",
                },
            ],
        },
        {
            "id": "claim.release.expired",
            "text": "Moonlight deployments use the legacy protocol.",
            "validUntil": "2020-01-01",
            "evidence": [],
        },
        {
            "id": "claim.release.future",
            "text": "Aurora deployments use the future protocol.",
            "validFrom": "2099-01-01",
            "evidence": [],
        },
        {
            "id": "claim.release.superseded",
            "text": "Twilight deployments use the superseded protocol.",
            "status": "superseded",
            "evidence": [],
        },
    ]
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return path


def test_units_have_stable_refs_and_exact_anchors(
        fixture_wiki, make_page):
    path = _unit_page(fixture_wiki, make_page)
    index = WikiIndex(fixture_wiki)
    index.refresh()

    current_ref = (
        "concepts/release-units.md#claim:claim.release.current")
    current = index.unit(current_ref)
    evidence = next(
        unit for unit in index.retrieval_units.values()
        if unit.kind == "evidence"
        and unit.evidence.get("sourceId") == "source.release-runbook")
    operations = index.unit(
        "concepts/release-units.md#heading:operations")
    source_lines = path.read_text(encoding="utf-8").splitlines()

    assert current is not None and current.claim_id == "claim.release.current"
    assert evidence.ref.startswith(
        "concepts/release-units.md#evidence:claim.release.current:")
    assert evidence.evidence_anchor == "sources/release-runbook.md#L10-L12"
    assert operations.line_start == source_lines.index("## Operations") + 1
    assert operations.line_end >= operations.line_start

    before = {unit.ref for unit in index.retrieval_units.values()}
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    fm["claims"][0]["evidence"].reverse()
    fm["claims"][0]["evidence"][1]["confidence"] = 0.75
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    index.refresh()
    after = {unit.ref for unit in index.retrieval_units.values()}
    assert before == after


def test_unit_ranking_and_temporal_filters(fixture_wiki, make_page):
    _unit_page(fixture_wiki, make_page)
    index = WikiIndex(fixture_wiki)
    index.refresh()

    claim_hits = search.score_units(
        index, "starlight release quorum", granularity="claim")
    assert claim_hits[0][1].ref.endswith("#claim:claim.release.current")
    evidence_hits = search.score_units(
        index, "two operators production releases", granularity="evidence")
    assert evidence_hits[0][1].evidence_anchor.endswith("#L10-L12")
    heading_hits = search.score_units(
        index, "heliotrope deployment checklist", granularity="heading")
    assert heading_hits[0][1].ref.endswith("#heading:operations")

    assert search.score_units(
        index, "moonlight legacy protocol", granularity="claim") == []
    historical = search.score_units(
        index, "moonlight legacy protocol", granularity="claim",
        include_historical=True)
    assert historical[0][1].temporal_state() == "expired"
    as_of = search.score_units(
        index, "moonlight legacy protocol", granularity="claim",
        as_of="2019-01-01")
    assert as_of[0][1].temporal_state("2019-01-01") == "current"
    assert search.score_units(
        index, "aurora future protocol", granularity="claim") == []
    assert search.score_units(
        index, "twilight superseded protocol", granularity="claim") == []

    legacy = search.score_units(
        index, "small tools frameworks", granularity="claim")
    assert legacy[0][1].page_ref == "claims/small-tools-beat-frameworks.md"


def test_auto_diversifies_pages_and_preserves_prefetch_privacy(
        fixture_wiki, make_page):
    _unit_page(fixture_wiki, make_page)
    private = make_page(
        fixture_wiki, "concepts/private-units.md", "concept", "Private units",
        extras={"privacyTier": "private"},
        body="A private heliotrope procedure.\n")
    assert private.is_file()
    index = WikiIndex(fixture_wiki)
    index.refresh()

    scored = search.score_units(index, "release operations", granularity="auto")
    diversified = search._diversify_pages(scored)
    assert len({unit.page_ref for _score, unit in diversified}) == len(diversified)
    assert not any(
        unit.page_ref == "concepts/private-units.md"
        for unit in index.units("prefetch"))


def test_tool_searches_and_opens_exact_units(
        fixture_wiki, tmp_path, make_page):
    _unit_page(fixture_wiki, make_page)
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
    })
    provider.initialize("unit-session", hermes_home=str(tmp_path),
                        platform="cli")

    schema = tools.WIKI_SEARCH_SCHEMA["parameters"]["properties"]
    assert schema["granularity"]["enum"] == [
        "page", "claim", "evidence", "heading", "auto",
    ]
    assert "as_of" in schema and "include_historical" in schema

    claim_result = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "starlight release quorum", "granularity": "claim",
    }))
    assert claim_result["ok"] and claim_result["count"] == 1
    claim_hit = claim_result["results"][0]
    assert claim_hit["ref"].endswith("#claim:claim.release.current")
    assert claim_hit["unit_kind"] == "claim"
    assert claim_hit["retrieval_trace"]["matched_terms"] == [
        "quorum", "release", "starlight"]
    assert claim_hit["retrieval_trace"]["temporal_state"] == "current"

    opened_claim = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": claim_hit["ref"]}))
    assert opened_claim["ok"]
    assert opened_claim["claim"]["id"] == "claim.release.current"

    evidence_result = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "two operators production releases",
        "granularity": "evidence",
    }))
    evidence_hit = evidence_result["results"][0]
    opened_evidence = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": evidence_hit["ref"]}))
    assert opened_evidence["evidence_anchor"] == \
        "sources/release-runbook.md#L10-L12"
    assert opened_evidence["evidence"]["excerptHash"].startswith("sha256:")

    hidden = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "moonlight legacy protocol", "granularity": "claim",
    }))
    assert hidden["results"] == []
    hidden_auto = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "moonlight legacy protocol",
    }))
    assert hidden_auto["results"] == []
    assert "moonlight" not in provider.prefetch(
        "moonlight legacy protocol").lower()
    historical = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "moonlight legacy protocol", "granularity": "claim",
        "include_historical": True,
    }))
    assert historical["results"][0]["temporal_state"] == "expired"
    past = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "moonlight legacy protocol", "granularity": "claim",
        "as_of": "2019-01-01",
    }))
    assert past["results"][0]["temporal_state"] == "current"


def test_qmd_candidates_are_enriched_from_local_units(
        fixture_wiki, tmp_path, make_page, monkeypatch):
    _unit_page(fixture_wiki, make_page)
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "search_backend": "qmd",
        "qmd_state_dir": str(tmp_path / "qmd"), "embeddings": False,
    })
    provider.initialize("qmd-units", hermes_home=str(tmp_path), platform="cli")
    monkeypatch.setattr(provider.qmd, "search", lambda *args, **kwargs: [{
        "ref": "concepts/release-units.md", "title": "Release units",
        "type": "concept", "score": 0.9, "relevance": 0.9,
        "snippet": "page candidate", "backend": "qmd",
    }])

    result = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "starlight release quorum", "granularity": "claim",
    }))

    assert result["backend"] == "qmd"
    assert result["results"][0]["ref"].endswith(
        "#claim:claim.release.current")
    assert result["results"][0]["unit_backend"] == "local"


def test_invalid_temporal_search_arguments_fail_cleanly(
        fixture_wiki, tmp_path):
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
    })
    provider.initialize("invalid-as-of", hermes_home=str(tmp_path),
                        platform="cli")

    invalid_date = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "zettelkasten", "as_of": "not-a-date",
    }))
    invalid_history = json.loads(provider.handle_tool_call("wiki_search", {
        "query": "zettelkasten", "include_historical": "yes",
    }))

    assert not invalid_date["ok"] and "as_of" in invalid_date["error"]
    assert not invalid_history["ok"] and "boolean" in invalid_history["error"]


def test_evaluator_matches_page_claim_and_unit_identifiers(
        fixture_wiki, cfg, make_page):
    _unit_page(fixture_wiki, make_page)
    unit_ref = "concepts/release-units.md#claim:claim.release.current"
    case = evaluation.QueryCase(
        "current-claim", "starlight release quorum",
        (evaluation.ExpectedResult(
            "concepts/release-units.md",
            claim_id="claim.release.current", unit_id=unit_ref),),
        granularity="claim",
    )

    metrics = evaluation.evaluate(
        cfg, (case,), backend="local")["backends"]["local"]["metrics"]

    assert metrics["recall_at_1"] == 1.0
