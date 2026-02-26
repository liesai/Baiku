"""Workout execution against an FTMS trainer in ERG mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Literal, Optional

from backend.ble.ftms_client import FTMSClient
from backend.workout.model import WorkoutPlan, WorkoutStep


TargetMode = Literal["erg", "resistance", "slope"]


@dataclass(frozen=True)
class WorkoutProgress:
    step_index: int
    step_total: int
    step_label: str
    target_watts: int
    target_mode: TargetMode
    target_display_value: float
    target_display_unit: str
    expected_power_min_watts: int | None
    expected_power_max_watts: int | None
    expected_cadence_min_rpm: int | None
    expected_cadence_max_rpm: int | None
    step_duration_sec: int
    step_elapsed_sec: int
    remaining_sec: int
    elapsed_total_sec: int
    total_duration_sec: int
    total_remaining_sec: int


ProgressCallback = Callable[[WorkoutProgress], None]
FinishCallback = Callable[[bool], None]


class WorkoutRunner:
    def __init__(self, client: FTMSClient) -> None:
        self._client = client
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        plan: WorkoutPlan,
        target_mode: TargetMode,
        ftp_watts: int,
        on_progress: ProgressCallback,
        on_finish: FinishCallback,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Workout already running")

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(plan, target_mode, ftp_watts, on_progress, on_finish)
        )

    async def stop(self) -> None:
        if not self.is_running:
            return

        self._stop_event.set()
        assert self._task is not None
        await self._task
        self._task = None

    async def _run(
        self,
        plan: WorkoutPlan,
        target_mode: TargetMode,
        ftp_watts: int,
        on_progress: ProgressCallback,
        on_finish: FinishCallback,
    ) -> None:
        completed = False
        elapsed_offset = 0
        try:
            for index, step in enumerate(plan.steps, start=1):
                if self._stop_event.is_set():
                    break

                target_value, target_unit = await self._apply_step_target(
                    step,
                    target_mode=target_mode,
                    ftp_watts=ftp_watts,
                )
                await self._countdown_step(
                    step=step,
                    target_mode=target_mode,
                    target_value=target_value,
                    target_unit=target_unit,
                    step_index=index,
                    step_total=len(plan.steps),
                    elapsed_offset_sec=elapsed_offset,
                    total_duration_sec=plan.total_duration_sec,
                    on_progress=on_progress,
                )
                elapsed_offset += step.duration_sec

            completed = not self._stop_event.is_set()
        finally:
            on_finish(completed)

    async def _apply_step_target(
        self,
        step: WorkoutStep,
        *,
        target_mode: TargetMode,
        ftp_watts: int,
    ) -> tuple[float, str]:
        attempts = 0
        last_exc: Exception | None = None
        while attempts < 3 and not self._stop_event.is_set():
            attempts += 1
            try:
                if target_mode == "erg":
                    applied_watts = await self._client.set_target_power(step.target_watts)
                    return float(applied_watts), "W"
                if target_mode == "resistance":
                    level = _watts_to_resistance(step.target_watts, ftp_watts)
                    applied_resistance = await self._client.set_target_resistance(level)
                    return float(applied_resistance), "%"
                slope = _watts_to_slope(step.target_watts, ftp_watts)
                applied_slope = await self._client.set_target_slope(slope)
                return float(applied_slope), "%"
            except Exception as exc:  # pragma: no cover - BLE runtime variability
                last_exc = exc
                await asyncio.sleep(1.0)

        if last_exc is not None:
            raise RuntimeError(f"Unable to apply {target_mode} target") from last_exc
        raise RuntimeError(f"Unable to apply {target_mode} target")

    async def _countdown_step(
        self,
        *,
        step: WorkoutStep,
        target_mode: TargetMode,
        target_value: float,
        target_unit: str,
        step_index: int,
        step_total: int,
        elapsed_offset_sec: int,
        total_duration_sec: int,
        on_progress: ProgressCallback,
    ) -> None:
        label = step.label or f"Step {step_index}"
        for remaining in range(step.duration_sec, 0, -1):
            if self._stop_event.is_set():
                return
            step_elapsed = (step.duration_sec - remaining) + 1
            elapsed_total = elapsed_offset_sec + step_elapsed

            on_progress(
                WorkoutProgress(
                    step_index=step_index,
                    step_total=step_total,
                    step_label=label,
                    target_watts=step.target_watts,
                    target_mode=target_mode,
                    target_display_value=target_value,
                    target_display_unit=target_unit,
                    expected_power_min_watts=_expected_power_min(step.target_watts),
                    expected_power_max_watts=_expected_power_max(step.target_watts),
                    expected_cadence_min_rpm=step.cadence_min_rpm,
                    expected_cadence_max_rpm=step.cadence_max_rpm,
                    step_duration_sec=step.duration_sec,
                    step_elapsed_sec=step_elapsed,
                    remaining_sec=remaining,
                    elapsed_total_sec=elapsed_total,
                    total_duration_sec=total_duration_sec,
                    total_remaining_sec=max(0, total_duration_sec - elapsed_total),
                )
            )
            await asyncio.sleep(1.0)


def _watts_to_resistance(target_watts: int, ftp_watts: int) -> float:
    safe_ftp = max(100, ftp_watts)
    return max(1.0, min(200.0, (target_watts / safe_ftp) * 100.0))


def _watts_to_slope(target_watts: int, ftp_watts: int) -> float:
    safe_ftp = max(100, ftp_watts)
    return max(-10.0, min(15.0, (target_watts - safe_ftp) / 20.0))


def _expected_power_min(target_watts: int) -> int:
    return max(1, int(round(target_watts * 0.95)))


def _expected_power_max(target_watts: int) -> int:
    return max(1, int(round(target_watts * 1.05)))
