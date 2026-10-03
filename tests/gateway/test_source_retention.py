"""Regression for #110589: housekeeping forwards each profile's source retention."""

import time


def test_housekeeping_passes_profile_retention_to_live_store(tmp_path, monkeypatch):
    from gateway.run import _housekeeping_state_db_maintenance
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state_registry import acquire, release_or_close

    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    for home, days in ((home_a, 14), (home_b, 70)):
        home.mkdir()
        (home / "config.yaml").write_text(
            "sessions:\n  auto_prune: true\n  retention_days: 90\n"
            "  min_interval_hours: 0\n  vacuum_after_prune: false\n"
            f"  source_retention_days: {{cron: {days}}}\n")

    for home, days in ((home_a, 14), (home_b, 70), (home_a, 14)):
        token = set_hermes_home_override(home)
        db = acquire()
        try:
            for source in ("cron", "cli"):
                sid = source + "-" + str(time.time_ns())
                db.create_session(sid, source)
                db.end_session(sid, "done")
                old = time.time() - 60 * 86400
                db._conn.execute(
                    "UPDATE sessions SET started_at=?, last_activity_at=?, ended_at=? WHERE id=?",
                    (old, old, old, sid))
                if source == "cron":
                    cron_id = sid
                else:
                    cli_id = sid
            db._conn.commit()
            maintain = db.maybe_auto_prune_and_vacuum
            forwarded = []

            def run_maintenance(**kwargs):
                forwarded.append(kwargs.get("source_retention_days"))
                return maintain(**kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(db, "maybe_auto_prune_and_vacuum", run_maintenance)
                _housekeeping_state_db_maintenance()
            assert forwarded == [{"cron": days}]
            assert (db.get_session(cron_id) is None) == (days == 14)
            assert db.get_session(cli_id) is not None
        finally:
            release_or_close(db)
            reset_hermes_home_override(token)
