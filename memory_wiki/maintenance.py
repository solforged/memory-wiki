"""Read-only maintenance tasks derived from compiler artifacts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import claims, compiler, schema, type_registry


MAX_ITEMS = 100
_MAX_JSON = 5 * 1024 * 1024
_MAX_JSONL = 8 * 1024 * 1024
_SEVERITY = {"high": 0, "medium": 1, "low": 2}
_NONPUBLIC = {"local-private", "private"}


def build(cfg, limit=50):
    """Build bounded maintenance work from existing compiler output.

    This function never invokes the compiler. Missing or stale artifacts are
    surfaced as state so callers can choose when to refresh them.
    """
    root = Path(getattr(cfg, "wiki_root", cfg)).expanduser()
    item_limit = _coerce_limit(limit)
    digest_path = root / compiler.AGENT_DIGEST_PATH
    claims_path = root / compiler.CLAIMS_DIGEST_PATH
    issues = []
    digest = _read_json(digest_path, issues)
    claim_rows = _read_jsonl(claims_path, issues)
    digest = _supported_artifact(digest, "compiler digest", issues)
    claim_rows = [
        row for row in claim_rows
        if _supported_artifact(row, "claim cache row", issues)
    ]
    tasks = []

    for cluster in _list(digest.get("contradictionClusters")):
        if not isinstance(cluster, dict):
            continue
        paths = _strings(cluster.get("paths"), 12, 240)
        label = _line(cluster.get("label") or cluster.get("key"), 240)
        tasks.append(_task(
            "contradiction", "high", paths[0] if paths else "", label,
            f"Reconcile contradictory entries across {len(paths)} page(s).",
            refs=paths))

    for row in claim_rows:
        claim = row.get("claim") if isinstance(row.get("claim"), dict) else row
        if not isinstance(claim, dict):
            continue
        ref = _line(claim.get("pagePath") or claim.get("ref"), 240)
        text = _line(claim.get("text") or claim.get("title") or "Claim", 500)
        claim_id = _line(claim.get("id") or claim.get("claimId"), 160)
        common = {"claim_id": claim_id} if claim_id else {}
        if claims.is_contested(claim.get("status")):
            tasks.append(_task(
                "contested_claim", "high", ref, text,
                f"Resolve claim status {claims.normalize_status(claim.get('status'))}.",
                **common))
        if claim.get("missingEvidence") is True or (
                "missingEvidence" not in claim
                and not claim.get("evidence") and not claim.get("provenance")):
            tasks.append(_task(
                "missing_evidence", "medium", ref, text,
                "Add evidence or narrow the claim.", **common))
        confidence = _number(claim.get("confidence"))
        if confidence is not None and confidence < 0.5:
            tasks.append(_task(
                "low_confidence", "medium", ref, text,
                f"Review confidence {confidence:.2f} and supporting evidence.",
                **common))
        freshness = claim.get("freshness")
        freshness_level = claim.get("freshnessLevel")
        if not freshness_level and isinstance(freshness, dict):
            freshness_level = freshness.get("level")
        freshness_level = _line(freshness_level, 20).lower()
        if freshness_level in ("stale", "unknown"):
            severity = "medium" if freshness_level == "stale" else "low"
            tasks.append(_task(
                f"{freshness_level}_claim", severity, ref, text,
                ("Verify or retire this stale claim." if freshness_level == "stale"
                 else "Add a reliable updated date or evidence timestamp."),
                **common))
        privacy_tiers = sorted(set(_strings(claim.get("privacyTiers"), 12, 40)))
        private = [tier for tier in privacy_tiers if tier in _NONPUBLIC]
        if private:
            tasks.append(_task(
                "privacy_review", "medium", ref, text,
                "Confirm this claim's non-public material is scoped correctly.",
                privacy_tiers=private, **common))

    for page in _list(digest.get("pages")):
        if not isinstance(page, dict):
            continue
        ref = _line(page.get("path") or page.get("ref"), 240)
        title = _line(page.get("title") or ref or "Page", 240)
        for question in _strings(page.get("questions"), 20, 500):
            tasks.append(_task(
                "open_question", "low", ref, question,
                f"Answer or close the open question on {title}."))
        privacy = _line(page.get("privacyTier"), 40)
        if privacy in _NONPUBLIC:
            tasks.append(_task(
                "privacy_review", "medium", ref, title,
                "Confirm this page's non-public classification and recall scope.",
                privacy_tiers=[privacy]))

    tasks = _deduplicate(tasks)
    tasks.sort(key=lambda item: (
        _SEVERITY.get(item["severity"], 99), item["category"],
        item.get("ref", ""), item["id"]))
    total = len(tasks)
    returned = tasks[:item_limit]
    by_category = Counter(item["category"] for item in tasks)
    by_severity = Counter(item["severity"] for item in tasks)
    generated_at = max(
        (_mtime_iso(path) for path in (digest_path, claims_path) if path.is_file()),
        default="")
    source_mtime = _newest_source_mtime(root)
    artifact_mtimes = [path.stat().st_mtime for path in (digest_path, claims_path)
                       if path.is_file()]
    artifacts_stale = bool(
        source_mtime and artifact_mtimes and source_mtime > min(artifact_mtimes))
    available = bool(digest or claim_rows)
    if not available and not issues:
        issues.append("compiler artifacts are missing")
    return {
        "compiler_available": available,
        "artifacts_stale": artifacts_stale,
        "generated_at": generated_at,
        "summary": {
            "total": total,
            "returned": len(returned),
            "truncated": total > len(returned),
            "by_category": _counts(by_category),
            "by_severity": _counts(by_severity),
        },
        "tasks": returned,
        "issues": issues[:20],
    }


def _task(category, severity, ref, title, summary, refs=None, **extra):
    seed = "\0".join((category, ref, extra.get("claim_id", ""), title))
    item = {
        "id": "maint-" + hashlib.sha256(
            seed.encode("utf-8", "replace")).hexdigest()[:12],
        "category": category,
        "severity": severity,
        "ref": ref,
        "title": _line(title, 500),
        "summary": _line(summary, 500),
    }
    if refs:
        item["refs"] = refs[:12]
    item.update({key: value for key, value in extra.items() if value})
    return item


def _read_json(path, issues):
    if not path.is_file():
        return {}
    try:
        if path.stat().st_size > _MAX_JSON:
            issues.append(f"{path.name} exceeds the read limit")
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        issues.append(f"{path.name} is unreadable or invalid")
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path, issues):
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > _MAX_JSONL:
            issues.append(f"{path.name} exceeds the read limit")
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        issues.append(f"{path.name} is unreadable")
        return []
    rows = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
    if invalid:
        issues.append(f"{path.name} contains {invalid} invalid line(s)")
    return rows


def _supported_artifact(value, label, issues):
    """Keep legacy or supported artifacts, reject explicit future versions."""
    if not isinstance(value, dict) or "schemaVersion" not in value:
        return value
    try:
        type_registry.require_supported_version(value["schemaVersion"], label)
    except ValueError as exc:
        issues.append(str(exc))
        return {}
    return value


def _newest_source_mtime(root):
    newest = 0.0
    for directory, ntype in schema.DIR_TYPE.items():
        if ntype == "report":
            continue
        folder = root / directory
        if not folder.is_dir():
            continue
        try:
            paths = folder.glob("*.md")
            for path in paths:
                if path.is_symlink():
                    continue
                newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _deduplicate(tasks):
    by_id = {}
    for task in tasks:
        by_id.setdefault(task["id"], task)
    return list(by_id.values())


def _coerce_limit(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError("maintenance limit must be an integer")
    if result < 1:
        raise ValueError("maintenance limit must be at least 1")
    return min(result, MAX_ITEMS)


def _list(value):
    return value if isinstance(value, list) else []


def _strings(value, count, width):
    values = value if isinstance(value, list) else [value] if value else []
    return [_line(item, width) for item in values[:count] if _line(item, width)]


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _counts(counts):
    return {key: counts[key] for key in sorted(counts)}


def _mtime_iso(path):
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return ""


build_maintenance = build
