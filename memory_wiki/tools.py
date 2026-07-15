"""The three model-facing tools: wiki_search, wiki_get, wiki_apply.

Schemas are bare {name, description, parameters} dicts; the memory
manager wraps them in the function-call envelope. Handlers return JSON
strings ({"ok": true, ...} or {"ok": false, "error": ...}).
"""

from __future__ import annotations

import json
import logging
import re
import hashlib
from pathlib import Path

from . import schema, search, wikifs

logger = logging.getLogger(__name__)

CANDIDATE_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8,8}-\d{2}$")
_MAX_GET_LINES = 400
_MAX_DIGEST_BYTES = 5_000_000

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
        "'review-queue' (pending and approved candidates), 'dream' "
        "(scored deep-curation plan), 'dream-state' (phase reinforcement), "
        "'compiler' (claim digest health), "
        "'decisions' (audit tail)."),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1,
                          "maximum": _MAX_GET_LINES},
            "kind": {
                "type": "array", "minItems": 1,
                "items": {"type": "string",
                          "enum": list(schema.CANDIDATE_KINDS)},
                "description": "review-queue only: restrict candidate kinds."},
            "confidence": {
                "type": "array", "minItems": 1,
                "items": {"type": "string", "enum": list(schema.CONFIDENCE)},
                "description": "review-queue only: restrict confidence levels."},
            "target": {
                "type": "array", "minItems": 1,
                "items": {"type": "string"},
                "description": "review-queue only: match explicit or effective targets."},
            "effective_target": {
                "type": "array", "minItems": 1,
                "items": {"type": "string"},
                "description": "review-queue only: match effective targets exactly."},
            "stale_only": {
                "type": "boolean",
                "description": "review-queue only: return stale candidates."},
            "min_age": {
                "type": "integer", "minimum": 0,
                "description": "review-queue only: minimum age in days."},
            "limit": {
                "type": "integer", "minimum": 1,
                "description": "review-queue only: maximum returned candidates."},
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
        "the old candidate) with new content, in place. dream returns a "
        "phase result and only deep/all can apply conservative profile/agent "
        "promotions."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["stage", "approve", "reject", "supersede",
                                "dream", "write_profile", "append_journal"]},
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
            "idempotency_key": {"type": "string",
                                "description": "Optional retry key for direct profile/journal writes."},
            "and_promote": {"type": "boolean",
                            "description": "approve: promote now (default true)."},
            "limit": {"type": "integer", "minimum": 1,
                      "description": "dream: max pending candidates to inspect."},
            "phase": {"type": "string",
                      "enum": ["light", "rem", "deep", "all"],
                      "description": "dream: phase to run (default deep)."},
            "apply_safe": {"type": "boolean",
                           "description": "dream: promote only recall-qualified profile/agent suggestions."},
            "write_report": {"type": "boolean",
                             "description": "dream: append the review plan to DREAMS.md."},
            "scheduled": {"type": "boolean",
                          "description": "dream: use stricter scheduled thresholds."},
            "compile": {"type": "boolean",
                        "description": "dream: compile claim artifacts afterward."},
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
    results = provider.search_results(
        query, corpus=corpus, type_filter=args.get("type"), limit=limit,
        source="wiki_search")
    return ok(query=query, corpus=corpus, backend=provider.last_search_backend,
              count=len(results), results=results)


# -- get ---------------------------------------------------------------------

def _get(provider, args):
    ref = (args.get("ref") or "").strip()
    if not ref:
        return err("ref is required")
    if ref == "status":
        return _get_status(provider)
    if ref == "review-queue":
        return _get_review_queue(provider, args)
    if ref == "dream":
        return _get_dream(provider)
    if ref == "dream-state":
        return _get_dream_state(provider)
    if ref == "compiler":
        return _get_compiler(provider)
    if ref == "decisions":
        return _get_decisions(provider)
    root = provider.cfg.wiki_root
    claim_match = None
    if CANDIDATE_ID.match(ref):
        matches = sorted(Path(root, schema.INBOX_DIR).glob(f"{ref}-*.md"))
        if matches:
            ref = f"{schema.INBOX_DIR}/{matches[0].name}"
        else:
            claim_match = _resolve_claim_ref(root, ref)
        if claim_match and claim_match.get("ambiguous"):
            return err(f"claim id {claim_match['id']} is ambiguous",
                       claim_id=claim_match["id"],
                       owners=claim_match["owners"],
                       match_count=claim_match.get("match_count", 0))
        if not matches and not claim_match:
            return err(f"no candidate with id {ref}")
        if claim_match:
            ref = claim_match["pagePath"]
    elif not ref.endswith(".md"):
        claim_match = _resolve_claim_ref(root, ref)
        if claim_match and claim_match.get("ambiguous"):
            return err(f"claim id {claim_match['id']} is ambiguous",
                       claim_id=claim_match["id"],
                       owners=claim_match["owners"],
                       match_count=claim_match.get("match_count", 0))
        if claim_match:
            ref = claim_match["pagePath"]
    if not ref.endswith(".md"):
        return err("ref must be a .md path, a candidate id, or one of "
                   "status | review-queue | dream | dream-state | compiler | "
                   "decisions")
    path = wikifs.safe_wiki_path(root, ref)
    text = wikifs.read_text(path)
    if text is None:
        return err(f"not found: {ref}")
    lines = text.splitlines()
    start = max(int(args.get("start_line") or 1), 1)
    count = min(int(args.get("max_lines") or _MAX_GET_LINES), _MAX_GET_LINES)
    window = lines[start - 1:start - 1 + count]
    payload = {
        "ref": ref,
        "total_lines": len(lines),
        "start_line": start,
        "content": "\n".join(window),
    }
    if claim_match:
        payload["claim_id"] = claim_match.get("id")
        payload["claim"] = claim_match
    return ok(**payload)


def _resolve_claim_ref(wiki_root, claim_id):
    """Resolve one structured claim id from the bounded compiler artifact."""
    from . import compiler

    claim_id = str(claim_id or "").strip()
    if not claim_id:
        return None
    path = Path(wiki_root) / compiler.CLAIMS_DIGEST_PATH
    try:
        if not path.is_file() or path.stat().st_size > _MAX_DIGEST_BYTES:
            return None
        matches = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and item.get("id") == claim_id \
                    and isinstance(item.get("pagePath"), str):
                matches.append(item)
        if not matches:
            return None
        matches.sort(key=lambda item: item["pagePath"])
        owners = sorted({item["pagePath"] for item in matches})
        if len(matches) > 1:
            return {"id": claim_id, "ambiguous": True, "owners": owners,
                    "match_count": len(matches)}
        return matches[0]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _get_status(provider):
    from . import dream_phases, scheduler

    provider.index.refresh()
    counts = provider.index.counts()
    by_status = {}
    for rec in provider.index.records("candidates"):
        by_status[rec.status or "?"] = by_status.get(rec.status or "?", 0) + 1
    embed_state = "off"
    if provider.embed is not None:
        embed_state = ("failed" if provider.embed.failed
                       else "ready" if provider.embed.ready else "loading")
    qmd_backend = getattr(provider, "qmd", None)
    qmd = qmd_backend.status() if qmd_backend is not None else {
        "enabled": False}
    compiler = _compiler_status(provider.cfg)
    dream_state = dream_phases.status(provider.cfg)
    schedule = scheduler.status(
        provider.cfg, hermes_home=getattr(provider, "_hermes_home", None))
    return ok(ref="status",
              wiki_root=str(provider.cfg.wiki_root),
              read_only=provider.read_only,
              embedding_state=embed_state,
              search_backend=str(getattr(provider.cfg, "search_backend", "local")),
              last_search_backend=provider.last_search_backend,
              qmd=qmd,
              compiler=compiler,
              dream=dream_state,
              schedule=schedule,
              pages={k: v for k, v in sorted(counts.items()) if k != "candidate"},
              candidates=by_status,
              prompt_block_chars=len(provider.system_prompt_block()),
              config={k: getattr(provider.cfg, k) for k in
                      ("auto_capture", "llm_extraction", "search_backend",
                       "auto_compile",
                       "journal_episodes",
                       "prefetch_limit", "max_candidates_per_session",
                       "candidate_stale_days", "dream_limit",
                       "dream_schedule_enabled", "dream_schedule")})


def _get_review_queue(provider, args=None):
    from . import hints
    provider.index.refresh()
    args = args or {}
    filters = {key: args[key] for key in
               ("kind", "confidence", "target", "effective_target",
                "stale_only", "min_age")
               if key in args}
    queue = hints.build_review_queue(
        provider.cfg, provider.index, filters=filters,
        limit=args.get("limit"))
    return ok(ref="review-queue", count=len(queue["candidates"]), **queue)


def _get_dream(provider):
    from . import dream
    provider.index.refresh()
    result = dream.run(provider.cfg, provider.index, apply_safe=False,
                       actor="tool", session_id=provider.session_id)
    return ok(ref="dream", **result)


def _get_dream_state(provider):
    from . import dream_phases
    return ok(ref="dream-state", **dream_phases.status(provider.cfg))


def _get_compiler(provider):
    return ok(ref="compiler", **_compiler_status(provider.cfg))


def _compiler_status(cfg):
    from . import compiler

    path = Path(cfg.wiki_root) / compiler.AGENT_DIGEST_PATH
    base = {
        "available": False,
        "path": compiler.AGENT_DIGEST_PATH,
        "claims_path": compiler.CLAIMS_DIGEST_PATH,
        "page_count": 0,
        "claim_count": 0,
        "page_counts": {},
        "claim_health": {},
        "contradiction_clusters": 0,
        "error": "",
    }
    try:
        if not path.is_file():
            return base
        if path.stat().st_size > _MAX_DIGEST_BYTES:
            return {**base, "error": "compiler digest exceeded size limit"}
        digest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(digest, dict):
            raise ValueError("compiler digest was not an object")
        page_counts = digest.get("pageCounts")
        page_counts = page_counts if isinstance(page_counts, dict) else {}
        clusters = digest.get("contradictionClusters")
        clusters = clusters if isinstance(clusters, list) else []
        health = digest.get("claimHealth")
        health = health if isinstance(health, dict) else {}
        return {
            **base,
            "available": True,
            "page_count": sum(
                value for value in page_counts.values()
                if isinstance(value, int) and not isinstance(value, bool)),
            "claim_count": max(0, int(digest.get("claimCount") or 0)),
            "page_counts": page_counts,
            "claim_health": health,
            "contradiction_clusters": len(clusters),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {**base, "error": f"{type(exc).__name__}: {exc}"}


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
        write_id, marker = _direct_write_identity(
            action, session, args, args.get("page") or "user")
        content_hash = candidates.content_hash(args.get("content"))
        payload_hash = _direct_payload_hash(args.get("content"))
        replayed = False
        with wikifs.wiki_lock(cfg.wiki_root):
            replay = _direct_write_replay_unlocked(
                candidates, cfg, action, write_id, content_hash, payload_hash)
            if replay["conflict"]:
                return err("idempotency key was already used with different content")
            if replay["target"]:
                rel, replayed = replay["target"], True
            else:
                rel = layers.write_profile_unlocked(
                    cfg, args.get("page") or "user", args.get("content"),
                    section=args.get("section") or None,
                    mode=args.get("mode") or "append", marker=marker)
                candidates.record_unlocked(
                    cfg, "write_profile", cand_id=write_id, target=rel,
                    session=session, actor="tool", chash=content_hash,
                    detail={"payload_hash": payload_hash})
        if not replayed:
            provider.on_wiki_mutation()
        return ok(action=action, ref=rel, write_id=write_id,
                  replayed=replayed)

    if action == "append_journal":
        write_id, marker = _direct_write_identity(action, session, args, "journal")
        content_hash = candidates.content_hash(args.get("content"))
        payload_hash = _direct_payload_hash(args.get("content"))
        replayed = False
        with wikifs.wiki_lock(cfg.wiki_root):
            replay = _direct_write_replay_unlocked(
                candidates, cfg, action, write_id, content_hash, payload_hash)
            if replay["conflict"]:
                return err("idempotency key was already used with different content")
            if replay["target"]:
                rel, replayed = replay["target"], True
            else:
                rel = layers.append_journal_note_unlocked(
                    cfg, args.get("content"), marker=marker)
                candidates.record_unlocked(
                    cfg, "append_journal", cand_id=write_id, target=rel,
                    session=session, actor="tool", chash=content_hash,
                    detail={"payload_hash": payload_hash})
        if not replayed:
            provider.on_wiki_mutation()
        return ok(action=action, ref=rel, write_id=write_id,
                  replayed=replayed)

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
        provider.on_wiki_mutation()
        return ok(action=action, id=result["id"], ref=result["ref"],
                  note="pending human review")

    if action == "dream":
        from . import dream, dream_phases
        provider.index.refresh()
        phase = str(args.get("phase") or "deep").strip().lower()
        if phase not in ("light", "rem", "deep", "all"):
            return err("dream phase must be light, rem, deep, or all")
        apply_safe = _bool_arg(args, "apply_safe", False)
        write_report = _bool_arg(args, "write_report", False)
        scheduled = _bool_arg(args, "scheduled", False)
        compile_requested = _bool_arg(args, "compile", False)
        if phase in ("light", "rem") and (apply_safe or write_report):
            return err("apply_safe and write_report require deep or all")
        if phase in ("light", "rem") and scheduled:
            return err("scheduled policy requires deep or all")

        limit = args.get("limit")
        if phase == "light":
            phases = dream_phases.run_tracked(
                cfg, ("light",),
                lambda _phase, _completed: dream_phases.run_light(
                    cfg, provider.index, limit=limit))
            result = phases["light"]
            deep_result = {}
        elif phase == "rem":
            def run_phase(name, completed):
                if name == "light":
                    return dream_phases.run_light(
                        cfg, provider.index, limit=limit)
                return dream_phases.run_rem(
                    cfg, completed["light"], limit=limit)

            phases = dream_phases.run_tracked(
                cfg, ("light", "rem"), run_phase)
            result = {"phase_order": ["light", "rem"],
                      "light": phases["light"], "rem": phases["rem"]}
            deep_result = {}
        elif phase == "all":
            result = dream.run_sweep(
                cfg, provider.index, limit=limit, apply_safe=apply_safe,
                write_report=write_report, actor="tool", session_id=session,
                scheduled=scheduled)
            deep_result = result.get("deep") or {}
        else:
            phases = dream_phases.run_tracked(
                cfg, ("deep",),
                lambda _phase, _completed: dream.run(
                    cfg, provider.index, limit=limit,
                    apply_safe=apply_safe, write_report=write_report,
                    actor="tool", session_id=session, scheduled=scheduled))
            result = phases["deep"]
            deep_result = result

        changed = any(
            item.get("ok") for item in deep_result.get("applied", []))
        if changed or write_report or compile_requested:
            provider.on_wiki_mutation(force_compile=compile_requested)
        payload = dict(result)
        payload.setdefault("phase", phase)
        return ok(action=action, **payload)

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
                and_promote=_bool_arg(args, "and_promote", True),
                actor="tool", session_id=session)
        else:
            result = promote.reject(cfg, cand_id,
                                    reason=args.get("reason") or "",
                                    actor="tool", session_id=session)
        if not result.get("ok"):
            return err(result.get("error", "failed"), action=action)
        provider.on_wiki_mutation()
        result.pop("ok", None)
        return ok(action=action, **result)

    return err(f"unknown action '{action}'")


def _bool_arg(args, name, default):
    if name not in args:
        return default
    value = args[name]
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _direct_write_identity(action, session_id, args, target):
    supplied = str(args.get("idempotency_key") or "").strip()
    section = str(args.get("section") or "").strip()
    mode = str(args.get("mode") or ("append" if action == "write_profile"
                                    else "")).strip()
    scope = "\0".join((action, str(target or "").strip(), section, mode))
    payload = (f"{scope}\0key\0{supplied}" if supplied else
               f"{scope}\0session\0{session_id or ''}\0"
               f"{str(args.get('content') or '').strip()}")
    token = hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:20]
    return f"write-{token}", f"<!-- memory-wiki: write {token} -->"


def _direct_payload_hash(content):
    return hashlib.sha256(str(content or "").strip().encode(
        "utf-8", "replace")).hexdigest()[:24]


def _direct_write_replay_unlocked(candidates_mod, cfg, action, write_id,
                                  content_hash, payload_hash):
    entries, _issues = candidates_mod._read_decisions_unlocked(
        cfg, repair_tail=True)
    for entry in reversed(entries):
        if entry.get("action") != action or entry.get("id") != write_id:
            continue
        detail = entry.get("detail") if isinstance(entry.get("detail"), dict) \
            else {}
        stored_payload = str(detail.get("payload_hash") or "")
        matches = (stored_payload == payload_hash if stored_payload
                   else entry.get("hash") == content_hash)
        return {"target": str(entry.get("target") or ""),
                "conflict": not matches}
    return {"target": "", "conflict": False}
