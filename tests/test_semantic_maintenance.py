import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_wiki import (
    candidates, cli, scaffold, semantic_maintenance as semantic, tools,
    wikifs,
)
from memory_wiki.index import WikiIndex


def unit(ref, text, claim=None):
    page_ref = ref.split("#", 1)[0]
    return SimpleNamespace(
        ref=ref, page_ref=page_ref,
        kind="claim" if claim is not None else "page",
        title=Path(page_ref).stem, text=text, claim=claim, page=None)


@pytest.mark.parametrize(("left", "right", "classification"), [
    (
        unit("claims/a.md", "Review candidates before promotion."),
        unit("claims/b.md", "Review candidates before promotion."),
        "duplicate",
    ),
    (
        unit("claims/a.md", "The plugin automatically recalls private raw records."),
        unit("claims/b.md", "The plugin does not automatically recall private raw records."),
        "contradicts",
    ),
    (
        unit("claims/a.md", "Memory review stages durable claims safely."),
        unit("claims/b.md", (
            "Memory review stages durable claims safely with cited evidence "
            "and explicit approval.")),
        "refines",
    ),
    (
        unit("claims/a.md", "Evidence anchors improve claim review and provenance checks."),
        unit("claims/b.md", "Claim review and evidence anchors support source verification."),
        "supports",
    ),
    (
        unit("claims/a.md#claim:claim.alpha.old", "The service uses port 8000.", {
            "id": "claim.alpha.old", "text": "The service uses port 8000.",
        }),
        unit("claims/b.md#claim:claim.alpha.new", "The service uses port 9000.", {
            "id": "claim.alpha.new", "text": "The service uses port 9000.",
            "supersedes": "claim.alpha.old",
        }),
        "temporal_successor",
    ),
    (
        unit("claims/a.md", "Database migration locking behavior."),
        unit("claims/b.md", "Watercolor pigments for landscapes."),
        "unrelated",
    ),
])
def test_local_classifier_covers_relation_vocabulary(left, right, classification):
    result = semantic.classify_pair(left, right)
    assert result["classification"] == classification
    assert 0 <= result["confidence"] <= 1
    assert result["rationale"]
    assert result["classifier_source"] == "local"


def test_optional_classifier_is_injectable_and_fail_soft():
    left = unit("claims/a.md", "Alpha beta gamma delta.")
    right = unit("claims/b.md", "Unrelated words live here.")

    def unavailable(_request):
        raise RuntimeError("offline")

    fallback = semantic.classify_pair(left, right, llm_classifier=unavailable)
    assert fallback["classification"] == "unrelated"
    assert fallback["classifier_source"] == "local"

    def classify(request):
        assert "duplicate" in request["allowed_classifications"]
        return {
            "classification": "supports", "confidence": 0.81,
            "rationale": "The second statement supplies supporting context.",
        }

    overridden = semantic.classify_pair(
        left, right, llm_classifier=classify)
    assert overridden == {
        "classification": "supports", "confidence": 0.81,
        "rationale": "The second statement supplies supporting context.",
        "classifier_source": "llm",
    }


def _add_duplicate_claims(root, make_page):
    text = "Review-first memory staging prevents unreviewed durable writes."
    for name in ("alpha", "beta"):
        make_page(
            root, f"claims/{name}.md", "claim", name,
            topics=("memory",), body=f"Argument for {name}.\n",
            extras={"claims": [{
                "id": f"claim.{name}.primary", "text": text,
                "evidence": [{"kind": "test", "note": name}],
            }]})


def _snapshot(root, *, include_inbox=True):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and (include_inbox or "_inbox" not in path.parts)
    }


def _duplicate_claim_finding(findings):
    return next(
        item for item in findings
        if item["classification"] == "duplicate"
        and item["left"]["unit_kind"] == "claim"
        and item["right"]["unit_kind"] == "claim")


def test_generation_is_deterministic_bound_and_read_only(cfg, make_page):
    root = Path(cfg.wiki_root)
    _add_duplicate_claims(root, make_page)
    index = WikiIndex(root)
    before = _snapshot(root)

    first = semantic.generate_findings(index, min_similarity=0.2)
    second = semantic.generate_findings(index, min_similarity=0.2)

    assert first == second
    finding = _duplicate_claim_finding(first)
    assert finding["classifier_version"] == semantic.CLASSIFIER_VERSION
    assert finding["finding_hash"].startswith("sha256:")
    assert finding["left"]["content_hash"].startswith("sha256:")
    assert finding["bindings"]["contents"][finding["left"]["ref"]] == (
        finding["left"]["content_hash"])
    assert _snapshot(root) == before


def test_stale_content_classifier_and_tampering_are_rejected(cfg, make_page):
    root = Path(cfg.wiki_root)
    scaffold.ensure_runtime(cfg)
    _add_duplicate_claims(root, make_page)
    index = WikiIndex(root)
    finding = _duplicate_claim_finding(
        semantic.generate_findings(index, min_similarity=0.2))

    with pytest.raises(semantic.StaleFindingError, match="classifier version"):
        semantic.validate_finding(
            index, finding, classifier_version="semantic-maintenance/2")

    tampered = dict(finding)
    tampered["rationale"] = "Trust me."
    with pytest.raises(semantic.StaleFindingError, match="binding changed"):
        semantic.validate_finding(index, tampered)

    path = root / finding["left"]["page_ref"]
    fm, body = wikifs.parse_frontmatter(path.read_text(encoding="utf-8"))
    fm["claims"][0]["text"] += " Updated."
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    index.refresh()
    with pytest.raises(semantic.StaleFindingError, match="content changed"):
        semantic.validate_finding(index, finding)

    rejected = semantic.stage_accepted_finding(
        cfg, index, finding, accept=True)
    assert rejected["staged"] is False
    assert "stale finding" in rejected["reason"]
    assert list((root / "_inbox").glob("*.md")) == []


def test_staging_requires_opt_in_and_only_creates_pending_candidate(
        cfg, make_page):
    root = Path(cfg.wiki_root)
    scaffold.ensure_runtime(cfg)
    _add_duplicate_claims(root, make_page)
    index = WikiIndex(root)
    finding = _duplicate_claim_finding(
        semantic.generate_findings(index, min_similarity=0.2))
    knowledge_before = _snapshot(root, include_inbox=False)

    declined = semantic.stage_accepted_finding(cfg, index, finding)
    assert declined == {
        "staged": False, "reason": "explicit accept=True is required"}
    assert list((root / "_inbox").glob("*.md")) == []

    staged = semantic.stage_accepted_finding(
        cfg, index, finding, accept=True, session_id="semantic-session")
    assert staged["staged"] is True
    assert staged["finding_id"] == finding["id"]
    loaded = candidates.load(cfg, staged["id"])
    assert loaded is not None
    _path, fm, content, evidence = loaded
    assert fm["status"] == "pending"
    assert fm["target"] == finding["left"]["page_ref"]
    assert content.startswith("Semantic review:")
    assert any(finding["id"] in line for line in evidence)
    assert _snapshot(root, include_inbox=False) == knowledge_before


def test_unrelated_findings_cannot_be_staged(cfg, make_page):
    root = Path(cfg.wiki_root)
    scaffold.ensure_runtime(cfg)
    make_page(root, "claims/alpha.md", "claim", "alpha",
              body="Database migration locking behavior.\n")
    make_page(root, "claims/beta.md", "claim", "beta",
              body="Watercolor pigments for landscapes.\n")
    index = WikiIndex(root)
    findings = semantic.generate_findings(
        index, include_unrelated=True, min_similarity=0)
    finding = next(item for item in findings
                   if item["classification"] == "unrelated")
    result = semantic.stage_accepted_finding(
        cfg, index, finding, accept=True)
    assert result == {
        "staged": False, "reason": "unrelated findings are not actionable"}
    assert list((root / "_inbox").glob("*.md")) == []


def _maintain_args(**overrides):
    values = {
        "limit": 50, "include_unrelated": False, "json": False,
        "accept": "", "yes": False, "wiki_root": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_cli_parser_and_read_only_json_listing(
        cfg, make_page, monkeypatch, capsys):
    root = Path(cfg.wiki_root)
    _add_duplicate_claims(root, make_page)
    monkeypatch.setattr(cli, "_load_cfg", lambda *_args, **_kwargs: cfg)
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    cli.register_cli(commands.add_parser("memory-wiki"))
    parsed = parser.parse_args([
        "memory-wiki", "maintain", "--limit", "7", "--json",
    ])
    assert parsed.limit == 7 and parsed.json is True
    before = _snapshot(root)

    assert cli._cmd_maintain(_maintain_args(limit=7, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert payload["classifier_version"] == semantic.CLASSIFIER_VERSION
    assert payload["count"] == len(payload["findings"])
    assert _duplicate_claim_finding(payload["findings"])
    assert _snapshot(root) == before


def test_cli_accept_requires_yes_regenerates_and_only_stages_pending(
        cfg, make_page, monkeypatch, capsys):
    root = Path(cfg.wiki_root)
    scaffold.ensure_runtime(cfg)
    _add_duplicate_claims(root, make_page)
    index = WikiIndex(root)
    finding = _duplicate_claim_finding(
        semantic.generate_findings(index, min_similarity=0.2))
    monkeypatch.setattr(cli, "_load_cfg", lambda *_args, **_kwargs: cfg)
    knowledge_before = _snapshot(root, include_inbox=False)

    assert cli._cmd_maintain(_maintain_args(accept=finding["id"])) == 1
    assert "--accept requires --yes" in capsys.readouterr().out
    assert list((root / "_inbox").glob("*.md")) == []

    assert cli._cmd_maintain(_maintain_args(
        accept="semantic-missing", yes=True)) == 1
    assert "not found after regeneration" in capsys.readouterr().out
    assert list((root / "_inbox").glob("*.md")) == []

    assert cli._cmd_maintain(_maintain_args(
        accept=finding["id"], yes=True)) == 0
    assert "pending review" in capsys.readouterr().out
    staged = candidates.list_candidates(cfg)
    assert len(staged) == 1
    assert staged[0]["status"] == "pending"
    assert any(finding["id"] in line
               for line in candidates.load(cfg, staged[0]["id"])[3])
    assert _snapshot(root, include_inbox=False) == knowledge_before


def test_wiki_get_semantic_maintenance_is_read_only(cfg, make_page):
    root = Path(cfg.wiki_root)
    _add_duplicate_claims(root, make_page)
    provider = SimpleNamespace(cfg=cfg, index=WikiIndex(root))
    before = _snapshot(root)

    result = json.loads(tools.handle(provider, "wiki_get", {
        "ref": "semantic-maintenance", "limit": 10,
    }))

    assert result["ok"] is True
    assert result["ref"] == "semantic-maintenance"
    assert result["classifier_version"] == semantic.CLASSIFIER_VERSION
    assert _duplicate_claim_finding(result["findings"])
    assert result["by_classification"]["duplicate"] >= 1
    assert _snapshot(root) == before
    props = tools.WIKI_GET_SCHEMA["parameters"]["properties"]
    assert "include_unrelated" in props
    apply_actions = tools.WIKI_APPLY_SCHEMA["parameters"]["properties"][
        "action"]["enum"]
    assert "semantic-maintenance" not in apply_actions
