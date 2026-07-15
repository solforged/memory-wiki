"""Lexical recall over the in-memory index: tf-idf with field boosts.

Small on purpose. Ranking only has to surface the right handful of
pages for the model to open with wiki_get; it is not a search engine.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

from . import wikifs
from .index import tokenize

# additive per-token boosts on top of tf*idf
_BOOST_TITLE = 2.0
_BOOST_ALIAS = 2.0
_BOOST_TOPIC = 1.0
_BOOST_HEADING = 0.5
# Coverage bonus is capped below this query's maximum raw lexical score.
_COVERAGE_BONUS_WEIGHT = 0.95
_JOURNAL_RECENCY_MULT = 1.5
_JOURNAL_RECENCY_DAYS = 7

# hybrid blend (OpenClaw uses the same 70/30 split for vector/BM25)
_VECTOR_WEIGHT = 0.7
_LEXICAL_WEIGHT = 0.3
_HYBRID_MIN_SCORE = 0.05

_SNIPPET_TOKEN = re.compile(r"[a-z0-9]+")
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
    raw_scored = []
    query_terms = set(qtokens)
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
        raw_scored.append((s, rec))
    max_raw = max((s for s, _ in raw_scored), default=0.0)
    scored = [
        (s + _COVERAGE_BONUS_WEIGHT * max_raw
         * _query_term_coverage(query_terms, rec.tokens) ** 3, rec)
        for s, rec in raw_scored
    ]
    scored.sort(key=lambda x: (-x[0], x[1].rel))
    return scored


def _query_term_coverage(query_terms, record_terms):
    """Fraction of distinct query terms present among record terms."""
    if not query_terms:
        return 0.0
    return len(query_terms.intersection(record_terms)) / len(query_terms)


def query_coverage(query, rec):
    """Absolute lexical relevance, independent of other result scores."""
    return _query_term_coverage(set(tokenize(query)), rec.tokens)


def _recent_cutoff():
    return (date.today() - timedelta(days=_JOURNAL_RECENCY_DAYS)).isoformat()


def hybrid_scores(index, embedder, query, corpus="pages", type_filter=None,
                  include_candidate_statuses=("pending",)):
    """Lexical tf-idf blended with embedding similarity when available.

    Falls back to pure lexical while the embedder is loading, failed,
    or absent. Vector-only hits (zero lexical overlap) are included;
    that is the point of the semantic layer.
    """
    lexical = score_records(index, query, corpus, type_filter,
                            include_candidate_statuses)
    if embedder is None:
        return lexical
    records = [r for r in index.records(corpus, type_filter)
               if r.ntype != "candidate" or not include_candidate_statuses
               or r.status in include_candidate_statuses]
    try:
        sims = embedder.scores(query, records)
    except Exception:
        sims = {}
    if not sims:
        return lexical
    max_lex = max((s for s, _ in lexical), default=0.0)
    lex_by_rel = {rec.rel: s / max_lex for s, rec in lexical} if max_lex \
        else {}
    scored = []
    for rec in records:
        s = (_LEXICAL_WEIGHT * lex_by_rel.get(rec.rel, 0.0)
             + _VECTOR_WEIGHT * sims.get(rec.rel, 0.0))
        if s >= _HYBRID_MIN_SCORE:
            scored.append((s, rec))
    scored.sort(key=lambda x: (-x[0], x[1].rel))
    return scored


def snippet(rec, query):
    """Fragment from the canonical body containing the first query match."""
    body = getattr(rec, "body", "") or rec.body_head
    lowered = body.lower()
    query_terms = set(tokenize(query))
    first = next(
        (match.start() for match in _SNIPPET_TOKEN.finditer(lowered)
         if match.group() in query_terms),
        None,
    )
    best = 0
    if first is not None:
        best = max(0, lowered.rfind(" ", 0, max(0, first - 40)))
    return body[best:best + SNIPPET_LEN].strip()


def result_dict(rec, s, query):
    out = {"ref": rec.rel, "title": rec.title, "type": rec.ntype,
           "score": round(s, 2), "snippet": snippet(rec, query)}
    if rec.ntype == "claim":
        out["confidence"], out["maturity"] = rec.confidence, rec.maturity
    if rec.ntype == "candidate":
        out.update(id=rec.cand_id, status=rec.status, kind=rec.kind,
                   created=rec.created, content_hash=rec.content_hash)
    return out


def prefetch_block(index, cfg, query, session_id="", embedder=None):
    """Compact cited recall block for per-turn injection ('' if nothing)."""
    scored = hybrid_scores(index, embedder, query, corpus="all")
    results = [result_dict(rec, score, query) for score, rec in scored]
    return prefetch_results_block(results, cfg)


def prefetch_results_block(results, cfg, selected=None):
    """Compact recall block from backend-neutral result dictionaries."""
    lines = ["## Wiki recall"]
    used = len(lines[0]) + 1
    shown = 0
    for result in results:
        ntype = result.get("type", "")
        if ntype == "profile":     # already in the system prompt
            continue
        if ntype == "candidate" and result.get("status") != "pending":
            continue
        if ntype == "candidate":
            age = wikifs.days_since(result.get("created", ""))
            if age is not None and age > cfg.candidate_stale_days:
                continue          # stale proposal; review queue still lists it
            staged = f", staged {age}d ago" if age else ""
            line = (f"- [cand {result.get('id', '')}] "
                    f"(unreviewed {result.get('kind', '')}{staged}) "
                    f"{result.get('snippet', '')}")
        else:
            meta = ""
            if ntype == "claim" and result.get("confidence"):
                meta = (f" ({result.get('confidence')}, "
                        f"{result.get('maturity', '')})")
            line = (f"- [{result.get('ref', '')}] {result.get('title', '')}"
                    f"{meta}: {result.get('snippet', '')}")
        if used + len(line) + 1 > cfg.prefetch_char_budget - 60:
            break
        lines.append(line)
        if selected is not None:
            selected.append(result)
        used += len(line) + 1
        shown += 1
        if shown >= cfg.prefetch_limit:
            break
    if shown == 0:
        return ""
    lines.append("Cite paths when you use these; open with wiki_get.")
    return "\n".join(lines)
