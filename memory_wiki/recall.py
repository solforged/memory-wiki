"""Bounded short-term recall telemetry for staged candidates.

Only hashed queries and aggregate candidate metrics are persisted. The state is
advisory and rebuildable, so malformed state degrades to an empty store.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from . import schema, wikifs

STATE_FILE = "short-term-recall.json"
STATE_VERSION = 1

MAX_CANDIDATES = 512
MAX_EVENT_KEYS = 4096
MAX_QUERY_HASHES = 64
MAX_RECALL_DAYS = 90
MAX_SOURCES = 16
MAX_STATE_BYTES = 2_000_000

_QUERY_HASH_LEN = 20
_EVENT_HASH_LEN = 24
_QUERY_HASH = re.compile(rf"^[0-9a-f]{{{_QUERY_HASH_LEN}}}$")
_EVENT_HASH = re.compile(rf"^[0-9a-f]{{{_EVENT_HASH_LEN}}}$")
_CONTENT_HASH = re.compile(r"^[0-9a-f]{16}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def record(cfg, results, query, session_id="", source="prefetch"):
    """Record candidate recalls and return a compact operation summary.

    A candidate is counted once for a given session and normalized query. Page
    results and malformed result items are ignored.
    """
    prepared = []
    for result in results or []:
        if not isinstance(result, dict) or result.get("type") != "candidate":
            continue
        candidate_id = str(result.get("id") or "").strip()
        if not candidate_id or len(candidate_id) > 256:
            continue
        prepared.append((candidate_id, _result_score(result),
                         _clean_content_hash(result.get("content_hash"))))
    if not prepared:
        return {"recorded": 0, "deduplicated": 0, "issue": ""}

    query_hash = _hash_query(query)
    session_hash = _hash_session(session_id)
    source = _clean_source(source)
    path = _state_path(cfg)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, issue = _read_state_unlocked(path)
        now = wikifs.utc_iso()
        day = now[:10] if _DAY.match(now[:10]) else wikifs.today()
        recorded = deduplicated = 0

        for candidate_id, score, content_hash in prepared:
            current = _normalise_metric(
                state["candidates"].get(candidate_id))
            if content_hash and content_hash != current["content_hash"]:
                current = _empty_metric()
                state["events"] = {
                    key: event for key, event in state["events"].items()
                    if event.get("candidate_id") != candidate_id
                }
            if content_hash:
                current["content_hash"] = content_hash
            event_key = _event_key(candidate_id, session_hash, query_hash)
            if event_key in state["events"]:
                deduplicated += 1
                continue

            state["next_sequence"] += 1
            sequence = state["next_sequence"]
            state["events"][event_key] = {
                "candidate_id": candidate_id,
                "at": now,
                "sequence": sequence,
            }
            current["recall_count"] += 1
            current["total_score"] += score
            current["max_score"] = max(current["max_score"], score)
            current["query_hashes"] = _bounded_unique(
                current["query_hashes"], query_hash, MAX_QUERY_HASHES)
            current["recall_days"] = _bounded_unique(
                current["recall_days"], day, MAX_RECALL_DAYS)
            if source:
                current["sources"] = _bounded_unique(
                    current["sources"], source, MAX_SOURCES)
            current["first_recalled_at"] = current["first_recalled_at"] or now
            current["last_recalled_at"] = now
            current["_sequence"] = sequence
            state["candidates"][candidate_id] = current
            recorded += 1

        if recorded:
            state["updated_at"] = now
            _trim_state(state)
            _write_state_unlocked(path, state)
        return {
            "recorded": recorded,
            "deduplicated": deduplicated,
            "issue": issue,
        }


def metrics(cfg, candidate_id):
    """Normalized metrics for one candidate, or zero values when absent."""
    path = _state_path(cfg)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, _issue = _read_state_unlocked(path)
        return _public_metric(state["candidates"].get(str(candidate_id) or ""))


def all_metrics(cfg):
    """Return normalized metrics keyed by candidate id."""
    path = _state_path(cfg)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, _issue = _read_state_unlocked(path)
        return {
            candidate_id: _public_metric(metric)
            for candidate_id, metric in sorted(state["candidates"].items())
        }


def forget(cfg, candidate_id):
    """Forget one candidate and its bounded deduplication events."""
    candidate_id = str(candidate_id or "").strip()
    if not candidate_id:
        return False
    path = _state_path(cfg)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, issue = _read_state_unlocked(path)
        if issue or candidate_id not in state["candidates"]:
            return False
        del state["candidates"][candidate_id]
        state["events"] = {
            key: event for key, event in state["events"].items()
            if event.get("candidate_id") != candidate_id
        }
        state["updated_at"] = wikifs.utc_iso()
        _write_state_unlocked(path, state)
        return True


def _state_path(cfg):
    return Path(cfg.wiki_root) / schema.INBOX_DIR / STATE_FILE


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": "",
        "next_sequence": 0,
        "candidates": {},
        "events": {},
    }


def _empty_metric():
    return {
        "recall_count": 0,
        "total_score": 0.0,
        "max_score": 0.0,
        "query_hashes": [],
        "recall_days": [],
        "first_recalled_at": "",
        "last_recalled_at": "",
        "sources": [],
        "content_hash": "",
        "_sequence": 0,
    }


def _public_metric(raw):
    metric = _normalise_metric(raw)
    count = metric["recall_count"]
    return {
        "recall_count": count,
        "total_score": metric["total_score"],
        "average_score": metric["total_score"] / count if count else 0.0,
        "max_score": metric["max_score"],
        "query_hashes": list(metric["query_hashes"]),
        "unique_query_count": len(metric["query_hashes"]),
        "recall_days": list(metric["recall_days"]),
        "recall_day_count": len(metric["recall_days"]),
        "first_recalled_at": metric["first_recalled_at"],
        "last_recalled_at": metric["last_recalled_at"],
        "sources": list(metric["sources"]),
        "content_hash": metric["content_hash"],
    }


def _normalise_metric(raw):
    raw = raw if isinstance(raw, dict) else {}
    count = _nonnegative_int(raw.get("recall_count"))
    total = _nonnegative_float(raw.get("total_score"))
    maximum = _nonnegative_float(raw.get("max_score"))
    return {
        "recall_count": count,
        "total_score": total,
        "max_score": min(1.0, maximum),
        "query_hashes": _clean_list(
            raw.get("query_hashes"), _QUERY_HASH, MAX_QUERY_HASHES),
        "recall_days": _clean_list(
            raw.get("recall_days"), _DAY, MAX_RECALL_DAYS),
        "first_recalled_at": _clean_timestamp(raw.get("first_recalled_at")),
        "last_recalled_at": _clean_timestamp(raw.get("last_recalled_at")),
        "sources": _clean_sources(raw.get("sources")),
        "content_hash": _clean_content_hash(raw.get("content_hash")),
        "_sequence": _nonnegative_int(raw.get("_sequence")),
    }


def _read_state_unlocked(path):
    if not path.is_file():
        return _empty_state(), ""
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return _empty_state(), "recall state exceeded size limit"
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_state(), "recall state was unreadable"
    if not isinstance(raw, dict):
        return _empty_state(), "recall state was not an object"

    state = _empty_state()
    state["updated_at"] = _clean_timestamp(raw.get("updated_at"))
    state["next_sequence"] = _nonnegative_int(raw.get("next_sequence"))
    candidates = raw.get("candidates")
    if isinstance(candidates, dict):
        for candidate_id, metric in candidates.items():
            if isinstance(candidate_id, str) and candidate_id \
                    and len(candidate_id) <= 256:
                state["candidates"][candidate_id] = _normalise_metric(metric)
    events = raw.get("events")
    if isinstance(events, dict):
        for key, event in events.items():
            if not isinstance(key, str) or not _EVENT_HASH.match(key) \
                    or not isinstance(event, dict):
                continue
            candidate_id = str(event.get("candidate_id") or "")
            if candidate_id not in state["candidates"]:
                continue
            state["events"][key] = {
                "candidate_id": candidate_id,
                "at": _clean_timestamp(event.get("at")),
                "sequence": _nonnegative_int(event.get("sequence")),
            }
    state["next_sequence"] = max(
        [state["next_sequence"]]
        + [metric["_sequence"] for metric in state["candidates"].values()]
        + [event["sequence"] for event in state["events"].values()])
    _trim_state(state)
    return state, ""


def _write_state_unlocked(path, state):
    text = json.dumps(
        state, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), sort_keys=True) + "\n"
    wikifs.atomic_write(path, text)


def _trim_state(state):
    candidate_limit = max(0, int(MAX_CANDIDATES))
    if len(state["candidates"]) > candidate_limit:
        ordered = sorted(
            state["candidates"],
            key=lambda candidate_id: (
                state["candidates"][candidate_id].get("_sequence", 0),
                candidate_id),
            reverse=True,
        )
        keep = set(ordered[:candidate_limit])
        state["candidates"] = {
            key: value for key, value in state["candidates"].items()
            if key in keep
        }
    state["events"] = {
        key: event for key, event in state["events"].items()
        if event.get("candidate_id") in state["candidates"]
    }
    event_limit = max(0, int(MAX_EVENT_KEYS))
    if len(state["events"]) > event_limit:
        ordered = sorted(
            state["events"],
            key=lambda key: (state["events"][key].get("sequence", 0), key),
            reverse=True,
        )
        keep = set(ordered[:event_limit])
        state["events"] = {
            key: value for key, value in state["events"].items() if key in keep
        }


def _hash_query(query):
    normalized = " ".join(str(query or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()[
        :_QUERY_HASH_LEN]


def _hash_session(session_id):
    return hashlib.sha256(str(session_id or "").encode(
        "utf-8", "replace")).hexdigest()[:_QUERY_HASH_LEN]


def _event_key(candidate_id, session_hash, query_hash):
    payload = f"{candidate_id}\0{session_hash}\0{query_hash}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[
        :_EVENT_HASH_LEN]


def _result_score(result):
    for key in ("relevance", "score"):
        if key not in result:
            continue
        value = min(1.0, _nonnegative_float(result.get(key)))
        if value or result.get(key) in (0, 0.0, "0", "0.0"):
            return value
    return 0.0


def _nonnegative_float(value):
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _nonnegative_int(value):
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _bounded_unique(values, value, limit):
    limit = max(0, int(limit))
    values = [item for item in values if item != value]
    values.append(value)
    return values[-limit:] if limit else []


def _clean_list(value, pattern, limit):
    out = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and pattern.match(item):
                out = _bounded_unique(out, item, limit)
    return out


def _clean_source(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:64]


def _clean_sources(value):
    out = []
    if isinstance(value, list):
        for item in value:
            item = _clean_source(item)
            if item:
                out = _bounded_unique(out, item, MAX_SOURCES)
    return out


def _clean_timestamp(value):
    value = str(value or "")
    return value[:64] if value else ""


def _clean_content_hash(value):
    value = str(value or "").strip().lower()
    return value if _CONTENT_HASH.fullmatch(value) else ""
