"""Async controller used by the Linux UI."""

from __future__ import annotations

from typing import Callable

from backend.ble.ftms_client import FTMSClient, IndoorBikeData, ScannedDevice
from backend.workout.model import WorkoutPlan
from backend.workout.runner import TargetMode, WorkoutProgress, WorkoutRunner


class UIController:
    def __init__(
        self,
        debug_ftms: bool = False,
        simulate_ht: bool = False,
        ble_pair: bool = True,
    ) -> None:
        self._client = FTMSClient(
            debug_ftms=debug_ftms,
            simulate_ht=simulate_ht,
            ble_pair=ble_pair,
        )
        self._runner = WorkoutRunner(self._client)
        self._measurement_stream_ready = False

    async def scan(self) -> list[ScannedDevice]:
        return await self._client.scan(timeout=5.0)

    async def connect(
        self,
        target: str,
        metrics_callback: Callable[[IndoorBikeData], None],
        connect_timeout: float = 25.0,
    ) -> str:
        label = await self._client.connect(target=target, timeout=connect_timeout)

        def _on_metrics(data: IndoorBikeData) -> None:
            metrics_callback(data)

        try:
            await self._client.subscribe_indoor_bike_data(_on_metrics)
            self._measurement_stream_ready = True
        except RuntimeError as exc:
            if "No compatible measurement characteristic found" not in str(exc):
                raise
            # Keep control connection alive even when measurement chars are missing.
            self._measurement_stream_ready = False
        return label

    async def disconnect(self) -> None:
        await self.stop_workout()
        await self._client.disconnect()
        self._measurement_stream_ready = False

    async def set_erg(self, watts: int) -> int:
        return await self._client.set_target_power(watts)

    async def probe_erg_support(self) -> bool:
        return await self._client.probe_erg_support()

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

    @property
    def measurement_stream_ready(self) -> bool:
        return self._measurement_stream_ready
