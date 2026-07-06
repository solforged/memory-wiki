import time
from pathlib import Path

import pytest

from memory_wiki import wikifs
from memory_wiki.wikifs import (LockTimeout, atomic_write, append_jsonl,
                                create_exclusive, parse_frontmatter,
                                render_frontmatter, render_page,
                                safe_wiki_path, slugify, wiki_lock)

REAL_WIKI = Path("/Users/admin/wiki")


def roundtrip(fm, body="Body.\n"):
    text = render_page(fm, body)
    fm2, body2 = parse_frontmatter(text)
    return text, fm2, body2


def test_simple_roundtrip():
    fm = {"type": "concept", "title": "Zettelkasten", "topics": ["note-taking"],
          "created": "2026-07-01"}
    _, fm2, body2 = roundtrip(fm)
    assert fm2 == fm
    assert body2.lstrip("\n") == "Body.\n"


def test_quoted_title_with_colon():
    fm = {"type": "source", "title": "Molecular Notes: Principles",
          "author": "Robert Martin", "medium": "post", "topics": [],
          "created": "2026-07-01"}
    text, fm2, _ = roundtrip(fm)
    assert 'title: "Molecular Notes: Principles"' in text
    assert fm2["title"] == "Molecular Notes: Principles"
    assert fm2["topics"] == []


def test_empty_string_value_roundtrip():
    fm = {"id": "x", "target": "", "created": "2026-07-01"}
    text, fm2, _ = roundtrip(fm)
    assert 'target: ""' in text
    assert fm2["target"] == ""


def test_nested_fields_dict_roundtrip():
    fm = {"id": "c1", "fields": {"author": "Jane Doe", "medium": "post"},
          "hash": "abc"}
    text, fm2, _ = roundtrip(fm)
    assert "fields:\n  author: Jane Doe\n  medium: post" in text
    assert fm2["fields"] == {"author": "Jane Doe", "medium": "post"}
    assert fm2["hash"] == "abc"


def test_empty_dict_and_list():
    fm = {"fields": {}, "supports": []}
    _, fm2, _ = roundtrip(fm)
    assert fm2["fields"] == {}
    assert fm2["supports"] == []


def test_dash_list_parsing():
    text = "---\ntopics:\n- alpha\n- beta\ntitle: x\n---\nBody.\n"
    fm, _ = parse_frontmatter(text)
    assert fm["topics"] == ["alpha", "beta"]
    assert fm["title"] == "x"


def test_comment_stripping_matches_check_py():
    text = '---\nconfidence: likely  # speculative | plausible\n---\n'
    fm, _ = parse_frontmatter(text)
    assert fm["confidence"] == "likely"


def test_no_frontmatter():
    fm, body = parse_frontmatter("just text")
    assert fm is None and body == "just text"


@pytest.mark.skipif(not REAL_WIKI.is_dir(), reason="no real wiki on this machine")
def test_real_wiki_pages_roundtrip():
    checked = 0
    for sub in ("concepts", "claims", "sources", "atlases", "refs"):
        for page in sorted((REAL_WIKI / sub).glob("*.md")):
            text = page.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            assert fm is not None, page
            assert fm.get("type"), page
            # render -> reparse must preserve every field
            fm2, _ = parse_frontmatter(render_page(fm, body))
            assert fm2 == fm, page
            checked += 1
    assert checked >= 5


def test_atomic_write_and_read(tmp_path):
    p = tmp_path / "sub" / "x.md"
    atomic_write(p, "one")
    atomic_write(p, "two")
    assert p.read_text() == "two"
    assert wikifs.read_text(tmp_path / "missing.md") is None
    assert not list(p.parent.glob(".*tmp*"))


def test_create_exclusive(tmp_path):
    p = tmp_path / "c.md"
    assert create_exclusive(p, "a") is True
    assert create_exclusive(p, "b") is False
    assert p.read_text() == "a"


def test_append_jsonl(tmp_path):
    import json
    p = tmp_path / "d.jsonl"
    append_jsonl(p, {"a": 1})
    append_jsonl(p, {"b": "x"})
    lines = p.read_text().strip().splitlines()
    assert [json.loads(l) for l in lines] == [{"a": 1}, {"b": "x"}]


_HOLD_LOCK_SCRIPT = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("wf", sys.argv[1])
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)
with wf.wiki_lock(sys.argv[2], timeout=5):
    print("HELD", flush=True)
    time.sleep(1.0)
"""


def test_lock_contention_two_processes(tmp_path):
    import subprocess
    import sys
    wikifs_path = Path(wikifs.__file__)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK_SCRIPT, str(wikifs_path), str(tmp_path)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "HELD"
        t0 = time.monotonic()
        with pytest.raises(LockTimeout):
            with wiki_lock(tmp_path, timeout=0.2):
                pass
        assert time.monotonic() - t0 < 5
        with wiki_lock(tmp_path, timeout=5):  # acquires once child releases
            pass
    finally:
        proc.wait(10)


def test_safe_wiki_path(tmp_path):
    (tmp_path / "claims").mkdir()
    p = safe_wiki_path(tmp_path, "claims/foo.md")
    assert p == tmp_path.resolve() / "claims" / "foo.md"
    for bad in ("/etc/passwd", "../out.md", "claims/../../out.md", "", "~/x"):
        with pytest.raises(ValueError):
            safe_wiki_path(tmp_path, bad)


def test_slugify():
    assert slugify("Prefers uv over pip!") == "prefers-uv-over-pip"
    assert slugify("  --- ") == "note"
    long = slugify("word " * 40)
    assert len(long) <= 60 and not long.endswith("-")
