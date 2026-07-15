"""Optional, isolated QMD retrieval backend.

QMD is a disposable search index. Markdown and ``WikiIndex`` remain the
source of truth: QMD supplies ranking and snippets, while the in-memory index
decides which paths, corpora, types, and candidate states are visible.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from .index import tokenize


_MODES = {"search", "vsearch", "query"}
_BACKOFF_SECONDS = 30.0
_MAX_RESULTS = 100
_MAX_SNIPPET_CHARS = 400
_COLLECTION_PATTERN = "**/*.md"


class _QmdFailure(RuntimeError):
    pass


def _number(value, default, minimum=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < minimum:
        return default
    return parsed


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(_MAX_RESULTS, parsed))


class QmdBackend:
    """Fail-soft QMD adapter with one private collection per wiki."""

    def __init__(self, cfg, index, read_only=False):
        self.cfg = cfg
        self.index = index
        self.read_only = bool(read_only)
        self.wiki_root = Path(cfg.wiki_root).expanduser().resolve()
        tag = hashlib.sha256(str(self.wiki_root).encode("utf-8")).hexdigest()[:12]
        self.collection = f"memory-wiki-{tag}"
        configured_state = str(getattr(cfg, "qmd_state_dir", "") or "").strip()
        self.state_dir = (Path(configured_state).expanduser()
                          if configured_state else
                          Path.home() / ".cache" / "memory-wiki" / "qmd" / tag)
        self.state_dir = self.state_dir.resolve()
        self.command = str(getattr(cfg, "qmd_command", "qmd") or "qmd")
        mode = str(getattr(cfg, "qmd_search_mode", "search") or "search").lower()
        self.search_mode = mode if mode in _MODES else "search"
        self.timeout = _number(getattr(cfg, "qmd_timeout_seconds", 5.0),
                               5.0, minimum=0.1)
        self.maintenance_timeout = _number(
            getattr(cfg, "qmd_maintenance_timeout_seconds", 120.0),
            120.0, minimum=0.1)
        self.update_interval = _number(
            getattr(cfg, "qmd_update_interval_seconds", 300.0), 300.0)
        self.embed_interval = _number(
            getattr(cfg, "qmd_embed_interval_seconds", 3600.0), 3600.0)
        self.rerank = bool(getattr(cfg, "qmd_rerank", True))
        self._xdg_root = self.state_dir / "xdg"
        self._config_home = self._xdg_root / "config"
        self._cache_home = self._xdg_root / "cache"
        self._data_home = self._xdg_root / "data"
        self._state_home = self._xdg_root / "state"
        self._config_path = self._config_home / "qmd" / "index.yml"
        self._db_path = self._cache_home / "qmd" / "index.sqlite"
        self._marker_path = self.state_dir / "adapter.json"
        self._last_update = self._marker_timestamp()
        self._last_embed = self._marker_timestamp("embedded_at")
        self._last_signature = self._marker_signature()
        self._last_error = ""
        self._backoff_until = 0.0
        self._force_update = False
        self._force_embed = False
        self._maintenance_lock = threading.Lock()

    def search(self, query, corpus="pages", type_filter=None, limit=5):
        """Return QMD-ranked result dicts, or ``None`` for local fallback."""
        query = str(query or "").strip()
        result_limit = _positive_int(limit, 5)
        if not query or result_limit == 0:
            return []
        if not self._ensure_ready():
            return None
        try:
            self.index.refresh()
            records = list(self.index.records(corpus, type_filter))
        except Exception as exc:
            self._fail(f"wiki index filter failed: {exc}")
            return None
        allowed = {
            rec.rel: rec for rec in records
            if rec.ntype != "candidate" or rec.status == "pending"
        }
        if not allowed:
            return []
        fetch_limit = (_MAX_RESULTS if corpus != "all" or type_filter else
                       min(_MAX_RESULTS, max(result_limit, result_limit * 4)))
        args = [
            self.search_mode, query, "--format", "json", "--full-path",
            "-n", str(fetch_limit), "-c", self.collection,
        ]
        if self.search_mode == "query" and not self.rerank:
            args.append("--no-rerank")
        try:
            raw = self._run(args)
            payload = json.loads(raw or "[]")
            items = self._result_items(payload)
        except (json.JSONDecodeError, _QmdFailure) as exc:
            if not isinstance(exc, _QmdFailure):
                self._fail("QMD returned invalid JSON")
            return None
        aliases = self._allowed_aliases(allowed)
        results = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            rel = self._match_result(item, allowed, aliases)
            if not rel or rel in seen:
                continue
            rec = allowed[rel]
            out = {
                "ref": rec.rel,
                "title": rec.title,
                "type": rec.ntype,
                "score": round(self._score(item.get("score")), 3),
                "snippet": self._snippet(item),
                "backend": "qmd",
            }
            line = item.get("line")
            if isinstance(line, int) and line > 0:
                out["line"] = line
            if rec.ntype == "claim":
                out["confidence"] = rec.confidence
                out["maturity"] = rec.maturity
            if rec.ntype == "candidate":
                out.update(id=rec.cand_id, status=rec.status, kind=rec.kind,
                           created=rec.created,
                           content_hash=rec.content_hash)
            from . import search as local_search
            local_search.add_claim_details(
                out, rec, query, context=out.get("snippet", ""),
                fallback_single=True)
            results.append(out)
            seen.add(rel)
            if len(results) >= result_limit:
                break
        for item in results:
            # QMD 2.5 can return a useful BM25 ordering with every JSON score
            # set to zero. Preserve that raw score. Lexical relevance uses
            # absolute query coverage, so a weak top hit cannot look perfect.
            rec = allowed[item["ref"]]
            query_terms = set(tokenize(query))
            coverage = (len(query_terms.intersection(rec.tokens))
                        / len(query_terms)) if query_terms else 0.0
            relevance = (coverage if self.search_mode == "search"
                         or item["score"] <= 0 else item["score"])
            item["relevance"] = round(min(1.0, relevance), 4)
        if (corpus != "all" or type_filter) and len(results) < result_limit:
            self._merge_local_results(
                results, seen, query, corpus, type_filter, result_limit)
        self._last_error = ""
        return results

    def status(self):
        """Return adapter state without starting QMD or creating files."""
        now = time.monotonic()
        initialized = self._state_initialized()
        command_available = self._command_available()
        backed_off = now < self._backoff_until
        available = command_available and not backed_off
        if self.read_only and not initialized:
            available = False
        return {
            "enabled": True,
            "backend": "qmd",
            "available": available,
            "initialized": initialized,
            "read_only": self.read_only,
            "command": self.command,
            "search_mode": self.search_mode,
            "collection": self.collection,
            "state_dir": str(self.state_dir),
            "update_due": self._update_due(),
            "embed_due": self._embed_due(),
            "backoff": backed_off,
            "last_error": self._last_error,
        }

    def invalidate(self):
        """Force an index update before the next search."""
        with self._maintenance_lock:
            self._force_update = True
            self._force_embed = True
            self._backoff_until = 0.0

    def _ensure_ready(self):
        with self._maintenance_lock:
            if time.monotonic() < self._backoff_until:
                return False
            present, configured = self._collection_status()
            initialized = self._db_path.is_file() and configured
            if self.read_only:
                if not initialized:
                    self._last_error = "QMD state is not initialized for read-only use"
                    return False
                return True
            try:
                self._prepare_dirs()
                current_signature = self._wiki_signature()
                content_changed = (
                    not initialized or not configured or self._force_update
                    or current_signature != self._last_signature
                )
                if present and not configured:
                    self._run(["collection", "remove", self.collection],
                              timeout=self.maintenance_timeout)
                    present = False
                if not configured:
                    self._run(["collection", "add", str(self.wiki_root),
                               "--name", self.collection,
                               "--mask", _COLLECTION_PATTERN],
                              timeout=self.maintenance_timeout)
                    configured = True
                maintained = False
                if content_changed or self._interval_update_due():
                    self._run(["update"], timeout=self.maintenance_timeout)
                    self._last_update = time.time()
                    self._last_signature = current_signature
                    self._force_update = False
                    if content_changed:
                        self._force_embed = True
                    maintained = True
                if self.search_mode in ("query", "vsearch") \
                        and self._embed_due():
                    self._run(["embed", "-c", self.collection],
                              timeout=self.maintenance_timeout)
                    self._last_embed = time.time()
                    self._force_embed = False
                    maintained = True
                if maintained:
                    self._write_marker()
                return True
            except (OSError, _QmdFailure) as exc:
                if not isinstance(exc, _QmdFailure):
                    self._fail(f"QMD state setup failed: {exc}")
                return False

    def _prepare_dirs(self):
        for path in (self.state_dir, self._config_home, self._cache_home,
                     self._data_home, self._state_home):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _run(self, args, timeout=None):
        argv = [self.command, *args]
        deadline = self.timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.wiki_root),
                env=self._environment(),
                capture_output=True,
                text=True,
                timeout=deadline,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            self._fail(f"QMD command timed out after {deadline:g}s")
            raise _QmdFailure(self._last_error) from None
        except (FileNotFoundError, OSError) as exc:
            self._fail(f"QMD command unavailable: {exc}")
            raise _QmdFailure(self._last_error) from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "command failed").strip()
            detail = detail.splitlines()[-1] if detail else "command failed"
            self._fail(f"QMD command failed: {detail[:300]}")
            raise _QmdFailure(self._last_error)
        return proc.stdout

    def _environment(self):
        env = os.environ.copy()
        env.update({
            "XDG_CONFIG_HOME": str(self._config_home),
            "XDG_CACHE_HOME": str(self._cache_home),
            "XDG_DATA_HOME": str(self._data_home),
            "XDG_STATE_HOME": str(self._state_home),
        })
        # mise shims consult XDG config before dispatch. Private QMD XDG state
        # would therefore hide the trusted mise config. Prefer the real QMD and
        # node bins already present later on PATH.
        entries = env.get("PATH", "").split(os.pathsep)
        entries = [entry for entry in entries
                   if not self._mise_shim_dir(entry)]
        # npm tools can contain native modules built for mise's Node. Keep
        # that runtime ahead of an unrelated system Node after removing shims.
        mise_node = [entry for entry in entries
                     if self._mise_node_bin(entry)]
        env["PATH"] = os.pathsep.join(
            mise_node + [entry for entry in entries if entry not in mise_node]
        )
        return env

    @staticmethod
    def _mise_shim_dir(entry):
        path = PurePosixPath(entry.replace("\\", "/"))
        return path.name == "shims" and "mise" in path.parts

    @staticmethod
    def _mise_node_bin(entry):
        parts = PurePosixPath(entry.replace("\\", "/")).parts
        return (len(parts) >= 4 and parts[-1] == "bin"
                and parts[-3] == "node" and "mise" in parts)

    def _command_available(self):
        if os.sep in self.command or (os.altsep and os.altsep in self.command):
            return os.path.isfile(self.command) and os.access(self.command, os.X_OK)
        return shutil.which(self.command, path=self._environment().get("PATH")) is not None

    def _state_initialized(self):
        return self._db_path.is_file() and self._collection_configured()

    def _collection_configured(self):
        return self._collection_status()[1]

    def _collection_status(self):
        """Return ``(present, valid)`` for our exact private collection."""
        entry = self._collection_entry()
        if entry is None:
            return False, False
        raw_path = entry.get("path")
        pattern = entry.get("pattern")
        if not isinstance(raw_path, str) or not isinstance(pattern, str):
            return True, False
        try:
            configured_root = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError):
            return True, False
        return True, (configured_root == self.wiki_root
                      and pattern == _COLLECTION_PATTERN)

    def _collection_entry(self):
        """Parse the small QMD YAML subset without adding a dependency."""
        try:
            lines = self._config_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        start = next((i for i, line in enumerate(lines)
                      if re.fullmatch(r"collections:\s*(?:#.*)?", line)), None)
        if start is None:
            return None
        names = {
            f"  {self.collection}:",
            f'  "{self.collection}":',
            f"  '{self.collection}':",
        }
        entry_start = None
        for i in range(start + 1, len(lines)):
            line = lines[i]
            if line.strip() and not line.startswith(" "):
                break
            candidate = line.split(" #", 1)[0].rstrip()
            if candidate in names:
                entry_start = i
                break
        if entry_start is None:
            return None
        entry = {}
        for line in lines[entry_start + 1:]:
            if line.strip() and len(line) - len(line.lstrip(" ")) <= 2:
                break
            match = re.fullmatch(r" {4}(path|pattern):\s*(.*?)\s*", line)
            if match:
                entry[match.group(1)] = self._yaml_scalar(match.group(2))
        return entry

    @staticmethod
    def _yaml_scalar(raw):
        raw = raw.strip()
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
                return value if isinstance(value, str) else None
            except (TypeError, ValueError):
                return None
        if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1].replace("''", "'")
        return raw.split(" #", 1)[0].rstrip() or None

    def _update_due(self):
        if self._force_update:
            return True
        if self._wiki_signature() != self._last_signature:
            return True
        return self._interval_update_due()

    def _interval_update_due(self):
        return (self._last_update <= 0
                or time.time() - self._last_update >= self.update_interval)

    def _wiki_signature(self):
        digest = hashlib.sha256()
        try:
            paths = sorted(self.wiki_root.rglob("*.md"))
        except OSError:
            paths = []
        for path in paths:
            try:
                stat = path.stat()
                rel = path.relative_to(self.wiki_root).as_posix()
            except (OSError, ValueError):
                continue
            digest.update(rel.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(b":")
            digest.update(str(stat.st_size).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _embed_due(self):
        if self.search_mode == "search":
            return False
        if self._force_embed:
            return True
        if self._last_embed <= 0:
            return True
        return time.time() - self._last_embed >= self.embed_interval

    def _marker_timestamp(self, key="updated_at"):
        try:
            payload = json.loads(self._marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return 0.0
        if not isinstance(payload, dict):
            return 0.0
        if (payload.get("wiki_root") != str(self.wiki_root)
                or payload.get("collection") != self.collection):
            return 0.0
        return _number(payload.get(key), 0.0)

    def _marker_signature(self):
        try:
            payload = json.loads(self._marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        if (payload.get("wiki_root") != str(self.wiki_root)
                or payload.get("collection") != self.collection):
            return ""
        value = payload.get("wiki_signature")
        return value if isinstance(value, str) else ""

    def _write_marker(self):
        payload = {
            "version": 1,
            "wiki_root": str(self.wiki_root),
            "collection": self.collection,
            "updated_at": self._last_update,
            "embedded_at": self._last_embed,
            "wiki_signature": self._last_signature,
        }
        tmp = self._marker_path.with_name(
            f".{self._marker_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self._marker_path)

    def _fail(self, message):
        self._last_error = str(message)
        self._backoff_until = time.monotonic() + _BACKOFF_SECONDS

    @staticmethod
    def _result_items(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("results", "hits", "documents"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise json.JSONDecodeError("QMD result list missing", "", 0)

    def _match_result(self, item, allowed, aliases):
        raw = item.get("file") or item.get("path") or item.get("source")
        if not isinstance(raw, str) or not raw.strip():
            return None
        rel = self._result_rel(raw.strip())
        if rel in allowed:
            return rel
        matches = aliases.get(self._qmd_rel_key(rel), ()) if rel else ()
        return matches[0] if len(matches) == 1 else None

    def _result_rel(self, raw):
        if raw.startswith("qmd://"):
            parsed = urlparse(raw)
            if parsed.netloc != self.collection:
                return None
            rel = unquote(parsed.path.lstrip("/"))
            parts = PurePosixPath(rel).parts
            if not rel or ".." in parts:
                return None
            return PurePosixPath(*parts).as_posix()
        path = Path(raw)
        try:
            resolved = path.resolve() if path.is_absolute() else (self.wiki_root / path).resolve()
            return resolved.relative_to(self.wiki_root).as_posix()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _qmd_rel_key(rel):
        if not rel:
            return ""
        return "/".join(
            part.replace("_", "-").replace(" ", "-")
            for part in PurePosixPath(rel).parts
        )

    def _allowed_aliases(self, allowed):
        aliases = {}
        for rel in allowed:
            aliases.setdefault(self._qmd_rel_key(rel), []).append(rel)
        return aliases

    def _merge_local_results(self, results, seen, query, corpus, type_filter,
                             result_limit):
        """Fill restricted QMD result gaps from the exact local corpus."""
        from . import search as local_search

        scored = local_search.score_records(
            self.index, query, corpus=corpus, type_filter=type_filter)
        for score, rec in scored:
            if rec.rel in seen:
                continue
            item = local_search.result_dict(rec, score, query)
            item["relevance"] = round(
                local_search.query_coverage(query, rec), 4)
            item["backend"] = "local"
            results.append(item)
            seen.add(rec.rel)
            if len(results) >= result_limit:
                break

    @staticmethod
    def _score(raw):
        return _number(raw, 0.0)

    @staticmethod
    def _snippet(item):
        raw = item.get("snippet")
        if not isinstance(raw, str):
            raw = item.get("content") if isinstance(item.get("content"), str) else ""
        lines = raw.strip().splitlines()
        if lines and lines[0].startswith("@@"):
            lines = lines[1:]
        text = " ".join(" ".join(lines).split())
        if len(text) > _MAX_SNIPPET_CHARS:
            text = text[:_MAX_SNIPPET_CHARS].rsplit(" ", 1)[0].rstrip() + "..."
        return text
