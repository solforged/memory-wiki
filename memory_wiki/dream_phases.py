"""Deterministic light and REM phases plus bounded reinforcement state.

The phase store is advisory. Markdown candidates remain the source of truth,
and a missing or malformed store simply removes phase boosts. DREAMS.md is a
human report and is never read here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import candidates, recall, wikifs
from .index import tokenize

STATE_RELATIVE_PATH = ".memory-wiki/dream-state.json"
STATE_VERSION = 1
MAX_CANDIDATES = 512
MAX_STATE_BYTES = 1_000_000
MAX_ERROR_CHARS = 500

LIGHT_BOOST_MAX = 0.06
REM_BOOST_MAX = 0.09
LIGHT_BOOST_PER_HIT = 0.01
REM_BOOST_PER_HIT = 0.015
PHASE_HALF_LIFE_DAYS = 14

LIGHT_DEDUPE_SIMILARITY = 0.88
REM_MIN_TRUTH_CONFIDENCE = 0.45
REM_TRUTH_LIMIT = 3
REM_THEME_LIMIT = 8

_CONTENT_HASH = re.compile(r"^[0-9a-f]{16}$")
_BASIS_HASH = re.compile(r"^[0-9a-f]{24}$")
_THEME_STOP = frozenset({
    "agent", "always", "candidate", "current", "fact", "keep", "memory",
    "never", "note", "notes", "prefer", "prefers", "said", "something",
    "thing", "things", "use", "user", "uses", "using",
})


def state_path(cfg):
    return Path(cfg.wiki_root) / STATE_RELATIVE_PATH


def read_state(cfg):
    """Return normalized state. Corruption degrades to an empty store."""
    state, _issue = _read_state_unlocked(state_path(cfg))
    return _public_state(state)


def status(cfg):
    """Compact state summary for CLI, tools, and dashboards."""
    path = state_path(cfg)
    state, issue = _read_state_unlocked(path)
    entries = state["candidates"].values()
    return {
        "path": STATE_RELATIVE_PATH,
        "exists": path.is_file(),
        "issue": issue,
        "updated_at": state["updated_at"],
        "candidate_count": len(state["candidates"]),
        "light_hits": sum(item["light_hits"] for item in entries),
        "rem_hits": sum(item["rem_hits"]
                        for item in state["candidates"].values()),
        "sweep": dict(state["sweep"]),
    }


def all_signals(cfg):
    """Phase signals keyed by candidate id."""
    return read_state(cfg)["candidates"]


def record_phase(cfg, phase, items, now=None, require_pending=False):
    """Increment hash-bound phase hits once per candidate in this call."""
    if phase not in ("light", "rem"):
        raise ValueError("phase must be 'light' or 'rem'")
    prepared = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("id") or "").strip()
        content_hash = _clean_hash(
            item.get("content_hash") or item.get("hash"))
        if not candidate_id or len(candidate_id) > 256 or not content_hash:
            continue
        basis = _clean_basis(
            item.get(f"{phase}_basis") or item.get("phase_basis"))
        prepared[candidate_id] = (content_hash, basis)
    if not prepared:
        return {"recorded": 0, "unchanged": 0, "issue": ""}

    path = state_path(cfg)
    stamp = _iso(now)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, issue = _read_state_unlocked(path)
        recorded = 0
        unchanged = 0
        for candidate_id, prepared_signal in sorted(prepared.items()):
            content_hash, basis = prepared_signal
            if require_pending:
                loaded = candidates.load(cfg, candidate_id)
                if not loaded or loaded[1].get("status") != "pending" \
                        or candidates.content_hash(loaded[2]) != content_hash:
                    continue
            current = _normalise_signal(
                state["candidates"].get(candidate_id))
            if current["content_hash"] != content_hash:
                current = _empty_signal()
                current["content_hash"] = content_hash
            basis_key = f"last_{phase}_basis"
            if basis and current[basis_key] == basis:
                unchanged += 1
                continue
            state["next_sequence"] += 1
            if phase == "light":
                current["light_hits"] += 1
                current["last_light_at"] = stamp
            else:
                current["rem_hits"] += 1
                current["last_rem_at"] = stamp
            if basis:
                current[basis_key] = basis
            current["_sequence"] = state["next_sequence"]
            state["candidates"][candidate_id] = current
            recorded += 1
        state["updated_at"] = stamp
        _trim_state(state)
        _write_state_unlocked(path, state)
    return {"recorded": recorded, "unchanged": unchanged, "issue": issue}


def mark_sweep(cfg, sweep_status, phase="", error="", now=None):
    """Persist sweep progress without changing candidate signals."""
    if sweep_status not in ("running", "complete", "failed"):
        raise ValueError("unknown sweep status")
    stamp = _iso(now)
    path = state_path(cfg)
    with wikifs.wiki_lock(cfg.wiki_root):
        state, _issue = _read_state_unlocked(path)
        sweep = state["sweep"]
        if sweep_status == "running" and not phase:
            sweep = _empty_sweep()
            sweep["started_at"] = stamp
        elif not sweep["started_at"]:
            sweep["started_at"] = stamp
        previous = sweep.get("last_phase", "")
        if sweep_status == "running" and phase and previous and previous != phase:
            prior = sweep["phases"].get(previous, _empty_phase())
            if prior["status"] == "running":
                prior["status"] = "complete"
                prior["completed_at"] = stamp
                sweep["phases"][previous] = prior
        sweep["status"] = sweep_status
        if phase:
            phase = str(phase)[:32]
            sweep["last_phase"] = phase
            record = sweep["phases"].get(phase, _empty_phase())
            if not record["started_at"]:
                record["started_at"] = stamp
            record["status"] = sweep_status
            record["completed_at"] = (
                stamp if sweep_status in ("complete", "failed") else "")
            record["error"] = (str(error or "")[:MAX_ERROR_CHARS]
                               if sweep_status == "failed" else "")
            sweep["phases"][phase] = record
        sweep["error"] = str(error or "")[:MAX_ERROR_CHARS]
        sweep["completed_at"] = (stamp if sweep_status in ("complete", "failed")
                                 else "")
        state["sweep"] = sweep
        state["updated_at"] = stamp
        _write_state_unlocked(path, state)
    return dict(sweep)


def run_tracked(cfg, phases, runner, now=None):
    """Run one serialized phase sequence with durable phase telemetry."""
    sequence = tuple(str(phase).strip().lower() for phase in phases)
    if not sequence or len(set(sequence)) != len(sequence) \
            or any(phase not in ("light", "rem", "deep")
                   for phase in sequence):
        raise ValueError("phases must be a unique light, rem, deep sequence")
    results = {}
    current_phase = sequence[0]
    with sweep_lock(cfg):
        mark_sweep(cfg, "running", now=now)
        try:
            for current_phase in sequence:
                mark_sweep(cfg, "running", phase=current_phase, now=now)
                results[current_phase] = runner(current_phase, results)
            mark_sweep(cfg, "complete", phase=current_phase, now=now)
        except Exception as exc:
            try:
                mark_sweep(
                    cfg, "failed", phase=current_phase,
                    error=f"{type(exc).__name__}: {exc}", now=now)
            except Exception:
                pass
            raise
    return results


def run_light(cfg, index, limit=None, now=None):
    """Stage a deterministic, deduplicated view of pending candidates."""
    if index is not None:
        index.refresh()
    pending = candidates.list_candidates(cfg, statuses=("pending",))
    metrics = recall.all_metrics(cfg)
    ranked = []
    for cand in pending:
        content_hash = candidates.content_hash(cand.get("content", ""))
        signal = metrics.get(cand["id"], {})
        if str(signal.get("content_hash") or "") != content_hash:
            signal = {}
        count = _nonnegative_int(signal.get("recall_count"))
        average = _bounded_float(signal.get("average_score"))
        query_count = max(
            _nonnegative_int(signal.get("unique_query_count")),
            len(signal.get("query_hashes") or []),
        )
        item = {
            "id": cand["id"],
            "ref": cand.get("ref", ""),
            "content_hash": content_hash,
            "content": cand.get("content", ""),
            "excerpt": _excerpt(cand.get("content", "")),
            "kind": cand.get("kind", ""),
            "confidence": cand.get("confidence", ""),
            "topics": list(cand.get("topics") or []),
            "recall_count": count,
            "average_score": round(average, 4),
            "unique_queries": query_count,
            "recall_days": list(signal.get("recall_days") or []),
            "last_recalled_at": str(signal.get("last_recalled_at") or ""),
        }
        item["light_basis"] = _fingerprint({
            "content_hash": content_hash,
            "recall_count": count,
            "average_score": item["average_score"],
            "unique_queries": query_count,
            "recall_days": sorted(set(item["recall_days"])),
            "last_recalled_at": item["last_recalled_at"],
        })
        ranked.append(item)
    ranked.sort(key=lambda item: (
        -item["recall_count"], -item["average_score"],
        -item["unique_queries"], item["ref"], item["id"],
    ))

    staged = []
    token_sets = []
    seen_hashes = set()
    deduplicated = 0
    max_items = _coerce_limit(limit, getattr(cfg, "dream_limit", 50))
    for item in ranked:
        item_tokens = set(tokenize(item["content"]))
        duplicate = item["content_hash"] in seen_hashes or any(
            _token_overlap(item_tokens, other) >= LIGHT_DEDUPE_SIMILARITY
            for other in token_sets if item_tokens and other
        )
        if duplicate:
            deduplicated += 1
            continue
        if len(staged) >= max_items:
            break
        staged.append(item)
        token_sets.append(item_tokens)
        seen_hashes.add(item["content_hash"])

    recorded = record_phase(
        cfg, "light", staged, now=now, require_pending=True)
    return {
        "phase": "light",
        "input_count": len(pending),
        "staged_count": len(staged),
        "deduplicated": deduplicated,
        "recorded_hits": recorded["recorded"],
        "unchanged_hits": recorded["unchanged"],
        "state_issue": recorded["issue"],
        "items": staged,
    }


def run_rem(cfg, light_result, limit=None, now=None):
    """Derive recurring themes and lasting-truth suggestions from light."""
    live = []
    skipped_changed = 0
    for item in (light_result or {}).get("items", []):
        loaded = candidates.load(cfg, item.get("id", ""))
        if not loaded:
            skipped_changed += 1
            continue
        _path, fm, content, _evidence = loaded
        content_hash = candidates.content_hash(content)
        if fm.get("status") != "pending" \
                or content_hash != item.get("content_hash"):
            skipped_changed += 1
            continue
        refreshed = dict(item)
        refreshed.update({
            "content": content,
            "content_hash": content_hash,
            "excerpt": _excerpt(content),
            "topics": list(fm.get("topics") or []),
        })
        live.append(refreshed)

    candidate_tokens = {
        item["id"]: _concept_tokens(item) for item in live
    }
    frequencies = Counter(
        token for tokens in candidate_tokens.values() for token in tokens)
    theme_names = [
        token for token, count in sorted(
            frequencies.items(), key=lambda pair: (-pair[1], pair[0]))
        if count >= 2
    ][ :REM_THEME_LIMIT]
    themes = [{
        "theme": token,
        "count": frequencies[token],
        "candidate_ids": sorted(
            item["id"] for item in live
            if token in candidate_tokens[item["id"]]),
    } for token in theme_names]

    truths = []
    for item in live:
        recall_count = _nonnegative_int(item.get("recall_count"))
        average = _bounded_float(item.get("average_score"))
        frequency = min(1.0, math.log1p(recall_count) / math.log1p(6))
        consolidation = min(1.0, len(set(item.get("recall_days") or [])) / 3)
        conceptual = min(1.0, len(candidate_tokens[item["id"]]) / 6)
        confidence = (0.45 * average + 0.25 * frequency
                      + 0.20 * consolidation + 0.10 * conceptual)
        if confidence < REM_MIN_TRUTH_CONFIDENCE:
            continue
        truth = {
            "id": item["id"],
            "content_hash": item["content_hash"],
            "excerpt": item["excerpt"],
            "confidence": round(confidence, 4),
            "recall_count": recall_count,
            "themes": sorted(token for token in theme_names
                             if token in candidate_tokens[item["id"]]),
        }
        truth["rem_basis"] = _fingerprint({
            "light_basis": item.get("light_basis", ""),
            "confidence": truth["confidence"],
            "themes": truth["themes"],
            "recall_count": recall_count,
            "recall_days": sorted(set(item.get("recall_days") or [])),
        })
        truths.append(truth)
    truths.sort(key=lambda item: (-item["confidence"], item["id"]))
    truth_limit = min(REM_TRUTH_LIMIT, _coerce_limit(
        limit, getattr(cfg, "dream_limit", 50)))
    truths = truths[:truth_limit]
    recorded = record_phase(
        cfg, "rem", truths, now=now, require_pending=True)
    return {
        "phase": "rem",
        "considered": len(live),
        "skipped_changed": skipped_changed,
        "themes": themes,
        "lasting_truths": truths,
        "recorded_hits": recorded["recorded"],
        "unchanged_hits": recorded["unchanged"],
        "state_issue": recorded["issue"],
    }


def score_reinforcement(entry, content_hash, now=None):
    """Return recency-decayed light and REM boosts for exact content."""
    signal = _normalise_signal(entry)
    if not content_hash or signal["content_hash"] != content_hash:
        signal = _empty_signal()
    light = min(LIGHT_BOOST_MAX,
                signal["light_hits"] * LIGHT_BOOST_PER_HIT)
    rem = min(REM_BOOST_MAX, signal["rem_hits"] * REM_BOOST_PER_HIT)
    light *= _decay(signal["last_light_at"], now)
    rem *= _decay(signal["last_rem_at"], now)
    return {
        "light_hits": signal["light_hits"],
        "rem_hits": signal["rem_hits"],
        "last_light_at": signal["last_light_at"],
        "last_rem_at": signal["last_rem_at"],
        "light_boost": round(light, 4),
        "rem_boost": round(rem, 4),
        "total_boost": round(light + rem, 4),
        "half_life_days": PHASE_HALF_LIFE_DAYS,
    }


@contextmanager
def sweep_lock(cfg, timeout=5.0):
    """Serialize whole sweeps across processes without holding the wiki lock."""
    path = Path(cfg.wiki_root) / ".memory-wiki" / ".dream.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise wikifs.LockTimeout(
                        f"could not lock {path} within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _empty_state():
    return {
        "version": STATE_VERSION,
        "updated_at": "",
        "next_sequence": 0,
        "sweep": _empty_sweep(),
        "candidates": {},
    }


def _empty_sweep():
    return {
        "status": "never",
        "started_at": "",
        "completed_at": "",
        "last_phase": "",
        "error": "",
        "phases": {},
    }


def _empty_phase():
    return {"status": "never", "started_at": "", "completed_at": "",
            "error": ""}


def _empty_signal():
    return {
        "content_hash": "",
        "light_hits": 0,
        "rem_hits": 0,
        "last_light_at": "",
        "last_rem_at": "",
        "last_light_basis": "",
        "last_rem_basis": "",
        "_sequence": 0,
    }


def _read_state_unlocked(path):
    if not path.is_file():
        return _empty_state(), ""
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return _empty_state(), "dream state exceeded size limit"
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty_state(), "dream state was unreadable"
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return _empty_state(), "dream state had an unsupported shape"
    state = _empty_state()
    state["updated_at"] = _clean_timestamp(raw.get("updated_at"))
    state["next_sequence"] = _nonnegative_int(raw.get("next_sequence"))
    state["sweep"] = _normalise_sweep(raw.get("sweep"))
    raw_candidates = raw.get("candidates")
    if isinstance(raw_candidates, dict):
        for candidate_id, signal in raw_candidates.items():
            if isinstance(candidate_id, str) and candidate_id \
                    and len(candidate_id) <= 256:
                normalized = _normalise_signal(signal)
                if normalized["content_hash"]:
                    state["candidates"][candidate_id] = normalized
    state["next_sequence"] = max(
        [state["next_sequence"]]
        + [item["_sequence"] for item in state["candidates"].values()])
    _trim_state(state)
    return state, ""


def _write_state_unlocked(path, state):
    _trim_state(state)
    text = _serialise(state)
    while len(text.encode("utf-8")) > MAX_STATE_BYTES \
            and state["candidates"]:
        oldest = min(
            state["candidates"],
            key=lambda key: (state["candidates"][key]["_sequence"], key))
        del state["candidates"][oldest]
        text = _serialise(state)
    wikifs.atomic_write(path, text)


def _serialise(state):
    return json.dumps(state, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=True) + "\n"


def _trim_state(state):
    limit = max(0, int(MAX_CANDIDATES))
    if len(state["candidates"]) <= limit:
        return
    ordered = sorted(
        state["candidates"],
        key=lambda key: (state["candidates"][key]["_sequence"], key),
        reverse=True)
    keep = set(ordered[:limit])
    state["candidates"] = {
        key: value for key, value in state["candidates"].items()
        if key in keep
    }


def _public_state(state):
    return {
        "version": STATE_VERSION,
        "updated_at": state["updated_at"],
        "next_sequence": state["next_sequence"],
        "sweep": dict(state["sweep"]),
        "candidates": {
            key: {name: value for name, value in signal.items()
                  if name != "_sequence"}
            for key, signal in sorted(state["candidates"].items())
        },
    }


def _normalise_signal(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "content_hash": _clean_hash(raw.get("content_hash")),
        "light_hits": _nonnegative_int(raw.get("light_hits")),
        "rem_hits": _nonnegative_int(raw.get("rem_hits")),
        "last_light_at": _clean_timestamp(raw.get("last_light_at")),
        "last_rem_at": _clean_timestamp(raw.get("last_rem_at")),
        "last_light_basis": _clean_basis(raw.get("last_light_basis")),
        "last_rem_basis": _clean_basis(raw.get("last_rem_basis")),
        "_sequence": _nonnegative_int(raw.get("_sequence")),
    }


def _normalise_sweep(raw):
    raw = raw if isinstance(raw, dict) else {}
    status_value = str(raw.get("status") or "never")
    if status_value not in ("never", "running", "complete", "failed"):
        status_value = "never"
    phases = {}
    raw_phases = raw.get("phases")
    if isinstance(raw_phases, dict):
        for name in ("light", "rem", "deep"):
            item = raw_phases.get(name)
            if not isinstance(item, dict):
                continue
            phase_status = str(item.get("status") or "never")
            if phase_status not in ("never", "running", "complete", "failed"):
                phase_status = "never"
            phases[name] = {
                "status": phase_status,
                "started_at": _clean_timestamp(item.get("started_at")),
                "completed_at": _clean_timestamp(item.get("completed_at")),
                "error": str(item.get("error") or "")[:MAX_ERROR_CHARS],
            }
    return {
        "status": status_value,
        "started_at": _clean_timestamp(raw.get("started_at")),
        "completed_at": _clean_timestamp(raw.get("completed_at")),
        "last_phase": str(raw.get("last_phase") or "")[:32],
        "error": str(raw.get("error") or "")[:MAX_ERROR_CHARS],
        "phases": phases,
    }


def _clean_timestamp(value):
    value = str(value or "").strip()
    if not value or len(value) > 64:
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return value


def _clean_hash(value):
    value = str(value or "").strip().lower()
    return value if _CONTENT_HASH.fullmatch(value) else ""


def _clean_basis(value):
    value = str(value or "").strip().lower()
    return value if _BASIS_HASH.fullmatch(value) else ""


def _fingerprint(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def _nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_float(value):
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, min(1.0, number))


def _coerce_limit(value, default):
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        number = int(default or 50)
    return max(0, number)


def _token_overlap(left, right):
    return len(left & right) / max(1, min(len(left), len(right)))


def _concept_tokens(item):
    topics = set(tokenize(" ".join(str(topic)
                                   for topic in item.get("topics") or [])))
    content = set(tokenize(item.get("content", "")))
    return (topics | content) - _THEME_STOP


def _excerpt(text, limit=180):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _iso(now=None):
    if now is None:
        return wikifs.utc_iso()
    if isinstance(now, datetime):
        value = now
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    value = str(now).strip()
    if not _clean_timestamp(value):
        raise ValueError("now must be an ISO timestamp or datetime")
    return value


def _decay(timestamp, now=None):
    if not timestamp:
        return 0.0
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        current = (now if isinstance(now, datetime) else
                   datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                   if now is not None else datetime.now(timezone.utc))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    age_days = max(0.0, (current - then).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / PHASE_HALF_LIFE_DAYS)
