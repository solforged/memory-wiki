from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_wiki import cli, evaluation


QUERY_FIXTURE = Path(__file__).parent / "fixtures" / "evaluation.jsonl"


def test_load_query_set_and_evaluate_local(cfg):
    query_set = evaluation.load_query_set(QUERY_FIXTURE)

    assert [case.id for case in query_set.cases] == [
        "atomic-notes", "slip-box-alias",
    ]
    assert query_set.cases[1].expected == (
        evaluation.ExpectedResult("concepts/zettelkasten.md"),)

    result = evaluation.evaluate(cfg, query_set, backend="local")
    local = result["backends"]["local"]

    assert local["ok"] is True
    assert local["metrics"] == {
        "query_count": 2,
        "recall_at_1": 1.0,
        "recall_at_3": 1.0,
        "recall_at_5": 1.0,
        "mrr": 1.0,
        "zero_hit_count": 0,
        "zero_hit_rate": 0.0,
        "latency_ms": local["metrics"]["latency_ms"],
    }
    assert set(local["metrics"]["latency_ms"]) == {
        "mean", "p50", "p95", "max",
    }
    assert all(value >= 0 for value in local["metrics"]["latency_ms"].values())
    assert all("query" not in case for case in local["cases"])
    assert all(result["backend"] == "local"
               for case in local["cases"] for result in case["top_results"])


def test_zero_hit_metrics(cfg):
    cases = (
        evaluation.QueryCase(
            "missing", "term-that-does-not-exist-anywhere",
            (evaluation.ExpectedResult("concepts/zettelkasten.md"),)),
    )

    metrics = evaluation.evaluate(cfg, cases)["backends"]["local"]["metrics"]

    assert metrics["recall_at_1"] == 0.0
    assert metrics["recall_at_3"] == 0.0
    assert metrics["recall_at_5"] == 0.0
    assert metrics["mrr"] == 0.0
    assert metrics["zero_hit_count"] == 1
    assert metrics["zero_hit_rate"] == 1.0


@pytest.mark.parametrize(
    "content, message",
    (
        ('{"id":"bad"\n', "line 1: invalid JSON"),
        (
            '{"id":"same","query":"one","expected":["a.md"]}\n'
            '{"id":"same","query":"two","expected":["b.md"]}\n',
            "duplicate id",
        ),
        (
            '{"id":"empty","query":"one","expected":[]}\n',
            "expected must be a non-empty list",
        ),
        (
            '{"id":"missing","query":"one","expected":[{}]}\n',
            "ref must be a non-empty string",
        ),
    ),
)
def test_load_query_set_rejects_invalid_jsonl(tmp_path, content, message):
    path = tmp_path / "queries.jsonl"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        evaluation.load_query_set(path)


def test_query_set_parses_claim_and_unit_identifiers(tmp_path):
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps({
        "id": "unit", "query": "specific unit", "corpus": "all",
        "type": "claim", "limit": 7, "tags": ["regression"],
        "expected": [{
            "ref": "claims/example.md",
            "claim_id": "claim.example.primary",
            "unit_id": "unit.example.primary",
        }],
    }) + "\n", encoding="utf-8")

    case = evaluation.load_query_set(path).cases[0]

    assert case.corpus == "all"
    assert case.type_filter == "claim"
    assert case.limit == 7
    assert case.tags == ("regression",)
    assert case.expected[0].claim_id == "claim.example.primary"
    assert case.expected[0].unit_id == "unit.example.primary"


def test_query_set_parses_unit_and_temporal_options(tmp_path):
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps({
        "id": "historical-claim", "query": "old policy",
        "expected": ["claims/example.md"], "granularity": "claim",
        "as_of": "2020-01-01", "include_historical": True,
    }) + "\n", encoding="utf-8")

    case = evaluation.load_query_set(path).cases[0]

    assert case.granularity == "claim"
    assert case.as_of == "2020-01-01"
    assert case.include_historical is True


def test_result_matching_supports_page_claim_and_unit_targets():
    result = {
        "ref": "claims/example.md",
        "matched_claim": {"id": "claim.example.primary"},
        "unit_id": "unit.example.primary",
    }

    assert evaluation.result_matches(
        result, evaluation.ExpectedResult("claims/example.md"))
    assert evaluation.result_matches(
        result, evaluation.ExpectedResult(
            "claims/example.md", claim_id="claim.example.primary"))
    assert evaluation.result_matches(
        result, evaluation.ExpectedResult(
            "claims/example.md", unit_id="unit.example.primary"))
    assert not evaluation.result_matches(
        result, evaluation.ExpectedResult(
            "claims/example.md", claim_id="claim.example.other"))
    unit_result = {
        "ref": "claims/example.md#claim:claim.example.primary",
        "page_ref": "claims/example.md",
        "unit_id": "claims/example.md#claim:claim.example.primary",
        "claim_id": "claim.example.primary",
    }
    assert evaluation.result_matches(
        unit_result, evaluation.ExpectedResult(
            "claims/example.md", claim_id="claim.example.primary",
            unit_id="claims/example.md#claim:claim.example.primary"))


def test_qmd_unavailable_is_not_relabelled_local(
        cfg, monkeypatch):
    calls = []

    class UnavailableQmd:
        def __init__(self, _cfg, _index, read_only=False):
            assert read_only is True

        def search(self, *args, **kwargs):
            calls.append((args, kwargs))
            return None

        def status(self):
            return {"last_error": "QMD fixture state is unavailable"}

    from memory_wiki import qmd
    monkeypatch.setattr(qmd, "QmdBackend", UnavailableQmd)
    query_set = evaluation.load_query_set(QUERY_FIXTURE)

    qmd_only = evaluation.evaluate(cfg, query_set, backend="qmd")
    combined = evaluation.evaluate(cfg, query_set, backend="all")

    assert qmd_only["backends"]["qmd"] == {
        "ok": False,
        "error": "RuntimeError: QMD fixture state is unavailable",
    }
    assert combined["backends"]["local"]["ok"] is True
    assert combined["backends"]["qmd"]["ok"] is False
    assert len(calls) == 2


def test_compare_results_reports_quality_regressions():
    current = _evaluation_payload(
        recall_at_1=0.5, recall_at_3=0.75, recall_at_5=1.0,
        mrr=0.6, zero_hit_rate=0.25, latency=8.0)
    baseline = _evaluation_payload(
        recall_at_1=0.75, recall_at_3=0.75, recall_at_5=1.0,
        mrr=0.7, zero_hit_rate=0.0, latency=5.0)

    comparison = evaluation.compare_results(current, baseline)

    assert comparison["comparable"] is True
    assert {(item["metric"], item["delta"])
            for item in comparison["regressions"]} == {
        ("recall_at_1", -0.25), ("mrr", -0.1), ("zero_hit_rate", 0.25),
    }
    assert comparison["backends"]["local"]["metrics"][
        "latency_ms_mean"]["delta"] == 3.0


def test_compare_results_rejects_different_query_sets():
    current = _evaluation_payload()
    baseline = _evaluation_payload()
    baseline["query_set"]["sha256"] = "different"

    comparison = evaluation.compare_results(current, baseline)

    assert comparison["comparable"] is False
    assert comparison["regressions"] == []


def test_compare_results_requires_matching_backend_metrics():
    current = _evaluation_payload()
    baseline = _evaluation_payload()
    baseline["backends"] = {}

    comparison = evaluation.compare_results(current, baseline)

    assert comparison["comparable"] is False
    assert "backend metrics" in comparison["reason"]


def test_cli_eval_json_and_parser(cfg, monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args([
        "eval", "--queries", str(QUERY_FIXTURE), "--backend", "local", "--json",
    ])
    monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)

    assert cli.memory_wiki_command(args) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["backends"]["local"]["metrics"]["recall_at_5"] == 1.0
    assert "atomic notes" not in output


def test_cli_eval_can_fail_on_baseline_regression(
        cfg, tmp_path, monkeypatch, capsys):
    queries = tmp_path / "queries.jsonl"
    queries.write_text(json.dumps({
        "id": "missing", "query": "not-in-the-wiki",
        "expected": ["concepts/zettelkasten.md"],
    }) + "\n", encoding="utf-8")
    query_set = evaluation.load_query_set(queries)
    baseline = _evaluation_payload(
        recall_at_1=1.0, recall_at_3=1.0, recall_at_5=1.0, mrr=1.0)
    baseline["query_set"]["sha256"] = query_set.sha256
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(cli, "_load_cfg", lambda *args, **kwargs: cfg)
    args = SimpleNamespace(
        queries=str(queries), backend="local", baseline=str(baseline_path),
        limit=None, wiki_root=None, json=True, fail_on_regression=True,
    )

    assert cli._cmd_eval(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["comparison"]["regressions"]


def _evaluation_payload(
        recall_at_1=1.0, recall_at_3=1.0, recall_at_5=1.0, mrr=1.0,
        zero_hit_rate=0.0, latency=1.0):
    return {
        "version": 1,
        "query_set": {"sha256": "same", "count": 4, "path": "queries.jsonl"},
        "backends": {
            "local": {
                "ok": True,
                "metrics": {
                    "query_count": 4,
                    "recall_at_1": recall_at_1,
                    "recall_at_3": recall_at_3,
                    "recall_at_5": recall_at_5,
                    "mrr": mrr,
                    "zero_hit_count": 0,
                    "zero_hit_rate": zero_hit_rate,
                    "latency_ms": {"mean": latency},
                },
            },
        },
    }
