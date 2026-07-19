"""In-memory page index: one per process, refreshed by mtime scan.

No database. A full refresh is a scandir over at most nine flat
directories plus a reparse of files whose (mtime, size) changed;
cheap for thousands of notes.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import chunks
from . import claims as claim_model
from . import schema, wikifs

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

STOPWORDS = frozenset("""
a about after all also an and any are as at be because been before but by can
could did do does for from had has have he her his how i if in into is it its
just like me my no not of on one only or our out over she so some than that
the their them then there these they this to und up us was we were what when
which who will with would you your
""".split())

_BODY_CAP = 20_000  # chars of body tokenized per page
_RAW_BODY_CAP = 120_000  # edited transcripts remain searchable past summaries


def tokenize(text):
    return [t for t in _TOKEN.findall((text or "").lower())
            if len(t) > 1 and t not in STOPWORDS]


@dataclass
class PageRecord:
    rel: str                    # repo-relative path
    ntype: str                  # page type, or "candidate"
    title: str = ""
    topics: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    page_id: str = ""
    created: str = ""
    confidence: str = ""
    maturity: str = ""
    status: str = ""            # candidates only
    kind: str = ""              # candidates only
    cand_id: str = ""           # candidates only
    content_hash: str = ""      # candidates only (dedup key)
    headings: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    privacy_tier: str = ""
    auto_recall_allowed: bool = True
    body: str = ""              # full canonical, comment-stripped body
    body_source: str = ""       # line-preserving body for derived units
    body_line_offset: int = 0    # source lines before body text
    body_head: str = ""         # first lines, comments stripped
    tokens: Counter = field(default_factory=Counter)
    title_tokens: frozenset = frozenset()
    topic_tokens: frozenset = frozenset()
    alias_tokens: frozenset = frozenset()
    heading_tokens: frozenset = frozenset()
    mtime_ns: int = 0
    size: int = 0


class WikiIndex:
    """Thread-safety: sync_turn runs on the manager's background worker
    while the turn thread searches, so mutation and iteration are
    serialized by one lock."""

    def __init__(self, wiki_root):
        self.root = Path(wiki_root)
        self.pages = {}             # rel -> PageRecord
        self.retrieval_units = {}   # stable unit ref -> RetrievalUnit
        self._unit_refs_by_page = {}
        self.candidate_hashes = set()
        self._lock = threading.Lock()

    # -- refresh ----------------------------------------------------------

    def refresh(self):
        """Rescan the wiki; parse only files whose (mtime, size) changed."""
        with self._lock:
            self._refresh_inner()

    def _refresh_inner(self):
        seen = set()
        for d in list(schema.DIR_TYPE) + [schema.INBOX_DIR]:
            dpath = self.root / d
            if not dpath.is_dir():
                continue
            try:
                entries = list(dpath.iterdir())
            except OSError:
                continue
            for p in entries:
                if (p.suffix != ".md" or p.name.startswith(".")
                        or p.is_symlink()):
                    continue
                rel = f"{d}/{p.name}"
                seen.add(rel)
                try:
                    st = p.stat()
                except OSError:
                    continue
                rec = self.pages.get(rel)
                if rec and rec.mtime_ns == st.st_mtime_ns and rec.size == st.st_size:
                    continue
                parsed = self._parse(rel, p, st)
                if parsed is not None:
                    self.pages[rel] = parsed
                    self._replace_units(parsed)
        for rel in set(self.pages) - seen:
            del self.pages[rel]
            self._drop_units(rel)
        self.candidate_hashes = {r.content_hash for r in self.pages.values()
                                 if r.ntype == "candidate" and r.content_hash}

    def _parse(self, rel, path, st):
        text = wikifs.read_text(path)
        if text is None:
            return None
        fm, body = wikifs.parse_frontmatter(text)
        body_line_offset = 0
        if fm is not None:
            end = text.find("\n---", 4)
            body_line_offset = text[:end + 4].count("\n") if end >= 0 else 0
        fm = fm or {}
        body = _COMMENT.sub(
            lambda match: "\n" * match.group().count("\n"), body)
        is_candidate = rel.startswith(schema.INBOX_DIR + "/")
        searchable_body = body
        if is_candidate:
            marker = f"\n{schema.EVIDENCE_HEADING}\n"
            buffered = "\n" + body
            marker_at = buffered.rfind(marker)
            if marker_at >= 0:
                searchable_body = buffered[1:marker_at]
        ntype = "candidate" if is_candidate else str(fm.get("type") or "")
        body_cap = _RAW_BODY_CAP if ntype == "raw" else _BODY_CAP
        indexed_body = searchable_body[:body_cap]
        canonical_body = " ".join(searchable_body.split())
        title = str(fm.get("title") or path.stem)
        topics = [str(t) for t in fm.get("topics", []) if t] \
            if isinstance(fm.get("topics"), list) else []
        aliases = [str(a) for a in fm.get("aliases", []) if a] \
            if isinstance(fm.get("aliases"), list) else []
        structured_claims = claim_model.normalize_claims(fm.get("claims"))
        privacy_tier = str(fm.get("privacyTier") or "").strip()
        private_evidence = any(
            str(evidence.get("privacyTier") or "").strip().lower()
            not in ("", "public")
            for claim in structured_claims
            for evidence in claim.get("evidence", [])
        )
        auto_recall_allowed = (
            ntype != "raw"
            and privacy_tier.lower() in ("", "public")
            and not private_evidence
        )
        claim_text = _claim_search_text(structured_claims)[:_BODY_CAP]
        headings = [h.strip("# ").strip()
                    for h in re.findall(
                        r"^#{2,4} .+$", indexed_body, re.MULTILINE)]
        rec = PageRecord(
            rel=rel, ntype=ntype, title=title, topics=topics, aliases=aliases,
            page_id=str(fm.get("id") or ""),
            created=str(fm.get("createdAt") or fm.get("created") or ""),
            confidence=str(fm.get("confidence") or ""),
            maturity=str(fm.get("maturity") or ""),
            status=str(fm.get("status") or "") if is_candidate else "",
            kind=str(fm.get("kind") or ""),
            cand_id=str(fm.get("id") or "") if is_candidate else "",
            headings=headings, claims=structured_claims,
            privacy_tier=privacy_tier,
            auto_recall_allowed=auto_recall_allowed,
            body=canonical_body,
            body_source=searchable_body,
            body_line_offset=body_line_offset,
            body_head=" ".join(indexed_body.split())[:400],
            mtime_ns=st.st_mtime_ns, size=st.st_size,
        )
        rec.tokens = Counter(
            tokenize(title) + tokenize(" ".join(topics + aliases + headings))
            + tokenize(indexed_body) + tokenize(claim_text))
        rec.title_tokens = frozenset(tokenize(title))
        rec.topic_tokens = frozenset(tokenize(" ".join(topics)))
        rec.alias_tokens = frozenset(tokenize(" ".join(aliases)))
        rec.heading_tokens = frozenset(tokenize(" ".join(headings)))
        if is_candidate:
            rec.content_hash = str(fm.get("hash") or "")
        return rec

    def _replace_units(self, rec):
        self._drop_units(rec.rel)
        unit_refs = []
        for unit in chunks.units_for_record(rec):
            cap = _RAW_BODY_CAP if unit.ntype == "raw" else _BODY_CAP
            unit.tokens = Counter(tokenize(unit.search_text[:cap]))
            if unit.kind == "page":
                unit.title_tokens = frozenset(tokenize(rec.title))
                unit.topic_tokens = frozenset(tokenize(" ".join(rec.topics)))
                unit.alias_tokens = frozenset(tokenize(" ".join(rec.aliases)))
            elif unit.kind == "heading":
                unit.heading_tokens = frozenset(tokenize(unit.heading))
            self.retrieval_units[unit.ref] = unit
            unit_refs.append(unit.ref)
        self._unit_refs_by_page[rec.rel] = tuple(unit_refs)

    def _drop_units(self, page_ref):
        for unit_ref in self._unit_refs_by_page.pop(page_ref, ()):
            self.retrieval_units.pop(unit_ref, None)

    # -- queries ------------------------------------------------------------

    def records(self, corpus="pages", type_filter=None):
        """Records for a corpus: pages (curated knowledge), raw archives,
        profile, journal, candidates, privacy-safe automatic prefetch, or all.
        Iterates a snapshot; safe across threads."""
        with self._lock:
            snapshot = list(self.pages.values())
        for rec in snapshot:
            if corpus == "pages":
                if rec.ntype not in schema.KNOWLEDGE_TYPES:
                    continue
            elif corpus == "raw":
                if rec.ntype not in schema.ARCHIVE_TYPES:
                    continue
            elif corpus == "profile":
                if rec.ntype != "profile":
                    continue
            elif corpus == "journal":
                if rec.ntype != "journal":
                    continue
            elif corpus == "candidates":
                if rec.ntype != "candidate":
                    continue
            elif corpus == "prefetch":
                if rec.ntype not in (*schema.KNOWLEDGE_TYPES, "journal",
                                     "candidate"):
                    continue
                if not rec.auto_recall_allowed:
                    continue
            elif corpus != "all":
                continue
            if type_filter and rec.ntype != type_filter:
                continue
            yield rec

    def pending_candidates(self):
        return sorted((r for r in self.records("candidates")
                       if r.status == "pending"), key=lambda r: r.rel)

    def units(self, corpus="pages", type_filter=None, granularity="auto",
              as_of=None, include_historical=False, page_refs=None):
        """Derived units filtered through the same page/corpus contract."""
        if granularity not in chunks.GRANULARITIES:
            raise ValueError(f"unsupported retrieval granularity: {granularity}")
        allowed = {rec.rel for rec in self.records(corpus, type_filter)}
        if page_refs is not None:
            allowed.intersection_update(str(ref) for ref in page_refs)
        with self._lock:
            snapshot = list(self.retrieval_units.values())
        for unit in snapshot:
            if unit.page_ref not in allowed:
                continue
            if granularity != "auto" and unit.kind != granularity:
                continue
            if not unit.recallable(
                    as_of, include_historical=include_historical):
                continue
            yield unit

    def unit(self, ref):
        """Resolve one exact derived ref from current Markdown state."""
        with self._lock:
            return self.retrieval_units.get(str(ref or ""))

    def counts(self):
        by = {}
        for rec in self.records("all"):
            key = rec.ntype or "?"
            by[key] = by.get(key, 0) + 1
        return by


def _claim_search_text(claims):
    values = []
    for claim in claims:
        values.extend(str(claim.get(field) or "")
                      for field in (
                          "id", "text", "status", "validFrom", "validUntil",
                          "supersedes"))
        for evidence in claim.get("evidence", []):
            values.extend(str(evidence.get(field) or "") for field in (
                "kind", "sourceId", "path", "lines", "privacyTier", "note",
                "observedAt", "excerpt", "excerptHash"))
    return " ".join(values)
