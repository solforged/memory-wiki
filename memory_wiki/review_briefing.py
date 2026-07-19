"""Bounded, read-only briefing over reviewable memory candidates."""

from __future__ import annotations

from collections import Counter

from . import candidates, dream, dream_phases, hints, recall


MAX_ITEMS = 50
MAX_CONTENT = 2_000
MAX_PLAN_ITEMS = 100
_BUCKET_ORDER = {
    "corrections": 0,
    "ready": 1,
    "knowledge": 2,
    "needs_review": 3,
    "cleanup": 4,
    "deferred": 5,
}
_ACTION_BUCKET = {
    "supersede_existing": "corrections",
    "promote_profile": "ready",
    "promote_approved": "ready",
    "review_knowledge": "knowledge",
    "retarget": "needs_review",
    "needs_review": "needs_review",
    "consolidate_similar": "needs_review",
    "reject_stale": "cleanup",
    "reject_covered": "cleanup",
    "defer_unproven": "deferred",
}


def build(cfg, index, filters=None, limit=10):
    """Return one deduplicated review briefing without mutating the wiki.

    The briefing merges the review queue's candidate details with the dream
    planner's recommendations. Similar candidates remain one bundle.
    """
    item_limit = _coerce_limit(limit)
    queue = hints.build_review_queue(cfg, index, filters=filters)
    selected = queue["candidates"][:item_limit]
    selected_by_id = {item["id"]: item for item in selected if item.get("id")}

    plan_limit = min(MAX_PLAN_ITEMS, max(item_limit * 3, item_limit))
    plan = _read_only_plan(cfg, index, limit=plan_limit)
    items = []
    emitted = set()
    for suggestion in plan.get("suggestions", []):
        suggestion_ids = _suggestion_ids(suggestion)
        matched = [selected_by_id[cand_id] for cand_id in suggestion_ids
                   if cand_id in selected_by_id and cand_id not in emitted]
        if not matched:
            continue
        emitted.update(item["id"] for item in matched)
        items.append(_briefing_item(matched, suggestion))

    for candidate in selected:
        cand_id = candidate.get("id", "")
        if cand_id in emitted:
            continue
        action = ("promote_approved" if candidate.get("status") == "approved"
                  else "needs_review")
        reason = ("candidate is approved and ready for promotion"
                  if action == "promote_approved"
                  else "candidate was not ranked in the bounded dream plan")
        items.append(_briefing_item(
            [candidate], {"action": action, "reason": reason}))

    items.sort(key=lambda item: (
        _BUCKET_ORDER.get(item["bucket"], 99),
        item.get("ref", ""),
        ",".join(item.get("ids", [])),
    ))
    by_bucket = Counter(item["bucket"] for item in items)
    by_action = Counter(item["action"] for item in items)
    summary = dict(queue["summary"])
    summary.update({
        "returned": len(items),
        "candidate_count": sum(len(item["candidates"]) for item in items),
        "truncated": len(queue["candidates"]) > len(selected),
        "by_status": _candidate_counts(selected, "status"),
        "by_origin": _candidate_counts(selected, "origin"),
        "by_bucket": _sorted_counts(by_bucket),
        "by_action": _sorted_counts(by_action),
    })
    return {
        "summary": summary,
        "buckets": [name for name, _rank in sorted(
            _BUCKET_ORDER.items(), key=lambda pair: pair[1])],
        "items": items,
    }


def _briefing_item(candidate_items, suggestion):
    action = str(suggestion.get("action") or "needs_review")
    public_candidates = [_public_candidate(item) for item in candidate_items]
    first = public_candidates[0]
    out = {
        "bucket": _ACTION_BUCKET.get(action, "needs_review"),
        "action": action,
        "reason": _one_line(suggestion.get("reason"), 500),
        "ids": [item["id"] for item in public_candidates if item["id"]],
        "ref": first["ref"],
        "target": _one_line(
            suggestion.get("target") or first["effective_target"], 240),
        "safe_apply": bool(suggestion.get("safe_apply", False)),
        "candidates": public_candidates,
    }
    if len(public_candidates) == 1:
        out["id"] = first["id"]
        out["title"] = first["title"]
    for key, limit in (
            ("command", 500), ("suggested_target", 240)):
        if suggestion.get(key):
            out[key] = _one_line(suggestion[key], limit)
    for key in ("covered_by", "targets", "warnings"):
        values = suggestion.get(key)
        if isinstance(values, list):
            out[key] = [_one_line(value, 240) for value in values[:12]]
    return out


def _read_only_plan(cfg, index, limit):
    """Dream-compatible planning without the recall store's write lock."""
    if index is not None:
        index.refresh()
    pending_all = candidates.list_candidates(cfg, statuses=("pending",))
    raw_metrics, _issue = recall._read_state_unlocked(recall._state_path(cfg))
    signal_by_id = {
        candidate_id: recall._public_metric(metric)
        for candidate_id, metric in sorted(raw_metrics["candidates"].items())
    }
    phase_by_id = dream_phases.all_signals(cfg)
    rank_by_id = {}
    for candidate in pending_all:
        loaded = candidates.load(cfg, candidate["id"])
        evidence = loaded[3] if loaded else []
        candidate["grounded"] = dream._grounded_evidence(
            candidate.get("origin"), evidence, candidate.get("content", ""))
        rank_by_id[candidate["id"]] = dream.promotion_score(
            cfg, candidate, signals=signal_by_id.get(candidate["id"], {}),
            phase_signals=phase_by_id.get(candidate["id"], {}))
    pending = sorted(
        pending_all,
        key=lambda candidate: (
            not rank_by_id[candidate["id"]]["eligible"],
            -rank_by_id[candidate["id"]]["score"],
            candidate.get("ref", "")),
    )[:limit]
    hint_by_id = {
        candidate["id"]: hints.candidate_hints(index, candidate, pending_all)
        for candidate in pending
    }
    groups = dream._similar_groups(pending, hint_by_id)
    emitted_groups = set()
    suggestions = []
    for candidate in pending:
        candidate_id = candidate["id"]
        group = groups.get(candidate_id)
        if group:
            key = tuple(group)
            if key in emitted_groups:
                continue
            emitted_groups.add(key)
            members = [item for item in pending if item["id"] in group]
            suggestions.append(dream._group_suggestion(
                members, hint_by_id, rank_by_id))
        else:
            suggestions.append(dream._single_suggestion(
                candidate, hint_by_id[candidate_id],
                getattr(cfg, "candidate_stale_days", 30),
                rank_by_id[candidate_id]))
    return {"suggestions": suggestions}


def _public_candidate(item):
    content = str(item.get("content") or "")
    return {
        "id": _one_line(item.get("id"), 120),
        "title": _one_line(item.get("title"), 240),
        "content": content[:MAX_CONTENT],
        "content_truncated": len(content) > MAX_CONTENT,
        "preview": _one_line(item.get("preview"), 240),
        "status": _one_line(item.get("status"), 30),
        "kind": _one_line(item.get("kind"), 30),
        "confidence": _one_line(item.get("confidence"), 30),
        "origin": _one_line(item.get("origin"), 40),
        "session": _one_line(item.get("session"), 160),
        "created": _one_line(item.get("created"), 40),
        "age_days": item.get("age_days"),
        "stale": bool(item.get("stale")),
        "ref": _one_line(item.get("ref"), 240),
        "explicit_target": _one_line(item.get("explicit_target"), 240),
        "effective_target": _one_line(item.get("effective_target"), 240),
        "warnings": [_one_line(value, 80)
                     for value in item.get("warnings", [])[:12]],
    }


def _suggestion_ids(suggestion):
    values = suggestion.get("ids")
    if isinstance(values, list):
        return [str(value) for value in values]
    value = suggestion.get("id")
    return [str(value)] if value else []


def _coerce_limit(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError("review briefing limit must be an integer")
    if result < 1:
        raise ValueError("review briefing limit must be at least 1")
    return min(result, MAX_ITEMS)


def _one_line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _sorted_counts(counts):
    return {key: counts[key] for key in sorted(counts)}


def _candidate_counts(items, key):
    counts = Counter(str(item.get(key) or "unknown") for item in items)
    return _sorted_counts(counts)


build_review_briefing = build
