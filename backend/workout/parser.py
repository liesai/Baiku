"""Workout file parser (CSV/JSON)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from backend.workout.model import WorkoutPlan, WorkoutStep


class WorkoutParseError(ValueError):
    """Raised when a workout file is invalid."""


def load_workout(path: str | Path) -> WorkoutPlan:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        return _load_json(file_path)
    if suffix == ".csv":
        return _load_csv(file_path)
    raise WorkoutParseError(
        f"Unsupported workout format '{file_path.suffix}'. Use .json or .csv"
    )


def _load_json(path: Path) -> WorkoutPlan:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkoutParseError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise WorkoutParseError("Workout JSON must be an object")

    name_obj = data.get("name", path.stem)
    if not isinstance(name_obj, str):
        raise WorkoutParseError("Workout field 'name' must be a string")

    steps_obj = data.get("steps")
    if not isinstance(steps_obj, list):
        raise WorkoutParseError("Workout field 'steps' must be an array")

    steps: list[WorkoutStep] = []
    for i, raw in enumerate(steps_obj):
        if not isinstance(raw, dict):
            raise WorkoutParseError(f"Step {i + 1}: must be an object")
        steps.append(
            _build_step(
                duration_obj=raw.get("duration_sec"),
                watts_obj=raw.get("target_watts"),
                label_obj=raw.get("label"),
                cadence_min_obj=raw.get("cadence_min_rpm"),
                cadence_max_obj=raw.get("cadence_max_rpm"),
                index=i,
            )
        )

    return _build_plan(name=name_obj.strip() or path.stem, steps=steps)


def _load_csv(path: Path) -> WorkoutPlan:
    rows: list[WorkoutStep] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        required = {"duration_sec", "target_watts"}
        if not required.issubset(fields):
            raise WorkoutParseError(
                "CSV must contain headers: duration_sec,target_watts[,label,"
                "cadence_min_rpm,cadence_max_rpm]"
            )

        for i, row in enumerate(reader):
            rows.append(
                _build_step(
                    duration_obj=row.get("duration_sec"),
                    watts_obj=row.get("target_watts"),
                    label_obj=row.get("label"),
                    cadence_min_obj=row.get("cadence_min_rpm"),
                    cadence_max_obj=row.get("cadence_max_rpm"),
                    index=i,
                )
            )

    return _build_plan(name=path.stem, steps=rows)


def _build_step(
    *,
    duration_obj: object,
    watts_obj: object,
    label_obj: object,
    cadence_min_obj: object,
    cadence_max_obj: object,
    index: int,
) -> WorkoutStep:
    duration_sec = _parse_int_field(
        raw=duration_obj,
        field_name="duration_sec",
        index=index,
    )
    target_watts = _parse_int_field(
        raw=watts_obj,
        field_name="target_watts",
        index=index,
    )

    if duration_sec <= 0:
        raise WorkoutParseError(f"Step {index + 1}: duration_sec must be > 0")
    if target_watts <= 0:
        raise WorkoutParseError(f"Step {index + 1}: target_watts must be > 0")

    label: str | None
    if label_obj is None:
        label = None
    else:
        label = str(label_obj).strip() or None

    cadence_min_rpm = _parse_optional_int_field(
        raw=cadence_min_obj,
        field_name="cadence_min_rpm",
        index=index,
    )
    cadence_max_rpm = _parse_optional_int_field(
        raw=cadence_max_obj,
        field_name="cadence_max_rpm",
        index=index,
    )
    if cadence_min_rpm is not None and cadence_min_rpm <= 0:
        raise WorkoutParseError(f"Step {index + 1}: cadence_min_rpm must be > 0")
    if cadence_max_rpm is not None and cadence_max_rpm <= 0:
        raise WorkoutParseError(f"Step {index + 1}: cadence_max_rpm must be > 0")
    if (
        cadence_min_rpm is not None
        and cadence_max_rpm is not None
        and cadence_min_rpm > cadence_max_rpm
    ):
        raise WorkoutParseError(
            f"Step {index + 1}: cadence_min_rpm must be <= cadence_max_rpm"
        )

    return WorkoutStep(
        duration_sec=duration_sec,
        target_watts=target_watts,
        label=label,
        cadence_min_rpm=cadence_min_rpm,
        cadence_max_rpm=cadence_max_rpm,
    )


def _build_plan(*, name: str, steps: list[WorkoutStep]) -> WorkoutPlan:
    if not steps:
        raise WorkoutParseError("Workout must contain at least one step")
    return WorkoutPlan(name=name, steps=tuple(steps))


def _parse_int_field(*, raw: object, field_name: str, index: int) -> int:
    if raw is None:
        raise WorkoutParseError(f"Step {index + 1}: invalid {field_name}")
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise WorkoutParseError(f"Step {index + 1}: invalid {field_name}") from exc


def _parse_optional_int_field(
    *, raw: object, field_name: str, index: int
) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    return _parse_int_field(raw=raw, field_name=field_name, index=index)
