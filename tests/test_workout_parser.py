from __future__ import annotations

from pathlib import Path

import pytest

from backend.workout.parser import WorkoutParseError, load_workout


def test_load_workout_json(tmp_path: Path) -> None:
    workout_file = tmp_path / "sample.json"
    workout_file.write_text(
        (
            '{"name":"Tempo","steps":[{"duration_sec":60,"target_watts":120},'
            '{"duration_sec":30,"target_watts":180,"label":"Push"}]}'
        ),
        encoding="utf-8",
    )

    plan = load_workout(workout_file)

    assert plan.name == "Tempo"
    assert len(plan.steps) == 2
    assert plan.total_duration_sec == 90
    assert plan.steps[1].label == "Push"


def test_load_workout_json_with_cadence_objective(tmp_path: Path) -> None:
    workout_file = tmp_path / "cadence.json"
    workout_file.write_text(
        (
            '{"name":"Cadence Drill","steps":[{"duration_sec":120,'
            '"target_watts":180,"cadence_min_rpm":90,"cadence_max_rpm":100}]}'
        ),
        encoding="utf-8",
    )

    plan = load_workout(workout_file)

    assert plan.steps[0].cadence_min_rpm == 90
    assert plan.steps[0].cadence_max_rpm == 100


def test_load_workout_csv(tmp_path: Path) -> None:
    workout_file = tmp_path / "sample.csv"
    workout_file.write_text(
        "duration_sec,target_watts,label\n60,100,warmup\n120,160,tempo\n",
        encoding="utf-8",
    )

    plan = load_workout(workout_file)

    assert plan.name == "sample"
    assert len(plan.steps) == 2
    assert plan.steps[0].target_watts == 100
    assert plan.total_duration_sec == 180


def test_load_workout_invalid_extension(tmp_path: Path) -> None:
    workout_file = tmp_path / "sample.txt"
    workout_file.write_text("hello", encoding="utf-8")

    with pytest.raises(WorkoutParseError):
        load_workout(workout_file)


def test_load_workout_invalid_value(tmp_path: Path) -> None:
    workout_file = tmp_path / "bad.csv"
    workout_file.write_text(
        "duration_sec,target_watts\n0,100\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkoutParseError):
        load_workout(workout_file)
