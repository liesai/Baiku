"""NiceGUI web UI for Velox Engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
ASSETS_ROUTE = "/velox-assets"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
SPRITE_URL = f"{ASSETS_ROUTE}/cyclist_sprite_aligned.png"
SCENE_BG_URL = f"{ASSETS_ROUTE}/forest_bg.png"
DMD_CYCLIST_URL = f"{ASSETS_ROUTE}/dmd_cyclist_bonus.png"
_ASSETS_MOUNTED = False


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

    controller = UIController(debug_ftms=False, simulate_ht=simulate_ht)
    state = WebState()
    pinball_mode = ui_theme == "pinball"
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
            height: 88px;
            border-radius: 8px;
            background: #090303;
            display: block;
            image-rendering: pixelated;
          }
          .gb-pixel {
            font-family: "Courier New", monospace;
            font-weight: 700;
            letter-spacing: 0.02em;
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
          .ve-scene {
            display: block;
            position: relative;
            width: 100%;
            min-width: 100%;
            height: 144px;
            border: 1px solid rgba(56, 189, 248, 0.35);
            border-radius: 12px;
            overflow: hidden;
            background: linear-gradient(180deg, #0a2a4e 0%, #15426d 58%, #0f2f52 100%);
            --ve-bg-offset: 0px;
            --ve-pedal-rot: 0deg;
            --ve-rider-bob: 0px;
            --ve-sprite-shift-x: 0px;
          }
          .ve-scene[data-zone="ok"] { box-shadow: inset 0 0 0 2px rgba(34,197,94,.25); }
          .ve-scene[data-zone="bad"] { box-shadow: inset 0 0 0 2px rgba(239,68,68,.25); }
          .ve-scene[data-action="up"] .ve-hud-action { color: #f59e0b; }
          .ve-scene[data-action="down"] .ve-hud-action { color: #ef4444; }
          .ve-scene[data-action="steady"] .ve-hud-action { color: #22c55e; }
          .ve-bg {
            position: absolute;
            left: 0;
            top: 0;
            right: 0;
            bottom: 0;
            background-image: url('__SCENE_BG_URL__');
            background-repeat: repeat-x;
            background-size: auto 100%;
            image-rendering: pixelated;
          }
          .ve-bg-main {
            opacity: 1;
            background-position-x: var(--ve-bg-offset);
            filter: saturate(1.05) contrast(1.02);
          }
          .ve-rider {
            position: absolute;
            left: 74px;
            bottom: 14px;
            width: 112px;
            height: 74px;
            transform: translateY(var(--ve-rider-bob));
            z-index: 5;
          }
          .ve-sprite {
            position: absolute;
            inset: 0;
            image-rendering: pixelated;
            background-image: url('__SPRITE_URL__');
            background-repeat: no-repeat;
            background-size: 300% 100%;
            background-position: 0% 0;
            transform: translateX(var(--ve-sprite-shift-x));
            filter: drop-shadow(0 2px 2px rgba(2, 6, 23, 0.45));
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
          window.veloxUpdateScene = function(speed, cadence, inZone, action) {
            const scene = document.getElementById('ve-scene');
            if (!scene) return;
            const speedNode = document.getElementById('ve-scene-speed');
            const actionNode = document.getElementById('ve-scene-action');
            const spriteNode = document.getElementById('ve-sprite');
            const state = window.__velox_scene_state || {
              bg: 0,
              pedal: 0,
              bobTick: 0,
              frameTick: 0,
            };
            const s = Math.max(0, Number(speed || 0));
            const c = Math.max(0, Number(cadence || 0));
            state.bg = (state.bg - Math.max(0.15, s * 0.25)) % 900;
            state.pedal = (state.pedal + (c * 0.92)) % 360;
            state.bobTick += 0.35;
            state.frameTick += Math.max(0.25, c / 65);
            const bob = Math.sin(state.bobTick + c / 20) * Math.min(2.5, 0.6 + c / 65);
            scene.style.setProperty('--ve-bg-offset', `${state.bg}px`);
            scene.style.setProperty('--ve-pedal-rot', `${state.pedal}deg`);
            scene.style.setProperty('--ve-rider-bob', `${bob}px`);
            scene.dataset.zone = inZone ? 'ok' : 'bad';
            scene.dataset.action = action || 'steady';
            if (spriteNode) {
              const frame = Math.floor(state.frameTick) % 3;
              const x = frame * 50;
              const shiftByFrame = [0, 0, 0];
              spriteNode.style.backgroundPosition = `${x}% 0%`;
              scene.style.setProperty('--ve-sprite-shift-x', `${shiftByFrame[frame]}px`);
            }
            if (speedNode) speedNode.textContent = `${s.toFixed(1)} km/h`;
            if (actionNode) {
              if (action === 'up') actionNode.textContent = 'PUSH';
              else if (action === 'down') actionNode.textContent = 'EASE';
              else actionNode.textContent = 'HOLD';
            }
            window.__velox_scene_state = state;
          };
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
            off.width = 192;
            off.height = 64;
            const octx = off.getContext('2d');

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
                  const lit = frame[idx] > 30 || frame[idx + 3] > 30;
                  ctx.fillStyle = lit ? glow : base;
                  ctx.beginPath();
                  ctx.arc(px, py, Math.min(cw, ch) * (lit ? 0.34 : 0.23), 0, Math.PI * 2);
                  ctx.fill();
                  if (lit) {
                    ctx.fillStyle = hot;
                    ctx.beginPath();
                    ctx.arc(px, py, Math.min(cw, ch) * 0.14, 0, Math.PI * 2);
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
              octx.fillText(msg || 'VELOX READY', 96, 22);
              octx.font = 'bold 12px monospace';
              octx.fillText('TRACK  •  COMPETE  •  WIN', 96, 46);
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
        </script>
        """
        .replace("__SPRITE_URL__", SPRITE_URL)
        .replace("__SCENE_BG_URL__", SCENE_BG_URL)
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
    strict_mode = False
    viewport_preset = "auto"
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

        plan_chart = None
        if pinball_mode:
            with ui.card().classes("w-full gb-card gb-compact"):
                ui.label("Pinball mode active: CSP-safe display").classes(
                    "text-sm gb-title-neutral"
                )
                ui.label(
                    "Workout preview chart disabled in pinball mode to avoid CSP unsafe-eval."
                ).classes("text-xs gb-muted")
        else:
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

    with ui.column().classes("w-full gap-2") as workout_view:
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("Workout Session").classes("text-lg gb-title-neutral")
            with ui.row().classes("gap-2"):
                back_btn = ui.button("Back to setup").props("outline color=white")
                stop_btn = ui.button("Stop session").props("color=negative")
                stop_btn.disable()

        with ui.row().classes("w-full gap-2") as pinball_hud_row:
            ui.label("MODE: PINBALL").classes("pinball-chip")
            pinball_mission_label = ui.label("MISSION: Keep target zone").classes("pinball-chip")
            pinball_multiplier_label = ui.label("MULTI x1").classes("pinball-chip")
            pinball_jackpot_label = ui.label("JACKPOT 0").classes("pinball-chip pinball-jackpot")
            pinball_reward_label = ui.label("BONUS READY").classes("pinball-chip")
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
            with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                game_score_label = ui.label("Score: 0").classes("text-sm gb-pixel")
                game_coins_label = ui.label("Coins: 0").classes("text-sm gb-pixel")
                game_streak_label = ui.label("Streak: 0").classes("text-sm gb-pixel")
                game_goal_label = ui.label("Goal: -").classes("text-xs gb-pixel")
                game_goal_progress_label = ui.label("0/0 s").classes("text-xs gb-pixel")
            ui.html(
                """
                <div id="ve-scene" class="ve-scene" data-zone="ok">
                  <div class="ve-bg ve-bg-main"></div>
                  <div id="ve-fx" class="ve-fx">BONUS!</div>
                  <div class="ve-hud">
                    <span id="ve-scene-action" class="ve-hud-action">HOLD</span>
                    <span id="ve-scene-speed" class="ve-hud-speed">0,0 km/h</span>
                  </div>
                  <div class="ve-rider">
                    <div id="ve-sprite" class="ve-sprite"></div>
                  </div>
                </div>
                """
            ).classes("w-full")

        live_chart = None
        with ui.card().classes("w-full gb-card gb-compact"):
            ui.label("Live chart disabled (CSP-safe mode)").classes("text-sm gb-muted")

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
        theme_cls = "gb-theme-pinball" if pinball_mode else "gb-theme-classic"
        if core.loop is None:
            # NiceGUI loop/client not ready yet during initial startup.
            return
        _safe_run_js(
            "document.body.classList.remove("
            "'gb-layout-auto','gb-layout-1080','gb-layout-1440',"
            "'gb-theme-classic','gb-theme-pinball'"
            ");"
            f"document.body.classList.add('{cls}','{theme_cls}');"
        )

    def _safe_run_js(code: str) -> None:
        """Best-effort JS execution; skip when timer/background has no slot/client context."""
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
        workout_view.set_visibility(False)

    def show_workout_screen() -> None:
        setup_header.set_visibility(False)
        setup_view.set_visibility(False)
        workout_view.set_visibility(True)
        if pinball_mode:
            _safe_run_js("window.veloxDmd.init();")

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
        _safe_run_js(
            "window.veloxUpdateScene("
            f"{state.speed if state.speed is not None else 0},"
            f"{state.cadence if state.cadence is not None else 0},"
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
        nonlocal last_goal_tick_ts
        nonlocal pinball_score_bonus, pinball_multiplier, pinball_jackpots
        nonlocal pinball_last_bonus, pinball_last_step_seen, pinball_last_jackpot_ts
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
        state.progress = progress

    def on_finish(completed: bool) -> None:
        _save_session_snapshot(completed)
        state.progress = None
        state.status = "Workout completed" if completed else "Workout stopped"
        refresh_history()
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
