"""Wiki schema: the wiki's own conventions plus the memory extensions.

Mirrors ``_scripts/check.py`` (the wiki's validator) and extends it
with the two memory types (profile, journal) and the candidate spec.
check.py remains the external authority; this module exists so pages
are validated *before* they are written.
"""

from __future__ import annotations

import re

from . import claims, wikifs

# -- page types (check.py mirror + memory extensions) ---------------------

DIR_TYPE = {
    "claims": "claim", "concepts": "concept", "sources": "source",
    "atlases": "atlas", "syntheses": "synthesis", "refs": "ref",
    "profile": "profile", "journal": "journal", "tastes": "taste",
    "reports": "report",
}
TYPE_DIR = {v: k for k, v in DIR_TYPE.items()}
KNOWLEDGE_TYPES = ("concept", "claim", "source", "atlas", "synthesis", "ref",
                   "taste", "report")
MEMORY_TYPES = ("profile", "journal")

REQUIRED = ["type", "title", "topics"]
EXTRA_REQUIRED = {
    "claim": ["stance", "confidence", "maturity"],
    "source": ["author", "medium"],
    "synthesis": ["status"],
}
ENUMS = {
    "stance": {"external", "candidate", "model"},
    "confidence": {"unassessed", "speculative", "plausible", "likely",
                   "confident"},
    "maturity": {"seedling", "budding", "evergreen"},
    "status": {"draft", "published"},
    "medium": {"book", "textbook", "paper", "course", "video", "post",
               "podcast", "game", "thread", "wiki"},
}
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.md$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# -- candidate spec --------------------------------------------------------

CONFIDENCE = ("speculative", "plausible", "likely", "confident")
CANDIDATE_STATUSES = ("pending", "approved", "rejected", "promoted", "superseded")
CANDIDATE_KINDS = ("preference", "fact", "decision", "correction",
                   "claim", "concept", "note")
CANDIDATE_ORIGINS = ("sync_turn", "pre_compress", "session_end",
                     "tool", "memory_write", "llm")
EVIDENCE_HEADING = "## Evidence"

# extra frontmatter keys allowed in candidate `fields` for new-page promotion
FIELDS_ALLOWLIST = ("author", "medium", "year", "url", "aliases", "ref",
                    "supports", "contradicts", "stance", "maturity", "status",
                    "claim_id")

INBOX_DIR = "_inbox"
DECISIONS_FILE = "_inbox/decisions.jsonl"

# -- markers ---------------------------------------------------------------

STYLE_GUIDE_BEGIN = "<!-- memory-wiki:begin -->"
STYLE_GUIDE_END = "<!-- memory-wiki:end -->"


def session_marker(session_id):
    return f"<!-- memory-wiki: session {session_id} -->"


def candidate_marker(candidate_id):
    return f"<!-- memory-wiki: {candidate_id} -->"


def promoted_comment(candidate_id):
    return f"<!-- memory-wiki: promoted {candidate_id} -->"


# -- journal / profile layout ----------------------------------------------

JOURNAL_NOTES = "## Notes"
JOURNAL_SESSIONS = "## Sessions"
PROFILE_PAGES = ("profile/user.md", "profile/agent.md")

_PROFILE_SEEDS = {
    "user.md": ("User profile", [
        "## Identity", "## Preferences", "## Current focus", "## Notes"]),
    "agent.md": ("Agent notes", [
        "## Working agreements", "## Ongoing threads", "## Notes"]),
}


def profile_seed(filename, created=None):
    """(frontmatter, body) for a fresh profile page."""
    created = created or wikifs.today()
    title, headings = _PROFILE_SEEDS[filename]
    fm = {"type": "profile", "title": title, "topics": ["profile"],
          "createdAt": created, "updated": created}
    body = "\n\n".join(headings) + "\n"
    return fm, body


def journal_frontmatter(date_str):
    return {"type": "journal", "title": date_str, "topics": ["journal"],
            "createdAt": date_str}


def journal_seed_body():
    return f"{JOURNAL_NOTES}\n\n{JOURNAL_SESSIONS}\n"


# -- page construction -------------------------------------------------------

def page_frontmatter(ntype, title, topics=None, created=None, extras=None):
    """Ordered frontmatter dict for a new page, house field order per type."""
    extras = dict(extras or {})
    created = created or wikifs.today()
    topics = list(topics or [])
    fm = {"type": ntype, "title": title}
    if ntype == "concept":
        if extras.get("aliases"):
            fm["aliases"] = extras["aliases"]
        fm["topics"] = topics
        if extras.get("ref"):
            fm["ref"] = extras["ref"]
    elif ntype == "claim":
        fm["topics"] = topics
        fm["stance"] = extras.get("stance", "candidate")
        fm["confidence"] = extras.get("confidence", "plausible")
        fm["maturity"] = extras.get("maturity", "seedling")
        fm["supports"] = extras.get("supports", [])
        fm["contradicts"] = extras.get("contradicts", [])
        fm["claims"] = extras.get("claims", [])
    elif ntype == "source":
        fm["author"] = extras.get("author", "")
        if extras.get("year"):
            fm["year"] = extras["year"]
        fm["medium"] = extras.get("medium", "")
        if extras.get("url"):
            fm["url"] = extras["url"]
        fm["topics"] = topics
    elif ntype == "ref":
        if extras.get("url"):
            fm["url"] = extras["url"]
        fm["topics"] = topics
    elif ntype == "synthesis":
        fm["status"] = extras.get("status", "draft")
        fm["claims"] = extras.get("claims", [])
        fm["topics"] = topics
    elif ntype == "report":
        fm["topics"] = topics
        fm["status"] = extras.get("status", "published")
    else:  # atlas, profile, journal, taste
        fm["topics"] = topics
    fm["createdAt"] = created
    if ntype == "profile":
        fm["updated"] = extras.get("updated", created)
    return fm


# -- validation --------------------------------------------------------------

def validate_page(relpath, text):
    """Schema errors for one page (check.py's rules, minus link checks)."""
    errors = []
    relpath = relpath.replace("\\", "/")
    d, _, rest = relpath.partition("/")
    ntype = DIR_TYPE.get(d)
    if ntype is None or not rest or "/" in rest:
        return [f"{relpath}: not directly inside a recognized type folder"]
    if not KEBAB.match(rest):
        errors.append(f"{relpath}: filename not kebab-case")
    fm, _body = wikifs.parse_frontmatter(text)
    if fm is None:
        errors.append(f"{relpath}: missing frontmatter")
        return errors
    for field in REQUIRED + EXTRA_REQUIRED.get(ntype, []):
        if field == "topics":
            continue  # warning-only in check.py
        if field not in fm or fm[field] in ("", []):
            errors.append(f"{relpath}: missing or empty field '{field}'")
    if not fm.get("createdAt") and not fm.get("created"):
        errors.append(f"{relpath}: missing or empty field 'createdAt'")
    if fm.get("type") != ntype:
        errors.append(f"{relpath}: type '{fm.get('type')}' but lives in {d}/ "
                      f"(expected '{ntype}')")
    created_at = fm.get("createdAt") or fm.get("created") or ""
    if isinstance(created_at, str) and created_at and not DATE.match(created_at):
        errors.append(
            f"{relpath}: createdAt '{created_at}' is not an ISO date")
    for field, allowed in ENUMS.items():
        val = fm.get(field)
        if isinstance(val, str) and val and val not in allowed:
            errors.append(f"{relpath}: {field} '{val}' not in {sorted(allowed)}")
    structured_claims = fm.get("claims")
    legacy_synthesis_refs = (
        ntype == "synthesis" and isinstance(structured_claims, list)
        and not any(isinstance(item, dict) for item in structured_claims))
    if "claims" in fm and not legacy_synthesis_refs:
        errors.extend(f"{relpath}: {error}"
                      for error in claims.validate_claims(structured_claims))
    return errors


def validate_candidate(fm):
    """Schema errors for a candidate file's frontmatter."""
    errors = []
    for field in ("id", "status", "kind", "confidence", "origin", "created"):
        if not fm.get(field):
            errors.append(f"missing or empty field '{field}'")
    checks = (("status", CANDIDATE_STATUSES), ("kind", CANDIDATE_KINDS),
              ("confidence", CONFIDENCE), ("origin", CANDIDATE_ORIGINS))
    for field, allowed in checks:
        val = fm.get(field)
        if val and val not in allowed:
            errors.append(f"{field} '{val}' not in {list(allowed)}")
    fields = fm.get("fields", {})
    if isinstance(fields, dict):
        for k in fields:
            if k not in FIELDS_ALLOWLIST:
                errors.append(f"fields key '{k}' not in allowlist {list(FIELDS_ALLOWLIST)}")
    return errors
