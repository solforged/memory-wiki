import json
from pathlib import Path

from memory_wiki import history, schema


def test_history_sanitizes_sessions_and_never_repairs_tail(cfg):
    path = Path(cfg.wiki_root) / schema.DECISIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-07-01T10:00:00Z", "action": "staged", "id": "c1",
         "target": "", "session": "raw-session-secret", "actor": "hook",
         "hash": "abc", "detail": {"origin": "sync_turn",
                                      "secret": "must-not-leak"}},
        {"ts": "2026-07-02T10:00:00Z", "action": "promoted", "id": "c1",
         "target": "profile/user.md", "session": "raw-session-secret",
         "actor": "cli", "hash": "abc", "detail": {"status": "promoted"}},
    ]
    payload = "".join(json.dumps(row) + "\n" for row in rows) + "{torn"
    path.write_text(payload, encoding="utf-8")
    before = path.read_bytes()

    result = history.build(cfg, limit=10)

    assert [item["action"] for item in result["events"]] == [
        "promoted", "staged"]
    assert result["lifecycles"][0]["current_action"] == "promoted"
    assert "raw-session-secret" not in repr(result)
    assert "must-not-leak" not in repr(result)
    assert result["events"][0]["session_hash"]
    assert "invalid audit" in result["issues"][0]
    assert path.read_bytes() == before


def test_history_filters_actions(cfg):
    path = Path(cfg.wiki_root) / schema.DECISIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": "2026-07-01T10:00:00Z", "action": "rejected",
                    "id": "c1"}) + "\n",
        encoding="utf-8")
    assert history.build(cfg, actions="promoted")["events"] == []
