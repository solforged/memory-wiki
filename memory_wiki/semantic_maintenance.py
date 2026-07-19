"""Read-only semantic maintenance findings with review-gated staging."""

from __future__ import annotations

import hashlib
import json
import re

from . import candidates, temporal


CLASSIFIER_VERSION = "semantic-maintenance/1"
CLASSIFICATIONS = (
    "duplicate",
    "supports",
    "refines",
    "contradicts",
    "temporal_successor",
    "unrelated",
)

_ACTIONABLE = frozenset(CLASSIFICATIONS) - {"unrelated"}
_TOKEN = re.compile(r"[a-z0-9]+")
_SPACE = re.compile(r"\s+")
_STOPWORDS = frozenset("""
a about after all also an and any are as at be because been before but by can
could did do does for from had has have he her his how i if in into is it its
just like me my of on one only or our out over she so some than that the their
them then there these they this to up us was we were what when which who will
with would you your
""".split())
_NEGATIONS = frozenset(("no", "not", "never", "neither", "without"))
_ANTONYMS = (
    ("allow", "deny"), ("allowed", "prohibited"),
    ("always", "never"), ("enable", "disable"),
    ("enabled", "disabled"), ("increase", "decrease"),
    ("public", "private"), ("required", "optional"),
    ("supports", "opposes"), ("true", "false"),
)
_CLASS_ORDER = {name: index for index, name in enumerate(CLASSIFICATIONS)}


class StaleFindingError(ValueError):
    """A finding no longer matches current content or classifier policy."""


def generate_findings(index, *, corpus="pages", limit=100, max_units=500,
                      min_similarity=0.24, include_unrelated=False,
                      llm_classifier=None,
                      classifier_version=CLASSIFIER_VERSION, refresh=True):
    """Generate bounded findings from page and claim retrieval units.

    The local pass is deterministic. ``llm_classifier``, when supplied, is a
    callable receiving a JSON-native request and returning a classification
    object. Failure or malformed output falls back to the local result.
    Nothing in this function writes to the wiki.
    """
    limit = _bounded_int(limit, "limit", 1, 1000)
    max_units = _bounded_int(max_units, "max_units", 2, 2000)
    try:
        min_similarity = float(min_similarity)
    except (TypeError, ValueError):
        raise ValueError("min_similarity must be a number")
    if not 0.0 <= min_similarity <= 1.0:
        raise ValueError("min_similarity must be from 0 to 1")
    classifier_version = _line(classifier_version, 120)
    if not classifier_version:
        raise ValueError("classifier_version must not be empty")
    if refresh:
        index.refresh()
    units = sorted((
        unit for unit in index.units(
            corpus=corpus, granularity="auto", include_historical=True)
        if unit.kind in ("page", "claim")
    ), key=lambda unit: unit.ref)[:max_units]
    pairs = _candidate_pairs(
        units, min_similarity=min_similarity,
        include_unrelated=include_unrelated)
    pair_limit = limit if include_unrelated else min(
        10_000, max(limit * 8, limit))
    findings = []
    for _score, left, right in pairs[:pair_limit]:
        result = classify_pair(left, right, llm_classifier=llm_classifier,
                               classifier_version=classifier_version)
        if result["classification"] == "unrelated" and not include_unrelated:
            continue
        findings.append(_finding(
            left, right, result, classifier_version=classifier_version))
    findings.sort(key=lambda item: (
        _CLASS_ORDER[item["classification"]], -item["confidence"],
        item["left"]["ref"], item["right"]["ref"]))
    return findings[:limit]


def classify_pair(left, right, *, llm_classifier=None,
                  classifier_version=CLASSIFIER_VERSION):
    """Classify one pair, with an optional fail-soft classifier override."""
    local = _local_classification(left, right)
    if llm_classifier is None:
        return {**local, "classifier_source": "local"}
    request = {
        "classifier_version": str(classifier_version),
        "allowed_classifications": list(CLASSIFICATIONS),
        "left": _classifier_payload(left),
        "right": _classifier_payload(right),
        "local_result": dict(local),
    }
    try:
        response = llm_classifier(request)
    except Exception:
        return {**local, "classifier_source": "local"}
    override = _valid_override(response)
    if override is None:
        return {**local, "classifier_source": "local"}
    return {**override, "classifier_source": "llm"}


def stale_reasons(index, finding, *,
                  classifier_version=CLASSIFIER_VERSION):
    """Return reasons a persisted finding is unsafe to accept now."""
    if not isinstance(finding, dict):
        return ("finding must be an object",)
    reasons = []
    expected_version = str(classifier_version or "")
    actual_version = str(finding.get("classifier_version") or "")
    bindings = finding.get("bindings")
    bindings = bindings if isinstance(bindings, dict) else {}
    if actual_version != expected_version:
        reasons.append("classifier version changed")
    if bindings.get("classifier_version") != actual_version:
        reasons.append("classifier binding is invalid")
    if finding.get("classification") not in CLASSIFICATIONS:
        reasons.append("classification is invalid")
    contents = bindings.get("contents")
    contents = contents if isinstance(contents, dict) else {}
    current_sides = []
    for label in ("left", "right"):
        side = finding.get(label)
        if not isinstance(side, dict):
            reasons.append(f"{label} binding is missing")
            continue
        ref = str(side.get("ref") or "")
        unit = index.unit(ref)
        if unit is None or unit.kind not in ("page", "claim"):
            reasons.append(f"{label} unit is missing")
            continue
        digest = unit_content_hash(unit)
        if side.get("content_hash") != digest or contents.get(ref) != digest:
            reasons.append(f"{label} content changed")
        current_sides.append(ref)
    if len(current_sides) == 2 and current_sides[0] == current_sides[1]:
        reasons.append("finding compares a unit with itself")
    expected_hash = _finding_hash(finding)
    if finding.get("finding_hash") != expected_hash:
        reasons.append("finding binding changed")
    expected_id = "semantic-" + expected_hash.removeprefix("sha256:")[:20]
    if finding.get("id") != expected_id:
        reasons.append("finding id changed")
    return tuple(dict.fromkeys(reasons))


def validate_finding(index, finding, *,
                     classifier_version=CLASSIFIER_VERSION, refresh=True):
    """Return ``finding`` or reject it when any binding is stale."""
    if refresh:
        index.refresh()
    reasons = stale_reasons(
        index, finding, classifier_version=classifier_version)
    if reasons:
        raise StaleFindingError("; ".join(reasons))
    return finding


def stage_accepted_finding(cfg, index, finding, *, accept=False,
                           classifier_version=CLASSIFIER_VERSION,
                           session_id="", actor="semantic_maintenance"):
    """Stage one current, actionable finding only after explicit opt-in.

    This creates a normal pending candidate. It never edits a knowledge page
    and never approves or promotes the candidate.
    """
    if accept is not True:
        return {"staged": False, "reason": "explicit accept=True is required"}
    try:
        index.refresh()
        validate_finding(
            index, finding, classifier_version=classifier_version,
            refresh=False)
    except (OSError, StaleFindingError) as exc:
        return {"staged": False, "reason": f"stale finding: {exc}"}
    classification = finding["classification"]
    if classification not in _ACTIONABLE:
        return {"staged": False, "reason": "unrelated findings are not actionable"}
    left = finding["left"]
    right = finding["right"]
    content = _proposal(classification, left["ref"], right["ref"])
    evidence = [
        finding["rationale"],
        f"semantic finding {finding['id']}",
        f"classifier {finding['classifier_version']}",
        f"left {left['ref']} {left['content_hash']}",
        f"right {right['ref']} {right['content_hash']}",
    ]
    left_unit = index.unit(left["ref"])
    right_unit = index.unit(right["ref"])
    topics = sorted(set(
        list(getattr(getattr(left_unit, "page", None), "topics", []) or [])
        + list(getattr(getattr(right_unit, "page", None), "topics", []) or [])
    ))[:8]
    result = candidates.stage(
        cfg, index, content, kind="note",
        origin=("llm" if finding.get("classifier_source") == "llm" else "tool"),
        session_id=session_id, target=left["page_ref"],
        confidence=_candidate_confidence(finding["confidence"]),
        topics=topics, evidence=evidence, actor=actor)
    return {**result, "finding_id": finding["id"],
            "classification": classification}


def unit_content_hash(unit):
    """Stable SHA-256 binding for the unit content used by a finding."""
    claim = getattr(unit, "claim", None)
    payload = {
        "ref": str(getattr(unit, "ref", "")),
        "page_ref": str(getattr(unit, "page_ref", "")),
        "kind": str(getattr(unit, "kind", "")),
        "title": str(getattr(unit, "title", "")),
        "text": str(getattr(unit, "text", "")),
        "claim": claim if isinstance(claim, dict) else None,
    }
    return _sha256(payload)


def _candidate_pairs(units, *, min_similarity, include_unrelated):
    pairs = []
    for position, left in enumerate(units):
        for right in units[position + 1:]:
            if left.page_ref == right.page_ref:
                continue
            metrics = _metrics(left, right)
            direct = _explicit_successor(left, right) is not None
            score = max(metrics["jaccard"], metrics["containment"] * 0.9)
            if not include_unrelated and not direct and score < min_similarity:
                continue
            pairs.append((score + (1.0 if direct else 0.0), left, right))
    return sorted(pairs, key=lambda item: (
        -item[0], item[1].ref, item[2].ref))


def _local_classification(left, right):
    metrics = _metrics(left, right)
    successor = _explicit_successor(left, right)
    if successor is not None:
        return {
            "classification": "temporal_successor",
            "confidence": 0.99,
            "rationale": (
                f"{successor[0]} explicitly supersedes {successor[1]}."),
        }
    contradiction = _contradiction(left, right, metrics)
    if contradiction:
        return {
            "classification": "contradicts",
            "confidence": round(max(0.72, metrics["containment"]), 3),
            "rationale": contradiction,
        }
    successor = _interval_successor(left, right, metrics)
    if successor is not None:
        return {
            "classification": "temporal_successor",
            "confidence": round(max(0.72, metrics["containment"]), 3),
            "rationale": (
                f"{successor[0]} begins when or after {successor[1]} ends."),
        }
    if metrics["normalized_equal"]:
        return {
            "classification": "duplicate", "confidence": 1.0,
            "rationale": "The normalized unit content is identical.",
        }
    if metrics["containment"] >= 0.92 and metrics["jaccard"] >= 0.78:
        return {
            "classification": "duplicate",
            "confidence": round(min(0.99, metrics["containment"]), 3),
            "rationale": "The units contain nearly the same meaningful terms.",
        }
    refiner = _refiner(left, right, metrics)
    if refiner is not None:
        return {
            "classification": "refines",
            "confidence": round(min(0.94, metrics["containment"]), 3),
            "rationale": (
                f"{refiner[0]} retains most of {refiner[1]} and adds detail."),
        }
    if (metrics["shared"] >= 3 and (
            metrics["containment"] >= 0.55 or metrics["jaccard"] >= 0.38)):
        return {
            "classification": "supports",
            "confidence": round(min(
                0.9, 0.45 + metrics["jaccard"] * 0.45), 3),
            "rationale": (
                f"The units share {metrics['shared']} meaningful terms "
                "without a detected conflict."),
        }
    return {
        "classification": "unrelated",
        "confidence": round(max(0.5, 1.0 - metrics["containment"]), 3),
        "rationale": "The units do not share enough meaning for a safe relation.",
    }


def _metrics(left, right):
    left_tokens = _tokens(getattr(left, "text", ""))
    right_tokens = _tokens(getattr(right, "text", ""))
    shared = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    smaller = min(len(left_tokens), len(right_tokens))
    return {
        "left_tokens": left_tokens,
        "right_tokens": right_tokens,
        "shared": shared,
        "jaccard": shared / max(1, union),
        "containment": shared / max(1, smaller),
        "normalized_equal": bool(
            _normalize(getattr(left, "text", ""))
            and _normalize(getattr(left, "text", "")) ==
            _normalize(getattr(right, "text", ""))),
    }


def _explicit_successor(left, right):
    left_claim = getattr(left, "claim", None) or {}
    right_claim = getattr(right, "claim", None) or {}
    left_id = str(left_claim.get("id") or "")
    right_id = str(right_claim.get("id") or "")
    left_old = set(_predecessors(left_claim.get("supersedes")))
    right_old = set(_predecessors(right_claim.get("supersedes")))
    if right_id and right_id in left_old:
        return left.ref, right.ref
    if left_id and left_id in right_old:
        return right.ref, left.ref
    return None


def _interval_successor(left, right, metrics):
    if metrics["containment"] < 0.55:
        return None
    left_claim = getattr(left, "claim", None) or {}
    right_claim = getattr(right, "claim", None) or {}
    left_start = temporal.parse_timestamp(left_claim.get("validFrom"))
    left_end = temporal.parse_timestamp(left_claim.get("validUntil"))
    right_start = temporal.parse_timestamp(right_claim.get("validFrom"))
    right_end = temporal.parse_timestamp(right_claim.get("validUntil"))
    if left_start is not None and right_end is not None and left_start >= right_end:
        return left.ref, right.ref
    if right_start is not None and left_end is not None and right_start >= left_end:
        return right.ref, left.ref
    return None


def _contradiction(left, right, metrics):
    left_tokens = metrics["left_tokens"]
    right_tokens = metrics["right_tokens"]
    left_core = left_tokens - _NEGATIONS
    right_core = right_tokens - _NEGATIONS
    shared_core = len(left_core & right_core) / max(
        1, min(len(left_core), len(right_core)))
    left_negative = bool(left_tokens & _NEGATIONS)
    right_negative = bool(right_tokens & _NEGATIONS)
    if left_negative != right_negative and shared_core >= 0.72:
        return "The units have matching subject terms but opposite negation."
    for positive, negative in _ANTONYMS:
        opposed = ((positive in left_tokens and negative in right_tokens)
                   or (negative in left_tokens and positive in right_tokens))
        if not opposed:
            continue
        left_rest = left_tokens - {positive, negative}
        right_rest = right_tokens - {positive, negative}
        overlap = len(left_rest & right_rest) / max(
            1, min(len(left_rest), len(right_rest)))
        if overlap >= 0.6:
            return f"The units use opposing terms: {positive} and {negative}."
    return ""


def _refiner(left, right, metrics):
    if metrics["containment"] < 0.72:
        return None
    left_size = len(metrics["left_tokens"])
    right_size = len(metrics["right_tokens"])
    if left_size >= right_size + 2 and left_size >= right_size * 1.15:
        return left.ref, right.ref
    if right_size >= left_size + 2 and right_size >= left_size * 1.15:
        return right.ref, left.ref
    return None


def _finding(left, right, result, *, classifier_version):
    left_side = _side(left)
    right_side = _side(right)
    finding = {
        "classification": result["classification"],
        "confidence": round(float(result["confidence"]), 3),
        "rationale": _line(result["rationale"], 500),
        "classifier_source": result["classifier_source"],
        "classifier_version": classifier_version,
        "left": left_side,
        "right": right_side,
        "bindings": {
            "classifier_version": classifier_version,
            "contents": {
                left_side["ref"]: left_side["content_hash"],
                right_side["ref"]: right_side["content_hash"],
            },
        },
    }
    finding_hash = _finding_hash(finding)
    finding["finding_hash"] = finding_hash
    finding["id"] = "semantic-" + finding_hash.removeprefix("sha256:")[:20]
    return finding


def _side(unit):
    return {
        "ref": str(unit.ref),
        "page_ref": str(unit.page_ref),
        "unit_kind": str(unit.kind),
        "title": _line(getattr(unit, "title", ""), 240),
        "content_hash": unit_content_hash(unit),
    }


def _finding_hash(finding):
    core = {
        key: finding.get(key) for key in (
            "classification", "confidence", "rationale", "classifier_source",
            "classifier_version", "left", "right", "bindings")
    }
    return _sha256(core)


def _classifier_payload(unit):
    claim = getattr(unit, "claim", None)
    return {
        "ref": str(getattr(unit, "ref", "")),
        "page_ref": str(getattr(unit, "page_ref", "")),
        "unit_kind": str(getattr(unit, "kind", "")),
        "text": str(getattr(unit, "text", ""))[:4000],
        "claim": claim if isinstance(claim, dict) else None,
    }


def _valid_override(value):
    if not isinstance(value, dict):
        return None
    classification = value.get("classification")
    rationale = _line(value.get("rationale"), 500)
    try:
        confidence = float(value.get("confidence"))
    except (TypeError, ValueError):
        return None
    if (classification not in CLASSIFICATIONS or not rationale
            or not 0.0 <= confidence <= 1.0):
        return None
    return {
        "classification": classification,
        "confidence": round(confidence, 3),
        "rationale": rationale,
    }


def _proposal(classification, left_ref, right_ref):
    verbs = {
        "duplicate": "may duplicate",
        "supports": "may support",
        "refines": "may refine",
        "contradicts": "may contradict",
        "temporal_successor": "may supersede over time",
    }
    return (f"Semantic review: `{left_ref}` {verbs[classification]} "
            f"`{right_ref}`.")


def _candidate_confidence(value):
    value = float(value)
    if value >= 0.9:
        return "confident"
    if value >= 0.7:
        return "likely"
    if value >= 0.45:
        return "plausible"
    return "speculative"


def _predecessors(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _tokens(value):
    return {token for token in _TOKEN.findall(str(value or "").lower())
            if len(token) > 1 and token not in _STOPWORDS}


def _normalize(value):
    return _SPACE.sub(" ", " ".join(_TOKEN.findall(
        str(value or "").lower()))).strip()


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _sha256(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str)
    return "sha256:" + hashlib.sha256(
        payload.encode("utf-8", "replace")).hexdigest()


def _bounded_int(value, label, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be from {minimum} to {maximum}")
    return value
