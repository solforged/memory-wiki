import json
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_wiki import candidates, cli, dream, layers, recall, scaffold, wikifs
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def stage(cfg, idx, content, **kw):
    kw.setdefault("origin", "sync_turn")
    if kw["origin"] in {"sync_turn", "pre_compress", "session_end", "llm"}:
        kw.setdefault("evidence", [f'> [user, turn 1] "{content}"'])
    result = candidates.stage(cfg, idx, content, **kw)
    assert result["staged"], result
    return result["id"]


def actions_by_id(plan):
    out = {}
    for suggestion in plan["suggestions"]:
        if suggestion.get("id"):
            out[suggestion["id"]] = suggestion
    return out


def group_actions(plan):
    return [s for s in plan["suggestions"] if s.get("ids")]


def qualify(cfg, cand_id):
    loaded = candidates.load(cfg, cand_id)
    assert loaded is not None
    hit = {"type": "candidate", "id": cand_id, "relevance": 1.0,
           "content_hash": loaded[1]["hash"]}
    for i in range(10):
        recall.record(cfg, [hit], f"organic query {i}",
                      session_id=f"session-{i}")


def test_dream_plan_classifies_pending_candidates(ready):
    cfg, idx = ready
    layers.write_profile(cfg, "user", "Prefers detailed commit messages",
                         section="## Preferences")
    covered = stage(cfg, None, "prefers detailed commit messages",
                    kind="preference", confidence="likely")
    similar_a = stage(cfg, None, "Prefers uv over pip for python",
                      kind="preference", confidence="likely")
    similar_b = stage(cfg, None, "prefers uv over pip always",
                      kind="preference", confidence="likely")
    retarget = stage(cfg, None,
                     "Set port 8080 in ~/Library/LaunchAgents/com.foo.plist",
                     kind="fact", confidence="likely")
    claim = stage(cfg, None, "Review-first staging beats direct writes for memory.",
                  kind="claim", confidence="likely")
    ready_pref = stage(cfg, None, "Prefers terse CLI output.",
                       kind="preference", confidence="likely")
    qualify(cfg, ready_pref)

    idx.refresh()
    plan = dream.run(cfg, idx)
    singles = actions_by_id(plan)
    groups = group_actions(plan)

    assert singles[covered]["action"] == "reject_covered"
    assert singles[retarget]["action"] == "retarget"
    assert singles[retarget]["suggested_target"] == "profile/agent.md"
    assert singles[claim]["action"] == "review_knowledge"
    assert singles[ready_pref]["action"] == "promote_profile"
    assert singles[ready_pref]["safe_apply"] is True
    assert any(set(g["ids"]) == {similar_a, similar_b}
               and g["action"] == "consolidate_similar" for g in groups)


def test_dream_apply_safe_is_conservative(ready):
    cfg, idx = ready
    safe = stage(cfg, None, "Prefers terse CLI output.",
                 kind="preference", confidence="likely")
    qualify(cfg, safe)
    plausible = stage(cfg, None, "Prefers verbose design notes.",
                      kind="preference", confidence="plausible")
    fact = stage(cfg, None, "The current test server runs on port 8123.",
                 kind="fact", confidence="likely")
    claim = stage(cfg, None, "Dream passes should never write knowledge pages.",
                  kind="claim", confidence="likely")
    correction = stage(cfg, None, "Actually wants headline summaries first.",
                       kind="correction", confidence="likely")

    idx.refresh()
    plan = dream.run(cfg, idx, apply_safe=True)

    applied = {a["id"]: a for a in plan["applied"]}
    assert set(applied) == {safe}
    assert applied[safe]["ok"] is True
    text = wikifs.read_text(Path(cfg.wiki_root) / "profile/user.md") or ""
    assert "Prefers terse CLI output." in text
    for cid in (plausible, fact, claim, correction):
        _, fm, _, _ = candidates.load(cfg, cid)
        assert fm["status"] == "pending"


def test_dream_reports_promotion_exceptions_without_losing_plan(
        ready, monkeypatch):
    cfg, idx = ready
    safe = stage(cfg, None, "Prefers terse CLI output.",
                 kind="preference", confidence="likely")
    qualify(cfg, safe)
    monkeypatch.setattr(
        dream.promote, "approve",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    plan = dream.run(cfg, idx, apply_safe=True)

    assert plan["applied"] == [{
        "id": safe,
        "action": "promote_profile",
        "ok": False,
        "result": "",
        "target": "profile/user.md",
        "error": "OSError: disk full",
    }]


def test_dream_tool_read_and_apply_safe(fixture_wiki, tmp_path):
    import memory_wiki

    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("8f3e9c12-sess", hermes_home=str(tmp_path),
                        platform="cli")

    provider.sync_turn("I prefer terse CLI output.", "Noted",
                       session_id=provider.session_id)
    staged = candidates.list_candidates(provider.cfg)[0]
    qualify(provider.cfg, staged["id"])

    plan = json.loads(provider.handle_tool_call("wiki_get", {"ref": "dream"}))
    assert plan["ok"] and plan["summary"]["promote_profile"] == 1

    applied = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "dream", "apply_safe": True}))
    assert applied["ok"]
    assert applied["applied"][0]["id"] == staged["id"]
    assert "prefer terse CLI output" in provider.system_prompt_block()


def test_dream_never_auto_promotes_ungrounded_tool_candidate(ready):
    cfg, idx = ready
    cand_id = candidates.stage(
        cfg, idx, "Prefers terse CLI output.", kind="preference",
        confidence="likely", origin="tool")["id"]
    qualify(cfg, cand_id)
    plan = dream.run(cfg, idx, apply_safe=True)
    suggestion = actions_by_id(plan)[cand_id]
    assert suggestion["action"] == "needs_review"
    assert suggestion["safe_apply"] is False
    assert plan["applied"] == []


def test_dream_never_auto_promotes_llm_inference(ready):
    cfg, idx = ready
    cand_id = candidates.stage(
        cfg, idx, "I prefer terse output.", kind="preference",
        confidence="likely", origin="llm",
        evidence=['> [user, turn 1] "I do not prefer terse output."'])["id"]
    qualify(cfg, cand_id)

    plan = dream.run(cfg, idx, apply_safe=True)

    suggestion = actions_by_id(plan)[cand_id]
    assert suggestion["action"] == "needs_review"
    assert suggestion["safe_apply"] is False
    assert plan["applied"] == []


def test_dream_limit_still_checks_duplicates_outside_output(ready):
    cfg, idx = ready
    first = stage(cfg, None, "Prefers terse CLI output for deploys.",
                  kind="preference", confidence="likely")
    second = stage(cfg, None, "Prefers terse CLI output during reviews.",
                   kind="preference", confidence="likely")
    qualify(cfg, first)

    plan = dream.run(cfg, idx, limit=1, apply_safe=True)

    suggestion = actions_by_id(plan)[first]
    assert suggestion["action"] == "consolidate_similar"
    assert second in suggestion["similar_candidates"]
    assert suggestion["safe_apply"] is False
    assert plan["applied"] == []


def test_changed_candidate_content_invalidates_recall_evidence(ready):
    cfg, idx = ready
    cand_id = stage(cfg, None, "Prefers terse CLI output.",
                    kind="preference", confidence="likely")
    qualify(cfg, cand_id)
    path, fm, _content, evidence = candidates.load(cfg, cand_id)
    with wikifs.wiki_lock(cfg.wiki_root):
        candidates.set_status_unlocked(
            cfg, path, fm, "Prefers terse CLI output with colors.", evidence,
            "pending")

    plan = dream.run(cfg, idx, apply_safe=True)
    suggestion = actions_by_id(plan)[cand_id]
    assert suggestion["action"] == "defer_unproven"
    assert suggestion["promotion"]["recall_count"] == 0
    assert plan["applied"] == []


def test_dream_correction_overlap_suggests_supersede_not_reject(ready):
    cfg, idx = ready
    layers.write_profile(cfg, "user", "Uses black for code formatting",
                         section="## Preferences")
    correction = stage(cfg, None, "No longer uses black for code formatting",
                       kind="correction", confidence="likely")

    idx.refresh()
    plan = dream.run(cfg, idx, apply_safe=True)

    suggestion = actions_by_id(plan)[correction]
    assert suggestion["action"] == "supersede_existing"
    assert suggestion["covered_by"] == ["profile/user.md"]
    assert not suggestion.get("command")
    assert plan["applied"] == []


def test_dream_defers_unrecalled_and_rejects_explicit_profile_target(ready):
    cfg, idx = ready
    unrecalled = stage(cfg, None, "Prefers terse CLI output.",
                       kind="preference", confidence="likely")
    explicit = stage(cfg, None, "Prefers compact terminal output.",
                     kind="preference", confidence="likely",
                     target="profile/custom.md")
    qualify(cfg, explicit)

    idx.refresh()
    plan = dream.run(cfg, idx, apply_safe=True)
    singles = actions_by_id(plan)

    assert singles[unrecalled]["action"] == "defer_unproven"
    assert singles[unrecalled]["promotion"]["recall_count"] == 0
    assert singles[explicit]["safe_apply"] is False
    assert plan["applied"] == []


def test_promotion_gate_requires_distinct_queries_not_only_days(ready):
    cfg, _idx = ready
    cand = {"id": "candidate", "topics": ["one", "two"]}
    ranking = dream.promotion_score(cfg, cand, signals={
        "recall_count": 10,
        "total_score": 10.0,
        "query_hashes": ["one-query"],
        "recall_days": ["2026-07-12", "2026-07-13", "2026-07-14"],
        "last_recalled_at": datetime.now(timezone.utc).isoformat(),
    })
    assert ranking["unique_queries"] == 1
    assert ranking["gates"]["unique_queries"] is False
    assert ranking["eligible"] is False


def test_dream_group_all_covered_suggests_reject(ready):
    cfg, idx = ready
    layers.write_profile(cfg, "user", "Prefers detailed commit messages",
                         section="## Preferences")
    covered_a = stage(cfg, None, "prefers detailed commit messages",
                      kind="preference", confidence="likely")
    covered_b = stage(cfg, None, "user prefers detailed commit messages",
                      kind="preference", confidence="likely")

    idx.refresh()
    plan = dream.run(cfg, idx)

    group = next(g for g in group_actions(plan)
                 if set(g["ids"]) == {covered_a, covered_b})
    assert group["action"] == "reject_covered"
    assert group["covered_by"] == ["profile/user.md"]
    assert all(item["covered_by"] for item in group["excerpts"])
    assert f"--reject {covered_a}" in group["command"]
    assert f"--reject {covered_b}" in group["command"]


def test_dream_flags_stale_candidates(ready):
    cfg, idx = ready
    stale = stage(cfg, None, "Prefers terse CLI output.",
                  kind="preference", confidence="likely")
    path = candidates.find_path(cfg, stale)
    fm, body = wikifs.parse_frontmatter(path.read_text())
    fm["created"] = (date.today()
                     - timedelta(days=cfg.candidate_stale_days + 1)).isoformat()
    path.write_text(wikifs.render_page(fm, body))

    idx.refresh()
    plan = dream.run(cfg, idx, apply_safe=True)

    suggestion = actions_by_id(plan)[stale]
    assert suggestion["action"] == "reject_stale"
    assert f"--reject {stale}" in suggestion["command"]
    assert plan["applied"] == []


def test_recent_recall_keeps_old_candidate_out_of_stale_rejection(ready):
    cfg, idx = ready
    cand_id = stage(cfg, None, "Prefers terse CLI output.",
                    kind="preference", confidence="likely")
    path = candidates.find_path(cfg, cand_id)
    fm, body = wikifs.parse_frontmatter(path.read_text())
    fm["created"] = (date.today()
                     - timedelta(days=cfg.candidate_stale_days + 1)).isoformat()
    path.write_text(wikifs.render_page(fm, body))
    qualify(cfg, cand_id)

    plan = dream.run(cfg, idx)

    assert actions_by_id(plan)[cand_id]["action"] == "promote_profile"


def test_dream_cli_exit_code_on_failed_promotion(ready, monkeypatch, capsys):
    cfg, _idx = ready
    monkeypatch.setattr(cli, "_load_cfg", lambda *a, **k: cfg)
    plan = {"pending": 1, "considered": 1, "limit": 50, "summary": {},
            "suggestions": [],
            "applied": [{"id": "x", "action": "promote_profile", "ok": False,
                         "result": "", "target": "", "error": "boom"}]}
    monkeypatch.setattr(dream, "run", lambda *a, **k: plan)

    rc = cli._cmd_dream(Namespace(limit=None, apply_safe=True))
    out = capsys.readouterr().out
    assert rc == 1
    assert "applied safe promotions: 0/1" in out
    assert "ERROR boom" in out


def test_dream_cli_lists_and_applies_safe(ready, monkeypatch, capsys):
    cfg, _idx = ready
    stage(cfg, None, "Prefers terse CLI output.",
          kind="preference", confidence="likely")
    cand_id = candidates.list_candidates(cfg)[0]["id"]
    qualify(cfg, cand_id)
    monkeypatch.setattr(cli, "_load_cfg", lambda *a, **k: cfg)

    rc = cli._cmd_dream(Namespace(limit=None, apply_safe=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "promote_profile" in out
    assert "[safe]" in out

    rc = cli._cmd_dream(Namespace(limit=None, apply_safe=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "applied safe promotions: 1/1" in out
    assert "Prefers terse CLI output." in (wikifs.read_text(
        Path(cfg.wiki_root) / "profile/user.md") or "")


def test_dream_explain_and_report(ready, monkeypatch, capsys):
    cfg, idx = ready
    cand_id = stage(cfg, None, "Prefers terse CLI output.",
                    kind="preference", confidence="likely")
    qualify(cfg, cand_id)
    explained = dream.explain(cfg, idx, cand_id)
    assert explained["promotion"]["eligible"] is True
    assert explained["suggestion"]["action"] == "promote_profile"

    monkeypatch.setattr(cli, "_load_cfg", lambda *a, **k: cfg)
    rc = cli._cmd_dream(Namespace(
        limit=None, apply_safe=False, report=False, explain=cand_id))
    out = capsys.readouterr().out
    assert rc == 0 and "failed gates: none" in out

    rc = cli._cmd_dream(Namespace(
        limit=None, apply_safe=False, report=True, explain=None))
    out = capsys.readouterr().out
    assert rc == 0 and "report: DREAMS.md" in out
    report = wikifs.read_text(Path(cfg.wiki_root) / "DREAMS.md") or ""
    assert cand_id in report
    assert "never promotion inputs" in report
