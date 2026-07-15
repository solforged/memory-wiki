import argparse
import json
from argparse import Namespace
from pathlib import Path

import pytest

import memory_wiki
from memory_wiki import (
    cli, compiler, config as mw_config, dream, dream_phases, scheduler, tools,
)


def _parser():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    cli.register_cli(commands.add_parser("memory-wiki"))
    return parser


def test_cli_registers_compile_phase_and_schedule_flags():
    args = _parser().parse_args([
        "memory-wiki", "dream", "--phase", "all", "--scheduled",
        "--compile", "--quiet",
    ])
    assert args.phase == "all"
    assert args.scheduled is True
    assert args.compile_wiki is True
    assert args.quiet is True
    assert _parser().parse_args(
        ["memory-wiki", "dream"]).phase == "deep"


def test_cli_compile_and_schedule(fixture_wiki, tmp_path, monkeypatch, capsys):
    cfg = mw_config.load_config(None, {"wiki_root": str(fixture_wiki)})
    monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
    assert cli._cmd_compile(Namespace()) == 0
    assert (fixture_wiki / compiler.AGENT_DIGEST_PATH).is_file()
    assert "compiled" in capsys.readouterr().out

    home = tmp_path / "hermes"
    observed = {}
    saved = {}

    def reconcile(current, hermes_home):
        observed.update(enabled=current.dream_schedule_enabled,
                        schedule=current.dream_schedule,
                        home=hermes_home)
        return {
            "available": True, "enabled": True,
            "schedule": current.dream_schedule, "job": None,
            "created": True, "updated": False, "removed": [], "error": "",
        }

    monkeypatch.setattr(cli, "_get_hermes_home", lambda: home)
    monkeypatch.setattr(scheduler, "reconcile", reconcile)
    monkeypatch.setattr(scheduler, "validate_schedule", lambda value: value)
    monkeypatch.setattr(
        mw_config, "save_plugin_block",
        lambda values, hermes_home: saved.update(
            values=dict(values), home=hermes_home))
    args = Namespace(enable=True, disable=False, cron="15 2 * * *")
    assert cli._cmd_schedule(args) == 0
    assert observed == {"enabled": True, "schedule": "15 2 * * *",
                        "home": home}
    assert saved == {
        "values": {"dream_schedule_enabled": True,
                   "dream_schedule": "15 2 * * *"},
        "home": home,
    }


def test_cli_invalid_schedule_does_not_persist(tmp_path, monkeypatch, capsys):
    cfg = mw_config.load_config(None)
    saved = []
    reconciled = []
    monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        scheduler, "validate_schedule",
        lambda _value: (_ for _ in ()).throw(ValueError("invalid cron")))
    monkeypatch.setattr(
        scheduler, "reconcile",
        lambda *args, **kwargs: reconciled.append((args, kwargs)))
    monkeypatch.setattr(
        mw_config, "save_plugin_block",
        lambda *args, **kwargs: saved.append((args, kwargs)))

    result = cli._cmd_schedule(Namespace(
        enable=True, disable=False, cron="not cron"))

    assert result == 1
    assert saved == [] and reconciled == []
    assert "invalid cron" in capsys.readouterr().out


def test_primary_initialize_reconciles_disabled_schedule(
        fixture_wiki, tmp_path, monkeypatch):
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
        "auto_compile": False, "dream_schedule_enabled": False,
    })
    calls = []
    monkeypatch.setattr(
        provider, "_reconcile_schedule", lambda: calls.append("reconcile"))

    provider.initialize("session", hermes_home=str(tmp_path), platform="cli")

    assert calls == ["reconcile"]


def test_cli_all_phase_passes_scheduled_policy(fixture_wiki, monkeypatch, capsys):
    cfg = mw_config.load_config(None, {"wiki_root": str(fixture_wiki)})
    monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
    observed = {}

    def run_sweep(*args, **kwargs):
        observed.update(kwargs)
        return {
            "light": {"recorded_hits": 0},
            "rem": {"recorded_hits": 0},
            "deep": {"pending": 0, "considered": 0, "summary": {},
                     "suggestions": [], "applied": []},
        }

    monkeypatch.setattr(dream, "run_sweep", run_sweep)
    args = Namespace(
        phase="all", scheduled=True, compile_wiki=False, quiet=True,
        apply_safe=True, report=False, explain=None, limit=8)
    assert cli._cmd_dream(args) == 0
    assert observed["scheduled"] is True
    assert observed["apply_safe"] is True
    assert observed["limit"] == 8
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("surface", ("cli", "tool"))
@pytest.mark.parametrize("phase, expected", (
    ("light", ("light",)),
    ("rem", ("light", "rem")),
    ("deep", ("deep",)),
))
def test_standalone_phase_records_zero_hit_completion(
        fixture_wiki, tmp_path, monkeypatch, capsys, surface, phase, expected):
    cfg = mw_config.load_config(None, {"wiki_root": str(fixture_wiki)})
    if surface == "cli":
        monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
        assert cli._cmd_dream(Namespace(
            phase=phase, scheduled=False, compile_wiki=False, quiet=True,
            apply_safe=False, report=False, explain=None, limit=None)) == 0
        assert capsys.readouterr().out == ""
    else:
        provider = memory_wiki.MemoryWikiProvider(overrides={
            "wiki_root": str(fixture_wiki), "embeddings": False,
            "auto_compile": False,
        })
        provider.initialize("session", hermes_home=str(tmp_path), platform="cli")
        result = json.loads(provider.handle_tool_call(
            "wiki_apply", {"action": "dream", "phase": phase}))
        assert result["ok"] is True

    sweep = dream_phases.status(cfg)["sweep"]
    assert sweep["status"] == "complete"
    assert sweep["last_phase"] == phase
    assert {name: item["status"] for name, item in sweep["phases"].items()} \
        == {name: "complete" for name in expected}


@pytest.mark.parametrize("surface", ("cli", "tool"))
@pytest.mark.parametrize("phase, expected", (
    ("light", {"light": "failed"}),
    ("rem", {"light": "complete", "rem": "failed"}),
    ("deep", {"deep": "failed"}),
))
def test_standalone_phase_records_failure(
        fixture_wiki, tmp_path, monkeypatch, surface, phase, expected):
    cfg = mw_config.load_config(None, {"wiki_root": str(fixture_wiki)})

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    if phase == "light":
        monkeypatch.setattr(dream_phases, "run_light", fail)
    elif phase == "rem":
        monkeypatch.setattr(dream_phases, "run_rem", fail)
    else:
        monkeypatch.setattr(dream, "run", fail)

    if surface == "cli":
        with pytest.raises(RuntimeError, match="boom"):
            monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
            cli._cmd_dream(Namespace(
                phase=phase, scheduled=False, compile_wiki=False, quiet=True,
                apply_safe=False, report=False, explain=None, limit=None))
    else:
        provider = memory_wiki.MemoryWikiProvider(overrides={
            "wiki_root": str(fixture_wiki), "embeddings": False,
            "auto_compile": False,
        })
        provider.initialize(
            "session", hermes_home=str(tmp_path), platform="cli")
        result = json.loads(provider.handle_tool_call(
            "wiki_apply", {"action": "dream", "phase": phase}))
        assert result["ok"] is False
        assert "boom" in result["error"]

    sweep = dream_phases.status(cfg)["sweep"]
    assert sweep["status"] == "failed"
    assert sweep["last_phase"] == phase
    assert sweep["error"] == "RuntimeError: boom"
    assert {name: item["status"] for name, item in sweep["phases"].items()} \
        == expected


def test_provider_compiles_before_refresh_and_continues_after_failure(
        fixture_wiki, tmp_path, monkeypatch):
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
        "auto_compile": False,
    })
    provider.initialize("session", hermes_home=str(tmp_path), platform="cli")
    provider.cfg.auto_compile = True
    calls = []

    def fail_compile():
        calls.append("compile")
        raise OSError("compiler unavailable")

    monkeypatch.setattr(provider, "_compile_wiki", fail_compile)
    monkeypatch.setattr(provider.index, "refresh", lambda: calls.append("index"))
    monkeypatch.setattr(provider, "recompile_prompt_block",
                        lambda: calls.append("prompt"))
    provider.on_wiki_mutation()
    assert calls == ["compile", "index", "prompt"]


def test_tools_expose_compiler_dream_and_resolve_claim_id(
        fixture_wiki, tmp_path, make_page, monkeypatch):
    make_page(
        fixture_wiki, "claims/structured.md", "claim", "Structured",
        extras={"claims": [{
            "id": "claim.structured.primary",
            "text": "Structured claims resolve to their owner.",
            "status": "supported",
            "confidence": 0.9,
            "evidence": [{"kind": "test", "sourceId": "suite"}],
        }]},
    )
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
    })
    provider.initialize("session", hermes_home=str(tmp_path), platform="cli")

    compiler_status = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "compiler"}))
    dream_state = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "dream-state"}))
    status = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "status"}))
    resolved = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "claim.structured.primary"}))

    assert compiler_status["available"] is True
    assert dream_state["ref"] == "dream-state"
    assert {"compiler", "dream", "schedule"} <= status.keys()
    assert resolved["ref"] == "claims/structured.md"
    assert resolved["claim_id"] == "claim.structured.primary"
    assert resolved["claim"]["text"] == \
        "Structured claims resolve to their owner."

    monkeypatch.setattr(tools, "_MAX_DIGEST_BYTES", 1)
    bounded = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "claim.structured.primary"}))
    assert bounded["ok"] is False


def test_duplicate_claim_id_returns_ambiguity(fixture_wiki, tmp_path, make_page):
    claim = {
        "id": "claim.duplicate.primary", "text": "First owner.",
        "evidence": [{"kind": "test"}],
    }
    make_page(fixture_wiki, "claims/first-owner.md", "claim", "First",
              extras={"claims": [claim]})
    make_page(fixture_wiki, "claims/second-owner.md", "claim", "Second",
              extras={"claims": [{**claim, "text": "Second owner."}]})
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
    })
    provider.initialize("session", hermes_home=str(tmp_path), platform="cli")

    result = json.loads(provider.handle_tool_call(
        "wiki_get", {"ref": "claim.duplicate.primary"}))

    assert result["ok"] is False
    assert result["match_count"] == 2
    assert result["owners"] == [
        "claims/first-owner.md", "claims/second-owner.md"]


def test_tool_all_phase_and_forced_compile(fixture_wiki, tmp_path, monkeypatch):
    provider = memory_wiki.MemoryWikiProvider(overrides={
        "wiki_root": str(fixture_wiki), "embeddings": False,
        "auto_compile": False,
    })
    provider.initialize("session", hermes_home=str(tmp_path), platform="cli")
    observed = {}
    mutations = []

    def run_sweep(*args, **kwargs):
        observed.update(kwargs)
        return {
            "phase_order": ["light", "rem", "deep"],
            "light": {}, "rem": {},
            "deep": {"applied": [], "pending": 0, "considered": 0,
                     "summary": {}, "suggestions": []},
        }

    monkeypatch.setattr(dream, "run_sweep", run_sweep)
    monkeypatch.setattr(
        provider, "on_wiki_mutation",
        lambda *, force_compile=False: mutations.append(force_compile))
    result = json.loads(provider.handle_tool_call("wiki_apply", {
        "action": "dream", "phase": "all", "scheduled": True,
        "compile": True,
    }))
    assert result["ok"] is True and result["phase"] == "all"
    assert observed["scheduled"] is True
    assert mutations == [True]
