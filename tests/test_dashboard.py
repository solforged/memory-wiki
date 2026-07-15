import importlib.util
import json
import sys
import types
from pathlib import Path


class _Router:
    def get(self, *_args, **_kwargs):
        return lambda fn: fn


def load_api(monkeypatch, tmp_path):
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = _Router
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)

    plugin_link = tmp_path / "plugins" / "memory-wiki"
    plugin_link.parent.mkdir(parents=True)
    plugin_link.symlink_to(
        Path(__file__).parents[1] / "memory_wiki", target_is_directory=True)
    api_path = plugin_link / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("memory_wiki_dashboard_test", api_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_manifest_and_assets():
    root = Path(__file__).parents[1] / "memory_wiki" / "dashboard"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["name"] == "memory-wiki"
    assert manifest["icon"] == "Database"
    assert manifest["tab"] == {
        "path": "/memory-wiki", "position": "after:analytics"}
    assert manifest["api"] == "plugin_api.py"
    for rel in ("dist/index.html", "dist/index.js", "dist/style.css"):
        assert (root / rel).is_file()


def test_overview_reads_compiler_dream_candidates_and_qmd(monkeypatch, tmp_path):
    api = load_api(monkeypatch, tmp_path)
    wiki = tmp_path / "wiki"
    home = tmp_path / "hermes"
    cache = wiki / ".memory-wiki" / "cache"
    reports = wiki / "reports"
    inbox = wiki / "_inbox"
    for path in (cache, reports, inbox):
        path.mkdir(parents=True)
    (home / "cron").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  memory-wiki:\n"
        f"    wiki_root: '{wiki}'\n"
        "    search_backend: qmd\n"
        "    qmd_command: definitely-missing-qmd\n"
        "    qmd_search_mode: query\n"
        "    dream_schedule_enabled: true\n"
        "    dream_schedule: '0 3 * * *'\n",
        encoding="utf-8",
    )
    digest_path = cache / "agent-digest.json"
    digest_path.write_text(json.dumps({
        "pageCounts": {"claim": 2, "concept": 1},
        "claimCount": 2,
        "claimHealth": {
            "freshness": {"fresh": 2, "aging": 0, "stale": 0, "unknown": 0},
            "contested": 1,
            "lowConfidence": 1,
            "missingEvidence": 1,
        },
        "contradictionClusters": [{
            "key": "claim.shared.alpha",
            "label": "claim.shared.alpha",
            "kind": "claim-id",
            "entryCount": 2,
            "paths": ["claims/a.md", "claims/b.md"],
        }],
        "pages": [
            {"path": "claims/a.md", "freshnessLevel": "stale"},
            {"path": "claims/b.md"},
            {"path": "concepts/c.md"},
        ],
    }), encoding="utf-8")
    (cache / "claims.jsonl").write_text(
        json.dumps({
            "pagePath": "claims/a.md", "pageTitle": "Alpha", "text": "A",
            "confidence": 0.8, "missingEvidence": False,
            "freshnessLevel": "fresh", "evidence": [{"kind": "source"}],
        }) + "\n" +
        json.dumps({
            "pagePath": "claims/b.md", "pageTitle": "Beta", "text": "B",
            "confidence": 0.49, "missingEvidence": True,
            "freshnessLevel": "stale", "evidence": [],
        }) + "\n",
        encoding="utf-8",
    )
    report_bodies = {
        "open-questions": "# Open questions\n",
        "contradictions": "# Contradictions\n\n- A conflicts with B\n",
        "low-confidence": "# Low confidence\n",
        "claim-health": "# Claim health\n\n- Two claims checked\n",
        "stale-pages": "# Stale pages\n",
        "provenance-coverage": "# Provenance\n",
        "privacy-review": "# Privacy review\n",
    }
    for name, body in report_bodies.items():
        (reports / f"{name}.md").write_text(body, encoding="utf-8")
    (wiki / ".memory-wiki" / "dream-state.json").write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-07-14T23:00:00Z",
        "sweep": {"status": "idle", "completed_at": "2026-07-14T23:00:00Z",
                  "last_phase": "rem", "error": ""},
        "candidates": {
            "a": {"content_hash": "a" * 16, "light_hits": 2, "rem_hits": 1,
                  "last_light_at": "2026-07-14T21:00:00Z",
                  "last_rem_at": "2026-07-14T22:00:00Z"},
        },
    }), encoding="utf-8")
    (wiki / "DREAMS.md").write_text(
        "# Dream diary\n\n## Dream sweep 2026-07-14T20:00:00Z\n\n"
        "- a: needs_review\n",
        encoding="utf-8",
    )
    (inbox / "pending.md").write_text("---\nstatus: pending\n---\n", encoding="utf-8")
    (inbox / "approved.md").write_text("---\nstatus: approved\n---\n", encoding="utf-8")
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{
        "id": "dream-job",
        "name": "memory-wiki-dream-sweep",
        "script": "memory-wiki-dream-sweep.py",
        "schedule_display": "0 3 * * *",
        "enabled": True,
        "state": "scheduled",
        "last_run_at": "2026-07-14T10:00:00Z",
        "next_run_at": "2026-07-15T10:00:00Z",
        "last_status": "success",
    }]}), encoding="utf-8")

    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    cron_modules = {name for name in sys.modules if name == "cron" or name.startswith("cron.")}
    result = api.build_overview(home)
    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    assert result["ok"] is True
    assert result["compiler"]["available"] is True
    assert result["compiler"]["page_count"] == 3
    assert result["compiler"]["generated_at"] == api._mtime_iso(digest_path)
    assert result["claims"]["total"] == 2
    assert result["claims"]["contradiction_count"] == 1
    assert result["claims"]["low_confidence_count"] == 1
    assert result["claims"]["stale_count"] == 1
    assert result["claims"]["provenance_gap_count"] == 1
    assert result["claims"]["by_confidence"] == {"high": 1, "low": 1}
    assert result["claims"]["contradictions"][0]["title"] == \
        "claim.shared.alpha"
    assert result["claims"]["low_confidence"][0]["ref"] == "claims/b.md"
    assert result["claims"]["stale"][0]["ref"] == "claims/b.md"
    assert result["claims"]["provenance_gaps"][0]["ref"] == "claims/b.md"
    assert result["candidates"] == {"pending": 1, "approved": 1}
    assert [phase["name"] for phase in result["dream"]["phases"]] == [
        "light", "rem", "deep"]
    assert {run["phase"] for run in result["dream"]["recent_runs"]} == {
        "light", "rem", "deep"}
    assert result["qmd"]["configured"] is True
    assert result["qmd"]["available"] is False
    assert result["qmd"]["search_mode"] == "query"
    assert result["scheduler"]["enabled"] is True
    assert result["scheduler"]["schedule"] == "0 3 * * *"
    assert result["scheduler"]["job"]["id"] == "dream-job"
    assert result["scheduler"]["in_sync"] is True
    assert {name for name in sys.modules
            if name == "cron" or name.startswith("cron.")} == cron_modules
    assert before == after


def test_overview_has_clear_empty_state(monkeypatch, tmp_path):
    api = load_api(monkeypatch, tmp_path)
    home = tmp_path / "empty-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  memory-wiki:\n    wiki_root: /definitely/missing/memory-wiki-test\n",
        encoding="utf-8",
    )
    result = api.build_overview(home)
    assert result["ok"] is True
    assert result["wiki"]["exists"] is False
    assert result["compiler"]["available"] is False
    assert result["claims"]["total"] == 0
    assert result["dream"]["exists"] is False


def test_overview_tolerates_legacy_compiler_shape(monkeypatch, tmp_path):
    api = load_api(monkeypatch, tmp_path)
    home = tmp_path / "home"
    wiki = tmp_path / "wiki"
    cache = wiki / ".memory-wiki" / "cache"
    home.mkdir()
    cache.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n  memory-wiki:\n"
        f"    wiki_root: '{wiki}'\n",
        encoding="utf-8",
    )
    (cache / "agent-digest.json").write_text(json.dumps({
        "generatedAt": "2026-07-14T22:00:00Z",
        "claimCount": 1,
        "health": {
            "contradictions": 1,
            "lowConfidence": 1,
            "stalePages": 1,
            "provenanceGaps": 1,
        },
        "contradictions": [{"title": "Legacy conflict", "ref": "claims/a.md"}],
    }), encoding="utf-8")
    (cache / "claims.jsonl").write_text(json.dumps({"claim": {
        "ref": "claims/a.md",
        "text": "Legacy claim",
        "confidence": "plausible",
        "stale": True,
    }}) + "\n", encoding="utf-8")

    result = api.build_overview(home)

    assert result["compiler"]["generated_at"] == "2026-07-14T22:00:00Z"
    assert result["claims"]["by_confidence"] == {"plausible": 1}
    assert result["claims"]["contradictions"][0]["title"] == "Legacy conflict"
    assert result["claims"]["low_confidence"][0]["ref"] == "claims/a.md"
    assert result["claims"]["stale"][0]["ref"] == "claims/a.md"
    assert result["claims"]["provenance_gaps"][0]["ref"] == "claims/a.md"


def test_empty_contradiction_digest_ignores_report_placeholder(
        monkeypatch, tmp_path):
    api = load_api(monkeypatch, tmp_path)
    home = tmp_path / "home"
    wiki = tmp_path / "wiki"
    cache = wiki / ".memory-wiki" / "cache"
    reports = wiki / "reports"
    home.mkdir()
    cache.mkdir(parents=True)
    reports.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  memory-wiki:\n"
        f"    wiki_root: '{wiki}'\n",
        encoding="utf-8",
    )
    (cache / "agent-digest.json").write_text(json.dumps({
        "claimHealth": {"lowConfidence": 0, "missingEvidence": 0},
        "contradictionClusters": [],
        "pages": [],
    }), encoding="utf-8")
    (reports / "contradictions.md").write_text(
        "# Contradictions\n\n- No contradictions flagged right now.\n",
        encoding="utf-8",
    )

    result = api.build_overview(home)

    assert result["claims"]["contradiction_count"] == 0


def test_dashboard_surfaces_failed_dream_phase(monkeypatch, tmp_path):
    api = load_api(monkeypatch, tmp_path)
    home = tmp_path / "home"
    wiki = tmp_path / "wiki"
    state_dir = wiki / ".memory-wiki"
    home.mkdir()
    state_dir.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "plugins:\n  memory-wiki:\n"
        f"    wiki_root: '{wiki}'\n",
        encoding="utf-8",
    )
    (state_dir / "dream-state.json").write_text(json.dumps({
        "version": 1,
        "sweep": {
            "status": "failed", "last_phase": "rem",
            "started_at": "2026-07-14T10:00:00Z",
            "completed_at": "2026-07-14T10:01:00Z",
            "error": "RuntimeError: boom",
            "phases": {
                "light": {"status": "complete",
                          "started_at": "2026-07-14T10:00:00Z",
                          "completed_at": "2026-07-14T10:00:30Z"},
                "rem": {"status": "failed",
                        "started_at": "2026-07-14T10:00:30Z",
                        "completed_at": "2026-07-14T10:01:00Z",
                        "error": "RuntimeError: boom"},
            },
        },
        "candidates": {},
    }), encoding="utf-8")

    result = api.build_overview(home)
    phases = {item["name"]: item for item in result["dream"]["phases"]}

    assert phases["light"]["status"] == "complete"
    assert phases["rem"]["status"] == "failed"
    assert any(run["phase"] == "rem" and run["status"] == "failed"
               for run in result["dream"]["recent_runs"])
