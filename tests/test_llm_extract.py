import sys
import types
from pathlib import Path

import pytest

import memory_wiki
from memory_wiki import candidates, llm_extract


class FakeResult:
    def __init__(self, parsed):
        self.parsed = parsed


def install_fake_plugin_llm(monkeypatch, parsed=None, boom=False):
    calls = []

    class FakePluginLlm:
        def __init__(self, *, plugin_id):
            assert plugin_id == "memory-wiki"

        def complete_structured(self, **kwargs):
            calls.append(kwargs)
            if boom:
                raise RuntimeError("provider offline")
            return FakeResult(parsed)

    mod = types.ModuleType("agent.plugin_llm")
    mod.PluginLlm = FakePluginLlm
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", mod)
    monkeypatch.setattr(llm_extract, "_llm", None)
    return calls


@pytest.fixture
def provider(fixture_wiki, tmp_path):
    p = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "llm_extraction": True})
    p.initialize("11aa22bb-sess", hermes_home=str(tmp_path), platform="cli")
    return p


def test_flag_off_never_touches_plugin_llm(fixture_wiki, tmp_path, monkeypatch):
    calls = install_fake_plugin_llm(monkeypatch, parsed={"memories": []})
    p = memory_wiki.MemoryWikiProvider(overrides={"wiki_root": str(fixture_wiki)})
    p.initialize("sess", hermes_home=str(tmp_path), platform="cli")
    p.sync_turn("Just a normal question about code.", "answer", session_id="s")
    assert calls == []


def test_llm_candidates_carry_origin_llm(provider, monkeypatch):
    calls = install_fake_plugin_llm(monkeypatch, parsed={"memories": [
        {"content": "Deploys are frozen every December.", "kind": "fact",
         "confidence": "likely"},
        {"content": "", "kind": "fact"},                      # dropped
        {"content": "Weird kind survives as fact.", "kind": "opinion"},
    ]})
    provider.sync_turn("Just chatting about release schedules.", "ok",
                       session_id="s")
    assert len(calls) == 1
    assert calls[0]["purpose"] == "memory-wiki.extract"
    staged = candidates.list_candidates(provider.cfg, statuses=("pending",))
    contents = {c["content"] for c in staged}
    assert "Deploys are frozen every December." in contents
    ref = next(c for c in staged
               if c["content"] == "Deploys are frozen every December.")
    from memory_wiki import wikifs
    fm, _ = wikifs.parse_frontmatter(
        (Path(provider.cfg.wiki_root) / ref["ref"]).read_text())
    assert fm["origin"] == "llm"
    weird = next(c for c in staged
                 if c["content"] == "Weird kind survives as fact.")
    assert weird["kind"] == "fact"


def test_broken_llm_degrades_to_deterministic(provider, monkeypatch):
    install_fake_plugin_llm(monkeypatch, boom=True)
    provider.sync_turn("I prefer uv over pip for python projects.", "ok",
                       session_id="s")
    staged = candidates.list_candidates(provider.cfg, statuses=("pending",))
    assert len(staged) == 1  # deterministic capture still landed
    assert "uv over pip" in staged[0]["content"]


def test_missing_plugin_llm_module_is_silent(provider, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.plugin_llm", None)
    monkeypatch.setattr(llm_extract, "_llm", None)
    provider.sync_turn("We decided to ship the plugin this weekend.", "ok",
                       session_id="s")
    staged = candidates.list_candidates(provider.cfg, statuses=("pending",))
    assert len(staged) == 1  # deterministic only, no crash
