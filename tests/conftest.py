"""Test bootstrap: load the flat plugin dir as package `memory_wiki`,
providing the hermes `agent.memory_provider` ABC (real if a checkout is
present, minimal stub otherwise)."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent")
).expanduser()


def _ensure_memory_provider_module():
    if "agent.memory_provider" in sys.modules:
        return
    mp = HERMES_SRC / "agent" / "memory_provider.py"
    pkg = types.ModuleType("agent")
    if mp.is_file():
        pkg.__path__ = [str(mp.parent)]
        sys.modules["agent"] = pkg
        spec = importlib.util.spec_from_file_location("agent.memory_provider", mp)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["agent.memory_provider"] = mod
        spec.loader.exec_module(mod)
        return
    # No hermes checkout: minimal stand-in with the same surface.
    stub = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        def system_prompt_block(self): return ""
        def prefetch(self, query, *, session_id=""): return ""
        def queue_prefetch(self, query, *, session_id=""): return None
        def sync_turn(self, user_content, assistant_content, *, session_id="",
                      messages=None): return None
        def on_session_end(self, messages): return None
        def on_pre_compress(self, messages): return ""
        def on_memory_write(self, action, target, content, metadata=None): return None
        def on_session_switch(self, new_session_id, **kwargs): return None
        def get_config_schema(self): return []
        def save_config(self, values, hermes_home): return None
        def backup_paths(self): return []
        def shutdown(self): return None

    stub.MemoryProvider = MemoryProvider
    pkg.__path__ = []
    sys.modules["agent"] = pkg
    sys.modules["agent.memory_provider"] = stub


def _load_package():
    if "memory_wiki" in sys.modules:
        return sys.modules["memory_wiki"]
    _ensure_memory_provider_module()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("memory_wiki")


_load_package()

from memory_wiki import schema, wikifs  # noqa: E402


# Minimal check.py stand-in carrying the real patch anchor.
FAKE_CHECK_PY = '''#!/usr/bin/env python3
import sys
DIR_TYPE = {
    "claims": "claim", "concepts": "concept", "sources": "source",
    "atlases": "atlas", "syntheses": "synthesis", "refs": "ref",
}
print("checked 0 notes")
print("ok")
'''

STYLE_GUIDE_STUB = "# Wiki style guide\n\nOriginal content.\n"


def make_page(root, relpath, ntype, title, topics=("testing",), extras=None,
              body="Body text.\n"):
    fm = schema.page_frontmatter(ntype, title, topics=list(topics),
                                 created="2026-07-01", extras=extras)
    path = root / relpath
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return path


@pytest.fixture(name="make_page")
def _make_page_fixture():
    return make_page


@pytest.fixture
def fixture_wiki(tmp_path):
    """A miniature ~/wiki with every type folder, check.py, style guide."""
    root = tmp_path / "wiki"
    for d in schema.DIR_TYPE:
        if d not in ("profile", "journal", "raw"):
            (root / d).mkdir(parents=True)
    (root / "_scripts").mkdir()
    (root / "_meta").mkdir()
    (root / "_scripts" / "check.py").write_text(FAKE_CHECK_PY, encoding="utf-8")
    (root / "_meta" / "style-guide.md").write_text(STYLE_GUIDE_STUB, encoding="utf-8")
    make_page(root, "concepts/zettelkasten.md", "concept", "Zettelkasten",
              topics=("note-taking",), extras={"aliases": ["slip-box"]},
              body="Atomic notes, densely interlinked.\n")
    make_page(root, "claims/small-tools-beat-frameworks.md", "claim",
              "small tools beat frameworks", topics=("engineering",),
              extras={"confidence": "likely"},
              body="Argument.\n\n## Evidence\n\n## Open questions\n")
    make_page(root, "sources/some-post.md", "source", "Some Post",
              topics=("engineering",),
              extras={"author": "Jane Doe", "medium": "post"},
              body="- thesis bullet\n")
    make_page(root, "atlases/engineering.md", "atlas", "engineering",
              topics=("engineering",), body="## Members\n")
    make_page(root, "refs/git-cheatsheet.md", "ref", "git cheatsheet",
              body="| cmd | what |\n")
    return root


@pytest.fixture
def cfg(fixture_wiki):
    from memory_wiki import config as mw_config
    return mw_config.load_config(None, {"wiki_root": str(fixture_wiki)})
