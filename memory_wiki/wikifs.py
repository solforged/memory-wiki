"""Filesystem primitives for the memory wiki.

Stdlib only: the Hermes plugin loader execs every module in this
directory and swallows import failures at debug level, so nothing here
may depend on third-party packages.

Frontmatter parsing/rendering stays compatible with the wiki's own
``_scripts/check.py`` YAML-subset parser. Extensions beyond it are a
single level of nested mappings and inline JSON values. The latter
allows structured claims while leaving the house-style scalar lists
unchanged.
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


_SECTION_HEADING = re.compile(r"^## [^\r\n]+$")


def validate_section_heading(section):
    """Require one plain level-2 heading, preventing Markdown injection."""
    section = str(section or "")
    if not _SECTION_HEADING.fullmatch(section):
        raise ValueError("section must be one line like '## Notes'")
    return section


# -- frontmatter ---------------------------------------------------------

def parse_frontmatter(text):
    """Parse leading frontmatter. Returns (dict, body) or (None, text).

    Mirrors check.py: ``key: value`` scalars, inline ``[a, b]`` lists,
    unindented ``- item`` continuations, ``  # comment`` stripping, and
    a bare ``key:`` yielding ``[]``. Extensions: 2-space-indented
    ``sub: val`` lines under a bare key form a one-level dict, and valid
    inline JSON arrays/objects retain nested values and numeric types.
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
                fm[key][k2.strip()] = _parse_value(v2.strip())
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
    if ((val.startswith("[") and val.endswith("]"))
            or (val.startswith("{") and val.endswith("}"))):
        try:
            decoded = json.loads(val)
            # Scalar lists belong to the original check.py-compatible
            # syntax. Only nested sequences opt into JSON typing.
            if isinstance(decoded, dict) or any(
                    isinstance(item, (dict, list)) for item in decoded):
                return decoded
        except (json.JSONDecodeError, TypeError):
            pass
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


def _render_scalar(v):
    """Render one scalar in Obsidian-friendly house style.

    String scalars are always double-quoted so YAML 1.1 consumers
    (including Obsidian Properties) keep ISO dates as strings, and so
    the vault matches the quoted corpus majority. Booleans and numbers
    stay bare.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(v)
    s = str(v)
    if "\n" in s or "\r" in s:
        raise ValueError("frontmatter scalars cannot contain newlines")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_frontmatter(fm):
    """Render a frontmatter dict in the wiki's house style.

    House style matches the Obsidian-friendly corpus majority:
    always-quoted string scalars, block lists with two-space-indented
    dashes, and empty collections as ``[]`` / ``{}``. Nested structured
    values (claim objects, etc.) still use compact JSON.
    """
    out = ["---"]
    for k, v in fm.items():
        validate_frontmatter_value(k)
        validate_frontmatter_value(v)
        if isinstance(v, dict) and _is_flat_scalar_mapping(v):
            if not v:
                out.append(f"{k}: {{}}")
            else:
                out.append(f"{k}:")
                for k2, v2 in v.items():
                    out.append(f"  {k2}: {_render_scalar(v2)}")
        elif isinstance(v, (list, tuple)):
            if any(isinstance(item, (dict, list, tuple)) for item in v):
                out.append(f"{k}: {_render_json(v)}")
            elif not v:
                out.append(f"{k}: []")
            else:
                out.append(f"{k}:")
                out.extend(f"  - {_render_scalar(item)}" for item in v)
        elif isinstance(v, dict):
            out.append(f"{k}: {_render_json(v)}")
        else:
            out.append(f"{k}: {_render_scalar(v)}")
    out.append("---")
    return "\n".join(out) + "\n"


def _is_flat_scalar_mapping(value):
    return all(not isinstance(item, (dict, list, tuple))
               for item in value.values())


def _render_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def validate_frontmatter_value(value):
    """Reject newline-bearing scalar data before it reaches YAML."""
    if isinstance(value, dict):
        for key, item in value.items():
            validate_frontmatter_value(key)
            validate_frontmatter_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_frontmatter_value(item)
        return
    if isinstance(value, str) and ("\n" in value or "\r" in value):
        raise ValueError("frontmatter scalars cannot contain newlines")


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


def write_if_changed(path, text):
    """Atomically write text only when its bytes changed. Returns bool."""
    path = Path(path)
    if read_text(path) == text:
        return False
    atomic_write(path, text)
    return True


def replace_managed_markdown_block(original, heading, start_marker,
                                   end_marker, body):
    """Replace one generated block while preserving all human content.

    A half-present marker pair is rejected because guessing ownership could
    destroy a hand-edited report. When no markers exist, the block is added
    beneath an existing heading or appended with a new heading.
    """
    original = str(original or "")
    body = str(body or "").rstrip("\n")
    if start_marker in body or end_marker in body:
        raise ValueError("managed block body contains its ownership marker")
    start = original.find(start_marker)
    end = original.find(end_marker)
    if (start < 0) != (end < 0):
        raise ValueError("managed Markdown block has an incomplete marker pair")
    block = f"{start_marker}\n{body}\n{end_marker}" if body else \
        f"{start_marker}\n{end_marker}"
    if start >= 0:
        if end < start:
            raise ValueError("managed Markdown block markers are out of order")
        end += len(end_marker)
        rendered = original[:start] + block + original[end:]
    else:
        base = original.rstrip("\n")
        heading_re = re.compile(rf"^{re.escape(heading)}[ \t]*$", re.MULTILINE)
        addition = block if heading_re.search(base) else f"{heading}\n\n{block}"
        rendered = f"{base}\n\n{addition}" if base else addition
    return rendered.rstrip("\n") + "\n"


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
    """Append one JSON object durably before returning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            + "\n").encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write appending JSONL")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


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
    parts = Path(rel).parts
    if (not rel or rel.startswith(("/", "~")) or "\\" in rel
            or any(part in (".", "..") for part in parts)):
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
    """Local ISO date; wiki `createdAt` dates are user-local."""
    return datetime.now().strftime("%Y-%m-%d")


def utc_stamp():
    """Compact UTC timestamp for candidate ids: YYYYmmdd-HHMMSS."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hhmm():
    return datetime.now().strftime("%H:%M")


def days_since(date_str):
    """Whole days since a YYYY-MM-DD date, or None if unparsable."""
    try:
        then = datetime.strptime(str(date_str), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (datetime.now().date() - then).days
