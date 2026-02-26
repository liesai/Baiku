from __future__ import annotations

from pathlib import Path

from backend.workout.session_store import SessionRecord, append_session, load_recent_sessions


def test_append_and_load_recent_sessions(tmp_path: Path) -> None:
    store = tmp_path / "sessions.jsonl"
    r1 = SessionRecord(
        started_at_utc="2026-02-25T10:00:00+00:00",
        ended_at_utc="2026-02-25T10:30:00+00:00",
        workout_name="Tempo 30",
        target_mode="erg",
        ftp_watts=220,
        completed=True,
        planned_duration_sec=1800,
        elapsed_duration_sec=1800,
        distance_km=16.1,
        avg_power_watts=175.0,
        avg_cadence_rpm=90.0,
        avg_speed_kmh=32.0,
        power_compliance_pct=91.0,
        rpm_compliance_pct=89.0,
        both_compliance_pct=84.0,
    )
    r2 = SessionRecord(
        started_at_utc="2026-02-26T10:00:00+00:00",
        ended_at_utc="2026-02-26T10:20:00+00:00",
        workout_name="Wake Up 20",
        target_mode="erg",
        ftp_watts=220,
        completed=False,
        planned_duration_sec=1200,
        elapsed_duration_sec=600,
        distance_km=8.2,
        avg_power_watts=145.0,
        avg_cadence_rpm=88.0,
        avg_speed_kmh=29.0,
        power_compliance_pct=80.0,
        rpm_compliance_pct=85.0,
        both_compliance_pct=74.0,
    )
    append_session(r1, path=store)
    append_session(r2, path=store)

    loaded = load_recent_sessions(limit=5, path=store)

    assert len(loaded) == 2
    assert loaded[0].workout_name == "Wake Up 20"
    assert loaded[1].workout_name == "Tempo 30"
