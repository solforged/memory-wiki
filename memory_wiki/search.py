"""Lexical recall over the in-memory index: tf-idf with field boosts.

Small on purpose. Ranking only has to surface the right handful of
pages for the model to open with wiki_get; it is not a search engine.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta

from . import chunks, temporal, wikifs
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
_RAW_ARCHIVE_MULT = 0.5

# hybrid blend (OpenClaw uses the same 70/30 split for vector/BM25)
_VECTOR_WEIGHT = 0.7
_LEXICAL_WEIGHT = 0.3
_HYBRID_MIN_SCORE = 0.05

_UNIT_KIND_MULTIPLIER = {
    "page": 1.0,
    "heading": 1.08,
    "claim": 1.12,
    "evidence": 1.12,
}

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
        elif rec.ntype == "raw":
            # Evidence archives should be available to recall without
            # routinely outranking reviewed concepts, claims, and syntheses.
            s *= _RAW_ARCHIVE_MULT
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


def score_units(index, query, corpus="pages", type_filter=None,
                granularity="auto", as_of=None, include_historical=False,
                page_refs=None, include_candidate_statuses=("pending",)):
    """Lexically rank disposable retrieval units from canonical pages."""
    qtokens = tokenize(query)
    if not qtokens:
        return []
    units = [
        unit for unit in index.units(
            corpus, type_filter, granularity, as_of,
            include_historical, page_refs)
        if unit.ntype != "candidate" or not include_candidate_statuses
        or getattr(unit.page, "status", "") in include_candidate_statuses
    ]
    if not units:
        return []
    idf = {}
    for token in set(qtokens):
        frequency = sum(1 for unit in units if unit.tokens.get(token))
        idf[token] = math.log((len(units) + 1) / (frequency + 1)) + 1.0
    query_terms = set(qtokens)
    cutoff = _recent_cutoff()
    raw_scored = []
    for unit in units:
        score = 0.0
        for token in qtokens:
            frequency = unit.tokens.get(token, 0)
            if not frequency:
                continue
            bonus = (
                _BOOST_TITLE * (token in unit.title_tokens)
                + _BOOST_ALIAS * (token in unit.alias_tokens)
                + _BOOST_TOPIC * (token in unit.topic_tokens)
                + _BOOST_HEADING * (token in unit.heading_tokens)
            )
            score += idf[token] * (1.0 + math.log(frequency) + bonus)
        if score <= 0:
            continue
        score *= _UNIT_KIND_MULTIPLIER.get(unit.kind, 1.0)
        if unit.ntype == "journal" and unit.created >= cutoff:
            score *= _JOURNAL_RECENCY_MULT
        elif unit.ntype == "raw":
            score *= _RAW_ARCHIVE_MULT
        raw_scored.append((score, unit))
    max_raw = max((score for score, _unit in raw_scored), default=0.0)
    scored = [
        (score + _COVERAGE_BONUS_WEIGHT * max_raw
         * _query_term_coverage(query_terms, unit.tokens) ** 3, unit)
        for score, unit in raw_scored
    ]
    scored.sort(key=lambda item: (-item[0], item[1].ref))
    return scored


def hybrid_unit_scores(index, embedder, query, corpus="pages",
                       type_filter=None, granularity="auto", as_of=None,
                       include_historical=False, page_refs=None):
    """Blend lexical and cached unit vectors, with lexical fallback."""
    lexical = score_units(
        index, query, corpus, type_filter, granularity, as_of,
        include_historical, page_refs)
    if embedder is None:
        return lexical
    units = list(index.units(
        corpus, type_filter, granularity, as_of,
        include_historical, page_refs))
    units = [unit for unit in units
             if unit.ntype != "candidate"
             or getattr(unit.page, "status", "") == "pending"]
    try:
        similarities = embedder.unit_scores(query, units)
    except Exception:
        similarities = {}
    if not similarities:
        return lexical
    max_lexical = max((score for score, _unit in lexical), default=0.0)
    lexical_by_ref = {
        unit.ref: score / max_lexical for score, unit in lexical
    } if max_lexical else {}
    scored = []
    for unit in units:
        score = (
            _LEXICAL_WEIGHT * lexical_by_ref.get(unit.ref, 0.0)
            + _VECTOR_WEIGHT * similarities.get(unit.ref, 0.0)
        )
        if score >= _HYBRID_MIN_SCORE:
            scored.append((score, unit))
    scored.sort(key=lambda item: (-item[0], item[1].ref))
    return scored


def provider_unit_results(provider, query, *, corpus="pages", type_filter=None,
                          limit=5, granularity="auto", as_of=None,
                          include_historical=False, source="wiki_search",
                          session_id="", record_recall=True):
    """Run page candidate generation, then derive and rank local units."""
    if granularity not in chunks.GRANULARITIES:
        raise ValueError(f"unsupported retrieval granularity: {granularity}")
    if as_of is not None:
        temporal.coerce_as_of(as_of)
    if granularity == "page":
        return provider.search_results(
            query, corpus=corpus, type_filter=type_filter, limit=limit,
            source=source, session_id=session_id,
            record_recall=record_recall)

    candidate_limit = min(20, max(limit * 4, 10))
    page_results = provider.search_results(
        query, corpus=corpus, type_filter=type_filter, limit=candidate_limit,
        source=source, session_id=session_id, record_recall=False)
    backend = str(getattr(provider, "last_search_backend", "local") or "local")
    qmd_candidates = backend == "qmd"
    page_refs = ([item.get("ref") for item in page_results
                  if isinstance(item, dict) and item.get("ref")]
                 if qmd_candidates else None)
    candidate_backends = {
        str(item.get("ref")): str(item.get("backend") or backend)
        for item in page_results if isinstance(item, dict) and item.get("ref")
    }
    scored = hybrid_unit_scores(
        provider.index, getattr(provider, "embed", None), query,
        corpus=corpus, type_filter=type_filter, granularity=granularity,
        as_of=as_of, include_historical=include_historical,
        page_refs=page_refs)
    if qmd_candidates:
        scored = enrich_page_candidates(
            provider.index, scored, page_results, corpus, type_filter,
            granularity, as_of, include_historical)
    if granularity == "auto":
        scored = _diversify_pages(scored)
    embedder = getattr(provider, "embed", None)
    semantic_ready = bool(
        embedder is not None and getattr(embedder, "ready", False)
        and not getattr(embedder, "failed", False))
    if granularity == "auto" and not scored:
        fallback = page_results
        if not include_historical:
            fallback = [
                result for result in page_results
                if not _historical_page_match(
                    provider.index, result, query, as_of)
            ]
        results = list(fallback[:limit])
    else:
        results = [
            unit_result_dict(
                unit, score, query, backend=backend,
                semantic_ready=semantic_ready, as_of=as_of,
                candidate_backend=candidate_backends.get(unit.page_ref, ""))
            for score, unit in scored[:limit]
        ]
    recorder = getattr(provider, "_record_recall", None)
    if (record_recall and not getattr(provider, "read_only", False) and results
            and callable(recorder)):
        recorder(
            results, query,
            session_id=session_id or getattr(provider, "_session_id", ""),
            source=source)
    return results


def _historical_page_match(index, result, query, as_of=None):
    """Whether a page fallback matched only a non-recallable claim."""
    if not isinstance(result, dict):
        return False
    record = index.pages.get(str(result.get("ref") or ""))
    if record is None:
        return False
    claim = matched_claim(record, query)
    return claim is not None and not temporal.is_recallable(claim, as_of)


def enrich_page_candidates(index, local_scores, page_results, corpus="pages",
                           type_filter=None, granularity="auto", as_of=None,
                           include_historical=False):
    """Rerank locally derived units within page-level backend candidates."""
    local_by_ref = {unit.ref: score for score, unit in local_scores}
    max_local = max(local_by_ref.values(), default=0.0)
    page_scores = {}
    for position, result in enumerate(page_results, start=1):
        if not isinstance(result, dict) or not result.get("ref"):
            continue
        value = result.get("relevance", result.get("score", 0.0))
        try:
            value = max(0.0, float(value))
        except (TypeError, ValueError):
            value = 0.0
        page_scores[result["ref"]] = max(value, 1.0 / position)
    max_page = max(page_scores.values(), default=1.0)
    units = index.units(
        corpus, type_filter, granularity, as_of, include_historical,
        page_scores)
    scored = []
    for unit in units:
        if unit.ntype == "candidate" and getattr(
                unit.page, "status", "") != "pending":
            continue
        local = local_by_ref.get(unit.ref, 0.0) / max_local if max_local else 0.0
        prior = page_scores.get(unit.page_ref, 0.0) / max_page
        score = 0.8 * local + 0.2 * prior
        if score > 0:
            scored.append((score, unit))
    scored.sort(key=lambda item: (-item[0], item[1].ref))
    return scored


def _diversify_pages(scored):
    diversified = []
    seen = set()
    for score, unit in scored:
        if unit.page_ref in seen:
            continue
        seen.add(unit.page_ref)
        diversified.append((score, unit))
    return diversified


def unit_result_dict(unit, score, query, *, backend="local",
                     semantic_ready=False, as_of=None,
                     candidate_backend=""):
    """Backend-neutral result metadata for one derived unit."""
    out = {
        "ref": unit.ref,
        "unit_id": unit.ref,
        "page_ref": unit.page_ref,
        "unit_kind": unit.kind,
        "title": unit.title,
        "type": unit.ntype,
        "score": round(score, 4),
        "snippet": unit_snippet(unit, query),
        "backend": backend,
        "unit_backend": "local",
    }
    relevance = (min(1.0, max(0.0, score)) if semantic_ready
                 else _query_term_coverage(
                     set(tokenize(query)), unit.tokens))
    out["relevance"] = round(relevance, 4)
    query_terms = set(tokenize(query))
    matched_terms = sorted(query_terms.intersection(unit.tokens))
    out["retrieval_trace"] = {
        "backend": backend,
        "unit_backend": "local",
        "unit_kind": unit.kind,
        "matched_terms": matched_terms,
        "query_term_coverage": round(
            len(matched_terms) / len(query_terms), 4) if query_terms else 0.0,
        "semantic_ready": semantic_ready,
        "ranking_score": round(score, 4),
    }
    if candidate_backend:
        out["retrieval_trace"]["candidate_backend"] = candidate_backend
    page = unit.page
    if unit.ntype == "claim":
        out["confidence"] = getattr(page, "confidence", "")
        out["maturity"] = getattr(page, "maturity", "")
    if unit.ntype == "candidate":
        out.update(
            id=getattr(page, "cand_id", ""),
            status=getattr(page, "status", ""),
            kind=getattr(page, "kind", ""),
            created=getattr(page, "created", ""),
            content_hash=getattr(page, "content_hash", ""),
        )
    if unit.line_start is not None:
        out["line_start"] = unit.line_start
        out["line_end"] = unit.line_end or unit.line_start
    if unit.claim is not None:
        out["claim_id"] = unit.claim_id
        out["temporal_state"] = unit.temporal_state(as_of)
        out["retrieval_trace"]["temporal_state"] = out["temporal_state"]
    if unit.kind == "claim":
        out["matched_claim"] = _unit_claim_metadata(unit.claim)
    elif unit.kind == "evidence":
        out["evidence"] = _evidence_metadata(unit.evidence)
        if unit.evidence_anchor:
            out["evidence_anchor"] = unit.evidence_anchor
    elif unit.kind == "heading":
        out["heading"] = unit.heading
    return out


def unit_snippet(unit, query):
    text = " ".join(str(unit.text or "").split())
    lowered = text.lower()
    query_terms = set(tokenize(query))
    first = next(
        (match.start() for match in _SNIPPET_TOKEN.finditer(lowered)
         if match.group() in query_terms),
        0,
    )
    start = max(0, lowered.rfind(" ", 0, max(0, first - 40)))
    return text[start:start + SNIPPET_LEN].strip()


def _unit_claim_metadata(claim):
    claim = claim if isinstance(claim, dict) else {}
    evidence = claim.get("evidence", []) or []
    out = {
        "id": str(claim.get("id") or ""),
        "text": str(claim.get("text") or ""),
        "status": str(claim.get("status") or "supported"),
        "confidence": claim.get("confidence"),
        "evidence_kinds": sorted({
            str(item.get("kind")) for item in evidence if item.get("kind")}),
        "evidence_source_ids": sorted({
            str(item.get("sourceId")) for item in evidence
            if item.get("sourceId")}),
    }
    for field in ("validFrom", "validUntil", "supersedes"):
        if claim.get(field) is not None:
            out[field] = claim[field]
    return out


def _evidence_metadata(evidence):
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        field: evidence[field] for field in (
            "kind", "sourceId", "path", "lines", "observedAt",
            "excerptHash", "privacyTier", "confidence", "weight")
        if evidence.get(field) is not None
    }


def snippet(rec, query):
    """Fragment from the canonical body containing the first query match."""
    claim = matched_claim(rec, query)
    body_overlap = len(set(tokenize(query)).intersection(
        tokenize(getattr(rec, "body", ""))))
    claim_overlap = len(set(tokenize(query)).intersection(
        tokenize(_claim_search_text(claim)))) if claim else 0
    if claim and claim_overlap > body_overlap:
        return str(claim.get("text") or "")[:SNIPPET_LEN].strip()
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
    add_claim_details(out, rec, query)
    query_terms = set(tokenize(query))
    matched_terms = sorted(query_terms.intersection(rec.tokens))
    out["retrieval_trace"] = {
        "backend": "local",
        "unit_backend": "page",
        "unit_kind": "page",
        "matched_terms": matched_terms,
        "query_term_coverage": round(
            len(matched_terms) / len(query_terms), 4) if query_terms else 0.0,
        "ranking_score": round(s, 4),
        "matched_fields": [field for field, tokens in (
            ("title", rec.title_tokens), ("aliases", rec.alias_tokens),
            ("topics", rec.topic_tokens), ("headings", rec.heading_tokens),
        ) if query_terms.intersection(tokens)],
    }
    return out


def matched_claim(rec, query):
    """Best structured claim match on a page, or None."""
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return None
    ranked = []
    for index, claim in enumerate(getattr(rec, "claims", []) or []):
        claim_tokens = set(tokenize(_claim_search_text(claim)))
        overlap = len(query_tokens.intersection(claim_tokens))
        if overlap:
            ranked.append((overlap / len(query_tokens), overlap, -index, claim))
    return max(ranked, key=lambda item: item[:3])[3] if ranked else None


def add_claim_details(result, rec, query, context="", fallback_single=False):
    claim = matched_claim(rec, query)
    if not claim and context:
        claim = matched_claim(rec, context)
    page_claims = list(getattr(rec, "claims", []) or [])
    if not claim and fallback_single and len(page_claims) == 1:
        claim = page_claims[0]
    if not claim:
        return result
    evidence = claim.get("evidence", [])
    result["matched_claim"] = {
        "id": str(claim.get("id") or ""),
        "text": str(claim.get("text") or ""),
        "status": str(claim.get("status") or "supported"),
        "confidence": claim.get("confidence"),
        "evidence_kinds": sorted({str(item.get("kind")) for item in evidence
                                   if item.get("kind")}),
        "evidence_source_ids": sorted({str(item.get("sourceId"))
                                        for item in evidence
                                        if item.get("sourceId")}),
    }
    return result


def _claim_search_text(claim):
    if not claim:
        return ""
    values = [str(claim.get(field) or "")
              for field in ("id", "text", "status")]
    for evidence in claim.get("evidence", []):
        values.extend(str(evidence.get(field) or "") for field in (
            "kind", "sourceId", "path", "lines", "privacyTier", "note"))
    return " ".join(values)


def prefetch_block(index, cfg, query, session_id="", embedder=None):
    """Compact cited recall block for per-turn injection ('' if nothing)."""
    del session_id
    scored = _diversify_pages(hybrid_unit_scores(
        index, embedder, query, corpus="prefetch", granularity="auto"))
    semantic_ready = bool(
        embedder is not None and embedder.ready and not embedder.failed)
    results = [
        unit_result_dict(
            unit, score, query, backend="local",
            semantic_ready=semantic_ready)
        for score, unit in scored
    ]
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
