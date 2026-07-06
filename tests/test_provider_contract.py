"""Round-trip through the real hermes MemoryManager (loaded from the
checkout, with its two non-stdlib collaborators stubbed) plus signature
drift checks against the real ABC."""

import importlib.util
import inspect
import json
import os
import sys
import types
from pathlib import Path

import pytest

import memory_wiki

HERMES_SRC = Path(os.environ.get("HERMES_AGENT_SRC",
                                 "/Users/admin/src/hermes-agent"))


def _load_memory_manager():
    mm_path = HERMES_SRC / "agent" / "memory_manager.py"
    if not mm_path.is_file():
        pytest.skip("no hermes checkout on this machine")
    if "agent.skill_commands" not in sys.modules:
        sc = types.ModuleType("agent.skill_commands")
        sc.extract_user_instruction_from_skill_message = lambda text: text
        sys.modules["agent.skill_commands"] = sc
    if "tools.registry" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []
        reg = types.ModuleType("tools.registry")
        reg.tool_error = lambda message, **extra: json.dumps(
            {"error": message, **extra})
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg
    if "toolsets" not in sys.modules:
        ts = types.ModuleType("toolsets")
        ts._HERMES_CORE_TOOLS = {"memory", "clarify", "delegate_task"}
        sys.modules["toolsets"] = ts
    if "agent.memory_manager" in sys.modules:
        return sys.modules["agent.memory_manager"]
    spec = importlib.util.spec_from_file_location("agent.memory_manager",
                                                  mm_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent.memory_manager"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_signatures_match_the_abc():
    from agent.memory_provider import MemoryProvider
    if not hasattr(MemoryProvider, "__abstractmethods__"):
        pytest.skip("stub ABC in use; no hermes checkout")
    hooks = ["initialize", "get_tool_schemas", "handle_tool_call",
             "system_prompt_block", "prefetch", "sync_turn",
             "on_session_end", "on_session_switch", "on_pre_compress",
             "on_memory_write", "get_config_schema", "save_config",
             "backup_paths", "shutdown"]
    for name in hooks:
        base = inspect.signature(getattr(MemoryProvider, name))
        ours = inspect.signature(getattr(memory_wiki.MemoryWikiProvider, name))
        base_params = list(base.parameters)
        ours_params = list(ours.parameters)
        # ours may add trailing **kwargs but must accept every base param
        for p in base_params:
            assert p in ours_params or any(
                ours.parameters[q].kind == inspect.Parameter.VAR_KEYWORD
                for q in ours_params), f"{name} missing param {p}"


def test_manager_round_trip(fixture_wiki, tmp_path):
    mm = _load_memory_manager()
    manager = mm.MemoryManager()
    provider = memory_wiki.MemoryWikiProvider(
        overrides={"wiki_root": str(fixture_wiki)})
    manager.add_provider(provider)  # returns None; registration is by side effect
    assert provider in manager._providers
    manager.initialize_all(session_id="rt123456-full", platform="cli",
                           hermes_home=str(tmp_path), agent_context="primary")

    # tool schemas surface through the manager
    names = [s.get("name") for s in manager.get_all_tool_schemas()]
    assert "wiki_search" in names and "wiki_apply" in names
    assert manager.has_tool("wiki_search")

    # prompt block through the manager
    block = manager.build_system_prompt()
    assert "Memory wiki" in block

    # prefetch through the manager (fenced by build_memory_context_block)
    ctx = manager.prefetch_all("tell me about zettelkasten",
                               session_id="rt123456-full")
    assert "zettelkasten" in ctx.lower()

    # tool dispatch through the manager
    result = json.loads(manager.handle_tool_call(
        "wiki_search", {"query": "zettelkasten"}))
    assert result["ok"]

    # sync_all runs on the background worker; wait for it to land
    manager.sync_all("I prefer uv over pip for everything python.",
                     "noted", session_id="rt123456-full",
                     messages=[{"role": "user", "content": "x"}])
    import time
    from memory_wiki import candidates
    deadline = time.time() + 5
    while time.time() < deadline:
        if candidates.list_candidates(provider.cfg, statuses=("pending",)):
            break
        time.sleep(0.05)
    staged = candidates.list_candidates(provider.cfg, statuses=("pending",))
    assert staged and "uv over pip" in staged[0]["content"]

    manager.on_session_end([
        {"role": "user", "content": "I prefer uv over pip these days."},
        {"role": "assistant", "content": "ok"}])
    from memory_wiki import layers
    jtext = (Path(fixture_wiki) / layers.journal_rel()).read_text()
    assert "session rt123456" in jtext

    manager.shutdown_all()
