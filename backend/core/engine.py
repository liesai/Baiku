"""Async runtime engine for BLE FTMS streaming in terminal."""

from __future__ import annotations

import asyncio
from datetime import datetime

from backend.ble.ftms_client import FTMSClient, IndoorBikeData
from backend.core.state import EngineState


class VeloxEngine:
    def __init__(
        self,
        ftms_client: FTMSClient | None = None,
        debug_ftms: bool = False,
        simulate_ht: bool = False,
        ble_pair: bool = True,
        startup_wait_seconds: float = 30.0,
    ) -> None:
        self._client = ftms_client or FTMSClient(
            debug_ftms=debug_ftms,
            simulate_ht=simulate_ht,
            ble_pair=ble_pair,
        )
        self.state = EngineState()
        self._stop_event = asyncio.Event()
        self._first_metrics_event = asyncio.Event()
        self._startup_wait_seconds = startup_wait_seconds
        self._deferred_erg_watts: int | None = None

    async def run(self, target: str | None = None, erg_watts: int | None = None) -> None:
        try:
            connected_label = await self._client.connect(target=target)
            self.state.connected_device = connected_label
            print(f"Connected to {connected_label}")

            await self._client.subscribe_indoor_bike_data(self._on_metrics)

            if erg_watts is not None:
                await self._set_erg_with_startup_wait(erg_watts)

            while not self._stop_event.is_set():
                self._print_metrics_line()
                await asyncio.sleep(1)
        finally:
            await self._client.disconnect()

    def stop(self) -> None:
        self._stop_event.set()

    async def _on_metrics(self, metrics: IndoorBikeData) -> None:
        self.state.last_power_watts = metrics.instantaneous_power
        self.state.last_cadence_rpm = metrics.instantaneous_cadence
        self.state.last_update = datetime.utcnow()
        self._first_metrics_event.set()

        if self._deferred_erg_watts is not None:
            watts = self._deferred_erg_watts
            self._deferred_erg_watts = None
            await self._try_set_erg(watts, reason="first trainer signal")

    def _print_metrics_line(self) -> None:
        power = (
            f"{self.state.last_power_watts} W"
            if self.state.last_power_watts is not None
            else "N/A"
        )
        cadence = (
            f"{self.state.last_cadence_rpm:.1f} rpm"
            if self.state.last_cadence_rpm is not None
            else "N/A"
        )
        print(f"Power: {power} | Cadence: {cadence}")

    async def _set_erg_with_startup_wait(self, watts: int) -> None:
        print(
            f"Waiting up to {self._startup_wait_seconds:.0f}s for trainer signal before ERG..."
        )
        try:
            await asyncio.wait_for(
                self._first_metrics_event.wait(),
                timeout=self._startup_wait_seconds,
            )
            await self._try_set_erg(watts, reason="startup trainer signal")
        except TimeoutError:
            self._deferred_erg_watts = watts
            print(
                f"Warning: no trainer signal after {self._startup_wait_seconds:.0f}s. "
                "Will retry ERG on first signal."
            )

    async def _try_set_erg(self, watts: int, reason: str) -> None:
        try:
            applied_watts = await self._client.set_target_power(watts)
            if applied_watts == watts:
                print(f"ERG target set to {applied_watts}W ({reason})")
            else:
                print(
                    f"ERG target set to {applied_watts}W ({reason}, requested {watts}W)"
                )
        except Exception as exc:
            print(
                f"Warning: ERG target {watts}W refused by trainer ({exc}). "
                "Continuing without ERG control."
            )
