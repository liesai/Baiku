"""Built-in workout templates inspired by common FTP/power sessions."""

from __future__ import annotations

from dataclasses import dataclass

from backend.workout.model import WorkoutPlan, WorkoutStep


@dataclass(frozen=True)
class WorkoutTemplateStep:
    duration_sec: int
    intensity_pct: float
    label: str
    cadence_min_rpm: int | None = None
    cadence_max_rpm: int | None = None


@dataclass(frozen=True)
class WorkoutTemplate:
    key: str
    name: str
    category: str
    steps: tuple[WorkoutTemplateStep, ...]


TEMPLATES: tuple[WorkoutTemplate, ...] = (
    WorkoutTemplate(
        key="wake_up_20",
        name="Wake Up 20",
        category="Reveil",
        steps=(
            WorkoutTemplateStep(300, 0.50, "Warmup"),
            WorkoutTemplateStep(180, 0.60, "Cadence Prep", 90, 100),
            WorkoutTemplateStep(60, 0.80, "Activation", 95, 105),
            WorkoutTemplateStep(120, 0.55, "Recover"),
            WorkoutTemplateStep(60, 0.90, "Openers", 100, 110),
            WorkoutTemplateStep(180, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="tempo_30",
        name="Tempo 30",
        category="Tempo",
        steps=(
            WorkoutTemplateStep(420, 0.55, "Warmup"),
            WorkoutTemplateStep(720, 0.78, "Tempo Main", 88, 96),
            WorkoutTemplateStep(240, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="sweetspot_45",
        name="Sweet Spot 45",
        category="FTP",
        steps=(
            WorkoutTemplateStep(600, 0.55, "Warmup"),
            WorkoutTemplateStep(900, 0.88, "Sweet Spot Block", 88, 96),
            WorkoutTemplateStep(300, 0.60, "Recover"),
            WorkoutTemplateStep(600, 0.90, "Sweet Spot Finish", 88, 96),
            WorkoutTemplateStep(300, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="endurance_60",
        name="Endurance 60",
        category="Endurance",
        steps=(
            WorkoutTemplateStep(600, 0.55, "Warmup"),
            WorkoutTemplateStep(2400, 0.70, "Endurance Cruise", 85, 95),
            WorkoutTemplateStep(600, 0.60, "Tempo Finish", 88, 96),
        ),
    ),
    WorkoutTemplate(
        key="ftp_2x8",
        name="FTP Builder 2x8",
        category="FTP",
        steps=(
            WorkoutTemplateStep(480, 0.55, "Warmup"),
            WorkoutTemplateStep(480, 0.95, "Block 1", 85, 95),
            WorkoutTemplateStep(240, 0.60, "Recover"),
            WorkoutTemplateStep(480, 1.00, "Block 2", 85, 95),
            WorkoutTemplateStep(360, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="sweetspot_3x10",
        name="Sweet Spot 3x10",
        category="FTP",
        steps=(
            WorkoutTemplateStep(600, 0.55, "Warmup"),
            WorkoutTemplateStep(600, 0.88, "Sweet Spot 1", 88, 96),
            WorkoutTemplateStep(240, 0.60, "Recover"),
            WorkoutTemplateStep(600, 0.90, "Sweet Spot 2", 88, 96),
            WorkoutTemplateStep(240, 0.60, "Recover"),
            WorkoutTemplateStep(600, 0.92, "Sweet Spot 3", 88, 96),
            WorkoutTemplateStep(300, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="vo2_30_30",
        name="Power 30/30",
        category="Power",
        steps=(
            WorkoutTemplateStep(480, 0.55, "Warmup"),
            WorkoutTemplateStep(30, 1.20, "ON 1", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 1", 80, 95),
            WorkoutTemplateStep(30, 1.20, "ON 2", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 2", 80, 95),
            WorkoutTemplateStep(30, 1.20, "ON 3", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 3", 80, 95),
            WorkoutTemplateStep(30, 1.20, "ON 4", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 4", 80, 95),
            WorkoutTemplateStep(30, 1.20, "ON 5", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 5", 80, 95),
            WorkoutTemplateStep(30, 1.20, "ON 6", 100, 115),
            WorkoutTemplateStep(30, 0.50, "OFF 6", 80, 95),
            WorkoutTemplateStep(420, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="vo2max_5x3",
        name="VO2max 5x3",
        category="VO2max",
        steps=(
            WorkoutTemplateStep(600, 0.55, "Warmup"),
            WorkoutTemplateStep(180, 1.12, "VO2 #1", 95, 108),
            WorkoutTemplateStep(180, 0.55, "Recover #1", 82, 92),
            WorkoutTemplateStep(180, 1.12, "VO2 #2", 95, 108),
            WorkoutTemplateStep(180, 0.55, "Recover #2", 82, 92),
            WorkoutTemplateStep(180, 1.12, "VO2 #3", 95, 108),
            WorkoutTemplateStep(180, 0.55, "Recover #3", 82, 92),
            WorkoutTemplateStep(180, 1.12, "VO2 #4", 95, 108),
            WorkoutTemplateStep(180, 0.55, "Recover #4", 82, 92),
            WorkoutTemplateStep(180, 1.12, "VO2 #5", 95, 108),
            WorkoutTemplateStep(420, 0.50, "Cool-down"),
        ),
    ),
    WorkoutTemplate(
        key="threshold_4x6",
        name="Threshold 4x6",
        category="FTP",
        steps=(
            WorkoutTemplateStep(600, 0.55, "Warmup"),
            WorkoutTemplateStep(360, 1.02, "Threshold 1", 82, 92),
            WorkoutTemplateStep(180, 0.60, "Recover"),
            WorkoutTemplateStep(360, 1.03, "Threshold 2", 82, 92),
            WorkoutTemplateStep(180, 0.60, "Recover"),
            WorkoutTemplateStep(360, 1.04, "Threshold 3", 82, 92),
            WorkoutTemplateStep(180, 0.60, "Recover"),
            WorkoutTemplateStep(360, 1.05, "Threshold 4", 82, 92),
            WorkoutTemplateStep(420, 0.50, "Cool-down"),
        ),
    ),
)


def list_templates() -> tuple[WorkoutTemplate, ...]:
    return TEMPLATES


def _infer_cadence_range(intensity_pct: float) -> tuple[int, int]:
    """Infer cadence zone from ERG intensity when template does not define one."""
    if intensity_pct <= 0.60:
        return 80, 92
    if intensity_pct <= 0.78:
        return 85, 95
    if intensity_pct <= 0.95:
        return 88, 98
    if intensity_pct <= 1.05:
        return 82, 92
    return 95, 110


def build_plan_from_template(template_key: str, ftp_watts: int) -> WorkoutPlan:
    if ftp_watts <= 0:
        raise ValueError("FTP must be > 0")

    template = next((item for item in TEMPLATES if item.key == template_key), None)
    if template is None:
        raise ValueError(f"Unknown workout template '{template_key}'")

    steps: list[WorkoutStep] = []
    for step in template.steps:
        target_watts = max(1, int(round(ftp_watts * step.intensity_pct)))
        inferred_min, inferred_max = _infer_cadence_range(step.intensity_pct)
        cadence_min = step.cadence_min_rpm if step.cadence_min_rpm is not None else inferred_min
        cadence_max = step.cadence_max_rpm if step.cadence_max_rpm is not None else inferred_max
        steps.append(
            WorkoutStep(
                duration_sec=step.duration_sec,
                target_watts=target_watts,
                label=step.label,
                cadence_min_rpm=cadence_min,
                cadence_max_rpm=cadence_max,
            )
        )
    return WorkoutPlan(name=f"{template.name} ({ftp_watts} FTP)", steps=tuple(steps))
