from __future__ import annotations

from backend.workout.library import build_plan_from_template, list_templates


def test_vo2max_template_exists_and_builds() -> None:
    templates = list_templates()
    keys = {template.key for template in templates}
    assert "vo2max_5x3" in keys

    plan = build_plan_from_template("vo2max_5x3", ftp_watts=250)
    assert plan.name.startswith("VO2max 5x3")
    assert len(plan.steps) == 11
    assert plan.steps[1].target_watts == 280  # 250 * 1.12
    assert plan.steps[1].cadence_min_rpm == 95
    assert plan.steps[1].cadence_max_rpm == 108


def test_duration_templates_exist() -> None:
    keys = {template.key for template in list_templates()}
    assert "tempo_30" in keys
    assert "sweetspot_45" in keys
    assert "endurance_60" in keys


def test_inferred_cadence_is_present_and_correlated_with_intensity() -> None:
    plan = build_plan_from_template("tempo_30", ftp_watts=240)
    warmup = plan.steps[0]
    main = plan.steps[1]
    assert warmup.cadence_min_rpm is not None
    assert warmup.cadence_max_rpm is not None
    assert main.cadence_min_rpm is not None
    assert main.cadence_max_rpm is not None
    # Main block intensity (0.78) should require equal or higher cadence than warmup (0.55).
    assert main.cadence_min_rpm >= warmup.cadence_min_rpm
