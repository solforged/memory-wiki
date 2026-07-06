"""Deterministic extraction: user sentences worth staging as candidates.

Pure functions, no I/O, no personal tokens. Heuristics err toward
missing things rather than flooding the inbox; the LLM extractor
(optional, flag-gated) covers the long tail.
"""

from __future__ import annotations

import re

_MAX_INPUT = 4000      # longer user turns are pastes, not statements
_MIN_SENTENCE = 15
_MAX_SENTENCE = 300

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# checked in order; first hit wins
PATTERNS = (
    ("correction", re.compile(
        r"^(?:actually|no[, ]|that'?s (?:wrong|not right)|correction[:, ]|"
        r"not quite|i meant)\b", re.IGNORECASE)),
    ("preference", re.compile(
        r"\bi (?:prefer|like|dislike|love|hate|always|never|usually|"
        r"want|need|use)\b", re.IGNORECASE)),
    ("decision", re.compile(
        r"\b(?:let'?s|we'?ll|we will|we decided|decided to|going to go with|"
        r"from now on|we're switching to|going forward)\b", re.IGNORECASE)),
    ("fact", re.compile(
        r"\b(?:my \w+ is|i am a|i'?m a|i work |i live |call me )\b",
        re.IGNORECASE)),
)


def extract(text):
    """[{'content': sentence, 'kind': kind}, ...] from one user turn."""
    text = text or ""
    if len(text) > _MAX_INPUT:
        return []
    text = _FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    out, seen = [], set()
    for raw in _SPLIT.split(text):
        sentence = " ".join(raw.split()).strip()
        if len(sentence) < _MIN_SENTENCE or sentence.endswith("?"):
            continue
        if sentence.startswith(("- ", "* ", "> ", "#")):
            sentence = sentence.lstrip("-*># ").strip()
            if len(sentence) < _MIN_SENTENCE:
                continue
        if len(sentence) > _MAX_SENTENCE:
            sentence = sentence[:_MAX_SENTENCE].rsplit(" ", 1)[0] + "..."
        key = sentence.lower()
        if key in seen:
            continue
        for kind, pattern in PATTERNS:
            if pattern.search(sentence):
                out.append({"content": sentence, "kind": kind})
                seen.add(key)
                break
    return out


def message_text(message):
    """Plain text of an OpenAI-style message whose content may be a
    string or a list of typed parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(p for p in parts if p)
    return ""


def excerpt(text, limit=200):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."
