import sys
import json
from datetime import date, timedelta
from types import SimpleNamespace
from argparse import ArgumentParser, Namespace
from pathlib import Path

import pytest

from memory_wiki import candidates, cli, hints, scaffold, tools, wikifs
from memory_wiki.index import WikiIndex


def ns(**kw):
    base = dict(list_only=False, approve=[], reject=[], reason="",
                prune=False, days=30, content=None, target=None,
                section=None, confidence=None, topic=[], field=[],
                kind=[], confidence_filter=[], target_filter=[],
                effective_target=[], stale_only=False, min_age=None,
                limit=None, approve_filtered=False, reject_filtered=False,
                yes=False)
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def ready(cfg, monkeypatch):
    scaffold.ensure_runtime(cfg)
    monkeypatch.setattr(cli, "_load_cfg", lambda *a, **k: cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def read(cfg, rel):
    return wikifs.read_text(Path(cfg.wiki_root) / rel)

def set_candidate_age(cfg, cand_id, days):
    path = candidates.find_path(cfg, cand_id)
    fm, body = wikifs.parse_frontmatter(path.read_text())
    fm["created"] = (date.today() - timedelta(days=days)).isoformat()
    wikifs.atomic_write(path, wikifs.render_page(fm, body))


def test_approve_with_content_override(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "user said something roughly like this",
                         kind="preference")
    rc = cli._cmd_review(ns(approve=[r["id"]],
                            content="Prefers evening deploys."))
    assert rc == 0
    text = read(cfg, "profile/user.md")
    assert "Prefers evening deploys." in text
    assert "roughly like this" not in text


def test_approve_with_fields_sets_claim_frontmatter(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "Something durable is true about testing.",
                         kind="claim", topics=["testing"])
    rc = cli._cmd_review(ns(approve=[r["id"]], confidence="likely",
                            field=["stance=external", "maturity=budding"]))
    assert rc == 0
    _, fm, _, _ = candidates.load(cfg, r["id"])
    text = read(cfg, fm["promoted_to"])
    page_fm, _ = wikifs.parse_frontmatter(text)
    assert page_fm["stance"] == "external"
    assert page_fm["maturity"] == "budding"
    assert page_fm["confidence"] == "likely"


def test_approve_retarget_plus_edit(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "Matuschak has notes on this somewhere.",
                         kind="note")
    rc = cli._cmd_review(ns(approve=[r["id"]],
                            target="claims/small-tools-beat-frameworks.md",
                            section="## Evidence",
                            content="Matuschak's notes support this."))
    assert rc == 0
    text = read(cfg, "claims/small-tools-beat-frameworks.md")
    evidence = text.split("## Evidence")[1].split("##")[0]
    assert "Matuschak's notes support this." in evidence


def test_overrides_require_single_approve(ready):
    cfg, idx = ready
    r1 = candidates.stage(cfg, idx, "First distinct fact about builds.")
    r2 = candidates.stage(cfg, idx, "Second distinct fact about deploys.")
    assert cli._cmd_review(ns(approve=[r1["id"], r2["id"]],
                              content="x")) == 1
    assert cli._cmd_review(ns(content="x")) == 1


def test_edit_content_uses_editor(monkeypatch, tmp_path):
    script = tmp_path / "ed.py"
    script.write_text(
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text(p.read_text().replace('old', 'new'))\n")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.delenv("VISUAL", raising=False)
    assert cli._edit_content("old text") == "new text"


def test_interactive_edit_fallback_without_editor(ready, monkeypatch):
    cfg, idx = ready
    candidates.stage(cfg, idx, "User said something roughly.",
                     kind="preference")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    answers = iter(["e", "Prefers precise notes."])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cli._cmd_review(ns()) == 0
    assert "Prefers precise notes." in read(cfg, "profile/user.md")


def test_cli_and_tool_share_exact_review_items(ready, monkeypatch):
    cfg, idx = ready
    candidates.stage(
        cfg, idx, "Prefers compact terminal review output with clear context.",
        kind="preference", origin="sync_turn", session_id="session-42", turn=7,
        section="## Current focus")
    tool_queue = json.loads(tools._get_review_queue(
        SimpleNamespace(cfg=cfg, index=idx)))
    captured = {}

    def capture(_cfg, _index, pending):
        captured["candidates"] = pending
        return 0

    monkeypatch.setattr(cli, "_review_interactive", capture)
    assert cli._cmd_review(ns()) == 0
    assert captured["candidates"] == tool_queue["candidates"]
    assert tool_queue["count"] == tool_queue["summary"]["matched"] == 1
    item = tool_queue["candidates"][0]
    assert item["title"] == (
        "Prefers compact terminal review output with clear context")
    assert item["content"].endswith("clear context.")
    assert item["origin"] == "sync_turn"
    assert item["session"] == "session-42"
    assert item["turn"] == 7
    assert item["explicit_target"] == ""
    assert item["effective_target"] == "profile/user.md"
    assert item["ref"].startswith("_inbox/")
    assert isinstance(item["warnings"], list)
    assert isinstance(item["covered_by"], list)
    assert isinstance(item["similar_candidates"], list)
    assert item["section"] == "## Current focus"
    assert item["effective_section"] == "## Current focus"


def test_review_queue_stale_target_filters_and_summary(ready):
    cfg, idx = ready
    old = candidates.stage(
        cfg, idx, "Prefers old terminal workflows with durable summaries.",
        kind="preference", confidence="likely")
    candidates.stage(
        cfg, idx, "Keep a fresh operations note for the current setup.",
        kind="note", confidence="plausible",
        target="refs/git-cheatsheet.md")
    set_candidate_age(cfg, old["id"], cfg.candidate_stale_days + 1)
    idx.refresh()

    queue = hints.build_review_queue(
        cfg, idx, filters={"target": "profile/user.md",
                           "stale_only": True,
                           "min_age": cfg.candidate_stale_days})
    assert [item["id"] for item in queue["candidates"]] == [old["id"]]
    assert queue["summary"] == {
        "total_pending": 2,
        "total_approved": 0,
        "total_reviewable": 2,
        "matched": 1,
        "stale_count": 1,
        "by_kind": {"preference": 1},
        "by_confidence": {"likely": 1},
        "by_effective_target": {"profile/user.md": 1},
    }

    explicit = hints.build_review_queue(
        cfg, idx, filters={"effective_target": "refs/git-cheatsheet.md"})
    assert explicit["summary"]["matched"] == 1
    assert explicit["candidates"][0]["explicit_target"] == (
        "refs/git-cheatsheet.md")
    tool_queue = json.loads(tools._get_review_queue(
        SimpleNamespace(cfg=cfg, index=idx),
        {"target": ["profile/user.md"], "stale_only": True,
         "min_age": cfg.candidate_stale_days}))
    assert tool_queue["candidates"] == queue["candidates"]
    assert tool_queue["summary"] == queue["summary"]

    limited = hints.build_review_queue(cfg, idx, limit=1)
    assert limited["summary"]["total_pending"] == 2
    assert limited["summary"]["matched"] == len(limited["candidates"]) == 1


def test_review_parser_accepts_queue_filters_and_batch_flags():
    parser = ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args([
        "review", "--kind", "note", "--confidence-filter", "likely",
        "--target-filter", "profile/agent.md", "--effective-target",
        "profile/agent.md", "--stale-only", "--min-age", "7", "--limit", "3",
        "--reject-filtered", "--reason", "stale", "--yes",
    ])
    assert args.kind == ["note"]
    assert args.confidence_filter == ["likely"]
    assert args.target_filter == ["profile/agent.md"]
    assert args.effective_target == ["profile/agent.md"]
    assert args.stale_only and args.min_age == 7 and args.limit == 3
    assert args.reject_filtered and args.reason == "stale" and args.yes


def test_filtered_batch_guards_do_not_mutate(ready, capsys):
    cfg, idx = ready
    staged = candidates.stage(
        cfg, idx, "A guarded batch candidate remains pending.",
        kind="fact")

    assert cli._cmd_review(ns(approve_filtered=True, kind=["fact"])) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert "--yes" in capsys.readouterr().out

    assert cli._cmd_review(ns(approve_filtered=True, yes=True)) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert "requires at least one filter" in capsys.readouterr().out

    assert cli._cmd_review(ns(approve_filtered=True, yes=True, limit=1)) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert "requires at least one filter" in capsys.readouterr().out

    assert cli._cmd_review(ns(approve_filtered=True, yes=True, kind=["fact"],
                              content="override")) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert "override flags cannot be used" in capsys.readouterr().out

    assert cli._cmd_review(ns(reject_filtered=True, yes=True, kind=["fact"],
                              list_only=True)) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert "cannot be combined with --list" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"approve_filtered": True, "yes": True, "kind": ["unknown"]},
         "unknown kind filter"),
        ({"approve_filtered": True, "yes": True,
          "confidence_filter": ["certain"]},
         "unknown confidence filter"),
        ({"approve_filtered": True, "yes": True, "min_age": -1},
         "minimum review age cannot be negative"),
        ({"approve_filtered": True, "yes": True, "limit": -1},
         "review queue limit must be at least 1"),
    ])
def test_invalid_filtered_batch_values_do_not_mutate(
        ready, capsys, args, message):
    cfg, idx = ready
    staged = candidates.stage(
        cfg, idx, "Invalid filters must not mutate this candidate.",
        kind="fact")
    assert cli._cmd_review(ns(**args)) == 1
    assert candidates.load(cfg, staged["id"])[1]["status"] == "pending"
    assert message in capsys.readouterr().out


def test_tool_rejects_invalid_queue_enum(ready):
    cfg, idx = ready
    out = json.loads(tools.handle(
        SimpleNamespace(cfg=cfg, index=idx), "wiki_get",
        {"ref": "review-queue", "kind": ["unknown"]}))
    assert not out["ok"]
    assert "unknown kind filter" in out["error"]


def test_filtered_batch_approve_and_reject(ready, capsys):
    cfg, idx = ready
    approved = candidates.stage(
        cfg, idx, "Prefers explicit batch review confirmations.",
        kind="preference")
    rejected = candidates.stage(
        cfg, idx, "A temporary note should be rejected in this batch.",
        kind="note", confidence="speculative")

    assert cli._cmd_review(ns(
        approve_filtered=True, yes=True, kind=["preference"])) == 0
    assert candidates.load(cfg, approved["id"])[1]["status"] == "promoted"
    assert candidates.load(cfg, rejected["id"])[1]["status"] == "pending"
    assert "selected 1 candidate(s) for filtered approval" in (
        capsys.readouterr().out)

    assert cli._cmd_review(ns(
        reject_filtered=True, yes=True,
        confidence_filter=["speculative"], reason="temporary")) == 0
    rejected_fm = candidates.load(cfg, rejected["id"])[1]
    assert rejected_fm["status"] == "rejected"
    assert "temporary" in rejected_fm["reviewed"]
    assert "selected 1 candidate(s) for filtered rejection" in (
        capsys.readouterr().out)


def test_filtered_batch_stops_after_real_promotion_error(ready, capsys):
    cfg, idx = ready
    staged = [
        candidates.stage(cfg, idx, "First valid batch note.", kind="note"),
        candidates.stage(
            cfg, idx, "Second note has multiple lines.\n"
                      "It cannot be appended without an edit.",
            kind="note"),
        candidates.stage(cfg, idx, "Third valid batch note.", kind="note"),
    ]

    assert cli._cmd_review(ns(
        approve_filtered=True, yes=True, kind=["note"])) == 1
    assert candidates.load(cfg, staged[0]["id"])[1]["status"] == "promoted"
    assert candidates.load(cfg, staged[1]["id"])[1]["status"] == "pending"
    assert candidates.load(cfg, staged[2]["id"])[1]["status"] == "pending"
    output = capsys.readouterr().out
    assert "ERROR" in output and "one-line content override" in output
    assert "stopped after 1 of 3 candidate(s)" in output
