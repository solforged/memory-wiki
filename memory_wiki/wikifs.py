"""Filesystem primitives for the memory wiki.

Stdlib only: the Hermes plugin loader execs every module in this
directory and swallows import failures at debug level, so nothing here
may depend on third-party packages.

Frontmatter parsing/rendering stays compatible with the wiki's own
``_scripts/check.py`` YAML-subset parser. One extension beyond it: a
single level of nested mappings, used only by candidate files in
``_inbox/`` (which check.py never reads).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class LockTimeout(RuntimeError):
    """Could not acquire the wiki lock within the timeout."""


# -- frontmatter ---------------------------------------------------------

def parse_frontmatter(text):
    """Parse leading frontmatter. Returns (dict, body) or (None, text).

    Mirrors check.py: ``key: value`` scalars, inline ``[a, b]`` lists,
    unindented ``- item`` continuations, ``  # comment`` stripping, and
    a bare ``key:`` yielding ``[]``. Extension: 2-space-indented
    ``sub: val`` lines under a bare key form a one-level dict.
    """
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end == -1:
        return None, text
    fm, key = {}, None
    for raw in text[4:end].splitlines():
        line = raw.split("  #")[0].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- ") and key is not None and isinstance(fm.get(key), list):
            fm[key].append(_parse_scalar(stripped[2:].strip()))
            continue
        if indent >= 2:
            # nested mapping line under a bare key (candidate `fields`)
            if ":" in stripped and key is not None and isinstance(fm.get(key), (list, dict)):
                if isinstance(fm[key], list):
                    if fm[key]:
                        continue  # malformed: dict line after list items
                    fm[key] = {}
                k2, _, v2 = stripped.partition(":")
                fm[key][k2.strip()] = _parse_scalar(v2.strip())
            continue
        if ":" not in line:
            continue
        k, _, val = line.partition(":")
        key, val = k.strip(), val.strip()
        fm[key] = _parse_value(val)
    return fm, text[end + 4:]


def _parse_value(val):
    if val == "":
        return []  # may become a dash-list, a dict, or stay empty (check.py compat)
    if val == "{}":
        return {}
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_parse_scalar(v.strip()) for v in inner.split(",")] if inner else []
    return _parse_scalar(val)


def _parse_scalar(val):
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
        inner = val[1:-1]
        if val[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return val


_NEEDS_QUOTES = re.compile(r'^[\[\{\'"#&*!|>%@`-]|: |:$|\s#|^\s|\s$')


def _render_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if _NEEDS_QUOTES.search(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def render_frontmatter(fm):
    """Render a frontmatter dict in the wiki's house style."""
    out = ["---"]
    for k, v in fm.items():
        if isinstance(v, dict):
            if not v:
                out.append(f"{k}: {{}}")
            else:
                out.append(f"{k}:")
                for k2, v2 in v.items():
                    out.append(f"  {k2}: {_render_scalar(v2)}")
        elif isinstance(v, (list, tuple)):
            out.append(f"{k}: [" + ", ".join(_render_scalar(i) for i in v) + "]")
        else:
            out.append(f"{k}: {_render_scalar(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


def render_page(fm, body):
    """Frontmatter + body, no blank line between (house style)."""
    body = body.lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return render_frontmatter(fm) + body


# -- writes --------------------------------------------------------------

def atomic_write(path, text):
    """Write text to path via tmp file + os.replace (never partial)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def create_exclusive(path, text):
    """Create a new file, failing if it already exists. Returns bool."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def append_jsonl(path, obj):
    """Append one JSON object as a line with a single O_APPEND write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@contextmanager
def wiki_lock(wiki_root, timeout=5.0):
    """One coarse advisory lock (flock on _inbox/.lock) for all
    read-modify-write of shared files. Hold times are milliseconds."""
    lock_dir = Path(wiki_root) / "_inbox"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not lock {lock_path} within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# -- paths and misc ------------------------------------------------------

def safe_wiki_path(wiki_root, rel):
    """Resolve a repo-relative path, refusing escapes from wiki_root."""
    rel = (rel or "").strip()
    if not rel or rel.startswith(("/", "~")) or "\\" in rel:
        raise ValueError(f"invalid wiki path: {rel!r}")
    root = Path(wiki_root).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes wiki root: {rel!r}")
    return p


def read_text(path):
    """Return file contents, or None if the file does not exist."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None


def slugify(text, max_len=60):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > max_len:
        s = s[:max_len].rsplit("-", 1)[0] or s[:max_len].rstrip("-")
    return s or "note"


def today():
    """Local ISO date; wiki `created` dates are user-local."""
    return datetime.now().strftime("%Y-%m-%d")


def utc_stamp():
    """Compact UTC timestamp for candidate ids: YYYYmmdd-HHMMSS."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hhmm():
    return datetime.now().strftime("%H:%M")
