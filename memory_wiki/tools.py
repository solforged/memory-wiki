"""The three model-facing tools: wiki_search, wiki_get, wiki_apply.

Schemas are bare {name, description, parameters} dicts; the memory
manager wraps them in the function-call envelope. Handlers return JSON
strings ({"ok": true, ...} or {"ok": false, "error": ...}).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import schema, search, wikifs

logger = logging.getLogger(__name__)

CANDIDATE_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8,8}-\d{2}$")
_MAX_GET_LINES = 400

WIKI_SEARCH_SCHEMA = {
    "name": "wiki_search",
    "description": (
        "Search the memory wiki (lexical, cited). Use before answering "
        "questions about the user's history, projects, preferences, or "
        "anything the wiki may already know. Results carry repo-relative "
        "refs to open with wiki_get."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "corpus": {
                "type": "string",
                "enum": ["pages", "profile", "journal", "candidates", "all"],
                "description": "pages = knowledge types only (default)."},
            "type": {
                "type": "string",
                "enum": list(schema.KNOWLEDGE_TYPES),
                "description": "Restrict to one page type (corpus=pages)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    },
}

WIKI_GET_SCHEMA = {
    "name": "wiki_get",
    "description": (
        "Read from the memory wiki: a repo-relative page path "
        "('claims/foo.md', 'journal/2026-07-05.md', '_inbox/<file>.md'), a "
        "candidate id, or the specials 'status' (provider health + counts), "
        "'review-queue' (pending candidates), 'decisions' (audit tail)."),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1,
                          "maximum": _MAX_GET_LINES},
        },
        "required": ["ref"],
    },
}

WIKI_APPLY_SCHEMA = {
    "name": "wiki_apply",
    "description": (
        "Write to the memory wiki. Direct writes (day-to-day memory): "
        "write_profile saves a durable user fact/preference or agent note; "
        "append_journal logs a timestamped note to today's journal. "
        "Review-first writes (knowledge base): stage proposes a candidate; "
        "approve/reject manage the review queue. approve promotes "
        "immediately unless and_promote=false (approve again later to "
        "promote). supersede replaces an outdated promoted memory (id = "
        "the old candidate) with new content, in place."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["stage", "approve", "reject", "supersede",
                                "write_profile", "append_journal"]},
            "id": {"type": "string",
                   "description": "Candidate id (approve/reject/"
                                  "supersede)."},
            "content": {"type": "string",
                        "description": "Text to stage/write/append."},
            "kind": {"type": "string", "enum": list(schema.CANDIDATE_KINDS),
                     "description": "stage: what the content is (default fact)."},
            "target": {"type": "string",
                       "description": "Repo-relative target page (stage/"
                                      "approve override; empty = route by kind)."},
            "section": {"type": "string",
                        "description": "'## Heading' to append under."},
            "confidence": {"type": "string", "enum": list(schema.CONFIDENCE)},
            "topics": {"type": "array", "items": {"type": "string"},
                       "description": "Topics for a new page on promotion."},
            "fields": {"type": "object",
                       "description": "Extra frontmatter for new-page "
                                      "promotion (author, medium, url, ...)."},
            "page": {"type": "string", "enum": ["user", "agent"],
                     "description": "write_profile: which profile page."},
            "mode": {"type": "string", "enum": ["append", "replace_section"]},
            "reason": {"type": "string", "description": "reject: why."},
            "and_promote": {"type": "boolean",
                            "description": "approve: promote now (default true)."},
        },
        "required": ["action"],
    },
}


def tool_schemas(read_only=False):
    if read_only:
        return [WIKI_SEARCH_SCHEMA, WIKI_GET_SCHEMA]
    return [WIKI_SEARCH_SCHEMA, WIKI_GET_SCHEMA, WIKI_APPLY_SCHEMA]


def ok(**kw):
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def err(msg, **kw):
    return json.dumps({"ok": False, "error": str(msg), **kw}, ensure_ascii=False)


def handle(provider, tool_name, args):
    args = args if isinstance(args, dict) else {}
    try:
        if tool_name == "wiki_search":
            return _search(provider, args)
        if tool_name == "wiki_get":
            return _get(provider, args)
        if tool_name == "wiki_apply":
            return _apply(provider, args)
        return err(f"unknown tool '{tool_name}'")
    except wikifs.LockTimeout as e:
        return err(f"wiki busy, retry: {e}")
    except ValueError as e:
        return err(e)
    except Exception as e:  # never crash the agent loop on a tool bug
        logger.exception("memory-wiki tool %s failed", tool_name)
        return err(f"{type(e).__name__}: {e}")


# -- search -----------------------------------------------------------------

def _search(provider, args):
    query = (args.get("query") or "").strip()
    if not query:
        return err("query is required")
    corpus = args.get("corpus") or "pages"
    limit = min(int(args.get("limit") or 5), 20)
    provider.index.refresh()
    scored = search.hybrid_scores(provider.index, provider.embed, query,
                                  corpus=corpus,
                                  type_filter=args.get("type"))
    results = [search.result_dict(rec, s, query) for s, rec in scored[:limit]]
    return ok(query=query, corpus=corpus, count=len(results), results=results)


# -- get ---------------------------------------------------------------------

def _get(provider, args):
    ref = (args.get("ref") or "").strip()
    if not ref:
        return err("ref is required")
    if ref == "status":
        return _get_status(provider)
    if ref == "review-queue":
        return _get_review_queue(provider)
    if ref == "decisions":
        return _get_decisions(provider)
    root = provider.cfg.wiki_root
    if CANDIDATE_ID.match(ref):
        matches = sorted(Path(root, schema.INBOX_DIR).glob(f"{ref}-*.md"))
        if not matches:
            return err(f"no candidate with id {ref}")
        ref = f"{schema.INBOX_DIR}/{matches[0].name}"
    if not ref.endswith(".md"):
        return err("ref must be a .md path, a candidate id, or one of "
                   "status | review-queue | decisions")
    path = wikifs.safe_wiki_path(root, ref)
    text = wikifs.read_text(path)
    if text is None:
        return err(f"not found: {ref}")
    lines = text.splitlines()
    start = max(int(args.get("start_line") or 1), 1)
    count = min(int(args.get("max_lines") or _MAX_GET_LINES), _MAX_GET_LINES)
    window = lines[start - 1:start - 1 + count]
    return ok(ref=ref, total_lines=len(lines), start_line=start,
              content="\n".join(window))


def _get_status(provider):
    provider.index.refresh()
    counts = provider.index.counts()
    by_status = {}
    for rec in provider.index.records("candidates"):
        by_status[rec.status or "?"] = by_status.get(rec.status or "?", 0) + 1
    embed_state = "off"
    if provider.embed is not None:
        embed_state = ("failed" if provider.embed.failed
                       else "ready" if provider.embed.ready else "loading")
    return ok(ref="status",
              wiki_root=str(provider.cfg.wiki_root),
              read_only=provider.read_only,
              embedding_state=embed_state,
              pages={k: v for k, v in sorted(counts.items()) if k != "candidate"},
              candidates=by_status,
              prompt_block_chars=len(provider.system_prompt_block()),
              config={k: getattr(provider.cfg, k) for k in
                      ("auto_capture", "llm_extraction", "journal_episodes",
                       "prefetch_limit", "max_candidates_per_session",
                       "candidate_stale_days")})


def _get_review_queue(provider):
    from . import candidates, hints
    provider.index.refresh()
    pending = candidates.list_candidates(provider.cfg, statuses=("pending",))
    items = []
    for c in pending:
        item = {"id": c["id"], "kind": c["kind"],
                "confidence": c["confidence"], "target": c["target"],
                "age_days": c["age_days"],
                "excerpt": " ".join(c["content"].split())[:120]}
        item.update(hints.candidate_hints(provider.index, c, pending))
        items.append(item)
    return ok(ref="review-queue", count=len(items), candidates=items)


def _get_decisions(provider):
    path = Path(provider.cfg.wiki_root) / schema.DECISIONS_FILE
    text = wikifs.read_text(path) or ""
    tail = text.strip().splitlines()[-20:]
    return ok(ref="decisions", count=len(tail), entries=tail)


# -- apply ---------------------------------------------------------------------

def _apply(provider, args):
    if provider.read_only:
        return err("this session is read-only (subagent/cron context); "
                   "writes are disabled")
    from . import candidates, layers, promote

    action = args.get("action") or ""
    cfg, session = provider.cfg, provider.session_id

    if action == "write_profile":
        rel = layers.write_profile(
            cfg, args.get("page") or "user", args.get("content"),
            section=args.get("section") or None,
            mode=args.get("mode") or "append")
        candidates.record(cfg, "write_profile", target=rel, session=session,
                          actor="tool",
                          chash=candidates.content_hash(args.get("content")))
        provider.recompile_prompt_block()
        return ok(action=action, ref=rel)

    if action == "append_journal":
        rel = layers.append_journal_note(cfg, args.get("content"))
        candidates.record(cfg, "append_journal", target=rel, session=session,
                          actor="tool")
        return ok(action=action, ref=rel)

    if action == "stage":
        provider.index.refresh()
        result = candidates.stage(
            cfg, provider.index, args.get("content"),
            kind=args.get("kind") or "fact", origin="tool",
            session_id=session, target=args.get("target") or "",
            section=args.get("section") or "",
            confidence=args.get("confidence") or "plausible",
            topics=args.get("topics"), fields=args.get("fields"),
            evidence=[f"proposed via wiki_apply, session "
                      f"{candidates.session8(session)}"])
        if not result.get("staged"):
            return err(f"not staged: {result.get('reason')}")
        return ok(action=action, id=result["id"], ref=result["ref"],
                  note="pending human review")

    if action in ("approve", "reject", "supersede"):
        cand_id = (args.get("id") or "").strip()
        if not cand_id:
            return err(f"{action} needs a candidate id")
        if action == "supersede":
            provider.index.refresh()
            result = promote.supersede(
                cfg, provider.index, cand_id, args.get("content"),
                confidence=args.get("confidence") or "",
                actor="tool", session_id=session)
            if result.get("ok") and \
                    str(result.get("target", "")).startswith("profile/"):
                provider.recompile_prompt_block()
        elif action == "approve":
            overrides = {k: args[k] for k in
                         ("content", "target", "section", "confidence",
                          "topics", "fields") if args.get(k) is not None}
            result = promote.approve(
                cfg, provider.index, cand_id, overrides=overrides,
                and_promote=args.get("and_promote", True),
                actor="tool", session_id=session)
        else:
            result = promote.reject(cfg, cand_id,
                                    reason=args.get("reason") or "",
                                    actor="tool", session_id=session)
        if not result.get("ok"):
            return err(result.get("error", "failed"), action=action)
        if action != "supersede" and \
                str(result.get("target", "")).startswith("profile/"):
            provider.recompile_prompt_block()
        result.pop("ok", None)
        return ok(action=action, **result)

    return err(f"unknown action '{action}'")
