"""Local persistence for completed/stopped workout sessions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def _default_sessions_path() -> Path:
    return Path.home() / ".velox-engine" / "sessions.jsonl"


@dataclass(frozen=True)
class SessionRecord:
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


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def append_session(record: SessionRecord, path: Path | None = None) -> None:
    target = path or _default_sessions_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=True) + "\n")


def load_recent_sessions(limit: int = 20, path: Path | None = None) -> list[SessionRecord]:
    target = path or _default_sessions_path()
    if not target.exists():
        return []

    lines = target.read_text(encoding="utf-8").splitlines()
    out: list[SessionRecord] = []
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
            out.append(SessionRecord(**item))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out
