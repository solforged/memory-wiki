"""Deterministic claim compiler and generated health dashboards."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from . import claims as claim_model
from . import entities as entity_model
from . import schema, temporal, wikifs


AGENT_DIGEST_PATH = ".memory-wiki/cache/agent-digest.json"
CLAIMS_DIGEST_PATH = ".memory-wiki/cache/claims.jsonl"
ENTITIES_DIGEST_PATH = ".memory-wiki/cache/entities.jsonl"
RELATIONSHIPS_DIGEST_PATH = ".memory-wiki/cache/relationships.jsonl"
REPORT_DEFINITIONS = (
    ("open-questions", "Open Questions"),
    ("contradictions", "Contradictions"),
    ("low-confidence", "Low Confidence"),
    ("claim-health", "Claim Health"),
    ("stale-pages", "Stale Pages"),
    ("provenance-coverage", "Provenance Coverage"),
    ("privacy-review", "Privacy Review"),
    ("temporal-integrity", "Temporal integrity"),
    ("entity-relationships", "Entity relationships"),
    ("entity-migration-candidates", "Entity migration candidates"),
)
_LEGACY_CONFIDENCE = {
    "speculative": 0.25,
    "plausible": 0.5,
    "likely": 0.75,
    "confident": 0.9,
}
_EVIDENCE_SECTION = re.compile(
    r"^## Evidence[ \t]*$\n(?P<body>.*?)(?=^## [^\n]+$|\Z)", re.MULTILINE | re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value
            if isinstance(item, str) and item.strip()]


def _confidence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if 0.0 <= value <= 1.0 else None


def _one_line(value):
    return " ".join(str(value or "").split())


def _effective_updated_at(fm):
    for field in ("updatedAt", "updated", "createdAt", "created"):
        value = _string(fm.get(field))
        if value:
            return value
    return None


def _legacy_evidence(body):
    match = _EVIDENCE_SECTION.search(body or "")
    if not match:
        return []
    seen = set()
    evidence = []
    clean = _COMMENT.sub("", match.group("body"))
    for raw in clean.splitlines():
        note = re.sub(r"^(?:[-*+]\s+|>\s*)+", "", raw.strip()).strip()
        if not note or note in seen:
            continue
        seen.add(note)
        evidence.append({"kind": "legacy", "note": note})
    return evidence


def _fallback_claim(fm, body, title):
    fallback = {
        "text": title,
        "status": "supported",
        "evidence": _legacy_evidence(body),
    }
    claim_id = _string(fm.get("id"))
    if claim_id:
        fallback["id"] = claim_id
    numeric = _confidence(fm.get("confidence"))
    if numeric is None:
        numeric = _LEGACY_CONFIDENCE.get(
            str(fm.get("confidence") or "").strip().lower())
    if numeric is not None:
        fallback["confidence"] = numeric
    return fallback


def _page_from_file(root, relpath):
    text = wikifs.read_text(root / relpath)
    if text is None:
        return None
    fm, body = wikifs.parse_frontmatter(text)
    if not isinstance(fm, dict):
        return None
    ntype = _string(fm.get("type"))
    directory = relpath.split("/", 1)[0]
    if (ntype not in schema.TYPE_DIR or ntype == "report"
            or schema.TYPE_DIR[ntype] != directory):
        return None
    title = _string(fm.get("title")) or Path(relpath).stem.replace("-", " ")
    structured = claim_model.normalize_claims(fm.get("claims"))
    if ntype == "claim" and not structured:
        structured = [_fallback_claim(fm, body, title)]
    contradictions = []
    for value in _strings(fm.get("contradictions")) + _strings(fm.get("contradicts")):
        if value not in contradictions:
            contradictions.append(value)
    page = {
        "title": title,
        "kind": ntype,
        "path": relpath,
        "topics": _strings(fm.get("topics")),
        "aliases": _strings(fm.get("aliases")),
        "sourceIds": _strings(fm.get("sourceIds")) + _strings(fm.get("source_ids")),
        "questions": _strings(fm.get("questions")),
        "contradictions": contradictions,
        "claims": structured,
    }
    for field in ("id", "privacyTier"):
        value = _string(fm.get(field))
        if value:
            page[field] = value
    if "entityKind" in fm:
        page["entityKind"] = fm["entityKind"]
    if "relationships" in fm:
        page["relationships"] = fm["relationships"]
    updated_at = _effective_updated_at(fm)
    if updated_at:
        page["updatedAt"] = updated_at
    confidence = _confidence(fm.get("confidence"))
    if confidence is not None:
        page["confidence"] = confidence
    return page


def scan_pages(wiki_root):
    """Read every direct, typed, non-report page in path order."""
    root = Path(wiki_root)
    relpaths = []
    for directory, ntype in schema.DIR_TYPE.items():
        if ntype == "report":
            continue
        folder = root / directory
        if not folder.is_dir():
            continue
        relpaths.extend(
            f"{directory}/{path.name}" for path in folder.iterdir()
            if path.is_file() and not path.is_symlink()
            and path.suffix == ".md" and not path.name.startswith("."))
    pages = [_page_from_file(root, relpath) for relpath in sorted(relpaths)]
    return [page for page in pages if page is not None]


def _freshness_rank(level):
    return {"fresh": 3, "aging": 2, "stale": 1, "unknown": 0}[level]


def _sorted_claims(page, now=None, include_historical=True):
    def key(claim):
        confidence = claim.get("confidence")
        confidence = confidence if isinstance(confidence, (int, float)) else -1
        freshness = claim_model.assess_claim_freshness(page, claim, now)
        return (-confidence, -_freshness_rank(freshness["level"]), claim["text"])
    claims = page.get("claims", [])
    if not include_historical:
        claims = [claim for claim in claims
                  if temporal.is_recallable(claim, now)]
    return sorted(claims, key=key)


def _explicit_contradictions(pages):
    lookup = {}
    for page in pages:
        for key in (page["path"], page["path"].removesuffix(".md"),
                    page.get("id"), page["title"]):
            if key:
                lookup[_one_line(key).lower()] = page["path"]
    entries = []
    for page in pages:
        for target in page.get("contradictions", []):
            normalized = target.replace("\\", "/").removeprefix("./").rstrip("/")
            resolved = lookup.get(normalized.lower()) or lookup.get(
                normalized.removesuffix(".md").lower())
            paths = sorted(set([page["path"]] + ([resolved] if resolved else [])))
            entries.append({
                "key": f"{page['path']}->{normalized}",
                "label": target,
                "pagePath": page["path"],
                "pageTitle": page["title"],
                "target": target,
                "paths": paths,
            })
    return sorted(entries, key=lambda item: (item["pagePath"], item["target"]))


def _claim_health_summary(pages, now=None):
    freshness = {"fresh": 0, "aging": 0, "stale": 0, "unknown": 0}
    temporal_states = {state: 0 for state in sorted(temporal.TEMPORAL_STATES)}
    contested = low = missing = 0
    for item in claim_model.collect_health(pages, now):
        freshness[item["freshness"]["level"]] += 1
        temporal_states[item["temporalState"]] += 1
        contested += int(claim_model.is_contested(item["status"]))
        low += int(isinstance(item.get("confidence"), (int, float))
                   and item["confidence"] < 0.5)
        missing += int(item["missingEvidence"])
    return {
        "freshness": freshness,
        "contested": contested,
        "lowConfidence": low,
        "missingEvidence": missing,
        "temporalStates": temporal_states,
        "lineageIssueCount": len(claim_model.lineage_issues(pages)),
    }


def _digest_claim(page, claim, now=None):
    fresh = claim_model.assess_claim_freshness(page, claim, now)
    item = {
        "text": claim["text"],
        "status": claim_model.normalize_status(claim.get("status")),
        "evidenceCount": len(claim.get("evidence", [])),
        "missingEvidence": not claim.get("evidence"),
        "evidence": list(claim.get("evidence", [])),
        "freshnessLevel": fresh["level"],
        "temporalState": temporal.claim_state(claim, now),
    }
    for field in ("id", "confidence", "validFrom", "validUntil", "supersedes"):
        if field in claim:
            item[field] = claim[field]
    if fresh.get("lastTouchedAt"):
        item["lastTouchedAt"] = fresh["lastTouchedAt"]
    return item


def build_agent_digest(pages, now=None, entity_graph=None):
    page_types = sorted(set(schema.TYPE_DIR) - {"report"})
    counts = {ntype: sum(page["kind"] == ntype for page in pages)
              for ntype in page_types}
    claim_clusters = claim_model.contradiction_clusters(pages, now)
    explicit = _explicit_contradictions(pages)
    clusters = [{
        "key": item["key"], "label": item["label"], "kind": "explicit",
        "entryCount": 1, "paths": item["paths"],
    } for item in explicit]
    clusters.extend({
        "key": cluster["key"], "label": cluster["label"], "kind": "claim-id",
        "entryCount": len(cluster["entries"]),
        "paths": sorted({entry["pagePath"] for entry in cluster["entries"]}),
    } for cluster in claim_clusters)
    digest_pages = []
    for page in sorted(pages, key=lambda item: item["path"]):
        fresh = claim_model.assess_page_freshness(page, now)
        item = {
            "title": page["title"],
            "kind": page["kind"],
            "path": page["path"],
            "topics": list(page.get("topics", [])),
            "aliases": list(page.get("aliases", [])),
            "sourceIds": list(page.get("sourceIds", [])),
            "questions": list(page.get("questions", [])),
            "contradictions": list(page.get("contradictions", [])),
            "freshnessLevel": fresh["level"],
            "claimCount": len(page.get("claims", [])),
            "topClaims": [_digest_claim(page, claim, now)
                          for claim in _sorted_claims(
                              page, now, include_historical=False)[:5]],
        }
        for field in ("id", "confidence", "privacyTier"):
            if field in page:
                item[field] = page[field]
        if fresh.get("lastTouchedAt"):
            item["lastTouchedAt"] = fresh["lastTouchedAt"]
        digest_pages.append(item)
    entity_graph = entity_graph or entity_model.analyze_graph(
        pages, schema.SCHEMA_VERSION)
    return {
        "schemaVersion": schema.SCHEMA_VERSION,
        "pageCounts": counts,
        "claimCount": sum(len(page.get("claims", [])) for page in pages),
        "claimHealth": _claim_health_summary(pages, now),
        "entityGraph": entity_graph["summary"],
        "contradictionClusters": sorted(
            clusters, key=lambda item: (item["label"].lower(), item["kind"])),
        "pages": digest_pages,
    }


def build_claims_jsonl(pages, now=None):
    lines = []
    for page in pages:
        for claim in _sorted_claims(page, now):
            fresh = claim_model.assess_claim_freshness(page, claim, now)
            evidence = list(claim.get("evidence", []))
            item = {
                "schemaVersion": schema.SCHEMA_VERSION,
                "pageTitle": page["title"],
                "pageKind": page["kind"],
                "pagePath": page["path"],
                "aliases": list(page.get("aliases", [])),
                "text": claim["text"],
                "status": claim_model.normalize_status(claim.get("status")),
                "sourceIds": list(page.get("sourceIds", [])),
                "evidenceKinds": sorted({entry["kind"] for entry in evidence
                                         if entry.get("kind")}),
                "privacyTiers": sorted({tier for tier in (
                    [page.get("privacyTier")]
                    + [entry.get("privacyTier") for entry in evidence]) if tier}),
                "evidenceCount": len(evidence),
                "missingEvidence": not evidence,
                "evidence": evidence,
                "freshnessLevel": fresh["level"],
                "temporalState": temporal.claim_state(claim, now),
            }
            if page.get("id"):
                item["pageId"] = page["id"]
            for field in (
                    "id", "confidence", "validFrom", "validUntil",
                    "supersedes"):
                if field in claim:
                    item[field] = claim[field]
            if fresh.get("lastTouchedAt"):
                item["lastTouchedAt"] = fresh["lastTouchedAt"]
            lines.append(json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return "".join(line + "\n" for line in sorted(lines))


def _link(page):
    title = page["title"].replace("]", "\\]")
    return f"[{title}](../{page['path']})"


def _freshness_label(value):
    touched = value.get("lastTouchedAt") or "unknown"
    return f"{value['level']} ({touched})"


def _health_line(item, page_by_path):
    page = page_by_path[item["pagePath"]]
    identity = f"`{item['claimId']}`: " if item.get("claimId") else ""
    details = [f"status {item['status']}"]
    if "confidence" in item:
        details.append(f"confidence {item['confidence']:.2f}")
    details.append("missing evidence" if item["missingEvidence"]
                   else f"{item['evidenceCount']} evidence")
    details.append(_freshness_label(item["freshness"]))
    details.append(f"temporal {item['temporalState']}")
    return f"- {_link(page)}: {identity}{_one_line(item['text'])} ({', '.join(details)})"


def _contradictions_report(pages, now):
    explicit = _explicit_contradictions(pages)
    clusters = claim_model.contradiction_clusters(pages, now)
    if not explicit and not clusters:
        return "- No contradictions flagged right now."
    lines = [f"- Explicit contradiction links: {len(explicit)}",
             f"- Competing claim clusters: {len(clusters)}"]
    page_by_path = {page["path"]: page for page in pages}
    if explicit:
        lines += ["", "### Explicit Links"]
        for item in explicit:
            lines.append(
                f"- {_link(page_by_path[item['pagePath']])} -> `{_one_line(item['target'])}`")
    if clusters:
        lines += ["", "### Claim Clusters"]
        for cluster in clusters:
            entries = [
                f"{_link(page_by_path[item['pagePath']])} -> "
                f"{_one_line(item['text'])} ({item['status']}, "
                f"{_freshness_label(item['freshness'])})"
                for item in cluster["entries"]
            ]
            lines.append(f"- `{cluster['label']}`: " + " | ".join(entries))
    return "\n".join(lines)


def _open_questions_report(pages, now):
    del now
    matches = [page for page in pages if page.get("questions")]
    if not matches:
        return "- No open questions right now."
    lines = [f"- Pages with open questions: {len(matches)}", ""]
    for page in sorted(matches, key=lambda item: item["path"]):
        questions = " | ".join(_one_line(item)
                               for item in page["questions"])
        lines.append(f"- {_link(page)}: {questions}")
    return "\n".join(lines)


def _low_confidence_report(pages, now):
    page_matches = sorted(
        (page for page in pages if isinstance(page.get("confidence"), (int, float))
         and page["confidence"] < 0.5),
        key=lambda page: (page["confidence"], page["path"]))
    claim_matches = sorted(
        (item for item in claim_model.collect_health(pages, now)
         if isinstance(item.get("confidence"), (int, float))
         and item["confidence"] < 0.5),
        key=lambda item: (item["confidence"], item["pagePath"], item["text"]))
    if not page_matches and not claim_matches:
        return "- No low-confidence pages or claims right now."
    lines = [f"- Low-confidence pages: {len(page_matches)}",
             f"- Low-confidence claims: {len(claim_matches)}"]
    if page_matches:
        lines += ["", "### Pages"]
        lines.extend(f"- {_link(page)}: confidence {page['confidence']:.2f}"
                     for page in page_matches)
    if claim_matches:
        by_path = {page["path"]: page for page in pages}
        lines += ["", "### Claims"]
        lines.extend(_health_line(item, by_path) for item in claim_matches)
    return "\n".join(lines)


def _claim_health_report(pages, now):
    health = claim_model.collect_health(pages, now)
    missing = [item for item in health if item["missingEvidence"]]
    contested = [item for item in health if claim_model.is_contested(item["status"])]
    stale = [item for item in health
             if item["freshness"]["level"] in ("stale", "unknown")]
    if not missing and not contested and not stale:
        return "- No claim health issues right now."
    lines = [f"- Claims missing evidence: {len(missing)}",
             f"- Contested claims: {len(contested)}",
             f"- Stale or unknown claims: {len(stale)}"]
    by_path = {page["path"]: page for page in pages}
    for heading, matches in (("Missing Evidence", missing),
                             ("Contested Claims", contested),
                             ("Stale Claims", stale)):
        if matches:
            lines += ["", f"### {heading}"]
            lines.extend(_health_line(item, by_path) for item in matches)
    return "\n".join(lines)


def _stale_pages_report(pages, now):
    matches = []
    for page in pages:
        if page["kind"] in ("concept", "synthesis", "raw"):
            continue
        fresh = claim_model.assess_page_freshness(page, now)
        if fresh["level"] != "fresh":
            matches.append((page, fresh))
    matches.sort(key=lambda item: (item[0]["title"].lower(), item[0]["path"]))
    if not matches:
        return f"- No aging or stale pages older than {claim_model.AGING_DAYS} days."
    return "\n".join([f"- Stale pages: {len(matches)}", ""] + [
        f"- {_link(page)}: {_freshness_label(fresh)}" for page, fresh in matches])


def _count_lines(counter):
    if not counter:
        return ["- None"]
    return [f"- {label}: {count}" for label, count in sorted(
        counter.items(), key=lambda item: (-item[1], item[0]))]


def _provenance_report(pages, now):
    del now
    evidence_entries = []
    missing = []
    for page in pages:
        for claim in page.get("claims", []):
            evidence = claim.get("evidence", [])
            if not evidence:
                missing.append((page, claim))
            evidence_entries.extend(evidence)
    if not evidence_entries and not missing:
        return "- No structured claims with provenance coverage yet."
    kinds = Counter(item.get("kind") or "unspecified" for item in evidence_entries)
    sources = Counter(item.get("sourceId") or item.get("path") or "inline"
                      for item in evidence_entries)
    exact = sum(bool(item.get("excerpt") and item.get("excerptHash"))
                for item in evidence_entries)
    located = sum(bool(item.get("path") and item.get("lines"))
                  for item in evidence_entries)
    observed = sum(bool(item.get("observedAt")) for item in evidence_entries)
    unanchored = sum(not any((item.get("sourceId"), item.get("path"),
                              item.get("excerptHash")))
                     for item in evidence_entries)
    lines = [f"- Evidence entries: {len(evidence_entries)}",
             f"- Claims missing evidence: {len(missing)}", "",
             f"- Exact excerpt anchors: {exact}",
             f"- Path and line anchors: {located}",
             f"- Observation timestamps: {observed}",
             f"- Unanchored evidence: {unanchored}", "",
             "### Evidence Classes", *_count_lines(kinds), "",
             "### Top Evidence Sources", *_count_lines(sources)[:20]]
    if missing:
        lines += ["", "### Missing Evidence"]
        for page, claim in missing:
            identity = f"`{claim['id']}`: " if claim.get("id") else ""
            lines.append(f"- {_link(page)}: {identity}{_one_line(claim['text'])}")
    return "\n".join(lines)


def _temporal_integrity_report(pages, now):
    health = claim_model.collect_health(pages, now)
    counts = Counter(item["temporalState"] for item in health)
    issues = claim_model.lineage_issues(pages)
    invalid = [item for item in health if item["temporalState"] == "invalid"]
    lines = [
        f"- Claims: {len(health)}",
        *(f"- {state}: {counts.get(state, 0)}"
          for state in sorted(temporal.TEMPORAL_STATES)),
        f"- Lineage issues: {len(issues)}",
    ]
    if invalid:
        lines += ["", "### Invalid intervals"]
        lines.extend(
            f"- `{item['key']}`: {_one_line(item['text'])}"
            for item in invalid)
    if issues:
        lines += ["", "### Supersession lineage"]
        for issue in issues:
            if issue["kind"] == "missing-predecessor":
                detail = (f"`{issue['claimId']}` references missing "
                          f"`{issue['predecessorId']}`")
            elif issue["kind"] == "duplicate-id":
                detail = f"duplicate `{issue['claimId']}` in " + " | ".join(
                    f"`{path}`" for path in issue["paths"])
            else:
                detail = "cycle: " + " -> ".join(
                    f"`{claim_id}`" for claim_id in issue["claimIds"])
            lines.append(f"- {detail}")
    if not invalid and not issues:
        lines += ["", "- No temporal or lineage integrity issues right now."]
    return "\n".join(lines)


def _privacy_report(pages, now):
    del now
    entries = []
    for page in pages:
        page_tier = _string(page.get("privacyTier"))
        if page_tier and page_tier.lower() != "public":
            entries.append((page, "page", page_tier, ""))
        for claim in page.get("claims", []):
            for evidence in claim.get("evidence", []):
                tier = _string(evidence.get("privacyTier"))
                if tier and tier.lower() != "public":
                    entries.append((page, "evidence", tier,
                                    _one_line(claim.get("text"))))
    if not entries:
        return "- No non-public privacy tiers need review right now."
    lines = [f"- Privacy review entries: {len(entries)}", ""]
    for page, kind, tier, claim_text in sorted(
            entries, key=lambda item: (item[0]["path"], item[1], item[2], item[3])):
        detail = f"{kind} tier `{tier}`"
        if claim_text:
            detail += f": {claim_text}"
        lines.append(f"- {_link(page)}: {detail}")
    return "\n".join(lines)


def _entity_relationships_report(pages, now):
    del now
    graph = entity_model.analyze_graph(pages, schema.SCHEMA_VERSION)
    summary = graph["summary"]
    lines = [
        f"- Entities: {summary['entityCount']}",
        f"- Relationships: {summary['relationshipCount']}",
        f"- Orphan relationships: {summary['orphanRelationshipCount']}",
        f"- Ambiguous relationships: {summary['ambiguousRelationshipCount']}",
        f"- Invalid entity issues: {summary['invalidEntityCount']}",
        f"- Invalid relationship issues: {summary['invalidRelationshipCount']}",
    ]
    duplicate_ids = graph["duplicateIds"]
    if duplicate_ids:
        lines += ["", "### Duplicate entity IDs"]
        for entity_id, paths in sorted(duplicate_ids.items()):
            lines.append(f"- `{entity_id}`: " + " | ".join(
                f"`{path}`" for path in paths))
    if graph["orphanRelationships"]:
        lines += ["", "### Orphan relationships"]
        for item in graph["orphanRelationships"]:
            lines.append(
                f"- `{item['sourceId']}` `{item['predicate']}` "
                f"`{item['targetId']}` from `{item['sourcePath']}`")
    if graph["ambiguousRelationships"]:
        lines += ["", "### Ambiguous relationships"]
        for item in graph["ambiguousRelationships"]:
            paths = " | ".join(f"`{path}`" for path in item["targetPaths"])
            lines.append(
                f"- `{item['sourceId']}` `{item['predicate']}` "
                f"`{item['targetId']}` from `{item['sourcePath']}`: {paths}")
    issues = graph["invalidEntities"] + graph["invalidRelationships"]
    if issues:
        lines += ["", "### Invalid graph data"]
        lines.extend(f"- {_one_line(issue)}" for issue in issues)
    if not any((duplicate_ids, graph["orphanRelationships"],
                graph["ambiguousRelationships"], issues)):
        lines += ["", "- No entity graph integrity issues right now."]
    return "\n".join(lines)


def _entity_migration_report(pages, now):
    del now
    graph = entity_model.analyze_graph(pages, schema.SCHEMA_VERSION)
    candidates = graph["migrationCandidates"]
    lines = [
        "- Review only: this report never moves or rewrites pages.",
        f"- Refs migration candidates: {len(candidates)}",
    ]
    if not candidates:
        return "\n".join(lines + ["", "- No refs/ pages need entity review."])
    page_by_path = {page["path"]: page for page in pages}
    lines += ["", "### Candidates"]
    for item in candidates:
        existing = ""
        if item["existingEntityPaths"]:
            existing = "; existing entities: " + " | ".join(
                f"`{path}`" for path in item["existingEntityPaths"])
        reason = ", ".join(item["reasons"])
        lines.append(
            f"- {_link(page_by_path[item['path']])}: suggest "
            f"`{item['suggestedPath']}` with `{item['suggestedId']}` "
            f"({item['entityKind']}; {reason}){existing}")
    return "\n".join(lines)


_REPORT_BUILDERS = {
    "open-questions": _open_questions_report,
    "contradictions": _contradictions_report,
    "low-confidence": _low_confidence_report,
    "claim-health": _claim_health_report,
    "stale-pages": _stale_pages_report,
    "provenance-coverage": _provenance_report,
    "privacy-review": _privacy_report,
    "temporal-integrity": _temporal_integrity_report,
    "entity-relationships": _entity_relationships_report,
    "entity-migration-candidates": _entity_migration_report,
}


def _date_string(now):
    if now is None:
        return datetime.now().date().isoformat()
    return (now.date() if isinstance(now, datetime) else now).isoformat()


def _render_report(root, slug, title, body, now=None):
    path = root / "reports" / f"{slug}.md"
    original = wikifs.read_text(path)
    if original is None:
        fm = schema.page_frontmatter(
            "report", title, topics=["report", "health"],
            created=_date_string(now), extras={"status": "published"})
        original = wikifs.render_page(fm, f"# {title}\n")
    else:
        fm, human_body = wikifs.parse_frontmatter(original)
        if not isinstance(fm, dict):
            fm = schema.page_frontmatter(
                "report", title, topics=["report", "health"],
                created=_date_string(now), extras={"status": "published"})
        else:
            fm = dict(fm)
            fm["type"] = "report"
            fm["title"] = title
            fm["topics"] = list(fm.get("topics") or ["report", "health"])
            fm["status"] = "published"
            fm["createdAt"] = str(
                fm.get("createdAt") or _date_string(now))
        original = wikifs.render_page(fm, human_body)
    return wikifs.replace_managed_markdown_block(
        original, "## Generated",
        f"<!-- memory-wiki:report:{slug}:start -->",
        f"<!-- memory-wiki:report:{slug}:end -->", body)


def _compile_unlocked(wiki_root, now=None):
    root = Path(wiki_root)
    pages = scan_pages(root)
    entity_graph = entity_model.analyze_graph(pages, schema.SCHEMA_VERSION)
    digest = build_agent_digest(pages, now, entity_graph)
    updated = []
    for slug, title in REPORT_DEFINITIONS:
        rendered = _render_report(
            root, slug, title, _REPORT_BUILDERS[slug](pages, now), now)
        relpath = f"reports/{slug}.md"
        if wikifs.write_if_changed(root / relpath, rendered):
            updated.append(relpath)
    digest_text = json.dumps(
        digest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    claims_text = build_claims_jsonl(pages, now)
    entities_text = entity_model.jsonl(entity_graph["entities"])
    relationships_text = entity_model.jsonl(entity_graph["relationships"])
    for relpath, content in ((AGENT_DIGEST_PATH, digest_text),
                             (CLAIMS_DIGEST_PATH, claims_text),
                             (ENTITIES_DIGEST_PATH, entities_text),
                             (RELATIONSHIPS_DIGEST_PATH, relationships_text)):
        if wikifs.write_if_changed(root / relpath, content):
            updated.append(relpath)
    return {
        "wikiRoot": str(root),
        "pageCounts": digest["pageCounts"],
        "claimCount": digest["claimCount"],
        "entityGraph": entity_graph["summary"],
        "pages": pages,
        "updatedFiles": sorted(updated),
    }


def compile_wiki(wiki_root, now=None):
    """Compile under the wiki lock. Only changed files are replaced."""
    with wikifs.wiki_lock(wiki_root):
        return _compile_unlocked(wiki_root, now)


compile = compile_wiki
