"""Lexical recall over the in-memory index: tf-idf with field boosts.

Small on purpose. Ranking only has to surface the right handful of
pages for the model to open with wiki_get; it is not a search engine.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from .index import tokenize

# additive per-token boosts on top of tf*idf
_BOOST_TITLE = 2.0
_BOOST_ALIAS = 2.0
_BOOST_TOPIC = 1.0
_BOOST_HEADING = 0.5
_JOURNAL_RECENCY_MULT = 1.5
_JOURNAL_RECENCY_DAYS = 7

SNIPPET_LEN = 160


def score_records(index, query, corpus="pages", type_filter=None,
                  include_candidate_statuses=("pending",)):
    """[(score, PageRecord)] sorted best-first; zero-score dropped."""
    qtokens = tokenize(query)
    if not qtokens:
        return []
    records = [r for r in index.records(corpus, type_filter)
               if r.ntype != "candidate" or not include_candidate_statuses
               or r.status in include_candidate_statuses]
    n = len(records)
    if not n:
        return []
    idf = {}
    for t in set(qtokens):
        df = sum(1 for r in records if r.tokens.get(t))
        idf[t] = math.log((n + 1) / (df + 1)) + 1.0
    cutoff = _recent_cutoff()
    scored = []
    for rec in records:
        s = 0.0
        for t in qtokens:
            tf = rec.tokens.get(t, 0)
            if not tf:
                continue
            bonus = (_BOOST_TITLE * (t in rec.title_tokens)
                     + _BOOST_ALIAS * (t in rec.alias_tokens)
                     + _BOOST_TOPIC * (t in rec.topic_tokens)
                     + _BOOST_HEADING * (t in rec.heading_tokens))
            s += idf[t] * (1.0 + math.log(tf) + bonus)
        if s <= 0:
            continue
        if rec.ntype == "journal" and rec.created >= cutoff:
            s *= _JOURNAL_RECENCY_MULT
        scored.append((s, rec))
    scored.sort(key=lambda x: (-x[0], x[1].rel))
    return scored


def _recent_cutoff():
    return (date.today() - timedelta(days=_JOURNAL_RECENCY_DAYS)).isoformat()


def snippet(rec, query):
    """First body fragment containing a query token, else the head."""
    body = rec.body_head
    qtokens = set(tokenize(query))
    lowered = body.lower()
    best = 0
    for t in qtokens:
        i = lowered.find(t)
        if i > -1:
            best = max(0, lowered.rfind(" ", 0, max(0, i - 40)))
            break
    frag = body[best:best + SNIPPET_LEN].strip()
    return frag


def result_dict(rec, s, query):
    out = {"ref": rec.rel, "title": rec.title, "type": rec.ntype,
           "score": round(s, 2), "snippet": snippet(rec, query)}
    if rec.ntype == "claim":
        out["confidence"], out["maturity"] = rec.confidence, rec.maturity
    if rec.ntype == "candidate":
        out.update(id=rec.cand_id, status=rec.status, kind=rec.kind)
    return out


def prefetch_block(index, cfg, query, session_id=""):
    """Compact cited recall block for per-turn injection ('' if nothing)."""
    scored = score_records(index, query, corpus="all")
    lines = ["## Wiki recall"]
    used = len(lines[0]) + 1
    shown = 0
    for s, rec in scored:
        if rec.ntype in ("profile",):     # already in the system prompt
            continue
        if rec.ntype == "candidate" and rec.status != "pending":
            continue
        if rec.ntype == "candidate":
            line = (f"- [cand {rec.cand_id}] (unreviewed {rec.kind}) "
                    f"{snippet(rec, query)}")
        else:
            meta = ""
            if rec.ntype == "claim" and rec.confidence:
                meta = f" ({rec.confidence}, {rec.maturity})"
            line = f"- [{rec.rel}] {rec.title}{meta}: {snippet(rec, query)}"
        if used + len(line) + 1 > cfg.prefetch_char_budget - 60:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
        if shown >= cfg.prefetch_limit:
            break
    if shown == 0:
        return ""
    lines.append("Cite paths when you use these; open with wiki_get.")
    return "\n".join(lines)
