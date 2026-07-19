"""Deterministic retrieval units derived from canonical Markdown pages."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote

from . import temporal


UNIT_KINDS = ("page", "claim", "evidence", "heading")
GRANULARITIES = (*UNIT_KINDS, "auto")

_HEADING = re.compile(r"^(#{2,4})\s+(.+?)\s*$")
_LINE_RANGE = re.compile(r"^L?(\d+)(?:\s*-\s*L?(\d+))?$")
_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass
class RetrievalUnit:
    """One disposable search unit owned by a page."""

    ref: str
    page_ref: str
    kind: str
    title: str
    ntype: str
    text: str
    search_text: str
    page: object = field(default=None, repr=False, compare=False)
    claim: dict | None = field(default=None, repr=False, compare=False)
    evidence: dict | None = field(default=None, repr=False, compare=False)
    claim_id: str = ""
    heading: str = ""
    evidence_anchor: str = ""
    line_start: int | None = None
    line_end: int | None = None
    privacy_tier: str = ""
    auto_recall_allowed: bool = True
    created: str = ""
    tokens: Counter = field(default_factory=Counter)
    title_tokens: frozenset = frozenset()
    topic_tokens: frozenset = frozenset()
    alias_tokens: frozenset = frozenset()
    heading_tokens: frozenset = frozenset()

    def temporal_state(self, as_of=None):
        if self.claim is None:
            return "not-applicable"
        return temporal.claim_state(self.claim, as_of)

    def recallable(self, as_of=None, include_historical=False):
        if self.claim is None:
            return True
        return temporal.is_recallable(
            self.claim, as_of, include_historical=include_historical)


def units_for_record(record):
    """Derive stable page, claim, evidence, and heading units."""
    body = str(getattr(record, "body_source", "") or record.body or "")
    units = [_page_unit(record, body)]
    used = set()
    claims = list(getattr(record, "claims", []) or [])
    if record.ntype == "claim" and not claims:
        fallback = {
            "text": record.title,
            "status": "supported",
            "evidence": [],
        }
        if getattr(record, "page_id", ""):
            fallback["id"] = record.page_id
        claims = [fallback]
    for claim in claims:
        claim_anchor = _claim_anchor(claim)
        claim_anchor = _unique_anchor(claim_anchor, used)
        units.append(_claim_unit(record, claim, claim_anchor))
        evidence_used = set()
        for evidence in claim.get("evidence", []) or []:
            evidence_anchor = _evidence_unit_anchor(
                claim_anchor, evidence, evidence_used)
            units.append(_evidence_unit(
                record, claim, evidence, evidence_anchor))
    units.extend(_heading_units(record, body, used))
    return units


def split_unit_ref(ref):
    """Return ``(page_ref, kind, anchor)`` for a derived unit ref."""
    page_ref, separator, anchor = str(ref or "").rpartition("#")
    if not separator or not page_ref.endswith(".md") or ":" not in anchor:
        return None
    kind, _, value = anchor.partition(":")
    if kind not in UNIT_KINDS[1:] or not value:
        return None
    return page_ref, kind, anchor


def unit_payload(unit, as_of=None):
    """Exact, JSON-native payload for ``wiki_get``."""
    out = {
        "ref": unit.ref,
        "unit_id": unit.ref,
        "page_ref": unit.page_ref,
        "unit_kind": unit.kind,
        "title": unit.title,
        "type": unit.ntype,
        "content": unit.text,
    }
    if unit.line_start is not None:
        out["line_start"] = unit.line_start
        out["line_end"] = unit.line_end or unit.line_start
    if unit.claim is not None:
        out["claim_id"] = unit.claim_id
        out["temporal_state"] = unit.temporal_state(as_of)
    if unit.kind == "claim":
        out["claim"] = dict(unit.claim or {})
    elif unit.kind == "evidence":
        out["evidence"] = dict(unit.evidence or {})
        if unit.evidence_anchor:
            out["evidence_anchor"] = unit.evidence_anchor
    elif unit.kind == "heading":
        out["heading"] = unit.heading
    return out


def exact_evidence_anchor(evidence):
    """Build a precise source/excerpt anchor when evidence supplies one."""
    evidence = evidence if isinstance(evidence, dict) else {}
    path = str(evidence.get("path") or "").strip()
    source_id = str(evidence.get("sourceId") or "").strip()
    lines = str(evidence.get("lines") or "").strip()
    excerpt_hash = str(evidence.get("excerptHash") or "").strip()
    base = path or source_id
    if path and lines:
        match = _LINE_RANGE.fullmatch(lines)
        if match:
            start, end = match.groups()
            suffix = f"#L{start}"
            if end and end != start:
                suffix += f"-L{end}"
            return path + suffix
    if base and excerpt_hash:
        return f"{base}#excerpt={excerpt_hash}"
    return base


def _page_unit(record, body):
    search_text = " | ".join(value for value in (
        record.title,
        " ".join(getattr(record, "topics", []) or []),
        " ".join(getattr(record, "aliases", []) or []),
        body,
    ) if value)
    return _base_unit(
        record, ref=record.rel, kind="page", text=body.strip(),
        search_text=search_text)


def _claim_unit(record, claim, anchor):
    claim_id = str(claim.get("id") or "")
    text = str(claim.get("text") or "").strip()
    search_text = " | ".join(
        str(claim.get(field) or "") for field in (
            "id", "text", "status", "validFrom", "validUntil", "supersedes")
        if claim.get(field))
    unit = _base_unit(
        record, ref=f"{record.rel}#{anchor}", kind="claim", text=text,
        search_text=search_text)
    unit.claim = claim
    unit.claim_id = claim_id
    return unit


def _evidence_unit(record, claim, evidence, anchor):
    excerpt = str(evidence.get("excerpt") or "").strip()
    note = str(evidence.get("note") or "").strip()
    text = excerpt or note or _evidence_summary(evidence)
    search_text = " | ".join(
        str(evidence.get(field) or "") for field in (
            "kind", "sourceId", "path", "lines", "note", "observedAt",
            "updatedAt", "excerpt", "excerptHash") if evidence.get(field))
    unit = _base_unit(
        record, ref=f"{record.rel}#{anchor}", kind="evidence", text=text,
        search_text=search_text)
    unit.claim = claim
    unit.claim_id = str(claim.get("id") or "")
    unit.evidence = evidence
    unit.evidence_anchor = exact_evidence_anchor(evidence)
    unit.privacy_tier = str(
        evidence.get("privacyTier") or unit.privacy_tier).strip()
    return unit


def _heading_units(record, body, used):
    lines = body.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    units = []
    line_offset = int(getattr(record, "body_line_offset", 0) or 0)
    for position, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _next_title in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        content = "\n".join(lines[start:end]).strip()
        anchor = _unique_anchor(f"heading:{_slug(title)}", used)
        unit = _base_unit(
            record, ref=f"{record.rel}#{anchor}", kind="heading",
            text=content, search_text=content)
        unit.heading = title
        unit.line_start = line_offset + start + 1
        unit.line_end = line_offset + max(start, end - 1) + 1
        units.append(unit)
    return units


def _base_unit(record, *, ref, kind, text, search_text):
    return RetrievalUnit(
        ref=ref,
        page_ref=record.rel,
        kind=kind,
        title=record.title,
        ntype=record.ntype,
        text=text,
        search_text=search_text,
        page=record,
        privacy_tier=str(getattr(record, "privacy_tier", "") or ""),
        auto_recall_allowed=bool(
            getattr(record, "auto_recall_allowed", True)),
        created=str(getattr(record, "created", "") or ""),
    )


def _claim_anchor(claim):
    claim_id = str(claim.get("id") or "").strip()
    if claim_id:
        key = quote(claim_id, safe=".-_~")
    else:
        key = "anon-" + _digest({
            field: claim.get(field) for field in (
                "text", "status", "validFrom", "validUntil")
            if claim.get(field) is not None
        })
    return f"claim:{key}"


def _evidence_unit_anchor(claim_anchor, evidence, used):
    identity_fields = (
        "kind", "sourceId", "path", "lines", "observedAt", "excerptHash",
        "excerpt", "note",
    )
    identity = {
        field: evidence[field] for field in identity_fields
        if evidence.get(field) is not None
    }
    digest = _digest(identity)
    claim_key = claim_anchor.removeprefix("claim:")
    return _unique_anchor(f"evidence:{claim_key}:{digest}", used)


def _unique_anchor(anchor, used):
    candidate = anchor
    suffix = 2
    while candidate in used:
        candidate = f"{anchor}:{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _slug(value):
    slug = _SLUG.sub("-", value.lower()).strip("-")
    return slug[:80] or "section"


def _digest(value):
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _evidence_summary(evidence):
    return " | ".join(
        str(evidence.get(field) or "") for field in (
            "kind", "sourceId", "path", "lines", "observedAt")
        if evidence.get(field))
