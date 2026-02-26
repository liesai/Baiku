"""Workout session snapshots and exports."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path


def _default_snapshot_dir() -> Path:
    return Path.home() / ".velox-engine" / "snapshots"


@dataclass(frozen=True)
class SessionPoint:
    step_label: str
    t_label: str
    expected_power_watts: int
    actual_power_watts: int | None
    expected_cadence_rpm: float
    actual_cadence_rpm: float | None
    power_in_zone: bool | None
    cadence_in_zone: bool | None


@dataclass(frozen=True)
class SessionSnapshot:
    snapshot_id: str
    started_at_utc: str
    ended_at_utc: str
    workout_name: str
    target_mode: str
    ftp_watts: int
    completed: bool
    planned_duration_sec: int
    elapsed_duration_sec: int
    distance_km: float
    avg_power_watts: float | None
    avg_cadence_rpm: float | None
    avg_speed_kmh: float | None
    power_compliance_pct: float | None
    rpm_compliance_pct: float | None
    both_compliance_pct: float | None
    points: tuple[SessionPoint, ...]


def save_snapshot(snapshot: SessionSnapshot, base_dir: Path | None = None) -> Path:
    target_dir = base_dir or _default_snapshot_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{snapshot.snapshot_id}.json"
    out.write_text(json.dumps(asdict(snapshot), ensure_ascii=True, indent=2), encoding="utf-8")
    return out


def export_snapshot_csv(snapshot: SessionSnapshot, out_dir: Path | None = None) -> Path:
    target_dir = out_dir or _default_snapshot_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{snapshot.snapshot_id}.csv"
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "snapshot_id",
                "workout_name",
                "started_at_utc",
                "ended_at_utc",
                "target_mode",
                "ftp_watts",
                "completed",
                "planned_duration_sec",
                "elapsed_duration_sec",
                "distance_km",
                "avg_power_watts",
                "avg_cadence_rpm",
                "avg_speed_kmh",
                "power_compliance_pct",
                "rpm_compliance_pct",
                "both_compliance_pct",
                "t_label",
                "step_label",
                "expected_power_watts",
                "actual_power_watts",
                "expected_cadence_rpm",
                "actual_cadence_rpm",
                "power_in_zone",
                "cadence_in_zone",
            ]
        )
        for point in snapshot.points:
            writer.writerow(
                [
                    snapshot.snapshot_id,
                    snapshot.workout_name,
                    snapshot.started_at_utc,
                    snapshot.ended_at_utc,
                    snapshot.target_mode,
                    snapshot.ftp_watts,
                    snapshot.completed,
                    snapshot.planned_duration_sec,
                    snapshot.elapsed_duration_sec,
                    snapshot.distance_km,
                    snapshot.avg_power_watts,
                    snapshot.avg_cadence_rpm,
                    snapshot.avg_speed_kmh,
                    snapshot.power_compliance_pct,
                    snapshot.rpm_compliance_pct,
                    snapshot.both_compliance_pct,
                    point.t_label,
                    point.step_label,
                    point.expected_power_watts,
                    point.actual_power_watts,
                    point.expected_cadence_rpm,
                    point.actual_cadence_rpm,
                    point.power_in_zone,
                    point.cadence_in_zone,
                ]
            )
    return out
