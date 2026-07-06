import json
from pathlib import Path

import pytest

import memory_wiki
from memory_wiki import candidates, schema, wikifs


@pytest.fixture
def provider(fixture_wiki, tmp_path):
    p = memory_wiki.MemoryWikiProvider(overrides={"wiki_root": str(fixture_wiki)})
    p.initialize("8f3e9c12-session", hermes_home=str(tmp_path), platform="cli")
    return p


def pending(p):
    return candidates.list_candidates(p.cfg, statuses=("pending",))


def test_sync_turn_stages_preference(provider):
    provider.sync_turn("I prefer uv over pip for python projects.",
                       "Noted!", session_id="8f3e9c12-session")
    items = pending(provider)
    assert len(items) == 1
    assert items[0]["kind"] == "preference"
    assert "uv over pip" in items[0]["content"]
    path = Path(provider.cfg.wiki_root) / items[0]["ref"]
    fm, body = wikifs.parse_frontmatter(path.read_text())
    assert fm["origin"] == "sync_turn" and fm["turn"] == "1"
    assert '> [user, turn 1]' in body


def test_sync_turn_caps_per_turn_and_session(provider):
    provider.cfg.max_candidates_per_turn = 2
    chatty = ("I prefer tabs over spaces always. "
              "I always deploy on Friday evenings. "
              "I never use docker on my laptop machine.")
    provider.sync_turn(chatty, "ok", session_id="s")
    assert len(pending(provider)) == 2
    provider.cfg.max_candidates_per_session = 3
    provider.sync_turn("I usually work from the Dublin office these days.",
                       "ok", session_id="s")
    provider.sync_turn("I love profiling before optimizing anything at all.",
                       "ok", session_id="s")
    assert len(pending(provider)) == 3  # session cap reached
    assert provider.state.session_count == 3


def test_sync_turn_dedups_repeated_turn(provider):
    provider.sync_turn("I prefer uv over pip for python projects.", "ok",
                       session_id="s")
    provider.sync_turn("I prefer uv over pip for python projects.", "ok",
                       session_id="s")
    assert len(pending(provider)) == 1


def test_sync_turn_respects_flags(fixture_wiki, tmp_path):
    p = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "auto_capture": False})
    p.initialize("sess", hermes_home=str(tmp_path), platform="cli")
    p.sync_turn("I prefer uv over pip for everything.", "ok", session_id="s")
    assert pending(p) == []

    ro = memory_wiki.MemoryWikiProvider(overrides={"wiki_root": str(fixture_wiki)})
    ro.initialize("sub", hermes_home=str(tmp_path), platform="cli",
                  agent_context="subagent")
    ro.sync_turn("I prefer uv over pip for everything.", "ok", session_id="s")
    assert pending(ro) == []


def test_pre_compress_stages_unseen_and_reports(provider):
    provider.sync_turn("I prefer uv over pip for python projects.", "ok",
                       session_id="s")
    messages = [
        {"role": "user", "content": "I prefer uv over pip for python projects."},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": "We decided to ship on Thursday next week."},
    ]
    note = provider.on_pre_compress(messages)
    items = pending(provider)
    kinds = {i["kind"] for i in items}
    assert "decision" in kinds and len(items) == 2  # no double-stage of turn 1
    assert note.startswith("Durable notes already staged")
    assert "uv over pip" in note and len(note) <= 700


def test_pre_compress_empty_when_nothing(provider):
    assert provider.on_pre_compress([]) == ""


def test_session_end_writes_idempotent_episode(provider):
    messages = [
        {"role": "user", "content": "I prefer uv over pip these days."},
        {"role": "assistant", "content": "Understood, noting that."},
        {"role": "user", "content": "thanks, bye"},
        {"role": "assistant", "content": "See you tomorrow."},
    ]
    provider.on_session_end(messages)
    from memory_wiki import layers
    rel = layers.journal_rel()
    text = wikifs.read_text(Path(provider.cfg.wiki_root) / rel)
    assert text.count(schema.session_marker("8f3e9c12-session")) == 1
    assert "cli session 8f3e9c12" in text
    assert "- turns: 2" in text
    assert 'opened: "I prefer uv over pip' in text
    assert 'closed: "See you tomorrow."' in text
    assert schema.validate_page(rel, text) == []
    provider.on_session_end(messages)  # duplicate fire
    text2 = wikifs.read_text(Path(provider.cfg.wiki_root) / rel)
    assert text2.count(schema.session_marker("8f3e9c12-session")) == 1


def test_session_end_skips_trivial_and_flagged(provider):
    provider.on_session_end([{"role": "user", "content": "hi"}])
    provider.cfg.journal_episodes = False
    provider.on_session_end([
        {"role": "user", "content": "hello there my friend"},
        {"role": "assistant", "content": "hi"}])
    assert not (Path(provider.cfg.wiki_root) / "journal").glob("*.md") or \
        not list((Path(provider.cfg.wiki_root) / "journal").glob("*.md"))


def test_memory_write_mirror_add_and_replace(provider):
    before = provider.system_prompt_block()
    provider.on_memory_write("add", "user", "Prefers weekly summaries",
                             {"write_origin": "memory_tool"})
    text = wikifs.read_text(Path(provider.cfg.wiki_root) / "profile/user.md")
    assert "Prefers weekly summaries" in text
    assert "Prefers weekly summaries" in provider.system_prompt_block()
    assert provider.system_prompt_block() != before
    # duplicate add is skipped
    provider.on_memory_write("add", "user", "Prefers weekly summaries", {})
    assert text.count("Prefers weekly summaries") == 1
    # replace/remove only audit
    provider.on_memory_write("replace", "memory", "new text", {})
    decisions = wikifs.read_text(
        Path(provider.cfg.wiki_root) / schema.DECISIONS_FILE)
    actions = [json.loads(l)["action"] for l in decisions.strip().splitlines()]
    assert "memory_write_replace" in actions
    agent_before = wikifs.read_text(
        Path(provider.cfg.wiki_root) / "profile/agent.md")
    assert "new text" not in agent_before


def test_memory_write_memory_target_goes_to_agent(provider):
    provider.on_memory_write("add", "memory",
                             "Wiki pages use markdown links", {})
    text = wikifs.read_text(Path(provider.cfg.wiki_root) / "profile/agent.md")
    assert "Wiki pages use markdown links" in text


def test_session_switch_reset_and_rewind(provider):
    provider.sync_turn("I prefer uv over pip for python projects.", "ok",
                       session_id="s")
    assert provider.state.session_count == 1
    provider.prefetch("zettelkasten", session_id="s")
    provider.on_session_switch("new-session", reset=True)
    assert provider.session_id == "new-session"
    assert provider.state.session_count == 0
    assert provider._prefetch_cache == {}
    # resume (no reset) keeps counters
    provider.sync_turn("I always review PRs before merging them.", "ok",
                       session_id="new-session")
    count = provider.state.session_count
    provider.on_session_switch("resumed", reset=False, rewound=True)
    assert provider.state.session_count == count
    assert provider.state.processed == set()


def test_hooks_never_raise(provider, monkeypatch):
    from memory_wiki import hooks as hooks_mod
    monkeypatch.setattr(hooks_mod, "sync_turn",
                        lambda *a, **k: 1 / 0)
    provider.sync_turn("boom", "ok", session_id="s")  # swallowed, logged
    monkeypatch.setattr(hooks_mod, "on_pre_compress",
                        lambda *a, **k: 1 / 0)
    assert provider.on_pre_compress([]) == ""
