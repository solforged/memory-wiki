"""Read-only retrieval evaluation over a bounded JSONL query set."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path


_BACKENDS = ("local", "qmd")
_CORPORA = {
    "pages", "raw", "profile", "journal", "candidates", "prefetch", "all",
}
_MAX_QUERY_SET_BYTES = 4 * 1024 * 1024
_MAX_CASES = 1000
_MAX_QUERY_CHARS = 4000
_MAX_EXPECTED = 100
_MAX_RESULTS = 20
_QUALITY_METRICS = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr")


@dataclass(frozen=True)
class ExpectedResult:
    """One acceptable retrieval target."""

    ref: str
    claim_id: str = ""
    unit_id: str = ""

    def as_dict(self):
        out = {"ref": self.ref}
        if self.claim_id:
            out["claim_id"] = self.claim_id
        if self.unit_id:
            out["unit_id"] = self.unit_id
        return out


@dataclass(frozen=True)
class QueryCase:
    """A query and its acceptable targets."""

    id: str
    query: str
    expected: tuple[ExpectedResult, ...]
    corpus: str = "pages"
    type_filter: str | None = None
    limit: int = 5
    tags: tuple[str, ...] = ()
    granularity: str = "page"
    as_of: str | None = None
    include_historical: bool = False


@dataclass(frozen=True)
class QuerySet:
    """Parsed query set with provenance for baseline comparisons."""

    path: str
    sha256: str
    cases: tuple[QueryCase, ...]


def load_query_set(path):
    """Load and validate a bounded JSONL query set."""
    query_path = Path(path).expanduser()
    try:
        size = query_path.stat().st_size
    except OSError as exc:
        raise ValueError(f"could not read query set: {exc}") from exc
    if size > _MAX_QUERY_SET_BYTES:
        raise ValueError(
            f"query set exceeds {_MAX_QUERY_SET_BYTES} bytes")
    try:
        raw = query_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read query set: {exc}") from exc

    cases = []
    seen_ids = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(cases) >= _MAX_CASES:
            raise ValueError(f"query set exceeds {_MAX_CASES} cases")
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"query set line {line_number}: invalid JSON: {exc.msg}") from exc
        case = _parse_case(item, line_number)
        if case.id in seen_ids:
            raise ValueError(
                f"query set line {line_number}: duplicate id {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("query set contains no cases")
    return QuerySet(
        path=str(query_path.resolve()),
        sha256=hashlib.sha256(raw).hexdigest(),
        cases=tuple(cases),
    )


def _parse_case(item, line_number):
    prefix = f"query set line {line_number}"
    if not isinstance(item, dict):
        raise ValueError(f"{prefix}: each case must be an object")
    allowed = {
        "id", "query", "expected", "corpus", "type", "type_filter",
        "limit", "tags", "granularity", "as_of", "include_historical",
    }
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise ValueError(f"{prefix}: unknown field(s): {', '.join(unknown)}")
    case_id = _required_text(item.get("id"), "id", prefix, 200)
    query = _required_text(item.get("query"), "query", prefix,
                           _MAX_QUERY_CHARS)
    expected_raw = item.get("expected")
    if not isinstance(expected_raw, list) or not expected_raw:
        raise ValueError(f"{prefix}: expected must be a non-empty list")
    if len(expected_raw) > _MAX_EXPECTED:
        raise ValueError(f"{prefix}: expected exceeds {_MAX_EXPECTED} targets")
    expected = tuple(
        _parse_expected(value, prefix, position)
        for position, value in enumerate(expected_raw, start=1)
    )
    corpus = str(item.get("corpus") or "pages").strip().lower()
    if corpus not in _CORPORA:
        raise ValueError(f"{prefix}: unsupported corpus {corpus!r}")
    if "type" in item and "type_filter" in item:
        raise ValueError(f"{prefix}: use only one of type or type_filter")
    type_value = item.get("type_filter", item.get("type"))
    type_filter = None
    if type_value is not None:
        type_filter = _required_text(type_value, "type_filter", prefix, 100)
    limit = item.get("limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(f"{prefix}: limit must be an integer")
    if limit < 1 or limit > _MAX_RESULTS:
        raise ValueError(f"{prefix}: limit must be between 1 and {_MAX_RESULTS}")
    tags_raw = item.get("tags", [])
    if not isinstance(tags_raw, list) or len(tags_raw) > 100:
        raise ValueError(f"{prefix}: tags must be a list of at most 100 strings")
    tags = tuple(
        _required_text(value, "tag", prefix, 100) for value in tags_raw)
    granularity = str(item.get("granularity") or "page").strip().lower()
    from . import chunks, temporal
    if granularity not in chunks.GRANULARITIES:
        raise ValueError(f"{prefix}: unsupported granularity {granularity!r}")
    as_of = item.get("as_of")
    if as_of is not None:
        as_of = _required_text(as_of, "as_of", prefix, 100)
        temporal.coerce_as_of(as_of)
    include_historical = item.get("include_historical", False)
    if not isinstance(include_historical, bool):
        raise ValueError(f"{prefix}: include_historical must be a boolean")
    return QueryCase(
        case_id, query, expected, corpus, type_filter, limit, tags,
        granularity, as_of, include_historical)


def _required_text(value, field, prefix, max_chars):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{prefix}: {field} must be a non-empty string")
    value = value.strip()
    if len(value) > max_chars:
        raise ValueError(f"{prefix}: {field} exceeds {max_chars} characters")
    return value


def _parse_expected(value, prefix, position):
    label = f"{prefix}: expected item {position}"
    if isinstance(value, str):
        return ExpectedResult(_required_text(value, "ref", label, 1000))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a ref string or object")
    unknown = sorted(set(value) - {"ref", "claim_id", "unit_id"})
    if unknown:
        raise ValueError(f"{label} has unknown field(s): {', '.join(unknown)}")
    ref = _required_text(value.get("ref"), "ref", label, 1000)
    claim_id = _optional_text(value.get("claim_id"), "claim_id", label)
    unit_id = _optional_text(value.get("unit_id"), "unit_id", label)
    return ExpectedResult(ref, claim_id, unit_id)


def _optional_text(value, field, prefix):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{prefix}: {field} must be a string")
    value = value.strip()
    if len(value) > 1000:
        raise ValueError(f"{prefix}: {field} exceeds 1000 characters")
    return value


def evaluate(cfg, query_set, *, backend="local", limit=None):
    """Evaluate local, QMD, or both backends without mutating wiki state."""
    if backend not in (*_BACKENDS, "all"):
        raise ValueError(f"unsupported evaluation backend: {backend}")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("evaluation limit must be an integer")
        if limit < 1 or limit > _MAX_RESULTS:
            raise ValueError(
                f"evaluation limit must be between 1 and {_MAX_RESULTS}")
    if isinstance(query_set, QuerySet):
        cases = query_set.cases
        query_meta = {
            "path": query_set.path,
            "sha256": query_set.sha256,
            "count": len(cases),
        }
    else:
        cases = tuple(query_set)
        query_meta = {"path": "", "sha256": "", "count": len(cases)}
    if not cases:
        raise ValueError("evaluation requires at least one query case")

    from .index import WikiIndex

    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    names = _BACKENDS if backend == "all" else (backend,)
    results = {}
    for name in names:
        try:
            searcher = (_local_searcher(index) if name == "local"
                        else _qmd_searcher(cfg, index))
            results[name] = _evaluate_backend(cases, searcher, limit)
        except Exception as exc:
            results[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return {
        "version": 1,
        "query_set": query_meta,
        "requested_backend": backend,
        "backends": results,
    }


def _local_searcher(index):
    from . import search

    def run(case, limit):
        if case.granularity != "page":
            scored = search.score_units(
                index, case.query, corpus=case.corpus,
                type_filter=case.type_filter,
                granularity=case.granularity, as_of=case.as_of,
                include_historical=case.include_historical)
            return [
                search.unit_result_dict(unit, score, case.query)
                for score, unit in scored[:limit]
            ]
        scored = search.score_records(
            index, case.query, corpus=case.corpus,
            type_filter=case.type_filter)
        results = []
        for score, record in scored[:limit]:
            result = search.result_dict(record, score, case.query)
            result["backend"] = "local"
            results.append(result)
        return results

    return run


def _qmd_searcher(cfg, index):
    from .qmd import QmdBackend

    qmd = QmdBackend(cfg, index, read_only=True)

    def run(case, limit):
        results = qmd.search(
            case.query, corpus=case.corpus, type_filter=case.type_filter,
            limit=limit)
        if results is None:
            status = qmd.status()
            detail = status.get("last_error") or (
                "QMD is unavailable or uninitialized for read-only evaluation")
            raise RuntimeError(detail)
        if case.granularity != "page":
            from . import search
            page_refs = [item.get("ref") for item in results
                         if isinstance(item, dict) and item.get("ref")]
            local = search.score_units(
                index, case.query, corpus=case.corpus,
                type_filter=case.type_filter,
                granularity=case.granularity, as_of=case.as_of,
                include_historical=case.include_historical,
                page_refs=page_refs)
            scored = search.enrich_page_candidates(
                index, local, results, case.corpus, case.type_filter,
                case.granularity, case.as_of, case.include_historical)
            if case.granularity == "auto":
                scored = search._diversify_pages(scored)
            return [
                search.unit_result_dict(
                    unit, score, case.query, backend="qmd", as_of=case.as_of)
                for score, unit in scored[:limit]
            ]
        return results

    return run


def _evaluate_backend(cases, searcher, limit_override):
    outcomes = []
    latencies = []
    for case in cases:
        result_limit = max(5, limit_override or case.limit)
        started = time.perf_counter()
        results = searcher(case, result_limit)
        elapsed = (time.perf_counter() - started) * 1000
        if not isinstance(results, list):
            raise TypeError("retrieval backend returned a non-list result")
        results = results[:result_limit]
        rank = _relevant_rank(results, case.expected)
        latency = round(elapsed, 3)
        latencies.append(elapsed)
        outcomes.append({
            "id": case.id,
            "expected": [item.as_dict() for item in case.expected],
            "relevant_rank": rank,
            "result_count": len(results),
            "zero_hit": not results,
            "latency_ms": latency,
            "top_results": [
                _result_identity(item, position)
                for position, item in enumerate(results, start=1)
            ],
        })
    return {
        "ok": True,
        "metrics": _metrics(outcomes, latencies),
        "cases": outcomes,
    }


def result_matches(result, expected):
    """Return whether one result satisfies an expected page/unit target."""
    if not isinstance(result, dict):
        return False
    refs = {str(result.get("ref") or ""),
            str(result.get("page_ref") or "")}
    if expected.ref not in refs:
        return False
    if expected.claim_id and expected.claim_id not in _claim_ids(result):
        return False
    if expected.unit_id and expected.unit_id not in _unit_ids(result):
        return False
    return True


def _claim_ids(result):
    values = {str(result.get("claim_id") or "")}
    matched = result.get("matched_claim")
    if isinstance(matched, dict):
        values.add(str(matched.get("id") or ""))
    return values


def _unit_ids(result):
    values = {
        str(result.get("unit_id") or ""),
        str(result.get("unit_ref") or ""),
    }
    matched = result.get("matched_unit")
    if isinstance(matched, dict):
        values.add(str(matched.get("id") or ""))
    return values


def _relevant_rank(results, expected):
    for position, result in enumerate(results, start=1):
        if any(result_matches(result, target) for target in expected):
            return position
    return None


def _result_identity(result, rank):
    out = {"rank": rank, "ref": str(result.get("ref") or "")}
    if result.get("page_ref"):
        out["page_ref"] = str(result["page_ref"])
    backend = str(result.get("backend") or "")
    if backend in _BACKENDS:
        out["backend"] = backend
    claim_ids = sorted(value for value in _claim_ids(result) if value)
    unit_ids = sorted(value for value in _unit_ids(result) if value)
    if claim_ids:
        out["claim_id"] = claim_ids[0]
    if unit_ids:
        out["unit_id"] = unit_ids[0]
    score = result.get("score")
    if _finite_number(score):
        out["score"] = score
    return out


def _metrics(outcomes, latencies):
    count = len(outcomes)
    ranks = [item["relevant_rank"] for item in outcomes]
    zero_hits = sum(item["zero_hit"] for item in outcomes)
    return {
        "query_count": count,
        "recall_at_1": _ratio(sum(rank == 1 for rank in ranks), count),
        "recall_at_3": _ratio(
            sum(rank is not None and rank <= 3 for rank in ranks), count),
        "recall_at_5": _ratio(
            sum(rank is not None and rank <= 5 for rank in ranks), count),
        "mrr": round(sum(0 if rank is None else 1 / rank for rank in ranks)
                     / count, 6),
        "zero_hit_count": zero_hits,
        "zero_hit_rate": _ratio(zero_hits, count),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(max(latencies), 3),
        },
    }


def _ratio(numerator, denominator):
    return round(numerator / denominator, 6)


def _percentile(values, fraction):
    ordered = sorted(values)
    position = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[position]


def load_baseline(path):
    """Load a previous JSON evaluation output."""
    baseline_path = Path(path).expanduser()
    try:
        if baseline_path.stat().st_size > _MAX_QUERY_SET_BYTES * 4:
            raise ValueError("baseline file is too large")
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read baseline: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(
            payload.get("backends"), dict):
        raise ValueError("baseline must be an evaluation JSON object")
    return payload


def compare_results(current, baseline):
    """Compare metrics with a prior evaluation, without mutating either."""
    current_hash = _query_hash(current)
    baseline_hash = _query_hash(baseline)
    if current_hash and baseline_hash and current_hash != baseline_hash:
        return {
            "comparable": False,
            "reason": "query set sha256 differs from baseline",
            "backends": {},
            "regressions": [],
        }

    comparisons = {}
    regressions = []
    current_backends = current.get("backends") or {}
    baseline_backends = baseline.get("backends") or {}
    for backend, current_result in current_backends.items():
        baseline_result = baseline_backends.get(backend)
        if not _successful_result(current_result) or not _successful_result(
                baseline_result):
            continue
        current_metrics = current_result["metrics"]
        baseline_metrics = baseline_result["metrics"]
        deltas = {}
        for metric in (*_QUALITY_METRICS, "zero_hit_rate"):
            current_value = current_metrics.get(metric)
            baseline_value = baseline_metrics.get(metric)
            if not (_finite_number(current_value)
                    and _finite_number(baseline_value)):
                continue
            delta = float(current_value) - float(baseline_value)
            deltas[metric] = {
                "baseline": baseline_value,
                "current": current_value,
                "delta": round(delta, 6),
            }
            regressed = (delta < -1e-12 if metric in _QUALITY_METRICS
                         else delta > 1e-12)
            if regressed:
                regressions.append({
                    "backend": backend,
                    "metric": metric,
                    "delta": round(delta, 6),
                })
        current_latency = _mean_latency(current_metrics)
        baseline_latency = _mean_latency(baseline_metrics)
        if current_latency is not None and baseline_latency is not None:
            deltas["latency_ms_mean"] = {
                "baseline": baseline_latency,
                "current": current_latency,
                "delta": round(current_latency - baseline_latency, 3),
            }
        comparisons[backend] = {"metrics": deltas}
    if not comparisons:
        return {
            "comparable": False,
            "reason": "baseline has no matching successful backend metrics",
            "backends": {},
            "regressions": [],
        }
    return {
        "comparable": True,
        "backends": comparisons,
        "regressions": regressions,
    }


def _query_hash(payload):
    query_set = payload.get("query_set") if isinstance(payload, dict) else None
    return str(query_set.get("sha256") or "") if isinstance(
        query_set, dict) else ""


def _successful_result(value):
    return (isinstance(value, dict) and value.get("ok") is True
            and isinstance(value.get("metrics"), dict))


def _finite_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _mean_latency(metrics):
    latency = metrics.get("latency_ms")
    if not isinstance(latency, dict) or not _finite_number(latency.get("mean")):
        return None
    return float(latency["mean"])
