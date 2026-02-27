"""Tkinter Linux app for FTMS trainer control and workout execution."""

from __future__ import annotations

import asyncio
import threading
import time
import tkinter as tk
from concurrent.futures import Future
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, cast

from backend.ble.ftms_client import IndoorBikeData, ScannedDevice
from backend.ui.controller import UIController
from backend.workout.library import build_plan_from_template, list_templates
from backend.workout.model import WorkoutPlan
from backend.workout.parser import WorkoutParseError, load_workout
from backend.workout.runner import TargetMode, WorkoutProgress
from backend.workout.session_store import (
    SessionRecord,
    append_session,
    load_recent_sessions,
    now_utc_iso,
)


class AsyncBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def shutdown(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2.0)


class VeloxUI:
    def __init__(
        self,
        root: tk.Tk,
        simulate_ht: bool = False,
        ble_pair: bool = True,
    ) -> None:
        self.root = root
        self.root.title("Velox Engine")
        self.root.geometry("1240x860")

        self.bridge = AsyncBridge()
        self.controller = UIController(
            debug_ftms=False,
            simulate_ht=simulate_ht,
            ble_pair=ble_pair,
        )
        self.simulate_ht = simulate_ht

        self.devices: list[ScannedDevice] = []
        self.connected = False
        self.workout: WorkoutPlan | None = None
        self.current_progress: WorkoutProgress | None = None

        self.current_power_w: int | None = None
        self.current_cadence_rpm: float | None = None
        self.current_speed_kmh: float | None = None
        self.distance_km = 0.0
        self._last_metric_ts: float | None = None
        self._estimated_total_distance_km = 0.0
        self._session_started_utc: str | None = None
        self._session_mode: TargetMode = "erg"
        self._session_ftp_watts = 220
        self._metric_sample_count = 0
        self._sum_power_watts = 0.0
        self._sum_cadence_rpm = 0.0
        self._sum_speed_kmh = 0.0
        self._zone_power_hits = 0
        self._zone_power_total = 0
        self._zone_rpm_hits = 0
        self._zone_rpm_total = 0
        self._zone_both_hits = 0
        self._zone_both_total = 0

        self._template_values = [
            f"{template.category} - {template.name} [{template.key}]"
            for template in list_templates()
        ]
        self._template_by_label = {
            f"{template.category} - {template.name} [{template.key}]": template.key
            for template in list_templates()
        }
        self._duration_band_values = [
            "All",
            "<=30 min",
            "30-45 min",
            "45-60 min",
            ">=60 min",
        ]

        self._build_widgets()
        self._bind_responsive_redraw()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_widgets(self) -> None:
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill=tk.X)

        ttk.Button(top, text="Scan", command=self.on_scan).pack(side=tk.LEFT)
        self.connect_btn = ttk.Button(top, text="Connect", command=self.on_connect)
        self.connect_btn.pack(side=tk.LEFT, padx=8)
        self.disconnect_btn = ttk.Button(
            top,
            text="Disconnect",
            command=self.on_disconnect,
            state=tk.DISABLED,
        )
        self.disconnect_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=14)
        if self.simulate_ht:
            ttk.Label(top, text="SIM MODE", foreground="#f97316").pack(side=tk.LEFT)

        metrics = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        metrics.pack(fill=tk.X)
        self.power_var = tk.StringVar(value="Power: N/A")
        self.cadence_var = tk.StringVar(value="Cadence: N/A")
        self.speed_var = tk.StringVar(value="Speed: N/A")
        self.distance_var = tk.StringVar(value="Distance: 0.00 km")
        ttk.Label(metrics, textvariable=self.power_var, font=("DejaVu Sans", 12, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Label(
            metrics,
            textvariable=self.cadence_var,
            font=("DejaVu Sans", 12, "bold"),
        ).pack(side=tk.LEFT, padx=20)
        ttk.Label(metrics, textvariable=self.speed_var, font=("DejaVu Sans", 12, "bold")).pack(
            side=tk.LEFT, padx=20
        )
        ttk.Label(
            metrics,
            textvariable=self.distance_var,
            font=("DejaVu Sans", 12, "bold"),
        ).pack(side=tk.LEFT, padx=20)

        self.zone_var = tk.StringVar(value="Zones: -")
        ttk.Label(self.root, textvariable=self.zone_var, padding=(12, 0, 12, 4)).pack(
            fill=tk.X
        )
        self.compliance_var = tk.StringVar(value="Zone compliance: -")
        ttk.Label(self.root, textvariable=self.compliance_var, padding=(12, 0, 12, 4)).pack(
            fill=tk.X
        )

        gauges = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        gauges.pack(fill=tk.BOTH)
        gauges.columnconfigure(0, weight=1, uniform="gauge")
        gauges.columnconfigure(1, weight=1, uniform="gauge")
        gauges.rowconfigure(0, weight=1)
        gauges.rowconfigure(1, weight=1)
        self.gauge_speed = self._new_gauge_canvas(gauges, row=0, col=0)
        self.gauge_power = self._new_gauge_canvas(gauges, row=0, col=1)
        self.gauge_rpm = self._new_gauge_canvas(gauges, row=1, col=0)
        self.gauge_distance = self._new_gauge_canvas(gauges, row=1, col=1)

        center = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        center.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        left = ttk.Frame(center)
        center.add(left, weight=1)
        right = ttk.Frame(center)
        center.add(right, weight=2)

        ttk.Label(left, text="FTMS devices").pack(anchor=tk.W)
        self.device_list = tk.Listbox(left, height=12)
        self.device_list.pack(fill=tk.BOTH, expand=True)
        ttk.Label(left, text="Recent sessions").pack(anchor=tk.W, pady=(10, 0))
        self.history_list = tk.Listbox(left, height=8)
        self.history_list.pack(fill=tk.BOTH, expand=True)
        self._refresh_history_list()

        preset = ttk.LabelFrame(right, text="Prebuilt workouts", padding=8)
        preset.pack(fill=tk.X)
        ttk.Label(preset, text="Duration").grid(row=0, column=0, sticky=tk.W)
        self.duration_band_var = tk.StringVar(value=self._duration_band_values[0])
        self.duration_band_combo = ttk.Combobox(
            preset,
            textvariable=self.duration_band_var,
            values=self._duration_band_values,
            state="readonly",
            width=12,
        )
        self.duration_band_combo.grid(row=0, column=1, padx=8, sticky=tk.W)
        self.duration_band_combo.bind("<<ComboboxSelected>>", self.on_duration_filter_changed)

        ttk.Label(preset, text="Template").grid(row=1, column=0, sticky=tk.W)
        self.template_var = tk.StringVar(value=self._template_values[0])
        self.template_combo = ttk.Combobox(
            preset,
            textvariable=self.template_var,
            values=self._template_values,
            state="readonly",
            width=52,
        )
        self.template_combo.grid(row=1, column=1, padx=8, sticky=tk.W)
        self._apply_duration_filter()

        ttk.Label(preset, text="FTP (W)").grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        self.ftp_var = tk.StringVar(value="220")
        ttk.Entry(preset, textvariable=self.ftp_var, width=10).grid(
            row=2,
            column=1,
            sticky=tk.W,
            pady=(6, 0),
        )

        ttk.Label(preset, text="Mode").grid(row=3, column=0, sticky=tk.W, pady=(6, 0))
        self.mode_var = tk.StringVar(value="erg")
        self.mode_combo = ttk.Combobox(
            preset,
            textvariable=self.mode_var,
            values=["erg", "resistance", "slope"],
            state="readonly",
            width=12,
        )
        self.mode_combo.grid(row=3, column=1, sticky=tk.W, pady=(6, 0))

        ttk.Button(preset, text="Load preset", command=self.on_load_preset).grid(
            row=3,
            column=1,
            sticky=tk.E,
            pady=(6, 0),
        )

        workout_bar = ttk.Frame(right)
        workout_bar.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(
            workout_bar,
            text="Load workout file",
            command=self.on_load_workout,
        ).pack(side=tk.LEFT)
        self.start_btn = ttk.Button(
            workout_bar,
            text="Start",
            command=self.on_start_workout,
            state=tk.DISABLED,
        )
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.stop_btn = ttk.Button(
            workout_bar,
            text="Stop",
            command=self.on_stop_workout,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT)

        self.workout_var = tk.StringVar(value="No workout loaded")
        ttk.Label(right, textvariable=self.workout_var).pack(anchor=tk.W, pady=(8, 0))

        self.step_var = tk.StringVar(value="-")
        self.target_var = tk.StringVar(value="Target: -")
        self.rpm_objective_var = tk.StringVar(value="RPM objective: -")
        self.step_timer_var = tk.StringVar(value="Step timer: -")
        self.session_timer_var = tk.StringVar(value="Session timer: -")
        ttk.Label(right, textvariable=self.step_var).pack(anchor=tk.W)
        ttk.Label(right, textvariable=self.target_var).pack(anchor=tk.W)
        ttk.Label(right, textvariable=self.rpm_objective_var).pack(anchor=tk.W)
        ttk.Label(right, textvariable=self.step_timer_var).pack(anchor=tk.W)
        ttk.Label(right, textvariable=self.session_timer_var).pack(anchor=tk.W)

        self.progress = ttk.Progressbar(right, orient=tk.HORIZONTAL, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(6, 8))

        self.chart_canvas = tk.Canvas(
            right,
            height=220,
            bg="#0f172a",
            highlightthickness=1,
            highlightbackground="#334155",
        )
        self.chart_canvas.pack(fill=tk.X)

        ttk.Label(right, text="Workout steps").pack(anchor=tk.W, pady=(10, 0))
        self.steps_list = tk.Listbox(right)
        self.steps_list.pack(fill=tk.BOTH, expand=True)

    def _new_gauge_canvas(self, parent: ttk.Frame, *, row: int, col: int) -> tk.Canvas:
        canvas = tk.Canvas(
            parent,
            width=320,
            height=170,
            bg="#0b1220",
            highlightthickness=1,
            highlightbackground="#1f2937",
        )
        canvas.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
        return canvas

    def _bind_responsive_redraw(self) -> None:
        self.chart_canvas.bind("<Configure>", self._on_canvas_resize)
        self.gauge_speed.bind("<Configure>", self._on_canvas_resize)
        self.gauge_power.bind("<Configure>", self._on_canvas_resize)
        self.gauge_rpm.bind("<Configure>", self._on_canvas_resize)
        self.gauge_distance.bind("<Configure>", self._on_canvas_resize)

    def _on_canvas_resize(self, _event: tk.Event[tk.Misc]) -> None:
        if self.workout is not None:
            active_step = None
            elapsed_total = 0
            if self.current_progress is not None:
                active_step = self.current_progress.step_index
                elapsed_total = self.current_progress.elapsed_total_sec
            self._draw_workout_curve(
                self.workout,
                active_step_index=active_step,
                elapsed_total_sec=elapsed_total,
            )
        self._refresh_gauges()

    def _call_ui(self, fn: Callable[[], None]) -> None:
        self.root.after(0, fn)

    def _format_time(self, seconds: int) -> str:
        minutes, sec = divmod(max(0, seconds), 60)
        return f"{minutes:02d}:{sec:02d}"

    def _avg(self, total: float, count: int) -> float | None:
        if count <= 0:
            return None
        return total / count

    def _template_duration_sec(self, template_label: str) -> int:
        key = self._template_by_label[template_label]
        template = next(item for item in list_templates() if item.key == key)
        return sum(step.duration_sec for step in template.steps)

    def _filter_match(self, duration_sec: int, band: str) -> bool:
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

    def on_duration_filter_changed(self, _event: tk.Event[tk.Misc]) -> None:
        self._apply_duration_filter()

    def _apply_duration_filter(self) -> None:
        band = self.duration_band_var.get() if hasattr(self, "duration_band_var") else "All"
        filtered = [
            label
            for label in self._template_values
            if self._filter_match(self._template_duration_sec(label), band)
        ]
        if not filtered:
            filtered = self._template_values[:]
        self.template_combo.configure(values=filtered)
        if self.template_var.get() not in filtered:
            self.template_var.set(filtered[0])

    def _refresh_history_list(self) -> None:
        if not hasattr(self, "history_list"):
            return
        self.history_list.delete(0, tk.END)
        sessions = load_recent_sessions(limit=15)
        if not sessions:
            self.history_list.insert(tk.END, "No saved sessions yet")
            return
        for item in sessions:
            date = item.ended_at_utc.split("T")[0]
            status = "OK" if item.completed else "STOP"
            mins = item.elapsed_duration_sec // 60
            self.history_list.insert(
                tk.END,
                f"{date} | {status} | {item.workout_name} | {mins}min",
            )

    def _zone_color(self, value: float | None, lo: float | None, hi: float | None) -> str:
        if value is None:
            return "#475569"
        if lo is None or hi is None:
            return "#3b82f6"
        if lo <= value <= hi:
            return "#22c55e"
        span = max(1.0, hi - lo)
        soft = span * 0.25
        if (lo - soft) <= value <= (hi + soft):
            return "#f59e0b"
        return "#ef4444"

    def _draw_gauge(
        self,
        canvas: tk.Canvas,
        *,
        title: str,
        value: float | None,
        minimum: float,
        maximum: float,
        unit: str,
        color: str,
        expected_lo: float | None = None,
        expected_hi: float | None = None,
    ) -> None:
        canvas.update_idletasks()
        w = max(180, int(canvas.winfo_width()))
        h = max(140, int(canvas.winfo_height()))
        canvas.delete("all")
        canvas.create_rectangle(0, 0, w, h, fill="#0b1220", outline="")

        cx = w // 2
        cy = int(h * 0.75)
        radius = min(w // 2 - 18, h - 34)
        radius = max(40, radius)
        box = (cx - radius, cy - radius, cx + radius, cy + radius)

        start = 135
        span = 270
        canvas.create_arc(
            box,
            start=start,
            extent=span,
            style=tk.ARC,
            width=16,
            outline="#1f2937",
        )

        if expected_lo is not None and expected_hi is not None:
            norm_lo = max(0.0, min(1.0, (expected_lo - minimum) / (maximum - minimum)))
            norm_hi = max(0.0, min(1.0, (expected_hi - minimum) / (maximum - minimum)))
            if norm_hi > norm_lo:
                canvas.create_arc(
                    box,
                    start=start + (1.0 - norm_hi) * span,
                    extent=(norm_hi - norm_lo) * span,
                    style=tk.ARC,
                    width=16,
                    outline="#14532d",
                )

        if value is not None:
            clamped = max(minimum, min(maximum, value))
            norm = (clamped - minimum) / (maximum - minimum)
            canvas.create_arc(
                box,
                start=start + (1.0 - norm) * span,
                extent=norm * span,
                style=tk.ARC,
                width=16,
                outline=color,
            )

        title_size = max(9, min(12, int(radius * 0.16)))
        value_size = max(12, min(20, int(radius * 0.28)))
        sub_size = max(8, min(10, int(radius * 0.12)))
        canvas.create_text(
            cx,
            20,
            text=title,
            fill="#cbd5e1",
            font=("DejaVu Sans", title_size, "bold"),
        )
        if value is None:
            value_text = "--"
        else:
            value_text = f"{value:.1f}{unit}"
        canvas.create_text(
            cx,
            cy - 14,
            text=value_text,
            fill="#e2e8f0",
            font=("DejaVu Sans", value_size, "bold"),
        )
        canvas.create_text(
            cx,
            cy + 10,
            text=f"{minimum:.0f}..{maximum:.0f}{unit}",
            fill="#64748b",
            font=("DejaVu Sans", sub_size),
        )

    def _refresh_gauges(self) -> None:
        p_lo = p_hi = c_lo = c_hi = None
        elapsed_ratio = None
        if self.current_progress is not None:
            p_lo = self.current_progress.expected_power_min_watts
            p_hi = self.current_progress.expected_power_max_watts
            c_lo = self.current_progress.expected_cadence_min_rpm
            c_hi = self.current_progress.expected_cadence_max_rpm
            if self.current_progress.total_duration_sec > 0:
                elapsed_ratio = (
                    self.current_progress.elapsed_total_sec
                    / self.current_progress.total_duration_sec
                )

        speed_expected = None
        if self.current_progress is not None:
            speed_expected = max(8.0, min(60.0, 16.0 + (self.current_progress.target_watts / 12.0)))

        power_color = self._zone_color(
            None if self.current_power_w is None else float(self.current_power_w),
            None if p_lo is None else float(p_lo),
            None if p_hi is None else float(p_hi),
        )
        rpm_color = self._zone_color(
            self.current_cadence_rpm,
            None if c_lo is None else float(c_lo),
            None if c_hi is None else float(c_hi),
        )
        speed_color = self._zone_color(
            self.current_speed_kmh,
            None if speed_expected is None else speed_expected - 4.0,
            None if speed_expected is None else speed_expected + 4.0,
        )

        distance_expected = None
        if elapsed_ratio is not None and self._estimated_total_distance_km > 0:
            distance_expected = self._estimated_total_distance_km * elapsed_ratio
        distance_color = self._zone_color(
            self.distance_km,
            None if distance_expected is None else distance_expected * 0.8,
            None if distance_expected is None else distance_expected * 1.2,
        )

        self._draw_gauge(
            self.gauge_speed,
            title="Speed",
            value=self.current_speed_kmh,
            minimum=0.0,
            maximum=70.0,
            unit="km/h",
            color=speed_color,
            expected_lo=None if speed_expected is None else speed_expected - 4.0,
            expected_hi=None if speed_expected is None else speed_expected + 4.0,
        )
        self._draw_gauge(
            self.gauge_power,
            title="Power",
            value=None if self.current_power_w is None else float(self.current_power_w),
            minimum=0.0,
            maximum=700.0,
            unit="W",
            color=power_color,
            expected_lo=None if p_lo is None else float(p_lo),
            expected_hi=None if p_hi is None else float(p_hi),
        )
        self._draw_gauge(
            self.gauge_rpm,
            title="Cadence",
            value=self.current_cadence_rpm,
            minimum=40.0,
            maximum=130.0,
            unit="rpm",
            color=rpm_color,
            expected_lo=None if c_lo is None else float(c_lo),
            expected_hi=None if c_hi is None else float(c_hi),
        )
        self._draw_gauge(
            self.gauge_distance,
            title="Odometer",
            value=self.distance_km,
            minimum=0.0,
            maximum=max(5.0, self._estimated_total_distance_km * 1.2, 20.0),
            unit="km",
            color=distance_color,
            expected_lo=None if distance_expected is None else distance_expected * 0.8,
            expected_hi=None if distance_expected is None else distance_expected * 1.2,
        )

        zone_parts: list[str] = []
        if p_lo is not None and p_hi is not None:
            zone_parts.append(f"Power {p_lo}-{p_hi}W")
        if c_lo is not None and c_hi is not None:
            zone_parts.append(f"RPM {c_lo}-{c_hi}")
        if speed_expected is not None:
            zone_parts.append(f"Speed {speed_expected - 4:.0f}-{speed_expected + 4:.0f}km/h")
        if zone_parts:
            self.zone_var.set("Zones: " + " | ".join(zone_parts))
        else:
            self.zone_var.set("Zones: -")

        self.compliance_var.set(self._format_zone_compliance())

    def _format_zone_compliance(self) -> str:
        power = self._pct(self._zone_power_hits, self._zone_power_total)
        rpm = self._pct(self._zone_rpm_hits, self._zone_rpm_total)
        both = self._pct(self._zone_both_hits, self._zone_both_total)
        if power is None and rpm is None and both is None:
            return "Zone compliance: -"

        parts: list[str] = []
        if power is not None:
            parts.append(
                f"Power {power:.0f}% ({self._zone_power_hits}/{self._zone_power_total})"
            )
        if rpm is not None:
            parts.append(f"RPM {rpm:.0f}% ({self._zone_rpm_hits}/{self._zone_rpm_total})")
        if both is not None:
            parts.append(f"Both {both:.0f}% ({self._zone_both_hits}/{self._zone_both_total})")
        return "Zone compliance: " + " | ".join(parts)

    def _pct(self, hits: int, total: int) -> float | None:
        if total <= 0:
            return None
        return (hits * 100.0) / total

    def _reset_zone_compliance(self) -> None:
        self._zone_power_hits = 0
        self._zone_power_total = 0
        self._zone_rpm_hits = 0
        self._zone_rpm_total = 0
        self._zone_both_hits = 0
        self._zone_both_total = 0

    def _update_zone_compliance(self) -> None:
        progress = self.current_progress
        if progress is None:
            return

        power_ok: bool | None = None
        if (
            self.current_power_w is not None
            and progress.expected_power_min_watts is not None
            and progress.expected_power_max_watts is not None
        ):
            self._zone_power_total += 1
            power_ok = (
                progress.expected_power_min_watts
                <= self.current_power_w
                <= progress.expected_power_max_watts
            )
            if power_ok:
                self._zone_power_hits += 1

        rpm_ok: bool | None = None
        if (
            self.current_cadence_rpm is not None
            and progress.expected_cadence_min_rpm is not None
            and progress.expected_cadence_max_rpm is not None
        ):
            self._zone_rpm_total += 1
            rpm_ok = (
                progress.expected_cadence_min_rpm
                <= self.current_cadence_rpm
                <= progress.expected_cadence_max_rpm
            )
            if rpm_ok:
                self._zone_rpm_hits += 1

        if power_ok is not None and rpm_ok is not None:
            self._zone_both_total += 1
            if power_ok and rpm_ok:
                self._zone_both_hits += 1

    def on_scan(self) -> None:
        self.status_var.set("Scanning BLE...")
        future = self.bridge.submit(self.controller.scan())
        future.add_done_callback(self._on_scan_done)

    def _on_scan_done(self, future: Future[list[ScannedDevice]]) -> None:
        def update() -> None:
            try:
                self.devices = future.result()
            except Exception as exc:
                self.status_var.set("Scan failed")
                messagebox.showerror("Scan", str(exc))
                return

            self.device_list.delete(0, tk.END)
            for device in self.devices:
                mark = "FTMS" if device.has_ftms else "-"
                manufacturer = f" | {device.manufacturer}" if device.manufacturer else ""
                self.device_list.insert(
                    tk.END,
                    f"{device.name}{manufacturer} | {device.address} | RSSI={device.rssi} [{mark}]",
                )
            self.status_var.set(f"Scan done: {len(self.devices)} devices")

        self._call_ui(update)

    def _selected_device_target(self) -> str | None:
        idx = cast(
            tuple[int, ...],
            self.device_list.curselection(),  # type: ignore[no-untyped-call]
        )
        if not idx:
            return None
        return self.devices[idx[0]].address

    def on_connect(self) -> None:
        target = self._selected_device_target()
        if not target:
            messagebox.showwarning("Connect", "Select a device first")
            return

        self.status_var.set("Connecting...")
        future = self.bridge.submit(
            self.controller.connect(target=target, metrics_callback=self._on_metrics)
        )
        future.add_done_callback(self._on_connect_done)

    def _on_connect_done(self, future: Future[str]) -> None:
        def update() -> None:
            try:
                label = future.result()
            except Exception as exc:
                self.status_var.set("Connection failed")
                messagebox.showerror("Connect", str(exc))
                return

            self.connected = True
            self.status_var.set(f"Connected: {label}")
            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self._refresh_workout_buttons()

        self._call_ui(update)

    def on_disconnect(self) -> None:
        future = self.bridge.submit(self.controller.disconnect())
        future.add_done_callback(self._on_disconnect_done)

    def _on_disconnect_done(self, future: Future[None]) -> None:
        def update() -> None:
            try:
                future.result()
            except Exception as exc:
                messagebox.showerror("Disconnect", str(exc))
                return

            self.connected = False
            self.status_var.set("Not connected")
            self.connect_btn.config(state=tk.NORMAL)
            self.disconnect_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.DISABLED)
            self._refresh_workout_buttons()

        self._call_ui(update)

    def _on_metrics(self, metrics: IndoorBikeData) -> None:
        now = time.monotonic()
        if self._last_metric_ts is not None and metrics.instantaneous_speed_kmh is not None:
            dt = max(0.0, now - self._last_metric_ts)
            self.distance_km += (metrics.instantaneous_speed_kmh * dt) / 3600.0
        self._last_metric_ts = now

        self.current_power_w = metrics.instantaneous_power
        self.current_cadence_rpm = metrics.instantaneous_cadence
        self.current_speed_kmh = metrics.instantaneous_speed_kmh
        if metrics.instantaneous_power is not None:
            self._sum_power_watts += metrics.instantaneous_power
        if metrics.instantaneous_cadence is not None:
            self._sum_cadence_rpm += metrics.instantaneous_cadence
        if metrics.instantaneous_speed_kmh is not None:
            self._sum_speed_kmh += metrics.instantaneous_speed_kmh
        self._metric_sample_count += 1
        self._update_zone_compliance()

        def update() -> None:
            self.power_var.set(
                "Power: "
                + (
                    f"{metrics.instantaneous_power} W"
                    if metrics.instantaneous_power is not None
                    else "N/A"
                )
            )
            self.cadence_var.set(
                "Cadence: "
                + (
                    f"{metrics.instantaneous_cadence:.1f} rpm"
                    if metrics.instantaneous_cadence is not None
                    else "N/A"
                )
            )
            self.speed_var.set(
                "Speed: "
                + (
                    f"{metrics.instantaneous_speed_kmh:.1f} km/h"
                    if metrics.instantaneous_speed_kmh is not None
                    else "N/A"
                )
            )
            self.distance_var.set(f"Distance: {self.distance_km:.2f} km")
            self._refresh_gauges()

        self._call_ui(update)

    def on_load_preset(self) -> None:
        template_label = self.template_var.get()
        template_key = self._template_by_label.get(template_label)
        if template_key is None:
            messagebox.showerror("Preset", "Unknown template")
            return

        try:
            ftp = int(self.ftp_var.get().strip())
        except ValueError:
            messagebox.showerror("Preset", "FTP must be an integer")
            return

        if ftp <= 0:
            messagebox.showerror("Preset", "FTP must be > 0")
            return

        try:
            plan = build_plan_from_template(template_key, ftp)
        except Exception as exc:
            messagebox.showerror("Preset", str(exc))
            return

        self._set_workout(plan, source="preset")

    def on_load_workout(self) -> None:
        path = filedialog.askopenfilename(
            title="Select workout file",
            filetypes=[("Workout files", "*.json *.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            plan = load_workout(path)
        except WorkoutParseError as exc:
            messagebox.showerror("Workout file", str(exc))
            return

        self._set_workout(plan, source=Path(path).name)

    def _set_workout(self, plan: WorkoutPlan, source: str) -> None:
        self.workout = plan
        self.current_progress = None
        self._estimated_total_distance_km = self._estimate_total_distance_km(plan)
        self._reset_zone_compliance()
        self._reset_zone_compliance()

        self.workout_var.set(
            f"Workout: {plan.name} | source={source} | steps={len(plan.steps)} | "
            f"total={self._format_time(plan.total_duration_sec)}"
        )
        self.steps_list.delete(0, tk.END)
        for i, step in enumerate(plan.steps, start=1):
            label = step.label or f"Step {i}"
            cadence = ""
            if step.cadence_min_rpm is not None and step.cadence_max_rpm is not None:
                cadence = f" | RPM {step.cadence_min_rpm}-{step.cadence_max_rpm}"
            self.steps_list.insert(
                tk.END,
                f"{i:02d}. {label} | {step.target_watts}W{cadence} | "
                f"{self._format_time(step.duration_sec)}",
            )

        self.progress.configure(value=0, maximum=max(1, plan.total_duration_sec))
        self.step_var.set("-")
        self.target_var.set("Target: -")
        self.rpm_objective_var.set("RPM objective: -")
        self.step_timer_var.set("Step timer: -")
        self.session_timer_var.set(
            f"Session timer: 00:00 / {self._format_time(plan.total_duration_sec)}"
        )
        self._draw_workout_curve(plan)
        self._refresh_workout_buttons()
        self._refresh_gauges()

    def _estimate_total_distance_km(self, plan: WorkoutPlan) -> float:
        total = 0.0
        for step in plan.steps:
            speed = max(10.0, min(50.0, 14.0 + (step.target_watts / 12.0)))
            total += (speed * step.duration_sec) / 3600.0
        return total

    def _refresh_workout_buttons(self) -> None:
        if self.connected and self.workout is not None:
            self.start_btn.config(state=tk.NORMAL)
        else:
            self.start_btn.config(state=tk.DISABLED)

    def _draw_workout_curve(
        self,
        plan: WorkoutPlan,
        *,
        active_step_index: int | None = None,
        elapsed_total_sec: int = 0,
    ) -> None:
        canvas = self.chart_canvas
        canvas.update_idletasks()
        width = max(100, int(canvas.winfo_width()))
        height = max(120, int(canvas.winfo_height()))

        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#0f172a", outline="")

        left_pad = 50
        right_pad = 20
        top_pad = 16
        bottom_pad = 26
        plot_w = width - left_pad - right_pad
        plot_h = height - top_pad - bottom_pad
        if plot_w <= 0 or plot_h <= 0:
            return

        max_watts = max(step.target_watts for step in plan.steps)
        max_watts = max(100, int(max_watts * 1.15))

        canvas.create_line(left_pad, top_pad, left_pad, top_pad + plot_h, fill="#64748b")
        canvas.create_line(
            left_pad,
            top_pad + plot_h,
            left_pad + plot_w,
            top_pad + plot_h,
            fill="#64748b",
        )

        for y_mark in (0.25, 0.5, 0.75, 1.0):
            watts = int(max_watts * y_mark)
            y = top_pad + plot_h - int(plot_h * y_mark)
            canvas.create_line(left_pad, y, left_pad + plot_w, y, fill="#1e293b")
            canvas.create_text(6, y, text=str(watts), anchor=tk.W, fill="#94a3b8")

        total = plan.total_duration_sec
        elapsed = 0
        for idx, step in enumerate(plan.steps, start=1):
            x0 = left_pad + int((elapsed / total) * plot_w)
            elapsed += step.duration_sec
            x1 = left_pad + int((elapsed / total) * plot_w)
            level = step.target_watts / max_watts
            y = top_pad + plot_h - int(level * plot_h)

            fill = "#22c55e" if idx != active_step_index else "#f59e0b"
            canvas.create_rectangle(x0, y, x1, top_pad + plot_h, fill=fill, outline="#0f172a")

        if elapsed_total_sec > 0:
            progress_x = left_pad + int((min(elapsed_total_sec, total) / total) * plot_w)
            canvas.create_line(
                progress_x,
                top_pad,
                progress_x,
                top_pad + plot_h,
                fill="#f8fafc",
                width=2,
            )

        canvas.create_text(left_pad, height - 12, text="0", anchor=tk.W, fill="#94a3b8")
        canvas.create_text(
            left_pad + plot_w,
            height - 12,
            text=self._format_time(total),
            anchor=tk.E,
            fill="#94a3b8",
        )

    def on_start_workout(self) -> None:
        if self.workout is None:
            return
        target_mode = cast(TargetMode, self.mode_var.get())
        try:
            ftp_watts = int(self.ftp_var.get().strip())
        except ValueError:
            messagebox.showerror("Workout", "FTP must be an integer")
            return
        if ftp_watts <= 0:
            messagebox.showerror("Workout", "FTP must be > 0")
            return

        self.distance_km = 0.0
        self._last_metric_ts = None
        self._session_started_utc = now_utc_iso()
        self._session_mode = target_mode
        self._session_ftp_watts = ftp_watts
        self._metric_sample_count = 0
        self._sum_power_watts = 0.0
        self._sum_cadence_rpm = 0.0
        self._sum_speed_kmh = 0.0
        self._reset_zone_compliance()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        future = self.bridge.submit(
            self.controller.start_workout(
                self.workout,
                target_mode=target_mode,
                ftp_watts=ftp_watts,
                on_progress=self._on_workout_progress,
                on_finish=self._on_workout_finish,
            )
        )
        future.add_done_callback(self._on_start_workout_done)

    def _on_start_workout_done(self, future: Future[None]) -> None:
        def update() -> None:
            try:
                future.result()
            except Exception as exc:
                self.stop_btn.config(state=tk.DISABLED)
                self._refresh_workout_buttons()
                messagebox.showerror("Workout", str(exc))

        self._call_ui(update)

    def on_stop_workout(self) -> None:
        self.bridge.submit(self.controller.stop_workout())

    def _on_workout_progress(self, progress: WorkoutProgress) -> None:
        self.current_progress = progress

        def update() -> None:
            self.step_var.set(
                f"Step {progress.step_index}/{progress.step_total}: {progress.step_label}"
            )
            self.target_var.set(
                f"Target: {progress.target_display_value:.1f}{progress.target_display_unit} "
                f"({progress.target_mode}, ref {progress.target_watts}W)"
            )

            if (
                progress.expected_cadence_min_rpm is not None
                and progress.expected_cadence_max_rpm is not None
            ):
                self.rpm_objective_var.set(
                    "RPM objective: "
                    f"{progress.expected_cadence_min_rpm}-"
                    f"{progress.expected_cadence_max_rpm}"
                )
            else:
                self.rpm_objective_var.set("RPM objective: free")

            self.step_timer_var.set(
                "Step timer: "
                f"{self._format_time(progress.step_elapsed_sec)} / "
                f"{self._format_time(progress.step_duration_sec)} "
                f"(remaining {self._format_time(progress.remaining_sec)})"
            )
            self.session_timer_var.set(
                "Session timer: "
                f"{self._format_time(progress.elapsed_total_sec)} / "
                f"{self._format_time(progress.total_duration_sec)} "
                f"(remaining {self._format_time(progress.total_remaining_sec)})"
            )
            self.progress.configure(
                maximum=max(1, progress.total_duration_sec),
                value=progress.elapsed_total_sec,
            )
            self.steps_list.selection_clear(0, tk.END)
            self.steps_list.selection_set(progress.step_index - 1)
            self.steps_list.activate(progress.step_index - 1)
            if self.workout is not None:
                self._draw_workout_curve(
                    self.workout,
                    active_step_index=progress.step_index,
                    elapsed_total_sec=progress.elapsed_total_sec,
                )
            self._refresh_gauges()

        self._call_ui(update)

    def _on_workout_finish(self, completed: bool) -> None:
        def update() -> None:
            self._save_session(completed)
            self.stop_btn.config(state=tk.DISABLED)
            self._refresh_workout_buttons()
            if completed:
                self.status_var.set("Workout completed")
            else:
                self.status_var.set("Workout stopped")
            self.current_progress = None
            self._refresh_gauges()
            self._refresh_history_list()

        self._call_ui(update)

    def _save_session(self, completed: bool) -> None:
        if self.workout is None:
            return
        planned = self.workout.total_duration_sec
        elapsed = self.current_progress.elapsed_total_sec if self.current_progress else 0
        record = SessionRecord(
            started_at_utc=self._session_started_utc or now_utc_iso(),
            ended_at_utc=now_utc_iso(),
            workout_name=self.workout.name,
            target_mode=self._session_mode,
            ftp_watts=self._session_ftp_watts,
            completed=completed,
            planned_duration_sec=planned,
            elapsed_duration_sec=elapsed,
            distance_km=round(self.distance_km, 3),
            avg_power_watts=self._avg(self._sum_power_watts, self._metric_sample_count),
            avg_cadence_rpm=self._avg(self._sum_cadence_rpm, self._metric_sample_count),
            avg_speed_kmh=self._avg(self._sum_speed_kmh, self._metric_sample_count),
            power_compliance_pct=self._pct(self._zone_power_hits, self._zone_power_total),
            rpm_compliance_pct=self._pct(self._zone_rpm_hits, self._zone_rpm_total),
            both_compliance_pct=self._pct(self._zone_both_hits, self._zone_both_total),
        )
        append_session(record)

    def _on_close(self) -> None:
        self.bridge.submit(self.controller.disconnect())
        self.bridge.shutdown()
        self.root.destroy()


def run_ui(simulate_ht: bool = False, ble_pair: bool = True) -> int:
    root = tk.Tk()
    VeloxUI(root, simulate_ht=simulate_ht, ble_pair=ble_pair)
    root.mainloop()
    return 0
