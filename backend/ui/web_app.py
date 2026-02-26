"""NiceGUI web UI for Velox Engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, cast
from uuid import uuid4

from nicegui import core, ui

from backend.ble.ftms_client import IndoorBikeData, ScannedDevice
from backend.ui.coaching import ActionStabilizer, compute_coaching_signal
from backend.ui.controller import UIController
from backend.workout.library import build_plan_from_template, list_templates
from backend.workout.model import WorkoutPlan, WorkoutStep
from backend.workout.runner import TargetMode, WorkoutProgress
from backend.workout.session_artifacts import (
    SessionPoint,
    SessionSnapshot,
    export_snapshot_csv,
    save_snapshot,
)
from backend.workout.session_store import (
    SessionRecord,
    append_session,
    load_recent_sessions,
    now_utc_iso,
)
from backend.workout.user_workouts import (
    list_user_workouts,
    load_user_workout,
    save_user_workout,
)

MAX_POWER_WATTS = 500
MAX_CADENCE_RPM = 130
MAX_SPEED_KMH = 70
TIMELINE_SAMPLE_SEC = 2
ACTION_SWITCH_MIN_SEC = 2.0


@dataclass
class WebState:
    connected: bool = False
    status: str = "Not connected"
    workout: WorkoutPlan | None = None
    progress: WorkoutProgress | None = None
    power: int | None = None
    cadence: float | None = None
    speed: float | None = None
    distance_km: float = 0.0
    last_ts: float | None = None
    mode: TargetMode = "erg"
    ftp_watts: int = 220


@dataclass(frozen=True)
class WorkoutOption:
    label: str
    source: str  # builtin | custom
    key: str
    category: str
    name: str
    duration_sec: int
    avg_intensity_pct: int


def _fmt_number(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _fmt_power(value: int | None) -> str:
    return f"{value:d} W" if value is not None else "-- W"


def _fmt_cadence(value: float | None) -> str:
    return f"{_fmt_number(value, 1)} rpm" if value is not None else "-- rpm"


def _fmt_speed(value: float | None) -> str:
    return f"{_fmt_number(value, 1)} km/h" if value is not None else "-- km/h"


def _fmt_duration(total_seconds: int) -> str:
    minutes, seconds = divmod(max(0, total_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _fmt_timeline_mark(total_seconds: int) -> str:
    rounded_min = int(round((total_seconds / 60.0) / 10.0) * 10)
    if rounded_min % 10 != 0:
        return ""
    # Show sparse markers only, every 10 minutes.
    if abs((rounded_min * 60) - total_seconds) > TIMELINE_SAMPLE_SEC:
        return ""
    return f"{rounded_min:02d}:00"


def _gauge_options(title: str, unit: str, max_value: int) -> dict[str, Any]:
    return {
        "title": {
            "text": title,
            "left": "center",
            "top": "7%",
            "textStyle": {"color": "#ffffff", "fontWeight": "bold", "fontFamily": "Arial"},
        },
        "series": [
            {
                "type": "gauge",
                "min": 0,
                "max": max_value,
                "startAngle": 210,
                "endAngle": -30,
                "progress": {
                    "show": True,
                    "width": 14,
                    "itemStyle": {"color": "#22d3ee"},
                },
                "axisLine": {
                    "lineStyle": {
                        "width": 14,
                        "color": [[0.5, "#1d4ed8"], [0.8, "#0ea5e9"], [1, "#22d3ee"]],
                    }
                },
                "axisTick": {"show": False},
                "splitLine": {"show": False},
                "axisLabel": {"distance": -46, "fontSize": 10, "color": "#ffffff"},
                "pointer": {"width": 5, "length": "72%"},
                "anchor": {
                    "show": True,
                    "showAbove": True,
                    "size": 8,
                    "itemStyle": {"color": "#38bdf8"},
                },
                "detail": {
                    "valueAnimation": True,
                    "fontSize": 20,
                    "offsetCenter": [0, "62%"],
                    "formatter": "{value} " + unit,
                    "color": "#ffffff",
                },
                "data": [{"value": 0}],
            }
        ],
    }


def run_web_ui(
    *,
    simulate_ht: bool = False,
    host: str = "127.0.0.1",
    port: int = 8088,
    start_delay_sec: int = 10,
) -> int:
    controller = UIController(debug_ftms=False, simulate_ht=simulate_ht)
    state = WebState()
    ui.add_head_html(
        """
        <style>
          :root {
            --gb-bg: #0b1220;
            --gb-surface: #0f1b35;
            --gb-surface-2: #132449;
            --gb-text: #e5e7eb;
            --gb-muted: #9caecf;
            --gb-accent: #38bdf8;
          }
          body {
            background: radial-gradient(circle at top, #17223f 0%, var(--gb-bg) 58%);
            color: var(--gb-text);
            font-family: Arial, "Segoe UI", sans-serif;
          }
          body.gb-layout-1080 {
            height: 100vh;
            overflow: hidden;
            font-size: 0.95rem;
          }
          body.gb-layout-1440 {
            height: 100vh;
            overflow: hidden;
            font-size: 1rem;
          }
          .gb-card {
            background: linear-gradient(180deg, var(--gb-surface) 0%, var(--gb-surface-2) 100%);
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 14px;
            box-shadow: 0 12px 24px rgba(2, 6, 23, 0.32);
          }
          .gb-compact .q-card__section {
            padding: 8px 10px;
          }
          .gb-kpi {
            background: linear-gradient(180deg, #0c2a5d 0%, #113a83 100%);
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 12px;
            box-shadow: 0 10px 20px rgba(15, 23, 42, 0.4);
          }
          .gb-number {
            font-size: 1.1rem;
            font-weight: 700;
            color: #f8fafc;
          }
          .gb-help-ok { color: #22c55e; font-weight: 700; }
          .gb-help-warn { color: #f59e0b; font-weight: 700; }
          .gb-help-bad { color: #ef4444; font-weight: 700; }
          .gb-muted {
            color: var(--gb-muted);
          }
          .gb-title-neutral {
            color: #ffffff;
            font-family: Arial, "Segoe UI", sans-serif;
            font-weight: 700;
          }
          .q-field__control {
            background: rgba(15, 27, 53, 0.9) !important;
            color: #e2e8f0 !important;
            border-radius: 10px !important;
          }
          .q-field__native,
          .q-field__input,
          .q-select__dropdown-icon,
          .q-field__label {
            color: #cbd5e1 !important;
          }
          .q-menu,
          .q-virtual-scroll__content {
            background: #0f1b35 !important;
            color: #e2e8f0 !important;
          }
        </style>
        """
    )

    templates = list_templates()
    workout_options: list[WorkoutOption] = []
    workout_option_by_label: dict[str, WorkoutOption] = {}
    workout_option_labels: list[str] = []

    devices: list[ScannedDevice] = []
    selected_device_address: str | None = None
    selected_template_label = ""
    zone_compliance = {"power_ok": 0, "power_total": 0, "rpm_ok": 0, "rpm_total": 0}
    strict_mode = False
    viewport_preset = "auto"
    session_started_at_utc: str | None = None
    current_snapshot_path: Path | None = None
    current_snapshot_csv_path: Path | None = None
    sound_alerts = True
    coaching_stabilizer = ActionStabilizer(min_switch_sec=ACTION_SWITCH_MIN_SEC)
    last_coaching_alert_key: str | None = None

    timeline_labels: list[str] = []
    timeline_expected_power: list[int] = []
    timeline_expected_cadence: list[float] = []
    timeline_actual_power: list[int | None] = []
    timeline_actual_cadence: list[float | None] = []
    timeline_step_ranges: list[tuple[int, int, str]] = []
    metric_samples: list[tuple[int | None, float | None, float | None]] = []

    with ui.column().classes("w-full gap-1") as setup_header:
        ui.label("VELOX ENGINE").classes("text-xl font-semibold tracking-wide")
        status_label = ui.label("Status: Not connected").classes("text-lg font-semibold")
        if simulate_ht:
            ui.label("SIM MODE - no BLE required").classes("text-orange-500 font-bold")

    with ui.column().classes("w-full gap-4") as setup_view:
        ui.label("Course Setup").classes("text-xl font-semibold")
        with ui.card().classes("w-full gb-card"):
            with ui.row().classes("w-full items-end gap-2"):
                band_select = ui.select(
                    ["All", "<=30 min", "30-45 min", "45-60 min", ">=60 min"],
                    value="All",
                    label="Duration",
                )
                ftp_input = ui.number("FTP (W)", value=220, min=80, max=500)
                mode_select = ui.select(
                    ["erg", "resistance", "slope"], value="erg", label="Mode"
                )
                delay_input = ui.number(
                    "Start delay (sec)", value=int(max(0, start_delay_sec)), min=0, max=180
                )
                strict_switch = ui.switch("Single-screen strict", value=False)
                preset_select = ui.select(
                    {"auto": "Auto", "1080p": "1080p", "1440p": "1440p"},
                    value="auto",
                    label="Viewport",
                )
                device_select = ui.select([], label="Device").classes("min-w-[280px]")
                scan_btn = ui.button("Scan")
                connect_btn = ui.button("Connect")
                disconnect_btn = ui.button("Disconnect")
                builder_btn = ui.button("Workout builder")
                disconnect_btn.disable()
            selected_course_label = ui.label("Selected course: -").classes(
                "text-sm font-medium"
            )
            course_cards_grid = ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 gap-3")
            course_info = ui.label("No course loaded").classes("text-sm text-slate-300")

        plan_chart = ui.echart(
            {
                "title": {
                    "text": "Difficulty curve (target watts)",
                    "left": "center",
                    "textStyle": {"color": "#ffffff", "fontWeight": "bold", "fontFamily": "Arial"},
                },
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": [], "axisLabel": {"color": "#ffffff"}},
                "yAxis": {
                    "type": "value",
                    "name": "W",
                    "axisLabel": {"color": "#ffffff"},
                    "nameTextStyle": {"color": "#ffffff", "fontWeight": "bold"},
                },
                "series": [{"type": "bar", "data": []}],
                "grid": {"left": 50, "right": 20, "top": 48, "bottom": 40},
            }
        ).classes("w-full h-72")

        with ui.row().classes("w-full items-center gap-2"):
            start_btn = ui.button("Start")
            start_btn.disable()

        ui.label("Recent sessions").classes("text-base font-medium")
        history = ui.table(
            columns=[
                {"name": "ended", "label": "Ended", "field": "ended"},
                {"name": "status", "label": "Status", "field": "status"},
                {"name": "workout", "label": "Workout", "field": "workout"},
                {"name": "mins", "label": "Mins", "field": "mins"},
                {"name": "snapshot", "label": "Snapshot", "field": "snapshot"},
            ],
            rows=[],
        ).classes("w-full")
        with ui.row().classes("w-full gap-2"):
            export_json_btn = ui.button("Export last JSON")
            export_csv_btn = ui.button("Export last CSV")
            export_json_btn.disable()
            export_csv_btn.disable()

    with ui.column().classes("w-full gap-2") as workout_view:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Workout Session").classes("text-lg gb-title-neutral")
            with ui.row().classes("gap-2"):
                back_btn = ui.button("Back to setup").props("outline color=white")
                stop_btn = ui.button("Stop session").props("color=negative")
                stop_btn.disable()

        with ui.row().classes("w-full gap-2"):
            with ui.card().classes("w-full gb-card gb-compact"):
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    workout_info = ui.label("No workout loaded").classes(
                        "text-sm font-semibold"
                    )
                    step_info = ui.label("Step: -").classes("text-sm")
                    elapsed_label = ui.label("Elapsed: 00:00").classes("text-sm")
                    remaining_label = ui.label("Remaining: 00:00").classes("text-sm")
                    compliance_info = ui.label("Compliance: -").classes("text-sm")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    next_step_label = ui.label("Next: -").classes("text-sm gb-muted")
                    target_label = ui.label("Targets: -").classes("text-sm gb-muted")
                    mode_label = ui.label("Mode: ERG").classes("text-sm gb-muted")
                    sound_toggle = ui.switch("Sound alerts", value=True).classes("text-sm")
                with ui.row().classes("w-full items-center gap-3"):
                    guidance_label = ui.label("Action: -").classes("text-sm font-semibold")

        with ui.grid().classes("w-full grid-cols-1 md:grid-cols-4 gap-2"):
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Power").classes("text-xs gb-title-neutral")
                kpi_power = ui.label("-- W").classes("gb-number")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Cadence").classes("text-xs gb-title-neutral")
                kpi_cadence = ui.label("-- rpm").classes("gb-number")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Speed").classes("text-xs gb-title-neutral")
                kpi_speed = ui.label("-- km/h").classes("gb-number")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Distance").classes("text-xs gb-title-neutral")
                kpi_distance = ui.label("0,00 km").classes("gb-number")

        with ui.card().classes("w-full gb-card gb-compact"):
            live_chart = ui.echart(
                {
                    "title": {
                        "text": "Live performance curve",
                        "left": "center",
                        "textStyle": {
                            "color": "#ffffff",
                            "fontWeight": "bold",
                            "fontFamily": "Arial",
                        },
                    },
                    "textStyle": {"fontFamily": "Arial", "fontWeight": "normal"},
                    "legend": {
                        "data": [
                            "Power expected",
                            "Power actual",
                            "Cadence expected",
                            "Cadence actual",
                        ],
                        "selected": {
                            "Power expected": True,
                            "Power actual": True,
                            "Cadence expected": True,
                            "Cadence actual": True,
                        },
                        "top": 24,
                        "textStyle": {"color": "#ffffff"},
                    },
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {
                        "type": "category",
                        "data": [],
                        "axisLabel": {"color": "#ffffff"},
                    },
                    "yAxis": [
                        {
                            "type": "value",
                            "name": "W",
                            "position": "left",
                            "axisLabel": {"color": "#ffffff"},
                            "nameTextStyle": {"color": "#ffffff", "fontWeight": "bold"},
                            "splitLine": {"lineStyle": {"color": "rgba(148,163,184,.18)"}},
                        },
                        {
                            "type": "value",
                            "name": "rpm",
                            "position": "right",
                            "min": 40,
                            "max": 130,
                            "axisLabel": {"color": "#ffffff"},
                            "nameTextStyle": {"color": "#ffffff", "fontWeight": "bold"},
                            "splitLine": {"show": False},
                        },
                    ],
                    "series": [
                        {
                            "name": "Power expected",
                            "type": "line",
                            "data": [],
                            "smooth": False,
                            "lineStyle": {
                                "type": "dashed",
                                "width": 2,
                                "color": "rgba(148,163,184,.8)",
                            },
                            "symbol": "none",
                        },
                        {
                            "name": "Power actual",
                            "type": "line",
                            "data": [],
                            "smooth": True,
                            "lineStyle": {"width": 3, "color": "#22d3ee"},
                            "areaStyle": {
                                "opacity": 0.22,
                                "color": "rgba(56,189,248,.45)",
                            },
                            "connectNulls": False,
                            "symbol": "none",
                        },
                        {
                            "name": "Cadence expected",
                            "type": "line",
                            "yAxisIndex": 1,
                            "data": [],
                            "lineStyle": {
                                "type": "dashed",
                                "width": 2,
                                "color": "#fbbf24",
                            },
                            "z": 3,
                            "symbol": "none",
                        },
                        {
                            "name": "Cadence actual",
                            "type": "line",
                            "yAxisIndex": 1,
                            "data": [],
                            "smooth": True,
                            "lineStyle": {"width": 3, "color": "#86efac"},
                            "z": 4,
                            "connectNulls": False,
                            "symbol": "none",
                        },
                    ],
                    "grid": {"left": 48, "right": 48, "top": 70, "bottom": 32},
                }
            ).classes("w-full h-[250px]")

    workout_view.set_visibility(False)

    with ui.dialog() as builder_dialog, ui.card().classes("w-[920px] max-w-[96vw] gb-card"):
        ui.label("Workout builder").classes("text-lg font-semibold")
        with ui.row().classes("w-full gap-2"):
            builder_name = ui.input("Template name", value="Custom Workout").classes("w-1/2")
            builder_category = ui.input("Category", value="Custom").classes("w-1/4")
            builder_key = ui.input("Key (optional)", value="").classes("w-1/4")
        ui.label("Steps").classes("text-sm gb-muted")
        builder_steps_table = ui.table(
            columns=[
                {"name": "idx", "label": "#", "field": "idx"},
                {"name": "label", "label": "Label", "field": "label"},
                {"name": "dur", "label": "Sec", "field": "dur"},
                {"name": "watts", "label": "Watts", "field": "watts"},
                {"name": "cad", "label": "Cadence", "field": "cad"},
            ],
            rows=[],
        ).classes("w-full")
        with ui.row().classes("w-full items-end gap-2"):
            step_label_input = ui.input("Label", value="Step").classes("w-1/3")
            step_sec_input = ui.number("Duration sec", value=180, min=10, max=7200)
            step_watts_input = ui.number("Watts", value=200, min=30, max=900)
            step_rpm_min_input = ui.number("Cadence min", value=85, min=40, max=150)
            step_rpm_max_input = ui.number("Cadence max", value=95, min=40, max=150)
            add_step_btn = ui.button("Add step")
            remove_last_step_btn = ui.button("Remove last")
        with ui.row().classes("w-full justify-end gap-2"):
            cancel_builder_btn = ui.button("Close").props("outline")
            save_builder_btn = ui.button("Save template").props("color=primary")

    def pct(ok: int, total: int) -> float | None:
        if total <= 0:
            return None
        return (ok * 100.0) / total

    def in_band(duration_sec: int, band: str) -> bool:
        minutes = duration_sec / 60.0
        if band == "<=30 min":
            return minutes <= 30
        if band == "30-45 min":
            return 30 < minutes <= 45
        if band == "45-60 min":
            return 45 < minutes <= 60
        if band == ">=60 min":
            return minutes >= 60
        return True

    def in_range(
        value: float | int | None,
        min_value: float | int | None,
        max_value: float | int | None,
    ) -> bool | None:
        if value is None or min_value is None or max_value is None:
            return None
        return bool(min_value <= value <= max_value)

    def _step_label_for_index(index: int) -> str:
        for start, end, label in timeline_step_ranges:
            if start <= index <= end:
                return label
        return "-"

    def _compute_session_averages() -> tuple[float | None, float | None, float | None]:
        power_values = [float(p) for p, _, _ in metric_samples if p is not None]
        cadence_values = [c for _, c, _ in metric_samples if c is not None]
        speed_values = [s for _, _, s in metric_samples if s is not None]
        avg_power = sum(power_values) / len(power_values) if power_values else None
        avg_cadence = sum(cadence_values) / len(cadence_values) if cadence_values else None
        avg_speed = sum(speed_values) / len(speed_values) if speed_values else None
        return avg_power, avg_cadence, avg_speed

    def _compute_both_compliance_pct() -> float | None:
        point_total = min(len(timeline_actual_power), len(timeline_actual_cadence))
        if point_total <= 0:
            return None
        both_ok = 0
        both_total = 0
        for idx in range(point_total):
            expected_power = timeline_expected_power[idx]
            actual_power = timeline_actual_power[idx]
            expected_cadence = timeline_expected_cadence[idx]
            actual_cadence = timeline_actual_cadence[idx]
            power_ok = (
                actual_power is not None
                and int(round(expected_power * 0.95))
                <= actual_power
                <= int(round(expected_power * 1.05))
            )
            cadence_ok = (
                actual_cadence is not None
                and max(40.0, expected_cadence - 5.0)
                <= actual_cadence
                <= (expected_cadence + 5.0)
            )
            if actual_power is None and actual_cadence is None:
                continue
            both_total += 1
            if power_ok and cadence_ok:
                both_ok += 1
        if both_total == 0:
            return None
        return (both_ok * 100.0) / both_total

    def _save_session_snapshot(completed: bool) -> None:
        nonlocal current_snapshot_path, current_snapshot_csv_path
        if state.workout is None:
            return
        ended_at = now_utc_iso()
        started_at = session_started_at_utc or ended_at
        elapsed_sec = 0
        if state.progress is not None:
            elapsed_sec = state.progress.elapsed_total_sec
        else:
            elapsed_sec = min(
                state.workout.total_duration_sec,
                len([x for x in timeline_actual_power if x is not None]) * TIMELINE_SAMPLE_SEC,
            )
        power_pct = pct(zone_compliance["power_ok"], zone_compliance["power_total"])
        rpm_pct = pct(zone_compliance["rpm_ok"], zone_compliance["rpm_total"])
        both_pct = _compute_both_compliance_pct()
        avg_power, avg_cadence, avg_speed = _compute_session_averages()

        points: list[SessionPoint] = []
        for idx, expected_power in enumerate(timeline_expected_power):
            expected_cadence = timeline_expected_cadence[idx]
            actual_power = timeline_actual_power[idx]
            actual_cadence = timeline_actual_cadence[idx]
            power_ok = None
            cadence_ok = None
            if actual_power is not None:
                pmin = int(round(expected_power * 0.95))
                pmax = int(round(expected_power * 1.05))
                power_ok = pmin <= actual_power <= pmax
            if actual_cadence is not None:
                cmin = max(40.0, expected_cadence - 5.0)
                cmax = expected_cadence + 5.0
                cadence_ok = cmin <= actual_cadence <= cmax
            points.append(
                SessionPoint(
                    step_label=_step_label_for_index(idx),
                    t_label=timeline_labels[idx] or f"t+{idx * TIMELINE_SAMPLE_SEC}s",
                    expected_power_watts=expected_power,
                    actual_power_watts=actual_power,
                    expected_cadence_rpm=expected_cadence,
                    actual_cadence_rpm=actual_cadence,
                    power_in_zone=power_ok,
                    cadence_in_zone=cadence_ok,
                )
            )

        snapshot = SessionSnapshot(
            snapshot_id=str(uuid4()),
            started_at_utc=started_at,
            ended_at_utc=ended_at,
            workout_name=state.workout.name,
            target_mode=state.mode,
            ftp_watts=state.ftp_watts,
            completed=completed,
            planned_duration_sec=state.workout.total_duration_sec,
            elapsed_duration_sec=elapsed_sec,
            distance_km=state.distance_km,
            avg_power_watts=avg_power,
            avg_cadence_rpm=avg_cadence,
            avg_speed_kmh=avg_speed,
            power_compliance_pct=power_pct,
            rpm_compliance_pct=rpm_pct,
            both_compliance_pct=both_pct,
            points=tuple(points),
        )
        current_snapshot_path = save_snapshot(snapshot)
        current_snapshot_csv_path = export_snapshot_csv(snapshot)
        append_session(
            SessionRecord(
                started_at_utc=started_at,
                ended_at_utc=ended_at,
                workout_name=state.workout.name,
                target_mode=state.mode,
                ftp_watts=state.ftp_watts,
                completed=completed,
                planned_duration_sec=state.workout.total_duration_sec,
                elapsed_duration_sec=elapsed_sec,
                distance_km=state.distance_km,
                avg_power_watts=avg_power,
                avg_cadence_rpm=avg_cadence,
                avg_speed_kmh=avg_speed,
                power_compliance_pct=power_pct,
                rpm_compliance_pct=rpm_pct,
                both_compliance_pct=both_pct,
            )
        )

    def apply_layout_mode() -> None:
        cls = "gb-layout-auto"
        if strict_mode and viewport_preset == "1080p":
            cls = "gb-layout-1080"
        elif strict_mode and viewport_preset == "1440p":
            cls = "gb-layout-1440"
        if core.loop is None:
            # NiceGUI loop/client not ready yet during initial startup.
            return
        ui.run_javascript(
            (
                "document.body.classList.remove("
                "'gb-layout-auto','gb-layout-1080','gb-layout-1440'"
                ");"
                f"document.body.classList.add('{cls}');"
            )
        )

    def show_setup_screen() -> None:
        setup_header.set_visibility(True)
        setup_view.set_visibility(True)
        workout_view.set_visibility(False)

    def show_workout_screen() -> None:
        setup_header.set_visibility(False)
        setup_view.set_visibility(False)
        workout_view.set_visibility(True)

    def rebuild_workout_options() -> None:
        nonlocal workout_options, workout_option_by_label, workout_option_labels
        items: list[WorkoutOption] = []
        for item in templates:
            total_sec = sum(step.duration_sec for step in item.steps)
            avg_intensity = int(
                round(
                    sum(step.intensity_pct * step.duration_sec for step in item.steps)
                    / max(1, total_sec)
                    * 100
                )
            )
            label = f"{item.category} - {item.name} [{item.key}]"
            items.append(
                WorkoutOption(
                    label=label,
                    source="builtin",
                    key=item.key,
                    category=item.category,
                    name=item.name,
                    duration_sec=total_sec,
                    avg_intensity_pct=avg_intensity,
                )
            )
        for custom in list_user_workouts():
            plan = load_user_workout(custom.path)
            total_sec = sum(step.duration_sec for step in plan.steps)
            label = f"{custom.category} - {custom.name} [custom:{custom.key}]"
            items.append(
                WorkoutOption(
                    label=label,
                    source="custom",
                    key=custom.key,
                    category=custom.category,
                    name=custom.name,
                    duration_sec=total_sec,
                    avg_intensity_pct=100,
                )
            )
        workout_options = items
        workout_option_by_label = {item.label: item for item in items}
        workout_option_labels = [item.label for item in items]

    def load_selected_workout() -> None:
        if not selected_template_label:
            state.workout = None
            return
        selected = workout_option_by_label.get(selected_template_label)
        if selected is None:
            state.workout = None
            return
        state.ftp_watts = int(ftp_input.value or 220)
        state.mode = cast(TargetMode, mode_select.value or "erg")
        if selected.source == "builtin":
            state.workout = build_plan_from_template(selected.key, state.ftp_watts)
        else:
            custom_path = Path.home() / ".velox-engine" / "workouts" / f"{selected.key}.json"
            state.workout = load_user_workout(custom_path)
        build_expected_timeline()

    def refresh_templates() -> None:
        nonlocal selected_template_label
        rebuild_workout_options()
        filtered = [
            item for item in workout_options if in_band(item.duration_sec, str(band_select.value))
        ]
        if not filtered:
            filtered = workout_options[:]
        filtered_labels = [item.label for item in filtered]
        if not filtered_labels:
            selected_template_label = ""
            state.workout = None
            selected_course_label.text = "Selected course: -"
            course_cards_grid.clear()
            refresh_plan_chart()
            return
        if selected_template_label not in filtered_labels:
            selected_template_label = filtered_labels[0]
        selected_course_label.text = f"Selected course: {selected_template_label}"
        load_selected_workout()

        course_cards_grid.clear()
        with course_cards_grid:
            for option in filtered:
                selected = option.label == selected_template_label
                card_classes = "w-full cursor-pointer"
                if selected:
                    card_classes += " ring-2 ring-cyan-400"
                with ui.card().classes(f"{card_classes} gb-card") as card:
                    ui.label(option.name).classes("text-base font-semibold")
                    ui.label(
                        f"{option.category} | {_fmt_duration(option.duration_sec)} | "
                        f"{option.avg_intensity_pct}% FTP"
                    ).classes("text-xs text-slate-300")
                    ui.label(
                        "Built-in" if option.source == "builtin" else f"Custom: {option.key}"
                    ).classes("text-xs text-slate-500")

                    def on_pick(picked_label: str = option.label) -> None:
                        nonlocal selected_template_label
                        selected_template_label = picked_label
                        load_selected_workout()
                        if state.workout:
                            state.status = f"Loaded course: {state.workout.name}"
                        else:
                            state.status = "No course loaded"
                        refresh_templates()
                        refresh_ui()

                    card.on("click", on_pick)

    def refresh_history() -> None:
        rows: list[dict[str, str]] = []
        for item in load_recent_sessions(limit=12):
            snapshot_hint = item.ended_at_utc.replace(":", "-").split(".")[0]
            rows.append(
                {
                    "ended": item.ended_at_utc.split("T")[0],
                    "status": "OK" if item.completed else "STOP",
                    "workout": item.workout_name,
                    "mins": str(item.elapsed_duration_sec // 60),
                    "snapshot": snapshot_hint,
                }
            )
        history.rows = rows
        history.update()

    def refresh_plan_chart() -> None:
        options = cast(dict[str, Any], plan_chart.options)
        if state.workout is None:
            options["xAxis"]["data"] = []
            options["series"][0]["data"] = []
            plan_chart.update()
            return
        labels: list[str] = []
        watts: list[int] = []
        colors: list[str] = []
        active_index = (state.progress.step_index - 1) if state.progress else -1
        for idx, step in enumerate(state.workout.steps):
            labels.append(step.label or f"Step {idx + 1}")
            watts.append(step.target_watts)
            colors.append("#f59e0b" if idx == active_index else "#22c55e")
        options["xAxis"]["data"] = labels
        options["series"][0]["data"] = [
            {"value": value, "itemStyle": {"color": color}}
            for value, color in zip(watts, colors, strict=False)
        ]
        plan_chart.update()

    def refresh_live_chart() -> None:
        options = cast(dict[str, Any], live_chart.options)
        cadence_expected = timeline_expected_cadence[:]
        if cadence_expected and all(abs(v) < 0.1 for v in cadence_expected):
            cadence_expected = [round(70.0 + (p / 9.0), 1) for p in timeline_expected_power]
        options["xAxis"]["data"] = timeline_labels
        options["series"][0]["data"] = timeline_expected_power
        options["series"][1]["data"] = timeline_actual_power
        options["series"][2]["data"] = cadence_expected
        options["series"][3]["data"] = timeline_actual_cadence

        mark_areas: list[list[dict[str, Any]]] = []
        range_colors = [
            "rgba(34, 211, 238, 0.06)",
            "rgba(99, 102, 241, 0.08)",
            "rgba(14, 165, 233, 0.06)",
        ]
        for idx, (start, end, label) in enumerate(timeline_step_ranges):
            mark_areas.append(
                [
                    {
                        "name": label,
                        "xAxis": start,
                        "itemStyle": {"color": range_colors[idx % len(range_colors)]},
                        "label": {
                            "color": "#ffffff",
                            "fontFamily": "Arial",
                            "fontWeight": "normal",
                            "textBorderWidth": 0,
                            "textShadowBlur": 0,
                        },
                    },
                    {"xAxis": end},
                ]
            )
        options["series"][0]["markArea"] = {"silent": True, "data": mark_areas}
        live_chart.update()

    def refresh_ui() -> None:
        nonlocal last_coaching_alert_key, sound_alerts
        status_label.text = f"Status: {state.status}"
        kpi_power.text = _fmt_power(state.power)
        kpi_cadence.text = _fmt_cadence(state.cadence)
        kpi_speed.text = _fmt_speed(state.speed)
        kpi_distance.text = f"{_fmt_number(state.distance_km, 2)} km"
        mode_label.text = f"Mode: {state.mode.upper()}"
        workout_info.text = state.workout.name if state.workout else "No workout loaded"
        if state.workout:
            total = _fmt_duration(state.workout.total_duration_sec)
            course_info.text = f"{state.workout.name} | total {total} | mode {state.mode.upper()}"
        else:
            course_info.text = "No course loaded"

        expected_power_min = None
        expected_power_max = None
        expected_cadence_min = None
        expected_cadence_max = None
        if state.progress:
            expected_power_min = state.progress.expected_power_min_watts
            expected_power_max = state.progress.expected_power_max_watts
            expected_cadence_min = state.progress.expected_cadence_min_rpm
            expected_cadence_max = state.progress.expected_cadence_max_rpm
            step_info.text = (
                f"Step {state.progress.step_index}/{state.progress.step_total}"
                f" | {state.progress.step_label}"
                f" | total {_fmt_duration(state.progress.total_duration_sec)}"
            )
            elapsed_label.text = f"Elapsed: {_fmt_duration(state.progress.elapsed_total_sec)}"
            remaining_label.text = f"Remaining: {_fmt_duration(state.progress.total_remaining_sec)}"
            if state.workout and state.progress.step_index < state.progress.step_total:
                nxt = state.workout.steps[state.progress.step_index]
                next_step_label.text = (
                    f"Next: {nxt.label or f'Step {state.progress.step_index + 1}'} "
                    f"({nxt.target_watts} W)"
                )
            else:
                next_step_label.text = "Next: finish"
        else:
            step_info.text = "Step: -"
            elapsed_label.text = "Elapsed: 00:00"
            remaining_label.text = "Remaining: 00:00"
            next_step_label.text = "Next: -"
            target_label.text = "Targets: -"
            guidance_label.text = "Action: -"
            guidance_label.style("color: #cbd5e1; font-weight: 600;")
            coaching_stabilizer.reset()
            last_coaching_alert_key = None
        target_bits: list[str] = []
        if expected_power_min is not None and expected_power_max is not None:
            target_bits.append(f"Power {expected_power_min}-{expected_power_max}W")
        if expected_cadence_min is not None and expected_cadence_max is not None:
            target_bits.append(f"Cadence {expected_cadence_min}-{expected_cadence_max}rpm")
        target_label.text = (
            "Targets: " + " | ".join(target_bits)
            if target_bits
            else "Targets: -"
        )

        power_in_zone = in_range(state.power, expected_power_min, expected_power_max)
        cadence_in_zone = in_range(state.cadence, expected_cadence_min, expected_cadence_max)

        power_color = "#38bdf8"
        if power_in_zone is True:
            power_color = "#22c55e"
        elif power_in_zone is False:
            power_color = "#ef4444"

        cadence_color = "#38bdf8"
        if cadence_in_zone is True:
            cadence_color = "#22c55e"
        elif cadence_in_zone is False:
            cadence_color = "#ef4444"

        kpi_power.style(f"color: {power_color};")
        kpi_cadence.style(f"color: {cadence_color};")
        kpi_speed.style("color: #22d3ee;")
        kpi_distance.style("color: #ffffff;")

        if state.progress is not None:
            raw_signal = compute_coaching_signal(
                power=state.power,
                cadence=state.cadence,
                expected_power_min=expected_power_min,
                expected_power_max=expected_power_max,
                expected_cadence_min=expected_cadence_min,
                expected_cadence_max=expected_cadence_max,
            )
            stable_signal, changed = coaching_stabilizer.update(raw_signal, time.monotonic())
            guidance_label.text = stable_signal.text
            guidance_label.style(f"color: {stable_signal.color}; font-weight: 700;")
            if (
                changed
                and sound_alerts
                and stable_signal.severity in {"warn", "bad"}
                and stable_signal.key != last_coaching_alert_key
            ):
                ui.run_javascript(
                    """
                    (() => {
                      const ctx = new (window.AudioContext || window.webkitAudioContext)();
                      const osc = ctx.createOscillator();
                      const gain = ctx.createGain();
                      osc.type = 'sine';
                      osc.frequency.value = 740;
                      gain.gain.value = 0.02;
                      osc.connect(gain);
                      gain.connect(ctx.destination);
                      osc.start();
                      setTimeout(() => { osc.stop(); ctx.close(); }, 120);
                    })();
                    """
                )
            last_coaching_alert_key = stable_signal.key

        p = pct(zone_compliance["power_ok"], zone_compliance["power_total"])
        r = pct(zone_compliance["rpm_ok"], zone_compliance["rpm_total"])
        if p is None and r is None:
            compliance_info.text = "Compliance: -"
        else:
            bits: list[str] = []
            if p is not None:
                bits.append(f"Power {p:.0f}%")
            if r is not None:
                bits.append(f"RPM {r:.0f}%")
            compliance_info.text = "Compliance: " + " | ".join(bits)

        start_btn.set_enabled(state.connected and state.workout is not None)
        stop_btn.set_enabled(state.progress is not None)
        back_btn.set_enabled(state.progress is None)
        connect_btn.set_enabled(not state.connected)
        disconnect_btn.set_enabled(state.connected)
        export_json_btn.set_enabled(current_snapshot_path is not None)
        export_csv_btn.set_enabled(current_snapshot_csv_path is not None)
        refresh_plan_chart()
        refresh_live_chart()

    async def on_scan() -> None:
        nonlocal devices, selected_device_address
        state.status = "Scanning..."
        refresh_ui()
        devices = await controller.scan()
        options = {f"{d.name} ({d.address})": d.address for d in devices}
        device_select.options = options
        if options:
            selected_device_address = next(iter(options.values()))
            device_select.value = selected_device_address
        state.status = f"Scan done: {len(devices)} devices"
        refresh_ui()

    async def on_connect() -> None:
        nonlocal selected_device_address
        selected_device_address = cast(str | None, device_select.value)
        if not selected_device_address:
            state.status = "Select a device first"
            refresh_ui()
            return
        state.status = "Connecting..."
        refresh_ui()
        label = await controller.connect(
            target=selected_device_address,
            metrics_callback=on_metrics,
        )
        state.connected = True
        state.status = f"Connected: {label}"
        refresh_ui()

    async def on_disconnect() -> None:
        await controller.disconnect()
        state.connected = False
        state.status = "Disconnected"
        refresh_ui()

    def build_expected_timeline() -> None:
        timeline_labels.clear()
        timeline_expected_power.clear()
        timeline_expected_cadence.clear()
        timeline_actual_power.clear()
        timeline_actual_cadence.clear()
        timeline_step_ranges.clear()
        if state.workout is None:
            return

        elapsed = 0
        for step in state.workout.steps:
            step_start_idx = len(timeline_labels)
            cadence_target = None
            if step.cadence_min_rpm is not None and step.cadence_max_rpm is not None:
                cadence_target = (step.cadence_min_rpm + step.cadence_max_rpm) / 2.0
            for _ in range(0, step.duration_sec, TIMELINE_SAMPLE_SEC):
                timeline_labels.append(_fmt_timeline_mark(elapsed))
                timeline_expected_power.append(step.target_watts)
                timeline_expected_cadence.append(cadence_target or 0.0)
                timeline_actual_power.append(None)
                timeline_actual_cadence.append(None)
                elapsed += TIMELINE_SAMPLE_SEC
            step_end_idx = max(step_start_idx, len(timeline_labels) - 1)
            timeline_step_ranges.append((step_start_idx, step_end_idx, step.label or "Step"))

        # Add final point to make end-of-workout progress explicit.
        timeline_labels.append(_fmt_timeline_mark(state.workout.total_duration_sec))
        timeline_expected_power.append(
            timeline_expected_power[-1] if timeline_expected_power else 0
        )
        timeline_expected_cadence.append(
            timeline_expected_cadence[-1] if timeline_expected_cadence else 0.0
        )
        timeline_actual_power.append(None)
        timeline_actual_cadence.append(None)

    def on_metrics(metrics: IndoorBikeData) -> None:
        now = asyncio.get_event_loop().time()
        if state.last_ts is not None and metrics.instantaneous_speed_kmh is not None:
            state.distance_km += (
                metrics.instantaneous_speed_kmh * (now - state.last_ts)
            ) / 3600.0
        state.last_ts = now
        state.power = metrics.instantaneous_power
        state.cadence = metrics.instantaneous_cadence
        state.speed = metrics.instantaneous_speed_kmh
        metric_samples.append(
            (
                metrics.instantaneous_power,
                metrics.instantaneous_cadence,
                metrics.instantaneous_speed_kmh,
            )
        )

        if state.progress:
            if (
                metrics.instantaneous_power is not None
                and state.progress.expected_power_min_watts is not None
                and state.progress.expected_power_max_watts is not None
            ):
                zone_compliance["power_total"] += 1
                if (
                    state.progress.expected_power_min_watts
                    <= metrics.instantaneous_power
                    <= state.progress.expected_power_max_watts
                ):
                    zone_compliance["power_ok"] += 1

            if (
                metrics.instantaneous_cadence is not None
                and state.progress.expected_cadence_min_rpm is not None
                and state.progress.expected_cadence_max_rpm is not None
            ):
                zone_compliance["rpm_total"] += 1
                if (
                    state.progress.expected_cadence_min_rpm
                    <= metrics.instantaneous_cadence
                    <= state.progress.expected_cadence_max_rpm
                ):
                    zone_compliance["rpm_ok"] += 1

            if timeline_labels:
                idx = min(
                    len(timeline_labels) - 1,
                    max(0, int(state.progress.elapsed_total_sec / TIMELINE_SAMPLE_SEC)),
                )
                if metrics.instantaneous_power is not None:
                    timeline_actual_power[idx] = metrics.instantaneous_power
                if metrics.instantaneous_cadence is not None:
                    timeline_actual_cadence[idx] = metrics.instantaneous_cadence

    def on_progress(progress: WorkoutProgress) -> None:
        state.progress = progress

    def on_finish(completed: bool) -> None:
        _save_session_snapshot(completed)
        state.progress = None
        state.status = "Workout completed" if completed else "Workout stopped"
        refresh_history()
        refresh_ui()

    async def on_start() -> None:
        nonlocal session_started_at_utc, current_snapshot_path, current_snapshot_csv_path
        if state.workout is None:
            return
        zone_compliance["power_ok"] = 0
        zone_compliance["power_total"] = 0
        zone_compliance["rpm_ok"] = 0
        zone_compliance["rpm_total"] = 0
        build_expected_timeline()
        state.distance_km = 0.0
        state.last_ts = None
        metric_samples.clear()
        coaching_stabilizer.reset()
        session_started_at_utc = now_utc_iso()
        current_snapshot_path = None
        current_snapshot_csv_path = None
        state.mode = cast(TargetMode, mode_select.value or "erg")
        state.ftp_watts = int(ftp_input.value or 220)
        delay_sec = max(0, int(delay_input.value or 0))
        if delay_sec > 0:
            show_workout_screen()
            for remaining in range(delay_sec, 0, -1):
                state.status = f"Starting in {remaining}s - get ready"
                refresh_ui()
                await asyncio.sleep(1.0)
        await controller.start_workout(
            state.workout,
            target_mode=state.mode,
            ftp_watts=state.ftp_watts,
            on_progress=on_progress,
            on_finish=on_finish,
        )
        state.status = "Workout started"
        show_workout_screen()
        refresh_ui()

    async def on_stop() -> None:
        await controller.stop_workout()
        state.status = "Stopping workout..."
        refresh_ui()

    def on_back_to_setup() -> None:
        show_setup_screen()
        refresh_ui()

    def on_export_json() -> None:
        if current_snapshot_path is None or not current_snapshot_path.exists():
            ui.notify("No snapshot available yet", color="negative")
            return
        ui.download(str(current_snapshot_path), filename=current_snapshot_path.name)

    def on_export_csv() -> None:
        if current_snapshot_csv_path is None or not current_snapshot_csv_path.exists():
            ui.notify("No CSV export available yet", color="negative")
            return
        ui.download(str(current_snapshot_csv_path), filename=current_snapshot_csv_path.name)

    builder_steps: list[WorkoutStep] = []

    def refresh_builder_table() -> None:
        rows: list[dict[str, str | int]] = []
        for idx, step in enumerate(builder_steps, start=1):
            cad_txt = "-"
            if step.cadence_min_rpm is not None and step.cadence_max_rpm is not None:
                cad_txt = f"{step.cadence_min_rpm}-{step.cadence_max_rpm}"
            rows.append(
                {
                    "idx": idx,
                    "label": step.label or f"Step {idx}",
                    "dur": step.duration_sec,
                    "watts": step.target_watts,
                    "cad": cad_txt,
                }
            )
        builder_steps_table.rows = rows
        builder_steps_table.update()

    def on_open_builder() -> None:
        builder_steps.clear()
        refresh_builder_table()
        builder_dialog.open()

    def on_add_builder_step() -> None:
        rpm_min = int(step_rpm_min_input.value or 0)
        rpm_max = int(step_rpm_max_input.value or 0)
        if rpm_max < rpm_min:
            ui.notify("Cadence max must be >= cadence min", color="negative")
            return
        builder_steps.append(
            WorkoutStep(
                duration_sec=int(step_sec_input.value or 180),
                target_watts=int(step_watts_input.value or 200),
                label=str(step_label_input.value or f"Step {len(builder_steps) + 1}"),
                cadence_min_rpm=rpm_min if rpm_min > 0 else None,
                cadence_max_rpm=rpm_max if rpm_max > 0 else None,
            )
        )
        refresh_builder_table()

    def on_remove_builder_step() -> None:
        if builder_steps:
            builder_steps.pop()
            refresh_builder_table()

    def on_save_builder_workout() -> None:
        if not builder_steps:
            ui.notify("Add at least one step", color="negative")
            return
        saved = save_user_workout(
            name=str(builder_name.value or "Custom Workout"),
            category=str(builder_category.value or "Custom"),
            steps=builder_steps,
            overwrite_key=str(builder_key.value).strip() or None,
        )
        ui.notify(f"Template saved: {saved.name}", color="positive")
        builder_dialog.close()
        refresh_templates()
        refresh_ui()

    def on_strict_change() -> None:
        nonlocal strict_mode
        strict_mode = bool(strict_switch.value)
        apply_layout_mode()
        refresh_ui()

    def on_preset_change() -> None:
        nonlocal viewport_preset
        viewport_preset = str(preset_select.value or "auto")
        apply_layout_mode()
        refresh_ui()

    def on_ftp_or_mode_change() -> None:
        load_selected_workout()
        refresh_ui()

    def on_sound_toggle() -> None:
        nonlocal sound_alerts
        sound_alerts = bool(sound_toggle.value)

    band_select.on_value_change(lambda _: refresh_templates())
    ftp_input.on_value_change(lambda _: on_ftp_or_mode_change())
    mode_select.on_value_change(lambda _: on_ftp_or_mode_change())
    strict_switch.on_value_change(lambda _: on_strict_change())
    preset_select.on_value_change(lambda _: on_preset_change())
    sound_toggle.on_value_change(lambda _: on_sound_toggle())
    scan_btn.on_click(on_scan)
    connect_btn.on_click(on_connect)
    disconnect_btn.on_click(on_disconnect)
    builder_btn.on_click(on_open_builder)
    add_step_btn.on_click(on_add_builder_step)
    remove_last_step_btn.on_click(on_remove_builder_step)
    save_builder_btn.on_click(on_save_builder_workout)
    cancel_builder_btn.on_click(builder_dialog.close)
    start_btn.on_click(on_start)
    stop_btn.on_click(on_stop)
    back_btn.on_click(on_back_to_setup)
    export_json_btn.on_click(on_export_json)
    export_csv_btn.on_click(on_export_csv)

    refresh_templates()
    refresh_history()
    apply_layout_mode()
    show_setup_screen()
    ui.timer(0.5, refresh_ui)
    ui.run(host=host, port=port, reload=False, title="Velox Engine Web UI")
    return 0
