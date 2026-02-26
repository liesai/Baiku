"""FTMS constants and parsing helpers for BLE Fitness Machine Service."""

from __future__ import annotations

from dataclasses import dataclass

FTMS_SERVICE_UUID = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_CHAR_UUID = "00002ad2-0000-1000-8000-00805f9b34fb"
FITNESS_MACHINE_CONTROL_POINT_CHAR_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"
SUPPORTED_POWER_RANGE_CHAR_UUID = "00002ad8-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT_CHAR_UUID = "00002a63-0000-1000-8000-00805f9b34fb"

# Fitness Machine Control Point opcodes (FTMS)
OP_REQUEST_CONTROL = 0x00
OP_RESET = 0x01
OP_SET_TARGET_POWER = 0x05
OP_START_RESUME = 0x07

# Indoor Bike Data flags
FLAG_MORE_DATA = 1 << 0
FLAG_AVERAGE_SPEED_PRESENT = 1 << 1
FLAG_INSTANTANEOUS_CADENCE_PRESENT = 1 << 2
FLAG_AVERAGE_CADENCE_PRESENT = 1 << 3
FLAG_TOTAL_DISTANCE_PRESENT = 1 << 4
FLAG_RESISTANCE_LEVEL_PRESENT = 1 << 5
FLAG_INSTANTANEOUS_POWER_PRESENT = 1 << 6
FLAG_AVERAGE_POWER_PRESENT = 1 << 7
FLAG_EXPENDED_ENERGY_PRESENT = 1 << 8
FLAG_HEART_RATE_PRESENT = 1 << 9
FLAG_METABOLIC_EQUIVALENT_PRESENT = 1 << 10
FLAG_ELAPSED_TIME_PRESENT = 1 << 11
FLAG_REMAINING_TIME_PRESENT = 1 << 12


@dataclass(frozen=True)
class IndoorBikeDataFlags:
    more_data: bool
    average_speed_present: bool
    instantaneous_cadence_present: bool
    average_cadence_present: bool
    total_distance_present: bool
    resistance_level_present: bool
    instantaneous_power_present: bool
    average_power_present: bool
    expended_energy_present: bool
    heart_rate_present: bool
    metabolic_equivalent_present: bool
    elapsed_time_present: bool
    remaining_time_present: bool


def parse_indoor_bike_flags(raw_flags: int) -> IndoorBikeDataFlags:
    """Decode FTMS Indoor Bike Data flags into a typed structure."""
    return IndoorBikeDataFlags(
        more_data=bool(raw_flags & FLAG_MORE_DATA),
        average_speed_present=bool(raw_flags & FLAG_AVERAGE_SPEED_PRESENT),
        instantaneous_cadence_present=bool(raw_flags & FLAG_INSTANTANEOUS_CADENCE_PRESENT),
        average_cadence_present=bool(raw_flags & FLAG_AVERAGE_CADENCE_PRESENT),
        total_distance_present=bool(raw_flags & FLAG_TOTAL_DISTANCE_PRESENT),
        resistance_level_present=bool(raw_flags & FLAG_RESISTANCE_LEVEL_PRESENT),
        instantaneous_power_present=bool(raw_flags & FLAG_INSTANTANEOUS_POWER_PRESENT),
        average_power_present=bool(raw_flags & FLAG_AVERAGE_POWER_PRESENT),
        expended_energy_present=bool(raw_flags & FLAG_EXPENDED_ENERGY_PRESENT),
        heart_rate_present=bool(raw_flags & FLAG_HEART_RATE_PRESENT),
        metabolic_equivalent_present=bool(raw_flags & FLAG_METABOLIC_EQUIVALENT_PRESENT),
        elapsed_time_present=bool(raw_flags & FLAG_ELAPSED_TIME_PRESENT),
        remaining_time_present=bool(raw_flags & FLAG_REMAINING_TIME_PRESENT),
    )
