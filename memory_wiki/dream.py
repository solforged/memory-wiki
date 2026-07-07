"""Dreaming pass: conservative review planning for staged memories.

A dream is intentionally not an autonomous knowledge writer. It looks at
pending candidates, applies the same duplicate/target hints the review UI
shows, groups obvious overlaps, and returns a curation plan. Optional
``apply_safe`` only promotes high-confidence profile/agent bullets that are
boring enough to be reversible by ``supersede``; knowledge pages remain
review-first.
"""

from __future__ import annotations

from . import candidates, hints, promote, schema

_SAFE_PROFILE_KINDS = {"preference", "note"}
_SAFE_CONFIDENCE = {"likely", "confident"}
_DIRECT_TARGET_PREFIXES = ("profile/",)
_EXCERPT = 140


def run(cfg, index, limit=None, apply_safe=False, actor="tool", session_id=""):
    """Return a curation plan, optionally applying conservative promotions.

    ``apply_safe`` promotes only suggestions marked ``safe_apply``. It never
    rejects candidates, retargets candidates, or promotes knowledge pages.
    """
    plan = build_plan(cfg, index, limit=limit)
    applied = []
    if apply_safe:
        for suggestion in plan["suggestions"]:
            if not suggestion.get("safe_apply"):
                continue
            result = promote.approve(cfg, index, suggestion["id"], actor=actor,
                                     session_id=session_id)
            applied.append({
                "id": suggestion["id"],
                "action": suggestion["action"],
                "ok": bool(result.get("ok")),
                "result": result.get("result", ""),
                "target": result.get("target", suggestion.get("target", "")),
                "error": result.get("error", ""),
            })
        if index is not None:
            index.refresh()
    plan["applied"] = applied
    return plan


def build_plan(cfg, index, limit=None):
    """Pure-ish planner over the current pending candidate queue."""
    if index is not None:
        index.refresh()
    pending_all = candidates.list_candidates(cfg, statuses=("pending",))
    max_items = _coerce_limit(limit, getattr(cfg, "dream_limit", 50))
    stale_days = getattr(cfg, "candidate_stale_days", 30)
    pending = pending_all[:max_items]
    hint_by_id = {c["id"]: hints.candidate_hints(index, c, pending)
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
            suggestions.append(_group_suggestion(members, hint_by_id))
            continue
        suggestions.append(_single_suggestion(cand, hint_by_id[cand_id],
                                              stale_days))

    summary = {}
    for suggestion in suggestions:
        summary[suggestion["action"]] = summary.get(suggestion["action"], 0) + 1
    return {
        "pending": len(pending_all),
        "considered": len(pending),
        "limit": max_items,
        "summary": summary,
        "suggestions": suggestions,
    }


def _coerce_limit(value, default):
    try:
        n = int(value if value is not None else default)
    except (TypeError, ValueError):
        n = int(default or 50)
    return max(1, n)


def _single_suggestion(cand, hint, stale_days):
    target = hint.get("default_target") or cand.get("target") or ""
    base = {
        "id": cand["id"],
        "kind": cand.get("kind", ""),
        "confidence": cand.get("confidence", ""),
        "target": target,
        "excerpt": _excerpt(cand.get("content", "")),
        "safe_apply": False,
    }
    age = cand.get("age_days")
    if age is not None and stale_days and age > stale_days \
            and not _correction_like(cand):
        base.update({
            "action": "reject_stale",
            "reason": f"pending for {age} days (stale after {stale_days}); "
                      "recall no longer surfaces it",
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
    elif _safe_profile_candidate(cand, target):
        base.update({
            "action": "promote_profile",
            "reason": "high-confidence direct profile/agent memory",
            "safe_apply": True,
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    else:
        base.update({
            "action": "needs_review",
            "reason": "not safe enough for automatic promotion",
            "command": f"hermes memory-wiki review --approve {cand['id']}",
        })
    return base


def _group_suggestion(members, hint_by_id):
    ids = [c["id"] for c in members]
    targets = sorted({(hint_by_id[c["id"]].get("default_target")
                       or c.get("target") or "") for c in members})
    excerpts = []
    for c in members:
        hint = hint_by_id[c["id"]]
        item = {"id": c["id"], "excerpt": _excerpt(c.get("content", ""))}
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
            and target.startswith(_DIRECT_TARGET_PREFIXES)
            and "\n" not in content
            and not candidates.page_shaped(content))


def _excerpt(text):
    text = " ".join((text or "").split())
    if len(text) <= _EXCERPT:
        return text
    return text[:_EXCERPT].rsplit(" ", 1)[0] + "..."
