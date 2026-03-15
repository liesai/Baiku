"""NiceGUI web UI for Velox Engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import time
from typing import Any, cast
from uuid import uuid4

from nicegui import app, core, ui

from backend.ble.ftms_client import IndoorBikeData, ScannedDevice
from backend.ui.coaching import ActionStabilizer, compute_coaching_signal
from backend.ui.controller import UIController
from backend.ui.game_layer import DEFAULT_GAME_GOALS, GoalTracker
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
HT_CONNECT_TIMEOUT_SEC = 40.0
ASSETS_ROUTE = "/velox-assets"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SPRITE_URL = f"{ASSETS_ROUTE}/cyclist_sprite_user_v3.png"
SCENE_BG_URL = f"{ASSETS_ROUTE}/forest_bg.png"
SCENE_BG_ALT_1_URL = f"{ASSETS_ROUTE}/forest_bg_alt_1.jpg"
SCENE_BG_ALT_2_URL = f"{ASSETS_ROUTE}/forest_bg_alt_2.jpg"
FOREST_PARALLAX_BACK_URL = f"{ASSETS_ROUTE}/parallax/forest/back.png"
FOREST_PARALLAX_MID_URL = f"{ASSETS_ROUTE}/parallax/forest/mid.png"
FOREST_PARALLAX_FRONT_URL = f"{ASSETS_ROUTE}/parallax/forest/front.png"
FOREST_PARALLAX_OVERLAY_URL = f"{ASSETS_ROUTE}/parallax/forest/overlay.png"
ALPINE_PARALLAX_SKY_URL = f"{ASSETS_ROUTE}/parallax/alpine/sky.png"
ALPINE_PARALLAX_FAR_URL = f"{ASSETS_ROUTE}/parallax/alpine/far.png"
ALPINE_PARALLAX_MID_URL = f"{ASSETS_ROUTE}/parallax/alpine/mid.png"
ALPINE_PARALLAX_CLOUDS_URL = f"{ASSETS_ROUTE}/parallax/alpine/clouds.png"
NEON_PARALLAX_STARS_URL = f"{ASSETS_ROUTE}/parallax/neon/stars.png"
NEON_PARALLAX_BACK_URL = f"{ASSETS_ROUTE}/parallax/neon/back.png"
NEON_PARALLAX_MID_URL = f"{ASSETS_ROUTE}/parallax/neon/mid.png"
NEON_PARALLAX_FRONT_URL = f"{ASSETS_ROUTE}/parallax/neon/front.png"
NEON_PARALLAX_CLOUDS_URL = f"{ASSETS_ROUTE}/parallax/neon/clouds.png"
DMD_CYCLIST_URL = f"{ASSETS_ROUTE}/dmd_cyclist_bonus.png"
THREE_MODULE_URL = f"{ASSETS_ROUTE}/vendor/three.module.js"
_ASSETS_MOUNTED = False


@dataclass
class WebState:
    connected: bool = False
    hm_connected: bool = False
    status: str = "Not connected"
    ht_device_name: str | None = None
    hm_device_name: str | None = None
    erg_ready: bool | None = None
    ht_busy: bool = False
    workout: WorkoutPlan | None = None
    progress: WorkoutProgress | None = None
    power: int | None = None
    cadence: float | None = None
    heart_rate_bpm: int | None = None
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


def _phase_cue_text(progress: WorkoutProgress) -> str:
    countdown = progress.transition_countdown_sec
    if countdown is None:
        return "-"
    if countdown > 0:
        return str(countdown)
    label = (progress.transition_label or progress.step_label).lower()
    if any(token in label for token in ("recover", "off", "facile", "souple", "cool")):
        return "RECUP"
    return "GO"


def _phase_cue_kind(progress: WorkoutProgress) -> str:
    label = (progress.transition_label or progress.step_label).lower()
    if any(token in label for token in ("recover", "off", "facile", "souple", "cool")):
        return "phase-recover"
    return "phase-effort"


def _transition_status_text(progress: WorkoutProgress) -> str:
    countdown = progress.transition_countdown_sec
    label = progress.transition_label or progress.step_label
    if countdown is None:
        return "Transition: -"
    if countdown > 0:
        return f"Transition dans {countdown}s: {label}"
    return f"Phase: {label}"


def _fmt_timeline_mark(total_seconds: int) -> str:
    rounded_min = int(round((total_seconds / 60.0) / 10.0) * 10)
    if rounded_min % 10 != 0:
        return ""
    # Show sparse markers only, every 10 minutes.
    if abs((rounded_min * 60) - total_seconds) > TIMELINE_SAMPLE_SEC:
        return ""
    return f"{rounded_min:02d}:00"


def _fmt_device_label(device: ScannedDevice) -> str:
    icon = "🚴" if device.has_ftms else "📶"
    details = device.name
    if device.manufacturer:
        details = f"{details} ({device.manufacturer})"
    return (
        f"{icon} {details} • {device.address} • RSSI {device.rssi}"
    )


def _ht_candidates(devices: list[ScannedDevice]) -> list[ScannedDevice]:
    return [d for d in devices if d.has_ftms]


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
    ble_pair: bool = True,
    host: str = "127.0.0.1",
    port: int = 8088,
    start_delay_sec: int = 10,
    ui_theme: str = "classic",
) -> int:
    global _ASSETS_MOUNTED
    if not _ASSETS_MOUNTED:
        try:
            app.add_static_files(ASSETS_ROUTE, str(ASSETS_DIR))
        except Exception:
            # Route might already be mounted during hot reload.
            pass
        _ASSETS_MOUNTED = True

    controller = UIController(
        debug_ftms=False,
        simulate_ht=simulate_ht,
        ble_pair=ble_pair,
    )
    state = WebState()
    pinball_mode = ui_theme == "pinball"
    csp_safe_mode = pinball_mode and os.getenv("VELOX_UI_CSP_SAFE", "").lower() in {
        "1",
        "true",
        "yes",
    }
    ui.add_head_html(
        """
        <script type="module">
          import * as THREE from '__THREE_MODULE_URL__';
          window.THREE = THREE;
          window.dispatchEvent(new CustomEvent('velox-three-ready'));
        </script>
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
          body.gb-theme-pinball {
            background:
              radial-gradient(circle at 20% 10%, #1d0b2e 0%, rgba(29,11,46,0) 45%),
              radial-gradient(circle at 80% 0%, #052b45 0%, rgba(5,43,69,0) 42%),
              linear-gradient(180deg, #090c1d 0%, #0a1631 100%);
          }
          body.gb-theme-pinball .gb-card {
            border: 1px solid rgba(250, 204, 21, 0.35);
            box-shadow:
              0 0 0 1px rgba(244, 114, 182, 0.15),
              0 8px 24px rgba(2, 6, 23, 0.42),
              inset 0 0 18px rgba(56, 189, 248, 0.1);
          }
          .pinball-chip {
            border: 1px solid rgba(250, 204, 21, 0.35);
            border-radius: 10px;
            background: rgba(15, 23, 42, 0.45);
            padding: 4px 8px;
            font-size: .8rem;
            font-weight: 700;
            color: #f8fafc;
          }
          .pinball-jackpot {
            color: #facc15;
            text-shadow: 0 0 10px rgba(250, 204, 21, 0.6);
          }
          .dmd-shell {
            position: relative;
            border: 1px solid rgba(250, 204, 21, 0.35);
            border-radius: 12px;
            background: linear-gradient(180deg, #190e06 0%, #100702 100%);
            padding: 8px;
            box-shadow:
              inset 0 0 0 1px rgba(255, 181, 41, 0.16),
              inset 0 0 30px rgba(255, 120, 0, 0.14),
              0 8px 18px rgba(2, 6, 23, 0.42);
          }
          .dmd-bg {
            position: absolute;
            left: 50%;
            top: 50%;
            width: 220px;
            height: 88px;
            object-fit: cover;
            transform: translate(-50%, -50%);
            opacity: 0.14;
            pointer-events: none;
            image-rendering: pixelated;
            filter: saturate(0.95) contrast(1.02);
          }
          .dmd-screen {
            position: relative;
            z-index: 2;
            width: 100%;
            height: 96px;
            border-radius: 8px;
            background: #090303;
            display: block;
            image-rendering: pixelated;
          }
          .mini-graph-shell {
            border: 1px solid rgba(56, 189, 248, 0.28);
            border-radius: 10px;
            background: rgba(2, 6, 23, 0.35);
            padding: 6px;
          }
          .mini-graph {
            width: 100%;
            height: 86px;
            display: block;
            border-radius: 8px;
            background: rgba(2, 6, 23, 0.55);
          }
          .gb-pixel {
            font-family: "Courier New", monospace;
            font-weight: 700;
            letter-spacing: 0.02em;
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
          .ve-scene {
            display: block;
            position: relative;
            width: 100%;
            min-width: 100%;
            height: 164px;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 12px;
            overflow: hidden;
            background: linear-gradient(180deg, #0a2a4e 0%, #15426d 58%, #0f2f52 100%);
            --ve-bg-far-offset: 0px;
            --ve-bg-mid-offset: 0px;
            --ve-bg-front-offset: 0px;
            --ve-road-offset: 0px;
            --ve-pedal-rot: 0deg;
            --ve-rider-bob: 0px;
            --ve-sprite-shift-x: 0px;
            --ve-rider-tilt: -2deg;
            --ve-rider-scale: .88;
            --ve-rider-shadow-opacity: .26;
            --ve-rider-shadow-blur: 10px;
            --ve-rider-pulse: 0;
            --ve-sprite-rock: 0deg;
            --ve-sprite-lift: 0px;
            --ve-rider-dust-opacity: .18;
            --ve-rider-speed-opacity: .08;
            --ve-accent: rgba(56, 189, 248, 0.35);
            --ve-glow: rgba(34, 211, 238, 0.18);
            --ve-road-line: rgba(248, 250, 252, 0.9);
            --ve-scene-boost: 1;
            --ve-overlay-opacity: .9;
          }
          .ve-scene[data-zone="ok"] { box-shadow: inset 0 0 0 2px rgba(34,197,94,.25); }
          .ve-scene[data-zone="bad"] { box-shadow: inset 0 0 0 2px rgba(239,68,68,.25); }
          .ve-scene[data-action="up"] .ve-hud-action { color: #f59e0b; }
          .ve-scene[data-action="down"] .ve-hud-action { color: #ef4444; }
          .ve-scene[data-action="steady"] .ve-hud-action { color: #22c55e; }
          .ve-scene[data-intensity="low"] {
            --ve-scene-boost: .92;
            --ve-overlay-opacity: .58;
            --ve-rider-tilt: -1deg;
            --ve-rider-scale: .84;
            --ve-rider-shadow-opacity: .18;
            --ve-rider-dust-opacity: .08;
            --ve-rider-speed-opacity: 0;
          }
          .ve-scene[data-intensity="mid"] {
            --ve-scene-boost: 1;
            --ve-overlay-opacity: .86;
            --ve-rider-tilt: -2deg;
            --ve-rider-scale: .88;
            --ve-rider-shadow-opacity: .24;
            --ve-rider-dust-opacity: .16;
            --ve-rider-speed-opacity: .04;
          }
          .ve-scene[data-intensity="high"] {
            --ve-scene-boost: 1.12;
            --ve-overlay-opacity: 1;
            --ve-rider-tilt: -4deg;
            --ve-rider-scale: .93;
            --ve-rider-shadow-opacity: .32;
            --ve-rider-shadow-blur: 14px;
            --ve-rider-dust-opacity: .28;
            --ve-rider-speed-opacity: .1;
          }
          .ve-scene[data-theme="forest"] {
            background:
              linear-gradient(180deg, rgba(15, 90, 88, 0.18) 0%, rgba(8, 23, 32, 0) 45%),
              linear-gradient(180deg, #0c2740 0%, #17425f 56%, #12263b 100%);
            --ve-accent: rgba(74, 222, 128, 0.28);
            --ve-glow: rgba(34, 197, 94, 0.16);
            --ve-road-line: rgba(226, 232, 240, 0.92);
          }
          .ve-scene[data-theme="alpine"] {
            background:
              linear-gradient(180deg, rgba(253, 186, 116, 0.28) 0%, rgba(251, 191, 36, 0.02) 38%),
              linear-gradient(180deg, #132a58 0%, #27538d 52%, #153258 100%);
            --ve-accent: rgba(253, 186, 116, 0.3);
            --ve-glow: rgba(251, 191, 36, 0.16);
            --ve-road-line: rgba(255, 248, 220, 0.9);
          }
          .ve-scene[data-theme="neon"] {
            background:
              radial-gradient(circle at 50% -8%, rgba(244, 114, 182, 0.16) 0%, rgba(244, 114, 182, 0) 28%),
              linear-gradient(180deg, #030712 0%, #071426 24%, #0b1b35 58%, #050816 100%);
            --ve-accent: rgba(167, 139, 250, 0.4);
            --ve-glow: rgba(244, 114, 182, 0.18);
            --ve-road-line: rgba(45, 212, 191, 0.9);
          }
          .ve-three-layer {
            position: absolute;
            inset: 0;
            z-index: 2;
            opacity: 0;
            transition: opacity .24s ease-out;
            pointer-events: none;
          }
          .ve-three-canvas {
            display: block;
            width: 100%;
            height: 100%;
          }
          .ve-three-edge-fade {
            position: absolute;
            top: 0;
            bottom: 0;
            width: 8%;
            z-index: 7;
            pointer-events: none;
          }
          .ve-three-edge-fade.left {
            left: 0;
            background:
              linear-gradient(90deg, rgba(3,7,18,.95) 0%, rgba(3,7,18,.58) 34%, rgba(3,7,18,0) 100%);
          }
          .ve-three-edge-fade.right {
            right: 0;
            background:
              linear-gradient(270deg, rgba(3,7,18,.95) 0%, rgba(3,7,18,.58) 34%, rgba(3,7,18,0) 100%);
          }
          .ve-three-debug {
            position: absolute;
            left: 10px;
            top: 8px;
            z-index: 9;
            font-size: 0.62rem;
            font-weight: 700;
            letter-spacing: .04em;
            text-transform: uppercase;
            color: #cbd5e1;
            background: rgba(2, 6, 23, 0.58);
            border: 1px solid rgba(148, 163, 184, 0.28);
            border-radius: 999px;
            padding: 2px 7px;
            backdrop-filter: blur(4px);
          }
          .ve-three-debug.ok {
            color: #a7f3d0;
            border-color: rgba(45, 212, 191, 0.42);
          }
          .ve-three-debug.warn {
            color: #fde68a;
            border-color: rgba(251, 191, 36, 0.42);
          }
          .ve-three-debug.err {
            color: #fca5a5;
            border-color: rgba(248, 113, 113, 0.42);
          }
          .ve-scene[data-theme="neon"] .ve-three-layer {
            opacity: 1;
          }
          .ve-scene[data-theme="neon"][data-three-ready="1"] .ve-bg,
          .ve-scene[data-theme="neon"][data-three-ready="1"] .ve-road,
          .ve-scene[data-theme="neon"][data-three-ready="1"] .ve-rider {
            opacity: 0;
            visibility: hidden;
          }
          .ve-bg {
            position: absolute;
            left: 0;
            top: 0;
            right: 0;
            bottom: 0;
            background-repeat: repeat-x;
            image-rendering: pixelated;
          }
          .ve-bg-sky {
            background:
              radial-gradient(circle at 22% 20%, rgba(255,255,255,0.28) 0 8%, rgba(255,255,255,0) 9%),
              radial-gradient(circle at 70% 18%, rgba(255,255,255,0.22) 0 7%, rgba(255,255,255,0) 8%),
              linear-gradient(180deg, rgba(255,255,255,0.12) 0%, rgba(255,255,255,0) 42%);
            opacity: .9;
          }
          .ve-bg-far {
            bottom: 24px;
            background-image: url('__FOREST_PARALLAX_BACK_URL__');
            background-size: auto 148%;
            background-position: var(--ve-bg-far-offset) center;
            opacity: .52;
            filter: saturate(1.04) blur(.15px);
          }
          .ve-bg-mid {
            bottom: 8px;
            background-image: url('__FOREST_PARALLAX_MID_URL__');
            background-size: auto 156%;
            background-position: var(--ve-bg-mid-offset) center;
            opacity: .92;
            filter: saturate(1.08) contrast(1.04);
          }
          .ve-bg-front {
            top: auto;
            bottom: 24px;
            height: 96px;
            background-image: url('__FOREST_PARALLAX_FRONT_URL__');
            background-size: auto 162%;
            background-position: var(--ve-bg-front-offset) bottom;
            opacity: .82;
            filter: saturate(1.12) contrast(1.06);
            mix-blend-mode: normal;
            overflow: hidden;
          }
          .ve-bg-front::before {
            content: "";
            position: absolute;
            inset: 0;
            background-image: url('__FOREST_PARALLAX_FRONT_URL__');
            background-repeat: repeat-x;
            background-size: auto 138%;
            background-position: calc(var(--ve-bg-front-offset) * -0.68) bottom;
            opacity: .34;
            transform: scaleX(-1) translateX(10%);
            transform-origin: center;
            filter: saturate(1.02) brightness(.94);
          }
          .ve-bg-overlay {
            background:
              url('__FOREST_PARALLAX_OVERLAY_URL__'),
              radial-gradient(circle at 50% 35%, var(--ve-glow) 0%, rgba(2,6,23,0) 42%);
            background-repeat: repeat-x, no-repeat;
            background-size: auto 134%, 100% 100%;
            background-position: calc(var(--ve-bg-front-offset) * 0.78) bottom, center;
            opacity: var(--ve-overlay-opacity);
          }
          .ve-scene[data-theme="alpine"] .ve-bg-sky {
            background:
              url('__ALPINE_PARALLAX_SKY_URL__'),
              linear-gradient(180deg, rgba(253,186,116,0.28) 0%, rgba(255,255,255,0.02) 46%);
            background-repeat: repeat-x, no-repeat;
            background-size: auto 100%, 100% 100%;
            background-position: center, center;
          }
          .ve-scene[data-theme="alpine"] .ve-bg-far {
            top: 14px;
            bottom: 32px;
            background-image: url('__ALPINE_PARALLAX_CLOUDS_URL__');
            background-size: auto 134%;
            background-position: var(--ve-bg-far-offset) center;
            background-repeat: repeat-x;
            opacity: calc(.52 * var(--ve-scene-boost));
            filter: none;
          }
          .ve-scene[data-theme="alpine"] .ve-bg-mid {
            top: 28px;
            bottom: 14px;
            background-image: url('__ALPINE_PARALLAX_FAR_URL__');
            background-size: auto 146%;
            background-position: var(--ve-bg-mid-offset) bottom;
            background-repeat: repeat-x;
            opacity: .84;
            filter: saturate(calc(1.02 * var(--ve-scene-boost)));
          }
          .ve-scene[data-theme="alpine"] .ve-bg-front {
            top: auto;
            bottom: 18px;
            height: 112px;
            background-image: url('__ALPINE_PARALLAX_MID_URL__');
            background-size: auto 148%;
            background-position: var(--ve-bg-front-offset) bottom;
            background-repeat: repeat-x;
            opacity: calc(.9 * var(--ve-scene-boost));
            filter: saturate(1.08);
            mix-blend-mode: normal;
          }
          .ve-scene[data-theme="alpine"] .ve-bg-overlay {
            background:
              radial-gradient(circle at 48% 28%, rgba(255,244,214,0.16) 0%, rgba(255,244,214,0) 32%),
              linear-gradient(180deg, rgba(255,255,255,0.04) 0%, rgba(2,6,23,0) 56%);
            opacity: var(--ve-overlay-opacity);
          }
          .ve-scene[data-theme="neon"] .ve-bg-sky {
            background:
              url('__NEON_PARALLAX_STARS_URL__'),
              radial-gradient(circle at 50% 0%, rgba(56, 189, 248, 0.1) 0%, rgba(56, 189, 248, 0) 26%),
              linear-gradient(180deg, rgba(8,15,32,0.2) 0%, rgba(255,255,255,0) 46%);
            background-repeat: repeat-x, no-repeat;
            background-size: auto 100%, 100% 100%, 100% 100%;
            background-position: center, center, center;
            opacity: .9;
          }
          .ve-scene[data-theme="neon"] .ve-bg-far {
            top: 12px;
            bottom: 26px;
            background-image: url('__NEON_PARALLAX_CLOUDS_URL__');
            background-size: auto 164%;
            background-position: var(--ve-bg-far-offset) center;
            background-repeat: repeat-x;
            opacity: calc(.18 * var(--ve-scene-boost));
            filter: hue-rotate(22deg) saturate(.9) blur(1.3px);
          }
          .ve-scene[data-theme="neon"] .ve-bg-mid {
            top: 30px;
            bottom: 18px;
            background-image: url('__NEON_PARALLAX_BACK_URL__');
            background-size: auto 166%;
            background-position: var(--ve-bg-mid-offset) bottom;
            background-repeat: repeat-x;
            opacity: .7;
            filter:
              hue-rotate(18deg)
              saturate(calc(1.12 * var(--ve-scene-boost)))
              brightness(.88)
              blur(.45px);
          }
          .ve-scene[data-theme="neon"] .ve-bg-front {
            top: auto;
            bottom: 18px;
            height: 126px;
            background-image:
              url('__NEON_PARALLAX_FRONT_URL__'),
              url('__NEON_PARALLAX_MID_URL__');
            background-size: auto 158%, auto 142%;
            background-position: var(--ve-bg-front-offset) bottom, calc(var(--ve-bg-front-offset) * 0.72) bottom;
            background-repeat: repeat-x, repeat-x;
            opacity: calc(.88 * var(--ve-scene-boost));
            filter: saturate(1.18) brightness(1.02);
            mix-blend-mode: screen;
            overflow: hidden;
          }
          .ve-scene[data-theme="neon"] .ve-bg-front::before {
            content: "";
            position: absolute;
            inset: 0;
            background:
              repeating-linear-gradient(
                90deg,
                rgba(45,212,191,0) 0 144px,
                rgba(45,212,191,.84) 144px 150px,
                rgba(103,232,249,.94) 150px 154px,
                rgba(45,212,191,0) 154px 260px
              ),
              repeating-linear-gradient(
                90deg,
                rgba(251,191,36,0) 0 156px,
                rgba(251,191,36,.44) 156px 160px,
                rgba(251,191,36,0) 160px 266px
              );
            background-position: var(--ve-bg-front-offset) bottom, calc(var(--ve-bg-front-offset) * 1.2) bottom;
            opacity: .42;
            mix-blend-mode: screen;
          }
          .ve-scene[data-theme="neon"] .ve-bg-front::after {
            content: "";
            position: absolute;
            left: -8%;
            right: -8%;
            bottom: -1px;
            height: 18px;
            background:
              linear-gradient(180deg, rgba(45,212,191,0.24) 0%, rgba(45,212,191,0.06) 26%, rgba(0,0,0,0) 28%),
              repeating-linear-gradient(
                90deg,
                rgba(255,255,255,0) 0 30px,
                rgba(148,163,184,.2) 30px 32px,
                rgba(255,255,255,0) 32px 38px
              );
            opacity: .45;
          }
          .ve-scene[data-theme="neon"] .ve-bg-overlay {
            background:
              radial-gradient(circle at 50% 100%, rgba(56, 189, 248, 0.14) 0%, rgba(56, 189, 248, 0) 42%),
              linear-gradient(180deg, rgba(244,114,182,0.05) 0%, rgba(34,211,238,0) 48%),
              radial-gradient(circle at 52% 30%, rgba(34,211,238,0.2) 0%, rgba(34,211,238,0) 34%),
              linear-gradient(180deg, rgba(2,6,23,0) 62%, rgba(2,6,23,.22) 100%);
            opacity: var(--ve-overlay-opacity);
          }
          .ve-road {
            position: absolute;
            left: -6%;
            right: -6%;
            bottom: -8px;
            height: 56px;
            background:
              linear-gradient(180deg, rgba(148,163,184,0.16) 0%, rgba(15,23,42,0) 20%),
              linear-gradient(180deg, rgba(15,23,42,0.1) 0%, rgba(15,23,42,0.75) 100%);
            clip-path: polygon(12% 0%, 88% 0%, 100% 100%, 0% 100%);
            z-index: 4;
          }
          .ve-road::before {
            content: "";
            position: absolute;
            inset: 0;
            background:
              repeating-linear-gradient(
                90deg,
                rgba(255,255,255,0) 0 36px,
                var(--ve-road-line) 36px 58px,
                rgba(255,255,255,0) 58px 110px
              );
            background-position-x: var(--ve-road-offset);
            opacity: .55;
          }
          .ve-scene[data-intensity="high"] .ve-road::before {
            opacity: .9;
            filter: drop-shadow(0 0 8px var(--ve-road-line));
          }
          .ve-scene[data-intensity="low"] .ve-road::before {
            opacity: .38;
          }
          .ve-road::after {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(180deg, rgba(15,23,42,0) 0%, rgba(15,23,42,0.64) 100%);
          }
          .ve-scene[data-theme="neon"] .ve-road {
            background:
              linear-gradient(180deg, rgba(45,212,191,0.12) 0%, rgba(15,23,42,0) 20%),
              linear-gradient(180deg, rgba(15,23,42,0.04) 0%, rgba(2,6,23,0.82) 100%);
          }
          .ve-scene[data-theme="neon"] .ve-road::before {
            background:
              repeating-linear-gradient(
                90deg,
                rgba(255,255,255,0) 0 36px,
                rgba(94,234,212,.95) 36px 58px,
                rgba(255,255,255,0) 58px 110px
              ),
              repeating-linear-gradient(
                90deg,
                rgba(255,255,255,0) 0 120px,
                rgba(251,191,36,.22) 120px 124px,
                rgba(255,255,255,0) 124px 240px
              );
            background-position-x: var(--ve-road-offset), calc(var(--ve-road-offset) * 1.18);
            background-position-y: 0, 0;
            opacity: .68;
            box-shadow: inset 0 -10px 18px rgba(2,6,23,.4);
          }
          .ve-scene[data-theme="neon"] .ve-road::after {
            background:
              linear-gradient(180deg, rgba(125,211,252,.1) 0%, rgba(15,23,42,0) 18%),
              linear-gradient(180deg, rgba(15,23,42,0) 0%, rgba(2,6,23,.7) 100%);
          }
          .ve-scene[data-theme="neon"] .ve-rider-speedlines {
            background:
              repeating-linear-gradient(
                180deg,
                rgba(255,255,255,0) 0 4px,
                rgba(103,232,249,0.92) 4px 6px,
                rgba(255,255,255,0) 6px 12px
              );
          }
          .ve-scene[data-theme="neon"]::before {
            content: "";
            position: absolute;
            inset: 0;
            pointer-events: none;
            background:
              linear-gradient(180deg, rgba(0,0,0,0) 0%, rgba(71,85,105,.08) 58%, rgba(2,6,23,.2) 100%),
              radial-gradient(circle at 50% 54%, rgba(34,211,238,0) 0 44%, rgba(2,6,23,.24) 100%);
            z-index: 6;
          }
          .ve-scene[data-theme="neon"]::after {
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            top: 0;
            bottom: 0;
            pointer-events: none;
            background:
              radial-gradient(circle at 10% 50%, rgba(2,6,23,.34) 0%, rgba(2,6,23,0) 24%),
              radial-gradient(circle at 90% 50%, rgba(2,6,23,.34) 0%, rgba(2,6,23,0) 24%);
            z-index: 6;
          }
          .ve-scene[data-theme="alpine"] .ve-road {
            background:
              linear-gradient(180deg, rgba(255,244,214,0.14) 0%, rgba(15,23,42,0) 20%),
              linear-gradient(180deg, rgba(30,41,59,0.1) 0%, rgba(30,41,59,0.82) 100%);
          }
          .ve-scenery-badge {
            font-size: 0.68rem;
            font-weight: 700;
            letter-spacing: .08em;
            text-transform: uppercase;
            color: #bfdbfe;
            padding: 0.22rem 0.45rem;
            border-radius: 999px;
            border: 1px solid var(--ve-accent);
            background: rgba(2, 6, 23, 0.35);
            box-shadow: 0 0 12px rgba(2, 6, 23, 0.24);
          }
          .ve-rider {
            position: absolute;
            left: 40px;
            bottom: 13px;
            width: 120px;
            height: 80px;
            transform:
              translateY(var(--ve-rider-bob))
              rotate(var(--ve-rider-tilt))
              scale(var(--ve-rider-scale));
            transform-origin: 42% 72%;
            z-index: 5;
          }
          .ve-scene[data-intensity="high"] .ve-rider {
            filter: drop-shadow(0 0 12px rgba(255,255,255,0.08));
          }
          .ve-scene[data-theme="alpine"] .ve-rider {
            bottom: 14px;
            left: 48px;
          }
          .ve-scene[data-theme="neon"] .ve-rider {
            bottom: 11px;
            left: 44px;
          }
          .ve-rider-shadow {
            position: absolute;
            left: 10px;
            right: 20px;
            bottom: 1px;
            height: 14px;
            border-radius: 999px;
            background: radial-gradient(
              ellipse at center,
              rgba(2, 6, 23, var(--ve-rider-shadow-opacity)) 0%,
              rgba(2, 6, 23, 0.12) 42%,
              rgba(2, 6, 23, 0) 74%
            );
            filter: blur(var(--ve-rider-shadow-blur));
            transform: scaleX(0.86);
            z-index: 0;
          }
          .ve-sprite {
            position: absolute;
            inset: 0;
            image-rendering: pixelated;
            background-image: url('__SPRITE_URL__');
            background-repeat: no-repeat;
            background-size: 700% 100%;
            background-position: 0% 0;
            transform:
              translateX(var(--ve-sprite-shift-x))
              translateY(calc(var(--ve-rider-pulse) * -1px + var(--ve-sprite-lift)))
              rotate(var(--ve-sprite-rock));
            transform-origin: 42% 72%;
            filter: drop-shadow(0 2px 2px rgba(2, 6, 23, 0.45));
            z-index: 2;
          }
          .ve-rider-glow {
            position: absolute;
            inset: 12px 20px 10px 18px;
            background: radial-gradient(
              ellipse at 42% 46%,
              rgba(255,255,255,0.12) 0%,
              rgba(255,255,255,0.05) 26%,
              rgba(255,255,255,0) 62%
            );
            opacity: calc((var(--ve-scene-boost) - 0.9) * 0.55);
            mix-blend-mode: screen;
            pointer-events: none;
            z-index: 1;
          }
          .ve-rider-speedlines {
            position: absolute;
            left: -20px;
            top: 28px;
            width: 36px;
            height: 16px;
            opacity: var(--ve-rider-speed-opacity);
            background:
              repeating-linear-gradient(
                180deg,
                rgba(255,255,255,0) 0 4px,
                rgba(255,255,255,0.8) 4px 6px,
                rgba(255,255,255,0) 6px 12px
              );
            filter: blur(.8px);
            transform: skewX(-24deg);
            z-index: 1;
          }
          .ve-rider-dust {
            position: absolute;
            left: 22px;
            bottom: 6px;
            width: 72px;
            height: 16px;
            opacity: var(--ve-rider-dust-opacity);
            background:
              radial-gradient(circle at 18% 68%, rgba(226,232,240,0.85) 0 10%, rgba(226,232,240,0) 24%),
              radial-gradient(circle at 44% 54%, rgba(226,232,240,0.72) 0 8%, rgba(226,232,240,0) 22%),
              radial-gradient(circle at 72% 70%, rgba(226,232,240,0.55) 0 9%, rgba(226,232,240,0) 24%);
            filter: blur(1.5px);
            transform: translateX(calc(var(--ve-rider-pulse) * -0.5px));
            z-index: 1;
          }
          .ve-rider-occlusion {
            position: absolute;
            left: 48px;
            right: 12px;
            bottom: 10px;
            height: 12px;
            background: linear-gradient(
              180deg,
              rgba(0,0,0,0) 0%,
              rgba(15, 23, 42, 0.08) 42%,
              rgba(15, 23, 42, 0.24) 100%
            );
            border-radius: 999px 999px 4px 4px;
            z-index: 3;
            opacity: .58;
          }
          .ve-hud {
            position: absolute;
            right: 10px;
            top: 8px;
            display: flex;
            gap: 8px;
            align-items: center;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: .02em;
            background: rgba(2, 6, 23, 0.45);
            border: 1px solid rgba(56,189,248,.35);
            border-radius: 8px;
            padding: 3px 7px;
            color: #e2e8f0;
            z-index: 7;
          }
          .ve-hud-speed { color: #7dd3fc; }
          .ve-fx {
            position: absolute;
            left: 50%;
            top: 42%;
            transform: translate(-50%, -50%) scale(0.72);
            opacity: 0;
            pointer-events: none;
            z-index: 8;
            font-size: 1.35rem;
            font-weight: 900;
            letter-spacing: 0.05em;
            text-shadow: 0 0 12px rgba(2, 6, 23, 0.75);
            transition: transform .22s ease-out, opacity .22s ease-out;
            color: #f8fafc;
          }
          .ve-fx.show {
            opacity: 1;
            transform: translate(-50%, -50%) scale(1);
          }
          .ve-fx.bonus { color: #22d3ee; }
          .ve-fx.multi { color: #a78bfa; }
          .ve-fx.jackpot { color: #facc15; }
          .ve-fx.coach { color: #86efac; }
          .ve-fx.phase { color: #fcd34d; }
          .ve-fx.phase-effort { color: #fb7185; }
          .ve-fx.phase-recover { color: #22d3ee; }
          .ve-scene.fx-jackpot {
            box-shadow: inset 0 0 0 2px rgba(250,204,21,.45), 0 0 24px rgba(250,204,21,.38);
          }
          .ve-scene.fx-multi {
            box-shadow: inset 0 0 0 2px rgba(167,139,250,.42), 0 0 20px rgba(167,139,250,.3);
          }
          .ve-scene.fx-bonus {
            box-shadow: inset 0 0 0 2px rgba(34,211,238,.42), 0 0 20px rgba(34,211,238,.28);
          }
          .ve-dot {
            position: absolute;
            left: var(--x, 50%);
            top: var(--y, 50%);
            width: 6px;
            height: 6px;
            border-radius: 999px;
            pointer-events: none;
            z-index: 9;
            opacity: 0.95;
            transform: translate(-50%, -50%);
            box-shadow: 0 0 10px currentColor;
          }
          .ve-dot.bonus { color: #22d3ee; background: #22d3ee; }
          .ve-dot.multi { color: #a78bfa; background: #a78bfa; }
          .ve-dot.jackpot { color: #facc15; background: #facc15; }
        </style>
        <script>
          window.veloxEnsureThreeScene = function() {
            const mount = document.getElementById('ve-three-layer');
            const root = document.getElementById('ve-scene');
            const debugNode = document.getElementById('ve-three-debug');
            const setDebug = function(text, cls) {
              if (!debugNode) return;
              debugNode.textContent = text;
              debugNode.className = `ve-three-debug ${cls || ''}`.trim();
            };
            if (!mount || !root) return null;
            if (window.__velox_three_scene && window.__velox_three_scene.mount === mount) {
              root.dataset.threeReady = '1';
              setDebug(`Three ${window.__velox_three_scene.glMode}`, 'ok');
              return window.__velox_three_scene;
            }
            if (!window.THREE) {
              root.dataset.threeReady = '0';
              setDebug('Three script pending', 'warn');
              if (!window.__velox_three_retry) {
                window.__velox_three_retry = window.setTimeout(() => {
                  window.__velox_three_retry = 0;
                  window.veloxEnsureThreeScene();
                }, 180);
              }
              return null;
            }
            const T = window.THREE;
            mount.innerHTML = '<canvas class="ve-three-canvas"></canvas>';
            const canvas = mount.querySelector('canvas');
            let gl = null;
            let glMode = 'fallback';
            try {
              gl = canvas.getContext('webgl2', {
                alpha: true,
                antialias: true,
                powerPreference: 'high-performance',
                premultipliedAlpha: true,
              });
              if (gl) glMode = 'webgl2';
            } catch (err) {
              gl = null;
            }
            if (!gl) {
              try {
                gl = canvas.getContext('webgl', {
                  alpha: true,
                  antialias: true,
                  powerPreference: 'high-performance',
                  premultipliedAlpha: true,
                }) || canvas.getContext('experimental-webgl', {
                  alpha: true,
                  antialias: true,
                  powerPreference: 'high-performance',
                  premultipliedAlpha: true,
                });
                if (gl) glMode = 'webgl';
              } catch (err) {
                gl = null;
              }
            }
            if (!gl) {
              root.dataset.threeReady = '0';
              setDebug('WebGL context failed', 'err');
              return null;
            }
            let renderer = null;
            try {
              renderer = new T.WebGLRenderer({
                canvas,
                context: gl,
                antialias: true,
                alpha: true,
                powerPreference: 'high-performance',
              });
            } catch (err) {
              root.dataset.threeReady = '0';
              setDebug(`Renderer failed (${glMode})`, 'err');
              return null;
            }
            if ('outputColorSpace' in renderer && T.SRGBColorSpace) {
              renderer.outputColorSpace = T.SRGBColorSpace;
            }
            renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
            setDebug(`Three ${glMode}`, 'ok');

            const scene3d = new T.Scene();
            scene3d.fog = new T.FogExp2(0x060b16, 0.052);

            const camera = new T.OrthographicCamera(-9, 9, 3.8, -3.8, 0.1, 120);
            camera.position.set(0, 0.2, 12);
            camera.lookAt(0, 0.2, 0);

            const ambient = new T.HemisphereLight(0xa5d8ff, 0x07101c, 1.7);
            scene3d.add(ambient);
            const key = new T.PointLight(0x67e8f9, 16, 44, 2);
            key.position.set(-0.5, 1.8, 7);
            scene3d.add(key);
            const rim = new T.PointLight(0xf472b6, 12, 34, 2);
            rim.position.set(-5.5, 2.4, 6);
            scene3d.add(rim);
            const fill = new T.PointLight(0xfbbf24, 7, 28, 2);
            fill.position.set(5.5, -0.5, 6);
            scene3d.add(fill);

            const cityFar = new T.Group();
            const cityMid = new T.Group();
            const bridge = new T.Group();
            const poles = [];
            const railPosts = [];
            const laneMarks = [];
            const farBuildings = [];
            const midBuildings = [];
            scene3d.add(cityFar, cityMid, bridge);

            function makeMat(color, emissive, intensity, opacity) {
              return new T.MeshStandardMaterial({
                color,
                emissive,
                emissiveIntensity: intensity,
                transparent: opacity < 1,
                opacity,
                roughness: 0.45,
                metalness: 0.2,
              });
            }

            function makeCanvasTexture(width, height, painter, repeatX, repeatY) {
              const el = document.createElement('canvas');
              el.width = width;
              el.height = height;
              const ctx = el.getContext('2d');
              painter(ctx, width, height);
              const tex = new T.CanvasTexture(el);
              tex.wrapS = T.RepeatWrapping;
              tex.wrapT = T.RepeatWrapping;
              tex.repeat.set(repeatX || 1, repeatY || 1);
              if ('colorSpace' in tex && T.SRGBColorSpace) tex.colorSpace = T.SRGBColorSpace;
              return tex;
            }

            const roadTexture = makeCanvasTexture(512, 128, (ctx, w, h) => {
              const grad = ctx.createLinearGradient(0, 0, 0, h);
              grad.addColorStop(0, '#162133');
              grad.addColorStop(0.4, '#101827');
              grad.addColorStop(1, '#0b1220');
              ctx.fillStyle = grad;
              ctx.fillRect(0, 0, w, h);
              for (let i = 0; i < 1100; i += 1) {
                const a = 0.02 + Math.random() * 0.045;
                ctx.fillStyle = `rgba(${18 + (i % 30)},${26 + (i % 26)},${36 + (i % 18)},${a})`;
                ctx.fillRect(Math.random() * w, Math.random() * h, 2 + Math.random() * 3, 1 + Math.random() * 2);
              }
              ctx.strokeStyle = 'rgba(80,95,118,0.22)';
              ctx.lineWidth = 1;
              for (let i = 0; i < 18; i += 1) {
                ctx.beginPath();
                const x = (i * 37) % w;
                const y = 20 + (i * 11) % (h - 24);
                ctx.moveTo(x, y);
                ctx.lineTo(x + 12 + (i % 5) * 3, y + 2 + (i % 4));
                ctx.lineTo(x + 22 + (i % 6) * 2, y - 3 + (i % 3));
                ctx.stroke();
              }
              ctx.fillStyle = 'rgba(34,211,238,0.08)';
              for (let x = 0; x < w; x += 48) {
                ctx.fillRect(x, 12, 20, 3);
              }
              ctx.fillStyle = 'rgba(244,114,182,0.05)';
              ctx.fillRect(0, 0, w, 14);
            }, 6, 1);

            const cityTexture = makeCanvasTexture(320, 320, (ctx, w, h) => {
              const grad = ctx.createLinearGradient(0, 0, 0, h);
              grad.addColorStop(0, '#6f84a3');
              grad.addColorStop(0.55, '#455873');
              grad.addColorStop(1, '#2c394c');
              ctx.fillStyle = grad;
              ctx.fillRect(0, 0, w, h);
              for (let x = 0; x < w; x += 32) {
                ctx.fillStyle = x % 64 === 0 ? 'rgba(255,255,255,0.04)' : 'rgba(0,0,0,0.06)';
                ctx.fillRect(x, 0, 2, h);
              }
              for (let y = 14; y < h - 10; y += 15) {
                ctx.fillStyle = y % 30 === 0 ? 'rgba(255,255,255,0.04)' : 'rgba(10,18,30,0.05)';
                ctx.fillRect(0, y - 1, w, 1);
                for (let x = 10; x < w - 10; x += 18) {
                  const r = (x * 17 + y * 13) % 17;
                  if (r < 3) {
                    ctx.fillStyle = 'rgba(72,92,118,0.22)';
                    ctx.fillRect(x, y, 8, 6);
                  } else if (r < 11) {
                    ctx.fillStyle = r % 2 === 0 ? 'rgba(255,232,165,0.92)' : 'rgba(255,249,214,0.76)';
                    ctx.fillRect(x, y, 8, 6);
                    ctx.fillStyle = 'rgba(255,255,255,0.12)';
                    ctx.fillRect(x + 1, y + 1, 6, 1);
                    ctx.fillStyle = 'rgba(244,114,182,0.18)';
                    ctx.fillRect(x, y + 6, 8, 1);
                  } else {
                    ctx.fillStyle = r < 14 ? 'rgba(103,232,249,0.72)' : 'rgba(244,114,182,0.62)';
                    ctx.fillRect(x, y, 8, 6);
                    ctx.fillStyle = 'rgba(255,255,255,0.1)';
                    ctx.fillRect(x, y + 6, 8, 1);
                  }
                }
              }
              for (let i = 0; i < 9; i += 1) {
                ctx.fillStyle = i % 2 === 0 ? 'rgba(244,114,182,0.11)' : 'rgba(34,211,238,0.09)';
                ctx.fillRect(0, 24 + i * 34, w, 2);
              }
            }, 1, 1);

            const cityFarTexture = makeCanvasTexture(256, 256, (ctx, w, h) => {
              const grad = ctx.createLinearGradient(0, 0, 0, h);
              grad.addColorStop(0, '#3a4967');
              grad.addColorStop(0.55, '#293955');
              grad.addColorStop(1, '#1c273b');
              ctx.fillStyle = grad;
              ctx.fillRect(0, 0, w, h);
              for (let y = 14; y < h - 10; y += 14) {
                ctx.fillStyle = y % 28 === 0 ? 'rgba(255,255,255,0.03)' : 'rgba(0,0,0,0.04)';
                ctx.fillRect(0, y, w, 1);
                for (let x = 10; x < w - 10; x += 14) {
                  const seed = (x * 11 + y * 17) % 19;
                  if (seed < 4) {
                    ctx.fillStyle = 'rgba(63,80,110,0.22)';
                    ctx.fillRect(x, y, 4, 3);
                  } else if (seed < 15) {
                    ctx.fillStyle = seed % 3 === 0 ? 'rgba(255,236,182,0.44)' : 'rgba(103,232,249,0.3)';
                    ctx.fillRect(x, y, 4, 3);
                  }
                }
              }
              for (let i = 0; i < 6; i += 1) {
                ctx.fillStyle = i % 2 === 0 ? 'rgba(34,211,238,0.05)' : 'rgba(244,114,182,0.05)';
                ctx.fillRect(0, 18 + i * 34, w, 2);
              }
            }, 1, 1);

            function unitCylinder(radius, color, emissive, intensity) {
              return new T.Mesh(
                new T.CylinderGeometry(radius, radius, 1, 8),
                makeMat(color, emissive, intensity, 1),
              );
            }

            function placeSegment(mesh, ax, ay, bx, by, depth) {
              const dx = bx - ax;
              const dy = by - ay;
              const len = Math.max(0.001, Math.hypot(dx, dy));
              mesh.position.set((ax + bx) / 2, (ay + by) / 2, depth);
              mesh.scale.set(mesh.userData.radius || 0.08, len, mesh.userData.radius || 0.08);
              mesh.rotation.set(0, 0, Math.atan2(dy, dx) - Math.PI / 2);
            }

            function makeFacadeLights(width, height, isFar) {
              const canvas = document.createElement('canvas');
              canvas.width = isFar ? 96 : 128;
              canvas.height = 256;
              const ctx = canvas.getContext('2d');
              ctx.clearRect(0, 0, canvas.width, canvas.height);
              const cols = Math.max(2, Math.floor(width / (isFar ? 0.36 : 0.28)));
              const rows = Math.max(5, Math.floor(height / (isFar ? 0.3 : 0.22)));
              const padX = isFar ? 8 : 10;
              const padY = 8;
              const cellW = (canvas.width - padX * 2) / cols;
              const cellH = (canvas.height - padY * 2) / rows;
              for (let y = 0; y < rows; y += 1) {
                for (let x = 0; x < cols; x += 1) {
                  const seed = (x * 19 + y * 23 + cols * 7) % 15;
                  if (seed < 4) continue;
                  const warm = seed % 4 !== 0;
                  ctx.fillStyle = warm
                    ? (seed % 3 === 0 ? 'rgba(255,244,200,0.95)' : 'rgba(255,224,148,0.9)')
                    : (seed % 2 === 0 ? 'rgba(125,211,252,0.82)' : 'rgba(244,114,182,0.72)');
                  const px = padX + x * cellW + cellW * 0.18;
                  const py = padY + y * cellH + cellH * 0.2;
                  const ww = cellW * 0.54;
                  const hh = cellH * (isFar ? 0.32 : 0.42);
                  ctx.fillRect(px, py, ww, hh);
                  if (!isFar) {
                    ctx.fillStyle = 'rgba(255,255,255,0.14)';
                    ctx.fillRect(px + 1, py + 1, Math.max(1, ww - 2), 1);
                  }
                }
              }
              const tex = new T.CanvasTexture(canvas);
              tex.needsUpdate = true;
              return new T.Mesh(
                new T.PlaneGeometry(width * 0.82, height * 0.88),
                new T.MeshBasicMaterial({
                  map: tex,
                  transparent: true,
                  opacity: isFar ? 0.72 : 0.92,
                  depthWrite: false,
                }),
              );
            }

            function addBuilding(group, store, x, y, z, w, h, d, color, emissive, opacity, texture) {
              const mat = makeMat(color, emissive, 0.9, opacity);
              if (texture) mat.map = texture;
              const tower = new T.Group();
              const mesh = new T.Mesh(
                new T.BoxGeometry(w, h, d),
                mat,
              );
              mesh.position.set(x, y + h / 2, z);
              tower.add(mesh);
              if (h > 4.2 && Math.random() > 0.45) {
                const crown = new T.Mesh(
                  new T.BoxGeometry(w * (0.42 + Math.random() * 0.24), 0.12 + Math.random() * 0.08, d * 0.78),
                  makeMat(0xf8fafc, emissive, 1.3, 0.94),
                );
                crown.position.set(x, y + h + 0.12, z);
                tower.add(crown);
              }
              const facadeLights = makeFacadeLights(w, h, z < -10);
              facadeLights.position.set(x, y + h / 2, z + d / 2 + 0.03);
              tower.add(facadeLights);
              if (Math.random() > 0.42) {
                const lightBand = new T.Mesh(
                  new T.PlaneGeometry(w * (0.38 + Math.random() * 0.28), 0.08 + Math.random() * 0.06),
                  new T.MeshBasicMaterial({
                    color: Math.random() > 0.5 ? 0x67e8f9 : 0xf9a8d4,
                    transparent: true,
                    opacity: 0.4,
                  }),
                );
                lightBand.position.set(x, y + h * (0.55 + Math.random() * 0.28), z + d / 2 + 0.02);
                tower.add(lightBand);
              }
              if (Math.random() > 0.58) {
                const facadeGlow = new T.Mesh(
                  new T.PlaneGeometry(w * 0.72, h * (0.18 + Math.random() * 0.16)),
                  new T.MeshBasicMaterial({
                    color: Math.random() > 0.55 ? 0xfff1b5 : 0x7dd3fc,
                    transparent: true,
                    opacity: 0.08,
                  }),
                );
                facadeGlow.position.set(x, y + h * (0.42 + Math.random() * 0.22), z + d / 2 + 0.01);
                tower.add(facadeGlow);
              }
              if (Math.random() > 0.68) {
                const beacon = new T.Mesh(
                  new T.BoxGeometry(0.06, 0.26 + Math.random() * 0.24, 0.06),
                  makeMat(0xfef3c7, 0xf472b6, 1.6, 0.92),
                );
                beacon.position.set(x + (Math.random() - 0.5) * w * 0.22, y + h + 0.26, z);
                tower.add(beacon);
              }
              group.add(tower);
              store.push({ mesh: tower, baseX: x, baseY: y + h / 2, pulse: 0.6 + Math.random() * 0.8 });
            }

            for (let i = 0; i < 38; i += 1) {
              const x = -24 + i * 1.18;
              addBuilding(
                cityFar,
                farBuildings,
                x,
                -3.4,
                -12.6 + Math.random() * 1.8,
                0.7 + Math.random() * 0.7,
                1.6 + Math.random() * 4.1,
                0.45 + Math.random() * 0.8,
                0x31415f,
                i % 3 === 0 ? 0xa855f7 : 0x38bdf8,
                0.34,
                cityFarTexture,
              );
            }
            for (let i = 0; i < 28; i += 1) {
              const x = -23 + i * 1.62;
              addBuilding(
                cityMid,
                midBuildings,
                x,
                -3.45,
                -8.8 + Math.random() * 1.6,
                0.9 + Math.random() * 1.6,
                2.6 + Math.random() * 5.4,
                0.8 + Math.random() * 1.4,
                0x556885,
                i % 2 === 0 ? 0xf472b6 : 0x2dd4bf,
                0.82,
                cityTexture,
              );
            }

            const roadMat = makeMat(0x101828, 0x164e63, 0.16, 1);
            roadMat.map = roadTexture;
            const road = new T.Mesh(
              new T.BoxGeometry(96, 1.9, 4.8),
              roadMat,
            );
            road.position.set(0, -4.05, 0.2);
            bridge.add(road);

            const curb = new T.Mesh(
              new T.BoxGeometry(96, 0.18, 5.0),
              makeMat(0x67e8f9, 0x22d3ee, 1.1, 0.96),
            );
            curb.position.set(0, -3.08, 0.5);
            bridge.add(curb);

            const roadGlow = new T.Mesh(
              new T.PlaneGeometry(88, 1.7),
              makeMat(0x0f172a, 0xf472b6, 0.14, 0.16),
            );
            roadGlow.position.set(0, -3.12, 0.78);
            roadGlow.rotation.x = -Math.PI / 2;
            bridge.add(roadGlow);

            for (let i = 0; i < 46; i += 1) {
              const post = new T.Mesh(
                new T.BoxGeometry(0.2, 2.4 + (i % 3) * 0.12, 0.28),
                makeMat(0x155e75, 0x2dd4bf, 0.34, 0.9),
              );
              const cap = new T.Mesh(
                new T.BoxGeometry(0.28, 0.08, 0.38),
                makeMat(0x67e8f9, 0x67e8f9, 0.52, 0.92),
              );
              const group = new T.Group();
              group.add(post, cap);
              post.position.y = -2.18;
              cap.position.y = -0.92;
              group.position.set(-40 + i * 1.78, 0, 1.6);
              bridge.add(group);
              railPosts.push({ group, baseX: group.position.x });
            }

            for (let i = 0; i < 6; i += 1) {
              const pole = new T.Group();
              const mast = new T.Mesh(
                new T.CylinderGeometry(0.05, 0.05, 5.8, 8),
                makeMat(0x94a3b8, 0x1e293b, 0.22, 0.92),
              );
              mast.position.y = -0.1;
              const arm = new T.Mesh(
                new T.BoxGeometry(1.2, 0.06, 0.06),
                makeMat(0x94a3b8, 0x1e293b, 0.2, 0.95),
              );
              arm.position.set(0.5, 2.45, 0);
              const lamp = new T.Mesh(
                new T.SphereGeometry(0.17, 12, 12),
                makeMat(0xfffbeb, 0xfff3b0, 4.2, 0.94),
              );
              lamp.position.set(0.98, 2.3, 0);
              const halo = new T.Mesh(
                new T.SphereGeometry(0.7, 16, 16),
                makeMat(0xfff7cc, 0xfff7cc, 0.7, 0.14),
              );
              halo.position.copy(lamp.position);
              pole.add(mast, arm, lamp, halo);
              pole.position.set(-24 + i * 9.8, 0, 1.05);
              bridge.add(pole);
              poles.push({ pole, lamp, halo, baseX: pole.position.x });
            }

            for (let i = 0; i < 34; i += 1) {
              const mark = new T.Mesh(
                new T.BoxGeometry(1.1, 0.03, 0.2),
                makeMat(0x93c5fd, 0x67e8f9, 0.92, 0.95),
              );
              mark.position.set(-35 + i * 2.15, -3.02, -0.05);
              bridge.add(mark);
              laneMarks.push({ mark, baseX: mark.position.x });
            }

            const starsGeo = new T.BufferGeometry();
            const starPositions = [];
            for (let i = 0; i < 180; i += 1) {
              starPositions.push((Math.random() - 0.5) * 34, Math.random() * 12 + 1.2, -18 - Math.random() * 10);
            }
            starsGeo.setAttribute('position', new T.Float32BufferAttribute(starPositions, 3));
            const stars = new T.Points(
              starsGeo,
              new T.PointsMaterial({ color: 0xe0f2fe, size: 0.09, transparent: true, opacity: 0.85 }),
            );
            scene3d.add(stars);

            const haze = new T.Mesh(
              new T.PlaneGeometry(40, 12),
              makeMat(0x5b9fff, 0x22d3ee, 0.08, 0.16),
            );
            haze.position.set(0, 0.5, -11.8);
            scene3d.add(haze);

            const rider = new T.Group();
            rider.position.set(-6.6, -2.18, 1.34);
            rider.scale.setScalar(0.98);
            scene3d.add(rider);

            const riderShadow = new T.Mesh(
              new T.CircleGeometry(1.9, 24),
              new T.MeshBasicMaterial({ color: 0x020617, transparent: true, opacity: 0.34 }),
            );
            riderShadow.rotation.x = -Math.PI / 2;
            riderShadow.position.set(0, -0.98, 0.45);
            rider.add(riderShadow);
            const riderAura = new T.Mesh(
              new T.CircleGeometry(2.2, 28),
              new T.MeshBasicMaterial({ color: 0x22d3ee, transparent: true, opacity: 0.07 }),
            );
            riderAura.position.set(0.1, 0.7, -0.18);
            rider.add(riderAura);

            const tireMat = makeMat(0x020617, 0x0f172a, 0.16, 1);
            const rimMat = makeMat(0x93c5fd, 0x67e8f9, 0.68, 1);
            const spokeMat = makeMat(0xe0f2fe, 0xbae6fd, 0.26, 0.95);
            const frameMat = makeMat(0x1d4ed8, 0x38bdf8, 0.42, 1);
            const frameHotMat = makeMat(0x2563eb, 0x67e8f9, 0.52, 1);
            const skinFrontMat = makeMat(0xfdba74, 0xffedd5, 0.08, 1);
            const skinRearMat = makeMat(0xf59e0b, 0xffedd5, 0.04, 0.94);
            const shortMat = makeMat(0x020617, 0x1e293b, 0.2, 1);
            const jerseyMat = makeMat(0xef4444, 0xfb7185, 0.28, 1);
            const gloveMat = makeMat(0x111827, 0x1e293b, 0.08, 1);
            const shoeMat = makeMat(0x111827, 0x1e293b, 0.08, 1);
            const helmetMat = makeMat(0xef4444, 0xffffff, 0.3, 1);
            const visorMat = makeMat(0x0f172a, 0x38bdf8, 0.14, 0.96);

            function makeFlatBar(thickness, depth, mat) {
              const mesh = new T.Mesh(new T.BoxGeometry(thickness, 1, depth), mat);
              mesh.userData.radius = thickness / 2;
              return mesh;
            }

            function placeFlatBar(mesh, ax, ay, bx, by, depth) {
              const dx = bx - ax;
              const dy = by - ay;
              const len = Math.max(0.001, Math.hypot(dx, dy));
              mesh.position.set((ax + bx) / 2, (ay + by) / 2, depth);
              mesh.scale.set(1, len, 1);
              mesh.rotation.set(0, 0, Math.atan2(dy, dx) - Math.PI / 2);
            }

            function joint(radius, mat, z) {
              const mesh = new T.Mesh(new T.CircleGeometry(radius, 18), mat);
              mesh.position.z = z;
              return mesh;
            }

            function makeWheel(x, z) {
              const g = new T.Group();
              const tire = new T.Mesh(new T.TorusGeometry(0.98, 0.07, 10, 34), tireMat);
              const rim = new T.Mesh(new T.TorusGeometry(0.83, 0.028, 8, 30), rimMat);
              const hub = new T.Mesh(new T.CircleGeometry(0.11, 18), rimMat);
              const spokeA = new T.Mesh(new T.BoxGeometry(0.034, 1.5, 0.03), spokeMat);
              const spokeB = new T.Mesh(new T.BoxGeometry(1.5, 0.034, 0.03), spokeMat);
              const spokeC = spokeA.clone();
              spokeC.rotation.z = Math.PI / 4;
              const spokeD = spokeA.clone();
              spokeD.rotation.z = -Math.PI / 4;
              const wheelGlow = new T.Mesh(
                new T.RingGeometry(0.9, 1.02, 32),
                new T.MeshBasicMaterial({ color: 0x67e8f9, transparent: true, opacity: 0.08 }),
              );
              g.add(wheelGlow, tire, rim, spokeA, spokeB, spokeC, spokeD, hub);
              g.position.set(x, 0, z);
              return g;
            }

            const rearWheel = makeWheel(-1.72, -0.12);
            const frontWheel = makeWheel(1.7, 0.12);
            rider.add(rearWheel, frontWheel);

            const bikeGeo = (() => {
              const unit = 3.42 / 1000.0; // 1000 mm wheelbase mapped to the current side-view span.
              const rearAxle = { x: -1.71, y: 0.0 };
              const frontAxle = { x: rearAxle.x + (1000 * unit), y: 0.0 };
              const bb = { x: rearAxle.x + (415 * unit), y: -(75 * unit) };
              const seatTop = {
                x: bb.x - Math.cos(73.5 * Math.PI / 180.0) * (520 * unit),
                y: bb.y + Math.sin(73.5 * Math.PI / 180.0) * (520 * unit),
              };
              const headTop = { x: bb.x + (378 * unit), y: bb.y + (575 * unit) };
              const headDx = Math.cos(73.0 * Math.PI / 180.0) * (168 * unit);
              const headDy = Math.sin(73.0 * Math.PI / 180.0) * (168 * unit);
              const headBottom = { x: headTop.x + headDx, y: headTop.y - headDy };
              const saddle = { x: seatTop.x - 0.02, y: seatTop.y + 0.18 };
              const handlebar = { x: headTop.x + 0.3, y: headTop.y + 0.02 };
              const stemTop = { x: headTop.x + 0.11, y: headTop.y + 0.06 };
              return { rearAxle, frontAxle, bb, seatTop, headTop, headBottom, saddle, handlebar, stemTop };
            })();

            const frameBars = [
              makeFlatBar(0.14, 0.08, frameMat),
              makeFlatBar(0.14, 0.08, frameMat),
              makeFlatBar(0.14, 0.08, frameHotMat),
              makeFlatBar(0.14, 0.08, frameMat),
              makeFlatBar(0.12, 0.08, frameHotMat),
              makeFlatBar(0.1, 0.08, frameHotMat),
              makeFlatBar(0.08, 0.08, frameHotMat),
              makeFlatBar(0.1, 0.08, frameHotMat),
              makeFlatBar(0.08, 0.08, gloveMat),
            ];
            frameBars.forEach((bar) => rider.add(bar));
            const saddle = new T.Mesh(new T.BoxGeometry(0.5, 0.08, 0.18), shortMat);
            saddle.position.set(bikeGeo.saddle.x, bikeGeo.saddle.y, -0.02);
            saddle.rotation.z = -0.02;
            const seatpost = new T.Mesh(new T.BoxGeometry(0.05, 0.38, 0.06), frameHotMat);
            seatpost.position.set((bikeGeo.seatTop.x + bikeGeo.saddle.x) / 2, (bikeGeo.seatTop.y + bikeGeo.saddle.y - 0.02) / 2, -0.03);
            seatpost.rotation.z = -0.08;
            const stem = new T.Mesh(new T.BoxGeometry(0.07, 0.28, 0.06), frameHotMat);
            stem.position.set(bikeGeo.stemTop.x, bikeGeo.stemTop.y, 0.08);
            stem.rotation.z = -0.94;
            const handlebar = new T.Mesh(new T.BoxGeometry(0.46, 0.06, 0.06), gloveMat);
            handlebar.position.set(bikeGeo.handlebar.x, bikeGeo.handlebar.y, 0.1);
            handlebar.rotation.z = -0.03;
            const hoodRear = new T.Mesh(new T.BoxGeometry(0.1, 0.18, 0.06), gloveMat);
            hoodRear.position.set(bikeGeo.handlebar.x - 0.1, bikeGeo.handlebar.y + 0.08, 0.1);
            hoodRear.rotation.z = 0.44;
            const hoodFront = hoodRear.clone();
            hoodFront.position.set(bikeGeo.handlebar.x + 0.1, bikeGeo.handlebar.y + 0.08, 0.1);
            hoodFront.rotation.z = 0.28;
            const dropRear = new T.Mesh(new T.BoxGeometry(0.07, 0.24, 0.06), gloveMat);
            dropRear.position.set(bikeGeo.handlebar.x - 0.14, bikeGeo.handlebar.y + 0.25, 0.1);
            dropRear.rotation.z = 0.7;
            const dropFront = dropRear.clone();
            dropFront.position.set(bikeGeo.handlebar.x + 0.14, bikeGeo.handlebar.y + 0.25, 0.1);
            dropFront.rotation.z = 0.54;
            rider.add(saddle, seatpost, stem, handlebar, hoodRear, hoodFront, dropRear, dropFront);

            const crank = new T.Group();
            crank.position.set(bikeGeo.bb.x, bikeGeo.bb.y, 0.08);
            const crankArmA = new T.Mesh(new T.BoxGeometry(0.09, 0.84, 0.04), makeMat(0xe2e8f0, 0xffffff, 0.06, 1));
            const crankArmB = crankArmA.clone();
            crankArmB.rotation.z = Math.PI;
            const pedalA = new T.Mesh(new T.BoxGeometry(0.26, 0.08, 0.06), shoeMat);
            const pedalB = pedalA.clone();
            pedalA.position.y = 0.42;
            pedalB.position.y = -0.42;
            const chainring = new T.Mesh(new T.CircleGeometry(0.16, 18), makeMat(0xe2e8f0, 0x93c5fd, 0.08, 1));
            crank.add(crankArmA, crankArmB, pedalA, pedalB, chainring);
            rider.add(crank);

            const torso = new T.Group();
            const torsoUpper = new T.Mesh(new T.BoxGeometry(0.86, 0.5, 0.22), jerseyMat);
            const torsoLower = new T.Mesh(new T.BoxGeometry(0.68, 0.38, 0.2), jerseyMat);
            const jerseyStripe = new T.Mesh(new T.BoxGeometry(0.16, 0.56, 0.23), makeMat(0xf8fafc, 0xffffff, 0.12, 0.96));
            const shorts = new T.Mesh(new T.BoxGeometry(0.88, 0.56, 0.2), shortMat);
            const head = new T.Mesh(new T.CircleGeometry(0.31, 18), skinFrontMat);
            const helmet = new T.Mesh(new T.CircleGeometry(0.39, 20, Math.PI * 0.1, Math.PI * 1.25), helmetMat);
            const visor = new T.Mesh(new T.BoxGeometry(0.28, 0.12, 0.03), visorMat);
            torsoUpper.position.set(-0.04, 0.1, 0.02);
            torsoLower.position.set(0.18, -0.2, 0);
            jerseyStripe.position.set(-0.12, 0.06, 0.03);
            torso.add(torsoUpper, torsoLower, jerseyStripe);
            torso.position.z = 0.08;
            visor.position.set(0.04, -0.06, 0.04);
            head.add(helmet, visor);
            rider.add(torso, shorts, head);

            const rearThigh = makeFlatBar(0.22, 0.12, skinRearMat);
            const rearCalf = makeFlatBar(0.18, 0.1, skinRearMat);
            const frontThigh = makeFlatBar(0.24, 0.14, skinFrontMat);
            const frontCalf = makeFlatBar(0.18, 0.12, skinFrontMat);
            const rearUpperArm = makeFlatBar(0.14, 0.08, skinRearMat);
            const rearForearm = makeFlatBar(0.12, 0.08, skinRearMat);
            const frontUpperArm = makeFlatBar(0.14, 0.1, skinFrontMat);
            const frontForearm = makeFlatBar(0.12, 0.1, skinFrontMat);
            const rearFoot = new T.Mesh(new T.BoxGeometry(0.34, 0.12, 0.08), shoeMat);
            const frontFoot = rearFoot.clone();
            const rearHand = new T.Mesh(new T.CircleGeometry(0.08, 12), gloveMat);
            const frontHand = rearHand.clone();
            rider.add(
              rearThigh, rearCalf, frontThigh, frontCalf,
              rearUpperArm, rearForearm, frontUpperArm, frontForearm,
              rearFoot, frontFoot, rearHand, frontHand,
            );

            const joints = {
              rearHip: joint(0.11, shortMat, -0.06),
              frontHip: joint(0.12, shortMat, 0.1),
              rearKnee: joint(0.09, skinRearMat, -0.04),
              frontKnee: joint(0.09, skinFrontMat, 0.12),
              rearAnkle: joint(0.07, shoeMat, -0.02),
              frontAnkle: joint(0.07, shoeMat, 0.1),
              rearShoulder: joint(0.09, jerseyMat, -0.04),
              frontShoulder: joint(0.09, jerseyMat, 0.1),
              rearElbow: joint(0.07, skinRearMat, -0.02),
              frontElbow: joint(0.07, skinFrontMat, 0.1),
            };
            Object.values(joints).forEach((mesh) => rider.add(mesh));

            function bendJoint(ax, ay, bx, by, lenA, lenB, bendSign) {
              const dx = bx - ax;
              const dy = by - ay;
              const dist = Math.max(0.001, Math.min(lenA + lenB - 0.02, Math.hypot(dx, dy)));
              const reach = (lenA * lenA - lenB * lenB + dist * dist) / (2 * dist);
              const h = Math.sqrt(Math.max(0.001, lenA * lenA - reach ** 2));
              const nx = -dy / dist;
              const ny = dx / dist;
              return {
                x: ax + (dx * reach) / dist + nx * h * bendSign,
                y: ay + (dy * reach) / dist + ny * h * bendSign,
              };
            }

            function bendJointPreferred(ax, ay, bx, by, lenA, lenB, preferredX, preferredY, preferredSign) {
              const c1 = bendJoint(ax, ay, bx, by, lenA, lenB, preferredSign || 1);
              const c2 = bendJoint(ax, ay, bx, by, lenA, lenB, -(preferredSign || 1));
              const d1 = ((c1.x - preferredX) ** 2) + ((c1.y - preferredY) ** 2);
              const d2 = ((c2.x - preferredX) ** 2) + ((c2.y - preferredY) ** 2);
              const best = d1 <= d2 ? c1 : c2;
              return {
                x: best.x < ax - 0.02 ? (ax - 0.02 + best.x) * 0.5 : best.x,
                y: best.y,
              };
            }

            const state = {
              speed: 0,
              cadence: 0,
              power: 0,
              intensity: 'mid',
              action: 'steady',
              theme: 'neon',
              tick: 0,
              lastFrameMs: 0,
              pedal: 0,
              wheel: 0,
              farOffset: 0,
              midOffset: 0,
              nearOffset: 0,
            };

            function resize() {
              const w = Math.max(1, mount.clientWidth);
              const h = Math.max(1, mount.clientHeight);
              renderer.setSize(w, h, false);
              const aspect = w / h;
              const orthoHeight = 3.8;
              camera.left = -orthoHeight * aspect;
              camera.right = orthoHeight * aspect;
              camera.top = orthoHeight;
              camera.bottom = -orthoHeight;
              camera.updateProjectionMatrix();
            }
            const ro = new ResizeObserver(resize);
            ro.observe(mount);
            resize();
            root.dataset.threeReady = '1';

            function wrapX(base, offset, span) {
              let x = base + offset;
              while (x < -span) x += span * 2;
              while (x > span) x -= span * 2;
              return x;
            }

            function render(nowMs) {
              const sceneState = window.__velox_three_state || state;
              const frameNow = Number(nowMs || performance.now());
              const dt = state.lastFrameMs ? Math.min(0.05, Math.max(1 / 240, (frameNow - state.lastFrameMs) / 1000)) : 1 / 60;
              state.lastFrameMs = frameNow;
              state.speed = sceneState.speed || 0;
              state.cadence = sceneState.cadence || 0;
              state.power = sceneState.power || 0;
              state.intensity = sceneState.intensity || 'mid';
              state.action = sceneState.action || 'steady';
              state.theme = sceneState.theme || 'neon';
              state.tick += dt;
              const themeNeon = state.theme === 'neon';
              mount.style.display = themeNeon ? 'block' : 'none';
              if (themeNeon) {
                const nearVelocity = Math.max(0.45, state.speed * 0.09);
                const midVelocity = nearVelocity * 0.26;
                const farVelocity = nearVelocity * 0.1;
                state.farOffset -= farVelocity * dt;
                state.midOffset -= midVelocity * dt;
                state.nearOffset -= nearVelocity * dt;
                state.pedal -= Math.max(0.25, state.cadence * (Math.PI * 2 / 60)) * dt;
                state.wheel -= nearVelocity * 4.8 * dt;
                const farOffset = state.farOffset;
                const midOffset = state.midOffset;
                const nearOffset = state.nearOffset;
                farBuildings.forEach((item, idx) => {
                  item.mesh.position.x = wrapX(item.baseX, farOffset + idx * 0.02, 28);
                  item.mesh.children.forEach((child, childIdx) => {
                    if (child.material) child.material.emissiveIntensity = 0.5 + Math.sin(state.tick * item.pulse + idx + childIdx * 0.4) * 0.1;
                  });
                });
                midBuildings.forEach((item, idx) => {
                  item.mesh.position.x = wrapX(item.baseX, midOffset + idx * 0.04, 28);
                  item.mesh.children.forEach((child, childIdx) => {
                    if (child.material) child.material.emissiveIntensity = 0.68 + Math.sin(state.tick * (item.pulse + 0.2) + idx + childIdx * 0.5) * 0.14;
                  });
                });
                railPosts.forEach((item) => {
                  item.group.position.x = wrapX(item.baseX, nearOffset, 44);
                });
                laneMarks.forEach((item) => {
                  item.mark.position.x = wrapX(item.baseX, nearOffset * 1.15, 38);
                });
                poles.forEach((item, idx) => {
                  item.pole.position.x = wrapX(item.baseX, nearOffset * 0.92, 32);
                  const boost = state.intensity === 'high' ? 1.45 : state.intensity === 'low' ? 0.88 : 1.12;
                  item.lamp.material.emissiveIntensity = 2.4 * boost;
                  item.halo.material.opacity = 0.15 * boost;
                });
                const boost = state.intensity === 'high' ? 1.2 : state.intensity === 'low' ? 0.88 : 1;
                haze.material.opacity = 0.1 * boost;
                stars.rotation.z = Math.sin(state.tick * 0.03) * 0.04;
                cityFar.position.y = Math.sin(state.tick * 0.2) * 0.03;
                cityMid.position.y = Math.sin(state.tick * 0.26) * 0.04;
                road.material.emissiveIntensity = 0.12 + (boost - 0.85) * 0.18;
                curb.material.emissiveIntensity = 0.72 + (boost - 0.9) * 0.9;

                const pedalAngle = state.pedal;
                const bob = Math.sin(state.tick * 5.6) * Math.min(0.024, state.cadence / 4200);
                const bikeLean = -0.015;
                const bodyLean = -0.34 - (boost - 0.9) * 0.08;
                const hipRear = { x: bikeGeo.saddle.x + 0.02, y: bikeGeo.saddle.y - 0.28 };
                const hipFront = { x: bikeGeo.saddle.x + 0.24, y: bikeGeo.saddle.y - 0.32 };
                const shoulderRear = { x: bikeGeo.headTop.x - 0.7, y: bikeGeo.headTop.y + 0.1 };
                const shoulderFront = { x: bikeGeo.headTop.x - 0.42, y: bikeGeo.headTop.y + 0.06 + (boost - 1) * 0.05 };
                const handRear = { x: bikeGeo.handlebar.x - 0.1, y: bikeGeo.handlebar.y + 0.09 };
                const handFront = { x: bikeGeo.handlebar.x + 0.1, y: bikeGeo.handlebar.y + 0.08 };
                const pedalFront = { x: bikeGeo.bb.x + Math.cos(pedalAngle) * 0.53, y: bikeGeo.bb.y + Math.sin(pedalAngle) * 0.53 };
                const pedalRear = { x: bikeGeo.bb.x + Math.cos(pedalAngle + Math.PI) * 0.53, y: bikeGeo.bb.y + Math.sin(pedalAngle + Math.PI) * 0.53 };
                const kneeFront = bendJointPreferred(
                  hipFront.x,
                  hipFront.y,
                  pedalFront.x,
                  pedalFront.y,
                  0.84,
                  0.86,
                  hipFront.x + 0.42,
                  hipFront.y + 0.16,
                  -1,
                );
                const kneeRear = bendJointPreferred(
                  hipRear.x,
                  hipRear.y,
                  pedalRear.x,
                  pedalRear.y,
                  0.86,
                  0.84,
                  hipRear.x + 0.36,
                  hipRear.y + 0.08,
                  1,
                );
                const elbowFront = bendJoint(shoulderFront.x, shoulderFront.y, handFront.x, handFront.y, 0.58, 0.52, -1);
                const elbowRear = bendJoint(shoulderRear.x, shoulderRear.y, handRear.x, handRear.y, 0.48, 0.5, 1);

                rider.position.y = -2.38 + bob;
                rider.rotation.z = bikeLean;
                riderShadow.scale.set(1 + boost * 0.05, 0.52 + boost * 0.02, 1);
                riderShadow.material.opacity = 0.2 + boost * 0.08;
                rearWheel.rotation.z = state.wheel;
                frontWheel.rotation.z = state.wheel;
                crank.rotation.z = pedalAngle;

                placeFlatBar(frameBars[0], bikeGeo.seatTop.x, bikeGeo.seatTop.y, bikeGeo.headTop.x, bikeGeo.headTop.y, 0.04);
                placeFlatBar(frameBars[1], bikeGeo.rearAxle.x, bikeGeo.rearAxle.y, bikeGeo.seatTop.x, bikeGeo.seatTop.y, -0.03);
                placeFlatBar(frameBars[2], bikeGeo.rearAxle.x, bikeGeo.rearAxle.y, bikeGeo.bb.x, bikeGeo.bb.y, 0.02);
                placeFlatBar(frameBars[3], bikeGeo.bb.x, bikeGeo.bb.y, bikeGeo.headBottom.x, bikeGeo.headBottom.y, 0.03);
                placeFlatBar(frameBars[4], bikeGeo.bb.x, bikeGeo.bb.y, bikeGeo.seatTop.x, bikeGeo.seatTop.y, -0.01);
                placeFlatBar(frameBars[5], bikeGeo.headTop.x, bikeGeo.headTop.y, bikeGeo.headBottom.x, bikeGeo.headBottom.y, 0.05);
                placeFlatBar(frameBars[6], bikeGeo.headBottom.x, bikeGeo.headBottom.y, bikeGeo.frontAxle.x, bikeGeo.frontAxle.y + 0.02, 0.07);
                placeFlatBar(frameBars[7], bikeGeo.seatTop.x, bikeGeo.seatTop.y, bikeGeo.saddle.x - 0.02, bikeGeo.saddle.y - 0.02, -0.04);
                placeFlatBar(frameBars[8], bikeGeo.headTop.x, bikeGeo.headTop.y, bikeGeo.handlebar.x, bikeGeo.handlebar.y, 0.07);

                torso.position.set(bikeGeo.headTop.x - 0.8, bikeGeo.headTop.y - 0.04, 0.06);
                torso.rotation.z = bodyLean;
                torso.rotation.y = 0.06;
                shorts.position.set(bikeGeo.saddle.x + 0.18, bikeGeo.saddle.y - 0.18, 0.05);
                shorts.rotation.z = bodyLean * 0.55;
                head.position.set(bikeGeo.headTop.x - 0.18, bikeGeo.headTop.y + 0.34 + Math.sin(state.tick * 2.2) * 0.025, 0.12);
                head.scale.set(1, 1, 1);
                head.rotation.z = bodyLean * 0.14;

                placeFlatBar(rearUpperArm, shoulderRear.x, shoulderRear.y, elbowRear.x, elbowRear.y, -0.03);
                placeFlatBar(rearForearm, elbowRear.x, elbowRear.y, handRear.x, handRear.y, -0.01);
                placeFlatBar(frontUpperArm, shoulderFront.x, shoulderFront.y, elbowFront.x, elbowFront.y, 0.1);
                placeFlatBar(frontForearm, elbowFront.x, elbowFront.y, handFront.x, handFront.y, 0.12);
                placeFlatBar(rearThigh, hipRear.x, hipRear.y, kneeRear.x, kneeRear.y, -0.05);
                placeFlatBar(rearCalf, kneeRear.x, kneeRear.y, pedalRear.x, pedalRear.y, -0.03);
                placeFlatBar(frontThigh, hipFront.x, hipFront.y, kneeFront.x, kneeFront.y, 0.12);
                placeFlatBar(frontCalf, kneeFront.x, kneeFront.y, pedalFront.x, pedalFront.y, 0.14);

                rearFoot.position.set(pedalRear.x, pedalRear.y - 0.02, 0);
                rearFoot.rotation.z = pedalAngle + Math.PI * 0.5;
                frontFoot.position.set(pedalFront.x, pedalFront.y - 0.02, 0.12);
                frontFoot.rotation.z = pedalAngle - Math.PI * 0.5;
                rearHand.position.set(handRear.x, handRear.y, 0.02);
                frontHand.position.set(handFront.x, handFront.y, 0.14);

                joints.rearHip.position.set(hipRear.x, hipRear.y, -0.04);
                joints.frontHip.position.set(hipFront.x, hipFront.y, 0.1);
                joints.rearKnee.position.set(kneeRear.x, kneeRear.y, -0.03);
                joints.frontKnee.position.set(kneeFront.x, kneeFront.y, 0.12);
                joints.rearAnkle.position.set(pedalRear.x, pedalRear.y, -0.01);
                joints.frontAnkle.position.set(pedalFront.x, pedalFront.y, 0.11);
                joints.rearShoulder.position.set(shoulderRear.x, shoulderRear.y, -0.03);
                joints.frontShoulder.position.set(shoulderFront.x, shoulderFront.y, 0.1);
                joints.rearElbow.position.set(elbowRear.x, elbowRear.y, -0.01);
                joints.frontElbow.position.set(elbowFront.x, elbowFront.y, 0.11);
              }
              renderer.render(scene3d, camera);
              window.requestAnimationFrame(render);
            }

            render(performance.now());
            window.__velox_three_scene = { mount, renderer, scene3d, camera, rider, resize, glMode };
            return window.__velox_three_scene;
          };
          window.veloxUpdateScene = function(speed, cadence, power, inZone, action) {
            const scene = document.getElementById('ve-scene');
            if (!scene) return;
            const speedNode = document.getElementById('ve-scene-speed');
            const actionNode = document.getElementById('ve-scene-action');
            const spriteNode = document.getElementById('ve-sprite');
            const state = window.__velox_scene_state || {
              far: 0,
              mid: 0,
              front: 0,
              road: 0,
              pedal: 0,
              bobTick: 0,
              frameTick: 0,
              pulseTick: 0,
            };
            const s = Math.max(0, Number(speed || 0));
            const c = Math.max(0, Number(cadence || 0));
            const p = Math.max(0, Number(power || 0));
            state.far = (state.far - Math.max(0.08, s * 0.06)) % 1400;
            state.mid = (state.mid - Math.max(0.15, s * 0.16)) % 1400;
            state.front = (state.front - Math.max(0.25, s * 0.34)) % 1400;
            state.road = (state.road - Math.max(1.2, s * 0.95)) % 240;
            state.pedal = (state.pedal + (c * 0.92)) % 360;
            state.bobTick += 0.35;
            state.frameTick += Math.max(0.25, c / 65);
            state.pulseTick += Math.max(0.18, c / 95);
            const bob = Math.sin(state.bobTick + c / 20) * Math.min(2.5, 0.6 + c / 65);
            const pulse = Math.sin(state.pulseTick) * Math.min(1.6, 0.3 + c / 120);
            scene.style.setProperty('--ve-bg-far-offset', `${state.far}px`);
            scene.style.setProperty('--ve-bg-mid-offset', `${state.mid}px`);
            scene.style.setProperty('--ve-bg-front-offset', `${state.front}px`);
            scene.style.setProperty('--ve-road-offset', `${state.road}px`);
            scene.style.setProperty('--ve-pedal-rot', `${state.pedal}deg`);
            scene.style.setProperty('--ve-rider-bob', `${bob}px`);
            scene.style.setProperty('--ve-rider-pulse', `${pulse.toFixed(2)}`);
            scene.dataset.zone = inZone ? 'ok' : 'bad';
            scene.dataset.action = action || 'steady';
            const intensityScore = Math.max(s / 28, c / 95, p / 240) +
              (action === 'up' ? 0.32 : action === 'down' ? -0.18 : 0);
            let intensity = 'mid';
            if (intensityScore >= 1.0) intensity = 'high';
            else if (intensityScore < 0.5) intensity = 'low';
            scene.dataset.intensity = intensity;
            window.__velox_three_state = {
              speed: s,
              cadence: c,
              power: p,
              intensity,
              action: action || 'steady',
              theme: scene.dataset.theme || 'forest',
            };
            window.veloxEnsureThreeScene();
            if (spriteNode) {
              const framePhase = Math.floor(state.frameTick) % 12;
              const frameMap = [0, 1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1];
              const frame = frameMap[framePhase];
              const x = frame * (100 / 6);
              const shiftByFrame = [0, 0, -1, -1, 0, 1, 1, 1, 0, -1, -1, 0];
              const rockByFrame = [-0.45, -0.25, -0.1, 0.15, 0.35, 0.55, 0.35, 0.55, 0.35, 0.15, -0.1, -0.25];
              const liftByFrame = [0, 0, -1, -1, 0, 0, 1, 0, 0, -1, -1, 0];
              spriteNode.style.backgroundPosition = `${x}% 0%`;
              scene.style.setProperty('--ve-sprite-shift-x', `${shiftByFrame[framePhase]}px`);
              scene.style.setProperty('--ve-sprite-rock', `${rockByFrame[framePhase]}deg`);
              scene.style.setProperty('--ve-sprite-lift', `${liftByFrame[framePhase]}px`);
            }
            if (speedNode) speedNode.textContent = `${s.toFixed(1)} km/h`;
            if (actionNode) {
              if (action === 'up') actionNode.textContent = 'PUSH';
              else if (action === 'down') actionNode.textContent = 'EASE';
              else actionNode.textContent = 'HOLD';
            }
            window.__velox_scene_state = state;
          };
          window.veloxSetSceneTheme = function(theme) {
            const scene = document.getElementById('ve-scene');
            const labelNode = document.getElementById('ve-scene-theme-label');
            if (!scene) return;
            const nextTheme = ['forest', 'alpine', 'neon'].includes(theme) ? theme : 'forest';
            const labels = {
              forest: 'Forest trail',
              alpine: 'Alpine dawn',
              neon: 'Neon night',
            };
            scene.dataset.theme = nextTheme;
            if (labelNode) labelNode.textContent = labels[nextTheme] || 'Forest trail';
            if (window.__velox_three_state) {
              window.__velox_three_state.theme = nextTheme;
            } else {
              window.__velox_three_state = { speed: 0, cadence: 0, power: 0, intensity: 'mid', action: 'steady', theme: nextTheme };
            }
            window.veloxEnsureThreeScene();
          };
          window.setTimeout(() => {
            if (window.veloxSetSceneTheme) window.veloxSetSceneTheme('neon');
            if (window.veloxEnsureThreeScene) window.veloxEnsureThreeScene();
          }, 0);
          window.addEventListener('velox-three-ready', () => {
            if (window.veloxEnsureThreeScene) window.veloxEnsureThreeScene();
          });
          window.veloxPinballFx = function(kind, label) {
            const scene = document.getElementById('ve-scene');
            const fx = document.getElementById('ve-fx');
            if (!scene || !fx) return;
            scene.classList.remove('fx-bonus', 'fx-multi', 'fx-jackpot');
            void scene.offsetWidth;
            const cssKind = (kind === 'jackpot' || kind === 'multi') ? kind : 'bonus';
            scene.classList.add(`fx-${cssKind}`);
            fx.classList.remove('bonus', 'multi', 'jackpot', 'show');
            fx.classList.add(cssKind);
            if (label) fx.textContent = label;
            else if (cssKind === 'jackpot') fx.textContent = 'JACKPOT!';
            else if (cssKind === 'multi') fx.textContent = 'MULTI!';
            else fx.textContent = 'BONUS!';
            void fx.offsetWidth;
            fx.classList.add('show');
            window.setTimeout(() => {
              fx.classList.remove('show');
              scene.classList.remove('fx-bonus', 'fx-multi', 'fx-jackpot');
            }, 850);
            window.veloxPinballDotBurst(cssKind, cssKind === 'jackpot' ? 22 : 14);
          };
          window.veloxCoachCue = function(kind, label, durationMs) {
            const scene = document.getElementById('ve-scene');
            const fx = document.getElementById('ve-fx');
            if (!scene || !fx) return;
            const cssKind = (
              kind === 'phase-effort' || kind === 'phase-recover' || kind === 'phase'
            ) ? kind : 'coach';
            fx.classList.remove(
              'bonus', 'multi', 'jackpot', 'coach', 'phase', 'phase-effort',
              'phase-recover', 'show'
            );
            fx.classList.add(cssKind);
            fx.textContent = label || (
              cssKind === 'coach' ? 'GOOD JOB' : 'TRANSITION'
            );
            void fx.offsetWidth;
            fx.classList.add('show');
            const duration = Math.max(1100, Number(durationMs || 1700));
            window.setTimeout(() => {
              fx.classList.remove('show');
            }, duration);
          };
          window.veloxPinballDotBurst = function(kind, count) {
            const scene = document.getElementById('ve-scene');
            if (!scene) return;
            const total = Math.max(6, Number(count || 14));
            for (let i = 0; i < total; i += 1) {
              const dot = document.createElement('span');
              dot.className = `ve-dot ${kind || 'bonus'}`;
              dot.style.setProperty('--x', `${50 + (Math.random() * 16 - 8)}%`);
              dot.style.setProperty('--y', `${44 + (Math.random() * 18 - 9)}%`);
              scene.appendChild(dot);
              const angle = Math.random() * Math.PI * 2;
              const dist = 18 + Math.random() * 44;
              const dx = Math.cos(angle) * dist;
              const dy = Math.sin(angle) * dist;
              dot.animate(
                [
                  { transform: 'translate(-50%, -50%) scale(1)', opacity: 0.95 },
                  {
                    transform: `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(0.25)`,
                    opacity: 0,
                  },
                ],
                { duration: 520 + Math.random() * 220, easing: 'cubic-bezier(.2,.7,.2,1)' },
              );
              window.setTimeout(() => dot.remove(), 900);
            }
          };
          window.veloxPinballPattern = function(pattern) {
            const p = pattern || 'bonus_chain';
            const seq = {
              bonus_chain: [
                [0, 'bonus', 'BONUS!'],
                [280, 'bonus', 'KEEP IT!'],
              ],
              step_clear: [
                [0, 'multi', 'STEP CLEAR'],
                [340, 'bonus', 'CLEAN LINE'],
              ],
              jackpot_rush: [
                [0, 'multi', 'MULTI UP'],
                [260, 'bonus', 'OVERDRIVE'],
                [560, 'jackpot', 'JACKPOT!'],
              ],
            }[p] || [[0, 'bonus', 'BONUS!']];
            seq.forEach((item) => {
              const [delay, kind, label] = item;
              window.setTimeout(() => window.veloxPinballFx(kind, label), delay);
            });
          };
          window.veloxDmd = (function() {
            let canvas = null;
            let ctx = null;
            let ticker = '';
            let flash = '';
            let flashKind = 'bonus';
            let flashUntil = 0;
            const off = document.createElement('canvas');
            off.width = 320;
            off.height = 64;
            const octx = off.getContext('2d', { willReadFrequently: true });

            function colors(kind) {
              if (kind === 'jackpot') return ['#2b0900', '#ff8800', '#ffd24d'];
              if (kind === 'multi') return ['#180a2a', '#c188ff', '#f0d9ff'];
              return ['#240900', '#ff9f1c', '#ffd166'];
            }

            function drawDotGrid(base, glow, hot) {
              if (!ctx || !canvas || !octx) return;
              const w = canvas.width;
              const h = canvas.height;
              const cols = off.width;
              const rows = off.height;
              const cw = w / cols;
              const ch = h / rows;
              const frame = octx.getImageData(0, 0, cols, rows).data;
              ctx.fillStyle = '#050202';
              ctx.fillRect(0, 0, w, h);
              for (let y = 0; y < rows; y += 1) {
                for (let x = 0; x < cols; x += 1) {
                  const px = (x + 0.5) * cw;
                  const py = (y + 0.5) * ch;
                  const idx = ((y * cols) + x) * 4;
                  const lit = frame[idx] > 30;
                  ctx.fillStyle = lit ? glow : base;
                  ctx.beginPath();
                  ctx.arc(px, py, Math.min(cw, ch) * (lit ? 0.28 : 0.18), 0, Math.PI * 2);
                  ctx.fill();
                  if (lit) {
                    ctx.fillStyle = hot;
                    ctx.beginPath();
                    ctx.arc(px, py, Math.min(cw, ch) * 0.1, 0, Math.PI * 2);
                    ctx.fill();
                  }
                }
              }
            }

            function render() {
              if (!ctx || !canvas || !octx) return;
              const now = Date.now();
              const kind = flashUntil > now ? flashKind : 'bonus';
              const colorset = colors(kind);
              octx.fillStyle = '#000';
              octx.fillRect(0, 0, off.width, off.height);
              octx.fillStyle = '#fff';
              octx.font = 'bold 22px monospace';
              octx.textAlign = 'center';
              octx.textBaseline = 'middle';
              const msg = flashUntil > now ? flash : ticker;
              octx.fillText(msg || 'VELOX READY', 160, 22);
              octx.font = 'bold 12px monospace';
              octx.fillText('TRACK  •  COMPETE  •  WIN', 160, 46);
              try {
                drawDotGrid(colorset[0], colorset[1], colorset[2]);
              } catch (e) {
                // Keep render loop alive even if one frame fails.
              }
              requestAnimationFrame(render);
            }

            return {
              init: function() {
                canvas = document.getElementById('ve-dmd');
                if (!canvas) return;
                ctx = canvas.getContext('2d');
                const rect = canvas.getBoundingClientRect();
                canvas.width = Math.max(320, Math.floor(rect.width));
                canvas.height = Math.max(88, Math.floor(rect.height));
                if (ctx) ctx.setTransform(1, 0, 0, 1, 0, 0);
                if (!window.__velox_dmd_started) {
                  window.__velox_dmd_started = true;
                  requestAnimationFrame(render);
                }
              },
              ticker: function(msg) {
                ticker = String(msg || '').slice(0, 24);
              },
              flash: function(msg, kind) {
                flash = String(msg || '').slice(0, 24);
                flashKind = kind || 'bonus';
                flashUntil = Date.now() + 1200;
              },
            };
          })();
          window.veloxCspSafeUiLoop = function() {
            if (window.__velox_csp_loop_started) return;
            window.__velox_csp_loop_started = true;
            let lastReward = '';
            let dmdInitDone = false;
            const parseVal = (id) => {
              const el = document.getElementById(id);
              if (!el) return 0;
              const m = String(el.textContent || '').replace(',', '.').match(/-?\\d+(?:\\.\\d+)?/);
              return m ? Number(m[0]) : 0;
            };
            const textOf = (id) => {
              const el = document.getElementById(id);
              return String(el ? (el.textContent || '') : '').trim();
            };
            window.setInterval(() => {
              if (!dmdInitDone) {
                window.veloxDmd.init();
                dmdInitDone = true;
              }
              const speed = parseVal('ve-kpi-speed');
              const cadence = parseVal('ve-kpi-cadence');
              const guidance = textOf('ve-guidance');
              let action = 'steady';
              let inZone = true;
              if (/Accelere|Augmente/i.test(guidance)) {
                action = 'up';
                inZone = false;
              } else if (/Reduis|Baisse/i.test(guidance)) {
                action = 'down';
                inZone = false;
              } else if (/Action:\\s*-/i.test(guidance)) {
                inZone = false;
              }
              window.veloxUpdateScene(speed, cadence, parseVal('ve-kpi-power'), inZone, action);
              if (window.veloxMiniGraph) {
                window.veloxMiniGraph.push(parseVal('ve-kpi-power'), cadence);
              }

              const score = textOf('ve-score');
              const multi = textOf('ve-pinball-multi');
              const jackpot = textOf('ve-pinball-jackpot');
              const step = textOf('ve-step-info');
              window.veloxDmd.ticker(`${score}  ${multi}  ${jackpot}  ${step}`.slice(0, 24));

              const reward = textOf('ve-pinball-reward');
              if (reward && reward !== lastReward && !/READY/i.test(reward)) {
                let kind = 'bonus';
                if (/JACKPOT/i.test(reward)) kind = 'jackpot';
                else if (/MULTI/i.test(reward)) kind = 'multi';
                window.veloxPinballFx(kind, reward.slice(0, 20));
                window.veloxDmd.flash(reward.slice(0, 22), kind);
                lastReward = reward;
              }
            }, 260);
          };
          window.addEventListener('DOMContentLoaded', () => {
            window.veloxCspSafeUiLoop();
          });
          window.veloxMiniGraph = (function() {
            let canvas = null;
            let ctx = null;
            const power = [];
            const cadence = [];
            const maxPoints = 90;
            function draw() {
              if (!ctx || !canvas) return;
              const w = canvas.width;
              const h = canvas.height;
              ctx.fillStyle = '#040912';
              ctx.fillRect(0, 0, w, h);
              ctx.strokeStyle = 'rgba(148,163,184,.22)';
              ctx.lineWidth = 1;
              for (let i = 1; i <= 3; i += 1) {
                const y = (h / 4) * i;
                ctx.beginPath();
                ctx.moveTo(0, y);
                ctx.lineTo(w, y);
                ctx.stroke();
              }
              const drawLine = (arr, color, maxv) => {
                if (!arr.length) return;
                ctx.strokeStyle = color;
                ctx.lineWidth = 2;
                ctx.beginPath();
                arr.forEach((v, i) => {
                  const x = (i / Math.max(1, maxPoints - 1)) * w;
                  const y = h - Math.max(0, Math.min(1, v / maxv)) * (h - 6) - 3;
                  if (i === 0) ctx.moveTo(x, y);
                  else ctx.lineTo(x, y);
                });
                ctx.stroke();
              };
              drawLine(power, '#22d3ee', 420);
              drawLine(cadence, '#86efac', 130);
            }
            return {
              init: function() {
                canvas = document.getElementById('ve-mini-graph');
                if (!canvas) return;
                ctx = canvas.getContext('2d');
                const rect = canvas.getBoundingClientRect();
                canvas.width = Math.max(320, Math.floor(rect.width));
                canvas.height = Math.max(86, Math.floor(rect.height));
                draw();
              },
              push: function(p, c) {
                if (!canvas || !ctx) this.init();
                power.push(Number.isFinite(p) ? Number(p) : 0);
                cadence.push(Number.isFinite(c) ? Number(c) : 0);
                while (power.length > maxPoints) power.shift();
                while (cadence.length > maxPoints) cadence.shift();
                draw();
              },
            };
          })();
        </script>
        """
        .replace("__THREE_MODULE_URL__", THREE_MODULE_URL)
        .replace("__SPRITE_URL__", SPRITE_URL)
        .replace("__SCENE_BG_URL__", SCENE_BG_URL)
        .replace("__SCENE_BG_ALT_1_URL__", SCENE_BG_ALT_1_URL)
        .replace("__SCENE_BG_ALT_2_URL__", SCENE_BG_ALT_2_URL)
        .replace("__FOREST_PARALLAX_BACK_URL__", FOREST_PARALLAX_BACK_URL)
        .replace("__FOREST_PARALLAX_MID_URL__", FOREST_PARALLAX_MID_URL)
        .replace("__FOREST_PARALLAX_FRONT_URL__", FOREST_PARALLAX_FRONT_URL)
        .replace("__FOREST_PARALLAX_OVERLAY_URL__", FOREST_PARALLAX_OVERLAY_URL)
        .replace("__ALPINE_PARALLAX_SKY_URL__", ALPINE_PARALLAX_SKY_URL)
        .replace("__ALPINE_PARALLAX_FAR_URL__", ALPINE_PARALLAX_FAR_URL)
        .replace("__ALPINE_PARALLAX_MID_URL__", ALPINE_PARALLAX_MID_URL)
        .replace("__ALPINE_PARALLAX_CLOUDS_URL__", ALPINE_PARALLAX_CLOUDS_URL)
        .replace("__NEON_PARALLAX_STARS_URL__", NEON_PARALLAX_STARS_URL)
        .replace("__NEON_PARALLAX_BACK_URL__", NEON_PARALLAX_BACK_URL)
        .replace("__NEON_PARALLAX_MID_URL__", NEON_PARALLAX_MID_URL)
        .replace("__NEON_PARALLAX_FRONT_URL__", NEON_PARALLAX_FRONT_URL)
        .replace("__NEON_PARALLAX_CLOUDS_URL__", NEON_PARALLAX_CLOUDS_URL)
        .replace("__DMD_CYCLIST_URL__", DMD_CYCLIST_URL)
    )

    templates = list_templates()
    workout_options: list[WorkoutOption] = []
    workout_option_by_label: dict[str, WorkoutOption] = {}
    workout_option_labels: list[str] = []

    devices: list[ScannedDevice] = []
    selected_device_address: str | None = None
    selected_template_label = ""
    zone_compliance = {"power_ok": 0, "power_total": 0, "rpm_ok": 0, "rpm_total": 0}
    hm_detected = False
    hm_sim_seed = 96.0
    session_started_at_utc: str | None = None
    current_snapshot_path: Path | None = None
    current_snapshot_csv_path: Path | None = None
    sound_alerts = True
    coaching_stabilizer = ActionStabilizer(min_switch_sec=ACTION_SWITCH_MIN_SEC)
    last_coaching_alert_key: str | None = None
    goal_tracker = GoalTracker(DEFAULT_GAME_GOALS)
    last_goal_tick_ts: float | None = None
    pinball_score_bonus = 0
    pinball_multiplier = 1
    pinball_jackpots = 0
    pinball_last_bonus = "READY"
    pinball_last_step_seen = 0
    pinball_last_jackpot_ts = 0.0
    last_encourage_bucket: int | None = None
    last_transition_marker: tuple[int, int, str] | None = None
    analytics_demo_mode = False
    analytics_window = 7

    timeline_labels: list[str] = []
    timeline_expected_power: list[int] = []
    timeline_expected_cadence: list[float] = []
    timeline_actual_power: list[int | None] = []
    timeline_actual_cadence: list[float | None] = []
    timeline_step_ranges: list[tuple[int, int, str]] = []
    metric_samples: list[tuple[int | None, float | None, float | None]] = []

    with ui.column().classes("w-full gap-2") as setup_header:
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.label("VELOX ENGINE").classes("text-xl font-semibold tracking-wide")
            with ui.row().classes("items-center gap-4"):
                with ui.row().classes("items-center gap-1"):
                    ht_icon = ui.icon("directions_bike").classes("text-base")
                    ui.label("HT").classes("text-xs font-semibold")
                    ht_name_label = ui.label("not connected").classes("text-xs gb-muted")
                    erg_badge = ui.label("ERG ?").classes("text-xs font-semibold")
                with ui.row().classes("items-center gap-1"):
                    hm_icon = ui.icon("favorite").classes("text-base")
                    ui.label("HM").classes("text-xs font-semibold")
                    hm_name_label = ui.label("not connected").classes("text-xs gb-muted")
                open_connections_btn = ui.button("Connections").props("outline")
                back_to_training_btn = ui.button("Back to training").props("outline")
                back_to_training_btn.set_visibility(False)
        status_label = ui.label("Status: Not connected").classes("text-lg font-semibold")
        if simulate_ht:
            ui.label("SIM MODE - no BLE required").classes("text-orange-500 font-bold")

    with ui.column().classes("w-full gap-4") as setup_view:
        ui.label("Course Setup").classes("text-xl font-semibold")
        with ui.card().classes("w-full gb-card"):
            with ui.grid().classes("w-full grid-cols-1 md:grid-cols-2 xl:grid-cols-5 gap-2"):
                band_select = ui.select(
                    ["All", "<=30 min", "30-45 min", "45-60 min", ">=60 min"],
                    value="All",
                    label="Duration",
                ).classes("w-full min-w-[180px]")
                ftp_input = ui.number("FTP (W)", value=220, min=80, max=500).classes(
                    "w-full min-w-[140px]"
                )
                mode_select = ui.select(
                    ["erg", "resistance", "slope"], value="erg", label="Mode"
                ).classes("w-full min-w-[150px]")
                delay_input = ui.number(
                    "Start delay (sec)", value=int(max(0, start_delay_sec)), min=0, max=180
                ).classes("w-full min-w-[170px]")
                builder_btn = ui.button("Workout builder").classes("w-full")
            selected_course_label = ui.label("Selected course: -").classes(
                "text-sm font-medium whitespace-normal break-words"
            )
            course_cards_grid = ui.grid().classes(
                "w-full grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3"
            )
            course_info = ui.label("No course loaded").classes("text-sm text-slate-300")

        plan_chart = ui.echart(
            {
                "title": {
                    "text": "Difficulty curve (target watts)",
                    "left": "center",
                    "textStyle": {
                        "color": "#ffffff",
                        "fontWeight": "bold",
                        "fontFamily": "Arial",
                    },
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

        ui.label("Analytics").classes("text-base font-medium")
        with ui.card().classes("w-full gb-card gb-compact"):
            with ui.row().classes("w-full items-center gap-2"):
                analytics_demo_switch = ui.switch("Analytics demo (sample data)", value=False)
                analytics_window_select = ui.select(
                    {7: "Last 7", 30: "Last 30"},
                    value=7,
                    label="Window",
                ).classes("min-w-[140px]")
            with ui.grid().classes("w-full grid-cols-2 md:grid-cols-4 gap-2"):
                analytics_sessions = ui.label("Sessions: 0").classes("text-sm")
                analytics_completed = ui.label("Completion: -").classes("text-sm")
                analytics_compliance = ui.label("Compliance (both): -").classes("text-sm")
                analytics_time = ui.label("Total time: 0 min").classes("text-sm")
            with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 gap-2"):
                analytics_power = ui.label("Avg power: -").classes("text-sm")
                analytics_cadence = ui.label("Avg cadence: -").classes("text-sm")
                analytics_speed = ui.label("Avg speed: -").classes("text-sm")
            analytics_trend_chart = ui.echart(
                {
                    "title": {
                        "text": "Trends (sessions)",
                        "left": "center",
                        "textStyle": {
                            "color": "#ffffff",
                            "fontWeight": "bold",
                            "fontFamily": "Arial",
                        },
                    },
                    "tooltip": {"trigger": "axis"},
                    "legend": {
                        "data": ["Compliance %", "Avg Power (W)"],
                        "top": 26,
                        "textStyle": {"color": "#ffffff"},
                    },
                    "xAxis": {"type": "category", "data": [], "axisLabel": {"color": "#ffffff"}},
                    "yAxis": [
                        {
                            "type": "value",
                            "name": "%",
                            "axisLabel": {"color": "#ffffff"},
                            "nameTextStyle": {"color": "#ffffff"},
                            "min": 0,
                            "max": 100,
                        },
                        {
                            "type": "value",
                            "name": "W",
                            "axisLabel": {"color": "#ffffff"},
                            "nameTextStyle": {"color": "#ffffff"},
                        },
                    ],
                    "series": [
                        {
                            "name": "Compliance %",
                            "type": "line",
                            "data": [],
                            "showSymbol": True,
                            "itemStyle": {"color": "#22d3ee"},
                            "lineStyle": {"width": 2},
                        },
                        {
                            "name": "Avg Power (W)",
                            "type": "line",
                            "yAxisIndex": 1,
                            "data": [],
                            "showSymbol": True,
                            "itemStyle": {"color": "#7ddc74"},
                            "lineStyle": {"width": 2},
                        },
                    ],
                    "grid": {"left": 42, "right": 48, "top": 62, "bottom": 34},
                    "animation": False,
                }
            ).classes("w-full h-56")

    with ui.column().classes("w-full gap-4") as connections_view:
        ui.label("Connections").classes("text-xl font-semibold")
        with ui.card().classes("w-full gb-card"):
            ui.label("Home Trainer (HT)").classes("text-base font-semibold")
            with ui.row().classes("w-full items-end gap-2"):
                ht_device_select = ui.select([], label="HT device").classes("min-w-[360px]")
                ht_scan_btn = ui.button("Scan HT")
                ht_connect_btn = ui.button("Connect HT")
                ht_disconnect_btn = ui.button("Disconnect HT")
                ht_disconnect_btn.disable()
        with ui.card().classes("w-full gb-card"):
            ui.label("Heart Monitor (HM)").classes("text-base font-semibold")
            with ui.row().classes("w-full items-center gap-3"):
                hm_sim_switch = ui.switch("Simulate HM", value=True)
                hm_detect_btn = ui.button("Detect HM")
                hm_connect_btn = ui.button("Connect HM")
                hm_disconnect_btn = ui.button("Disconnect HM")
                hm_hint_label = ui.label("HM: not detected").classes("text-sm gb-muted")
                hm_disconnect_btn.disable()
            ui.label(
                "For now HM is simulated locally. Later we can plug real BLE HRM."
            ).classes("text-xs gb-muted")

    connections_view.set_visibility(False)

    with ui.column().classes("w-full gap-2") as workout_view:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Workout Session").classes("text-lg gb-title-neutral")
            with ui.row().classes("gap-2"):
                back_btn = ui.button("Back to setup").props("outline color=white")
                stop_btn = ui.button("Stop session").props("color=negative")
                stop_btn.disable()

        with ui.row().classes("w-full gap-2") as pinball_hud_row:
            ui.label("MODE: PINBALL").classes("pinball-chip")
            if csp_safe_mode:
                ui.label("CSP-SAFE").classes("pinball-chip")
            pinball_mission_label = ui.label("MISSION: Keep target zone").classes("pinball-chip")
            pinball_multiplier_label = ui.label("MULTI x1").classes("pinball-chip").props(
                "id=ve-pinball-multi"
            )
            pinball_jackpot_label = ui.label("JACKPOT 0").classes(
                "pinball-chip pinball-jackpot"
            ).props("id=ve-pinball-jackpot")
            pinball_reward_label = ui.label("BONUS READY").classes("pinball-chip").props(
                "id=ve-pinball-reward"
            )
        pinball_hud_row.set_visibility(pinball_mode)

        pinball_dmd = ui.html(
            """
            <div class="dmd-shell">
              <img class="dmd-bg" src="__DMD_CYCLIST_URL__" alt="dmd cyclist" />
              <canvas id="ve-dmd" class="dmd-screen"></canvas>
            </div>
            """
            .replace("__DMD_CYCLIST_URL__", DMD_CYCLIST_URL)
        ).classes("w-full")
        pinball_dmd.set_visibility(pinball_mode)

        pinball_mini_graph = ui.html(
            """
            <div class="mini-graph-shell">
              <canvas id="ve-mini-graph" class="mini-graph"></canvas>
            </div>
            """
        ).classes("w-full")
        pinball_mini_graph.set_visibility(pinball_mode)

        with ui.row().classes("w-full gap-2") as pinball_sim_row:
            ui.label("Sim Pinball").classes("pinball-chip")
            sim_multi_btn = ui.button("Trigger Multi").props("outline color=purple")
            sim_jackpot_btn = ui.button("Trigger Jackpot").props("outline color=amber")
            sim_bonus_btn = ui.button("Trigger Bonus").props("outline color=cyan")
            sim_chain_btn = ui.button("Run Combo Chain").props("outline color=pink")
        pinball_sim_row.set_visibility(pinball_mode and simulate_ht)

        with ui.row().classes("w-full gap-2"):
            with ui.card().classes("w-full gb-card gb-compact"):
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    workout_info = ui.label("No workout loaded").classes(
                        "text-sm font-semibold"
                    )
                    step_info = ui.label("Step: -").classes("text-sm").props("id=ve-step-info")
                    elapsed_label = ui.label("Elapsed: 00:00").classes("text-sm")
                    remaining_label = ui.label("Remaining: 00:00").classes("text-sm")
                    compliance_info = ui.label("Compliance: -").classes("text-sm")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    next_step_label = ui.label("Next: -").classes("text-sm gb-muted")
                    transition_status_label = ui.label("Transition: -").classes(
                        "text-sm gb-muted font-semibold"
                    )
                    target_label = ui.label("Targets: -").classes("text-sm gb-muted")
                    mode_label = ui.label("Mode: ERG").classes("text-sm gb-muted")
                    sound_toggle = ui.switch("Sound alerts", value=True).classes("text-sm")
                with ui.row().classes("w-full items-center gap-3"):
                    guidance_label = ui.label("Action: -").classes(
                        "text-sm font-semibold"
                    ).props("id=ve-guidance")

        with ui.grid().classes("w-full grid-cols-1 md:grid-cols-5 gap-2"):
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Power").classes("text-xs gb-title-neutral")
                kpi_power = ui.label("-- W").classes("gb-number").props("id=ve-kpi-power")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Cadence").classes("text-xs gb-title-neutral")
                kpi_cadence = ui.label("-- rpm").classes("gb-number").props("id=ve-kpi-cadence")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Heart rate").classes("text-xs gb-title-neutral")
                kpi_hr = ui.label("-- bpm").classes("gb-number")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Speed").classes("text-xs gb-title-neutral")
                kpi_speed = ui.label("-- km/h").classes("gb-number").props("id=ve-kpi-speed")
            with ui.card().classes("w-full gb-kpi gb-compact"):
                ui.label("Distance").classes("text-xs gb-title-neutral")
                kpi_distance = ui.label("0,00 km").classes("gb-number")

        with ui.card().classes("w-full gb-card gb-compact"):
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                game_score_label = ui.label("Score: 0").classes("text-sm gb-pixel").props(
                    "id=ve-score"
                )
                game_coins_label = ui.label("Coins: 0").classes("text-sm gb-pixel")
                game_streak_label = ui.label("Streak: 0").classes("text-sm gb-pixel")
                game_goal_label = ui.label("Goal: -").classes("text-xs gb-pixel")
                game_goal_progress_label = ui.label("0/0 s").classes("text-xs gb-pixel")
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                ui.label("Arcade scenery demo").classes("text-xs font-semibold uppercase tracking-wider text-slate-300")
                scene_theme_toggle = ui.toggle(
                    {"forest": "Forest trail", "alpine": "Alpine dawn", "neon": "Neon night"},
                    value="neon",
                    on_change=lambda e: _safe_run_js(
                        f"window.veloxSetSceneTheme('{e.value}');"
                    ),
                ).props("toggle-color=cyan glossy unelevated")
            ui.html(
                """
                <div id="ve-scene" class="ve-scene" data-zone="ok" data-theme="neon">
                  <div class="ve-bg ve-bg-sky"></div>
                  <div class="ve-bg ve-bg-far"></div>
                  <div class="ve-bg ve-bg-mid"></div>
                  <div class="ve-bg ve-bg-front"></div>
                  <div class="ve-bg ve-bg-overlay"></div>
                  <div class="ve-road"></div>
                  <div id="ve-three-layer" class="ve-three-layer"></div>
                  <div class="ve-three-edge-fade left"></div>
                  <div class="ve-three-edge-fade right"></div>
                  <div id="ve-three-debug" class="ve-three-debug warn">Three script pending</div>
                  <div id="ve-fx" class="ve-fx">BONUS!</div>
                  <div class="ve-hud">
                    <span id="ve-scene-theme-label" class="ve-scenery-badge">Neon night</span>
                    <span id="ve-scene-action" class="ve-hud-action">HOLD</span>
                    <span id="ve-scene-speed" class="ve-hud-speed">0,0 km/h</span>
                  </div>
                  <div class="ve-rider">
                    <div class="ve-rider-shadow"></div>
                    <div class="ve-rider-speedlines"></div>
                    <div class="ve-rider-dust"></div>
                    <div class="ve-rider-glow"></div>
                    <div id="ve-sprite" class="ve-sprite"></div>
                    <div class="ve-rider-occlusion"></div>
                  </div>
                </div>
                """
            ).classes("w-full")

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
                "legend": {
                    "data": [
                        "Power expected",
                        "Power actual",
                        "Cadence expected",
                        "Cadence actual",
                    ],
                    "top": 28,
                    "textStyle": {"color": "#ffffff"},
                },
                "tooltip": {"trigger": "axis"},
                "xAxis": {
                    "type": "category",
                    "data": [],
                    "axisLabel": {"color": "#ffffff"},
                    "boundaryGap": False,
                },
                "yAxis": [
                    {
                        "type": "value",
                        "name": "W",
                        "axisLabel": {"color": "#ffffff"},
                        "nameTextStyle": {"color": "#ffffff", "fontWeight": "bold"},
                    },
                    {
                        "type": "value",
                        "name": "rpm",
                        "axisLabel": {"color": "#ffffff"},
                        "nameTextStyle": {"color": "#ffffff", "fontWeight": "bold"},
                    },
                ],
                "series": [
                    {
                        "name": "Power expected",
                        "type": "line",
                        "data": [],
                        "showSymbol": False,
                        "lineStyle": {"type": "dashed", "width": 2},
                        "itemStyle": {"color": "#6388ff"},
                    },
                    {
                        "name": "Power actual",
                        "type": "line",
                        "data": [],
                        "showSymbol": False,
                        "lineStyle": {"width": 2},
                        "itemStyle": {"color": "#7ddc74"},
                    },
                    {
                        "name": "Cadence expected",
                        "type": "line",
                        "yAxisIndex": 1,
                        "data": [],
                        "showSymbol": False,
                        "lineStyle": {"type": "dashed", "width": 2},
                        "itemStyle": {"color": "#ffd15a"},
                    },
                    {
                        "name": "Cadence actual",
                        "type": "line",
                        "yAxisIndex": 1,
                        "data": [],
                        "showSymbol": False,
                        "lineStyle": {"width": 2},
                        "itemStyle": {"color": "#ff7d7d"},
                    },
                ],
                "grid": {"left": 50, "right": 50, "top": 72, "bottom": 40},
                "animation": False,
            }
        ).classes("w-full h-72")

    workout_view.set_visibility(False)

    with ui.column().classes("w-full gap-3") as summary_view:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Session recap").classes("text-xl gb-title-neutral")
            with ui.row().classes("gap-2"):
                summary_export_json_btn = ui.button("Export JSON")
                summary_export_csv_btn = ui.button("Export CSV")
                summary_back_btn = ui.button("Back to setup").props("outline")
        with ui.card().classes("w-full gb-card"):
            summary_status_label = ui.label("Status: -").classes("text-base font-semibold")
            with ui.grid().classes("w-full grid-cols-2 md:grid-cols-4 gap-2"):
                summary_workout_label = ui.label("Workout: -").classes("text-sm")
                summary_mode_label = ui.label("Mode: -").classes("text-sm")
                summary_elapsed_label = ui.label("Elapsed: -").classes("text-sm")
                summary_distance_label = ui.label("Distance: -").classes("text-sm")
            with ui.grid().classes("w-full grid-cols-2 md:grid-cols-4 gap-2"):
                summary_power_label = ui.label("Avg power: -").classes("text-sm")
                summary_cadence_label = ui.label("Avg cadence: -").classes("text-sm")
                summary_speed_label = ui.label("Avg speed: -").classes("text-sm")
                summary_both_label = ui.label("Both compliance: -").classes("text-sm")

    summary_view.set_visibility(False)

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
        theme_cls = "gb-theme-pinball" if pinball_mode else "gb-theme-classic"
        if core.loop is None:
            # NiceGUI loop/client not ready yet during initial startup.
            return
        _safe_run_js(
            "document.body.classList.remove("
            "'gb-theme-classic','gb-theme-pinball'"
            ");"
            f"document.body.classList.add('{theme_cls}');"
        )

    def _safe_run_js(code: str) -> None:
        """Best-effort JS execution; skip when timer/background has no slot/client context."""
        if csp_safe_mode:
            return
        if core.loop is None:
            return
        try:
            ui.run_javascript(code)
        except (AssertionError, RuntimeError):
            return

    def trigger_pinball_event(kind: str, *, manual: bool = False) -> None:
        nonlocal pinball_score_bonus, pinball_multiplier, pinball_jackpots
        nonlocal pinball_last_bonus, pinball_last_jackpot_ts
        if not pinball_mode:
            return
        now_ts = time.monotonic()
        if kind == "multi":
            reward = 80 * pinball_multiplier
            pinball_score_bonus += reward
            pinball_multiplier = min(8, pinball_multiplier + 1)
            pinball_last_bonus = f"+{reward} MULTI"
            _safe_run_js(f"window.veloxPinballFx('multi', 'MULTI +{reward}');")
            _safe_run_js(f"window.veloxDmd.flash('MULTI +{reward}', 'multi');")
            return
        if kind == "jackpot":
            if not manual and (now_ts - pinball_last_jackpot_ts) < 6.0:
                return
            pinball_jackpots += 1
            reward = 150 * pinball_multiplier
            pinball_score_bonus += reward
            pinball_last_bonus = f"JACKPOT +{reward}"
            pinball_last_jackpot_ts = now_ts
            _safe_run_js(f"window.veloxPinballFx('jackpot', 'JACKPOT +{reward}');")
            _safe_run_js(f"window.veloxDmd.flash('JACKPOT +{reward}', 'jackpot');")
            return
        reward = 60 * pinball_multiplier
        pinball_score_bonus += reward
        pinball_last_bonus = f"+{reward} BONUS"
        _safe_run_js(f"window.veloxPinballFx('bonus', 'BONUS +{reward}');")
        _safe_run_js(f"window.veloxDmd.flash('BONUS +{reward}', 'bonus');")

    def trigger_pinball_pattern(name: str) -> None:
        if not pinball_mode:
            return
        _safe_run_js(f"window.veloxPinballPattern('{name}');")

    def show_setup_screen() -> None:
        setup_header.set_visibility(True)
        setup_view.set_visibility(True)
        connections_view.set_visibility(False)
        back_to_training_btn.set_visibility(False)
        workout_view.set_visibility(False)
        summary_view.set_visibility(False)

    def show_connections_screen() -> None:
        setup_header.set_visibility(True)
        setup_view.set_visibility(False)
        connections_view.set_visibility(True)
        back_to_training_btn.set_visibility(True)
        workout_view.set_visibility(False)
        summary_view.set_visibility(False)

    def show_workout_screen() -> None:
        setup_header.set_visibility(False)
        setup_view.set_visibility(False)
        connections_view.set_visibility(False)
        workout_view.set_visibility(True)
        summary_view.set_visibility(False)
        if pinball_mode:
            _safe_run_js("window.veloxDmd.init();")

    def show_summary_screen() -> None:
        setup_header.set_visibility(True)
        setup_view.set_visibility(False)
        connections_view.set_visibility(False)
        back_to_training_btn.set_visibility(False)
        workout_view.set_visibility(False)
        summary_view.set_visibility(True)

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
                    ui.label(option.name).classes(
                        "text-base font-semibold whitespace-normal break-words"
                    )
                    ui.label(
                        f"{option.category} | {_fmt_duration(option.duration_sec)} | "
                        f"{option.avg_intensity_pct}% FTP"
                    ).classes("text-xs text-slate-300 whitespace-normal")
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
        def demo_sessions() -> list[SessionRecord]:
            now = datetime.now(tz=timezone.utc)
            out: list[SessionRecord] = []
            seeds = [
                ("FTP Builder 2x8", 34 * 60, 210.0, 86.0, 31.5, 92.0, 88.0, 84.0, True),
                ("VO2 Burst 5x3", 42 * 60, 238.0, 91.0, 33.2, 85.0, 82.0, 77.0, True),
                ("Tempo 45", 45 * 60, 196.0, 83.5, 30.1, 90.0, 86.0, 83.0, True),
                ("Recovery Spin", 30 * 60, 128.0, 88.0, 27.3, 96.0, 95.0, 94.0, True),
                ("Sweet Spot 3x12", 58 * 60, 224.0, 84.0, 31.0, 82.0, 80.0, 74.0, False),
                ("Cadence Drill", 36 * 60, 172.0, 98.0, 29.7, 88.0, 93.0, 84.0, True),
                ("Over-Under 4x9", 52 * 60, 231.0, 87.2, 32.1, 79.0, 77.0, 70.0, False),
                ("Endurance 60", 60 * 60, 182.0, 82.3, 29.2, 93.0, 91.0, 89.0, True),
            ]
            for idx, item in enumerate(seeds):
                (
                    name,
                    elapsed,
                    avg_power,
                    avg_cadence,
                    avg_speed,
                    p_comp,
                    rpm_comp,
                    both_comp,
                    completed,
                ) = item
                ended = now - timedelta(days=idx)
                started = ended - timedelta(seconds=elapsed)
                out.append(
                    SessionRecord(
                        started_at_utc=started.isoformat(),
                        ended_at_utc=ended.isoformat(),
                        workout_name=name,
                        target_mode="erg",
                        ftp_watts=220,
                        completed=completed,
                        planned_duration_sec=elapsed,
                        elapsed_duration_sec=elapsed,
                        distance_km=(avg_speed * (elapsed / 3600.0)),
                        avg_power_watts=avg_power,
                        avg_cadence_rpm=avg_cadence,
                        avg_speed_kmh=avg_speed,
                        power_compliance_pct=p_comp,
                        rpm_compliance_pct=rpm_comp,
                        both_compliance_pct=both_comp,
                    )
                )
            return out

        rows: list[dict[str, str]] = []
        sessions = demo_sessions() if analytics_demo_mode else load_recent_sessions(limit=30)
        for item in sessions[:12]:
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

        if not sessions:
            analytics_sessions.text = "Sessions: 0"
            analytics_completed.text = "Completion: -"
            analytics_compliance.text = "Compliance (both): -"
            analytics_time.text = "Total time: 0 min"
            analytics_power.text = "Avg power: -"
            analytics_cadence.text = "Avg cadence: -"
            analytics_speed.text = "Avg speed: -"
            return

        completed_count = sum(1 for s in sessions if s.completed)
        total_count = len(sessions)
        completion_pct = (completed_count * 100.0) / max(1, total_count)
        total_min = sum(max(0, s.elapsed_duration_sec) for s in sessions) // 60

        both_vals = [s.both_compliance_pct for s in sessions if s.both_compliance_pct is not None]
        avg_power_vals = [s.avg_power_watts for s in sessions if s.avg_power_watts is not None]
        avg_cadence_vals = [s.avg_cadence_rpm for s in sessions if s.avg_cadence_rpm is not None]
        avg_speed_vals = [s.avg_speed_kmh for s in sessions if s.avg_speed_kmh is not None]

        avg_both = (sum(both_vals) / len(both_vals)) if both_vals else None
        avg_power = (sum(avg_power_vals) / len(avg_power_vals)) if avg_power_vals else None
        avg_cadence = (sum(avg_cadence_vals) / len(avg_cadence_vals)) if avg_cadence_vals else None
        avg_speed = (sum(avg_speed_vals) / len(avg_speed_vals)) if avg_speed_vals else None

        analytics_sessions.text = f"Sessions: {total_count}"
        analytics_completed.text = (
            f"Completion: {completion_pct:.0f}% ({completed_count}/{total_count})"
        )
        analytics_compliance.text = (
            f"Compliance (both): {avg_both:.0f}%"
            if avg_both is not None
            else "Compliance (both): -"
        )
        analytics_time.text = f"Total time: {total_min} min"
        analytics_power.text = (
            f"Avg power: {avg_power:.0f} W" if avg_power is not None else "Avg power: -"
        )
        analytics_cadence.text = (
            f"Avg cadence: {avg_cadence:.1f} rpm"
            if avg_cadence is not None
            else "Avg cadence: -"
        )
        analytics_speed.text = (
            f"Avg speed: {avg_speed:.1f} km/h" if avg_speed is not None else "Avg speed: -"
        )

        # Trend chart (oldest -> newest) based on selected session window.
        windowed = list(reversed(sessions[: max(1, analytics_window)]))
        labels: list[str] = []
        compliance_points: list[float | None] = []
        power_points: list[float | None] = []
        for idx, s in enumerate(windowed, start=1):
            date_hint = s.ended_at_utc[:10]
            labels.append(f"{idx}|{date_hint}")
            compliance_points.append(s.both_compliance_pct)
            power_points.append(s.avg_power_watts)

        trend_opts = cast(dict[str, Any], analytics_trend_chart.options)
        trend_opts["xAxis"]["data"] = labels
        trend_opts["series"][0]["data"] = compliance_points
        trend_opts["series"][1]["data"] = power_points
        analytics_trend_chart.update()

    def refresh_plan_chart() -> None:
        if plan_chart is None:
            return
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
        if live_chart is None:
            return
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
        nonlocal last_encourage_bucket
        nonlocal last_transition_marker
        status_label.text = f"Status: {state.status}"
        ht_icon.style(f"color: {'#22c55e' if state.connected else '#6b7280'};")
        hm_icon.style(f"color: {'#22c55e' if state.hm_connected else '#6b7280'};")
        ht_name_label.text = state.ht_device_name or "not connected"
        hm_name_label.text = state.hm_device_name or "not connected"
        if state.erg_ready is True:
            erg_badge.text = "ERG OK"
            erg_badge.style(
                "color: #052e16; background: #22c55e; padding: 2px 6px; "
                "border-radius: 10px;"
            )
        else:
            erg_badge.text = "ERG ?"
            erg_badge.style(
                "color: #d1d5db; background: #374151; padding: 2px 6px; "
                "border-radius: 10px;"
            )
        hm_hint_label.text = (
            "HM: connected"
            if state.hm_connected
            else ("HM: detected" if hm_detected else "HM: not detected")
        )
        kpi_power.text = _fmt_power(state.power)
        kpi_cadence.text = _fmt_cadence(state.cadence)
        kpi_hr.text = (
            f"{int(round(state.heart_rate_bpm))} bpm"
            if state.heart_rate_bpm is not None
            else "-- bpm"
        )
        kpi_speed.text = _fmt_speed(state.speed)
        kpi_distance.text = f"{_fmt_number(state.distance_km, 2)} km"
        mode_label.text = f"Mode: {state.mode.upper()}"
        workout_info.text = state.workout.name if state.workout else "No workout loaded"
        if state.workout:
            total = _fmt_duration(state.workout.total_duration_sec)
            course_info.text = f"{state.workout.name} | total {total} | mode {state.mode.upper()}"
        else:
            course_info.text = "No course loaded"
        shown_score = (
            goal_tracker.score + pinball_score_bonus if pinball_mode else goal_tracker.score
        )
        game_score_label.text = f"Score: {shown_score}"
        game_coins_label.text = f"Coins: {goal_tracker.coins}"
        game_streak_label.text = f"Streak: {goal_tracker.streak}"
        current_goal = goal_tracker.current_goal
        if current_goal is None:
            game_goal_label.text = "Goal: session clear"
            game_goal_progress_label.text = "--"
            if pinball_mode:
                pinball_mission_label.text = "MISSION: Clear workout"
        else:
            game_goal_label.text = f"Goal: {current_goal.definition.title}"
            game_goal_progress_label.text = (
                f"{int(current_goal.progress_sec)}/{int(current_goal.definition.target_sec)} s"
            )
            if pinball_mode:
                pinball_mission_label.text = (
                    "MISSION: "
                    f"{current_goal.definition.title} "
                    f"[{int(current_goal.progress_sec)}/{int(current_goal.definition.target_sec)}s]"
                )
        if pinball_mode:
            _safe_run_js("window.veloxDmd.init();")
            pinball_multiplier_label.text = f"MULTI x{pinball_multiplier}"
            pinball_jackpot_label.text = f"JACKPOT {pinball_jackpots}"
            pinball_reward_label.text = f"BONUS {pinball_last_bonus}"
            step_txt = "-"
            if state.progress is not None:
                step_txt = f"{state.progress.step_index}/{state.progress.step_total}"
            dmd_msg = (
                f"S{shown_score} Mx{pinball_multiplier} J{pinball_jackpots} "
                f"STEP {step_txt}"
            )
            _safe_run_js(f"window.veloxDmd.ticker('{dmd_msg}');")

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
            transition_status_label.text = _transition_status_text(state.progress)
        else:
            step_info.text = "Step: -"
            elapsed_label.text = "Elapsed: 00:00"
            remaining_label.text = "Remaining: 00:00"
            next_step_label.text = "Next: -"
            transition_status_label.text = "Transition: -"
            target_label.text = "Targets: -"
            guidance_label.text = "Action: -"
            guidance_label.style("color: #cbd5e1; font-weight: 600;")
            coaching_stabilizer.reset()
            last_coaching_alert_key = None
            last_encourage_bucket = None
            last_transition_marker = None
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
        in_zone_for_scene = power_in_zone is True and cadence_in_zone is True
        scene_action = "steady"

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
            if stable_signal.key in {"power_low", "cadence_low", "dual_pl_cl", "dual_ph_cl"}:
                scene_action = "up"
            elif stable_signal.key in {"power_high", "cadence_high", "dual_pl_ch", "dual_ph_ch"}:
                scene_action = "down"
            if (
                changed
                and sound_alerts
                and stable_signal.severity in {"warn", "bad"}
                and stable_signal.key != last_coaching_alert_key
            ):
                _safe_run_js(
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

            # Classic coaching cues near the rider:
            # - encouragement pulses
            if not pinball_mode:
                elapsed = int(state.progress.elapsed_total_sec)
                encourage_bucket = elapsed // 12
                if encourage_bucket != last_encourage_bucket:
                    last_encourage_bucket = encourage_bucket
                    if power_in_zone is True and cadence_in_zone is True:
                        _safe_run_js(
                            "window.veloxCoachCue("
                            "'coach', 'Excellent, garde ce rythme!', 1700);"
                        )
                    elif scene_action == "up":
                        _safe_run_js(
                            "window.veloxCoachCue("
                            "'coach', 'Allez, monte un peu!', 1700);"
                        )
                    elif scene_action == "down":
                        _safe_run_js(
                            "window.veloxCoachCue("
                            "'coach', 'Souple, reduis legerement', 1700);"
                        )
                    else:
                        _safe_run_js(
                            "window.veloxCoachCue("
                            "'coach', 'Stable, continue', 1600);"
                        )
        _safe_run_js(
            "window.veloxUpdateScene("
            f"{state.speed if state.speed is not None else 0},"
            f"{state.cadence if state.cadence is not None else 0},"
            f"{state.power if state.power is not None else 0},"
            f"{'true' if in_zone_for_scene else 'false'},"
            f"'{scene_action}'"
            ");"
        )

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
        ht_connect_btn.set_enabled((not state.connected) and (not state.ht_busy))
        ht_disconnect_btn.set_enabled(state.connected and (not state.ht_busy))
        ht_scan_btn.set_enabled(not state.ht_busy)
        hm_connect_btn.set_enabled(hm_detected and not state.hm_connected)
        hm_disconnect_btn.set_enabled(state.hm_connected)
        export_json_btn.set_enabled(current_snapshot_path is not None)
        export_csv_btn.set_enabled(current_snapshot_csv_path is not None)
        summary_export_json_btn.set_enabled(current_snapshot_path is not None)
        summary_export_csv_btn.set_enabled(current_snapshot_csv_path is not None)
        refresh_plan_chart()
        refresh_live_chart()

    async def on_scan_ht(auto_connect: bool = True) -> None:
        nonlocal devices, selected_device_address
        if state.ht_busy:
            return
        state.ht_busy = True
        ftms_devices: list[ScannedDevice] = []
        options: dict[str, str] = {}
        state.status = "Scanning HT..."
        refresh_ui()
        try:
            devices = await controller.scan()
            ftms_devices = _ht_candidates(devices)
            source = ftms_devices if ftms_devices else devices
            # NiceGUI select expects value->label mapping for dict options.
            options = {d.address: _fmt_device_label(d) for d in source}
            ht_device_select.options = options
            if ftms_devices:
                state.status = f"HT scan done: {len(ftms_devices)} trainer(s) found"
            elif devices:
                state.status = (
                    f"HT scan done: {len(devices)} BLE device(s), no FTMS trainer detected"
                )
            else:
                state.status = "HT scan done: no BLE devices"
            if options:
                if selected_device_address in options:
                    ht_device_select.value = selected_device_address
                elif len(ftms_devices) == 1:
                    selected_device_address = ftms_devices[0].address
                    ht_device_select.value = selected_device_address
                elif len(options) == 1:
                    selected_device_address = next(iter(options.keys()))
                    ht_device_select.value = selected_device_address
                else:
                    selected_device_address = None
                    ht_device_select.value = None
        except Exception as exc:
            state.status = f"HT scan failed: {exc}"
            selected_device_address = None
        state.ht_busy = False
        refresh_ui()
        if (
            auto_connect
            and not state.connected
            and selected_device_address is not None
            and (len(ftms_devices) == 1 or len(options) == 1)
        ):
            await on_connect_ht()

    async def on_connect_ht() -> None:
        nonlocal selected_device_address
        if state.connected or state.ht_busy:
            return
        selected_device_address = cast(str | None, ht_device_select.value)
        if not selected_device_address:
            state.status = "Select an HT device first"
            refresh_ui()
            return
        state.ht_busy = True
        state.erg_ready = None
        state.status = "Connecting HT..."
        refresh_ui()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                label = await asyncio.wait_for(
                    controller.connect(
                        target=selected_device_address,
                        metrics_callback=on_metrics,
                        connect_timeout=HT_CONNECT_TIMEOUT_SEC,
                    ),
                    timeout=HT_CONNECT_TIMEOUT_SEC + 5.0,
                )
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    state.status = (
                        "Connecting HT... retrying automatically "
                        "(refreshing BLE discovery)"
                    )
                    refresh_ui()
                    devices = await controller.scan()
                    ftms_devices = _ht_candidates(devices)
                    source = ftms_devices if ftms_devices else devices
                    options = {d.address: _fmt_device_label(d) for d in source}
                    ht_device_select.options = options
                    if options:
                        selected_device_address = (
                            selected_device_address
                            if selected_device_address in options
                            else next(iter(options.keys()))
                        )
                        ht_device_select.value = selected_device_address
                    await asyncio.sleep(0.2)
                    continue
            else:
                erg_ok = await asyncio.wait_for(
                    controller.probe_erg_support(),
                    timeout=8.0,
                )
                metrics_ok = controller.measurement_stream_ready
                state.connected = True
                state.ht_device_name = label
                state.erg_ready = erg_ok
                metrics_part = "metrics ready" if metrics_ok else "metrics pending"
                state.status = (
                    f"HT connected: {label} | ERG {'ready' if erg_ok else 'not ready'} | "
                    f"{metrics_part}"
                )
                state.ht_busy = False
                refresh_ui()
                return

        state.connected = False
        state.ht_device_name = None
        state.erg_ready = None
        if isinstance(last_error, TimeoutError):
            state.status = (
                f"HT connect timeout ({int(HT_CONNECT_TIMEOUT_SEC)}s x2). "
                "Trainer may need a short wake-up spin."
            )
        else:
            detail = str(last_error) if last_error else "Unknown error"
            if "No FTMS device found" in detail:
                detail = (
                    "No FTMS device found (trainer on, but BLE service not visible). "
                    "Auto-retry done; a short pedal wake-up may still be required "
                    "on some models."
                )
            state.status = f"HT connect failed: {detail}"
        state.ht_busy = False
        refresh_ui()

    async def on_disconnect_ht() -> None:
        if state.ht_busy:
            return
        state.ht_busy = True
        await controller.disconnect()
        state.connected = False
        state.ht_device_name = None
        state.erg_ready = None
        state.ht_busy = False
        state.status = "HT disconnected"
        refresh_ui()

    def on_detect_hm() -> None:
        nonlocal hm_detected
        hm_detected = bool(hm_sim_switch.value)
        if hm_detected:
            state.status = "HM detected (simulated)"
        else:
            state.hm_connected = False
            state.heart_rate_bpm = None
            state.hm_device_name = None
            state.status = "HM not detected"
        refresh_ui()

    def on_connect_hm() -> None:
        if not hm_detected:
            state.status = "Detect HM first"
            refresh_ui()
            return
        state.hm_connected = True
        state.hm_device_name = "Sim HM"
        state.status = "HM connected"
        refresh_ui()

    def on_disconnect_hm() -> None:
        state.hm_connected = False
        state.heart_rate_bpm = None
        state.hm_device_name = None
        state.status = "HM disconnected"
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
        nonlocal last_goal_tick_ts
        nonlocal pinball_score_bonus, pinball_multiplier, pinball_jackpots
        nonlocal pinball_last_bonus, pinball_last_step_seen, pinball_last_jackpot_ts
        nonlocal hm_sim_seed
        now = asyncio.get_event_loop().time()
        if state.last_ts is not None and metrics.instantaneous_speed_kmh is not None:
            state.distance_km += (
                metrics.instantaneous_speed_kmh * (now - state.last_ts)
            ) / 3600.0
        state.last_ts = now
        state.power = metrics.instantaneous_power
        state.cadence = metrics.instantaneous_cadence
        state.speed = metrics.instantaneous_speed_kmh
        hm_simulate = bool(hm_sim_switch.value)
        if state.hm_connected and hm_simulate:
            base = 88.0
            if state.power is not None:
                base += state.power * 0.20
            if state.cadence is not None:
                base += max(0.0, state.cadence - 70.0) * 0.35
            phase = (state.progress.elapsed_total_sec if state.progress else now) / 9.0
            wobble = 3.0 * math.sin(phase)
            target_hr = max(78.0, min(196.0, base + wobble))
            hm_sim_seed = (hm_sim_seed * 0.82) + (target_hr * 0.18)
            state.heart_rate_bpm = int(round(hm_sim_seed))
        else:
            state.heart_rate_bpm = None
        metric_samples.append(
            (
                metrics.instantaneous_power,
                metrics.instantaneous_cadence,
                metrics.instantaneous_speed_kmh,
            )
        )

        if state.progress:
            power_zone_ok: bool | None = None
            cadence_zone_ok: bool | None = None
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
                    power_zone_ok = True
                else:
                    power_zone_ok = False

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
                    cadence_zone_ok = True
                else:
                    cadence_zone_ok = False

            if timeline_labels:
                idx = min(
                    len(timeline_labels) - 1,
                    max(0, int(state.progress.elapsed_total_sec / TIMELINE_SAMPLE_SEC)),
                )
                if metrics.instantaneous_power is not None:
                    timeline_actual_power[idx] = metrics.instantaneous_power
                if metrics.instantaneous_cadence is not None:
                    timeline_actual_cadence[idx] = metrics.instantaneous_cadence
            dt_goal = 0.8
            if last_goal_tick_ts is not None:
                dt_goal = max(0.1, min(2.0, now - last_goal_tick_ts))
            last_goal_tick_ts = now
            goal_tracker.update(
                power_in_zone=power_zone_ok,
                cadence_in_zone=cadence_zone_ok,
                dt_sec=dt_goal,
            )
            if pinball_mode:
                if pinball_last_step_seen == 0:
                    pinball_last_step_seen = state.progress.step_index
                elif state.progress.step_index != pinball_last_step_seen:
                    in_zone = power_zone_ok is not False and cadence_zone_ok is not False
                    if in_zone:
                        trigger_pinball_event("multi")
                        trigger_pinball_pattern("step_clear")
                    else:
                        pinball_last_bonus = "COMBO BREAK"
                        pinball_multiplier = 1
                    pinball_last_step_seen = state.progress.step_index

                expected_hi = state.progress.expected_power_max_watts
                if (
                    expected_hi is not None
                    and state.ftp_watts > 0
                    and expected_hi >= int(state.ftp_watts * 0.95)
                    and power_zone_ok is True
                    and cadence_zone_ok is not False
                ):
                    trigger_pinball_event("jackpot")
                    trigger_pinball_pattern("jackpot_rush")

    def on_progress(progress: WorkoutProgress) -> None:
        nonlocal last_transition_marker
        state.progress = progress
        if progress.transition_countdown_sec is None:
            last_transition_marker = None
            return
        marker = (
            progress.step_index,
            progress.transition_countdown_sec,
            progress.transition_label or progress.step_label,
        )
        if marker == last_transition_marker:
            return
        last_transition_marker = marker
        cue_text = _phase_cue_text(progress)
        cue_kind = _phase_cue_kind(progress)
        cue_duration = 900 if progress.transition_countdown_sec > 0 else 1400
        _safe_run_js(
            "window.veloxCoachCue("
            f"'{cue_kind}', '{cue_text}', {cue_duration}"
            ");"
        )

    def on_finish(completed: bool) -> None:
        nonlocal last_transition_marker
        ended_workout_name = state.workout.name if state.workout is not None else "-"
        elapsed_sec = state.progress.elapsed_total_sec if state.progress is not None else 0
        both_pct = _compute_both_compliance_pct()
        avg_power, avg_cadence, avg_speed = _compute_session_averages()
        _save_session_snapshot(completed)
        state.progress = None
        last_transition_marker = None
        state.status = "Workout completed" if completed else "Workout stopped"
        summary_status_label.text = (
            "Status: Completed" if completed else "Status: Stopped"
        )
        summary_workout_label.text = f"Workout: {ended_workout_name}"
        summary_mode_label.text = f"Mode: {state.mode.upper()} | FTP {state.ftp_watts}"
        summary_elapsed_label.text = f"Elapsed: {_fmt_duration(elapsed_sec)}"
        summary_distance_label.text = f"Distance: {_fmt_number(state.distance_km, 2)} km"
        summary_power_label.text = (
            f"Avg power: {avg_power:.0f} W" if avg_power is not None else "Avg power: -"
        )
        summary_cadence_label.text = (
            f"Avg cadence: {avg_cadence:.1f} rpm"
            if avg_cadence is not None
            else "Avg cadence: -"
        )
        summary_speed_label.text = (
            f"Avg speed: {avg_speed:.1f} km/h" if avg_speed is not None else "Avg speed: -"
        )
        summary_both_label.text = (
            f"Both compliance: {both_pct:.0f}%"
            if both_pct is not None
            else "Both compliance: -"
        )
        refresh_history()
        show_summary_screen()
        refresh_ui()

    async def on_start() -> None:
        nonlocal session_started_at_utc
        nonlocal current_snapshot_path
        nonlocal current_snapshot_csv_path
        nonlocal last_goal_tick_ts
        nonlocal pinball_score_bonus, pinball_multiplier, pinball_jackpots
        nonlocal pinball_last_bonus, pinball_last_step_seen, pinball_last_jackpot_ts
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
        goal_tracker.reset()
        pinball_score_bonus = 0
        pinball_multiplier = 1
        pinball_jackpots = 0
        pinball_last_bonus = "READY"
        pinball_last_step_seen = 0
        pinball_last_jackpot_ts = 0.0
        last_goal_tick_ts = None
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

    async def on_open_connections() -> None:
        show_connections_screen()
        refresh_ui()
        if not state.connected:
            await on_scan_ht(auto_connect=True)

    def on_back_to_training_setup() -> None:
        show_setup_screen()
        refresh_ui()

    def on_ftp_or_mode_change() -> None:
        load_selected_workout()
        refresh_ui()

    def on_sound_toggle() -> None:
        nonlocal sound_alerts
        sound_alerts = bool(sound_toggle.value)

    def on_analytics_demo_toggle() -> None:
        nonlocal analytics_demo_mode
        analytics_demo_mode = bool(analytics_demo_switch.value)
        refresh_history()
        refresh_ui()

    def on_analytics_window_change() -> None:
        nonlocal analytics_window
        analytics_window = int(analytics_window_select.value or 7)
        refresh_history()

    band_select.on_value_change(lambda _: refresh_templates())
    ftp_input.on_value_change(lambda _: on_ftp_or_mode_change())
    mode_select.on_value_change(lambda _: on_ftp_or_mode_change())
    sound_toggle.on_value_change(lambda _: on_sound_toggle())
    analytics_demo_switch.on_value_change(lambda _: on_analytics_demo_toggle())
    analytics_window_select.on_value_change(lambda _: on_analytics_window_change())
    open_connections_btn.on_click(on_open_connections)
    back_to_training_btn.on_click(on_back_to_training_setup)
    ht_scan_btn.on_click(on_scan_ht)
    ht_connect_btn.on_click(on_connect_ht)
    ht_disconnect_btn.on_click(on_disconnect_ht)
    hm_detect_btn.on_click(on_detect_hm)
    hm_connect_btn.on_click(on_connect_hm)
    hm_disconnect_btn.on_click(on_disconnect_hm)
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
    summary_export_json_btn.on_click(on_export_json)
    summary_export_csv_btn.on_click(on_export_csv)
    summary_back_btn.on_click(on_back_to_setup)
    if pinball_mode and simulate_ht:
        sim_multi_btn.on_click(lambda: trigger_pinball_event("multi", manual=True))
        sim_jackpot_btn.on_click(lambda: trigger_pinball_event("jackpot", manual=True))
        sim_bonus_btn.on_click(lambda: trigger_pinball_event("bonus", manual=True))
        sim_chain_btn.on_click(lambda: trigger_pinball_pattern("jackpot_rush"))

    refresh_templates()
    refresh_history()
    apply_layout_mode()
    show_setup_screen()
    ui.timer(0.5, refresh_ui)
    ui.run(host=host, port=port, reload=False, title="Velox Engine Web UI")
    return 0
