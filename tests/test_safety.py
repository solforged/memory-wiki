from __future__ import annotations

import json
from pathlib import Path

import pytest

import memory_wiki
from memory_wiki import candidates, extract, safety, wikifs


@pytest.fixture
def provider(fixture_wiki, tmp_path):
    p = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki),
        "embeddings": False,
    })
    p.initialize("safe-session", hermes_home=str(tmp_path), platform="cli")
    return p


@pytest.mark.parametrize("text", [
    "My password is hunter2.",
    "passphrase: correct-horse-battery-staple",
    "API_KEY=sk-example-secret",
    "access token: ghp_exampletoken123",
    "PRIVATE_KEY=base64-private-key-value",
    "bearer_token=eyExampleToken123",
    "Authorization: Bearer eyExampleToken123",
    "Bearer eyExampleToken123",
    "--password super-secret-value",
    "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----",
    "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
    "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    "AKIAABCDEFGHIJKLMNOP",
])
def test_sensitive_content_is_detected_without_returning_value(text):
    result = safety.contains_sensitive_content(text)
    assert result is True
    assert isinstance(result, bool)
    assert safety.safe_excerpt(text) == ""


def test_ordinary_identity_and_preference_still_extract():
    items = extract.extract(
        "I prefer uv over pip for Python projects. "
        "I work remotely from Dublin.")
    assert {item["kind"] for item in items} == {"preference", "fact"}
    assert not safety.contains_sensitive_content(
        "I prefer password managers and short access-token expiry windows.")


def test_deterministic_capture_drops_credentials(provider, caplog):
    secret = "My password is hunter2."
    provider.sync_turn(secret, "Noted", session_id="safe-session")
    assert candidates.list_candidates(provider.cfg, statuses=("pending",)) == []
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path(provider.cfg.wiki_root).rglob("*") if path.is_file())
    assert "hunter2" not in persisted
    assert "hunter2" not in caplog.text


def test_llm_capture_filters_secrets_and_shares_turn_cap(
        provider, monkeypatch):
    provider.cfg.llm_extraction = True
    provider.cfg.max_candidates_per_turn = 3

    from memory_wiki import llm_extract
    monkeypatch.setattr(llm_extract, "extract_turn", lambda *args: [
        {"content": "access token: hidden-token-value", "kind": "fact"},
        {"content": "Stores deployment references in the team wiki.",
         "kind": "note", "confidence": "likely",
         "fields": {"url": "https://example.test/?access_token=field-secret"}},
        {"content": "Keeps release notes concise and actionable.",
         "kind": "note", "confidence": "likely",
         "rationale": "access token: rationale-secret-value"},
        {"content": "Runs integration checks before deployment.",
         "kind": "note", "confidence": "likely"},
        {"content": "Uses canary releases for risky migrations.",
         "kind": "note", "confidence": "likely"},
    ])

    provider.sync_turn(
        "My password is evidence-secret-value. "
        "I prefer tabs over spaces always. "
        "I usually review diffs before commits.",
        "Understood", session_id="safe-session")

    staged = candidates.list_candidates(provider.cfg, statuses=("pending",))
    assert len(staged) == 3
    assert all("hidden-token-value" not in item["content"] for item in staged)
    assert provider.state.session_count == 3
    persisted = "\n".join(
        (Path(provider.cfg.wiki_root) / item["ref"]).read_text(
            encoding="utf-8") for item in staged)
    assert "evidence-secret-value" not in persisted
    assert "rationale-secret-value" not in persisted
    assert "field-secret" not in persisted


def test_session_episode_omits_sensitive_excerpts(provider):
    secret = "My password is journal-secret-value."
    auth = "Authorization: Bearer assistant-secret-value"
    provider.on_session_end([
        {"role": "user", "content": secret},
        {"role": "assistant", "content": auth},
    ])
    journal = next((Path(provider.cfg.wiki_root) / "journal").glob("*.md"))
    text = wikifs.read_text(journal) or ""
    assert "journal-secret-value" not in text
    assert "assistant-secret-value" not in text
    assert "opened:" not in text
    assert "closed:" not in text


def test_memory_write_mirror_rejects_sensitive_content(provider):
    provider.on_memory_write(
        "add", "user", "My password is mirror-secret-value.", {})
    profile = wikifs.read_text(
        Path(provider.cfg.wiki_root) / "profile/user.md") or ""
    assert "mirror-secret-value" not in profile


def test_memory_write_metadata_does_not_persist_credentials(provider):
    provider.on_memory_write(
        "replace", "memory", "ignored",
        {"write_origin": "API key: metadata-secret-value"})
    decisions = wikifs.read_text(
        Path(provider.cfg.wiki_root) / "_inbox/decisions.jsonl") or ""
    assert "metadata-secret-value" not in decisions


def test_model_facing_writes_reject_sensitive_content(provider):
    for args in (
        {"action": "write_profile", "page": "user",
         "content": "My password is direct-secret-value."},
        {"action": "append_journal",
         "content": "Authorization: Bearer journal-secret-value"},
        {"action": "stage", "kind": "note",
         "content": "API key: staged-secret-value"},
        {"action": "stage", "kind": "claim",
         "content": "Keep deployment notes durable.",
         "fields": {"url": "https://example.test/?access_token=field-secret"}},
        {"action": "stage", "kind": "note",
         "content": "Keep deployment notes concise.",
         "topics": ["API key: topic-secret-value"]},
        {"action": "stage", "kind": "note",
         "content": "Keep metadata structurally safe.",
         "topics": ["safe\nstatus: promoted"]},
        {"action": "stage", "kind": "claim",
         "content": "Keep fields structurally safe.",
         "fields": {"url": "safe\nstatus: promoted"}},
    ):
        result = json.loads(provider.handle_tool_call("wiki_apply", args))
        assert result["ok"] is False
        assert ("credential material" in result["error"]
                or "frontmatter scalars" in result["error"])


def test_approval_overrides_reject_sensitive_content(provider):
    staged = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "stage", "kind": "claim",
        "content": "Keep deployment notes durable.",
    }))
    for override in (
        {"fields": {"url": "https://example.test/?access_token=override-secret"}},
        {"content": "API key: content-override-secret"},
        {"topics": ["safe\nstatus: promoted"]},
    ):
        result = json.loads(provider.handle_tool_call("wiki_apply", {
            "action": "approve", "id": staged["id"], **override,
        }))
        assert result["ok"] is False
        assert ("credential material" in result["error"]
                or "frontmatter scalars" in result["error"])
    _path, fm, content, _evidence = candidates.load(provider.cfg, staged["id"])
    assert fm["status"] == "pending"
    assert content == "Keep deployment notes durable."


def test_write_sections_and_reject_reasons_are_safe(provider):
    injected = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "write_profile", "page": "user", "content": "Harmless",
        "section": "## Notes\n\nAPI key: injected-secret-value",
    }))
    assert not injected["ok"] and "section" in injected["error"]

    staged = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "stage", "kind": "note", "content": "Reject me safely.",
    }))
    rejected = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "reject", "id": staged["id"],
        "reason": "API key: reject-secret-value",
    }))
    assert not rejected["ok"] and "credential material" in rejected["error"]
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path(provider.cfg.wiki_root).rglob("*") if path.is_file())
    assert "injected-secret-value" not in persisted
    assert "reject-secret-value" not in persisted
