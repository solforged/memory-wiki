import json
from pathlib import Path

import pytest

from memory_wiki import candidates, scaffold, schema, wikifs
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    idx = WikiIndex(cfg.wiki_root)
    idx.refresh()
    return cfg, idx


def test_stage_creates_valid_candidate(ready):
    cfg, idx = ready
    result = candidates.stage(cfg, idx, "User prefers uv over pip.",
                              kind="preference", origin="tool",
                              session_id="8f3e9c12-full", turn=3)
    assert result["staged"], result
    path = Path(cfg.wiki_root) / result["ref"]
    fm, body = wikifs.parse_frontmatter(path.read_text())
    assert schema.validate_candidate(fm) == []
    assert fm["id"] == result["id"]
    assert fm["session"] == "8f3e9c12-full" and fm["turn"] == "3"
    assert "8f3e9c12" in result["id"]
    assert "User prefers uv over pip." in body
    assert schema.EVIDENCE_HEADING in body
    decisions = (Path(cfg.wiki_root) / schema.DECISIONS_FILE).read_text()
    entry = json.loads(decisions.strip().splitlines()[-1])
    assert entry["action"] == "staged" and entry["id"] == result["id"]


def test_stage_seq_bumps_within_same_second(ready, monkeypatch):
    cfg, idx = ready
    monkeypatch.setattr(wikifs, "utc_stamp", lambda: "20260705-120000")
    r1 = candidates.stage(cfg, idx, "First fact about deployment.",
                          session_id="aabbccdd")
    r2 = candidates.stage(cfg, idx, "Second fact about testing.",
                          session_id="aabbccdd")
    assert r1["id"].endswith("-01") and r2["id"].endswith("-02")


def test_stage_dedup_by_hash(ready):
    cfg, idx = ready
    r1 = candidates.stage(cfg, idx, "User prefers uv over pip.")
    assert r1["staged"]
    r2 = candidates.stage(cfg, idx, "user prefers  UV over pip.")  # normalized
    assert not r2["staged"] and "duplicate" in r2["reason"]


def test_stage_dedup_vs_profile_and_titles(ready):
    cfg, idx = ready
    from memory_wiki import layers
    layers.write_profile(cfg, "user", "Deploys only on Fridays",
                         section="## Preferences")
    idx.refresh()
    r = candidates.stage(cfg, idx, "deploys only on fridays")
    assert not r["staged"] and "profile/user.md" in r["reason"]
    r2 = candidates.stage(cfg, idx, "small tools beat frameworks")
    assert not r2["staged"] and "claims/" in r2["reason"]


def test_stage_rejects_bad_enums_and_paths(ready):
    cfg, idx = ready
    assert not candidates.stage(cfg, idx, "x", kind="opinion")["staged"]
    assert not candidates.stage(cfg, idx, "x", confidence="sure")["staged"]
    assert not candidates.stage(cfg, idx, "  ")["staged"]
    with pytest.raises(ValueError):
        candidates.stage(cfg, idx, "escape", target="../../etc/x.md")


def test_stage_filters_fields_allowlist(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "A source worth keeping.",
                         kind="claim",
                         fields={"author": "Jane", "sneaky": "no"})
    path = Path(cfg.wiki_root) / r["ref"]
    fm, _ = wikifs.parse_frontmatter(path.read_text())
    assert fm["fields"] == {"author": "Jane"}


def test_multiline_content_survives_staging_and_loading(ready):
    cfg, idx = ready
    content = ("Claim body first paragraph.\n\n"
               "- bullet one\n- bullet two\n\n"
               "Closing paragraph with **markdown**.")
    r = candidates.stage(cfg, idx, content, kind="claim")
    assert r["staged"], r
    _, _, loaded, evidence = candidates.load(cfg, r["id"])
    assert loaded == content
    assert evidence
    listed = candidates.list_candidates(cfg)
    assert listed[0]["content"] == content


def test_long_content_not_truncated_and_oversize_rejected(ready):
    cfg, idx = ready
    long_content = "A sentence about deployment pipelines. " * 60  # ~2.4KB
    r = candidates.stage(cfg, idx, long_content, kind="note")
    assert r["staged"]
    _, _, loaded, _ = candidates.load(cfg, r["id"])
    assert loaded == long_content.strip()
    too_big = "x" * 50_001
    r2 = candidates.stage(cfg, idx, too_big)
    assert not r2["staged"] and "max 50000" in r2["reason"]


def test_split_evidence_last_heading_and_absent(ready):
    content = "Intro.\n\n## Evidence\n\n- the page's own evidence\n\nMore."
    body = content + "\n\n## Evidence\n\n> proposed via tool\n"
    head, evidence = candidates.split_evidence(body)
    assert head == content
    assert evidence == ["> proposed via tool"]
    # no separator: everything is content, no evidence
    assert candidates.split_evidence("just content\n") == ("just content", [])
    # mid-line and deeper headings do not split
    head2, ev2 = candidates.split_evidence(
        "mentions ## Evidence inline\n\n### Evidence\n\n## Evidence\n\n> e\n")
    assert head2.startswith("mentions") and "### Evidence" in head2
    assert ev2 == ["> e"]


def test_evidence_lines_sanitized(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "Fact with sneaky evidence.",
                         evidence=["- ok\n## Evidence\nsneaky"])
    assert r["staged"]
    _, _, content, evidence = candidates.load(cfg, r["id"])
    assert content == "Fact with sneaky evidence."
    assert all("\n" not in l for l in evidence)


def test_page_shaped_stage_validates(ready):
    cfg, idx = ready
    good = ("---\ntype: claim\ntitle: staged claims work\n"
            "topics: [memory]\nstance: model\nconfidence: likely\n"
            "maturity: seedling\ncreated: 2026-07-06\n---\n"
            "Body argument.\n\n## Evidence\n\n- some\n")
    r = candidates.stage(cfg, idx, good, kind="claim")
    assert r["staged"], r
    _, _, loaded, _ = candidates.load(cfg, r["id"])
    assert loaded == good.strip()
    assert "staged-claims-work" in r["ref"]  # slug from page title

    bad = "---\ntype: claim\ntitle: no stance\ntopics: [x]\ncreated: 2026-07-06\n---\nb\n"
    r2 = candidates.stage(cfg, idx, bad, kind="claim")
    assert not r2["staged"] and "stance" in r2["reason"]

    existing = "---\ntype: claim\ntitle: t\ntopics: [x]\ncreated: 2026-07-06\n---\nb\n"
    r3 = candidates.stage(cfg, idx, existing,
                          target="claims/small-tools-beat-frameworks.md")
    assert not r3["staged"] and "new-page target" in r3["reason"]


def test_list_load_and_prune(ready):
    cfg, idx = ready
    r = candidates.stage(cfg, idx, "Fact one about the system.")
    listed = candidates.list_candidates(cfg)
    assert len(listed) == 1 and listed[0]["id"] == r["id"]
    loaded = candidates.load(cfg, r["id"])
    assert loaded is not None
    path, fm, content, evidence = loaded
    assert content == "Fact one about the system."
    assert evidence  # the origin line

    # prune ignores pending, removes old rejected
    import os, time
    with wikifs.wiki_lock(cfg.wiki_root):
        candidates.set_status_unlocked(cfg, path, fm, content, evidence,
                                       "rejected", reviewed_note="no")
    old = time.time() - 40 * 86400
    os.utime(path, (old, old))
    assert candidates.prune(cfg, days=30) == [f"{schema.INBOX_DIR}/{path.name}"]
    assert candidates.load(cfg, r["id"]) is None


def test_concurrent_staging_two_processes(ready):
    import subprocess, sys
    cfg, idx = ready
    script = """
import importlib.util, sys, types
pkg = types.ModuleType("agent"); pkg.__path__ = []
stub = types.ModuleType("agent.memory_provider")
class MemoryProvider: pass
stub.MemoryProvider = MemoryProvider
sys.modules["agent"] = pkg; sys.modules["agent.memory_provider"] = stub
sys.path.insert(0, sys.argv[1])
from memory_wiki import candidates, config as mw_config
cfg = mw_config.load_config(None, {"wiki_root": sys.argv[2]})
r = candidates.stage(cfg, None, sys.argv[3], session_id=sys.argv[4])
print(r["id"] if r["staged"] else "FAIL")
"""
    repo = str(Path(__file__).resolve().parents[1])
    procs = [subprocess.run(
        [sys.executable, "-c", script, repo, str(cfg.wiki_root),
         f"Concurrent fact number {i} entirely distinct.", f"session{i}"],
        capture_output=True, text=True) for i in range(2)]
    ids = [p.stdout.strip() for p in procs]
    assert all(i and i != "FAIL" for i in ids), procs
    assert len(set(ids)) == 2
    assert len(candidates.list_candidates(cfg)) == 2
