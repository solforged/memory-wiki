"""Candidate lifecycle: one markdown file per staged memory in _inbox/.

Filenames are <UTCstamp>-<session8>-<seq>-<slug>.md, created O_EXCL so
concurrent sessions never contend. decisions.jsonl is the append-only
audit trail (O_APPEND single-write; no lock needed).
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

from . import schema, wikifs
from .index import tokenize

logger = logging.getLogger(__name__)

_HEX = re.compile(r"[^0-9a-f]")
_DEDUP_OVERLAP = 0.8
_MAX_CONTENT = 50_000


def content_hash(text):
    norm = " ".join((text or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def session8(session_id):
    h = _HEX.sub("", (session_id or "").lower())[:8]
    return (h + "0" * 8)[:8]


def record(cfg, action, cand_id="", target="", session="", actor="",
           chash="", detail=None):
    """One audit line in _inbox/decisions.jsonl."""
    wikifs.append_jsonl(Path(cfg.wiki_root) / schema.DECISIONS_FILE, {
        "ts": wikifs.utc_iso(), "action": action, "id": cand_id,
        "target": target, "session": session, "actor": actor,
        "hash": chash, "detail": detail or {}})


# -- staging ------------------------------------------------------------------

def stage(cfg, index, content, kind="fact", origin="tool", session_id="",
          turn=0, target="", section="", confidence="plausible",
          topics=None, fields=None, evidence=None, actor="tool",
          supersedes=""):
    """Stage one candidate. Returns {staged, id?, ref?, reason?}.

    supersedes: id of a promoted candidate this content replaces; skips
    dedup (overlap with the bullet being replaced is the whole point).

    Content keeps its original formatting (multi-line markdown and full
    pages with frontmatter survive review verbatim); normalization is
    applied only to the hash, slug, and dedup checks.
    """
    content = (content or "").strip()
    if not content:
        return {"staged": False, "reason": "empty content"}
    if len(content) > _MAX_CONTENT:
        return {"staged": False, "reason":
                f"content is {len(content)} chars (max {_MAX_CONTENT}); "
                "stage a summary and keep the full text elsewhere"}
    if kind not in schema.CANDIDATE_KINDS:
        return {"staged": False, "reason": f"unknown kind '{kind}'"}
    if confidence not in schema.CONFIDENCE:
        return {"staged": False, "reason": f"unknown confidence '{confidence}'"}
    if origin not in schema.CANDIDATE_ORIGINS:
        return {"staged": False, "reason": f"unknown origin '{origin}'"}
    fields = {k: v for k, v in (fields or {}).items()
              if k in schema.FIELDS_ALLOWLIST}
    if target:
        target_path = wikifs.safe_wiki_path(cfg.wiki_root, target)  # raises on escape
    if page_shaped(content):
        if target and target_path.exists():
            return {"staged": False, "reason":
                    "page-shaped content needs a new-page target"}
        errors = validate_page_candidate(content, target)
        if errors:
            return {"staged": False, "reason": "; ".join(errors)}

    chash = content_hash(content)
    if not supersedes:
        dup = duplicate_reason(index, chash, content)
        if dup:
            return {"staged": False, "reason": dup, "hash": chash}

    fm = {
        "id": "",  # filled below once the filename seq is known
        "status": "pending", "kind": kind,
        "target": target or "", "section": section or "",
        "confidence": confidence, "origin": origin,
        "session": session_id or "", "turn": int(turn or 0),
        "created": wikifs.today(), "reviewed": "", "promoted_to": "",
        "topics": list(topics or []), "fields": fields, "hash": chash,
    }
    if supersedes:
        fm["supersedes"] = supersedes
    body_parts = [content, "", schema.EVIDENCE_HEADING, ""]
    for line in _clean_evidence(evidence or [f"proposed via {origin}"]):
        body_parts.append(line if line.startswith(("- ", "> ")) else f"> {line}")
    body = "\n".join(body_parts) + "\n"

    stamp, s8 = wikifs.utc_stamp(), session8(session_id)
    slug = wikifs.slugify(_slug_source(content), max_len=40)
    inbox = Path(cfg.wiki_root) / schema.INBOX_DIR
    for seq in range(1, 100):
        cand_id = f"{stamp}-{s8}-{seq:02d}"
        if any(inbox.glob(f"{cand_id}-*.md")):
            continue  # id taken by a different-slug candidate this second
        fm["id"] = cand_id
        path = inbox / f"{cand_id}-{slug}.md"
        if wikifs.create_exclusive(path, wikifs.render_page(fm, body)):
            if index is not None:
                index.candidate_hashes.add(chash)
            record(cfg, "staged", cand_id=cand_id, target=target,
                   session=session_id, actor=actor, chash=chash,
                   detail={"kind": kind, "origin": origin,
                           "confidence": confidence})
            return {"staged": True, "id": cand_id,
                    "ref": f"{schema.INBOX_DIR}/{path.name}"}
    return {"staged": False, "reason": "could not allocate candidate id"}


def page_shaped(content):
    """True if staged content is a full page (leading frontmatter)."""
    return (content or "").startswith("---\n")


def validate_page_candidate(content, target=""):
    """Schema errors for page-shaped staged content, checked against its
    eventual rel so staging fails fast; promotion revalidates against
    the real target."""
    fm, _ = wikifs.parse_frontmatter(content)
    if fm is None:
        return ["page-shaped content has unparsable frontmatter"]
    ntype = fm.get("type")
    d = schema.TYPE_DIR.get(ntype)
    if d is None:
        return [f"page-shaped content has unknown type '{ntype}'"]
    rel = target or f"{d}/{wikifs.slugify(str(fm.get('title') or ''))}.md"
    return schema.validate_page(rel, content)


def _slug_source(content):
    if page_shaped(content):
        fm, _ = wikifs.parse_frontmatter(content)
        title = str((fm or {}).get("title") or "")
        if title:
            return title
    return content


def _clean_evidence(lines):
    """One physical line per evidence element; an embedded newline could
    otherwise plant a bare heading that breaks split_evidence."""
    return [" ".join(str(l).split()) for l in lines if str(l).strip()]


def split_evidence(body):
    """(content, evidence_lines) split at the LAST line-anchored evidence
    heading: staged content may contain its own '## Evidence', but the
    writers always append the real evidence section at the very end."""
    sep = f"\n{schema.EVIDENCE_HEADING}\n"
    buf = "\n" + (body or "")
    i = buf.rfind(sep)
    if i == -1:
        return (body or "").strip(), []
    content = buf[1:i]
    tail = buf[i + len(sep):]
    return content.strip(), [l for l in tail.splitlines() if l.strip()]


def duplicate_reason(index, chash, content):
    """Non-empty string if this content is already staged or known."""
    if index is None:
        return ""
    if chash in index.candidate_hashes:
        return "duplicate of an existing candidate"
    ctokens = set(tokenize(content))
    if not ctokens:
        return "no content tokens"
    for rec in index.records("profile"):
        for line in rec.body_head.split("- "):
            ltokens = set(tokenize(line))
            # short fragments/titles are too easy to overlap by accident
            if len(ltokens) >= 3 and _overlap(ctokens, ltokens) >= _DEDUP_OVERLAP:
                return f"already recorded in {rec.rel}"
    for rec in index.records("pages"):
        if len(rec.title_tokens) >= 3 and \
                _overlap(ctokens, rec.title_tokens) >= _DEDUP_OVERLAP:
            return f"already covered by {rec.rel}"
    return ""


def _overlap(a, b):
    return len(a & b) / max(1, min(len(a), len(b)))


# -- loading and status --------------------------------------------------------

def find_path(cfg, cand_id):
    matches = sorted(
        (Path(cfg.wiki_root) / schema.INBOX_DIR).glob(f"{cand_id}-*.md"))
    return matches[0] if matches else None


def load(cfg, cand_id):
    """(path, fm, content, evidence_lines) or None. content is the text
    that promotion writes (body above ## Evidence)."""
    path = find_path(cfg, cand_id)
    if path is None:
        return None
    text = wikifs.read_text(path)
    if text is None:
        return None
    fm, body = wikifs.parse_frontmatter(text)
    if fm is None:
        return None
    content, evidence = split_evidence(body)
    return path, fm, content, evidence


def set_status_unlocked(cfg, path, fm, body_content, evidence, status,
                        reviewed_note="", promoted_to="", overrides=None):
    """Rewrite a candidate file in place; caller holds the wiki lock."""
    fm = dict(fm)
    fm.update(overrides or {})
    fm["status"] = status
    if reviewed_note:
        fm["reviewed"] = reviewed_note
    if promoted_to:
        fm["promoted_to"] = promoted_to
    evidence = _clean_evidence(evidence)
    body = body_content.strip() + "\n\n" + schema.EVIDENCE_HEADING + "\n\n" \
        + "\n".join(evidence) + ("\n" if evidence else "")
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return fm


def list_candidates(cfg, statuses=("pending",)):
    out = []
    inbox = Path(cfg.wiki_root) / schema.INBOX_DIR
    if not inbox.is_dir():
        return out
    for path in sorted(inbox.glob("*.md")):
        text = wikifs.read_text(path)
        fm, body = wikifs.parse_frontmatter(text or "")
        if not fm:
            continue
        if statuses and fm.get("status") not in statuses:
            continue
        head = split_evidence(body)[0]
        out.append({"id": fm.get("id", ""), "status": fm.get("status", ""),
                    "kind": fm.get("kind", ""),
                    "confidence": fm.get("confidence", ""),
                    "target": fm.get("target", ""),
                    "created": fm.get("created", ""),
                    "age_days": wikifs.days_since(fm.get("created", "")),
                    "content": head, "ref": f"{schema.INBOX_DIR}/{path.name}"})
    return out


def prune(cfg, days=30):
    """Delete promoted/rejected candidate files older than `days`.
    decisions.jsonl keeps the history. Returns removed refs."""
    cutoff = time.time() - days * 86400
    removed = []
    inbox = Path(cfg.wiki_root) / schema.INBOX_DIR
    if not inbox.is_dir():
        return removed
    with wikifs.wiki_lock(cfg.wiki_root):
        for path in sorted(inbox.glob("*.md")):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            fm, _ = wikifs.parse_frontmatter(wikifs.read_text(path) or "")
            if not fm or fm.get("status") not in ("promoted", "rejected"):
                continue
            path.unlink()
            removed.append(f"{schema.INBOX_DIR}/{path.name}")
            record(cfg, "pruned", cand_id=fm.get("id", ""), actor="cli",
                   detail={"status": fm.get("status", "")})
    return removed
