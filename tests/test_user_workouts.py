from __future__ import annotations

from pathlib import Path

from backend.workout.model import WorkoutStep
from backend.workout.user_workouts import (
    list_user_workouts,
    load_user_workout,
    save_user_workout,
)


def test_save_list_load_user_workout(tmp_path: Path) -> None:
    saved = save_user_workout(
        name="My Build",
        category="Custom",
        steps=(
            WorkoutStep(
                duration_sec=180,
                target_watts=210,
                label="Block 1",
                cadence_min_rpm=85,
                cadence_max_rpm=95,
            ),
        ),
        base_dir=tmp_path,
    )
    assert saved.exists()

    items = list_user_workouts(base_dir=tmp_path)
    assert len(items) == 1
    assert items[0].name == "My Build"

    loaded = load_user_workout(Path(items[0].path))
    assert loaded.name == "My Build"
    assert loaded.steps[0].target_watts == 210
