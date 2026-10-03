"""Regression for #110589: explicit per-source retention only tightens global policy."""

import time

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_state import SessionDB

    store = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store.close()


def _seed(store, sid, source, age_days, *, ended=True):
    store.create_session(session_id=sid, source=source)
    if ended:
        store.end_session(sid, end_reason="done")
    old = time.time() - age_days * 86400
    store._conn.execute(
        "UPDATE sessions SET started_at = ?, last_activity_at = ?, ended_at = ? WHERE id = ?",
        (old, old, old if ended else None, sid),
    )
    store._conn.commit()


def test_automation_overrides_preserve_interactive_and_recent_history(db):
    for source in ("cron", "subagent", "oneshot"):
        _seed(db, source + "-old", source, 60)
        _seed(db, source + "-recent", source, 10)
    for source in ("cli", "telegram"):
        _seed(db, source, source, 60)
    _seed(db, "global-old", "telegram", 200)

    result = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, vacuum=False,
        source_retention_days={"cron": 14, "subagent": 14, "oneshot": 14})

    assert "error" not in result
    assert result["pruned"] == 4
    for source in ("cron", "subagent", "oneshot"):
        assert db.get_session(source + "-old") is None
        assert db.get_session(source + "-recent") is not None
    assert db.get_session("global-old") is None
    assert all(db.get_session(source) is not None for source in ("cli", "telegram"))


def test_shorter_orphan_window_closes_only_state_owned_sources(db):
    for source in ("oneshot", "subagent", "telegram", "tui", "desktop"):
        _seed(db, source, source, 20, ended=False)
    _seed(db, "pinned", "oneshot", 20, ended=False)
    db._conn.execute("UPDATE sessions SET pinned = 1 WHERE id = 'pinned'")
    db._conn.commit()
    overrides = {source: 14 for source in ("oneshot", "subagent", "telegram", "tui", "desktop")}

    result = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, vacuum=False, source_retention_days=overrides)

    assert "error" not in result
    assert result["closed"] == 2
    assert result["pruned"] == 0
    for source in ("oneshot", "subagent"):
        row = db.get_session(source)
        assert row["ended_at"] is not None
        assert row["end_reason"] == "startup_orphan_reap"
    for source in ("telegram", "tui", "desktop", "pinned"):
        assert db.get_session(source)["ended_at"] is None
    again = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, vacuum=False, source_retention_days=overrides)
    assert again["pruned"] == 0, "newly closed rows get a full retention window"


def test_override_only_deletions_reach_vacuum_gate(db):
    _seed(db, "old", "cron", 60)
    result = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, source_retention_days={"cron": 14})
    assert "error" not in result
    assert result["pruned"] == 1
    assert "freelist_ratio" in result


def test_invalid_maps_leave_global_behavior_unchanged(db):
    _seed(db, "old", "cron", 60)
    _seed(db, "open", "cron", 20, ended=False)
    baseline = db.maybe_auto_prune_and_vacuum(
        retention_days=90, min_interval_hours=0, vacuum=False)
    for raw in ({"cron": True}, {"cron": "banana"}, {"cron": "14"}, {"cron": -1},
                {"cron": float("inf")}, {"cron": float("nan")}, {"": 14, 1: 14},
                "banana", None):
        result = db.maybe_auto_prune_and_vacuum(
            retention_days=90, min_interval_hours=0, vacuum=False, source_retention_days=raw)
        assert result == baseline
        assert db.get_session("old") is not None
        assert db.get_session("open")["ended_at"] is None


def test_overrides_never_extend_or_repeat_global_work(db, monkeypatch):
    _seed(db, "old", "cron", 100)
    _seed(db, "open", "oneshot", 100, ended=False)
    prune = db.prune_sessions
    sweep = db.sweep_orphaned_sessions
    calls = []

    def record_prune(**kwargs):
        calls.append(("prune", kwargs.get("source")))
        return prune(**kwargs)

    def record_sweep(**kwargs):
        calls.append(("sweep", kwargs["sources"]))
        return sweep(**kwargs)

    monkeypatch.setattr(db, "prune_sessions", record_prune)
    monkeypatch.setattr(db, "sweep_orphaned_sessions", record_sweep)
    for retention, overrides in ((90, {"cron": 90, "oneshot": 120}), (0, {"cron": 14})):
        calls.clear()
        result = db.maybe_auto_prune_and_vacuum(
            retention_days=retention, min_interval_hours=0, vacuum=False,
            source_retention_days=overrides)
        assert "error" not in result
        assert [kind for kind, _ in calls] == ["prune", "sweep"]
        if retention == 90:
            assert db.get_session("open")["end_reason"] == "startup_orphan_reap"
        else:
            assert db.get_session("open") is None
    assert db.get_session("old") is None
