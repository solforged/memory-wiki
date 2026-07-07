"""Hook orchestration: auto-capture, compression notes, session episodes,
built-in memory mirroring, and session-switch bookkeeping.

Everything here is bounded work (regex + a handful of file creates);
the manager already runs sync_turn on a background worker, but nothing
in this module would hurt inline either.
"""

from __future__ import annotations

import hashlib
import logging

from . import candidates, extract, layers

logger = logging.getLogger(__name__)

_STAGED_NOTE_BUDGET = 600


class SessionState:
    """Per-session capture bookkeeping (reset on /reset)."""

    def __init__(self):
        self.turn = 0
        self.staged_ids = []
        self.staged_contents = []
        self.session_count = 0
        self.processed = set()      # sha1 of user contents already extracted


def _seen_key(text):
    return hashlib.sha1(" ".join(text.split()).encode("utf-8", "replace")
                        ).hexdigest()[:16]


def _capture_allowed(provider):
    return (provider.cfg is not None and not provider.read_only
            and provider.cfg.auto_capture and provider.index is not None)


def _stage_extracted(provider, text, origin, turn):
    """Extract from one user text and stage up to the configured caps.
    Returns the number staged."""
    cfg, state = provider.cfg, provider.state
    if state.session_count >= cfg.max_candidates_per_session:
        return 0
    key = _seen_key(text or "")
    if key in state.processed:
        return 0
    state.processed.add(key)
    staged = 0
    for item in extract.extract(text):
        if staged >= cfg.max_candidates_per_turn:
            break
        if state.session_count >= cfg.max_candidates_per_session:
            break
        result = candidates.stage(
            cfg, provider.index, item["content"], kind=item["kind"],
            origin=origin, session_id=provider.session_id, turn=turn,
            evidence=[f'> [user, turn {turn}] "{item["content"]}"'],
            actor="hook")
        if result.get("staged"):
            staged += 1
            state.session_count += 1
            state.staged_ids.append(result["id"])
            state.staged_contents.append(item["content"])
    return staged


# -- hook bodies ---------------------------------------------------------------

def sync_turn(provider, user_content, assistant_content, session_id, messages):
    if not _capture_allowed(provider):
        return
    state = provider.state
    state.turn += 1
    n = _stage_extracted(provider, user_content, "sync_turn", state.turn)
    if provider.cfg.llm_extraction:
        _llm_stage(provider, user_content, assistant_content, state.turn)
    if n:
        logger.info("memory-wiki: staged %d candidate(s) from turn %d",
                    n, state.turn)


def _llm_stage(provider, user_content, assistant_content, turn):
    try:
        from . import llm_extract
        items = llm_extract.extract_turn(provider.cfg, user_content,
                                         assistant_content)
    except Exception as e:  # fail-soft: LLM access is a soft contract
        logger.debug("memory-wiki: llm extraction unavailable: %s", e)
        return
    cfg, state = provider.cfg, provider.state
    for item in items:
        if state.session_count >= cfg.max_candidates_per_session:
            break
        evidence = [f'> [user, turn {turn}] "{extract.excerpt(user_content, 160)}"',
                    f"- proposed by llm extraction, turn {turn}"]
        if item.get("rationale"):
            evidence.append(f"- rationale: {item['rationale']}")
        result = candidates.stage(
            cfg, provider.index, item.get("content", ""),
            kind=item.get("kind", "fact"), origin="llm",
            session_id=provider.session_id, turn=turn,
            confidence=item.get("confidence", "plausible"),
            target=item.get("target", ""),
            topics=item.get("topics"), fields=item.get("fields"),
            evidence=evidence, actor="hook")
        if result.get("staged"):
            state.session_count += 1
            state.staged_ids.append(result["id"])
            state.staged_contents.append(item.get("content", ""))


def on_pre_compress(provider, messages):
    """Catch unprocessed user turns before they are compressed away,
    then tell the compressor what is already durably staged."""
    if _capture_allowed(provider):
        for message in messages or []:
            if message.get("role") != "user":
                continue
            text = extract.message_text(message)
            if text:
                _stage_extracted(provider, text, "pre_compress",
                                 provider.state.turn)
    contents = provider.state.staged_contents if provider.state else []
    if not contents:
        return ""
    lines = ["Durable notes already staged by memory-wiki (do not restate):"]
    used = len(lines[0])
    for c in contents:
        line = f"- {extract.excerpt(c, 120)}"
        if used + len(line) > _STAGED_NOTE_BUDGET:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def on_session_end(provider, messages):
    if provider.cfg is None or provider.read_only:
        return
    messages = [m for m in (messages or []) if isinstance(m, dict)]
    user_msgs = [m for m in messages if m.get("role") == "user"]
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    if len(messages) < 2 or not user_msgs:
        return
    if _capture_allowed(provider):
        for message in user_msgs:
            text = extract.message_text(message)
            if text:
                _stage_extracted(provider, text, "session_end",
                                 provider.state.turn)
    if not provider.cfg.journal_episodes:
        return
    state = provider.state
    s8 = candidates.session8(provider.session_id)
    lines = [f"{provider.platform or 'cli'} session {s8}"]
    staged = ", ".join(state.staged_ids[:4]) if state.staged_ids else "none"
    lines.append(f"- turns: {len(user_msgs)}; candidates staged: "
                 f"{len(state.staged_ids)} ({staged})")
    opened = extract.excerpt(extract.message_text(user_msgs[0]))
    if opened:
        lines.append(f'- opened: "{opened}"')
    if assistant_msgs:
        closed = extract.excerpt(extract.message_text(assistant_msgs[-1]))
        if closed:
            lines.append(f'- closed: "{closed}"')
    try:
        rel, status = layers.append_session_episode(
            provider.cfg, provider.session_id, lines)
        logger.info("memory-wiki: session episode %s (%s)", status, rel)
    except Exception as e:
        logger.warning("memory-wiki: episode write failed: %s", e)


def on_memory_write(provider, action, target, content, metadata):
    """Mirror of the built-in memory tool (only fires when the built-in
    store is left enabled alongside this provider)."""
    if provider.cfg is None or provider.read_only:
        return
    metadata = metadata or {}
    if action != "add":
        candidates.record(provider.cfg, f"memory_write_{action}",
                          target=target, session=provider.session_id,
                          actor="memory_write",
                          detail={"origin": metadata.get("write_origin", "")})
        return
    content = (content or "").strip()
    if not content:
        return
    chash = candidates.content_hash(content)
    provider.index.refresh()
    if candidates.duplicate_reason(provider.index, chash, content):
        return
    page = "user" if target == "user" else "agent"
    rel = layers.write_profile(provider.cfg, page, content)
    candidates.record(provider.cfg, "write_profile", target=rel,
                      session=provider.session_id, actor="memory_write",
                      chash=chash,
                      detail={"origin": metadata.get("write_origin", "")})
    provider.recompile_prompt_block()


def on_session_switch(provider, new_session_id, parent_session_id,
                      reset, rewound):
    provider.set_session_id(new_session_id)
    if reset:
        provider.state = SessionState()
        provider.clear_prefetch_cache()
    elif rewound:
        provider.clear_prefetch_cache()
        if provider.state:
            provider.state.processed.clear()
