"""Optional LLM extraction via the host's PluginLlm. Flag-gated
(llm_extraction, default off), lazily imported, and fail-soft: any
error degrades to deterministic-only capture.

PluginLlm's docstring says plugins should receive it from the host
rather than constructing it, but memory providers get a stub context,
so direct construction (plugin_id only; trust policy loads from the
user's config) is the supported-in-practice path. If a future Hermes
passes a real ctx to memory register(), prefer that.
"""

from __future__ import annotations

import logging

from . import schema

logger = logging.getLogger(__name__)

_MAX_TURN_CHARS = 6000
_MAX_ITEMS = 5

_llm = None

INSTRUCTIONS = """\
Extract durable memories from this conversation turn: stable user
preferences, identity facts, decisions, corrections, or knowledge worth
keeping across sessions. Only include things that will still matter in
future conversations. Return an empty list when nothing qualifies
(most turns). Never extract secrets, credentials, or transient detail.
Each memory: content (one self-contained sentence), kind (one of
preference|fact|decision|correction|claim|concept|note), confidence
(speculative|plausible|likely|confident)."""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": list(schema.CANDIDATE_KINDS)},
                    "confidence": {"type": "string",
                                   "enum": list(schema.CONFIDENCE)},
                },
                "required": ["content", "kind"],
            },
        },
    },
    "required": ["memories"],
}


def _get_llm():
    global _llm
    if _llm is None:
        from agent.plugin_llm import PluginLlm
        _llm = PluginLlm(plugin_id="memory-wiki")
    return _llm


def extract_turn(cfg, user_content, assistant_content):
    """[{'content', 'kind', 'confidence'}, ...]; raises on any failure
    (the caller swallows and logs at debug)."""
    if not cfg.llm_extraction:
        return []
    turn = (f"User: {(user_content or '')[:_MAX_TURN_CHARS]}\n\n"
            f"Assistant: {(assistant_content or '')[:_MAX_TURN_CHARS]}")
    result = _get_llm().complete_structured(
        instructions=INSTRUCTIONS,
        input=[{"type": "text", "text": turn}],
        json_schema=JSON_SCHEMA,
        schema_name="memory_candidates",
        temperature=0,
        max_tokens=512,
        timeout=30,
        purpose="memory-wiki.extract",
    )
    parsed = getattr(result, "parsed", None)
    items = parsed.get("memories") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for item in items[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split())
        if not content:
            continue
        kind = item.get("kind")
        confidence = item.get("confidence")
        out.append({
            "content": content,
            "kind": kind if kind in schema.CANDIDATE_KINDS else "fact",
            "confidence": (confidence if confidence in schema.CONFIDENCE
                           else "plausible"),
        })
    return out
