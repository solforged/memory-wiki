"""Structured claim normalization, validation, and health signals.

The public values are plain dictionaries so compiler artifacts remain small,
portable, and JSON-native. Invalid claim text is dropped during normalization;
validation is available to write paths that need actionable errors.
"""

from __future__ import annotations

import math
import re
from hashlib import sha256
from datetime import date, datetime, timezone

from . import temporal


AGING_DAYS = 30
STALE_DAYS = 90
CONTESTED_STATUSES = frozenset(
    ("contested", "contradicted", "refuted", "superseded"))

CLAIM_FIELDS = frozenset(
    ("id", "text", "status", "confidence", "evidence", "updatedAt",
     "validFrom", "validUntil", "supersedes"))
EVIDENCE_FIELDS = frozenset((
    "kind", "sourceId", "path", "lines", "weight", "confidence",
    "privacyTier", "note", "updatedAt", "observedAt", "excerpt",
    "excerptHash",
))
_EVIDENCE_STRING_FIELDS = (
    "kind", "sourceId", "path", "lines", "privacyTier", "note", "updatedAt",
    "observedAt", "excerpt", "excerptHash",
)
_CLAIM_STRING_FIELDS = (
    "id", "status", "updatedAt", "validFrom", "validUntil")
_SPACE = re.compile(r"\s+")
_EXCERPT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
CLAIM_ID = re.compile(
    r"^claim\.[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$")


def _string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _confidence(value):
    value = _number(value)
    return value if value is not None and 0.0 <= value <= 1.0 else None


def normalize_status(status=None):
    """Canonical status, defaulting missing values to ``supported``."""
    return (_string(status) or "supported").lower()


def is_contested(status=None):
    return normalize_status(status) in CONTESTED_STATUSES


def excerpt_hash(excerpt):
    """Content-bound SHA-256 anchor for an exact evidence excerpt."""
    if not isinstance(excerpt, str):
        raise ValueError("excerpt must be a string")
    return "sha256:" + sha256(excerpt.encode("utf-8")).hexdigest()


def superseded_claim_ids(value):
    """Normalize a claim's predecessor reference(s) to a stable list."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    for item in values:
        item = _string(item)
        if item is not None and item not in result:
            result.append(item)
    return result


def normalize_evidence(value):
    """Return one exact evidence object, or None for unusable input."""
    if not isinstance(value, dict):
        return None
    result = {}
    for field in _EVIDENCE_STRING_FIELDS:
        normalized = _string(value.get(field))
        if normalized is not None:
            result[field] = normalized
    weight = _number(value.get("weight"))
    if weight is not None:
        result["weight"] = weight
    confidence = _confidence(value.get("confidence"))
    if confidence is not None:
        result["confidence"] = confidence
    return result or None


def normalize_claims(value):
    """Normalize a JSON-compatible claims list.

    Entries without non-empty text are intentionally omitted. Missing or
    malformed evidence becomes an empty list so health reports can flag it.
    """
    if not isinstance(value, list):
        return []
    normalized = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        text = _string(entry.get("text"))
        if text is None:
            continue
        claim = {"text": text}
        claim_id = _string(entry.get("id"))
        if claim_id is not None:
            claim["id"] = claim_id
        status = _string(entry.get("status"))
        if status is not None:
            claim["status"] = status.lower()
        confidence = _confidence(entry.get("confidence"))
        if confidence is not None:
            claim["confidence"] = confidence
        raw_evidence = entry.get("evidence")
        candidates = raw_evidence if isinstance(raw_evidence, list) else []
        claim["evidence"] = [item for item in (
            normalize_evidence(candidate)
            for candidate in candidates
        ) if item is not None]
        updated_at = _string(entry.get("updatedAt"))
        if updated_at is not None:
            claim["updatedAt"] = updated_at
        for field in ("validFrom", "validUntil"):
            value = _string(entry.get(field))
            if value is not None:
                claim[field] = value
        predecessors = superseded_claim_ids(entry.get("supersedes"))
        if predecessors:
            claim["supersedes"] = (predecessors[0]
                                    if isinstance(entry.get("supersedes"), str)
                                    else predecessors)
        normalized.append(claim)
    return normalized


def _validate_confidence(value, path, errors):
    if _confidence(value) is None:
        errors.append(f"{path} must be a finite number from 0 to 1")


def validate_claims(value, prefix="claims"):
    """Return precise structural errors for a structured claims value."""
    errors = []
    if not isinstance(value, list):
        return [f"{prefix} must be a list"]
    for index, entry in enumerate(value):
        path = f"{prefix}[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path} must be an object")
            continue
        unknown = sorted(set(entry) - CLAIM_FIELDS)
        for field in unknown:
            errors.append(f"{path}.{field} is not a recognized claim field")
        if _string(entry.get("text")) is None:
            errors.append(f"{path}.text must be a non-empty string")
        for field in _CLAIM_STRING_FIELDS:
            if field in entry and _string(entry[field]) is None:
                errors.append(f"{path}.{field} must be a non-empty string")
        claim_id = _string(entry.get("id"))
        if claim_id is not None and not CLAIM_ID.fullmatch(claim_id):
            errors.append(
                f"{path}.id must match claim.<namespace>.<local-name>")
        errors.extend(temporal.interval_errors(entry, path))
        predecessors = superseded_claim_ids(entry.get("supersedes"))
        if "supersedes" in entry and not predecessors:
            errors.append(
                f"{path}.supersedes must be a claim id or non-empty list")
        for predecessor in predecessors:
            if not CLAIM_ID.fullmatch(predecessor):
                errors.append(
                    f"{path}.supersedes must contain claim ids")
            elif predecessor == claim_id:
                errors.append(f"{path}.supersedes must not reference itself")
        raw_predecessors = entry.get("supersedes")
        raw_predecessors = (raw_predecessors
                            if isinstance(raw_predecessors, (list, tuple))
                            else [raw_predecessors])
        normalized_raw = [_string(item) for item in raw_predecessors]
        normalized_raw = [item for item in normalized_raw if item is not None]
        if len(normalized_raw) != len(set(normalized_raw)):
            errors.append(f"{path}.supersedes must not contain duplicates")
        if "updatedAt" in entry and temporal.parse_timestamp(
                entry.get("updatedAt")) is None:
            errors.append(f"{path}.updatedAt must be an ISO date or datetime")
        if "confidence" in entry:
            _validate_confidence(entry["confidence"], f"{path}.confidence", errors)
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"{path}.evidence must be a list")
            continue
        for evidence_index, item in enumerate(evidence):
            evidence_path = f"{path}.evidence[{evidence_index}]"
            if not isinstance(item, dict):
                errors.append(f"{evidence_path} must be an object")
                continue
            unknown_evidence = sorted(set(item) - EVIDENCE_FIELDS)
            for field in unknown_evidence:
                errors.append(
                    f"{evidence_path}.{field} is not a recognized evidence field")
            if not item:
                errors.append(f"{evidence_path} must not be empty")
            for field in _EVIDENCE_STRING_FIELDS:
                if field in item and _string(item[field]) is None:
                    errors.append(
                        f"{evidence_path}.{field} must be a non-empty string")
            if "weight" in item and _number(item["weight"]) is None:
                errors.append(f"{evidence_path}.weight must be a finite number")
            if "confidence" in item:
                _validate_confidence(
                    item["confidence"], f"{evidence_path}.confidence", errors)
            for field in ("updatedAt", "observedAt"):
                if field in item and temporal.parse_timestamp(
                        item.get(field)) is None:
                    errors.append(
                        f"{evidence_path}.{field} must be an ISO date or datetime")
            excerpt = item.get("excerpt")
            digest = item.get("excerptHash")
            if excerpt is not None and digest is None:
                errors.append(
                    f"{evidence_path}.excerptHash is required with excerpt")
            if digest is not None:
                if not isinstance(digest, str) or not _EXCERPT_HASH.fullmatch(digest):
                    errors.append(
                        f"{evidence_path}.excerptHash must be sha256:<64 lowercase hex>")
                elif isinstance(excerpt, str) and excerpt_hash(excerpt) != digest:
                    errors.append(
                        f"{evidence_path}.excerptHash does not match excerpt")
    return errors


def _coerce_now(now):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, date) and not isinstance(now, datetime):
        now = datetime.combine(now, datetime.min.time())
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def freshness(timestamp=None, now=None):
    """Classify one timestamp into fresh, aging, stale, or unknown."""
    parsed = temporal.parse_timestamp(timestamp)
    raw = _string(timestamp)
    if parsed is None or raw is None:
        return {"level": "unknown", "reason": "missing updatedAt"}
    days = max(0, (_coerce_now(now) - parsed).days)
    level = "stale" if days >= STALE_DAYS else \
        "aging" if days >= AGING_DAYS else "fresh"
    return {
        "level": level,
        "reason": f"last touched {raw}",
        "daysSinceTouch": days,
        "lastTouchedAt": raw,
    }


def assess_page_freshness(page, now=None):
    return freshness(page.get("updatedAt"), now)


def assess_claim_freshness(page, claim, now=None):
    """Use newest claim/evidence timestamp, then the page timestamp.

    A present but invalid claim/evidence timestamp does not fall back to the
    page. This keeps malformed provenance visible as an unknown health state.
    """
    raw_timestamps = []
    if _string(claim.get("updatedAt")) is not None:
        raw_timestamps.append(claim["updatedAt"])
    for evidence in claim.get("evidence", []):
        if not isinstance(evidence, dict):
            continue
        for field in ("observedAt", "updatedAt"):
            if _string(evidence.get(field)) is not None:
                raw_timestamps.append(evidence[field])
    parsed = [(stamp, temporal.parse_timestamp(stamp))
              for stamp in raw_timestamps]
    valid = [(stamp, when) for stamp, when in parsed if when is not None]
    if valid:
        latest = max(valid, key=lambda item: item[1])[0]
        return freshness(latest, now)
    if raw_timestamps:
        return freshness(None, now)
    return freshness(page.get("updatedAt"), now)


def collect_health(pages, now=None):
    """Flatten normalized page claims into deterministic health records."""
    records = []
    for page in sorted(pages, key=lambda item: item["path"]):
        for index, claim in enumerate(page.get("claims", [])):
            claim_id = _string(claim.get("id"))
            item = {
                "key": f"{page['path']}#{claim_id or f'claim-{index + 1}'}",
                "pagePath": page["path"],
                "pageTitle": page["title"],
                "text": claim["text"],
                "status": normalize_status(claim.get("status")),
                "evidenceCount": len(claim.get("evidence", [])),
                "missingEvidence": not claim.get("evidence"),
                "freshness": assess_claim_freshness(page, claim, now),
                "temporalState": temporal.claim_state(claim, now),
            }
            if page.get("id"):
                item["pageId"] = page["id"]
            if claim_id:
                item["claimId"] = claim_id
            if _confidence(claim.get("confidence")) is not None:
                item["confidence"] = float(claim["confidence"])
            predecessors = superseded_claim_ids(claim.get("supersedes"))
            if predecessors:
                item["supersedes"] = predecessors
            records.append(item)
    return records


def lineage_issues(pages):
    """Find missing, duplicate, and cyclic semantic supersession links."""
    claims_by_id = {}
    for page in pages:
        for claim in page.get("claims", []):
            claim_id = _string(claim.get("id"))
            if claim_id:
                claims_by_id.setdefault(claim_id, []).append(
                    (page.get("path", ""), claim))
    issues = []
    for claim_id, entries in sorted(claims_by_id.items()):
        if len(entries) > 1:
            issues.append({
                "kind": "duplicate-id", "claimId": claim_id,
                "paths": sorted(path for path, _claim in entries),
            })
    edges = {}
    for claim_id, entries in claims_by_id.items():
        predecessors = superseded_claim_ids(entries[0][1].get("supersedes"))
        edges[claim_id] = predecessors
        for predecessor in predecessors:
            if predecessor not in claims_by_id:
                issues.append({
                    "kind": "missing-predecessor", "claimId": claim_id,
                    "predecessorId": predecessor, "paths": [entries[0][0]],
                })

    visiting = set()
    visited = set()
    cycles = set()

    def walk(node, trail):
        if node in visiting:
            start = trail.index(node)
            cycles.add(tuple(trail[start:]))
            return
        if node in visited:
            return
        visiting.add(node)
        for predecessor in edges.get(node, []):
            if predecessor in edges:
                walk(predecessor, trail + (
                    [] if predecessor in trail else [predecessor]))
        visiting.remove(node)
        visited.add(node)

    for claim_id in sorted(edges):
        walk(claim_id, [claim_id])
    for cycle in sorted(cycles):
        issues.append({"kind": "cycle", "claimIds": list(cycle)})
    return issues


def contradiction_clusters(pages, now=None):
    """Find repeated claim ID conflicts, never semantic lookalikes."""
    by_id = {}
    for item in collect_health(pages, now):
        claim_id = item.get("claimId")
        if claim_id:
            by_id.setdefault(claim_id, []).append(item)
    clusters = []
    for claim_id, entries in by_id.items():
        if len(entries) < 2:
            continue
        texts = {_SPACE.sub(" ", entry["text"]).strip().lower()
                 for entry in entries}
        statuses = {entry["status"] for entry in entries}
        if len(texts) < 2 and len(statuses) < 2:
            continue
        clusters.append({
            "key": claim_id,
            "label": claim_id,
            "entries": sorted(entries, key=lambda item: item["pagePath"]),
        })
    return sorted(clusters, key=lambda item: item["label"])
