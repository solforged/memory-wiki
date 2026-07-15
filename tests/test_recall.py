import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memory_wiki import recall, scaffold


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    return cfg


def state_path(cfg):
    return Path(cfg.wiki_root) / "_inbox" / recall.STATE_FILE


def candidate(candidate_id="cand-1", score=0.5, key="score",
              content_hash=""):
    result = {"type": "candidate", "id": candidate_id, key: score}
    if content_hash:
        result["content_hash"] = content_hash
    return result


def test_record_tracks_metrics_without_raw_query(ready, monkeypatch):
    monkeypatch.setattr(recall.wikifs, "utc_iso",
                        lambda: "2026-07-14T12:00:00Z")
    result = recall.record(
        ready,
        [candidate(score=0.4, key="relevance"),
         {"type": "concept", "id": "page-1", "score": 1.0}],
        "  Secret Router   VLAN  ", session_id="private-session",
        source="prefetch")

    assert result == {"recorded": 1, "deduplicated": 0, "issue": ""}
    metric = recall.metrics(ready, "cand-1")
    assert metric["recall_count"] == 1
    assert metric["total_score"] == pytest.approx(0.4)
    assert metric["average_score"] == pytest.approx(0.4)
    assert metric["max_score"] == pytest.approx(0.4)
    assert metric["unique_query_count"] == 1
    assert metric["recall_days"] == ["2026-07-14"]
    assert metric["recall_day_count"] == 1
    assert metric["first_recalled_at"] == "2026-07-14T12:00:00Z"
    assert metric["last_recalled_at"] == "2026-07-14T12:00:00Z"
    assert metric["sources"] == ["prefetch"]

    raw = state_path(ready).read_text()
    assert "Secret Router" not in raw
    assert "private-session" not in raw
    assert not list(state_path(ready).parent.glob(".*short-term-recall*tmp*"))


def test_same_session_query_is_deduplicated(ready):
    first = recall.record(
        ready, [candidate(score=0.3)], "router vlan", session_id="s1")
    duplicate = recall.record(
        ready, [candidate(score=0.9)], " ROUTER   vlan ", session_id="s1",
        source="wiki_search")
    other_session = recall.record(
        ready, [candidate(score=0.7)], "router vlan", session_id="s2",
        source="wiki_search")

    assert first["recorded"] == 1
    assert duplicate == {"recorded": 0, "deduplicated": 1, "issue": ""}
    assert other_session["recorded"] == 1
    metric = recall.metrics(ready, "cand-1")
    assert metric["recall_count"] == 2
    assert metric["total_score"] == pytest.approx(1.0)
    assert metric["max_score"] == pytest.approx(0.7)
    assert metric["unique_query_count"] == 1
    assert metric["sources"] == ["prefetch", "wiki_search"]


def test_changed_candidate_content_resets_recall_evidence(ready):
    recall.record(
        ready, [candidate(score=0.4, content_hash="a" * 16)],
        "router vlan", session_id="s1")
    recall.record(
        ready, [candidate(score=0.9, content_hash="b" * 16)],
        "router vlan", session_id="s1")

    metric = recall.metrics(ready, "cand-1")
    assert metric["recall_count"] == 1
    assert metric["total_score"] == pytest.approx(0.9)
    assert metric["content_hash"] == "b" * 16


def test_unbound_legacy_evidence_is_not_adopted_by_content(ready):
    recall.record(ready, [candidate(score=0.4)], "router vlan",
                  session_id="s1")
    result = recall.record(
        ready, [candidate(score=0.9, content_hash="a" * 16)],
        "router vlan", session_id="s1")

    assert result["recorded"] == 1
    metric = recall.metrics(ready, "cand-1")
    assert metric["recall_count"] == 1
    assert metric["total_score"] == pytest.approx(0.9)
    assert metric["content_hash"] == "a" * 16


def test_growing_fields_and_state_are_bounded(ready, monkeypatch):
    monkeypatch.setattr(recall, "MAX_CANDIDATES", 3)
    monkeypatch.setattr(recall, "MAX_EVENT_KEYS", 4)
    monkeypatch.setattr(recall, "MAX_QUERY_HASHES", 2)
    monkeypatch.setattr(recall, "MAX_RECALL_DAYS", 2)
    monkeypatch.setattr(recall, "MAX_SOURCES", 2)

    clock = iter([
        "2026-07-10T12:00:00Z", "2026-07-11T12:00:00Z",
        "2026-07-12T12:00:00Z", "2026-07-13T12:00:00Z",
        "2026-07-14T12:00:00Z", "2026-07-15T12:00:00Z",
        "2026-07-16T12:00:00Z",
    ])
    monkeypatch.setattr(recall.wikifs, "utc_iso", lambda: next(clock))
    for i in range(4):
        recall.record(
            ready, [candidate()], f"query {i}", session_id=f"session-{i}",
            source=f"source-{i}")

    metric = recall.metrics(ready, "cand-1")
    assert metric["query_hashes"] and len(metric["query_hashes"]) == 2
    assert metric["recall_days"] == ["2026-07-12", "2026-07-13"]
    assert metric["sources"] == ["source-2", "source-3"]

    for i in range(2, 5):
        recall.record(
            ready, [candidate(f"cand-{i}")], f"other {i}",
            session_id=f"other-session-{i}")

    all_values = recall.all_metrics(ready)
    assert set(all_values) == {"cand-2", "cand-3", "cand-4"}
    raw = json.loads(state_path(ready).read_text())
    assert len(raw["candidates"]) == 3
    assert len(raw["events"]) <= 4


def test_corrupt_state_fails_soft_and_recovers_on_record(ready):
    path = state_path(ready)
    path.write_text("{not json", encoding="utf-8")

    assert recall.metrics(ready, "cand-1")["recall_count"] == 0
    assert recall.all_metrics(ready) == {}
    result = recall.record(ready, [candidate()], "safe hashed query")
    assert result["recorded"] == 1
    assert result["issue"] == "recall state was unreadable"
    assert recall.metrics(ready, "cand-1")["recall_count"] == 1
    assert json.loads(path.read_text())["version"] == recall.STATE_VERSION


def test_forget_removes_metric_and_allows_event_again(ready):
    recall.record(ready, [candidate()], "router vlan", session_id="s1")
    assert recall.forget(ready, "cand-1") is True
    assert recall.forget(ready, "cand-1") is False
    assert recall.metrics(ready, "cand-1")["recall_count"] == 0
    assert recall.record(
        ready, [candidate()], "router vlan", session_id="s1")["recorded"] == 1


def test_concurrent_records_do_not_lose_updates(ready):
    def write(i):
        return recall.record(
            ready, [candidate(score=i / 10)], "same query",
            session_id=f"session-{i}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, range(20)))

    assert sum(item["recorded"] for item in outcomes) == 20
    metric = recall.metrics(ready, "cand-1")
    assert metric["recall_count"] == 20
    assert metric["total_score"] == pytest.approx(
        sum(min(i / 10, 1.0) for i in range(20)))
    assert metric["max_score"] == pytest.approx(1.0)
