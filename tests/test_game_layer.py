from __future__ import annotations

from backend.ui.game_layer import DEFAULT_GAME_GOALS, GoalTracker


def test_goal_tracker_progress_and_completion() -> None:
    tracker = GoalTracker(DEFAULT_GAME_GOALS)

    assert tracker.current_goal is not None
    assert tracker.current_goal.definition.kind == "power"

    for _ in range(20):
        tracker.update(power_in_zone=True, cadence_in_zone=False, dt_sec=1.0)

    assert tracker.score >= 100
    assert tracker.current_goal is not None
    assert tracker.current_goal.definition.kind == "cadence"


def test_goal_tracker_decay_when_outside_zone() -> None:
    tracker = GoalTracker(DEFAULT_GAME_GOALS)
    tracker.update(power_in_zone=True, cadence_in_zone=False, dt_sec=6.0)
    assert tracker.current_goal is not None
    assert tracker.current_goal.progress_sec > 0

    tracker.update(power_in_zone=False, cadence_in_zone=False, dt_sec=2.0)
    assert tracker.current_goal is not None
    assert tracker.current_goal.progress_sec < 6.0
