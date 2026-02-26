"""Async controller used by the Linux UI."""

from __future__ import annotations

from typing import Callable

from backend.ble.ftms_client import FTMSClient, IndoorBikeData, ScannedDevice
from backend.workout.model import WorkoutPlan
from backend.workout.runner import TargetMode, WorkoutProgress, WorkoutRunner


class UIController:
    def __init__(self, debug_ftms: bool = False, simulate_ht: bool = False) -> None:
        self._client = FTMSClient(debug_ftms=debug_ftms, simulate_ht=simulate_ht)
        self._runner = WorkoutRunner(self._client)

    async def scan(self) -> list[ScannedDevice]:
        return await self._client.scan(timeout=5.0)

    async def connect(
        self,
        target: str,
        metrics_callback: Callable[[IndoorBikeData], None],
    ) -> str:
        label = await self._client.connect(target=target)

        def _on_metrics(data: IndoorBikeData) -> None:
            metrics_callback(data)

        await self._client.subscribe_indoor_bike_data(_on_metrics)
        return label

    async def disconnect(self) -> None:
        await self.stop_workout()
        await self._client.disconnect()

    async def set_erg(self, watts: int) -> int:
        return await self._client.set_target_power(watts)

    async def start_workout(
        self,
        plan: WorkoutPlan,
        target_mode: TargetMode,
        ftp_watts: int,
        on_progress: Callable[[WorkoutProgress], None],
        on_finish: Callable[[bool], None],
    ) -> None:
        await self._runner.start(plan, target_mode, ftp_watts, on_progress, on_finish)

    async def stop_workout(self) -> None:
        await self._runner.stop()

    @property
    def workout_running(self) -> bool:
        return self._runner.is_running
