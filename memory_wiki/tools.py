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
        "approve/reject/promote manage the review queue. approve promotes "
        "immediately unless and_promote=false."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["stage", "approve", "reject", "promote",
                                "write_profile", "append_journal"]},
            "id": {"type": "string",
                   "description": "Candidate id (approve/reject/promote)."},
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
    scored = search.score_records(provider.index, query, corpus=corpus,
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
    return ok(ref="status",
              wiki_root=str(provider.cfg.wiki_root),
              read_only=provider.read_only,
              pages={k: v for k, v in sorted(counts.items()) if k != "candidate"},
              candidates=by_status,
              prompt_block_chars=len(provider.system_prompt_block()),
              config={k: getattr(provider.cfg, k) for k in
                      ("auto_capture", "llm_extraction", "journal_episodes",
                       "prefetch_limit", "max_candidates_per_session")})


def _get_review_queue(provider):
    provider.index.refresh()
    items = []
    for rec in provider.index.pending_candidates():
        items.append({"id": rec.cand_id, "kind": rec.kind,
                      "confidence": rec.confidence,
                      "excerpt": rec.body_head[:120]})
    return ok(ref="review-queue", count=len(items), candidates=items)


def _get_decisions(provider):
    path = Path(provider.cfg.wiki_root) / schema.DECISIONS_FILE
    text = wikifs.read_text(path) or ""
    tail = text.strip().splitlines()[-20:]
    return ok(ref="decisions", count=len(tail), entries=tail)


# -- apply (wired up by the write path) ---------------------------------------

def _apply(provider, args):
    if provider.read_only:
        return err("this session is read-only (subagent/cron context); "
                   "writes are disabled")
    return err("wiki_apply is not wired up yet (write path lands in phase 3)")
