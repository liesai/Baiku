"""Coaching signal computation with anti-flicker stabilization."""

from __future__ import annotations

from dataclasses import dataclass


ActionKey = str


@dataclass(frozen=True)
class CoachingSignal:
    key: ActionKey
    text: str
    color: str
    severity: str


def compute_coaching_signal(
    *,
    power: int | None,
    cadence: float | None,
    expected_power_min: int | None,
    expected_power_max: int | None,
    expected_cadence_min: int | None,
    expected_cadence_max: int | None,
) -> CoachingSignal:
    power_low = power is not None and expected_power_min is not None and power < expected_power_min
    power_high = power is not None and expected_power_max is not None and power > expected_power_max
    cadence_low = (
        cadence is not None and expected_cadence_min is not None and cadence < expected_cadence_min
    )
    cadence_high = (
        cadence is not None and expected_cadence_max is not None and cadence > expected_cadence_max
    )

    if (power_low or power_high) and (cadence_low or cadence_high):
        power_hint = "↑ puissance" if power_low else "↓ puissance"
        cadence_hint = "↑ cadence" if cadence_low else "↓ cadence"
        severity = "bad" if power_high or cadence_high else "warn"
        color = "#ef4444" if severity == "bad" else "#f59e0b"
        return CoachingSignal(
            key=f"dual_{'pl' if power_low else 'ph'}_{'cl' if cadence_low else 'ch'}",
            text=f"Action: {power_hint} + {cadence_hint}",
            color=color,
            severity=severity,
        )

    if power_low:
        return CoachingSignal(
            key="power_low",
            text="Action: ↑ Accelere (puissance trop basse)",
            color="#f59e0b",
            severity="warn",
        )
    if power_high:
        return CoachingSignal(
            key="power_high",
            text="Action: ↓ Reduis l'effort (puissance trop haute)",
            color="#ef4444",
            severity="bad",
        )
    if cadence_low:
        return CoachingSignal(
            key="cadence_low",
            text="Action: ↑ Augmente la cadence",
            color="#f59e0b",
            severity="warn",
        )
    if cadence_high:
        return CoachingSignal(
            key="cadence_high",
            text="Action: ↓ Baisse la cadence",
            color="#ef4444",
            severity="bad",
        )
    return CoachingSignal(
        key="ok",
        text="Action: Maintenir la zone",
        color="#22c55e",
        severity="ok",
    )


class ActionStabilizer:
    """Avoid rapid action flicker when values oscillate around thresholds."""

    def __init__(self, min_switch_sec: float = 2.0) -> None:
        self._min_switch_sec = min_switch_sec
        self._current: CoachingSignal | None = None
        self._pending: CoachingSignal | None = None
        self._pending_since: float | None = None

    def reset(self) -> None:
        self._current = None
        self._pending = None
        self._pending_since = None

    def update(self, candidate: CoachingSignal, now_ts: float) -> tuple[CoachingSignal, bool]:
        if self._current is None:
            self._current = candidate
            self._pending = None
            self._pending_since = None
            return candidate, True

        if candidate.key == self._current.key:
            self._pending = None
            self._pending_since = None
            return self._current, False

        if self._pending is None or self._pending.key != candidate.key:
            self._pending = candidate
            self._pending_since = now_ts
            return self._current, False

        assert self._pending_since is not None
        if now_ts - self._pending_since >= self._min_switch_sec:
            self._current = self._pending
            self._pending = None
            self._pending_since = None
            return self._current, True

        return self._current, False
