"""User-defined workout plans stored locally."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from backend.workout.model import WorkoutPlan, WorkoutStep
from backend.workout.parser import load_workout


def _default_workouts_dir() -> Path:
    return Path.home() / ".velox-engine" / "workouts"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "custom-workout"


@dataclass(frozen=True)
class UserWorkout:
    key: str
    name: str
    category: str
    path: Path


def list_user_workouts(base_dir: Path | None = None) -> list[UserWorkout]:
    root = base_dir or _default_workouts_dir()
    if not root.exists():
        return []
    out: list[UserWorkout] = []
    for file in sorted(root.glob("*.json")):
        try:
            payload = json.loads(file.read_text(encoding="utf-8"))
            name = str(payload.get("name", file.stem))
            category = str(payload.get("category", "Custom"))
        except Exception:
            name = file.stem
            category = "Custom"
        out.append(UserWorkout(key=file.stem, name=name, category=category, path=file))
    return out


def load_user_workout(path: Path) -> WorkoutPlan:
    return load_workout(path)


def save_user_workout(
    *,
    name: str,
    category: str,
    steps: list[WorkoutStep],
    base_dir: Path | None = None,
    overwrite_key: str | None = None,
) -> Path:
    if not steps:
        raise ValueError("Workout must include at least one step")
    root = base_dir or _default_workouts_dir()
    root.mkdir(parents=True, exist_ok=True)
    key = overwrite_key or _slugify(name)
    out = root / f"{key}.json"
    payload = {
        "name": name,
        "category": category,
        "steps": [
            {
                "duration_sec": int(step.duration_sec),
                "target_watts": int(step.target_watts),
                "label": step.label,
                "cadence_min_rpm": step.cadence_min_rpm,
                "cadence_max_rpm": step.cadence_max_rpm,
            }
            for step in steps
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return out
