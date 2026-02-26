from __future__ import annotations

from backend.ui.coaching import ActionStabilizer, compute_coaching_signal


def test_compute_coaching_signal_priority() -> None:
    s1 = compute_coaching_signal(
        power=150,
        cadence=90.0,
        expected_power_min=180,
        expected_power_max=200,
        expected_cadence_min=85,
        expected_cadence_max=95,
    )
    assert s1.key == "power_low"

    s2 = compute_coaching_signal(
        power=210,
        cadence=90.0,
        expected_power_min=180,
        expected_power_max=200,
        expected_cadence_min=85,
        expected_cadence_max=95,
    )
    assert s2.key == "power_high"

    s3 = compute_coaching_signal(
        power=190,
        cadence=70.0,
        expected_power_min=180,
        expected_power_max=200,
        expected_cadence_min=85,
        expected_cadence_max=95,
    )
    assert s3.key == "cadence_low"


def test_action_stabilizer_anti_flicker() -> None:
    stab = ActionStabilizer(min_switch_sec=2.0)
    ok = compute_coaching_signal(
        power=190,
        cadence=90.0,
        expected_power_min=180,
        expected_power_max=200,
        expected_cadence_min=85,
        expected_cadence_max=95,
    )
    low = compute_coaching_signal(
        power=150,
        cadence=90.0,
        expected_power_min=180,
        expected_power_max=200,
        expected_cadence_min=85,
        expected_cadence_max=95,
    )

    shown, changed = stab.update(ok, 0.0)
    assert changed is True
    assert shown.key == "ok"

    shown, changed = stab.update(low, 0.3)
    assert changed is False
    assert shown.key == "ok"

    shown, changed = stab.update(low, 1.8)
    assert changed is False
    assert shown.key == "ok"

    shown, changed = stab.update(low, 2.4)
    assert changed is True
    assert shown.key == "power_low"
