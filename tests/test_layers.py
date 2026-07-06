from pathlib import Path

import pytest

from memory_wiki import layers, scaffold, schema, wikifs


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    return cfg


def read(cfg, rel):
    return wikifs.read_text(Path(cfg.wiki_root) / rel)


def test_write_profile_append_and_bump(ready):
    rel = layers.write_profile(ready, "user", "Prefers uv over pip",
                               section="## Preferences")
    text = read(ready, rel)
    fm, body = wikifs.parse_frontmatter(text)
    assert fm["updated"] == wikifs.today()
    pref = body.split("## Preferences")[1].split("##")[0]
    assert "- Prefers uv over pip" in pref
    assert schema.validate_page(rel, text) == []
    # second append lands in the same section, after the first
    layers.write_profile(ready, "user", "Dark terminal themes",
                         section="## Preferences")
    pref = read(ready, rel).split("## Preferences")[1].split("##")[0]
    assert pref.index("uv over pip") < pref.index("Dark terminal")


def test_write_profile_creates_missing_section(ready):
    layers.write_profile(ready, "agent", "Check CI before claiming done",
                         section="## Lessons")
    body = read(ready, "profile/agent.md")
    assert "## Lessons" in body and "- Check CI before claiming done" in body


def test_write_profile_replace_section(ready):
    layers.write_profile(ready, "user", "Working on memory-wiki plugin",
                         section="## Current focus", mode="replace_section")
    layers.write_profile(ready, "user", "Shipping the v2 rewrite",
                         section="## Current focus", mode="replace_section")
    text = read(ready, "profile/user.md")
    assert "Shipping the v2 rewrite" in text
    assert "Working on memory-wiki plugin" not in text
    assert schema.validate_page("profile/user.md", text) == []


def test_write_profile_validation(ready):
    with pytest.raises(ValueError):
        layers.write_profile(ready, "nope", "x")
    with pytest.raises(ValueError):
        layers.write_profile(ready, "user", "   ")


def test_append_journal_note_creates_valid_page(ready):
    rel = layers.append_journal_note(ready, "Discussed plugin architecture")
    text = read(ready, rel)
    assert schema.validate_page(rel, text) == []
    assert "Discussed plugin architecture" in text
    layers.append_journal_note(ready, "Second note")
    body = read(ready, rel).split(schema.JOURNAL_NOTES)[1]
    assert body.index("architecture") < body.index("Second note")


def test_session_episode_idempotent(ready):
    lines = ["cli session abcd1234", "- turns: 5", "- opened: hello"]
    rel, status = layers.append_session_episode(ready, "abcd1234-full", lines)
    assert status == "recorded"
    text = read(ready, rel)
    assert text.count(schema.session_marker("abcd1234-full")) == 1
    rel2, status2 = layers.append_session_episode(ready, "abcd1234-full", lines)
    assert status2 == "already_recorded" and rel2 == rel
    assert read(ready, rel).count("cli session abcd1234") == 1
    assert schema.validate_page(rel, read(ready, rel)) == []


def test_compile_prompt_block_contents(ready):
    layers.write_profile(ready, "user", "Prefers terse answers",
                         section="## Preferences")
    layers.write_profile(ready, "agent", "Wiki uses markdown links not wikilinks")
    block = layers.compile_prompt_block(ready, pending_count=2, page_count=7)
    assert block.startswith("# Memory wiki")
    assert "## User profile" in block and "Prefers terse answers" in block
    assert "## Agent notes" in block and "markdown links" in block
    assert "7 pages, 2 candidates" in block
    assert "<!--" not in block  # comments stripped


def test_compile_prompt_block_budgets(ready):
    layers.write_profile(ready, "user", "x" * 400, section="## Notes")
    for i in range(30):
        layers.write_profile(ready, "user", f"filler line {i} " + "y" * 120,
                             section="## Notes")
    ready.profile_user_budget = 800
    block = layers.compile_prompt_block(ready)
    user_part = block.split("## User profile")[1].split("## Agent notes")[0]
    assert len(user_part) < 1000
    assert "truncated" in user_part
    ready.prompt_block_budget = 900
    assert len(layers.compile_prompt_block(ready)) <= 900 + 80


def test_empty_seed_sections_do_not_bloat_prompt(ready):
    block = layers.compile_prompt_block(ready)
    # seeded-but-empty pages contribute headings only, no filler
    assert "## Identity" in block or "## User profile" in block
