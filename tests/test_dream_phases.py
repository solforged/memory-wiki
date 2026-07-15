from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memory_wiki import candidates, dream, dream_phases, recall, scaffold, wikifs
from memory_wiki.index import WikiIndex


@pytest.fixture
def ready(cfg):
    scaffold.ensure_runtime(cfg)
    index = WikiIndex(cfg.wiki_root)
    index.refresh()
    return cfg, index


def stage(cfg, index, content, **kwargs):
    kwargs.setdefault("kind", "note")
    kwargs.setdefault("confidence", "likely")
    kwargs.setdefault("origin", "sync_turn")
    kwargs.setdefault("evidence", [f'> [user, turn 1] "{content}"'])
    result = candidates.stage(cfg, index, content, **kwargs)
    assert result["staged"], result
    return result["id"]


def qualify(cfg, candidate_id, count=10):
    loaded = candidates.load(cfg, candidate_id)
    assert loaded
    hit = {
        "type": "candidate",
        "id": candidate_id,
        "relevance": 1.0,
        "content_hash": loaded[1]["hash"],
    }
    for number in range(count):
        recall.record(cfg, [hit], f"organic query {number}",
                      session_id=f"session-{number}")


def test_sweep_runs_light_rem_deep_and_only_deep_promotes(ready):
    cfg, index = ready
    candidate_id = stage(
        cfg, index, "Prefers terse CLI output for deployment reviews.",
        kind="preference")
    qualify(cfg, candidate_id)
    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)

    light = dream_phases.run_light(cfg, index, now=now)
    rem = dream_phases.run_rem(cfg, light, now=now)
    profile = Path(cfg.wiki_root) / "profile/user.md"
    assert "terse CLI output" not in profile.read_text()
    assert light["recorded_hits"] == 1
    assert [item["id"] for item in rem["lasting_truths"]] == [candidate_id]

    result = dream.run_sweep(
        cfg, index, apply_safe=True, now=now,
        actor="test", session_id="test-session")
    assert result["phase_order"] == ["light", "rem", "deep"]
    assert result["deep"]["applied"][0]["id"] == candidate_id
    assert "terse CLI output" in profile.read_text()
    assert result["status"]["sweep"]["status"] == "complete"
    assert result["status"]["sweep"]["last_phase"] == "deep"
    assert {name: item["status"] for name, item in
            result["status"]["sweep"]["phases"].items()} == {
        "light": "complete", "rem": "complete", "deep": "complete"}


def test_light_dedupes_pending_candidates_and_never_reads_dreams(ready):
    cfg, index = ready
    first = stage(
        cfg, index,
        "A durable build deploy workflow uses strict checks nightly.")
    stage(
        cfg, index,
        "A durable build deploy workflow uses strict checks nightly for teams.")
    (Path(cfg.wiki_root) / "DREAMS.md").write_text(
        "## Light Sleep\n- An invented candidate must never be ingested.\n")

    result = dream_phases.run_light(cfg, index)

    assert result["input_count"] == 2
    assert result["staged_count"] == 1
    assert result["deduplicated"] == 1
    assert result["items"][0]["id"] == first
    assert "invented candidate" not in str(result)


def test_rem_builds_themes_and_records_only_lasting_truths(ready):
    cfg, index = ready
    strong = stage(
        cfg, index,
        "Backups use Glacier storage for archival retention.")
    weak = stage(
        cfg, index,
        "Backups need weekly checksum verification for archival retention.")
    other = stage(
        cfg, index,
        "Deploy reviews require two approvers before production.")
    qualify(cfg, strong)
    qualify(cfg, other)

    light = dream_phases.run_light(cfg, index)
    rem = dream_phases.run_rem(cfg, light)
    truth_ids = {item["id"] for item in rem["lasting_truths"]}
    state = dream_phases.read_state(cfg)["candidates"]

    assert any(theme["theme"] == "backups" and theme["count"] == 2
               for theme in rem["themes"])
    assert truth_ids == {strong, other}
    assert state[strong]["rem_hits"] == 1
    assert state[other]["rem_hits"] == 1
    assert state[weak]["rem_hits"] == 0


def test_phase_signals_are_bounded_hash_bound_and_concurrent(ready, monkeypatch):
    cfg, _index = ready
    monkeypatch.setattr(dream_phases, "MAX_CANDIDATES", 2)
    items = [
        {"id": f"candidate-{number}", "content_hash": f"{number:016x}"}
        for number in range(3)
    ]
    for item in items:
        dream_phases.record_phase(cfg, "light", [item])
    assert set(dream_phases.all_signals(cfg)) == {
        "candidate-1", "candidate-2"}

    target = items[-1]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(
            lambda _number: dream_phases.record_phase(cfg, "rem", [target]),
            range(20)))
    assert dream_phases.all_signals(cfg)["candidate-2"]["rem_hits"] == 20

    changed = dict(target, content_hash="f" * 16)
    dream_phases.record_phase(cfg, "light", [changed])
    reset = dream_phases.all_signals(cfg)["candidate-2"]
    assert reset["content_hash"] == "f" * 16
    assert reset["light_hits"] == 1
    assert reset["rem_hits"] == 0


def test_malformed_state_fails_soft_and_recovers(ready):
    cfg, index = ready
    path = dream_phases.state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert dream_phases.read_state(cfg)["candidates"] == {}
    assert dream_phases.status(cfg)["issue"] == "dream state was unreadable"

    stage(cfg, index, "Keep deployment rollback instructions current.")
    result = dream_phases.run_light(cfg, index)
    assert result["state_issue"] == "dream state was unreadable"
    assert dream_phases.status(cfg)["issue"] == ""


def test_state_reads_do_not_require_a_writable_lock(ready, monkeypatch):
    cfg, _index = ready

    def fail_lock(*_args, **_kwargs):
        raise AssertionError("read-only state access attempted a wiki lock")

    monkeypatch.setattr(dream_phases.wikifs, "wiki_lock", fail_lock)
    assert dream_phases.read_state(cfg)["candidates"] == {}
    assert dream_phases.status(cfg)["candidate_count"] == 0


def test_deep_score_adds_bounded_decaying_phase_reinforcement(ready):
    cfg, _index = ready
    content = "Prefers concise release summaries."
    content_hash = candidates.content_hash(content)
    item = {"id": "candidate", "content_hash": content_hash}
    signal_at = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    for _number in range(6):
        dream_phases.record_phase(cfg, "light", [item], now=signal_at)
        dream_phases.record_phase(cfg, "rem", [item], now=signal_at)
    organic = {
        "content_hash": content_hash,
        "recall_count": 3,
        "total_score": 2.4,
        "query_hashes": ["query-one", "query-two"],
        "recall_days": ["2026-07-01"],
        "last_recalled_at": "2026-07-01T12:00:00Z",
    }
    ranking = dream.promotion_score(
        cfg, {"id": "candidate", "content": content, "topics": []},
        signals=organic, now=signal_at + timedelta(days=14))

    phase = ranking["phase_reinforcement"]
    assert phase["light_boost"] == pytest.approx(0.03)
    assert phase["rem_boost"] == pytest.approx(0.045)
    assert phase["total_boost"] == pytest.approx(0.075)
    assert ranking["score"] == pytest.approx(
        min(1.0, ranking["base_score"] + 0.075), abs=1e-4)

    changed = dream.promotion_score(
        cfg, {"id": "candidate", "content": content + " Changed."},
        signals=organic, now=signal_at)
    assert changed["phase_reinforcement"]["total_boost"] == 0
    assert changed["gates"]["recall_count"] is False


def test_identical_sweeps_only_reinforce_changed_evidence(ready):
    cfg, index = ready
    candidate_id = stage(
        cfg, index, "Prefers compact release summaries for operators.",
        kind="preference")
    qualify(cfg, candidate_id)
    first_at = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)

    first = dream.run_sweep(cfg, index, now=first_at)
    second = dream.run_sweep(
        cfg, index, now=first_at + timedelta(hours=1))
    state = dream_phases.all_signals(cfg)[candidate_id]

    assert first["light"]["recorded_hits"] == 1
    assert first["rem"]["recorded_hits"] == 1
    assert second["light"]["recorded_hits"] == 0
    assert second["light"]["unchanged_hits"] == 1
    assert second["rem"]["recorded_hits"] == 0
    assert second["rem"]["unchanged_hits"] == 1
    assert state["light_hits"] == 1
    assert state["rem_hits"] == 1

    loaded = candidates.load(cfg, candidate_id)
    hit = {
        "type": "candidate",
        "id": candidate_id,
        "relevance": 1.0,
        "content_hash": loaded[1]["hash"],
    }
    recall.record(
        cfg, [hit], "new organic release context", session_id="fresh-session")
    third = dream.run_sweep(
        cfg, index, now=first_at + timedelta(hours=2))
    state = dream_phases.all_signals(cfg)[candidate_id]

    assert third["light"]["recorded_hits"] == 1
    assert third["rem"]["recorded_hits"] == 1
    assert state["light_hits"] == 2
    assert state["rem_hits"] == 2


def test_scheduled_policy_is_fixed_and_more_conservative(ready):
    cfg, _index = ready
    content = "Prefers compact release summaries for operators."
    signal = {
        "content_hash": candidates.content_hash(content),
        "recall_count": 10,
        "total_score": 10.0,
        "query_hashes": ["query-one", "query-two"],
        "recall_days": ["2026-07-12", "2026-07-13", "2026-07-14"],
        "last_recalled_at": wikifs.utc_iso(),
    }
    candidate = {
        "id": "candidate",
        "content": content,
        "topics": ["one", "two", "three", "four", "five", "six"],
    }

    manual = dream.promotion_score(
        cfg, candidate, signals=signal, phase_signals={})
    scheduled = dream.promotion_score(
        cfg, candidate, signals=signal, phase_signals={}, scheduled=True)

    assert manual["eligible"] is True
    assert manual["thresholds"] == {
        "mode": "manual",
        "min_score": 0.75,
        "min_recall_count": 3,
        "min_unique_queries": 2,
        "recency_half_life_days": 14,
    }
    assert scheduled["eligible"] is False
    assert scheduled["gates"]["unique_queries"] is False
    assert scheduled["thresholds"] == {
        "mode": "scheduled",
        "min_score": 0.80,
        "min_recall_count": 3,
        "min_unique_queries": 3,
        "recency_half_life_days": 14,
    }


def test_failed_sweep_records_phase_without_partial_promotion(
        ready, monkeypatch):
    cfg, index = ready
    stage(cfg, index, "A pending note survives a failed REM phase.")
    monkeypatch.setattr(
        dream_phases, "run_rem",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        dream.run_sweep(cfg, index)

    sweep = dream_phases.status(cfg)["sweep"]
    assert sweep["status"] == "failed"
    assert sweep["last_phase"] == "rem"
    assert sweep["error"] == "RuntimeError: boom"
    assert sweep["phases"]["light"]["status"] == "complete"
    assert sweep["phases"]["rem"]["status"] == "failed"
    assert candidates.list_candidates(cfg)[0]["status"] == "pending"
