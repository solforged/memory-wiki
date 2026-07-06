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
            return {"ok": True, "result": "already_promoted",
                    "target": fm.get("promoted_to", "")}
        if fm.get("status") == "rejected":
            return {"ok": False, "error": f"{cand_id} was rejected; re-stage it"}
        content = " ".join(overrides.pop("content", "").split()) or content
        fm_overrides = {k: v for k, v in overrides.items()
                        if k in ("target", "section", "confidence", "topics")}
        if "fields" in overrides:
            fm_overrides["fields"] = {
                k: v for k, v in (overrides["fields"] or {}).items()
                if k in schema.FIELDS_ALLOWLIST}
        fm = candidates.set_status_unlocked(
            cfg, path, fm, content, evidence, "approved",
            reviewed_note=f"{wikifs.today()} approved",
            overrides=fm_overrides)
    candidates.record(cfg, "approved", cand_id=cand_id,
                      target=fm.get("target", ""), session=session_id,
                      actor=actor, chash=fm.get("hash", ""))
    if not and_promote:
        return {"ok": True, "result": "approved", "id": cand_id}
    return promote(cfg, index, cand_id, actor=actor, session_id=session_id)


def reject(cfg, cand_id, reason="", actor="tool", session_id=""):
    with wikifs.wiki_lock(cfg.wiki_root):
        loaded = candidates.load(cfg, cand_id)
        if loaded is None:
            return {"ok": False, "error": f"no candidate {cand_id}"}
        path, fm, content, evidence = loaded
        note = f"{wikifs.today()} rejected" + (f": {reason}" if reason else "")
        candidates.set_status_unlocked(cfg, path, fm, content, evidence,
                                       "rejected", reviewed_note=note)
    candidates.record(cfg, "rejected", cand_id=cand_id, actor=actor,
                      session=session_id, detail={"reason": reason})
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
            return {"ok": True, "result": "already_promoted",
                    "target": fm.get("promoted_to", "")}
        if status == "rejected":
            return {"ok": False, "error": f"{cand_id} was rejected"}
        route = _resolve(cfg, fm, content)
        if "error" in route:
            return {"ok": False, "error": route["error"]}
        try:
            if route["mode"] == "append":
                result = _promote_append(cfg, cand_id, content, route)
            else:
                result = _promote_new_page(cfg, cand_id, fm, content, route)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not result.get("ok"):
            return result
        candidates.set_status_unlocked(
            cfg, path, fm, content, evidence, "promoted",
            reviewed_note=fm.get("reviewed") or f"{wikifs.today()} approved",
            promoted_to=result["target"])
    candidates.record(cfg, "promoted", cand_id=cand_id,
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
    first = content.split(". ")[0].strip().rstrip(".")
    return first[:80]


# -- writers (wiki lock already held) ------------------------------------------

def _promote_append(cfg, cand_id, content, route):
    rel, section = route["target"], route.get("section") or "## Notes"
    marker = schema.candidate_marker(cand_id)
    existing = wikifs.read_text(Path(cfg.wiki_root) / rel) or ""
    if marker in existing:
        return {"ok": True, "target": rel, "result": "already_promoted"}
    if rel.startswith("profile/"):
        page = "user" if rel.endswith("user.md") else "agent"
        layers.write_profile_unlocked(cfg, page, content, section=section,
                                      marker=marker)
    elif route.get("journal") or rel.startswith("journal/"):
        rel = layers.append_journal_note_unlocked(cfg, content, marker=marker)
    else:
        _append_to_page(cfg, rel, section, f"- {content} {marker}")
    return {"ok": True, "target": rel}


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


def _new_page_body(cand_id, fm, content, ntype):
    parts = [content, ""]
    if ntype == "claim":
        created = fm.get("created") or wikifs.today()
        s8 = candidates.session8(fm.get("session", ""))
        parts += ["## Evidence",
                  f"- [journal](../journal/{created}.md) - captured in "
                  f"conversation, session {s8}",
                  "", "## Open questions", ""]
    parts.append(schema.promoted_comment(cand_id))
    return "\n".join(parts) + "\n"
