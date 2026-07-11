"""Candidate lifecycle over Markdown files and one durable JSONL audit."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from . import schema, wikifs
from .index import tokenize

logger = logging.getLogger(__name__)

_HEX = re.compile(r"[^0-9a-f]")
_DEDUP_OVERLAP = 0.8
_MAX_CONTENT = 50_000
_ACTIVE_HASH_STATUSES = frozenset(("pending", "approved", "promoted"))
_FINAL_BULLET_MARKER = re.compile(
    r"<!-- memory-wiki: ([^\s>]+) -->\s*$")
_PROMOTED_MARKER = re.compile(
    r"<!-- memory-wiki: promoted ([^\s>]+) -->")


def content_hash(text):
    norm = " ".join((text or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def session8(session_id):
    h = _HEX.sub("", (session_id or "").lower())[:8]
    return (h + "0" * 8)[:8]


def record(cfg, action, cand_id="", target="", session="", actor="",
           chash="", detail=None):
    """Durably record one event, taking the wiki lock.

    Candidate lifecycle events are once-only by ``(action, id)``. Events
    without an id are direct-write audit entries and remain repeatable.
    """
    with wikifs.wiki_lock(cfg.wiki_root):
        return record_unlocked(
            cfg, action, cand_id=cand_id, target=target, session=session,
            actor=actor, chash=chash, detail=detail)


def record_unlocked(cfg, action, cand_id="", target="", session="", actor="",
                    chash="", detail=None):
    """Lock-required audit writer; return True only when appended."""
    entries, _issues = _read_decisions_unlocked(cfg, repair_tail=True)
    if cand_id and any(
            e.get("action") == action and e.get("id") == cand_id
            for e in entries):
        return False
    wikifs.append_jsonl(Path(cfg.wiki_root) / schema.DECISIONS_FILE, {
        "ts": wikifs.utc_iso(), "action": action, "id": cand_id,
        "target": target, "session": session, "actor": actor,
        "hash": chash, "detail": detail or {}})
    return True


def _read_decisions_unlocked(cfg, repair_tail=False):
    """Read valid audit objects and optionally repair an unterminated tail."""
    path = Path(cfg.wiki_root) / schema.DECISIONS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], []
    entries, issues = [], []
    lines = raw.splitlines(keepends=True)
    offset = 0
    for line_no, line in enumerate(lines, 1):
        line_start = offset
        offset += len(line)
        terminated = line.endswith(b"\n")
        payload = line.rstrip(b"\r\n")
        try:
            entry = json.loads(payload.decode("utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("audit entry is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if repair_tail and line_no == len(lines) and not terminated:
                _truncate_fsync(path, line_start)
                issues.append(f"discarded torn audit tail at line {line_no}")
                break
            issues.append(f"invalid audit line {line_no}")
        else:
            entries.append(entry)
            if repair_tail and not terminated:
                _append_newline_fsync(path)
    return entries, issues


def _truncate_fsync(path, size):
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, size)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_newline_fsync(path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        if os.write(fd, b"\n") != 1:
            raise OSError("short write terminating JSONL")
        os.fsync(fd)
    finally:
        os.close(fd)


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
    result = None
    with wikifs.wiki_lock(cfg.wiki_root):
        entries, _issues = _read_decisions_unlocked(
            cfg, repair_tail=True)
        allocated_ids = {str(e.get("id") or "") for e in entries
                         if e.get("id")}
        duplicate_id = _active_hash_duplicate_unlocked(inbox, chash)
        if duplicate_id:
            return {"staged": False,
                    "reason": "duplicate of an existing candidate",
                    "hash": chash}
        for seq in range(1, 100):
            cand_id = f"{stamp}-{s8}-{seq:02d}"
            if cand_id in allocated_ids \
                    or any(inbox.glob(f"{cand_id}-*.md")):
                continue
            fm["id"] = cand_id
            path = inbox / f"{cand_id}-{slug}.md"
            if not wikifs.create_exclusive(
                    path, wikifs.render_page(fm, body)):
                continue
            record_unlocked(
                cfg, "staged", cand_id=cand_id, target=target,
                session=session_id, actor=actor, chash=chash,
                detail={"kind": kind, "origin": origin,
                        "confidence": confidence})
            result = {"staged": True, "id": cand_id,
                      "ref": f"{schema.INBOX_DIR}/{path.name}"}
            break
    if result is None:
        return {"staged": False, "reason": "could not allocate candidate id"}
    if index is not None:
        try:
            index.refresh()
        except OSError:
            pass
    return result


def _active_hash_duplicate_unlocked(inbox, chash, exclude_id=""):
    """Other active candidate id with this hash; lock required."""
    for path in sorted(inbox.glob("*.md")):
        text = wikifs.read_text(path)
        fm, _body = wikifs.parse_frontmatter(text or "")
        cand_id = str((fm or {}).get("id") or "")
        if fm and cand_id != exclude_id and fm.get("hash") == chash \
                and fm.get("status") in _ACTIVE_HASH_STATUSES:
            return cand_id or path.name
    return ""


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
    """Advisory semantic/profile duplicate reason.

    Candidate hashes are deliberately excluded: their statuses are checked
    authoritatively from disk while holding the wiki lock.
    """
    if index is None:
        return ""
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
        turn = fm.get("turn", 0)
        try:
            turn = int(turn or 0)
        except (TypeError, ValueError):
            turn = 0
        out.append({"id": fm.get("id", ""), "status": fm.get("status", ""),
                    "kind": fm.get("kind", ""),
                    "confidence": fm.get("confidence", ""),
                    "target": fm.get("target", ""),
                    "section": fm.get("section", ""),
                    "origin": fm.get("origin", ""),
                    "session": fm.get("session", ""),
                    "turn": turn,
                    "created": fm.get("created", ""),
                    "supersedes": fm.get("supersedes", ""),
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
            rel = f"{schema.INBOX_DIR}/{path.name}"
            record_unlocked(
                cfg, "pruned", cand_id=fm.get("id", ""), actor="cli",
                detail={"status": fm.get("status", ""), "ref": rel})
            path.unlink()
            removed.append(rel)
    return removed


# -- crash recovery -----------------------------------------------------------

def recover(cfg):
    """Converge candidate files, page markers, and lifecycle audit records.

    Page markers are the commit evidence for promotion. Conflicting or
    missing evidence is reported in ``ambiguous`` and never guessed.
    """
    report = {"repaired": [], "ambiguous": []}
    with wikifs.wiki_lock(cfg.wiki_root):
        entries, audit_issues = _read_decisions_unlocked(
            cfg, repair_tail=True)
        for issue in audit_issues:
            bucket = "repaired" if issue.startswith("discarded torn") \
                else "ambiguous"
            report[bucket].append(issue)
        items = _candidate_items_unlocked(cfg, report)
        _finish_prunes_unlocked(entries, items, report)
        marker_targets = _marker_targets_unlocked(cfg)
        proven = _repair_promotions_unlocked(
            cfg, items, marker_targets, report)
        superseded_by = _repair_superseded_unlocked(
            cfg, items, proven, report)
        _report_unproven_promotions(items, proven, report)
        if not any(i.startswith("invalid audit line")
                   for i in audit_issues):
            _repair_audit_unlocked(cfg, items, superseded_by, report)
    return report


def _candidate_items_unlocked(cfg, report):
    items, duplicates = {}, set()
    inbox = Path(cfg.wiki_root) / schema.INBOX_DIR
    for path in sorted(inbox.glob("*.md")):
        text = wikifs.read_text(path)
        fm, body = wikifs.parse_frontmatter(text or "")
        cand_id = str((fm or {}).get("id") or "")
        if not fm or not cand_id:
            report["ambiguous"].append(
                f"unreadable candidate {schema.INBOX_DIR}/{path.name}")
            continue
        if cand_id in duplicates:
            continue
        if cand_id in items:
            report["ambiguous"].append(
                f"multiple candidate files for {cand_id}")
            duplicates.add(cand_id)
            del items[cand_id]
            continue
        content, evidence = split_evidence(body)
        items[cand_id] = [path, fm, content, evidence]
    return items


def _finish_prunes_unlocked(entries, items, report):
    intents = {str(e.get("id") or ""): e for e in entries
               if e.get("action") == "pruned" and e.get("id")}
    for cand_id, event in sorted(intents.items()):
        item = items.get(cand_id)
        if item is None:
            continue
        path, fm, _content, _evidence = item
        status = str(fm.get("status") or "")
        intended = str((event.get("detail") or {}).get("status") or "")
        if status not in ("promoted", "rejected") \
                or (intended and intended != status):
            report["ambiguous"].append(
                f"prune intent for {cand_id} conflicts with status {status}")
            continue
        path.unlink()
        del items[cand_id]
        report["repaired"].append(f"completed prune {cand_id}")


def _marker_targets_unlocked(cfg):
    root = Path(cfg.wiki_root)
    targets = {}
    for directory in schema.DIR_TYPE:
        page_dir = root / directory
        if not page_dir.is_dir():
            continue
        for path in sorted(page_dir.glob("*.md")):
            text = wikifs.read_text(path) or ""
            rel = f"{directory}/{path.name}"
            lines = text.splitlines()
            for line in lines:
                if not line.startswith("- "):
                    continue
                match = _FINAL_BULLET_MARKER.search(line)
                if match:
                    targets.setdefault(match.group(1), []).append(rel)
            promoted = [
                match for line in lines
                if (match := _PROMOTED_MARKER.fullmatch(line.strip()))
            ]
            if promoted:
                targets.setdefault(
                    promoted[-1].group(1), []).append(rel)
    return targets


def _repair_promotions_unlocked(cfg, items, marker_targets, report):
    proven = {}
    for cand_id, item in sorted(items.items()):
        path, fm, content, evidence = item
        targets = marker_targets.get(cand_id, [])
        status = str(fm.get("status") or "")
        if len(targets) > 1:
            report["ambiguous"].append(
                f"candidate {cand_id} has {len(targets)} page markers")
            continue
        if not targets:
            continue
        target = targets[0]
        if status in ("pending", "approved"):
            new_fm = set_status_unlocked(
                cfg, path, fm, content, evidence, "promoted",
                reviewed_note=fm.get("reviewed")
                or f"{wikifs.today()} approved",
                promoted_to=target)
            item[1] = new_fm
            proven[cand_id] = target
            report["repaired"].append(
                f"marked {cand_id} promoted to {target}")
        elif status == "promoted":
            current = str(fm.get("promoted_to") or "")
            if current and current != target:
                report["ambiguous"].append(
                    f"candidate {cand_id} says {current} but marker is {target}")
                continue
            if not current:
                new_fm = set_status_unlocked(
                    cfg, path, fm, content, evidence, "promoted",
                    reviewed_note=fm.get("reviewed") or "",
                    promoted_to=target)
                item[1] = new_fm
                report["repaired"].append(
                    f"restored promoted target for {cand_id}")
            proven[cand_id] = target
        else:
            report["ambiguous"].append(
                f"page marker for {cand_id} conflicts with status {status}")
    return proven


def _repair_superseded_unlocked(cfg, items, proven, report):
    relations = {}
    for new_id, target in proven.items():
        old_id = str(items[new_id][1].get("supersedes") or "")
        if old_id:
            relations.setdefault(old_id, []).append((new_id, target))
    resolved = {}
    for old_id, replacements in sorted(relations.items()):
        if len(replacements) != 1:
            report["ambiguous"].append(
                f"candidate {old_id} has multiple promoted replacements")
            continue
        item = items.get(old_id)
        if item is None:
            report["ambiguous"].append(
                f"replacement relation references missing {old_id}")
            continue
        new_id, _target = replacements[0]
        path, fm, content, evidence = item
        status = str(fm.get("status") or "")
        relation = str(fm.get("superseded_by") or "")
        if status == "promoted":
            new_fm = set_status_unlocked(
                cfg, path, fm, content, evidence, "superseded",
                reviewed_note=f"{wikifs.today()} superseded by {new_id}",
                overrides={"superseded_by": new_id})
            item[1] = new_fm
            report["repaired"].append(
                f"marked {old_id} superseded by {new_id}")
        elif status == "superseded":
            if relation and relation != new_id:
                report["ambiguous"].append(
                    f"candidate {old_id} has a conflicting superseded relation")
                continue
            if not relation:
                new_fm = set_status_unlocked(
                    cfg, path, fm, content, evidence, "superseded",
                    reviewed_note=fm.get("reviewed") or
                    f"{wikifs.today()} superseded by {new_id}",
                    overrides={"superseded_by": new_id})
                item[1] = new_fm
                report["repaired"].append(
                    f"restored superseded relation for {old_id}")
        else:
            report["ambiguous"].append(
                f"replacement for {old_id} conflicts with status {status}")
            continue
        resolved[old_id] = new_id
    return resolved


def _report_unproven_promotions(items, proven, report):
    for cand_id, (_path, fm, _content, _evidence) in sorted(items.items()):
        if fm.get("status") == "promoted" and cand_id not in proven:
            report["ambiguous"].append(
                f"promoted candidate {cand_id} has no unambiguous page marker")


def _repair_audit_unlocked(cfg, items, superseded_by, report):
    entries, _issues = _read_decisions_unlocked(cfg, repair_tail=True)
    keys = {(str(e.get("action") or ""), str(e.get("id") or ""))
            for e in entries if e.get("id")}
    for cand_id, (_path, fm, _content, _evidence) in sorted(items.items()):
        for action in _expected_actions(str(fm.get("status") or "")):
            key = (action, cand_id)
            if key in keys:
                continue
            detail = {"recovered": True}
            if action == "staged":
                detail.update({
                    "kind": fm.get("kind", ""),
                    "origin": fm.get("origin", ""),
                    "confidence": fm.get("confidence", "")})
            elif action == "superseded" and cand_id in superseded_by:
                detail["superseded_by"] = superseded_by[cand_id]
            target = fm.get("promoted_to", "") if action == "promoted" \
                else fm.get("target", "")
            if record_unlocked(
                    cfg, action, cand_id=cand_id, target=target,
                    session=fm.get("session", ""), actor="recovery",
                    chash=fm.get("hash", ""), detail=detail):
                keys.add(key)
                report["repaired"].append(
                    f"restored {action} audit for {cand_id}")


def _expected_actions(status):
    if status == "pending":
        return ("staged",)
    if status == "approved":
        return ("staged", "approved")
    if status == "rejected":
        return ("staged", "rejected")
    if status == "promoted":
        return ("staged", "approved", "promoted")
    if status == "superseded":
        return ("staged", "approved", "promoted", "superseded")
    return ()
