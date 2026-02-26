from __future__ import annotations

import asyncio

from backend.ble.ftms_client import FTMSClient, IndoorBikeData


async def _wait_for_metrics(client: FTMSClient, samples: list[IndoorBikeData]) -> None:
    def on_metrics(data: IndoorBikeData) -> None:
        samples.append(data)

    await client.subscribe_indoor_bike_data(on_metrics)
    await asyncio.sleep(2.2)


def test_simulated_scan_and_connect() -> None:
    async def _run() -> None:
        client = FTMSClient(simulate_ht=True)
        devices = await client.scan()
        assert len(devices) == 1
        assert devices[0].has_ftms

        label = await client.connect(target="auto")
        assert "Velox Sim HT" in label
        assert client.is_connected
        await client.disconnect()

    asyncio.run(_run())


def test_simulated_erg_and_metrics() -> None:
    async def _run() -> None:
        client = FTMSClient(simulate_ht=True)
        await client.connect(target="auto")

        samples: list[IndoorBikeData] = []
        await _wait_for_metrics(client, samples)
        assert len(samples) >= 2

        applied = await client.set_target_power(203)
        assert applied % 5 == 0

        await asyncio.sleep(2.2)
        assert samples[-1].instantaneous_power is not None

        await client.disconnect()

    asyncio.run(_run())


def test_simulated_resistance_and_slope() -> None:
    async def _run() -> None:
        client = FTMSClient(simulate_ht=True)
        await client.connect(target="auto")
        await client.subscribe_indoor_bike_data(lambda _d: None)

        resistance = await client.set_target_resistance(47.2)
        assert resistance > 0

        slope = await client.set_target_slope(5.5)
        assert -10.0 <= slope <= 15.0

        await client.disconnect()

    asyncio.run(_run())
