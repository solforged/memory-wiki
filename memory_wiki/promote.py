"""Review actions: approve, reject, and promote candidates into pages.

Promotion is idempotent (candidate-id markers), validates the rendered
page against the schema before replacing anything, and records every
decision in _inbox/decisions.jsonl.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import candidates, layers, schema, wikifs

logger = logging.getLogger(__name__)

# kind -> (target rel, section) when the candidate has no explicit target
ROUTES = {
    "preference": ("profile/user.md", "## Preferences"),
    "fact": ("profile/user.md", "## Notes"),
    "correction": ("profile/user.md", "## Notes"),
    "note": ("profile/agent.md", "## Notes"),
    "decision": ("journal", schema.JOURNAL_NOTES),
    "claim": ("claims/", None),
    "concept": ("concepts/", None),
}


def approve(cfg, index, cand_id, overrides=None, and_promote=True,
            actor="tool", session_id=""):
    """Mark a candidate approved (optionally overriding content/target/
    fields), then promote unless and_promote=False."""
    overrides = dict(overrides or {})
    with wikifs.wiki_lock(cfg.wiki_root):
        loaded = candidates.load(cfg, cand_id)
        if loaded is None:
            return {"ok": False, "error": f"no candidate {cand_id}"}
        path, fm, content, evidence = loaded
        if fm.get("status") == "promoted":
            candidates.record_unlocked(
                cfg, "approved", cand_id=cand_id,
                target=fm.get("target", ""), session=session_id,
                actor=actor, chash=fm.get("hash", ""))
            candidates.record_unlocked(
                cfg, "promoted", cand_id=cand_id,
                target=fm.get("promoted_to", ""), session=session_id,
                actor=actor, chash=fm.get("hash", ""))
            return {"ok": True, "result": "already_promoted",
                    "target": fm.get("promoted_to", "")}
        if fm.get("status") == "rejected":
            return {"ok": False, "error": f"{cand_id} was rejected; re-stage it"}
        if fm.get("status") == "superseded":
            return {"ok": False, "error":
                    f"{cand_id} was superseded ({fm.get('reviewed', '')}); "
                    "approving it would re-append stale content"}
        content_override = str(overrides.pop("content", "") or "").strip()
        if content_override:
            content = content_override
        fm_overrides = {k: v for k, v in overrides.items()
                        if k in ("target", "section", "confidence", "topics")}
        if content_override:
            fm_overrides["hash"] = candidates.content_hash(content)
            duplicate_id = candidates._active_hash_duplicate_unlocked(
                Path(cfg.wiki_root) / schema.INBOX_DIR,
                fm_overrides["hash"], exclude_id=cand_id)
            if duplicate_id:
                return {"ok": False, "error":
                        f"content duplicates active candidate {duplicate_id}"}
        if "fields" in overrides:
            fm_overrides["fields"] = {
                k: v for k, v in (overrides["fields"] or {}).items()
                if k in schema.FIELDS_ALLOWLIST}
        fm = candidates.set_status_unlocked(
            cfg, path, fm, content, evidence, "approved",
            reviewed_note=f"{wikifs.today()} approved",
            overrides=fm_overrides)
        candidates.record_unlocked(
            cfg, "approved", cand_id=cand_id,
            target=fm.get("target", ""), session=session_id,
            actor=actor, chash=fm.get("hash", ""))
    if not and_promote:
        return {"ok": True, "result": "approved", "id": cand_id}
    return promote(cfg, index, cand_id, actor=actor, session_id=session_id)


def supersede(cfg, index, old_id, content, confidence="", actor="tool",
              session_id=""):
    """Replace an outdated promoted bullet with new content.

    Stages a replacement candidate pointed at the old candidate's target.
    Profile/journal targets (the direct-write zone) are replaced
    immediately; knowledge-page targets stay pending for human review,
    and promotion swaps the marked bullet in place.
    """
    content = " ".join((content or "").split())
    if not content:
        return {"ok": False, "error": "supersede needs content"}
    loaded = candidates.load(cfg, old_id)
    if loaded is None:
        return {"ok": False, "error": f"no candidate {old_id}"}
    _path, old_fm, old_content, _ev = loaded
    if candidates.page_shaped(old_content):
        return {"ok": False, "error":
                f"{old_id} was promoted as a full page; edit the page "
                "directly or stage a replacement page"}
    if old_fm.get("status") != "promoted":
        return {"ok": False, "error":
                f"{old_id} is {old_fm.get('status')}; supersede only applies "
                "to promoted candidates (approve pending ones with new "
                "content instead)"}
    target = old_fm.get("promoted_to", "")
    if not target:
        return {"ok": False, "error": f"{old_id} has no promoted_to target"}
    staged = candidates.stage(
        cfg, index, content, kind=old_fm.get("kind", "fact"), origin="tool",
        session_id=session_id, target=target,
        section=old_fm.get("section", ""),
        confidence=confidence or old_fm.get("confidence", "plausible"),
        evidence=[f"supersedes {old_id}"], actor=actor, supersedes=old_id)
    if not staged.get("staged"):
        return {"ok": False, "error": f"not staged: {staged.get('reason')}"}
    new_id = staged["id"]
    if target.split("/", 1)[0] not in ("profile", "journal"):
        return {"ok": True, "result": "pending_review", "id": new_id,
                "supersedes": old_id, "target": target,
                "note": "knowledge pages are review-first; approve after review"}
    result = promote(cfg, index, new_id, actor=actor, session_id=session_id)
    if not result.get("ok"):
        return result
    result["supersedes"] = old_id
    return result


def reject(cfg, cand_id, reason="", actor="tool", session_id=""):
    with wikifs.wiki_lock(cfg.wiki_root):
        loaded = candidates.load(cfg, cand_id)
        if loaded is None:
            return {"ok": False, "error": f"no candidate {cand_id}"}
        path, fm, content, evidence = loaded
        status = str(fm.get("status") or "")
        if status == "rejected":
            candidates.record_unlocked(
                cfg, "rejected", cand_id=cand_id, actor=actor,
                session=session_id, chash=fm.get("hash", ""),
                detail={"reason": reason})
            return {"ok": True, "result": "rejected", "id": cand_id}
        if status in ("promoted", "superseded"):
            return {"ok": False, "error":
                    f"{cand_id} is {status} and cannot be rejected"}
        note = f"{wikifs.today()} rejected" + (f": {reason}" if reason else "")
        fm = candidates.set_status_unlocked(
            cfg, path, fm, content, evidence, "rejected",
            reviewed_note=note)
        candidates.record_unlocked(
            cfg, "rejected", cand_id=cand_id, actor=actor,
            session=session_id, chash=fm.get("hash", ""),
            detail={"reason": reason})
    return {"ok": True, "result": "rejected", "id": cand_id}


def promote(cfg, index, cand_id, actor="tool", session_id=""):
    """Write an approved (or pending, when invoked directly) candidate
    into its target and mark it promoted."""
    with wikifs.wiki_lock(cfg.wiki_root):
        loaded = candidates.load(cfg, cand_id)
        if loaded is None:
            return {"ok": False, "error": f"no candidate {cand_id}"}
        path, fm, content, evidence = loaded
        status = fm.get("status", "")
        if status == "promoted":
            candidates.record_unlocked(
                cfg, "promoted", cand_id=cand_id,
                target=fm.get("promoted_to", ""), session=session_id,
                actor=actor, chash=fm.get("hash", ""))
            return {"ok": True, "result": "already_promoted",
                    "target": fm.get("promoted_to", "")}
        if status == "rejected":
            return {"ok": False, "error": f"{cand_id} was rejected"}
        if status == "superseded":
            return {"ok": False, "error": f"{cand_id} was superseded"}
        old_id = str(fm.get("supersedes") or "")
        if old_id:
            old = candidates.load(cfg, old_id)
            old_status = str(old[1].get("status") or "") if old else "missing"
            if old_status != "promoted":
                return {"ok": False, "error":
                        f"{old_id} is {old_status}; replacement already "
                        "claimed or no longer promotable"}
        route = _resolve(cfg, fm, content)
        if "error" in route:
            return {"ok": False, "error": route["error"]}
        try:
            if route["mode"] == "append":
                result = _promote_append(cfg, cand_id, content, route,
                                         supersedes=fm.get("supersedes", ""))
            else:
                result = _promote_new_page(cfg, cand_id, fm, content, route)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not result.get("ok"):
            return result
        fm = candidates.set_status_unlocked(
            cfg, path, fm, content, evidence, "promoted",
            reviewed_note=fm.get("reviewed") or f"{wikifs.today()} approved",
            promoted_to=result["target"])
        _retire_superseded(cfg, fm.get("supersedes", ""), cand_id,
                           actor=actor, session_id=session_id)
        if status == "pending":
            candidates.record_unlocked(
                cfg, "approved", cand_id=cand_id,
                target=fm.get("target", ""), session=session_id,
                actor=actor, chash=fm.get("hash", ""),
                detail={"implicit": True})
        candidates.record_unlocked(
            cfg, "promoted", cand_id=cand_id,
            target=result["target"], session=session_id,
            actor=actor, chash=fm.get("hash", ""),
            detail={"kind": fm.get("kind", ""),
                    "result": result.get("result", "promoted")})
    if index is not None:
        try:
            index.refresh()
        except OSError:
            pass
    return {"ok": True, "result": result.get("result", "promoted"),
            "id": cand_id, "target": result["target"]}


# -- routing --------------------------------------------------------------

def _resolve(cfg, fm, content):
    """{'mode': 'append', 'target': rel, 'section': s} or
    {'mode': 'new_page', 'ntype': t, 'target': rel} or {'error': ...}."""
    kind = fm.get("kind", "fact")
    target = (fm.get("target") or "").strip()
    section = (fm.get("section") or "").strip()
    if not target:
        routed = ROUTES.get(kind)
        if routed is None:
            return {"error": f"no default route for kind '{kind}'"}
        target, default_section = routed
        if target == "journal":
            return {"mode": "append", "target": layers.journal_rel(),
                    "section": default_section, "journal": True}
        if target.endswith("/"):
            title = fm.get("title") or _title_from(content)
            rel = target + wikifs.slugify(title) + ".md"
            return {"mode": "new_page", "target": rel, "title": title,
                    "ntype": schema.DIR_TYPE[target.rstrip('/')]}
        return {"mode": "append", "target": target,
                "section": section or default_section}
    # explicit target
    try:
        path = wikifs.safe_wiki_path(cfg.wiki_root, target)
    except ValueError as e:
        return {"error": str(e)}
    d = target.split("/", 1)[0]
    if d not in schema.DIR_TYPE:
        return {"error": f"target must be inside a type folder: {target}"}
    if path.exists():
        default = schema.JOURNAL_NOTES if d == "journal" else "## Notes"
        return {"mode": "append", "target": target,
                "section": section or default}
    if not target.endswith(".md"):
        return {"error": f"target does not exist: {target}"}
    return {"mode": "new_page", "target": target,
            "title": fm.get("title") or _title_from(content),
            "ntype": schema.DIR_TYPE[d]}


def _title_from(content):
    if candidates.page_shaped(content):
        page_fm, _ = wikifs.parse_frontmatter(content)
        title = str((page_fm or {}).get("title") or "").strip()
        if title:
            return title
    first = content.split(". ")[0].strip().rstrip(".")
    return first[:80]


# -- writers (wiki lock already held) ------------------------------------------

def _promote_append(cfg, cand_id, content, route, supersedes=""):
    rel, section = route["target"], route.get("section") or "## Notes"
    if candidates.page_shaped(content):
        raise ValueError(f"page-shaped content needs a new-page target, "
                         f"not {rel}; approve with a different target")
    if "\n" in content:
        raise ValueError(
            f"multi-line content cannot be appended to {rel} (entries are "
            "single-line bullets); retarget to a new page or approve with "
            "a one-line content override")
    marker = schema.candidate_marker(cand_id)
    existing = wikifs.read_text(Path(cfg.wiki_root) / rel) or ""
    if marker in existing:
        return {"ok": True, "target": rel, "result": "already_promoted"}
    if supersedes:
        prefix = f"{wikifs.local_hhmm()} " if rel.startswith("journal/") else ""
        entry = f"- {prefix}{content} {marker}"
        if _replace_marker_line(cfg, rel,
                                schema.candidate_marker(supersedes), entry):
            return {"ok": True, "target": rel, "result": "replaced"}
        # old bullet was hand-edited away; fall through and append instead
    if rel.startswith("profile/"):
        page = "user" if rel.endswith("user.md") else "agent"
        layers.write_profile_unlocked(cfg, page, content, section=section,
                                      marker=marker)
    elif route.get("journal") or rel.startswith("journal/"):
        rel = layers.append_journal_note_unlocked(cfg, content, marker=marker)
    else:
        _append_to_page(cfg, rel, section, f"- {content} {marker}")
    return {"ok": True, "target": rel}


def _replace_marker_line(cfg, rel, old_marker, entry):
    """Swap the bullet carrying old_marker for entry; False if absent.
    Caller holds the wiki lock."""
    path = Path(cfg.wiki_root) / rel
    text = wikifs.read_text(path)
    if text is None:
        return False
    fm, body = wikifs.parse_frontmatter(text)
    if fm is None or old_marker not in body:
        return False
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if old_marker in line:
            lines[i] = entry
            break
    if "updated" in fm:
        fm["updated"] = wikifs.today()
    new_text = wikifs.render_page(fm, "\n".join(lines) + "\n")
    errors = schema.validate_page(rel, new_text)
    if errors:
        raise ValueError("; ".join(errors))
    wikifs.atomic_write(path, new_text)
    return True


def _retire_superseded(cfg, old_id, new_id, actor="tool", session_id=""):
    """Mark the replaced candidate superseded; caller holds the wiki lock."""
    if not old_id:
        return
    loaded = candidates.load(cfg, old_id)
    if loaded is None:
        return
    path, fm, content, evidence = loaded
    if fm.get("status") == "superseded":
        if fm.get("superseded_by") == new_id:
            return
        raise ValueError(f"{old_id} was superseded by another candidate")
    candidates.set_status_unlocked(
        cfg, path, fm, content, evidence, "superseded",
        reviewed_note=f"{wikifs.today()} superseded by {new_id}",
        overrides={"superseded_by": new_id})
    candidates.record_unlocked(
        cfg, "superseded", cand_id=old_id, actor=actor,
        session=session_id, chash=fm.get("hash", ""),
        detail={"superseded_by": new_id})


def _append_to_page(cfg, rel, section, entry):
    path = Path(cfg.wiki_root) / rel
    text = wikifs.read_text(path)
    if text is None:
        raise ValueError(f"target vanished: {rel}")
    fm, body = wikifs.parse_frontmatter(text)
    if fm is None:
        raise ValueError(f"target has no frontmatter: {rel}")
    body = layers._append_under(body, section, entry)
    if "updated" in fm:
        fm["updated"] = wikifs.today()
    new_text = wikifs.render_page(fm, body)
    errors = schema.validate_page(rel, new_text)
    if errors:
        raise ValueError("; ".join(errors))
    wikifs.atomic_write(path, new_text)


def _promote_new_page(cfg, cand_id, fm, content, route):
    rel, ntype, title = route["target"], route["ntype"], route["title"]
    path = Path(cfg.wiki_root) / rel
    existing = wikifs.read_text(path)
    if existing is not None:
        if schema.candidate_marker(cand_id) in existing \
                or schema.promoted_comment(cand_id) in existing:
            return {"ok": True, "target": rel, "result": "already_promoted"}
        return {"ok": False, "error":
                f"{rel} already exists; approve with a different target "
                "(never suffix filenames)"}
    if candidates.page_shaped(content):
        return _promote_page_shaped(cfg, cand_id, content, rel, path)
    fields = dict(fm.get("fields") or {})
    if ntype == "source":
        missing = [k for k in ("author", "medium") if not fields.get(k)]
        if missing:
            return {"ok": False, "error":
                    f"source pages need fields {missing}; approve with "
                    "fields={'author': ..., 'medium': ...}"}
    if ntype == "claim":
        fields.setdefault("confidence", fm.get("confidence", "plausible"))
    page_fm = schema.page_frontmatter(ntype, title,
                                      topics=fm.get("topics") or [],
                                      extras=fields)
    body = _new_page_body(cand_id, fm, content, ntype)
    text = wikifs.render_page(page_fm, body)
    errors = schema.validate_page(rel, text)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    wikifs.atomic_write(path, text)
    return {"ok": True, "target": rel}


def _promote_page_shaped(cfg, cand_id, content, rel, path):
    """Write a staged full page verbatim (its own frontmatter wins over
    candidate metadata), plus the promoted marker."""
    page_fm, page_body = wikifs.parse_frontmatter(content)
    if page_fm is None:
        return {"ok": False, "error": "page-shaped content has unparsable "
                                      "frontmatter"}
    body = page_body.strip() + "\n\n" + schema.promoted_comment(cand_id) + "\n"
    text = wikifs.render_page(page_fm, body)
    errors = schema.validate_page(rel, text)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    wikifs.atomic_write(path, text)
    return {"ok": True, "target": rel}


def _new_page_body(cand_id, fm, content, ntype):
    parts = [content, ""]
    if ntype == "claim":
        created = fm.get("created") or wikifs.today()
        s8 = candidates.session8(fm.get("session", ""))
        parts += ["## Evidence",
                  f"- [[journal/{created}|journal]] - captured in "
                  f"conversation, session {s8}",
                  "", "## Open questions", ""]
    parts.append(schema.promoted_comment(cand_id))
    return "\n".join(parts) + "\n"
