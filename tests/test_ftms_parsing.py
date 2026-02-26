from __future__ import annotations

import struct

from backend.ble.constants import parse_indoor_bike_flags
from backend.ble.ftms_client import FTMSClient, normalize_power_target, parse_indoor_bike_data


def test_parse_indoor_bike_flags_power_and_cadence_present() -> None:
    flags = parse_indoor_bike_flags(0x0044)
    assert flags.instantaneous_cadence_present is True
    assert flags.instantaneous_power_present is True
    assert flags.average_speed_present is False


def test_parse_indoor_bike_data_power_and_cadence() -> None:
    # Flags: cadence + power present. "More Data" is not set, so payload starts with
    # instantaneous speed (2 bytes) before cadence and power.
    payload = (
        struct.pack("<H", 0x0044)
        + struct.pack("<H", 3000)  # 30.00 km/h instantaneous speed
        + struct.pack("<H", 176)   # 88.0 rpm cadence (0.5 rpm units)
        + struct.pack("<h", 182)   # 182 W
    )

    data = parse_indoor_bike_data(payload)

    assert data.instantaneous_cadence == 88.0
    assert data.instantaneous_power == 182
    assert data.instantaneous_speed_kmh == 30.0


def test_parse_indoor_bike_data_negative_power() -> None:
    # Set "More Data" so instantaneous speed is not present.
    payload = struct.pack("<H", 0x0041) + struct.pack("<h", -10)

    data = parse_indoor_bike_data(payload)

    assert data.instantaneous_cadence is None
    assert data.instantaneous_power == -10


def test_parse_indoor_bike_data_fallback_when_speed_present_with_more_data_flag() -> None:
    # Device quirk: more_data is set but payload still includes instantaneous speed.
    payload = (
        struct.pack("<H", 0x0045)
        + struct.pack("<H", 2500)  # 25.00 km/h instantaneous speed
        + struct.pack("<H", 170)   # 85.0 rpm cadence
        + struct.pack("<h", 260)   # 260 W
    )

    data = parse_indoor_bike_data(payload)

    assert data.instantaneous_cadence == 85.0
    assert data.instantaneous_power == 260


def test_parse_cycling_power_measurement_crank_cadence() -> None:
    client = FTMSClient()

    # Flags: crank revolution data present (bit 5)
    # First packet seeds previous crank values.
    payload1 = struct.pack("<HhHH", 0x0020, 180, 1000, 20000)
    power1, cadence1 = client._parse_cycling_power_measurement(payload1)
    assert power1 == 180
    assert cadence1 is None

    # +2 rev in +1024 ticks => 120 rpm
    payload2 = struct.pack("<HhHH", 0x0020, 185, 1002, 21024)
    power2, cadence2 = client._parse_cycling_power_measurement(payload2)
    assert power2 == 185
    assert cadence2 == 120.0


def test_parse_cycling_power_measurement_zero_cadence_when_no_new_revs() -> None:
    client = FTMSClient()

    payload1 = struct.pack("<HhHH", 0x0020, 180, 1000, 20000)
    client._parse_cycling_power_measurement(payload1)

    # Same rev count, time advanced => cadence should be 0.
    payload2 = struct.pack("<HhHH", 0x0020, 0, 1000, 21024)
    power2, cadence2 = client._parse_cycling_power_measurement(payload2)
    assert power2 == 0
    assert cadence2 == 0.0


def test_parse_cycling_power_measurement_zero_cadence_when_sample_repeated_stopped() -> None:
    client = FTMSClient()

    # Seed state with a moving sample.
    moving = struct.pack("<HhHH", 0x0020, 180, 1000, 20000)
    client._parse_cycling_power_measurement(moving)
    moving2 = struct.pack("<HhHH", 0x0020, 185, 1002, 21024)
    client._parse_cycling_power_measurement(moving2)

    # Repeated stopped sample: no new rev/time and zero power.
    stopped_repeated = struct.pack("<HhHH", 0x0020, 0, 1002, 21024)
    power3, cadence3 = client._parse_cycling_power_measurement(stopped_repeated)
    assert power3 == 0
    assert cadence3 == 0.0


def test_normalize_power_target_clamps_to_supported_range() -> None:
    assert normalize_power_target(20, 30, 400, 5) == 30
    assert normalize_power_target(420, 30, 400, 5) == 400


def test_normalize_power_target_aligns_to_increment() -> None:
    assert normalize_power_target(33, 30, 400, 5) == 35
    assert normalize_power_target(32, 30, 400, 5) == 30
