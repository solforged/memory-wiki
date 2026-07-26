"""Read-only dashboard API for memory-wiki.

The dashboard loader imports this file directly through the user-plugin
symlink. Keep it independent of the memory_wiki package and optional runtime
dependencies. FastAPI is the only required non-stdlib import.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter


router = APIRouter()

_MAX_JSON = 4 * 1024 * 1024
_MAX_JSONL = 8 * 1024 * 1024
_MAX_REPORT = 1024 * 1024
_MAX_ITEMS = 40
_MAX_CANDIDATE = 128 * 1024
_SUPPORTED_SCHEMA_VERSION = 2
_CONFIG_DEFAULTS = {
    "wiki_root": "~/wiki",
    "search_backend": "local",
    "qmd_command": "qmd",
    "qmd_search_mode": "search",
    "qmd_state_dir": "",
    "qmd_update_interval_seconds": 300,
    "qmd_embed_interval_seconds": 3600,
    "dream_schedule_enabled": False,
    "dream_schedule": "0 3 * * *",
    "candidate_stale_days": 30,
}
_REPORT_NAMES = (
    "open-questions",
    "contradictions",
    "low-confidence",
    "claim-health",
    "stale-pages",
    "provenance-coverage",
    "privacy-review",
)
_DREAM_HEADING = re.compile(
    r"^##\s+Dream sweep\s+(\d{4}-\d{2}-\d{2}T[^\s]+)", re.MULTILINE)
_FRONTMATTER_STATUS = re.compile(
    r"(?m)^status:\s*['\"]?([a-z-]+)['\"]?\s*(?:#.*)?$")
_DREAM_JOB_NAME = "memory-wiki-dream-sweep"
_DREAM_JOB_SCRIPT = "memory-wiki-dream-sweep.py"
_LEGACY_DREAM_JOBS = {
    "memory-wiki-dream-light",
    "memory-wiki-dream-rem",
    "memory-wiki-dream-deep",
}


@router.get("/overview")
async def get_overview() -> dict[str, Any]:
    """Return bounded, read-only compiler and dream status."""
    try:
        return build_overview()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Overview unavailable: {type(exc).__name__}",
        }


@router.get("/review")
async def get_review(limit: int = _MAX_ITEMS, status: str = "",
                     kind: str = "", origin: str = "") -> dict[str, Any]:
    """Return bounded candidate details for read-only review."""
    try:
        return build_review(
            limit=limit, status=status, kind=kind, origin=origin)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Review queue unavailable: {type(exc).__name__}",
        }


@router.get("/history")
async def get_history(limit: int = _MAX_ITEMS, action: str = "",
                      target: str = "") -> dict[str, Any]:
    """Return bounded, sanitized lifecycle audit events."""
    try:
        return build_history(limit=limit, action=action, target=target)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"History unavailable: {type(exc).__name__}",
        }


def build_overview(hermes_home: str | Path | None = None) -> dict[str, Any]:
    home = _hermes_home(hermes_home)
    config, config_issue = _plugin_config(home)
    root = _wiki_root(config)
    issues: list[str] = []
    if config_issue:
        issues.append(config_issue)

    compiler, claims, reports, compiler_issues = _compiler_overview(root)
    issues.extend(compiler_issues)
    dream, dream_issues = _dream_overview(root)
    issues.extend(dream_issues)

    return {
        "ok": True,
        "wiki": {
            "exists": root.is_dir(),
            "root": str(root),
        },
        "compiler": compiler,
        "claims": claims,
        "reports": reports,
        "dream": dream,
        "scheduler": _scheduler_overview(home, config),
        "candidates": _candidate_overview(root),
        "qmd": _qmd_overview(root, config),
        "issues": issues[:20],
    }


def build_review(hermes_home: str | Path | None = None, limit: int = _MAX_ITEMS,
                 status: str = "", kind: str = "",
                 origin: str = "") -> dict[str, Any]:
    """Read candidate Markdown without importing package write paths."""
    home = _hermes_home(hermes_home)
    config, config_issue = _plugin_config(home)
    root = _wiki_root(config)
    item_limit = _api_limit(limit)
    stale_days = _positive_int(config.get("candidate_stale_days"), 30)
    filters = {
        "status": _clean_text(status, 30).lower(),
        "kind": _clean_text(kind, 30).lower(),
        "origin": _clean_text(origin, 40).lower(),
    }
    issues = [config_issue] if config_issue else []
    candidates: list[dict[str, Any]] = []
    inbox = root / "_inbox"
    if inbox.is_dir():
        try:
            paths = sorted(inbox.glob("*.md"))[:20_000]
        except OSError:
            paths = []
            issues.append("Candidate inbox could not be listed.")
        for path in paths:
            candidate, issue = _read_candidate(path, root, stale_days)
            if issue:
                issues.append(issue)
            if candidate is None:
                continue
            if not filters["status"] and candidate.get("status") not in (
                    "pending", "approved"):
                continue
            if any(filters[key] and candidate.get(key) != filters[key]
                   for key in filters):
                continue
            candidates.append(candidate)
    candidates.sort(key=lambda item: (item.get("ref", ""), item.get("id", "")))
    total = len(candidates)
    returned = candidates[:item_limit]
    return {
        "ok": True,
        "read_only": True,
        "summary": {
            "matched": total,
            "returned": len(returned),
            "truncated": total > len(returned),
            "stale": sum(1 for item in candidates if item.get("stale")),
            "by_status": _item_counts(candidates, "status"),
            "by_kind": _item_counts(candidates, "kind"),
            "by_origin": _item_counts(candidates, "origin"),
        },
        "candidates": returned,
        "issues": issues[:20],
    }


def build_history(hermes_home: str | Path | None = None,
                  limit: int = _MAX_ITEMS, action: str = "",
                  target: str = "") -> dict[str, Any]:
    """Read lifecycle audit entries without repairing or locking the log."""
    home = _hermes_home(hermes_home)
    config, config_issue = _plugin_config(home)
    root = _wiki_root(config)
    item_limit = _api_limit(limit)
    rows, issue = _read_jsonl(root / "_inbox" / "decisions.jsonl", _MAX_JSONL)
    action = _clean_text(action, 80)
    target = _clean_text(target, 240)
    events = []
    for row in rows:
        if action and str(row.get("action") or "") != action:
            continue
        if target and str(row.get("target") or "") != target:
            continue
        events.append(_public_history_event(row))
    events.sort(key=lambda item: (item.get("ts", ""), item["event_id"]),
                reverse=True)
    total = len(events)
    returned = events[:item_limit]
    issues = [value for value in (config_issue,
              f"Audit log: {issue}." if issue not in ("", "missing") else "")
              if value]
    return {
        "ok": True,
        "read_only": True,
        "summary": {
            "matched": total,
            "returned": len(returned),
            "truncated": total > len(returned),
            "by_action": _item_counts(events, "action"),
            "by_actor": _item_counts(events, "actor"),
        },
        "events": returned,
        "issues": issues[:20],
    }


def _hermes_home(value: str | Path | None) -> Path:
    raw = value if value is not None else os.environ.get("HERMES_HOME", "")
    return Path(raw or "~/.hermes").expanduser().resolve()


def _wiki_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("wiki_root") or _CONFIG_DEFAULTS["wiki_root"])
    path = Path(os.path.expandvars(raw)).expanduser()
    return path.resolve()


def _plugin_config(home: Path) -> tuple[dict[str, Any], str]:
    """Read the small YAML block needed by the dashboard without PyYAML."""
    values = dict(_CONFIG_DEFAULTS)
    path = home / "config.yaml"
    if not path.is_file():
        return values, "Hermes config.yaml was not found; using defaults."
    try:
        text = _bounded_text(path, 2 * 1024 * 1024)
    except (OSError, ValueError):
        return values, "Hermes config.yaml could not be read; using defaults."

    parents: list[tuple[int, str]] = []
    found_block = False
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        content = _strip_yaml_comment(raw_line.strip())
        if not content or ":" not in content:
            continue
        key_raw, value_raw = content.split(":", 1)
        key = _yaml_key(key_raw)
        if not key:
            continue
        while parents and parents[-1][0] >= indent:
            parents.pop()
        path_keys = tuple(item[1] for item in parents) + (key,)
        value_raw = value_raw.strip()
        if not value_raw:
            parents.append((indent, key))
            continue
        if path_keys[:2] != ("plugins", "memory-wiki"):
            continue
        found_block = True
        field = path_keys[-1].replace("-", "_")
        if field in values:
            values[field] = _yaml_scalar(value_raw)
    issue = "" if found_block else (
        "plugins.memory-wiki is absent from config.yaml; using defaults.")
    return values, issue


def _strip_yaml_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in ("'", '"'):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _yaml_key(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return str(_yaml_scalar(value))
    return value


def _yaml_scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _compiler_overview(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    cache = root / ".memory-wiki" / "cache"
    digest_path = cache / "agent-digest.json"
    claims_path = cache / "claims.jsonl"
    issues: list[str] = []

    digest, digest_issue = _read_json(digest_path, _MAX_JSON)
    if digest_issue and digest_issue != "missing":
        issues.append(f"Compiler digest: {digest_issue}.")
    digest = digest if isinstance(digest, dict) else {}
    artifact_issue = _schema_issue(digest, "Compiler digest")
    if artifact_issue:
        issues.append(artifact_issue)
        digest = {}

    claim_rows, claims_issue = _read_jsonl(claims_path, _MAX_JSONL)
    if claims_issue and claims_issue != "missing":
        issues.append(f"Claim cache: {claims_issue}.")
    supported_rows = []
    rejected_claim_schema = False
    for row in claim_rows:
        artifact_issue = _schema_issue(row, "Claim cache")
        if artifact_issue:
            rejected_claim_schema = True
            if artifact_issue not in issues:
                issues.append(artifact_issue)
            continue
        supported_rows.append(row)
    claim_rows = supported_rows

    reports = {
        name: _report_overview(root / "reports" / f"{name}.md")
        for name in _REPORT_NAMES
    }
    raw_health = digest.get("claimHealth") or digest.get("health")
    health = raw_health if isinstance(raw_health, dict) else {}
    raw_digest_clusters = digest.get("contradictionClusters")
    has_digest_clusters = isinstance(raw_digest_clusters, list)
    digest_contradiction_items = _contradiction_items(raw_digest_clusters)
    if not digest_contradiction_items:
        digest_contradiction_items = _normalise_items(
            digest.get("contradictions"), "contradiction")
    contradiction_items = digest_contradiction_items
    if not contradiction_items:
        contradiction_items = reports["contradictions"]["items"]

    by_confidence: dict[str, int] = {}
    derived_low: list[dict[str, str]] = []
    derived_stale: list[dict[str, str]] = []
    derived_provenance: list[dict[str, str]] = []
    derived_contradictions: list[dict[str, str]] = []
    for raw in claim_rows:
        claim = raw.get("claim") if isinstance(raw.get("claim"), dict) else raw
        if not isinstance(claim, dict):
            continue
        confidence = _confidence_bucket(claim.get("confidence"))
        by_confidence[confidence] = by_confidence.get(confidence, 0) + 1
        item = _claim_item(claim)
        if _is_low_confidence(claim.get("confidence")):
            derived_low.append(item)
        freshness = claim.get("freshness")
        freshness_level = claim.get("freshnessLevel")
        if not freshness_level and isinstance(freshness, dict):
            freshness_level = freshness.get("level")
        if (str(freshness_level or "").lower() == "stale"
                or bool(claim.get("stale"))):
            derived_stale.append(item)
        evidence = claim.get("evidence")
        provenance = claim.get("provenance")
        if (claim.get("missingEvidence") is True
                or ("missingEvidence" not in claim
                    and not evidence and not provenance)):
            derived_provenance.append(item)
        if claim.get("contradictions") or claim.get("contradicts") or claim.get("conflicts"):
            derived_contradictions.append(item)

    total = _first_number(
        digest.get("claimCount"), digest.get("claim_count"), len(claim_rows))
    low_items = reports["low-confidence"]["items"] or derived_low[:_MAX_ITEMS]
    stale_items = reports["stale-pages"]["items"] or derived_stale[:_MAX_ITEMS]
    provenance_items = (
        reports["provenance-coverage"]["items"] or derived_provenance[:_MAX_ITEMS])
    contradiction_items = (
        contradiction_items or derived_contradictions[:_MAX_ITEMS])
    digest_pages = digest.get("pages")
    digest_stale_pages = (sum(
        1 for page in digest_pages
        if isinstance(page, dict)
        and str(page.get("freshnessLevel") or "").lower() == "stale")
        if isinstance(digest_pages, list) else None)

    contradiction_count = _health_count(
        health,
        ("contradictions", "contradictionCount", "contradiction_count"),
        (len(digest_contradiction_items) if has_digest_clusters
         else reports["contradictions"]["count"] or len(contradiction_items)),
    )
    low_count = _health_count(
        health,
        ("lowConfidence", "low_confidence", "lowConfidenceCount"),
        reports["low-confidence"]["count"] or len(low_items),
    )
    stale_count = _health_count(
        health,
        ("stalePages", "stale_pages", "staleCount"),
        (digest_stale_pages if digest_stale_pages is not None
         else reports["stale-pages"]["count"] or len(stale_items)),
    )
    provenance_count = _health_count(
        health,
        ("missingEvidence", "provenanceGaps", "provenance_gaps",
         "missingProvenance"),
        reports["provenance-coverage"]["count"] or len(provenance_items),
    )

    page_counts = digest.get("pageCounts") or digest.get("page_counts") or {}
    if not isinstance(page_counts, dict):
        page_counts = {}
    page_total = (len(digest_pages) if isinstance(digest_pages, (list, dict))
                  else sum(v for v in page_counts.values()
                           if isinstance(v, int) and v >= 0))
    generated_at = _clean_text(
        digest.get("generatedAt") or digest.get("generated_at")
        or digest.get("compiledAt") or _mtime_iso(digest_path), 80)

    compiler = {
        "available": bool(digest or claim_rows or any(r["exists"] for r in reports.values())),
        "digest_available": bool(digest),
        "claims_available": claims_path.is_file() and not rejected_claim_schema,
        "generated_at": generated_at,
        "schema_version": digest.get("version") or digest.get("schemaVersion") or "",
        "page_count": page_total,
        "page_counts": _bounded_counts(page_counts),
    }
    claims = {
        "total": total,
        "by_confidence": by_confidence,
        "contradiction_count": contradiction_count,
        "low_confidence_count": low_count,
        "stale_count": stale_count,
        "provenance_gap_count": provenance_count,
        "contradictions": contradiction_items[:_MAX_ITEMS],
        "low_confidence": low_items[:_MAX_ITEMS],
        "stale": stale_items[:_MAX_ITEMS],
        "provenance_gaps": provenance_items[:_MAX_ITEMS],
    }
    return compiler, claims, reports, issues


def _schema_issue(value: Any, label: str) -> str:
    """Reject explicit artifact versions this bundled reader cannot parse."""
    if not isinstance(value, dict) or "schemaVersion" not in value:
        return ""
    version = value.get("schemaVersion")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return f"{label}: unsupported schemaVersion."
    if version > _SUPPORTED_SCHEMA_VERSION:
        return (f"{label}: schemaVersion {version} is newer than supported "
                f"version {_SUPPORTED_SCHEMA_VERSION}.")
    return ""


def _read_json(path: Path, limit: int) -> tuple[Any, str]:
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(_bounded_text(path, limit)), ""
    except ValueError as exc:
        return None, "too large" if "exceeds" in str(exc) else "invalid JSON"
    except OSError:
        return None, "unreadable"


def _read_jsonl(path: Path, limit: int) -> tuple[list[dict[str, Any]], str]:
    if not path.is_file():
        return [], "missing"
    try:
        text = _bounded_text(path, limit)
    except ValueError:
        return [], "too large"
    except OSError:
        return [], "unreadable"
    rows: list[dict[str, Any]] = []
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
        if len(rows) >= 100_000:
            break
    return rows, (f"{invalid} invalid line(s)" if invalid else "")


def _bounded_text(path: Path, limit: int) -> str:
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"file exceeds {limit} bytes")
    return path.read_text(encoding="utf-8")


def _read_candidate(path: Path, root: Path,
                    stale_days: int) -> tuple[dict[str, Any] | None, str]:
    try:
        text = _bounded_text(path, _MAX_CANDIDATE)
    except (OSError, UnicodeError, ValueError):
        return None, f"Candidate {path.name} is unreadable or too large."
    frontmatter, body = _simple_frontmatter(text)
    if not frontmatter:
        return None, f"Candidate {path.name} has invalid frontmatter."
    content = re.split(
        r"(?m)^##\s+Evidence\s*$", body, maxsplit=1)[0].strip()
    content = re.sub(
        r"<!--\s*memory-wiki:page-fm\n.*?\n-->",
        "",
        content,
        flags=re.S,
    ).strip()
    clean_content = _clean_content(content, 2_000)
    created = _clean_text(frontmatter.get("created"), 40)
    age_days = _days_since(created)
    session = str(frontmatter.get("session") or "")
    rel = path.relative_to(root).as_posix()
    title = _clean_text(frontmatter.get("title"), 240) or _candidate_title(
        content, frontmatter.get("id") or path.stem)
    return {
        "id": _clean_text(frontmatter.get("id"), 160),
        "title": title,
        "content": clean_content,
        "content_truncated": len(content) > len(clean_content),
        "preview": _clean_text(content, 240),
        "status": _clean_text(frontmatter.get("status"), 30).lower(),
        "kind": _clean_text(frontmatter.get("kind"), 30).lower(),
        "confidence": _clean_text(frontmatter.get("confidence"), 30).lower(),
        "origin": _clean_text(frontmatter.get("origin"), 40).lower(),
        "session_hash": (_session_hash(session) if session else ""),
        "turn": _positive_int(frontmatter.get("turn"), 0),
        "created": created,
        "age_days": age_days,
        "stale": bool(age_days is not None and stale_days
                      and age_days > stale_days),
        "target": _clean_text(frontmatter.get("target"), 240),
        "section": _clean_text(frontmatter.get("section"), 160),
        "supersedes": _clean_text(frontmatter.get("supersedes"), 160),
        "content_hash": _clean_text(frontmatter.get("hash"), 160),
        "ref": rel,
    }, ""


def _simple_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((index for index, line in enumerate(lines[1:], 1)
                if line.strip() == "---"), None)
    if end is None:
        return {}, text
    values: dict[str, Any] = {}
    for raw in lines[1:end]:
        if not raw or raw[0].isspace() or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = _yaml_key(key)
        if key:
            values[key] = _yaml_scalar(_strip_yaml_comment(value).strip())
    return values, "\n".join(lines[end + 1:])


def _candidate_title(content: str, fallback: Any) -> str:
    for raw in content.splitlines():
        line = re.sub(r"^(?:#{1,6}|[-*+])\s+", "", raw.strip())
        if line:
            return _clean_text(line, 240)
    return _clean_text(fallback, 240) or "Untitled candidate"


def _clean_content(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "")
    return text[:limit]


def _days_since(value: Any) -> int | None:
    raw = _clean_text(value, 40)
    if not raw:
        return None
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc).date() - created).days)


def _public_history_event(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = _clean_text(row.get("ts"), 80)
    action = _clean_text(row.get("action"), 80)
    cand_id = _clean_text(row.get("id"), 160)
    target = _clean_text(row.get("target"), 240)
    actor = _clean_text(row.get("actor"), 80)
    session = str(row.get("session") or "")
    content_hash = _clean_text(row.get("hash"), 160)
    seed = "\0".join((timestamp, action, cand_id, target, content_hash))
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    allowed = ("reason", "kind", "origin", "confidence", "status", "ref",
               "superseded_by")
    safe_detail = {
        key: _clean_text(detail[key], 500) for key in allowed
        if isinstance(detail.get(key), (str, int, float, bool))
        and detail.get(key) not in (None, "")
    }
    return {
        "event_id": "event-" + hashlib.sha256(
            seed.encode("utf-8", "replace")).hexdigest()[:12],
        "ts": timestamp,
        "action": action,
        "id": cand_id,
        "target": target,
        "actor": actor,
        "session_hash": _session_hash(session) if session else "",
        "content_hash": content_hash,
        "detail": safe_detail,
    }


def _session_hash(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8", "replace")).hexdigest()[:10]


def _api_limit(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = _MAX_ITEMS
    return min(_MAX_ITEMS, max(1, result))


def _positive_int(value: Any, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, result)


def _item_counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = _clean_text(item.get(key), 80) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return {name: counts[name] for name in sorted(counts)}


def _report_overview(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "updated_at": "", "count": 0, "items": []}
    try:
        text = _bounded_text(path, _MAX_REPORT)
    except (OSError, ValueError, UnicodeError):
        return {
            "exists": True,
            "updated_at": _mtime_iso(path),
            "count": 0,
            "items": [],
            "issue": "unreadable",
        }
    items: list[dict[str, str]] = []
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("- ") or re.match(r"^\d+\.\s", stripped)):
            continue
        summary = re.sub(r"^(?:- |\d+\.\s+)", "", stripped)
        if not summary.lower().startswith(("no ", "none ")):
            count += 1
        if len(items) < _MAX_ITEMS:
            items.append({"summary": _clean_markdown(summary)})
    return {
        "exists": True,
        "updated_at": _mtime_iso(path),
        "count": count,
        "items": items,
    }


def _clean_markdown(value: Any) -> str:
    text = _clean_text(value, 500)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_]", "", text)
    return text.strip()


def _normalise_items(value: Any, kind: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items = []
    for raw in value[:_MAX_ITEMS]:
        if isinstance(raw, str):
            items.append({"summary": _clean_text(raw, 500)})
        elif isinstance(raw, dict):
            item = _claim_item(raw)
            item["kind"] = kind
            items.append(item)
    return items


def _contradiction_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items = []
    for raw in value[:_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        paths = raw.get("paths") if isinstance(raw.get("paths"), list) else []
        safe_paths = [_clean_text(path, 240) for path in paths[:8] if path]
        count = _first_number(raw.get("entryCount"), len(safe_paths))
        summary = _clean_text(raw.get("summary") or raw.get("detail"), 500)
        if not summary:
            noun = "entry" if count == 1 else "entries"
            summary = f"{count} {noun}"
            if safe_paths:
                summary += ": " + ", ".join(safe_paths)
        items.append({
            "kind": _clean_text(raw.get("kind") or "contradiction", 40),
            "ref": safe_paths[0] if safe_paths else "",
            "title": _clean_text(
                raw.get("label") or raw.get("title") or raw.get("key")
                or "Contradiction", 240),
            "summary": summary,
        })
    return items


def _claim_item(claim: dict[str, Any]) -> dict[str, str]:
    ref = (claim.get("pagePath") or claim.get("ref") or claim.get("pageRef")
           or claim.get("page_ref") or "")
    title = (claim.get("title") or claim.get("text") or claim.get("claim")
             or claim.get("pageTitle")
             or claim.get("id") or ref or "Claim")
    detail = (claim.get("reason") or claim.get("summary")
              or claim.get("detail") or "")
    return {
        "ref": _clean_text(ref, 240),
        "title": _clean_text(title, 240),
        "summary": _clean_text(detail, 500),
    }


def _confidence_bucket(value: Any) -> str:
    numeric = _numeric_confidence(value)
    if numeric is not None:
        if numeric < 0.5:
            return "low"
        if numeric < 0.8:
            return "medium"
        return "high"
    text = _clean_text(value, 40).lower()
    return text or "unknown"


def _numeric_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_low_confidence(value: Any) -> bool:
    numeric = _numeric_confidence(value)
    if numeric is not None:
        return numeric < 0.5
    return _clean_text(value, 40).lower() in ("plausible", "low", "uncertain")


def _health_count(health: dict[str, Any], keys: tuple[str, ...], fallback: int) -> int:
    for key in keys:
        value = health.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return max(0, int(fallback or 0))


def _first_number(*values: Any) -> int:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return 0


def _bounded_counts(value: dict[Any, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, count in list(value.items())[:50]:
        if isinstance(count, (int, float)) and not isinstance(count, bool):
            result[_clean_text(key, 60)] = max(0, int(count))
    return result


def _dream_overview(root: Path) -> tuple[dict[str, Any], list[str]]:
    state_path = root / ".memory-wiki" / "dream-state.json"
    state, issue = _read_json(state_path, 1024 * 1024)
    issues: list[str] = []
    if issue and issue != "missing":
        issues.append(f"Dream state: {issue}.")
    state = state if isinstance(state, dict) else {}
    raw_candidates = state.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, dict) else {}
    sweep = state.get("sweep") if isinstance(state.get("sweep"), dict) else {}
    sweep_phases = (sweep.get("phases")
                    if isinstance(sweep.get("phases"), dict) else {})

    phase_data: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    for phase, hit_key, time_key in (
        ("light", "light_hits", "last_light_at"),
        ("rem", "rem_hits", "last_rem_at"),
    ):
        hits = 0
        touched = 0
        latest = ""
        for raw in candidates.values():
            if not isinstance(raw, dict):
                continue
            count = _first_number(raw.get(hit_key))
            hits += count
            if count:
                touched += 1
            timestamp = _clean_text(raw.get(time_key), 80)
            if timestamp > latest:
                latest = timestamp
        phase_record = (sweep_phases.get(phase)
                        if isinstance(sweep_phases.get(phase), dict) else {})
        phase_status = str(phase_record.get("status") or "").lower()
        if phase_status not in ("running", "complete", "failed"):
            running = (str(sweep.get("status") or "").lower() == "running"
                       and str(sweep.get("last_phase") or "").lower() == phase)
            failed = (str(sweep.get("status") or "").lower() == "failed"
                      and str(sweep.get("last_phase") or "").lower() == phase)
            phase_status = "running" if running else "failed" if failed \
                else "complete" if latest else "never"
        phase_time = _clean_text(
            phase_record.get("completed_at")
            or phase_record.get("started_at"), 80)
        latest = max(latest, phase_time)
        phase_data[phase] = {
            "name": phase,
            "status": phase_status,
            "last_run_at": latest,
            "hits": hits,
            "candidate_count": touched,
        }
        if latest:
            recent.append({
                "phase": phase,
                "at": latest,
                "status": phase_status,
                "summary": (_clean_text(phase_record.get("error"), 300)
                            if phase_status == "failed" else
                            f"{hits} reinforcement hit(s) across {touched} candidate(s)"),
            })

    deep_runs = _deep_runs(root / "DREAMS.md")
    deep_latest = deep_runs[0]["at"] if deep_runs else ""
    if str(sweep.get("last_phase") or "").lower() == "deep":
        deep_latest = max(
            deep_latest,
            _clean_text(sweep.get("completed_at") or sweep.get("started_at"), 80),
        )
    deep_record = (sweep_phases.get("deep")
                   if isinstance(sweep_phases.get("deep"), dict) else {})
    deep_status = str(deep_record.get("status") or "").lower()
    if deep_status not in ("running", "complete", "failed"):
        deep_running = (str(sweep.get("status") or "").lower() == "running"
                        and str(sweep.get("last_phase") or "").lower() == "deep")
        deep_failed = (str(sweep.get("status") or "").lower() == "failed"
                       and str(sweep.get("last_phase") or "").lower() == "deep")
        deep_status = "running" if deep_running else "failed" if deep_failed \
            else "complete" if deep_latest else "never"
    deep_latest = max(deep_latest, _clean_text(
        deep_record.get("completed_at") or deep_record.get("started_at"), 80))
    phase_data["deep"] = {
        "name": "deep",
        "status": deep_status,
        "last_run_at": deep_latest,
        "hits": 0,
        "candidate_count": len(candidates),
    }
    recent.extend(deep_runs)
    if deep_status != "never" and deep_latest and not any(
            run.get("at") == deep_latest for run in deep_runs):
        recent.append({
            "phase": "deep", "at": deep_latest, "status": deep_status,
            "summary": (_clean_text(deep_record.get("error"), 300)
                        or _clean_text(sweep.get("error"), 300)
                        or "Deep curation completed"),
        })
    recent.sort(key=lambda row: str(row.get("at") or ""), reverse=True)

    safe_sweep = {
        "status": _clean_text(sweep.get("status") or "idle", 30),
        "started_at": _clean_text(sweep.get("started_at"), 80),
        "completed_at": _clean_text(sweep.get("completed_at"), 80),
        "last_phase": _clean_text(sweep.get("last_phase"), 20),
        "error": _clean_text(sweep.get("error"), 300),
    }
    return {
        "exists": bool(state),
        "updated_at": _clean_text(state.get("updated_at"), 80),
        "candidate_count": len(candidates),
        "sweep": safe_sweep,
        "phases": [phase_data[name] for name in ("light", "rem", "deep")],
        "recent_runs": recent[:12],
    }, issues


def _deep_runs(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        text = _bounded_text(path, 2 * 1024 * 1024)
    except (OSError, ValueError, UnicodeError):
        return []
    matches = list(_DREAM_HEADING.finditer(text))
    runs = []
    for index, match in enumerate(matches[-12:]):
        start = match.end()
        end = matches[matches.index(match) + 1].start() if match is not matches[-1] else len(text)
        section = text[start:end]
        suggestions = sum(1 for line in section.splitlines()
                          if line.strip().startswith("- ")
                          and "never promotion inputs" not in line)
        runs.append({
            "phase": "deep",
            "at": _clean_text(match.group(1), 80),
            "status": "complete",
            "summary": f"{suggestions} review suggestion(s)",
        })
    runs.sort(key=lambda row: row["at"], reverse=True)
    return runs


def _scheduler_overview(home: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Read the active profile's cron store without importing Hermes."""
    enabled = bool(config.get("dream_schedule_enabled", False))
    schedule = _clean_text(
        config.get("dream_schedule") or _CONFIG_DEFAULTS["dream_schedule"], 120)
    path = home / "cron" / "jobs.json"
    raw, issue = _read_json(path, _MAX_JSON)
    if issue not in ("", "missing"):
        return {
            "available": False,
            "enabled": enabled,
            "schedule": schedule,
            "job": None,
            "duplicates": 0,
            "in_sync": False,
            "error": f"Cron store is {issue}.",
        }

    if issue == "missing":
        jobs: list[Any] = []
    elif isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
        jobs = raw["jobs"]
    elif isinstance(raw, list):
        jobs = raw
    else:
        return {
            "available": False,
            "enabled": enabled,
            "schedule": schedule,
            "job": None,
            "duplicates": 0,
            "in_sync": False,
            "error": "Cron store has an unsupported shape.",
        }

    managed = [job for job in jobs if _is_managed_job(job)]
    primary = next(
        (job for job in managed if job.get("name") == _DREAM_JOB_NAME),
        managed[0] if managed else None,
    )
    job = _public_job(primary)
    job_enabled = bool(job and job.get("enabled")
                       and job.get("state") != "paused")
    job_schedule = str(job.get("schedule_display") or "") if job else ""
    in_sync = (bool(job) and job_enabled and job_schedule == schedule
               if enabled else not managed)
    return {
        "available": True,
        "enabled": enabled,
        "schedule": schedule,
        "job": job,
        "duplicates": max(0, len(managed) - (1 if primary else 0)),
        "in_sync": bool(in_sync),
        "error": "",
    }


def _is_managed_job(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    name = _clean_text(job.get("name"), 120)
    script = Path(_clean_text(job.get("script"), 240)).name
    return (name == _DREAM_JOB_NAME or name in _LEGACY_DREAM_JOBS
            or script == _DREAM_JOB_SCRIPT)


def _public_job(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    schedule = _clean_text(job.get("schedule_display"), 120)
    if not schedule:
        raw_schedule = job.get("schedule")
        if isinstance(raw_schedule, dict):
            schedule = _clean_text(
                raw_schedule.get("display") or raw_schedule.get("value")
                or raw_schedule.get("expr") or raw_schedule.get("run_at"), 120)
        else:
            schedule = _clean_text(raw_schedule, 120)
    enabled = bool(job.get("enabled", True))
    state = _clean_text(
        job.get("state") or ("scheduled" if enabled else "paused"), 40)
    return {
        "id": _clean_text(job.get("id"), 120),
        "name": _clean_text(job.get("name") or _DREAM_JOB_NAME, 120),
        "schedule_display": schedule,
        "enabled": enabled,
        "state": state,
        "next_run_at": _clean_text(job.get("next_run_at"), 80),
        "last_run_at": _clean_text(job.get("last_run_at"), 80),
        "last_status": _clean_text(job.get("last_status"), 40),
        "last_error": _clean_text(job.get("last_error"), 300),
    }


def _candidate_overview(root: Path) -> dict[str, int]:
    inbox = root / "_inbox"
    counts = {"pending": 0, "approved": 0}
    if not inbox.is_dir():
        return counts
    try:
        paths = sorted(inbox.glob("*.md"))[:20_000]
    except OSError:
        return counts
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                prefix = handle.read(16_384)
        except (OSError, UnicodeError):
            continue
        match = _FRONTMATTER_STATUS.search(prefix)
        if match and match.group(1) in counts:
            counts[match.group(1)] += 1
    return counts


def _qmd_overview(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    backend = str(config.get("search_backend") or "local").lower()
    command = str(config.get("qmd_command") or "qmd")
    mode = str(config.get("qmd_search_mode") or "search").lower()
    tag = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    collection = f"memory-wiki-{tag}"
    configured_state = str(config.get("qmd_state_dir") or "").strip()
    state_dir = (Path(os.path.expandvars(configured_state)).expanduser()
                 if configured_state else
                 Path.home() / ".cache" / "memory-wiki" / "qmd" / tag)
    state_dir = state_dir.resolve()
    marker, _marker_issue = _read_json(state_dir / "adapter.json", 64 * 1024)
    marker = marker if isinstance(marker, dict) else {}
    marker_valid = (marker.get("wiki_root") == str(root)
                    and marker.get("collection") == collection)
    db_path = state_dir / "xdg" / "cache" / "qmd" / "index.sqlite"
    command_available = _command_available(command)
    configured = backend in ("qmd", "auto")
    return {
        "configured": configured,
        "backend": backend,
        "available": bool(configured and command_available),
        "initialized": bool(marker_valid and db_path.is_file()),
        "command": Path(command).name or "qmd",
        "search_mode": mode if mode in ("search", "vsearch", "query") else "search",
        "collection": collection,
        "updated_at": _timestamp_iso(marker.get("updated_at")) if marker_valid else "",
        "embedded_at": _timestamp_iso(marker.get("embedded_at")) if marker_valid else "",
    }


def _command_available(command: str) -> bool:
    if os.sep in command or (os.altsep and os.altsep in command):
        return os.path.isfile(command) and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def _timestamp_iso(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return ""


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]
