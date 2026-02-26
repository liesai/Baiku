"""Lightweight game layer for workout motivation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GoalKind = Literal["power", "cadence", "both"]


@dataclass(frozen=True)
class GoalDefinition:
    key: str
    title: str
    kind: GoalKind
    target_sec: float
    points: int


@dataclass
class GoalProgress:
    definition: GoalDefinition
    progress_sec: float = 0.0
    completed: bool = False


class GoalTracker:
    def __init__(self, goals: tuple[GoalDefinition, ...]) -> None:
        self._base_goals = goals
        self.goals: list[GoalProgress] = []
        self.current_index: int = 0
        self.score: int = 0
        self.coins: int = 0
        self.streak: int = 0
        self.reset()

    def reset(self) -> None:
        self.goals = [GoalProgress(definition=goal) for goal in self._base_goals]
        self.current_index = 0
        self.score = 0
        self.coins = 0
        self.streak = 0

    @property
    def current_goal(self) -> GoalProgress | None:
        if self.current_index >= len(self.goals):
            return None
        return self.goals[self.current_index]

    def _goal_condition(
        self,
        goal: GoalProgress,
        power_in_zone: bool | None,
        cadence_in_zone: bool | None,
    ) -> bool:
        if goal.definition.kind == "power":
            return power_in_zone is True
        if goal.definition.kind == "cadence":
            return cadence_in_zone is True
        return power_in_zone is True and cadence_in_zone is True

    def update(
        self,
        *,
        power_in_zone: bool | None,
        cadence_in_zone: bool | None,
        dt_sec: float,
    ) -> None:
        goal = self.current_goal
        if goal is None:
            return
        if dt_sec <= 0:
            return

        if self._goal_condition(goal, power_in_zone, cadence_in_zone):
            goal.progress_sec += dt_sec
            self.streak += 1
        else:
            # Keep some progress persistence but punish instability.
            goal.progress_sec = max(0.0, goal.progress_sec - (dt_sec * 0.5))
            self.streak = 0

        if goal.progress_sec >= goal.definition.target_sec:
            goal.completed = True
            self.score += goal.definition.points
            self.coins += max(1, goal.definition.points // 20)
            self.current_index += 1


DEFAULT_GAME_GOALS: tuple[GoalDefinition, ...] = (
    GoalDefinition(
        key="power_hold",
        title="Hold power zone",
        kind="power",
        target_sec=20.0,
        points=100,
    ),
    GoalDefinition(
        key="cadence_hold",
        title="Hold cadence zone",
        kind="cadence",
        target_sec=20.0,
        points=120,
    ),
    GoalDefinition(
        key="combo_hold",
        title="Hold both zones",
        kind="both",
        target_sec=25.0,
        points=180,
    ),
)
