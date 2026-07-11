from pathlib import Path

from memory_wiki import scaffold, schema, wikifs
from memory_wiki import config as mw_config


def test_ensure_runtime_creates_and_is_idempotent(cfg):
    made = scaffold.ensure_runtime(cfg)
    assert "profile/user.md" in made and "_inbox/" in made
    root = Path(cfg.wiki_root)
    for rel in ("profile/user.md", "profile/agent.md"):
        text = wikifs.read_text(root / rel)
        assert schema.validate_page(rel, text) == []
    assert scaffold.ensure_runtime(cfg) == []


def test_init_wiki_patches_and_is_idempotent(cfg):
    report = scaffold.init_wiki(cfg)
    assert report["check_py"][0] == "applied"
    assert report["style_guide"][0] == "applied"
    root = Path(cfg.wiki_root)
    check_text = (root / "_scripts" / "check.py").read_text()
    assert '"profile": "profile", "journal": "journal"' in check_text
    style = (root / "_meta" / "style-guide.md").read_text()
    assert style.count(schema.STYLE_GUIDE_BEGIN) == 1
    assert style.startswith("# Wiki style guide")  # original preserved

    again = scaffold.init_wiki(cfg)
    assert again["check_py"][0] == "already"
    assert again["style_guide"][0] == "already"
    assert (root / "_meta" / "style-guide.md").read_text() == style


def test_init_dry_run_changes_nothing(cfg):
    root = Path(cfg.wiki_root)
    before_check = (root / "_scripts" / "check.py").read_text()
    report = scaffold.init_wiki(cfg, dry_run=True)
    assert report["check_py"][0] == "would-apply"
    assert report["check_py"][1]  # non-empty diff
    assert "profile/user.md" in report["created"]
    assert (root / "_scripts" / "check.py").read_text() == before_check
    assert not (root / "profile").exists()
    assert not (root / schema.INBOX_DIR).exists()


def test_writable_runtime_recovers_but_dry_run_does_not(cfg):
    import json

    from memory_wiki import candidates

    scaffold.ensure_runtime(cfg)
    staged = candidates.stage(
        cfg, None, "Runtime initialization repairs lifecycle state.")
    audit = Path(cfg.wiki_root) / schema.DECISIONS_FILE
    audit.write_text("")
    scaffold.init_wiki(cfg, dry_run=True)
    assert audit.read_text() == ""
    assert scaffold.ensure_runtime(cfg) == []
    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [(e["action"], e["id"]) for e in entries] == [
        ("staged", staged["id"])]


def test_init_anchor_missing_leaves_file_alone(cfg):
    root = Path(cfg.wiki_root)
    check = root / "_scripts" / "check.py"
    check.write_text("# user rewrote this\nDIR_TYPE = {}\n")
    report = scaffold.init_wiki(cfg)
    assert report["check_py"][0] == "anchor-missing"
    assert check.read_text().startswith("# user rewrote this")


def test_patched_fake_check_py_still_runs(cfg):
    import subprocess, sys
    scaffold.init_wiki(cfg)
    check = Path(cfg.wiki_root) / "_scripts" / "check.py"
    proc = subprocess.run([sys.executable, str(check)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_lint_exit_codes(cfg, monkeypatch, capsys):
    from argparse import Namespace
    from memory_wiki import cli
    monkeypatch.setattr(cli, "_load_cfg", lambda *a, **k: cfg)
    root = Path(cfg.wiki_root)
    assert cli._cmd_lint(Namespace()) == 0
    assert "plugin checks: ok" in capsys.readouterr().out

    # plugin validation error -> nonzero
    bad = root / "claims" / "bad-claim.md"
    bad.write_text("no frontmatter here\n")
    assert cli._cmd_lint(Namespace()) == 1
    assert "missing frontmatter" in capsys.readouterr().out
    bad.unlink()

    # wiki's own checker failing -> nonzero even when plugin checks pass
    (root / "_scripts" / "check.py").write_text(
        "import sys\nprint('broken')\nsys.exit(1)\n")
    assert cli._cmd_lint(Namespace()) == 1


def test_config_defaults_and_overrides(tmp_path):
    c = mw_config.load_config(None, None)
    assert c.prefetch_limit == 5 and c.auto_capture is True
    c2 = mw_config.load_config(None, {
        "wiki_root": str(tmp_path), "auto_capture": "false",
        "prefetch_limit": "9", "llm_extraction": True})
    assert c2.wiki_root == tmp_path
    assert c2.auto_capture is False
    assert c2.prefetch_limit == 9
    assert c2.llm_extraction is True
    # unreadable/missing config.yaml degrades to defaults
    assert mw_config.load_config(tmp_path, None).prefetch_limit == 5


def test_provider_minimal_lifecycle(cfg, tmp_path):
    import memory_wiki
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(cfg.wiki_root)})
    assert provider.name == "memory-wiki"
    assert provider.is_available() is True
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli",
                        agent_context="primary")
    assert (Path(cfg.wiki_root) / "profile" / "user.md").exists()
    assert provider.system_prompt_block()
    assert [s["name"] for s in provider.get_tool_schemas()] == [
        "wiki_search", "wiki_get", "wiki_apply"]
    assert str(cfg.wiki_root) in provider.backup_paths()
    ro = memory_wiki.MemoryWikiProvider(overrides={"wiki_root": str(tmp_path / "w2")})
    ro.initialize("sess-2", hermes_home=str(tmp_path), platform="cli",
                  agent_context="subagent")
    assert not (tmp_path / "w2" / "profile").exists()  # read-only context


def test_register_collects_provider():
    import memory_wiki

    class Collector:
        provider = None
        def register_memory_provider(self, p):
            self.provider = p

    c = Collector()
    memory_wiki.register(c)
    assert isinstance(c.provider, memory_wiki.MemoryWikiProvider)
