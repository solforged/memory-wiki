"""Deterministic claim compiler and generated health dashboards."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from . import claims as claim_model
from . import schema, wikifs


AGENT_DIGEST_PATH = ".memory-wiki/cache/agent-digest.json"
CLAIMS_DIGEST_PATH = ".memory-wiki/cache/claims.jsonl"
REPORT_DEFINITIONS = (
    ("open-questions", "Open Questions"),
    ("contradictions", "Contradictions"),
    ("low-confidence", "Low Confidence"),
    ("claim-health", "Claim Health"),
    ("stale-pages", "Stale Pages"),
    ("provenance-coverage", "Provenance Coverage"),
    ("privacy-review", "Privacy Review"),
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


def _sorted_claims(page, now=None):
    def key(claim):
        confidence = claim.get("confidence")
        confidence = confidence if isinstance(confidence, (int, float)) else -1
        freshness = claim_model.assess_claim_freshness(page, claim, now)
        return (-confidence, -_freshness_rank(freshness["level"]), claim["text"])
    return sorted(page.get("claims", []), key=key)


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
    contested = low = missing = 0
    for item in claim_model.collect_health(pages, now):
        freshness[item["freshness"]["level"]] += 1
        contested += int(claim_model.is_contested(item["status"]))
        low += int(isinstance(item.get("confidence"), (int, float))
                   and item["confidence"] < 0.5)
        missing += int(item["missingEvidence"])
    return {
        "freshness": freshness,
        "contested": contested,
        "lowConfidence": low,
        "missingEvidence": missing,
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
    }
    for field in ("id", "confidence"):
        if field in claim:
            item[field] = claim[field]
    if fresh.get("lastTouchedAt"):
        item["lastTouchedAt"] = fresh["lastTouchedAt"]
    return item


def build_agent_digest(pages, now=None):
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
                          for claim in _sorted_claims(page, now)[:5]],
        }
        for field in ("id", "confidence", "privacyTier"):
            if field in page:
                item[field] = page[field]
        if fresh.get("lastTouchedAt"):
            item["lastTouchedAt"] = fresh["lastTouchedAt"]
        digest_pages.append(item)
    return {
        "pageCounts": counts,
        "claimCount": sum(len(page.get("claims", [])) for page in pages),
        "claimHealth": _claim_health_summary(pages, now),
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
            }
            if page.get("id"):
                item["pageId"] = page["id"]
            for field in ("id", "confidence"):
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
        if page["kind"] in ("concept", "synthesis"):
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
    lines = [f"- Evidence entries: {len(evidence_entries)}",
             f"- Claims missing evidence: {len(missing)}", "",
             "### Evidence Classes", *_count_lines(kinds), "",
             "### Top Evidence Sources", *_count_lines(sources)[:20]]
    if missing:
        lines += ["", "### Missing Evidence"]
        for page, claim in missing:
            identity = f"`{claim['id']}`: " if claim.get("id") else ""
            lines.append(f"- {_link(page)}: {identity}{_one_line(claim['text'])}")
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


_REPORT_BUILDERS = {
    "open-questions": _open_questions_report,
    "contradictions": _contradictions_report,
    "low-confidence": _low_confidence_report,
    "claim-health": _claim_health_report,
    "stale-pages": _stale_pages_report,
    "provenance-coverage": _provenance_report,
    "privacy-review": _privacy_report,
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
    digest = build_agent_digest(pages, now)
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
    for relpath, content in ((AGENT_DIGEST_PATH, digest_text),
                             (CLAIMS_DIGEST_PATH, claims_text)):
        if wikifs.write_if_changed(root / relpath, content):
            updated.append(relpath)
    return {
        "wikiRoot": str(root),
        "pageCounts": digest["pageCounts"],
        "claimCount": digest["claimCount"],
        "pages": pages,
        "updatedFiles": sorted(updated),
    }


def compile_wiki(wiki_root, now=None):
    """Compile under the wiki lock. Only changed files are replaced."""
    with wikifs.wiki_lock(wiki_root):
        return _compile_unlocked(wiki_root, now)


compile = compile_wiki
