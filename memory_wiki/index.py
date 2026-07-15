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
    created: str = ""
    confidence: str = ""
    maturity: str = ""
    status: str = ""            # candidates only
    kind: str = ""              # candidates only
    cand_id: str = ""           # candidates only
    content_hash: str = ""      # candidates only (dedup key)
    headings: list = field(default_factory=list)
    body: str = ""              # full canonical, comment-stripped body
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
                if p.suffix != ".md" or p.name.startswith("."):
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
        for rel in set(self.pages) - seen:
            del self.pages[rel]
        self.candidate_hashes = {r.content_hash for r in self.pages.values()
                                 if r.ntype == "candidate" and r.content_hash}

    def _parse(self, rel, path, st):
        text = wikifs.read_text(path)
        if text is None:
            return None
        fm, body = wikifs.parse_frontmatter(text)
        fm = fm or {}
        body = _COMMENT.sub("", body)
        is_candidate = rel.startswith(schema.INBOX_DIR + "/")
        searchable_body = body
        if is_candidate:
            marker = f"\n{schema.EVIDENCE_HEADING}\n"
            buffered = "\n" + body
            marker_at = buffered.rfind(marker)
            if marker_at >= 0:
                searchable_body = buffered[1:marker_at]
        indexed_body = searchable_body[:_BODY_CAP]
        canonical_body = " ".join(searchable_body.split())
        ntype = "candidate" if is_candidate else str(fm.get("type") or "")
        title = str(fm.get("title") or path.stem)
        topics = [str(t) for t in fm.get("topics", []) if t] \
            if isinstance(fm.get("topics"), list) else []
        aliases = [str(a) for a in fm.get("aliases", []) if a] \
            if isinstance(fm.get("aliases"), list) else []
        headings = [h.strip("# ").strip()
                    for h in re.findall(
                        r"^#{2,4} .+$", indexed_body, re.MULTILINE)]
        rec = PageRecord(
            rel=rel, ntype=ntype, title=title, topics=topics, aliases=aliases,
            created=str(fm.get("created") or ""),
            confidence=str(fm.get("confidence") or ""),
            maturity=str(fm.get("maturity") or ""),
            status=str(fm.get("status") or "") if is_candidate else "",
            kind=str(fm.get("kind") or ""),
            cand_id=str(fm.get("id") or "") if is_candidate else "",
            headings=headings,
            body=canonical_body,
            body_head=" ".join(indexed_body.split())[:400],
            mtime_ns=st.st_mtime_ns, size=st.st_size,
        )
        rec.tokens = Counter(
            tokenize(title) + tokenize(" ".join(topics + aliases + headings))
            + tokenize(indexed_body))
        rec.title_tokens = frozenset(tokenize(title))
        rec.topic_tokens = frozenset(tokenize(" ".join(topics)))
        rec.alias_tokens = frozenset(tokenize(" ".join(aliases)))
        rec.heading_tokens = frozenset(tokenize(" ".join(headings)))
        if is_candidate:
            rec.content_hash = str(fm.get("hash") or "")
        return rec

    # -- queries ------------------------------------------------------------

    def records(self, corpus="pages", type_filter=None):
        """Records for a corpus: pages (knowledge types), profile, journal,
        candidates, or all. Iterates a snapshot; safe across threads."""
        with self._lock:
            snapshot = list(self.pages.values())
        for rec in snapshot:
            if corpus == "pages":
                if rec.ntype not in schema.KNOWLEDGE_TYPES:
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
            elif corpus != "all":
                continue
            if type_filter and rec.ntype != type_filter:
                continue
            yield rec

    def pending_candidates(self):
        return sorted((r for r in self.records("candidates")
                       if r.status == "pending"), key=lambda r: r.rel)

    def counts(self):
        by = {}
        for rec in self.records("all"):
            key = rec.ntype or "?"
            by[key] = by.get(key, 0) + 1
        return by
