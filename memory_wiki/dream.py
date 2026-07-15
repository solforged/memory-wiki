"""Dreaming pass: conservative review planning for staged memories.

A dream is intentionally not an autonomous knowledge writer. It looks at
pending candidates, applies the same duplicate/target hints the review UI
shows, groups obvious overlaps, and returns a curation plan. Optional
``apply_safe`` only promotes high-confidence profile/agent bullets that are
boring enough to be reversible by ``supersede``; knowledge pages remain
review-first.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from . import candidates, hints, promote, schema
from .index import tokenize

logger = logging.getLogger(__name__)

_SAFE_PROFILE_KINDS = {"preference", "note"}
_SAFE_CONFIDENCE = {"likely", "confident"}
_GROUNDED_ORIGINS = {"sync_turn", "pre_compress", "session_end"}
_EXCERPT = 140
_WEIGHTS = {
    "frequency": 0.24,
    "relevance": 0.30,
    "diversity": 0.15,
    "recency": 0.15,
    "consolidation": 0.10,
    "conceptual": 0.06,
}


def run(cfg, index, limit=None, apply_safe=False, actor="tool", session_id="",
        write_report=False, scheduled=False):
    """Return a curation plan, optionally applying conservative promotions.

    ``apply_safe`` promotes only suggestions marked ``safe_apply``. It never
    rejects candidates, retargets candidates, or promotes knowledge pages.
    """
    plan = build_plan(cfg, index, limit=limit, scheduled=scheduled)
    applied = []
    if apply_safe:
        for suggestion in plan["suggestions"]:
            if not suggestion.get("safe_apply"):
                continue
            try:
                result = promote.approve(
                    cfg, index, suggestion["id"], actor=actor,
                    session_id=session_id,
                    precondition=_safe_apply_precondition_for(suggestion),
                    decision_detail={
                        "dream": suggestion.get("promotion", {})})
            except Exception as exc:
                logger.warning("memory-wiki: dream promotion failed: %s", exc)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            applied.append({
                "id": suggestion["id"],
                "action": suggestion["action"],
                "ok": bool(result.get("ok")),
                "result": result.get("result", ""),
                "target": result.get("target", suggestion.get("target", "")),
                "error": result.get("error", ""),
            })
        if index is not None:
            try:
                index.refresh()
            except Exception as exc:
                logger.warning(
                    "memory-wiki: index refresh after dream failed: %s", exc)
    plan["applied"] = applied
    if write_report:
        plan["report"] = append_report(cfg, plan)
    return plan


def run_sweep(cfg, index, limit=None, apply_safe=False, actor="tool",
              session_id="", write_report=False, now=None,
              scheduled=False):
    """Run one serialized light -> REM -> deep sweep.

    Light and REM only derive and record advisory reinforcement. The existing
    deep pass remains the sole phase allowed to promote, through ``apply_safe``.
    """
    from . import dream_phases

    def run_phase(phase, completed):
        if phase == "light":
            return dream_phases.run_light(
                cfg, index, limit=limit, now=now)
        if phase == "rem":
            return dream_phases.run_rem(
                cfg, completed["light"], limit=limit, now=now)
        return run(
                cfg, index, limit=limit, apply_safe=apply_safe, actor=actor,
                session_id=session_id, write_report=write_report,
                scheduled=scheduled)

    phases = dream_phases.run_tracked(
        cfg, ("light", "rem", "deep"), run_phase, now=now)
    return {
        "phase_order": ["light", "rem", "deep"],
        "light": phases["light"],
        "rem": phases["rem"],
        "deep": phases["deep"],
        "status": dream_phases.status(cfg),
    }


def build_plan(cfg, index, limit=None, scheduled=False):
    """Pure-ish planner over the current pending candidate queue."""
    if index is not None:
        index.refresh()
    pending_all = candidates.list_candidates(cfg, statuses=("pending",))
    max_items = _coerce_limit(limit, getattr(cfg, "dream_limit", 50))
    stale_days = getattr(cfg, "candidate_stale_days", 30)
    from . import recall
    from . import dream_phases
    signal_by_id = recall.all_metrics(cfg)
    phase_by_id = dream_phases.all_signals(cfg)
    rank_by_id = {}
    for cand in pending_all:
        loaded = candidates.load(cfg, cand["id"])
        evidence = loaded[3] if loaded else []
        cand["grounded"] = _grounded_evidence(
            cand.get("origin"), evidence, cand.get("content", ""))
        rank_by_id[cand["id"]] = promotion_score(
            cfg, cand, signals=signal_by_id.get(cand["id"]),
            phase_signals=phase_by_id.get(cand["id"]), scheduled=scheduled)
    pending = sorted(
        pending_all,
        key=lambda cand: (
            not rank_by_id[cand["id"]]["eligible"],
            -rank_by_id[cand["id"]]["score"],
            cand.get("ref", "")),
    )[:max_items]
    hint_by_id = {c["id"]: hints.candidate_hints(index, c, pending_all)
                  for c in pending}
    groups = _similar_groups(pending, hint_by_id)
    emitted_groups = set()
    suggestions = []

    for cand in pending:
        cand_id = cand["id"]
        group = groups.get(cand_id)
        if group:
            key = tuple(group)
            if key in emitted_groups:
                continue
            emitted_groups.add(key)
            members = [c for c in pending if c["id"] in group]
            suggestions.append(_group_suggestion(
                members, hint_by_id, rank_by_id))
            continue
        suggestions.append(_single_suggestion(cand, hint_by_id[cand_id],
                                              stale_days,
                                              rank_by_id[cand_id]))

    summary = {}
    for suggestion in suggestions:
        summary[suggestion["action"]] = summary.get(suggestion["action"], 0) + 1
    return {
        "pending": len(pending_all),
        "considered": len(pending),
        "limit": max_items,
        "promotion_policy": _policy(cfg, scheduled=scheduled),
        "summary": summary,
        "suggestions": suggestions,
    }


def explain(cfg, index, cand_id, scheduled=False):
    """Explain one candidate's current promotion score and recommendation."""
    if index is not None:
        index.refresh()
    cand_id = str(cand_id or "").strip()
    matches = [item for item in candidates.list_candidates(cfg, statuses=())
               if item.get("id") == cand_id]
    if not matches:
        raise ValueError(f"no candidate with id {cand_id}")
    cand = matches[0]
    loaded = candidates.load(cfg, cand_id)
    evidence = loaded[3] if loaded else []
    cand["grounded"] = _grounded_evidence(
        cand.get("origin"), evidence, cand.get("content", ""))
    ranking = promotion_score(cfg, cand, scheduled=scheduled)
    suggestion = None
    if cand.get("status") == "pending":
        pending = candidates.list_candidates(cfg, statuses=("pending",))
        hint = hints.candidate_hints(index, cand, pending)
        suggestion = _single_suggestion(
            cand, hint, getattr(cfg, "candidate_stale_days", 30), ranking)
    return {
        "id": cand_id,
        "status": cand.get("status", ""),
        "ref": cand.get("ref", ""),
        "promotion": ranking,
        "suggestion": suggestion,
    }


def _coerce_limit(value, default):
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = int(default or 50)
    return max(1, n)


def _single_suggestion(cand, hint, stale_days, ranking):
    target = hint.get("default_target") or cand.get("target") or ""
    base = {
        "id": cand["id"],
        "kind": cand.get("kind", ""),
        "confidence": cand.get("confidence", ""),
        "target": target,
        "excerpt": _excerpt(cand.get("content", "")),
        "content_hash": candidates.content_hash(cand.get("content", "")),
        "safe_apply": False,
        "promotion": ranking,
    }
    age = cand.get("age_days")
    if age is not None and stale_days and age > stale_days \
            and not _correction_like(cand) \
            and not _recently_recalled(ranking, stale_days):
        base.update({
            "action": "reject_stale",
            "reason": f"pending for {age} days (stale after {stale_days}); "
                      "no recent recall evidence supports keeping it",
            "command": f"hermes memory-wiki review --reject {cand['id']} --reason stale",
        })
    elif hint.get("covered_by") and _correction_like(cand):
        # token overlap cannot tell confirmation from contradiction, and a
        # correction exists to contradict; never recommend rejecting it
        base.update({
            "action": "supersede_existing",
            "reason": "correction overlaps an existing memory it may "
                      "contradict; supersede or edit that entry, don't reject",
            "covered_by": hint["covered_by"],
        })
    elif hint.get("covered_by"):
        base.update({
            "action": "reject_covered",
            "reason": "candidate appears already represented",
            "covered_by": hint["covered_by"],
            "command": f"hermes memory-wiki review --reject {cand['id']} --reason 'already covered'",
        })
    elif hint.get("similar_candidates"):
        # Normally handled by _similar_groups; keep this as a defensive path.
        base.update({
            "action": "consolidate_similar",
            "reason": "candidate overlaps pending candidates",
            "similar_candidates": hint["similar_candidates"],
        })
    elif hint.get("warnings"):
        base.update({
            "action": "needs_review",
            "reason": "quality warning: " + ", ".join(hint["warnings"]),
            "warnings": hint["warnings"],
        })
    elif hint.get("suggested_target"):
        base.update({
            "action": "retarget",
            "reason": "default route is probably not the best home",
            "suggested_target": hint["suggested_target"],
            "command": (f"hermes memory-wiki review --approve {cand['id']} "
                        f"--target {hint['suggested_target']}"),
        })
    elif _knowledge_candidate(cand, target):
        base.update({
            "action": "review_knowledge",
            "reason": "knowledge pages remain review-first",
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    elif _safe_profile_candidate(cand, target) and ranking["eligible"]:
        base.update({
            "action": "promote_profile",
            "reason": "high-confidence profile memory passed recall gates",
            "safe_apply": True,
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    elif _safe_profile_candidate(cand, target):
        base.update({
            "action": "defer_unproven",
            "reason": _gate_reason(ranking),
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    else:
        base.update({
            "action": "needs_review",
            "reason": "not safe enough for automatic promotion",
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    return base


def _group_suggestion(members, hint_by_id, rank_by_id):
    ids = [c["id"] for c in members]
    targets = sorted({(hint_by_id[c["id"]].get("default_target")
                       or c.get("target") or "") for c in members})
    excerpts = []
    for c in members:
        hint = hint_by_id[c["id"]]
        item = {"id": c["id"], "excerpt": _excerpt(c.get("content", ""))}
        item["promotion"] = rank_by_id[c["id"]]
        for key in ("covered_by", "warnings", "suggested_target"):
            if hint.get(key):
                item[key] = hint[key]
        excerpts.append(item)
    out = {
        "action": "consolidate_similar",
        "ids": ids,
        "kind": ",".join(sorted({c.get("kind", "") for c in members if c.get("kind")})),
        "target": targets[0] if len(targets) == 1 else "",
        "targets": targets,
        "reason": "pending candidates overlap; review as one bundle",
        "excerpts": excerpts,
        "safe_apply": False,
        "command": "review together; approve one edited memory and reject the stale duplicates",
    }
    if all(hint_by_id[c["id"]].get("covered_by") for c in members) \
            and not any(_correction_like(c) for c in members):
        rejects = " ".join(f"--reject {i}" for i in ids)
        out.update({
            "action": "reject_covered",
            "reason": "every candidate in the bundle appears already represented",
            "covered_by": sorted({ref for c in members
                                  for ref in hint_by_id[c["id"]]["covered_by"]}),
            "command": f"hermes memory-wiki review {rejects} --reason 'already covered'",
        })
    return out


def _similar_groups(pending, hint_by_id):
    ids = {c["id"] for c in pending}
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        if a not in parent or b not in parent:
            return
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cand in pending:
        for other in hint_by_id.get(cand["id"], {}).get("similar_candidates", []):
            if other in ids:
                union(cand["id"], other)
    buckets = {}
    for cand_id in ids:
        buckets.setdefault(find(cand_id), []).append(cand_id)
    out = {}
    for group in buckets.values():
        if len(group) < 2:
            continue
        ordered = sorted(group)
        for cand_id in ordered:
            out[cand_id] = ordered
    return out


def _correction_like(cand):
    return cand.get("kind") == "correction" or bool(cand.get("supersedes"))


def _knowledge_candidate(cand, target):
    if cand.get("kind") in ("claim", "concept"):
        return True
    folder = (target or "").split("/", 1)[0]
    return schema.DIR_TYPE.get(folder) in schema.KNOWLEDGE_TYPES


def _safe_profile_candidate(cand, target):
    content = cand.get("content") or ""
    return (cand.get("kind") in _SAFE_PROFILE_KINDS
            and cand.get("confidence") in _SAFE_CONFIDENCE
            and cand.get("origin") in _GROUNDED_ORIGINS
            and cand.get("grounded") is True
            and not cand.get("target")
            and target in schema.PROFILE_PAGES
            and "\n" not in content
            and not candidates.page_shaped(content))


def _safe_apply_precondition(fm, content, evidence, expected_hash=""):
    """Recheck autonomous-promotion safety while approve holds the wiki lock."""
    route = promote.ROUTES.get(fm.get("kind") or "fact")
    target = route[0] if route else ""
    cand = dict(fm)
    cand["content"] = content
    cand["grounded"] = _grounded_evidence(
        fm.get("origin"), evidence, content)
    if fm.get("status") != "pending":
        return "dream candidate is no longer pending"
    if expected_hash and candidates.content_hash(content) != expected_hash:
        return "candidate content changed after dream planning"
    if not _safe_profile_candidate(cand, target):
        return "candidate changed and no longer meets safe dream policy"
    return ""


def _safe_apply_precondition_for(suggestion):
    expected_hash = suggestion.get("content_hash", "")
    return lambda fm, content, evidence: _safe_apply_precondition(
        fm, content, evidence, expected_hash=expected_hash)


def _grounded_evidence(origin, evidence, content):
    if origin not in _GROUNDED_ORIGINS:
        return False
    source = " ".join(str(line) for line in (evidence or [])
                      if str(line).startswith("> [user, turn "))
    content_tokens = set(tokenize(content))
    if not source or not content_tokens:
        return False
    source_tokens = set(tokenize(source))
    return len(content_tokens & source_tokens) / len(content_tokens) >= 0.7


def _policy(cfg, scheduled=False):
    if scheduled:
        return {
            "mode": "scheduled",
            "min_score": 0.80,
            "min_recall_count": 3,
            "min_unique_queries": 3,
            "recency_half_life_days": max(1, int(
                getattr(cfg, "dream_recency_half_life_days", 14))),
        }
    return {
        "mode": "manual",
        "min_score": max(0.0, min(1.0, float(
            getattr(cfg, "dream_min_score", 0.75)))),
        "min_recall_count": max(0, int(
            getattr(cfg, "dream_min_recall_count", 3))),
        "min_unique_queries": max(0, int(
            getattr(cfg, "dream_min_unique_queries", 2))),
        "recency_half_life_days": max(1, int(
            getattr(cfg, "dream_recency_half_life_days", 14))),
    }


def promotion_score(cfg, cand, signals=None, phase_signals=None, now=None,
                    scheduled=False):
    """OpenClaw-style evidence score and independent threshold gates."""
    from . import dream_phases, recall

    policy = _policy(cfg, scheduled=scheduled)
    signals = (recall.metrics(cfg, cand.get("id", ""))
               if signals is None else signals)
    expected_hash = (candidates.content_hash(cand.get("content", ""))
                     if cand.get("content")
                     else str(cand.get("hash") or "").strip())
    signal_hash = str((signals or {}).get("content_hash") or "").strip()
    if expected_hash and signal_hash != expected_hash:
        signals = {}
    recall_count = max(0, int(signals.get("recall_count") or 0))
    total_score = max(0.0, float(signals.get("total_score") or 0.0))
    avg_score = min(1.0, total_score / max(1, recall_count))
    query_hashes = list(signals.get("query_hashes") or [])
    recall_days = list(signals.get("recall_days") or [])
    unique_queries = max(int(signals.get("unique_queries") or 0),
                         len(query_hashes))
    context_diversity = max(unique_queries, len(recall_days))
    frequency = min(1.0, math.log1p(recall_count) / math.log1p(10))
    diversity = min(1.0, context_diversity / 5.0)
    recency = _recency(
        signals.get("last_recalled_at") or signals.get("last_recalled"),
        policy["recency_half_life_days"])
    consolidation = _consolidation(recall_days)
    conceptual = min(1.0, len(set(cand.get("topics") or [])) / 6.0)
    components = {
        "frequency": frequency,
        "relevance": avg_score,
        "diversity": diversity,
        "recency": recency,
        "consolidation": consolidation,
        "conceptual": conceptual,
    }
    base_score = min(1.0, sum(_WEIGHTS[key] * components[key]
                              for key in _WEIGHTS))
    if phase_signals is None:
        phase_signals = dream_phases.all_signals(cfg).get(cand.get("id", ""))
    reinforcement = dream_phases.score_reinforcement(
        phase_signals, expected_hash, now=now)
    score = min(1.0, base_score + reinforcement["total_boost"])
    gates = {
        "score": score >= policy["min_score"],
        "recall_count": recall_count >= policy["min_recall_count"],
        "unique_queries": unique_queries >= policy["min_unique_queries"],
    }
    return {
        "eligible": all(gates.values()),
        "score": round(score, 4),
        "base_score": round(base_score, 4),
        "phase_reinforcement": reinforcement,
        "recall_count": recall_count,
        "unique_queries": unique_queries,
        "recall_days": recall_days,
        "last_recalled_at": str(signals.get("last_recalled_at") or ""),
        "components": {key: round(value, 4)
                       for key, value in components.items()},
        "gates": gates,
        "thresholds": policy,
    }


def _recently_recalled(ranking, max_age_days):
    value = str(ranking.get("last_recalled_at") or "")
    if not value:
        return False
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
    return 0 <= age <= max(0, max_age_days)


def _recency(last_recalled_at, half_life_days):
    if not last_recalled_at:
        return 0.0
    try:
        stamp = str(last_recalled_at).replace("Z", "+00:00")
        then = datetime.fromisoformat(stamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    age_days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds()
                   / 86400.0)
    return math.exp(-math.log(2) * age_days / half_life_days)


def _consolidation(recall_days):
    days = sorted(set(str(day) for day in recall_days if day))
    if not days:
        return 0.0
    if len(days) == 1:
        return 0.2
    try:
        first = datetime.fromisoformat(days[0]).date()
        last = datetime.fromisoformat(days[-1]).date()
        span_days = max(0, (last - first).days)
    except ValueError:
        span_days = 0
    spacing = min(1.0, math.log1p(len(days) - 1) / math.log1p(4))
    span = min(1.0, span_days / 7.0)
    return min(1.0, 0.55 * spacing + 0.45 * span)


def _gate_reason(ranking):
    missing = [name for name, passed in ranking["gates"].items() if not passed]
    return ("defer until organic recall evidence passes: "
            + ", ".join(missing))


def append_report(cfg, plan):
    """Append a human-readable, non-promotable dream report."""
    from . import wikifs

    path = Path(cfg.wiki_root) / "DREAMS.md"
    stamp = wikifs.utc_iso()
    policy = plan.get("promotion_policy", {})
    lines = [f"## Dream sweep {stamp}", "",
             (f"Policy: score >= {policy.get('min_score')}, recalls >= "
              f"{policy.get('min_recall_count')}, unique queries >= "
              f"{policy.get('min_unique_queries')}"), ""]
    for item in plan.get("suggestions", []):
        ids = item.get("ids") or [item.get("id")]
        ranking = item.get("promotion") or {}
        score = ranking.get("score")
        suffix = f", score {score:.3f}" if isinstance(score, (int, float)) else ""
        lines.append(f"- {', '.join(i for i in ids if i)}: "
                     f"{item.get('action', 'review')}{suffix}")
    if not plan.get("suggestions"):
        lines.append("- No pending candidates.")
    lines += ["", "Dream reports are review artifacts, never promotion inputs.", ""]
    with wikifs.wiki_lock(cfg.wiki_root):
        current = wikifs.read_text(path) or "# Dream diary\n\n"
        wikifs.atomic_write(path, current.rstrip() + "\n\n" + "\n".join(lines))
    return "DREAMS.md"


def _excerpt(text):
    text = " ".join((text or "").split())
    if len(text) <= _EXCERPT:
        return text
    return text[:_EXCERPT].rsplit(" ", 1)[0] + "..."
