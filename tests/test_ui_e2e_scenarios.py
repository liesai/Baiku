from __future__ import annotations

import asyncio

from backend.ble.ftms_client import IndoorBikeData
from backend.ui.controller import UIController
from backend.workout.model import WorkoutPlan, WorkoutStep
from backend.workout.runner import WorkoutProgress


def test_ui_like_start_stop_and_sim_variations() -> None:
    async def _run() -> None:
        controller = UIController(simulate_ht=True)
        samples: list[IndoorBikeData] = []
        progresses: list[WorkoutProgress] = []
        finishes: list[bool] = []

        await controller.connect(target="auto", metrics_callback=lambda m: samples.append(m))

        plan = WorkoutPlan(
            name="E2E Plan",
            steps=(
                WorkoutStep(3, 150, "Warmup", 80, 90),
                WorkoutStep(3, 260, "Build", 95, 105),
            ),
        )

        await controller.start_workout(
            plan,
            target_mode="erg",
            ftp_watts=220,
            on_progress=lambda p: progresses.append(p),
            on_finish=lambda done: finishes.append(done),
        )
        await asyncio.sleep(2.2)
        assert controller.workout_running

        await controller.stop_workout()
        assert finishes[-1] is False

        progresses.clear()
        await controller.start_workout(
            plan,
            target_mode="resistance",
            ftp_watts=220,
            on_progress=lambda p: progresses.append(p),
            on_finish=lambda done: finishes.append(done),
        )
        await asyncio.sleep(plan.total_duration_sec + 0.6)
        assert finishes[-1] is True

        assert any(p.step_label == "Warmup" for p in progresses)
        assert any(p.step_label == "Build" for p in progresses)

        powers = [s.instantaneous_power for s in samples if s.instantaneous_power is not None]
        assert len(powers) > 3
        assert max(powers) - min(powers) >= 8

        await controller.disconnect()

    asyncio.run(_run())
