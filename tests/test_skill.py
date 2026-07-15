"""Contract tests for the plugin-owned memory-wiki curation skill."""

from pathlib import Path
import re


PLUGIN_ROOT = Path(__file__).parents[1] / "memory_wiki"
SKILL = PLUGIN_ROOT / "skills" / "research" / "memory-wiki" / "SKILL.md"
REFERENCE = SKILL.parent / "references" / "page-types.md"


def _frontmatter(text):
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    assert end > 4
    values = {}
    for line in text[4:end].splitlines():
        match = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"')
    return values, text[end + 5:]


def test_skill_frontmatter_and_reference_are_valid():
    text = SKILL.read_text(encoding="utf-8")
    fm, body = _frontmatter(text)

    assert fm["name"] == "memory-wiki"
    assert fm["description"] == "Curate and query the review-first memory wiki."
    assert len(fm["description"]) <= 60
    assert body.strip()
    assert len(text) <= 100_000
    assert REFERENCE.is_file()


def test_skill_preserves_memory_wiki_boundaries():
    text = SKILL.read_text(encoding="utf-8")

    required_phrases = (
        "This skill replaces the generic `llm-wiki` workflow",
        "Never write directly to typed knowledge pages during normal operation",
        "No self-approval by default",
        "Source first",
        "page-shaped or multi-line content against an existing target",
        "`reports/` is compiler-owned",
        "cite repo-relative paths",
    )
    for phrase in required_phrases:
        assert phrase in text


def test_page_type_reference_covers_runtime_schema():
    text = REFERENCE.read_text(encoding="utf-8")

    for folder in (
        "concepts/", "claims/", "sources/", "atlases/", "syntheses/",
        "refs/", "tastes/", "reports/", "profile/", "journal/",
    ):
        assert f"`{folder}`" in text
    for required in ("stance", "confidence", "maturity", "author", "medium", "status"):
        assert f"`{required}`" in text or f"{required}:" in text
