"""Shared runtime state for the terminal MVP engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class EngineState:
    connected_device: str | None = None
    last_power_watts: int | None = None
    last_cadence_rpm: float | None = None
    last_update: datetime | None = None
