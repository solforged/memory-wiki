"""Scaffolding: runtime dirs, profile seeds, and the one-time `init`
patches to the wiki's own validator and style guide."""

from __future__ import annotations

import difflib
import logging
import re
from pathlib import Path

from . import schema, type_registry, wikifs

logger = logging.getLogger(__name__)

RUNTIME_DIRS = ("profile", "journal", "raw", "entities", "reports",
                schema.INBOX_DIR)

CHECK_TYPES_BEGIN = f"    # memory-wiki:types:v{schema.SCHEMA_VERSION}:begin"
CHECK_TYPES_END = f"    # memory-wiki:types:v{schema.SCHEMA_VERSION}:end"
_CHECK_TYPES_BLOCK = re.compile(
    r"(?m)^\s*# memory-wiki:types:v\d+:begin\n.*?"
    r"^\s*# memory-wiki:types:v\d+:end\n?",
    re.DOTALL,
)
_LEGACY_MANAGED_LINE = re.compile(r"(?m)^.*# memory-wiki\s*$\n?")

# Current ~/wiki validator anchors. New pages use createdAt, while existing
# pages may retain the former created field.
CHECK_DATE_REQUIRED_ANCHOR = (
    'REQUIRED = {"type", "title", "topics", "createdAt"}'
)
CHECK_DATE_REQUIRED_REPLACEMENT = 'REQUIRED = {"type", "title", "topics"}'
CHECK_DATE_ANCHOR = '''        created_at = fm.get("createdAt")
        if (created_at and
                (not isinstance(created_at, str) or not DATE.fullmatch(created_at))):
            errors.append(f"{rel}: invalid createdAt {created_at!r}")'''
CHECK_DATE_REPLACEMENT = '''        # memory-wiki: legacy-created-compat
        created_at = fm.get("createdAt") or fm.get("created")
        if not created_at:
            errors.append(f"{rel}: missing or empty createdAt")
        elif not isinstance(created_at, str) or not DATE.fullmatch(created_at):
            errors.append(f"{rel}: invalid createdAt {created_at!r}")'''
CHECK_DATE_FALLBACK = 'fm.get("createdAt") or fm.get("created")'

STYLE_GUIDE_SECTION = f"""
{schema.STYLE_GUIDE_BEGIN}
## Agent memory extensions

Additional types are maintained by the hermes memory-wiki plugin.
profile/ holds two singleton pages (user.md, agent.md): living context,
jointly edited by human and agent, `updated` bumped on write. journal/
holds one page per day, named by ISO date (the one sanctioned exception
to the no-dates filename rule); episodic, append-mostly. _inbox/ is
infrastructure (staged memory candidates awaiting review), not notes.
Knowledge captures stage into _inbox/ and reach pages only through review
(`hermes memory-wiki review`). reports/ contains compiler-owned health
dashboards; generated blocks are replaceable, human blocks are preserved.
raw/ contains private archival records, including edited conversation
transcripts. Raw pages are searchable through the `raw` or `all` corpus but
remain outside the default curated `pages` corpus. entities/ contains people,
organizations, institutions, parties, and collectives with stable IDs and
evidenced relationships; refs/ remains for reusable guides and lookup material.
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
    # Writable initialization is the recovery boundary. Import locally to
    # keep the scaffold/candidate dependency acyclic at module load time.
    from . import candidates
    recovery = candidates.recover(cfg)
    for issue in recovery["ambiguous"]:
        logger.warning("memory-wiki recovery: %s", issue)
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
    new = old
    managed = _managed_check_types_block()
    if _CHECK_TYPES_BLOCK.search(new):
        new = _CHECK_TYPES_BLOCK.sub(managed, new, count=1)
    else:
        new = _LEGACY_MANAGED_LINE.sub("", new)
        inserted = _insert_dir_type_block(new, managed)
        if inserted is None:
            return "anchor-missing", _diff(
                "check.py managed types", "DIR_TYPE = {\n}\n",
                "DIR_TYPE = {\n" + managed + "}\n")
        new = inserted

    date_compatible = (CHECK_DATE_REQUIRED_REPLACEMENT in new
                       and CHECK_DATE_FALLBACK in new)
    if not date_compatible and "createdAt" in new:
        if (CHECK_DATE_REQUIRED_ANCHOR not in new
                or CHECK_DATE_ANCHOR not in new):
            expected = (CHECK_DATE_REQUIRED_ANCHOR + "\n" +
                        CHECK_DATE_ANCHOR + "\n")
            replacement = (CHECK_DATE_REQUIRED_REPLACEMENT + "\n" +
                           CHECK_DATE_REPLACEMENT + "\n")
            return "anchor-missing", _diff(
                "check.py createdAt compatibility", expected, replacement)
        new = new.replace(
            CHECK_DATE_REQUIRED_ANCHOR,
            CHECK_DATE_REQUIRED_REPLACEMENT,
            1)
        new = new.replace(CHECK_DATE_ANCHOR, CHECK_DATE_REPLACEMENT, 1)

    if new == old:
        return "already", ""
    diff = _diff("_scripts/check.py", old, new)
    if not dry_run:
        wikifs.atomic_write(path, new)
    return ("would-apply" if dry_run else "applied"), diff


def _managed_check_types_block():
    lines = [CHECK_TYPES_BEGIN]
    lines.extend(
        f'    "{spec.directory}": "{spec.ntype}",'
        for spec in type_registry.external_types())
    lines.append(CHECK_TYPES_END)
    return "\n".join(lines) + "\n"


def _insert_dir_type_block(text, block):
    """Insert before the closing brace of the host's DIR_TYPE literal."""
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines)
                  if re.match(r"^DIR_TYPE\s*=\s*\{", line)), None)
    if start is None:
        return None
    stop = next((index for index in range(start + 1, len(lines))
                 if re.match(r"^}\s*(?:#.*)?$", lines[index].rstrip("\n"))), None)
    if stop is None:
        return None
    lines.insert(stop, block)
    return "".join(lines)


def _append_style_guide(root, dry_run):
    path = root / "_meta" / "style-guide.md"
    old = wikifs.read_text(path)
    if old is None:
        return "no-file", ""
    begin = schema.STYLE_GUIDE_BEGIN
    end = schema.STYLE_GUIDE_END
    if begin in old and end in old:
        start = old.index(begin)
        stop = old.index(end, start) + len(end)
        replacement = STYLE_GUIDE_SECTION.strip("\n")
        new = old[:start] + replacement + old[stop:]
        if new == old:
            return "already", ""
    elif begin in old or end in old:
        return "anchor-missing", _diff(
            "_meta/style-guide.md", old, old.rstrip("\n") + "\n" + STYLE_GUIDE_SECTION)
    else:
        new = old.rstrip("\n") + "\n" + STYLE_GUIDE_SECTION
    diff = _diff("_meta/style-guide.md", old, new)
    if not dry_run:
        wikifs.atomic_write(path, new)
    return ("would-apply" if dry_run else "applied"), diff


def _diff(name, old, new):
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}"))
