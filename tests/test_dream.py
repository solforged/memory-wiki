import json
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import pytest

from memory_wiki import candidates, cli, dream, layers, scaffold, wikifs
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def stage(cfg, idx, content, **kw):
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


def test_dream_tool_read_and_apply_safe(fixture_wiki, tmp_path):
    import memory_wiki

    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki), "embeddings": False})
    provider.initialize("8f3e9c12-sess", hermes_home=str(tmp_path),
                        platform="cli")

    staged = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "stage",
        "kind": "preference",
        "confidence": "likely",
        "content": "Prefers terse CLI output.",
    }))
    assert staged["ok"]

    plan = json.loads(provider.handle_tool_call("wiki_get", {"ref": "dream"}))
    assert plan["ok"] and plan["summary"]["promote_profile"] == 1

    applied = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "dream", "apply_safe": True}))
    assert applied["ok"]
    assert applied["applied"][0]["id"] == staged["id"]
    assert "Prefers terse CLI output." in provider.system_prompt_block()


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
