"""Workout domain models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkoutStep:
    duration_sec: int
    target_watts: int
    label: str | None = None
    cadence_min_rpm: int | None = None
    cadence_max_rpm: int | None = None


@dataclass(frozen=True)
class WorkoutPlan:
    name: str
    steps: tuple[WorkoutStep, ...]

    @property
    def total_duration_sec(self) -> int:
        return sum(step.duration_sec for step in self.steps)
