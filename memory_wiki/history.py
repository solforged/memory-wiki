"""Bounded, privacy-aware lifecycle history over decisions.jsonl."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import schema


MAX_ITEMS = 100
_MAX_BYTES = 8 * 1024 * 1024
_DETAIL_KEYS = frozenset((
    "reason", "kind", "origin", "confidence", "status", "ref",
    "superseded_by",
))


def build(cfg, limit=50, actions=None, target="", session="", since=""):
    """Return newest lifecycle events without modifying or repairing audit data."""
    root = Path(getattr(cfg, "wiki_root", cfg)).expanduser()
    item_limit = _coerce_limit(limit)
    action_filter = _values(actions)
    since_value = _parse_since(since)
    rows, issues = _read_tail(root / schema.DECISIONS_FILE)
    matched = []
    for row in rows:
        if action_filter and str(row.get("action") or "") not in action_filter:
            continue
        if target and str(row.get("target") or "") != str(target):
            continue
        if session and str(row.get("session") or "") != str(session):
            continue
        if since_value is not None:
            timestamp = _timestamp(row.get("ts"))
            if timestamp is None or timestamp < since_value:
                continue
        matched.append(_public_event(row))
    matched.sort(key=lambda item: (item.get("ts", ""), item["event_id"]),
                 reverse=True)
    returned = matched[:item_limit]
    by_action = Counter(item["action"] for item in matched)
    by_actor = Counter(item["actor"] or "unknown" for item in matched)
    return {
        "summary": {
            "read": len(rows),
            "matched": len(matched),
            "returned": len(returned),
            "truncated": len(matched) > len(returned),
            "by_action": _counts(by_action),
            "by_actor": _counts(by_actor),
        },
        "events": returned,
        "lifecycles": _lifecycles(returned),
        "issues": issues[:20],
    }


def _read_tail(path):
    if not path.is_file():
        return [], []
    issues = []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            truncated = size > _MAX_BYTES
            if truncated:
                handle.seek(size - _MAX_BYTES)
            raw = handle.read(_MAX_BYTES)
    except OSError:
        return [], ["decisions.jsonl is unreadable"]
    if truncated:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1:] if boundary >= 0 else b""
        issues.append("history was limited to the newest audit bytes")
    rows = []
    invalid = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            invalid += 1
    if invalid:
        issues.append(f"ignored {invalid} invalid audit line(s)")
    return rows, issues


def _public_event(row):
    timestamp = _line(row.get("ts"), 80)
    action = _line(row.get("action"), 80)
    cand_id = _line(row.get("id"), 160)
    target = _line(row.get("target"), 240)
    actor = _line(row.get("actor"), 80)
    session = str(row.get("session") or "")
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    safe_detail = {}
    for key in sorted(_DETAIL_KEYS):
        value = detail.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in (None, ""):
            safe_detail[key] = _line(value, 500)
    seed = "\0".join((timestamp, action, cand_id, target,
                       _line(row.get("hash"), 160)))
    return {
        "event_id": "event-" + hashlib.sha256(
            seed.encode("utf-8", "replace")).hexdigest()[:12],
        "ts": timestamp,
        "action": action,
        "id": cand_id,
        "target": target,
        "actor": actor,
        "session_hash": (_session_hash(session) if session else ""),
        "content_hash": _line(row.get("hash"), 160),
        "detail": safe_detail,
    }


def _lifecycles(events):
    grouped = {}
    for event in reversed(events):
        cand_id = event.get("id")
        if not cand_id:
            continue
        life = grouped.setdefault(cand_id, {
            "id": cand_id,
            "event_count": 0,
            "first_at": event.get("ts", ""),
            "last_at": event.get("ts", ""),
            "current_action": event.get("action", ""),
            "target": "",
            "actors": [],
        })
        life["event_count"] += 1
        life["last_at"] = max(life["last_at"], event.get("ts", ""))
        if event.get("ts", "") >= life["last_at"]:
            life["current_action"] = event.get("action", "")
        if event.get("target"):
            life["target"] = event["target"]
        if event.get("actor") and event["actor"] not in life["actors"]:
            life["actors"].append(event["actor"])
    return sorted(grouped.values(), key=lambda item: (
        item["last_at"], item["id"]), reverse=True)


def _parse_since(value):
    value = str(value or "").strip()
    if not value:
        return None
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("history since must be an ISO-8601 timestamp")
    return parsed


def _timestamp(value):
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _session_hash(value):
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:10]


def _values(value):
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def _coerce_limit(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError("history limit must be an integer")
    if result < 1:
        raise ValueError("history limit must be at least 1")
    return min(result, MAX_ITEMS)


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _counts(counts):
    return {key: counts[key] for key in sorted(counts)}


build_history = build
