"""Review-time hints: likely duplicates, better targets, and quality
warnings for pending candidates.

Advisory only. Hints are shown in the review queue (tool and CLI) and
never retarget, reject, or otherwise change promotion behavior.
"""

from __future__ import annotations

import re

from . import candidates, layers, promote, schema, wikifs
from .candidates import _overlap
from .index import tokenize

_COVERED = 0.8    # same bar as staging dedup
_SIMILAR = 0.6    # near-duplicate pending candidates
_MIN_TOKENS = 4   # fewer -> needs_context

_TASTE_TERMS = re.compile(
    r"\b(?:music|album|artist|band|playlist|song|genre|soundtrack|ost|"
    r"listening|vinyl|tastes?)\b", re.IGNORECASE)
_URL = re.compile(r"https?://")
_REF_TERMS = re.compile(
    r"(?:^|\s)[~/][\w./-]+|\.(?:plist|ya?ml|json|toml|conf|cfg|ini)\b|"
    r"\b(?:localhost|config|hostname|daemon|launchd|systemd|cron)\b|"
    r"\bport \d", re.IGNORECASE)


def build_review_queue(cfg, index, filters=None, limit=None):
    """Build the shared pending-candidate review read model.

    ``filters`` accepts scalar values or sequences for ``kind``,
    ``confidence``, ``target`` (explicit or effective target),
    ``explicit_target``, and ``effective_target``. ``stale_only`` and
    ``min_age`` are scalar predicates. The optional limit is applied after
    filtering, preserving the inbox's deterministic filename order.
    """
    filters = dict(filters or {})
    _validate_filters(filters)
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise ValueError("review queue limit must be an integer")
        if limit < 1:
            raise ValueError("review queue limit must be at least 1")
    pending = candidates.list_candidates(cfg, statuses=("pending",))
    approved = candidates.list_candidates(cfg, statuses=("approved",))
    reviewable = sorted(pending + approved, key=lambda item: item.get("ref", ""))
    stale_days = getattr(cfg, "candidate_stale_days", 30)
    items = [_review_item(cfg, index, cand, pending, stale_days)
             for cand in reviewable]
    items = [item for item in items if _matches_filters(item, filters)]
    if limit is not None:
        items = items[:limit]
    summary = {
        "total_pending": len(pending),
        "total_approved": len(approved),
        "total_reviewable": len(reviewable),
        "matched": len(items),
        "stale_count": sum(1 for item in items if item["stale"]),
        "by_kind": _counts(items, "kind"),
        "by_confidence": _counts(items, "confidence"),
        "by_effective_target": _counts(items, "effective_target"),
    }
    return {"summary": summary, "candidates": items}


def _review_item(cfg, index, cand, pending, stale_days):
    content = cand.get("content") or ""
    preview_source = content
    if candidates.page_shaped(content):
        _frontmatter, preview_source = wikifs.parse_frontmatter(content)
    preview = " ".join(preview_source.split())[:120]
    title = " ".join(promote._title_from(content).split())
    hint = candidate_hints(index, cand, pending)
    route = promote._resolve(cfg, cand, content)
    effective_target = route.get("target") or hint.get("default_target", "")
    age = cand.get("age_days")
    item = {
        "id": cand.get("id", ""),
        "title": title or cand.get("id", ""),
        "content": content,
        "preview": preview,
        "excerpt": preview,
        "status": cand.get("status", ""),
        "kind": cand.get("kind", ""),
        "confidence": cand.get("confidence", ""),
        "origin": cand.get("origin", ""),
        "session": cand.get("session", ""),
        "turn": cand.get("turn", 0),
        "created": cand.get("created", ""),
        "supersedes": cand.get("supersedes", ""),
        "age_days": age,
        "stale": bool(stale_days and age is not None and age > stale_days),
        "target": cand.get("target", ""),
        "explicit_target": cand.get("target", ""),
        "effective_target": effective_target,
        "section": cand.get("section", ""),
        "effective_section": route.get("section", ""),
        "ref": cand.get("ref", ""),
        "default_target": hint.get("default_target", ""),
        "warnings": hint.get("warnings", []),
        "covered_by": hint.get("covered_by", []),
        "similar_candidates": hint.get("similar_candidates", []),
        "suggested_target": hint.get("suggested_target", ""),
    }
    return item


def _validate_filters(filters):
    for name, allowed in (("kind", schema.CANDIDATE_KINDS),
                          ("confidence", schema.CONFIDENCE)):
        invalid = _filter_values(filters.get(name)) - set(allowed)
        if invalid:
            values = ", ".join(sorted(invalid))
            raise ValueError(f"unknown {name} filter: {values}")
    min_age = filters.get("min_age")
    if min_age is None:
        min_age = filters.get("min_age_days")
    if min_age is None:
        return
    try:
        min_age = int(min_age)
    except (TypeError, ValueError):
        raise ValueError("minimum review age must be an integer")
    if min_age < 0:
        raise ValueError("minimum review age cannot be negative")
    filters["min_age"] = min_age


def _matches_filters(item, filters):
    for name in ("kind", "confidence", "explicit_target",
                 "effective_target"):
        values = _filter_values(filters.get(name))
        if values and item.get(name) not in values:
            return False
    targets = _filter_values(filters.get("target"))
    if targets and not ({item.get("explicit_target"),
                         item.get("effective_target")} & targets):
        return False
    if filters.get("stale_only") and not item.get("stale"):
        return False
    min_age = filters.get("min_age")
    if min_age is not None:
        age = item.get("age_days")
        if age is None or age < min_age:
            return False
    return True

def _filter_values(value):
    if value is None or value == "":
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def _counts(items, key):
    counts = {}
    for item in items:
        value = item.get(key, "")
        counts[value] = counts.get(value, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def candidate_hints(index, cand, pending=None):
    """Hint dict for one pending candidate (empty values omitted):
    default_target, suggested_target, warnings, covered_by,
    similar_candidates."""
    content = cand.get("content") or ""
    ctokens = set(tokenize(content))
    hints = {
        "default_target": _default_target(cand),
        "suggested_target": _suggest_target(cand, content),
        "warnings": _warnings(content, ctokens),
        "covered_by": _covered_by(index, ctokens) if index is not None else [],
        "similar_candidates": _similar(cand, pending or [], ctokens),
    }
    return {k: v for k, v in hints.items() if v}


def _default_target(cand):
    """Where approve would write this candidate as-is."""
    if cand.get("target"):
        return cand["target"]
    routed = promote.ROUTES.get(cand.get("kind") or "fact")
    if routed is None:
        return ""
    target = routed[0]
    return layers.journal_rel() if target == "journal" else target


def _suggest_target(cand, content):
    if _TASTE_TERMS.search(content):
        suggestion = "tastes/tastes.md"
    elif _URL.search(content):
        suggestion = f"refs/{wikifs.slugify(content, max_len=40)}.md"
    elif _REF_TERMS.search(content):
        suggestion = "profile/agent.md"
    else:
        return ""
    return "" if suggestion == (cand.get("target") or "") else suggestion


def _warnings(content, ctokens):
    out = []
    if content.rstrip().endswith("..."):
        out.append("truncated")
    if len(ctokens) < _MIN_TOKENS:
        out.append("needs_context")
    return out


def _covered_by(index, ctokens):
    """Refs whose existing content already says (most of) this."""
    if not ctokens:
        return []
    out = set()
    for rec in index.records("all"):
        if rec.ntype in ("profile", "taste"):
            # bullet-level match; body_head keeps "- " prefixes
            for line in rec.body_head.split("- "):
                ltokens = set(tokenize(line))
                if len(ltokens) >= 3 and _overlap(ctokens, ltokens) >= _COVERED:
                    out.add(rec.rel)
                    break
        elif rec.ntype == "candidate":
            if rec.status != "promoted":
                continue
            head = rec.body_head.partition(schema.EVIDENCE_HEADING)[0]
            htokens = set(tokenize(head))
            if len(htokens) >= 3 and _overlap(ctokens, htokens) >= _COVERED:
                out.add(rec.rel)
        elif rec.ntype in schema.KNOWLEDGE_TYPES:
            if len(rec.title_tokens) >= 3 and \
                    _overlap(ctokens, rec.title_tokens) >= _COVERED:
                out.add(rec.rel)
    return sorted(out)


def _similar(cand, pending, ctokens):
    """Ids of other pending candidates that largely overlap this one."""
    out = []
    for other in pending:
        if other.get("id") == cand.get("id"):
            continue
        otokens = set(tokenize(other.get("content") or ""))
        if len(otokens) >= 3 and _overlap(ctokens, otokens) >= _SIMILAR:
            out.append(other["id"])
    return out
