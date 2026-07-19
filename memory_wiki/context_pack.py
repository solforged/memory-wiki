"""Task-scoped, cited context packs over explicit memory corpora."""

from __future__ import annotations


MAX_RESULTS = 20
MIN_CHAR_BUDGET = 500
MAX_CHAR_BUDGET = 20_000
_ALLOWED_CORPORA = frozenset((
    "pages", "raw", "profile", "journal", "candidates", "prefetch",
))


def build(provider, query, *, corpus="pages", type_filter=None,
          include_profile=True, include_journal=False,
          include_candidates=False, limit=8, char_budget=6_000):
    """Return a bounded context block and its structured cited results.

    Raw archives, journals, and candidates are searched only when explicitly
    requested. Recall telemetry is disabled for every search in this builder.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("context pack query is required")
    if getattr(provider, "cfg", None) is None or getattr(provider, "index", None) is None:
        raise ValueError("memory-wiki provider is not initialized")
    corpus = str(corpus or "pages").strip().lower()
    if corpus not in _ALLOWED_CORPORA:
        allowed = ", ".join(sorted(_ALLOWED_CORPORA))
        raise ValueError(f"context pack corpus must be one of: {allowed}")
    result_limit = _coerce_limit(limit)
    budget = _coerce_budget(char_budget)
    corpora = [corpus]
    if include_profile and "profile" not in corpora:
        corpora.append("profile")
    if include_journal and "journal" not in corpora:
        corpora.append("journal")
    if include_candidates and "candidates" not in corpora:
        corpora.append("candidates")

    found = []
    for current in corpora:
        if hasattr(provider.index, "units"):
            from . import search
            results = search.provider_unit_results(
                provider, query, corpus=current,
                type_filter=type_filter if current == corpus else None,
                limit=result_limit, granularity="auto",
                source="context_pack", record_recall=False)
        else:
            results = provider.search_results(
                query, corpus=current,
                type_filter=type_filter if current == corpus else None,
                limit=result_limit, source="context_pack",
                record_recall=False)
        for result in results or []:
            if not isinstance(result, dict):
                continue
            if result.get("type") == "candidate" and current == "candidates":
                if result.get("status") not in ("pending", "approved"):
                    continue
            found.append(_public_result(result, current))

    deduped = []
    seen = set()
    for result in found:
        identity = (result.get("ref", ""), result.get("id", ""))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(result)
    limited = deduped[:result_limit]
    context, included, budget_truncated = _render(query, limited, budget)
    refs = []
    for result in included:
        ref = result.get("ref") or (
            f"candidate:{result.get('id')}" if result.get("id") else "")
        if ref and ref not in refs:
            refs.append(ref)
    return {
        "query": query,
        "corpora": corpora,
        "type_filter": str(type_filter or ""),
        "count": len(included),
        "refs": refs,
        "results": included,
        "context": context,
        "chars": len(context),
        "char_budget": budget,
        "truncated": budget_truncated or len(deduped) > len(limited),
    }


def _public_result(result, corpus):
    ntype = _line(result.get("type"), 40)
    item = {
        "ref": _line(result.get("ref"), 240),
        "title": _line(result.get("title"), 240),
        "type": ntype,
        "snippet": _line(result.get("snippet"), 800),
        "score": result.get("score"),
        "relevance": result.get("relevance"),
        "corpus": corpus,
    }
    for field in ("unit_kind", "page_ref", "temporal_state",
                  "evidence_anchor"):
        if result.get(field) not in (None, ""):
            item[field] = result[field]
    if ntype == "candidate":
        item.update({
            "id": _line(result.get("id"), 160),
            "status": _line(result.get("status"), 30),
            "kind": _line(result.get("kind"), 30),
            "provisional": True,
        })
    matched_claim = result.get("matched_claim")
    if isinstance(matched_claim, dict):
        item["matched_claim"] = {
            key: value for key, value in {
                "id": _line(matched_claim.get("id"), 160),
                "text": _line(matched_claim.get("text"), 500),
                "status": _line(matched_claim.get("status"), 40),
                "confidence": matched_claim.get("confidence"),
            }.items() if value not in (None, "")
        }
    return item


def _render(query, results, budget):
    heading = "## Task context"
    footer = "Open cited refs before relying on details."
    lines = [heading]
    used = len(heading)
    included = []
    truncated = False
    for result in results:
        line = _result_line(result)
        available = budget - used - len(footer) - 2
        if len(line) > available:
            if available < 80:
                truncated = True
                break
            line = line[:available - 3].rstrip() + "..."
            truncated = True
        lines.append(line)
        included.append(result)
        used += len(line) + 1
        if truncated:
            break
    if included:
        lines.append(footer)
    else:
        empty = f'No cited memory matched "{_line(query, 160)}".'
        lines.append(empty[:max(0, budget - used - 1)])
    context = "\n".join(lines)
    return context[:budget], included, truncated


def _result_line(result):
    if result.get("type") == "candidate":
        identity = result.get("id") or result.get("ref") or "unknown"
        status = result.get("status") or "pending"
        label = f"candidate {identity}, provisional {status}"
    else:
        ref = result.get("ref") or "unknown"
        title = result.get("title")
        label = ref + (f" | {title}" if title else "")
        if result.get("corpus") == "raw":
            label += " | explicit private archive"
    snippet = result.get("snippet") or "No excerpt available."
    return f"- [{label}] {snippet}"


def _coerce_limit(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError("context pack limit must be an integer")
    if result < 1:
        raise ValueError("context pack limit must be at least 1")
    return min(result, MAX_RESULTS)


def _coerce_budget(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError("context pack character budget must be an integer")
    if result < 1:
        raise ValueError("context pack character budget must be at least 1")
    return min(MAX_CHAR_BUDGET, max(MIN_CHAR_BUDGET, result))


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


build_context_pack = build
