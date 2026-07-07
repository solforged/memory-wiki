"""Review-time hints: likely duplicates, better targets, and quality
warnings for pending candidates.

Advisory only. Hints are shown in the review queue (tool and CLI) and
never retarget, reject, or otherwise change promotion behavior.
"""

from __future__ import annotations

import re

from . import layers, promote, schema, wikifs
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
