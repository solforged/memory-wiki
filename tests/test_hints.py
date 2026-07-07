import json

import pytest

from memory_wiki import candidates, hints, layers, promote, scaffold
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def cand(content, kind="fact", target="", cand_id="c1"):
    return {"id": cand_id, "kind": kind, "target": target, "content": content}


def test_default_target_from_kind(ready):
    _, idx = ready
    h = hints.candidate_hints(idx, cand("x y z w", kind="preference"))
    assert h["default_target"] == "profile/user.md"
    h = hints.candidate_hints(idx, cand("x y z w", kind="decision"))
    assert h["default_target"].startswith("journal/")
    h = hints.candidate_hints(idx, cand("x y z w", target="refs/foo.md"))
    assert h["default_target"] == "refs/foo.md"


def test_covered_by_profile_line(ready):
    cfg, idx = ready
    layers.write_profile(cfg, "user", "Deploys only on Fridays",
                         section="## Preferences")
    idx.refresh()
    h = hints.candidate_hints(idx, cand("deploys only on fridays"))
    assert "profile/user.md" in h["covered_by"]


def test_covered_by_taste_page(ready, make_page):
    cfg, idx = ready
    make_page(cfg.wiki_root, "tastes/tastes.md", "taste", "Tastes",
              body="- Strong signal for synthwave and dark ambient\n")
    idx.refresh()
    h = hints.candidate_hints(idx, cand("strong signal for synthwave and "
                                        "dark ambient"))
    assert "tastes/tastes.md" in h["covered_by"]


def test_covered_by_promoted_candidate(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "The office wifi password rotates monthly.",
                         kind="note")
    promote.approve(cfg, idx, r["id"])
    idx.refresh()
    h = hints.candidate_hints(idx, cand("office wifi password rotates monthly"))
    assert any(r["id"] in ref for ref in h["covered_by"])


def test_suggested_targets(ready):
    _, idx = ready
    h = hints.candidate_hints(idx, cand(
        "Set port 8080 in ~/Library/LaunchAgents/com.foo.plist"))
    assert h["suggested_target"] == "profile/agent.md"
    h = hints.candidate_hints(idx, cand(
        "Great guide at https://example.com/notes worth keeping"))
    assert h["suggested_target"].startswith("refs/")
    h = hints.candidate_hints(idx, cand(
        "Loves the Disco Elysium soundtrack album"))
    assert h["suggested_target"] == "tastes/tastes.md"
    # no suggestion when it matches the explicit target already
    h = hints.candidate_hints(idx, cand(
        "Loves the Disco Elysium soundtrack album", target="tastes/tastes.md"))
    assert "suggested_target" not in h


def test_warnings(ready):
    _, idx = ready
    h = hints.candidate_hints(idx, cand("User prefers detailed commit "
                                        "messages that explain the why..."))
    assert "truncated" in h["warnings"]
    h = hints.candidate_hints(idx, cand("Let's start with three."))
    assert "needs_context" in h["warnings"]


def test_similar_pending_candidates(ready):
    _, idx = ready
    pending = [cand("User prefers uv over pip for python", cand_id="a"),
               cand("prefers uv over pip always", cand_id="b")]
    h = hints.candidate_hints(idx, pending[0], pending)
    assert h["similar_candidates"] == ["b"]


def test_review_queue_surfaces_hints(fixture_wiki, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    provider.initialize("8f3e9c12-sess", hermes_home=str(tmp_path),
                        platform="cli")
    out = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "stage", "kind": "note",
        "content": "Loves the Disco Elysium soundtrack album..."}))
    assert out["ok"]
    queue = json.loads(provider.handle_tool_call("wiki_get",
                                                 {"ref": "review-queue"}))
    assert queue["count"] == 1
    item = queue["candidates"][0]
    assert item["target"] == ""
    assert item["default_target"] == "profile/agent.md"
    assert item["suggested_target"] == "tastes/tastes.md"
    assert "truncated" in item["warnings"]
