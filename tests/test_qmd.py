import json
import os
from pathlib import Path
from types import SimpleNamespace

from memory_wiki import schema, wikifs
from memory_wiki.index import WikiIndex
from memory_wiki.qmd import QmdBackend


def make_cfg(root, state, **overrides):
    values = {
        "wiki_root": root,
        "qmd_command": "qmd",
        "qmd_search_mode": "search",
        "qmd_timeout_seconds": 2,
        "qmd_maintenance_timeout_seconds": 12,
        "qmd_update_interval_seconds": 300,
        "qmd_embed_interval_seconds": 3600,
        "qmd_rerank": True,
        "qmd_state_dir": state,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def initialize_state(backend, updated_at=None, embedded_at=None):
    backend._config_path.parent.mkdir(parents=True, exist_ok=True)
    backend._cache_home.joinpath("qmd").mkdir(parents=True, exist_ok=True)
    backend._config_path.write_text(
        f"collections:\n  {backend.collection}:\n"
        f"    path: {backend.wiki_root}\n    pattern: \"**/*.md\"\n",
        encoding="utf-8",
    )
    backend._db_path.write_bytes(b"sqlite")
    if updated_at is not None:
        backend.state_dir.mkdir(parents=True, exist_ok=True)
        signature = backend._wiki_signature()
        backend._marker_path.write_text(json.dumps({
            "version": 1,
            "wiki_root": str(backend.wiki_root),
            "collection": backend.collection,
            "updated_at": updated_at,
            "embedded_at": embedded_at or 0,
            "wiki_signature": signature,
        }), encoding="utf-8")
        backend._last_update = updated_at
        backend._last_embed = embedded_at or 0
        backend._last_signature = signature


def test_relative_state_dir_is_resolved_before_subprocess_use(
        fixture_wiki, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = QmdBackend(make_cfg(fixture_wiki, "relative-qmd"),
                         WikiIndex(fixture_wiki))
    assert backend.state_dir == (tmp_path / "relative-qmd").resolve()
    assert Path(backend._environment()["XDG_CONFIG_HOME"]).is_absolute()


def test_private_environment_prefers_mise_node_without_its_shims(
        fixture_wiki, tmp_path, monkeypatch):
    node_bin = "/home/user/.local/share/mise/installs/node/26/bin"
    monkeypatch.setenv(
        "PATH", "/usr/local/bin:/home/user/.local/share/mise/shims:"
        f"{node_bin}:/qmd/bin")
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"),
                         WikiIndex(fixture_wiki))
    entries = backend._environment()["PATH"].split(os.pathsep)
    assert entries[0] == node_bin
    assert "/home/user/.local/share/mise/shims" not in entries


def test_initializes_private_state_and_filters_qmd_results(
        fixture_wiki, tmp_path, monkeypatch):
    profile = fixture_wiki / "profile"
    profile.mkdir()
    (profile / "user.md").write_text("---\ntype: profile\ntitle: User\n---\nPrivate\n")
    inbox = fixture_wiki / schema.INBOX_DIR
    inbox.mkdir()
    rejected = inbox / "rejected.md"
    rejected.write_text(wikifs.render_page({
        "id": "20260714-120000-deadbeef-01", "status": "rejected",
        "kind": "fact", "title": "Rejected", "created": "2026-07-14",
    }, "Rejected memory.\n"))
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:3] == ["collection", "add"]:
            initialize_state(backend)
            return SimpleNamespace(returncode=0, stdout="created", stderr="")
        if argv[1] == "update":
            return SimpleNamespace(returncode=0, stdout="updated", stderr="")
        payload = [
            {"score": 0.91, "file": "./concepts/zettelkasten.md", "line": 4,
             "title": "wrong title", "snippet": "@@ -1,2 @@\nAtomic notes"},
            {"score": 0.90, "file": str(rejected), "snippet": "Rejected"},
            {"score": 0.80, "file": "./profile/user.md", "snippet": "Private"},
            {"score": 0.70, "file": "/tmp/outside.md", "snippet": "Outside"},
            {"score": 0.60,
             "file": f"qmd://{backend.collection}/claims/small-tools-beat-frameworks.md",
             "snippet": "Small tools"},
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    results = backend.search("small atomic tools", corpus="pages", limit=2)
    assert [item["ref"] for item in results] == [
        "concepts/zettelkasten.md", "claims/small-tools-beat-frameworks.md"]
    assert results[0]["title"] == "Zettelkasten"
    assert results[0]["snippet"] == "Atomic notes"
    assert results[0]["relevance"] == 0.3333
    assert results[1]["confidence"] == "likely"

    assert [call[0][1] for call in calls] == ["collection", "update", "search"]
    search_argv, search_kwargs = calls[-1]
    assert search_argv[2] == "small atomic tools"
    assert "--format" in search_argv and "json" in search_argv
    assert "--full-path" in search_argv
    assert search_argv[-2:] == ["-c", backend.collection]
    assert search_kwargs["shell"] is False
    assert search_kwargs["timeout"] == 2
    assert calls[0][1]["timeout"] == 12
    assert calls[1][1]["timeout"] == 12
    env = search_kwargs["env"]
    assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path / "qmd"))
    assert env["XDG_CACHE_HOME"].startswith(str(tmp_path / "qmd"))
    assert all(not (Path(p).name == "shims" and "mise" in Path(p).parts)
               for p in env["PATH"].split(os.pathsep))


def test_read_only_uninitialized_never_mutates_or_runs(
        fixture_wiki, tmp_path, monkeypatch):
    state = tmp_path / "absent"
    backend = QmdBackend(make_cfg(fixture_wiki, state),
                         WikiIndex(fixture_wiki), read_only=True)
    calls = []
    monkeypatch.setattr("memory_wiki.qmd.subprocess.run",
                        lambda *a, **k: calls.append(a))
    assert backend.search("zettelkasten") is None
    assert calls == []
    assert not state.exists()
    status = backend.status()
    assert not status["initialized"] and not status["available"]
    assert "read-only" in status["last_error"]


def test_read_only_initialized_searches_without_update(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index,
                         read_only=True)
    initialize_state(backend)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        payload = [{"score": 0.5, "file": "./concepts/zettelkasten.md",
                    "snippet": "Atomic notes"}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("atomic")[0]["ref"] == "concepts/zettelkasten.md"
    assert [argv[1] for argv in calls] == ["search"]


def test_zero_qmd_scores_use_absolute_query_coverage(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    initialize_state(backend, updated_at=10**10)
    payload = [
        {"score": 0, "file": "./concepts/zettelkasten.md"},
        {"score": 0, "file": "./claims/small-tools-beat-frameworks.md"},
    ]
    monkeypatch.setattr(
        "memory_wiki.qmd.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""))
    results = backend.search("zettelkasten", limit=2)
    assert [item["score"] for item in results] == [0.0, 0.0]
    assert [item["relevance"] for item in results] == [1.0, 0.0]


def test_qmd_scores_reject_nonfinite_and_negative_values():
    assert QmdBackend._score(float("nan")) == 0.0
    assert QmdBackend._score(float("inf")) == 0.0
    assert QmdBackend._score(-0.1) == 0.0
    assert QmdBackend._score(0.75) == 0.75


def test_invalidate_forces_next_update(fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    initialize_state(backend, updated_at=10**10)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "update":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("first") == []
    assert [argv[1] for argv in calls] == ["search"]
    backend.invalidate()
    assert backend.search("second") == []
    assert [argv[1] for argv in calls] == ["search", "update", "search"]


def test_reconciles_stale_same_name_collection(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    backend._config_path.parent.mkdir(parents=True)
    backend._cache_home.joinpath("qmd").mkdir(parents=True)
    backend._db_path.write_bytes(b"sqlite")
    backend._config_path.write_text(
        f"collections:\n  {backend.collection}:\n"
        f"    path: {tmp_path / 'wrong-wiki'}\n"
        "    pattern: \"*.txt\"\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["collection", "add"]:
            initialize_state(backend)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("atomic")
    assert [argv[1:3] for argv in calls[:2]] == [
        ["collection", "remove"], ["collection", "add"]]
    add = calls[1]
    assert add[3] == str(fixture_wiki.resolve())
    assert add[-2:] == ["--mask", "**/*.md"]
    assert [argv[1] for argv in calls[2:]] == ["update", "search"]


def test_similar_collection_name_does_not_count_as_configured(
        fixture_wiki, tmp_path):
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"),
                         WikiIndex(fixture_wiki))
    backend._config_path.parent.mkdir(parents=True)
    backend._config_path.write_text(
        f"collections:\n  {backend.collection}-old:\n"
        f"    path: {fixture_wiki}\n    pattern: \"**/*.md\"\n",
        encoding="utf-8",
    )
    assert backend._collection_status() == (False, False)


def test_wiki_edit_forces_update_before_interval(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    initialize_state(backend, updated_at=10**10)
    page = fixture_wiki / "concepts" / "zettelkasten.md"
    page.write_text(page.read_text() + "Changed content.\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("atomic")
    assert [argv[1] for argv in calls] == ["update", "search"]


def test_semantic_mode_maintains_embeddings_and_can_skip_reranking(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(
        fixture_wiki, tmp_path / "qmd", qmd_search_mode="query",
        qmd_rerank=False), index)
    initialize_state(backend, updated_at=10**10)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "embed":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        payload = [{"score": 0.8, "file": "./concepts/zettelkasten.md",
                    "snippet": "Atomic notes"}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    results = backend.search("atomic")
    assert results[0]["ref"] == "concepts/zettelkasten.md"
    assert [argv[1] for argv, _kwargs in calls] == ["embed", "query"]
    assert calls[0][1]["timeout"] == 12
    assert "--no-rerank" in calls[1][0]
    marker = json.loads(backend._marker_path.read_text())
    assert marker["embedded_at"] > 0


def test_semantic_only_hit_returns_unambiguous_claim_metadata(
        fixture_wiki, tmp_path, monkeypatch, make_page):
    make_page(
        fixture_wiki, "claims/semantic.md", "claim", "Semantic",
        extras={"claims": [{
            "id": "claim.semantic.primary",
            "text": "Rollback precedes another release attempt.",
            "status": "supported",
            "evidence": [{"kind": "runbook", "sourceId": "release"}],
        }]},
    )
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(
        fixture_wiki, tmp_path / "qmd", qmd_search_mode="query"), index)
    initialize_state(backend, updated_at=10**10, embedded_at=10**10)

    def fake_run(argv, **kwargs):
        payload = [{"score": 0.82, "file": "./claims/semantic.md",
                    "snippet": "A related operational safeguard."}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    result = backend.search("recovery safety")[0]
    assert result["matched_claim"]["id"] == "claim.semantic.primary"
    assert result["matched_claim"]["evidence_source_ids"] == ["release"]


def test_semantic_invalidate_forces_update_and_embed(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(
        fixture_wiki, tmp_path / "qmd", qmd_search_mode="vsearch"), index)
    initialize_state(backend, updated_at=10**10, embedded_at=10**10)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    backend.invalidate()
    assert backend.search("atomic")
    assert [argv[1] for argv in calls] == ["update", "embed", "vsearch"]


def test_semantic_wiki_edit_forces_embed_before_interval(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(
        fixture_wiki, tmp_path / "qmd", qmd_search_mode="query"), index)
    initialize_state(backend, updated_at=10**10, embedded_at=10**10)
    page = fixture_wiki / "concepts" / "zettelkasten.md"
    page.write_text(page.read_text() + "Changed content.\n", encoding="utf-8")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("atomic")
    assert [argv[1] for argv in calls] == ["update", "embed", "query"]


def test_periodic_update_does_not_force_semantic_embed(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(
        fixture_wiki, tmp_path / "qmd", qmd_search_mode="query"), index)
    initialize_state(backend, updated_at=1, embedded_at=10**10)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    assert backend.search("atomic")
    assert [argv[1] for argv in calls] == ["update", "query"]


def test_restricted_qmd_false_empty_fills_from_local_corpus(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    initialize_state(backend, updated_at=10**10)
    payload = [{
        "score": 0.9,
        "file": "./claims/small-tools-beat-frameworks.md",
        "snippet": "Disallowed by the concept filter",
    }]
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", fake_run)
    results = backend.search(
        "zettelkasten", corpus="pages", type_filter="concept", limit=1)
    assert [item["ref"] for item in results] == ["concepts/zettelkasten.md"]
    assert results[0]["backend"] == "local"
    assert results[0]["relevance"] == 1.0
    assert calls[0][calls[0].index("-n") + 1] == "100"


def test_failure_backs_off_and_invalidate_retries(
        fixture_wiki, tmp_path, monkeypatch):
    index = WikiIndex(fixture_wiki)
    index.refresh()
    backend = QmdBackend(make_cfg(fixture_wiki, tmp_path / "qmd"), index)
    calls = []

    def missing(*args, **kwargs):
        calls.append(args[0])
        raise FileNotFoundError("qmd missing")

    monkeypatch.setattr("memory_wiki.qmd.subprocess.run", missing)
    assert backend.search("atomic") is None
    assert backend.search("atomic") is None
    assert len(calls) == 1
    status = backend.status()
    assert status["backoff"] and "unavailable" in status["last_error"]
    backend.invalidate()
    assert backend.search("atomic") is None
    assert len(calls) == 2


def test_provider_uses_qmd_and_falls_back_to_local(
        fixture_wiki, tmp_path, monkeypatch):
    import memory_wiki

    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki),
        "search_backend": "qmd",
        "qmd_state_dir": str(tmp_path / "qmd"),
        "embeddings": False,
    })
    provider.initialize("qmd-session", hermes_home=str(tmp_path),
                        platform="cli")
    monkeypatch.setattr(provider.qmd, "search", lambda *args, **kwargs: [{
        "ref": "concepts/zettelkasten.md", "title": "Zettelkasten",
        "type": "concept", "score": 0.9, "relevance": 0.9,
        "snippet": "Atomic notes", "backend": "qmd",
    }])
    result = json.loads(provider.handle_tool_call(
        "wiki_search", {"query": "atomic notes"}))
    assert result["ok"] and result["backend"] == "qmd"

    monkeypatch.setattr(provider.qmd, "search", lambda *args, **kwargs: None)
    result = json.loads(provider.handle_tool_call(
        "wiki_search", {"query": "zettelkasten"}))
    assert result["ok"] and result["backend"] == "local"
    status = json.loads(provider.handle_tool_call("wiki_get", {"ref": "status"}))
    assert status["search_backend"] == "qmd"
    assert status["last_search_backend"] == "local"
