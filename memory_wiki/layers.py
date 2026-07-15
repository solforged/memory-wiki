"""Living pages: the profile pair and the daily journal.

profile/user.md and profile/agent.md replace USER.md/MEMORY.md; their
bodies are compiled (char-budgeted) into the provider's system prompt
block. journal/YYYY-MM-DD.md holds day notes and session episodes.
All read-modify-write here happens under the wiki lock.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import safety, schema, wikifs

logger = logging.getLogger(__name__)

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

INSTRUCTIONS = """\
# Memory wiki
Your persistent memory is a plaintext wiki at {root}. Day-to-day memory:
profile/user.md (who the user is) and profile/agent.md (your notes), both
injected below, plus journal/YYYY-MM-DD.md for episodic notes. Durable
knowledge lives in typed pages (concepts/ claims/ sources/ atlases/
syntheses/ refs/ tastes/).
Tools: wiki_search (cited recall), wiki_get (read a page, candidate,
`status`, or `review-queue`), wiki_apply (write).
Write policy: when the user states a preference, correction, or durable
fact, save it with wiki_apply write_profile; log day notes with
append_journal. Knowledge pages are never written directly: propose with
wiki_apply stage; staged candidates need human review, so never present
them as established facts. When a remembered fact turns out to be
outdated, wiki_apply supersede replaces it in place (id = the old
candidate id from the bullet's marker comment; open the page with
wiki_get to see markers). Search the wiki before answering questions
about the user's history, projects, or preferences."""


# -- prompt block ---------------------------------------------------------

def compile_prompt_block(cfg, pending_count=0, page_count=0,
                         oldest_pending_days=None):
    """Static system-prompt block: instructions + budgeted profile pages."""
    root = Path(cfg.wiki_root)
    parts = [INSTRUCTIONS.format(root=root)]
    for rel, heading, budget in (
            ("profile/user.md", "## User profile", cfg.profile_user_budget),
            ("profile/agent.md", "## Agent notes", cfg.profile_agent_budget)):
        body = _page_body(root / rel)
        if body:
            parts.append(f"{heading}\n{_truncate(body, budget, rel)}")
    status = (f"Wiki: {page_count} pages, {pending_count} candidates awaiting "
              "review or promotion.")
    if pending_count:
        oldest = (f", oldest {oldest_pending_days}d"
                  if oldest_pending_days else "")
        status += (f"\nReview nudge: {pending_count} candidate(s) waiting"
                   f"{oldest}. When the moment fits, remind the user to run "
                   "`hermes memory-wiki review` (or review them yourself with "
                   "wiki_get review-queue + wiki_apply approve/reject).")
    parts.append(status)
    block = "\n\n".join(parts)
    if len(block) > cfg.prompt_block_budget:
        block = _truncate(block, cfg.prompt_block_budget, "the wiki")
    return block


def _page_body(path):
    text = wikifs.read_text(path)
    if text is None:
        return ""
    _fm, body = wikifs.parse_frontmatter(text)
    body = _COMMENT.sub("", body)
    # drop empty seed sections so the prompt only carries real content
    lines = [l.rstrip() for l in body.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _truncate(text, budget, what):
    if len(text) <= budget:
        return text
    cut = text.rfind("\n## ", 0, budget)
    if cut <= 0:
        cut = text.rfind("\n", 0, budget)
    if cut <= 0:
        cut = budget
    return (text[:cut].rstrip()
            + f"\n[truncated - read the rest of {what} with wiki_get]")


# -- profile writes ---------------------------------------------------------

def write_profile(cfg, page, content, section=None, mode="append", marker=""):
    """Append to (or replace a section of) a profile page under the wiki
    lock. Returns the repo-relative path written."""
    with wikifs.wiki_lock(cfg.wiki_root):
        return write_profile_unlocked(cfg, page, content, section, mode, marker)


def write_profile_unlocked(cfg, page, content, section=None, mode="append",
                           marker=""):
    """write_profile core; the caller must already hold the wiki lock
    (flock is not re-entrant in-process)."""
    if page not in ("user", "agent"):
        raise ValueError("page must be 'user' or 'agent'")
    if mode not in ("append", "replace_section"):
        raise ValueError("mode must be 'append' or 'replace_section'")
    content = (content or "").strip()
    if not content:
        raise ValueError("content is empty")
    if safety.contains_sensitive_content(content):
        raise ValueError("content looks like credential material; not written")
    if section:
        wikifs.validate_section_heading(section)
        if safety.contains_sensitive_content(section):
            raise ValueError("section looks like credential material; not written")
    rel = f"profile/{page}.md"
    path = Path(cfg.wiki_root) / rel
    text = wikifs.read_text(path)
    if text is None:
        fm, body = schema.profile_seed(f"{page}.md")
    else:
        fm, body = wikifs.parse_frontmatter(text)
        if fm is None:
            fm, body = schema.profile_seed(f"{page}.md")[0], text
    if marker and marker in body:
        return rel
    direct_marker = marker.startswith("<!-- memory-wiki: write ")
    target = section or "## Notes"
    if mode == "append":
        entry = content if content.startswith("- ") else f"- {content}"
        if marker and not direct_marker:
            entry = f"{entry} {marker}"
        body = _append_under(body, target, entry)
    else:
        body = _replace_section(body, target, content)
    if marker and direct_marker:
        body = f"{marker}\n{body.lstrip()}"
    fm["updated"] = wikifs.today()
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return rel


def _append_under(body, heading, entry):
    """Insert entry at the end of a section, creating it if missing."""
    lines = body.splitlines()
    start = _find_heading(lines, heading)
    if start is None:
        out = body.rstrip("\n") + f"\n\n{heading}\n\n{entry}\n"
        return out.lstrip("\n") if not body.strip() else out
    end = _section_end(lines, start)
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, entry)
    return "\n".join(lines) + "\n"


def _replace_section(body, heading, content):
    lines = body.splitlines()
    start = _find_heading(lines, heading)
    if start is None:
        return _append_under(body, heading, content)
    end = _section_end(lines, start)
    new = lines[:start + 1] + ["", *content.splitlines(), ""] + lines[end:]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(new)).rstrip("\n") + "\n"


def _find_heading(lines, heading):
    for i, line in enumerate(lines):
        if line.strip() == heading:
            return i
    return None


def _section_end(lines, start):
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.startswith("#") and (len(s) - len(s.lstrip("#"))) <= level:
            return i
    return len(lines)


# -- journal ----------------------------------------------------------------

def journal_rel(date_str=None):
    return f"journal/{date_str or wikifs.today()}.md"


def append_journal_note(cfg, content, date_str=None, marker=""):
    """Timestamped bullet under ## Notes of today's journal page."""
    with wikifs.wiki_lock(cfg.wiki_root):
        return append_journal_note_unlocked(cfg, content, date_str, marker)


def append_journal_note_unlocked(cfg, content, date_str=None, marker=""):
    """append_journal_note core; caller must hold the wiki lock."""
    content = (content or "").strip()
    if not content:
        raise ValueError("content is empty")
    if safety.contains_sensitive_content(content):
        raise ValueError("content looks like credential material; not written")
    root = Path(cfg.wiki_root)
    if marker:
        for existing in sorted((root / "journal").glob("*.md")):
            if marker in (wikifs.read_text(existing) or ""):
                return f"journal/{existing.name}"
    rel = journal_rel(date_str)
    path = root / rel
    entry = f"- {wikifs.local_hhmm()} {content}"
    if marker:
        entry = f"{entry} {marker}"
    fm, body = _load_journal(path, rel)
    if marker and marker in body:
        return rel
    body = _append_under(body, schema.JOURNAL_NOTES, entry)
    wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return rel


def append_session_episode(cfg, session_id, episode_lines, date_str=None):
    """Idempotent session episode under ## Sessions (marker-guarded).

    Returns (rel, 'recorded' | 'already_recorded'). Checks today's and
    yesterday's page so a midnight-spanning session is not re-recorded.
    """
    marker = schema.session_marker(session_id)
    root = Path(cfg.wiki_root)
    rel = journal_rel(date_str)
    with wikifs.wiki_lock(cfg.wiki_root):
        for existing in sorted((root / "journal").glob("*.md"), reverse=True)[:2]:
            if marker in (wikifs.read_text(existing) or ""):
                return f"journal/{existing.name}", "already_recorded"
        path = root / rel
        fm, body = _load_journal(path, rel)
        block = "\n".join([f"### {wikifs.local_hhmm()} {episode_lines[0]}",
                           marker, *episode_lines[1:]])
        body = _append_under(body, schema.JOURNAL_SESSIONS, block)
        wikifs.atomic_write(path, wikifs.render_page(fm, body))
    return rel, "recorded"


def _load_journal(path, rel):
    date_str = rel.rsplit("/", 1)[1][:-3]
    text = wikifs.read_text(path)
    if text is None:
        return schema.journal_frontmatter(date_str), schema.journal_seed_body()
    fm, body = wikifs.parse_frontmatter(text)
    if fm is None:
        return schema.journal_frontmatter(date_str), text
    return fm, body
