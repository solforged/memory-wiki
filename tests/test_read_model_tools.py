import json
from pathlib import Path

import memory_wiki
from memory_wiki import candidates, compiler, schema, tools


def _provider(root, home):
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(root), "embeddings": False,
    })
    provider.initialize("read-model-tools", hermes_home=str(home),
                        platform="desktop")
    return provider


def test_wiki_get_exposes_read_only_cockpit_and_context_pack(
        fixture_wiki, tmp_path):
    provider = _provider(fixture_wiki, tmp_path)
    staged = candidates.stage(
        provider.cfg, provider.index,
        "Prefer exact live-path verification before declaring success.",
        kind="preference", origin="tool", session_id="session-secret")
    assert staged["staged"]
    decisions = Path(fixture_wiki) / schema.DECISIONS_FILE
    audit_before = decisions.read_bytes()

    review = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "review", "origin": ["tool"]}))
    maintenance = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "maintenance", "limit": 5}))
    history = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "history", "actions": ["staged"]}))
    context = json.loads(provider.handle_tool_call("wiki_get", {
        "ref": "context-pack", "query": "small tools frameworks",
        "include_profile": False,
    }))

    assert review["ok"] and review["items"][0]["bucket"]
    assert maintenance["ok"] and "summary" in maintenance
    assert history["ok"] and history["events"][0]["action"] == "staged"
    assert "session-secret" not in repr(history)
    assert context["ok"] and context["refs"]
    assert any("#claim:" in ref or ref.endswith(".md")
               for ref in context["refs"])
    assert decisions.read_bytes() == audit_before


def test_read_model_tool_schema_and_future_digest_guard(
        fixture_wiki, tmp_path):
    provider = _provider(fixture_wiki, tmp_path)
    props = tools.WIKI_GET_SCHEMA["parameters"]["properties"]
    assert {"query", "status", "origin", "session", "actions"} <= set(props)
    digest = Path(fixture_wiki) / compiler.AGENT_DIGEST_PATH
    payload = json.loads(digest.read_text(encoding="utf-8"))
    payload["schemaVersion"] = schema.SCHEMA_VERSION + 1
    digest.write_text(json.dumps(payload), encoding="utf-8")

    result = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "compiler"}))

    assert not result["available"]
    assert "newer than supported" in result["error"]
