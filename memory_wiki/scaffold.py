"""Scaffolding: runtime dirs, profile seeds, and the one-time `init`
patches to the wiki's own validator and style guide."""

from __future__ import annotations

import difflib
import logging
from pathlib import Path

from . import schema, wikifs

logger = logging.getLogger(__name__)

RUNTIME_DIRS = ("profile", "journal", schema.INBOX_DIR)

# Exact anchor in ~/wiki/_scripts/check.py (verified against the file).
CHECK_ANCHOR = (
    '    "atlases": "atlas", "syntheses": "synthesis", "refs": "ref",\n'
    '}'
)
CHECK_REPLACEMENT = (
    '    "atlases": "atlas", "syntheses": "synthesis", "refs": "ref",\n'
    '    "profile": "profile", "journal": "journal",  # memory-wiki\n'
    '}'
)
CHECK_SENTINEL = '"profile": "profile"'

STYLE_GUIDE_SECTION = f"""
{schema.STYLE_GUIDE_BEGIN}
## Agent memory extensions

Two additional types are maintained by the hermes memory-wiki plugin.
profile/ holds two singleton pages (user.md, agent.md): living context,
jointly edited by human and agent, `updated` bumped on write. journal/
holds one page per day, named by ISO date (the one sanctioned exception
to the no-dates filename rule); episodic, append-mostly. _inbox/ is
infrastructure (staged memory candidates awaiting review), not notes.
Knowledge types (concepts/claims/sources/atlases/syntheses/refs) are
never written directly by automation: captures stage into _inbox/ and
reach pages only through review (`hermes memory-wiki review`).
{schema.STYLE_GUIDE_END}
"""


def ensure_runtime(cfg, created=None):
    """Quietly ensure runtime dirs and profile seed pages exist.

    Safe to call every session start; never touches check.py or the
    style guide (that is `init`'s job). Returns created relative paths.
    """
    made = []
    root = Path(cfg.wiki_root)
    for d in RUNTIME_DIRS:
        p = root / d
        if not p.is_dir():
            p.mkdir(parents=True, exist_ok=True)
            made.append(d + "/")
    for fname in ("user.md", "agent.md"):
        p = root / "profile" / fname
        if not p.exists():
            fm, body = schema.profile_seed(fname, created)
            wikifs.atomic_write(p, wikifs.render_page(fm, body))
            made.append(f"profile/{fname}")
    if made:
        logger.info("memory-wiki: created %s", ", ".join(made))
    return made


def init_wiki(cfg, dry_run=False):
    """Full scaffold: runtime dirs/pages plus marker-guarded patches to
    _scripts/check.py and _meta/style-guide.md. Idempotent.

    Returns {"created": [...], "check_py": (status, diff),
    "style_guide": (status, diff)} where status is one of
    applied | would-apply | already | anchor-missing | no-file.
    """
    root = Path(cfg.wiki_root)
    created = []
    if dry_run:
        for d in RUNTIME_DIRS:
            if not (root / d).is_dir():
                created.append(d + "/")
        for fname in ("user.md", "agent.md"):
            if not (root / "profile" / fname).exists():
                created.append(f"profile/{fname}")
    else:
        created = ensure_runtime(cfg)
    return {
        "created": created,
        "check_py": _patch_check_py(root, dry_run),
        "style_guide": _append_style_guide(root, dry_run),
    }


def _patch_check_py(root, dry_run):
    path = root / "_scripts" / "check.py"
    old = wikifs.read_text(path)
    if old is None:
        return "no-file", ""
    if CHECK_SENTINEL in old:
        return "already", ""
    if CHECK_ANCHOR not in old:
        return "anchor-missing", _diff("check.py", CHECK_ANCHOR, CHECK_REPLACEMENT)
    new = old.replace(CHECK_ANCHOR, CHECK_REPLACEMENT, 1)
    diff = _diff("_scripts/check.py", old, new)
    if not dry_run:
        wikifs.atomic_write(path, new)
    return ("would-apply" if dry_run else "applied"), diff


def _append_style_guide(root, dry_run):
    path = root / "_meta" / "style-guide.md"
    old = wikifs.read_text(path)
    if old is None:
        return "no-file", ""
    if schema.STYLE_GUIDE_BEGIN in old:
        return "already", ""
    new = old.rstrip("\n") + "\n" + STYLE_GUIDE_SECTION
    diff = _diff("_meta/style-guide.md", old, new)
    if not dry_run:
        wikifs.atomic_write(path, new)
    return ("would-apply" if dry_run else "applied"), diff


def _diff(name, old, new):
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}"))
