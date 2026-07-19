from pathlib import Path

import pytest

from memory_wiki import candidates, review_briefing, scaffold
from memory_wiki.index import WikiIndex


def _snapshot(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_review_briefing_is_bounded_and_read_only(cfg):
    scaffold.ensure_runtime(cfg)
    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    staged = candidates.stage(
        cfg, index, "Prefers compact release notes with concrete examples.",
        kind="note", origin="tool", session_id="session-secret")
    assert staged["staged"]
    lock_path = Path(cfg.wiki_root) / "_inbox" / ".lock"
    lock_path.unlink(missing_ok=True)
    before = _snapshot(Path(cfg.wiki_root))

    result = review_briefing.build(
        cfg, index, filters={"origin": "tool", "session": "session-secret"},
        limit=500)

    assert result["summary"]["returned"] == 1
    assert result["summary"]["by_origin"] == {"tool": 1}
    item = result["items"][0]
    assert item["ids"] == [staged["id"]]
    assert item["bucket"] in result["buckets"]
    assert item["candidates"][0]["content"].startswith("Prefers compact")
    assert _snapshot(Path(cfg.wiki_root)) == before
    assert not lock_path.exists()


def test_review_briefing_rejects_bad_limit(cfg):
    with pytest.raises(ValueError, match="at least 1"):
        review_briefing.build(cfg, None, limit=0)
