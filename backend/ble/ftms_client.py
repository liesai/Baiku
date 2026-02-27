"""Async FTMS BLE client for trainers like Elite Direto XRT."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import math
import random
import struct
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.ble.constants import (
    CYCLING_POWER_MEASUREMENT_CHAR_UUID,
    FITNESS_MACHINE_CONTROL_POINT_CHAR_UUID,
    FTMS_SERVICE_UUID,
    INDOOR_BIKE_DATA_CHAR_UUID,
    OP_REQUEST_CONTROL,
    OP_START_RESUME,
    OP_SET_TARGET_POWER,
    SUPPORTED_POWER_RANGE_CHAR_UUID,
    parse_indoor_bike_flags,
)

_bleak: Any
try:
    _bleak = importlib.import_module("bleak")
except ImportError:  # pragma: no cover - runtime dependency guard
    _bleak = None


MetricsCallback = Callable[["IndoorBikeData"], Awaitable[None] | None]

_BLE_COMPANY_IDS: dict[int, str] = {
    0x004C: "Apple",
    0x0006: "Microsoft",
    0x000F: "Broadcom",
    0x0075: "Samsung",
    0x0087: "Garmin",
    0x00D2: "Wahoo Fitness",
    0x011F: "Tacx",
    0x04D8: "Elite",
}

_BRAND_HINTS: tuple[tuple[str, str], ...] = (
    ("wahoo", "Wahoo Fitness"),
    ("kicker", "Wahoo Fitness"),
    ("kickr", "Wahoo Fitness"),
    ("elite", "Elite"),
    ("direto", "Elite"),
    ("suito", "Elite"),
    ("tacx", "Tacx"),
    ("garmin", "Garmin"),
    ("saris", "Saris"),
    ("stages", "Stages"),
    ("zwift", "Zwift"),
)


def _resolve_manufacturer(
    name: str, manufacturer_data: Any | None
) -> str | None:
    if isinstance(manufacturer_data, dict) and manufacturer_data:
        for key in sorted(manufacturer_data.keys()):
            if isinstance(key, int):
                return _BLE_COMPANY_IDS.get(key, f"MFG 0x{key:04X}")
    lowered = name.lower()
    for hint, brand in _BRAND_HINTS:
        if hint in lowered:
            return brand
    return None


@dataclass(frozen=True)
class ScannedDevice:
    name: str
    address: str
    rssi: int
    has_ftms: bool
    manufacturer: str | None = None


@dataclass(frozen=True)
class IndoorBikeData:
    instantaneous_power: Optional[int] = None
    instantaneous_cadence: Optional[float] = None
    instantaneous_speed_kmh: Optional[float] = None


def _ensure_bleak_available() -> None:
    if _bleak is None:
        raise RuntimeError(
            "bleak is not installed. Run: pip install -r requirements.txt"
        )


def _require_bytes(data: bytes, cursor: int, size: int) -> None:
    if cursor + size > len(data):
        raise ValueError(
            f"Invalid Indoor Bike Data payload: expected {size} bytes at offset {cursor}"
        )


def _decode_indoor_bike_data(
    payload: bytes, *, speed_present: bool
) -> tuple[IndoorBikeData, int]:
    raw_flags = struct.unpack_from("<H", payload, 0)[0]
    flags = parse_indoor_bike_flags(raw_flags)
    cursor = 2

    speed_kmh: Optional[float] = None
    if speed_present:
        _require_bytes(payload, cursor, 2)
        raw_speed = struct.unpack_from("<H", payload, cursor)[0]
        speed_kmh = raw_speed / 100.0
        cursor += 2

    if flags.average_speed_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    cadence: Optional[float] = None
    if flags.instantaneous_cadence_present:
        _require_bytes(payload, cursor, 2)
        raw_cadence = struct.unpack_from("<H", payload, cursor)[0]
        cadence = raw_cadence / 2.0
        cursor += 2

    if flags.average_cadence_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    if flags.total_distance_present:
        _require_bytes(payload, cursor, 3)
        cursor += 3

    if flags.resistance_level_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    power: Optional[int] = None
    if flags.instantaneous_power_present:
        _require_bytes(payload, cursor, 2)
        power = struct.unpack_from("<h", payload, cursor)[0]
        cursor += 2

    if flags.average_power_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    if flags.expended_energy_present:
        _require_bytes(payload, cursor, 5)
        cursor += 5

    if flags.heart_rate_present:
        _require_bytes(payload, cursor, 1)
        cursor += 1

    if flags.metabolic_equivalent_present:
        _require_bytes(payload, cursor, 1)
        cursor += 1

    if flags.elapsed_time_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    if flags.remaining_time_present:
        _require_bytes(payload, cursor, 2)
        cursor += 2

    return IndoorBikeData(
        instantaneous_power=power,
        instantaneous_cadence=cadence,
        instantaneous_speed_kmh=speed_kmh,
    ), cursor


def _plausibility_score(metrics: IndoorBikeData) -> int:
    score = 0
    if metrics.instantaneous_cadence is not None:
        cadence = metrics.instantaneous_cadence
        if cadence < 0 or cadence > 220:
            score += 1000
    if metrics.instantaneous_power is not None:
        power = metrics.instantaneous_power
        if power < -200 or power > 3000:
            score += 1000
    if metrics.instantaneous_speed_kmh is not None:
        speed = metrics.instantaneous_speed_kmh
        if speed < 0 or speed > 130:
            score += 1000
    return score


def parse_indoor_bike_data(payload: bytes) -> IndoorBikeData:
    """Parse FTMS Indoor Bike Data characteristic payload (0x2AD2)."""
    if len(payload) < 2:
        raise ValueError("Indoor Bike Data payload too short")

    raw_flags = struct.unpack_from("<H", payload, 0)[0]
    flags = parse_indoor_bike_flags(raw_flags)
    # Most devices follow the spec: speed present when "more_data" is false.
    # Some devices are inconsistent in the wild, so try both alignments.
    preferred_speed_present = not flags.more_data
    candidates: list[tuple[int, int, IndoorBikeData]] = []
    errors: list[Exception] = []

    for speed_present in (preferred_speed_present, not preferred_speed_present):
        try:
            metrics, cursor = _decode_indoor_bike_data(
                payload, speed_present=speed_present
            )
            score = _plausibility_score(metrics)
            trailing_bytes = len(payload) - cursor
            candidates.append((score, trailing_bytes, metrics))
        except ValueError as exc:
            errors.append(exc)

    if not candidates:
        raise errors[0]

    # Prefer plausible values, then tighter decode (fewer trailing bytes).
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def normalize_power_target(
    requested_watts: int, min_watts: int, max_watts: int, increment_watts: int
) -> int:
    if increment_watts <= 0:
        increment_watts = 1

    clamped = min(max(requested_watts, min_watts), max_watts)
    steps = round((clamped - min_watts) / increment_watts)
    normalized = min_watts + (steps * increment_watts)
    return min(max(normalized, min_watts), max_watts)


class FTMSClient:
    """Thin async BLE FTMS client with scan/connect/subscribe/control operations."""

    def __init__(
        self,
        debug_ftms: bool = False,
        simulate_ht: bool = False,
        ble_pair: bool = True,
    ) -> None:
        self._client: Optional[Any] = None
        self._metrics_callback: Optional[MetricsCallback] = None
        self._debug_ftms = debug_ftms
        self._simulate_ht = simulate_ht
        self._ble_pair = ble_pair
        self._last_ftms_power: Optional[int] = None
        self._last_ftms_cadence: Optional[float] = None
        self._last_ftms_speed: Optional[float] = None
        self._last_cycling_power: Optional[int] = None
        self._last_cycling_cadence: Optional[float] = None
        self._last_crank_revs: Optional[int] = None
        self._last_crank_event_time: Optional[int] = None
        self._supported_power_range: Optional[tuple[int, int, int]] = None
        self._control_point_indications_enabled = False
        self._sim_connected = False
        self._sim_target_watts = 120
        self._sim_power = 100.0
        self._sim_cadence = 85.0
        self._sim_speed = 28.0
        self._sim_task: Optional[asyncio.Task[None]] = None
        self._sim_rng = random.Random(20260225)
        self._sim_tick = 0
        self._sim_mode: str = "steady"
        self._sim_mode_remaining = 0
        self._scan_cache: dict[str, Any] = {}
        self._services_discovered = False

    @property
    def is_connected(self) -> bool:
        if self._simulate_ht:
            return self._sim_connected
        return bool(self._client and self._client.is_connected)

    async def scan(self, timeout: float = 5.0) -> list[ScannedDevice]:
        if self._simulate_ht:
            return [
                ScannedDevice(
                    name="Velox Sim HT",
                    address="SIM:HT:00:00:00:01",
                    rssi=-30,
                    has_ftms=True,
                    manufacturer="Velox",
                )
            ]
        _ensure_bleak_available()
        discovered = await _bleak.BleakScanner.discover(
            timeout=timeout, return_adv=True
        )
        devices: list[ScannedDevice] = []
        self._scan_cache = {}

        for _, (device, adv_data) in discovered.items():
            uuids = {u.lower() for u in (adv_data.service_uuids or [])}
            has_ftms = FTMS_SERVICE_UUID in uuids
            self._scan_cache[device.address.lower()] = device
            manufacturer = _resolve_manufacturer(
                device.name or "",
                getattr(adv_data, "manufacturer_data", None),
            )
            devices.append(
                ScannedDevice(
                    name=device.name or "Unknown",
                    address=device.address,
                    rssi=adv_data.rssi,
                    has_ftms=has_ftms,
                    manufacturer=manufacturer,
                )
            )

        devices.sort(key=lambda d: d.rssi, reverse=True)
        return devices

    async def connect(self, target: Optional[str] = None, timeout: float = 25.0) -> str:
        """Connect to a specific BLE address/name, or the first FTMS capable device."""
        if self._simulate_ht:
            self._sim_connected = True
            return "Velox Sim HT (SIM:HT:00:00:00:01)"

        _ensure_bleak_available()

        device = await self._resolve_device(target=target, timeout=timeout)
        # Fallback: some trainers are powered on but not advertising continuously.
        # BleakClient accepts direct address string with BlueZ on Linux.
        if device is None and target and target != "auto":
            device = target
        if device is None:
            raise RuntimeError("No FTMS device found")

        client = self._build_bleak_client(device, pair=self._ble_pair)
        try:
            await client.connect(timeout=timeout)
        except Exception:
            # Some platforms/backends/devices don't support pairing from API.
            # Retry transparently without the pairing request.
            if not self._ble_pair:
                raise
            with contextlib.suppress(Exception):
                await client.disconnect()
            client = self._build_bleak_client(device, pair=False)
            await client.connect(timeout=timeout)
        self._client = client
        await self._ensure_services_discovered()

        if isinstance(device, str):
            return f"Unknown ({device})"
        return f"{device.name or 'Unknown'} ({device.address})"

    def _build_bleak_client(self, device: Any, *, pair: bool) -> Any:
        if pair:
            try:
                return _bleak.BleakClient(device, pair=True)
            except TypeError:
                if self._debug_ftms:
                    print("[BLE] pair=True unsupported by current backend, using plain connect")
        return _bleak.BleakClient(device)

    async def disconnect(self) -> None:
        if self._simulate_ht:
            self._sim_connected = False
            if self._sim_task is not None:
                self._sim_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._sim_task
                self._sim_task = None
            return

        if self._client:
            await self._client.disconnect()
            self._client = None
            self._services_discovered = False

    async def subscribe_indoor_bike_data(self, callback: MetricsCallback) -> None:
        if self._simulate_ht:
            if not self._sim_connected:
                raise RuntimeError("Not connected")
            self._metrics_callback = callback
            if self._sim_task is None or self._sim_task.done():
                self._sim_task = asyncio.create_task(self._simulation_loop())
            return

        if not self._client:
            raise RuntimeError("Not connected")

        await self._ensure_services_discovered()
        self._metrics_callback = callback
        subscribed_any = False

        try:
            await self._client.start_notify(
                INDOOR_BIKE_DATA_CHAR_UUID,
                self._handle_indoor_bike_data_notification,
            )
            subscribed_any = True
            if self._debug_ftms:
                print("[FTMS] subscribed to Indoor Bike Data (0x2AD2)")
        except Exception as exc:  # pragma: no cover - optional BLE characteristic
            if self._debug_ftms:
                print(f"[FTMS] Indoor Bike Data unavailable: {exc}")

        try:
            await self._client.start_notify(
                CYCLING_POWER_MEASUREMENT_CHAR_UUID,
                self._handle_cycling_power_measurement_notification,
            )
            subscribed_any = True
            if self._debug_ftms:
                print("[CPM] subscribed to Cycling Power Measurement (0x2A63)")
        except Exception as exc:  # pragma: no cover - optional BLE characteristic
            if self._debug_ftms:
                print(f"[CPM] unavailable: {exc}")

        if not subscribed_any:
            raise RuntimeError(
                "No compatible measurement characteristic found "
                "(expected 0x2AD2 and/or 0x2A63)"
            )

    async def set_target_power(self, watts: int) -> int:
        """Set fixed ERG target power through FTMS Control Point."""
        if self._simulate_ht:
            min_watts, max_watts, inc = (50, 1200, 5)
            applied = normalize_power_target(watts, min_watts, max_watts, inc)
            self._sim_target_watts = applied
            if self._debug_ftms:
                print(f"[SIM-HT] target request={watts}W applied={applied}W")
            return applied

        if not self._client:
            raise RuntimeError("Not connected")

        await self._ensure_services_discovered()
        await self._ensure_control_point_indications()
        target_watts = await self._normalize_target_power(watts)
        request_control = bytes([OP_REQUEST_CONTROL])
        start_resume = bytes([OP_START_RESUME])
        set_target_power = bytes([OP_SET_TARGET_POWER]) + struct.pack("<h", target_watts)

        errors: list[Exception] = []
        sequences = [
            [request_control, start_resume, set_target_power],
            [request_control, set_target_power],
            [set_target_power],
        ]

        for sequence in sequences:
            try:
                for i, command in enumerate(sequence):
                    await self._client.write_gatt_char(
                        FITNESS_MACHINE_CONTROL_POINT_CHAR_UUID,
                        command,
                        response=True,
                    )
                    if i < len(sequence) - 1:
                        await asyncio.sleep(0.05)
                return target_watts
            except Exception as exc:  # pragma: no cover - BLE runtime variability
                errors.append(exc)

        raise RuntimeError(
            f"Unable to set ERG target to {target_watts}W via FTMS Control Point"
        ) from errors[-1]

    async def probe_erg_support(self) -> bool:
        """Best-effort check that FTMS control point accepts ERG control commands."""
        if self._simulate_ht:
            return True
        if not self._client:
            return False

        try:
            await self._ensure_services_discovered()
            await self._ensure_control_point_indications()
            await self._client.write_gatt_char(
                FITNESS_MACHINE_CONTROL_POINT_CHAR_UUID,
                bytes([OP_REQUEST_CONTROL]),
                response=True,
            )
            return True
        except Exception:  # pragma: no cover - BLE runtime variability
            return False

    async def set_target_resistance(self, level: float) -> float:
        if self._simulate_ht:
            normalized = max(1.0, min(level, 200.0))
            self._sim_target_watts = int(round(70 + (normalized * 3.0)))
            if self._debug_ftms:
                print(
                    f"[SIM-HT] resistance level request={level:.1f} "
                    f"applied={normalized:.1f}"
                )
            return normalized

        raise NotImplementedError(
            "Resistance mode is not implemented for real FTMS trainer yet. "
            "Use ERG mode on hardware."
        )

    async def set_target_slope(self, percent: float) -> float:
        if self._simulate_ht:
            normalized = max(-10.0, min(percent, 15.0))
            self._sim_target_watts = int(round(180 + (normalized * 18.0)))
            if self._debug_ftms:
                print(
                    f"[SIM-HT] slope request={percent:.1f}% "
                    f"applied={normalized:.1f}%"
                )
            return normalized

        raise NotImplementedError(
            "Slope mode is not implemented for real FTMS trainer yet. "
            "Use ERG mode on hardware."
        )

    async def _ensure_control_point_indications(self) -> None:
        if not self._client:
            return
        await self._ensure_services_discovered()
        if self._control_point_indications_enabled:
            return
        try:
            await self._client.start_notify(
                FITNESS_MACHINE_CONTROL_POINT_CHAR_UUID,
                self._handle_control_point_indication,
            )
            self._control_point_indications_enabled = True
            if self._debug_ftms:
                print("[FTMS] control point indications enabled (0x2AD9)")
        except Exception as exc:  # pragma: no cover - BLE runtime variability
            if self._debug_ftms:
                print(f"[FTMS] control point indications unavailable: {exc}")

    def _handle_control_point_indication(
        self, _sender: object, data: bytearray
    ) -> None:
        payload = bytes(data)
        if not self._debug_ftms:
            return
        if len(payload) >= 3 and payload[0] == 0x80:
            req_opcode = payload[1]
            result_code = payload[2]
            print(
                f"[FTMS-CP] response req=0x{req_opcode:02X} result=0x{result_code:02X} "
                f"payload={payload.hex(' ')}"
            )
            return
        print(f"[FTMS-CP] indication payload={payload.hex(' ')}")

    async def _normalize_target_power(self, watts: int) -> int:
        supported_power_range = await self._read_supported_power_range()
        if supported_power_range is None:
            return watts

        min_watts, max_watts, increment_watts = supported_power_range
        normalized = normalize_power_target(
            requested_watts=watts,
            min_watts=min_watts,
            max_watts=max_watts,
            increment_watts=increment_watts,
        )
        if self._debug_ftms and normalized != watts:
            print(
                f"[FTMS] adjusted ERG target {watts}W -> {normalized}W "
                f"(range {min_watts}-{max_watts}W, step {increment_watts}W)"
            )
        return normalized

    async def _read_supported_power_range(self) -> Optional[tuple[int, int, int]]:
        if self._simulate_ht:
            return (50, 1200, 5)

        if self._supported_power_range is not None:
            return self._supported_power_range

        if not self._client:
            return None

    async def _ensure_services_discovered(self) -> None:
        if self._simulate_ht:
            return
        if not self._client:
            return
        if self._services_discovered:
            return
        if hasattr(self._client, "get_services"):
            await self._client.get_services()
        else:
            _ = self._client.services
        self._services_discovered = True

        try:
            raw = bytes(
                await self._client.read_gatt_char(SUPPORTED_POWER_RANGE_CHAR_UUID)
            )
        except Exception as exc:  # pragma: no cover - optional BLE characteristic
            if self._debug_ftms:
                print(f"[FTMS] supported power range unavailable: {exc}")
            return None

        if len(raw) < 6:
            if self._debug_ftms:
                print(f"[FTMS] supported power range payload too short: {raw.hex(' ')}")
            return None

        min_watts, max_watts, increment_watts = struct.unpack_from("<hhH", raw, 0)
        if increment_watts <= 0:
            increment_watts = 1

        self._supported_power_range = (min_watts, max_watts, increment_watts)
        if self._debug_ftms:
            print(
                f"[FTMS] supported power range: min={min_watts}W "
                f"max={max_watts}W step={increment_watts}W"
            )
        return self._supported_power_range

    async def _resolve_device(
        self, target: Optional[str], timeout: float
    ) -> Optional[Any]:
        if target and target != "auto":
            cached = self._scan_cache.get(target.lower())
            if cached is not None:
                return cached
            device = await _bleak.BleakScanner.find_device_by_filter(
                lambda d, _: (d.address.lower() == target.lower())
                or ((d.name or "").lower() == target.lower()),
                timeout=timeout,
            )
            return device

        discovered = await _bleak.BleakScanner.discover(
            timeout=timeout, return_adv=True
        )
        for _, (device, adv_data) in discovered.items():
            uuids = {u.lower() for u in (adv_data.service_uuids or [])}
            if FTMS_SERVICE_UUID in uuids:
                return device
        return None

    def _handle_indoor_bike_data_notification(
        self, _sender: object, data: bytearray
    ) -> None:
        payload = bytes(data)
        metrics = parse_indoor_bike_data(payload)
        self._last_ftms_power = metrics.instantaneous_power
        self._last_ftms_cadence = metrics.instantaneous_cadence
        self._last_ftms_speed = metrics.instantaneous_speed_kmh
        if self._debug_ftms:
            raw_flags = struct.unpack_from("<H", payload, 0)[0] if len(payload) >= 2 else 0
            flags = parse_indoor_bike_flags(raw_flags)
            flags_repr = ",".join(
                name for name, enabled in asdict(flags).items() if enabled
            ) or "-"
            preferred_speed_present = not flags.more_data
            candidates_repr: list[str] = []
            for speed_present in (preferred_speed_present, not preferred_speed_present):
                try:
                    candidate_metrics, consumed = _decode_indoor_bike_data(
                        payload, speed_present=speed_present
                    )
                    score = _plausibility_score(candidate_metrics)
                    candidates_repr.append(
                        "speed="
                        + ("yes" if speed_present else "no")
                        + f"/score={score}/used={consumed}"
                        + f"/p={candidate_metrics.instantaneous_power}"
                        + f"/c={candidate_metrics.instantaneous_cadence}"
                    )
                except ValueError as exc:
                    candidates_repr.append(
                        "speed="
                        + ("yes" if speed_present else "no")
                        + f"/err={exc}"
                    )
            print(
                f"[FTMS] flags=0x{raw_flags:04X} [{flags_repr}] "
                f"payload={payload.hex(' ')} "
                f"parsed_power={metrics.instantaneous_power} "
                f"parsed_cadence={metrics.instantaneous_cadence} "
                f"parsed_speed={metrics.instantaneous_speed_kmh} "
                f"candidates={' | '.join(candidates_repr)}"
            )
        self._publish_merged_metrics()

    def _handle_cycling_power_measurement_notification(
        self, _sender: object, data: bytearray
    ) -> None:
        payload = bytes(data)
        power, cadence = self._parse_cycling_power_measurement(payload)
        self._last_cycling_power = power
        if cadence is not None:
            self._last_cycling_cadence = cadence

        if self._debug_ftms:
            print(
                f"[CPM] payload={payload.hex(' ')} "
                f"power={power} cadence={cadence}"
            )

        self._publish_merged_metrics()

    def _publish_merged_metrics(self) -> None:
        if self._metrics_callback is None:
            return

        power = (
            self._last_ftms_power
            if self._last_ftms_power is not None
            else self._last_cycling_power
        )

        cadence = self._last_ftms_cadence
        if self._last_cycling_cadence is not None:
            cadence = self._last_cycling_cadence
        elif cadence is None or cadence == 0.0:
            cadence = self._last_cycling_cadence

        merged = IndoorBikeData(
            instantaneous_power=power,
            instantaneous_cadence=cadence,
            instantaneous_speed_kmh=self._last_ftms_speed,
        )
        maybe_coro = self._metrics_callback(merged)
        if asyncio.iscoroutine(maybe_coro):
            asyncio.create_task(maybe_coro)

    def _parse_cycling_power_measurement(
        self, payload: bytes
    ) -> tuple[Optional[int], Optional[float]]:
        if len(payload) < 4:
            return None, None

        flags = struct.unpack_from("<H", payload, 0)[0]
        power = struct.unpack_from("<h", payload, 2)[0]
        cursor = 4

        pedal_power_balance_present = bool(flags & (1 << 0))
        accumulated_torque_present = bool(flags & (1 << 2))
        wheel_revolution_data_present = bool(flags & (1 << 4))
        crank_revolution_data_present = bool(flags & (1 << 5))
        extreme_force_magnitudes_present = bool(flags & (1 << 6))
        extreme_torque_magnitudes_present = bool(flags & (1 << 7))
        extreme_angles_present = bool(flags & (1 << 8))
        top_dead_spot_angle_present = bool(flags & (1 << 9))
        bottom_dead_spot_angle_present = bool(flags & (1 << 10))
        accumulated_energy_present = bool(flags & (1 << 11))

        if pedal_power_balance_present:
            if cursor + 1 > len(payload):
                return power, None
            cursor += 1

        if accumulated_torque_present:
            if cursor + 2 > len(payload):
                return power, None
            cursor += 2

        if wheel_revolution_data_present:
            if cursor + 6 > len(payload):
                return power, None
            cursor += 6

        cadence: Optional[float] = None
        if crank_revolution_data_present:
            if cursor + 4 > len(payload):
                return power, None

            crank_revs = struct.unpack_from("<H", payload, cursor)[0]
            crank_event_time = struct.unpack_from("<H", payload, cursor + 2)[0]
            cursor += 4

            if (
                self._last_crank_revs is not None
                and self._last_crank_event_time is not None
            ):
                delta_revs = (crank_revs - self._last_crank_revs) & 0xFFFF
                delta_time_ticks = (
                    crank_event_time - self._last_crank_event_time
                ) & 0xFFFF
                if delta_time_ticks > 0:
                    cadence = (delta_revs * 60.0 * 1024.0) / delta_time_ticks
                elif delta_revs == 0 and power == 0:
                    # Some trainers repeat identical crank samples while stopped.
                    cadence = 0.0

            self._last_crank_revs = crank_revs
            self._last_crank_event_time = crank_event_time

        if extreme_force_magnitudes_present:
            if cursor + 4 > len(payload):
                return power, cadence
            cursor += 4

        if extreme_torque_magnitudes_present:
            if cursor + 4 > len(payload):
                return power, cadence
            cursor += 4

        if extreme_angles_present:
            if cursor + 3 > len(payload):
                return power, cadence
            cursor += 3

        if top_dead_spot_angle_present:
            if cursor + 2 > len(payload):
                return power, cadence
            cursor += 2

        if bottom_dead_spot_angle_present:
            if cursor + 2 > len(payload):
                return power, cadence
            cursor += 2

        if accumulated_energy_present:
            if cursor + 2 > len(payload):
                return power, cadence
            cursor += 2

        return power, cadence

    async def _simulation_loop(self) -> None:
        while self._sim_connected:
            self._sim_tick += 1
            if self._sim_mode_remaining <= 0:
                roll = self._sim_rng.random()
                if roll < 0.12:
                    self._sim_mode = "surge"
                    self._sim_mode_remaining = self._sim_rng.randint(8, 20)
                elif roll < 0.24:
                    self._sim_mode = "recovery"
                    self._sim_mode_remaining = self._sim_rng.randint(8, 18)
                else:
                    self._sim_mode = "steady"
                    self._sim_mode_remaining = self._sim_rng.randint(18, 45)
            self._sim_mode_remaining -= 1

            mode_offset = 0.0
            cadence_mode_offset = 0.0
            if self._sim_mode == "surge":
                mode_offset = self._sim_rng.uniform(20.0, 55.0)
                cadence_mode_offset = self._sim_rng.uniform(4.0, 11.0)
            elif self._sim_mode == "recovery":
                mode_offset = -self._sim_rng.uniform(15.0, 40.0)
                cadence_mode_offset = -self._sim_rng.uniform(5.0, 12.0)

            periodic = 10.0 * math.sin(self._sim_tick / 5.0) + 6.0 * math.sin(
                self._sim_tick / 11.0
            )
            noise = self._sim_rng.uniform(-6.0, 6.0)
            dynamic_target = max(
                50.0, min(1200.0, float(self._sim_target_watts) + mode_offset + periodic + noise)
            )

            delta = dynamic_target - self._sim_power
            step = max(-30.0, min(30.0, delta * 0.30))
            self._sim_power += step
            if abs(self._sim_power - dynamic_target) < 1.0:
                self._sim_power = float(dynamic_target)

            cadence_periodic = 8.0 * math.sin(self._sim_tick / 3.8) + 5.0 * math.sin(
                self._sim_tick / 8.5
            )
            cadence_target = (
                70.0
                + (self._sim_power / 8.8)
                + cadence_mode_offset
                + cadence_periodic
                + self._sim_rng.uniform(-8.0, 8.0)
            )
            speed_target = 14.0 + (self._sim_power / 11.0) + self._sim_rng.uniform(-2.2, 2.2)
            self._sim_cadence += max(
                -5.5, min(5.5, (cadence_target - self._sim_cadence) * 0.55)
            )
            self._sim_speed += max(-2.8, min(2.8, (speed_target - self._sim_speed) * 0.40))

            self._sim_cadence = max(45.0, min(128.0, self._sim_cadence))
            self._sim_speed = max(7.0, min(78.0, self._sim_speed))
            metrics = IndoorBikeData(
                instantaneous_power=int(round(self._sim_power)),
                instantaneous_cadence=round(self._sim_cadence, 1),
                instantaneous_speed_kmh=round(self._sim_speed, 1),
            )

            if self._metrics_callback is not None:
                maybe_coro = self._metrics_callback(metrics)
                if asyncio.iscoroutine(maybe_coro):
                    asyncio.create_task(maybe_coro)

            await asyncio.sleep(1.0)
