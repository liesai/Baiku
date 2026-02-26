from __future__ import annotations

import csv
import json
from pathlib import Path

from backend.workout.session_artifacts import (
    SessionPoint,
    SessionSnapshot,
    export_snapshot_csv,
    save_snapshot,
)


def test_save_snapshot_and_export_csv(tmp_path: Path) -> None:
    snapshot = SessionSnapshot(
        snapshot_id="snap-1",
        started_at_utc="2026-02-25T10:00:00+00:00",
        ended_at_utc="2026-02-25T10:30:00+00:00",
        workout_name="Tempo 30",
        target_mode="erg",
        ftp_watts=220,
        completed=True,
        planned_duration_sec=1800,
        elapsed_duration_sec=1800,
        distance_km=16.4,
        avg_power_watts=178.2,
        avg_cadence_rpm=89.1,
        avg_speed_kmh=31.9,
        power_compliance_pct=92.0,
        rpm_compliance_pct=90.0,
        both_compliance_pct=85.0,
        points=(
            SessionPoint(
                step_label="Warmup",
                t_label="00:00",
                expected_power_watts=120,
                actual_power_watts=118,
                expected_cadence_rpm=88.0,
                actual_cadence_rpm=87.5,
                power_in_zone=True,
                cadence_in_zone=True,
            ),
        ),
    )

    json_path = save_snapshot(snapshot, base_dir=tmp_path)
    csv_path = export_snapshot_csv(snapshot, out_dir=tmp_path)

    assert json_path.exists()
    assert csv_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["snapshot_id"] == "snap-1"
    assert payload["points"][0]["step_label"] == "Warmup"

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][16] == "t_label"
    assert rows[0][17] == "step_label"
    assert rows[1][17] == "Warmup"
