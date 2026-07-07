import sys
from argparse import Namespace
from pathlib import Path

import pytest

from memory_wiki import candidates, cli, scaffold, wikifs
from memory_wiki.index import WikiIndex


def ns(**kw):
    base = dict(list_only=False, approve=[], reject=[], reason="",
                prune=False, days=30, content=None, target=None,
                section=None, confidence=None, topic=[], field=[])
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
