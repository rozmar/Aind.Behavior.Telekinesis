#!/usr/bin/env python3
"""
Telekinesis Task GUI

Tab 1 – Task Configuration: mouse profile selector, task parameters, 2D LUT editor, config generation, task launch.
Tab 2 – Live Monitor: ZMQ subscriber, trial performance plot (time-to-reward vs trial), stats, event log.

Requirements (all already in the project venv):
    matplotlib, numpy, Pillow, pyzmq
Run:
    python set_up_task/task_gui.py
"""

import ctypes
import datetime
import json
import queue
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.ticker as ticker
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

# ── High-DPI awareness on Windows ─────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
LOCAL_DIR    = PROJECT_ROOT / "local"
MICE_DIR     = LOCAL_DIR / "mice"
MICE_DIR.mkdir(parents=True, exist_ok=True)

# ── Default mouse profile ──────────────────────────────────────────────────────
DEFAULT_PARAMS: dict = {
    "trial_length":          1000.0,
    "lick_response_time":    4.0,
    "inter_trial_interval":  2.0,
    "reward_size":           1.0,
    "far_position":          5.0,
    "close_position":        14.5,
    "mouse_motor_hard_limit": 15.0,
    "quiescence_duration":    0.5,
    "quiescence_threshold":   1.0,
    "is_operant":             False,
    "lut_gaussians": [
        {"center_lat":  750, "center_ap": -750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
        {"center_lat":  750, "center_ap":  750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
        {"center_lat": -750, "center_ap": -750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
    ],
    "lut_offset":    -0.05,
    "lut_scale":      2.5,
    "lat_range_min": -2000.0,
    "lat_range_max":  2000.0,
    "ap_range_min":  -2000.0,
    "ap_range_max":   2000.0,
    # Camera parameters — per mouse
    "camera_exposure":  10000,
    "camera_gain":      18.0,
    "camera_gamma":     None,   # None = disabled; float = enabled
    "camera2_exposure": 10000,
    "camera2_gain":     18.0,
    "camera2_gamma":    None,
}

# ── Default rig settings (global, not per-mouse) ──────────────────────────────
DEFAULT_RIG: dict = {
    "port_behavior":    "COM3",
    "port_load_cells":  "COM4",
    "port_lickometer":  "COM6",
    "port_clock":       "COM9",
    "port_manipulator": "COM10",
    "motor_mode":       "QUIET",   # "QUIET" or "DYNAMIC"
    "camera_serial":    "25312141",
    "camera_frame_rate": 80,
    "zmq_connection":   "@tcp://localhost:5556",
    "zmq_topic":        "Telekinesis",
    "data_directory":   "C:\\Data",
}

# ── LUT helpers ────────────────────────────────────────────────────────────────

def gaussian_2d(x_lat, x_ap, peak, trough, center_lat, center_ap, sigma_lat, sigma_ap):
    y = (peak - trough) * np.exp(
        -((x_lat[np.newaxis, :] - center_lat) ** 2 / (2 * sigma_lat ** 2)
          + (x_ap[:, np.newaxis] - center_ap) ** 2 / (2 * sigma_ap ** 2))
    )
    return trough + y


def compute_lut_matrix(params: dict, bin_num: int = 100):
    lat_vec = np.linspace(params.get("lat_range_min", -2000), params.get("lat_range_max", 2000), bin_num)
    ap_vec  = np.linspace(params.get("ap_range_min",  -2000), params.get("ap_range_max",  2000), bin_num)
    matrix  = np.zeros((bin_num, bin_num))
    for g in params.get("lut_gaussians", []):
        try:
            matrix += gaussian_2d(
                lat_vec, ap_vec,
                g["peak"], g["trough"],
                g["center_lat"], g["center_ap"],
                g["sigma_lat"],  g["sigma_ap"],
            )
        except Exception:
            pass
    matrix += params.get("lut_offset", 0.0)
    return matrix, lat_vec, ap_vec


def save_lut_image(params: dict, path: Path):
    matrix, _, _ = compute_lut_matrix(params)
    Image.fromarray(matrix.astype(np.float32)).save(str(path))


# ── Profile I/O ────────────────────────────────────────────────────────────────

def load_mouse_profiles() -> dict:
    profiles = {}
    for f in sorted(MICE_DIR.glob("*.json")):
        try:
            with open(f) as fp:
                profiles[f.stem] = json.load(fp)
        except Exception:
            pass
    return profiles


def save_mouse_profile(name: str, params: dict):
    MICE_DIR.mkdir(parents=True, exist_ok=True)
    with open(MICE_DIR / f"{name}.json", "w") as f:
        json.dump(params, f, indent=2)


# ── ZMQ subscriber ─────────────────────────────────────────────────────────────

class ZmqSubscriberThread(threading.Thread):
    """Background thread that subscribes to a ZMQ PUB socket and pushes events
    into a queue for the main tkinter thread to process."""

    def __init__(self, host: str, port: int, topic: str, event_queue: queue.Queue,
                 debug: bool = False):
        super().__init__(daemon=True)
        self.host        = host
        self.port        = port
        self.topic       = topic
        self.event_queue = event_queue
        self.debug       = debug
        self._stop       = threading.Event()

    def run(self):
        try:
            import zmq
        except ImportError:
            self.event_queue.put(("__error__", 0, "pyzmq not installed"))
            return

        ctx  = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        # Debug: subscribe to everything so we can see whatever Bonsai actually sends.
        # Normal: subscribe only to the configured topic prefix.
        sock.setsockopt(zmq.SUBSCRIBE, b"" if self.debug else self.topic.encode())
        sock.setsockopt(zmq.RCVTIMEO, 200)
        try:
            sock.connect(f"tcp://{self.host}:{self.port}")
            mode = "DEBUG – subscribed to ALL topics" if self.debug else f"topic='{self.topic}'"
            self.event_queue.put(("__connected__", time.time(), mode))
        except Exception as exc:
            self.event_queue.put(("__error__", 0, str(exc)))
            ctx.term()
            return

        while not self._stop.is_set():
            try:
                frames = sock.recv_multipart()
                if self.debug:
                    # Emit the raw frame bytes so the user can see exactly what arrived
                    raw = f"{len(frames)} frame(s): " + " | ".join(
                        repr(f[:120]) for f in frames
                    )
                    self.event_queue.put(("__raw__", time.time(), raw))
                self._parse(frames)
            except Exception:
                pass  # zmq.Again on timeout, or other transient errors

        sock.close()
        ctx.term()
        self.event_queue.put(("__disconnected__", time.time(), None))

    def _parse(self, frames: list):
        # Bonsai ZMQ format: event name is body["name"], timestamp is a plain Harp
        # float (seconds), payload is body["data"].
        # Bonsai may send a single-frame message where the ZMQ topic prefix is
        # prepended to the JSON bytes (e.g. b"Telekinesis {...}").  Strip it by
        # scanning forward to the first "{".
        try:
            if len(frames) >= 2:
                body_bytes = frames[1]
            else:
                body_bytes = frames[0]
                if not body_bytes.lstrip().startswith(b"{"):
                    idx = body_bytes.find(b"{")
                    if idx < 0:
                        self.event_queue.put(("__parse_err__", time.time(),
                                              f"no '{{' found in: {frames[0][:80]!r}"))
                        return
                    body_bytes = body_bytes[idx:]
            body       = json.loads(body_bytes)
            event_name = body.get("name", "")
            if not event_name:
                self.event_queue.put(("__parse_err__", time.time(),
                                      f"no 'name' key in body: {str(body)[:120]}"))
                return
            ts_raw  = body.get("timestamp")
            ts      = float(ts_raw) if ts_raw is not None else time.time()
            payload = body.get("data")
            self.event_queue.put((event_name, ts, payload))
        except Exception as exc:
            self.event_queue.put(("__parse_err__", time.time(), str(exc)))

    def stop(self):
        self._stop.set()


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1 – Task Configuration
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._profiles: dict         = load_mouse_profiles()
        self._params: dict           = deepcopy(DEFAULT_PARAMS)
        self._sel_gauss_idx: int | None = None
        self._gauss_loading: bool    = False   # guard against re-entrant gaussian trace
        self._params_loading: bool   = False   # guard: suppress param traces while loading a profile
        self._build_ui()
        self._refresh_mouse_list()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        left = ttk.Frame(self, width=300)
        left.pack(side="left", fill="y", padx=(6, 3), pady=6)
        left.pack_propagate(False)

        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, padx=(3, 6), pady=6)

        self._build_left(left)
        self._build_right(right)

    # ── Left panel ─────────────────────────────────────────────────────────────

    def _build_left(self, parent):
        # Scrollable container — left panel has many parameters that exceed window height
        vsb    = ttk.Scrollbar(parent, orient="vertical")
        canvas = tk.Canvas(parent, yscrollcommand=vsb.set, highlightthickness=0, bd=0)
        vsb.config(command=canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        _cwin = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _: canvas.config(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(_cwin, width=e.width))
        parent = inner  # redirect all widget creation into the scrollable inner frame

        # Mouse selector
        mg = ttk.LabelFrame(parent, text="Mouse")
        mg.pack(fill="x", pady=(0, 5))

        row = ttk.Frame(mg)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Label(row, text="Mouse:").pack(side="left")
        self._mouse_var   = tk.StringVar()
        self._mouse_combo = ttk.Combobox(row, textvariable=self._mouse_var, width=16, state="readonly")
        self._mouse_combo.pack(side="left", padx=(4, 2))
        self._mouse_combo.bind("<<ComboboxSelected>>", self._on_mouse_selected)
        ttk.Button(row, text="New",    width=5, command=self._on_new_mouse).pack(side="left")
        ttk.Button(row, text="Delete", width=6, command=self._on_delete_mouse).pack(side="left", padx=2)

        # Session info
        sg = ttk.LabelFrame(parent, text="Session")
        sg.pack(fill="x", pady=(0, 5))

        for attr, label, default in [
            ("_exp_var",   "Experimenter:", "rozmar"),
            ("_notes_var", "Notes:",        ""),
        ]:
            row2 = ttk.Frame(sg)
            row2.pack(fill="x", padx=4, pady=2)
            ttk.Label(row2, text=label, width=13, anchor="e").pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Entry(row2, textvariable=var).pack(side="left", fill="x", expand=True, padx=(4, 0))
            setattr(self, attr, var)

        # Task parameters
        pg = ttk.LabelFrame(parent, text="Task Parameters")
        pg.pack(fill="x", pady=(0, 5))

        self._param_vars: dict = {}
        for key, label, default, lo, hi, step in [
            ("trial_length",          "Trial Length (s)",    1000.0,  1,    10000, 100.0),
            ("lick_response_time",    "Lick Response (s)",      4.0,  0.1,     60,   0.5),
            ("inter_trial_interval",  "ITI (s)",                2.0,  0.1,     60,   0.5),
            ("reward_size",           "Reward (µL)",            1.0,  0.1,     20,   0.1),
            ("far_position",          "Far Position (mm)",      5.0,  0,       30,   0.5),
            ("close_position",        "Close Position (mm)",   14.5,  0,       30,   0.5),
            ("mouse_motor_hard_limit","Motor Limit (mm)",      15.0,  0,       30,   0.5),
            ("quiescence_duration",  "Quiescence (s)",         0.5,  0.0,     60,   0.1),
            ("quiescence_threshold", "Quiescence Threshold",   1.0,  0.0,   1000,   0.5),
        ]:
            var = self._add_spinrow(pg, label, default, lo, hi, step)
            var.trace_add("write", self._on_task_param_changed)
            self._param_vars[key] = var

        # is_operant
        iop_row = ttk.Frame(pg)
        iop_row.pack(fill="x", pady=1, padx=4)
        ttk.Label(iop_row, text="Is Operant:", width=20, anchor="e").pack(side="left")
        self._is_operant_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(iop_row, variable=self._is_operant_var,
                        command=self._on_task_param_changed).pack(side="left", padx=(4, 0))

        # Camera parameters — per mouse, independently for each camera
        cg = ttk.LabelFrame(parent, text="Cameras (per Mouse)")
        cg.pack(fill="x", pady=(0, 5))

        ttk.Label(cg, text="Camera 1 (Main):", font=("TkDefaultFont", 8, "bold"),
                  anchor="w").pack(fill="x", padx=6, pady=(2, 0))
        for key, label, default, lo, hi, step in [
            ("camera_exposure", "Exposure (µs):", 10000, 1, 1_000_000, 500),
            ("camera_gain",     "Gain:",           18.0, 0,       100,   0.5),
        ]:
            var = self._add_spinrow(cg, label, default, lo, hi, step)
            var.trace_add("write", self._on_task_param_changed)
            self._param_vars[key] = var
        grow = ttk.Frame(cg)
        grow.pack(fill="x", pady=1, padx=4)
        self._gamma_en_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(grow, text="Gamma:", variable=self._gamma_en_var,
                        command=lambda: self._on_gamma_toggled(1), width=14).pack(side="left")
        self._gamma_var  = tk.DoubleVar(value=1.0)
        self._gamma_spin = ttk.Spinbox(grow, from_=0.1, to=10.0, increment=0.1,
                                       textvariable=self._gamma_var, width=9, format="%.2f",
                                       state="disabled")
        self._gamma_spin.pack(side="left", padx=(4, 0))
        self._gamma_var.trace_add("write", self._on_task_param_changed)

        ttk.Separator(cg, orient="horizontal").pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(cg, text="Camera 2 (Second):", font=("TkDefaultFont", 8, "bold"),
                  anchor="w").pack(fill="x", padx=6, pady=(0, 0))
        for key, label, default, lo, hi, step in [
            ("camera2_exposure", "Exposure (µs):", 10000, 1, 1_000_000, 500),
            ("camera2_gain",     "Gain:",           18.0, 0,       100,   0.5),
        ]:
            var = self._add_spinrow(cg, label, default, lo, hi, step)
            var.trace_add("write", self._on_task_param_changed)
            self._param_vars[key] = var
        grow2 = ttk.Frame(cg)
        grow2.pack(fill="x", pady=1, padx=4)
        self._gamma2_en_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(grow2, text="Gamma:", variable=self._gamma2_en_var,
                        command=lambda: self._on_gamma_toggled(2), width=14).pack(side="left")
        self._gamma2_var  = tk.DoubleVar(value=1.0)
        self._gamma2_spin = ttk.Spinbox(grow2, from_=0.1, to=10.0, increment=0.1,
                                        textvariable=self._gamma2_var, width=9, format="%.2f",
                                        state="disabled")
        self._gamma2_spin.pack(side="left", padx=(4, 0))
        self._gamma2_var.trace_add("write", self._on_task_param_changed)

        # Buttons
        bf = ttk.Frame(parent)
        bf.pack(fill="x", pady=(4, 0))
        ttk.Label(bf,
                  text="Camera settings are written to the rig JSON\n"
                       "on Generate Config / Start Task, then take\n"
                       "effect the next time Bonsai starts.",
                  foreground="gray", justify="left", font=("TkDefaultFont", 7),
                  ).pack(fill="x", padx=4, pady=(0, 2))
        ttk.Button(bf, text="Save Profile",    command=self._on_save_profile).pack(fill="x", pady=2)
        ttk.Button(bf, text="Generate Config", command=self._on_generate_config).pack(fill="x", pady=2)

        start_btn = tk.Button(
            bf, text="▶  Start Task",
            bg="#2e7d32", fg="white", font=("TkDefaultFont", 10, "bold"),
            relief="flat", padx=8, pady=4,
            command=self._on_start_task,
        )
        start_btn.pack(fill="x", pady=2)

    @staticmethod
    def _add_spinrow(parent, label: str, default: float, lo: float, hi: float, step: float) -> tk.DoubleVar:
        """Helper: add a label + spinbox row, return the DoubleVar."""
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=1, padx=4)
        ttk.Label(row, text=label, width=20, anchor="e").pack(side="left")
        var = tk.DoubleVar(value=default)
        ttk.Spinbox(row, from_=lo, to=hi, increment=step,
                    textvariable=var, width=9, format="%.2f").pack(side="left", padx=(4, 0))
        return var

    # ── Right panel: LUT editor ────────────────────────────────────────────────

    def _build_right(self, parent):
        lut_frame = ttk.LabelFrame(parent, text="LUT Editor – 2D Gaussian Speed Map")
        lut_frame.pack(fill="both", expand=True)

        # Gaussian list + form (left side of LUT frame)
        gauss_frame = ttk.Frame(lut_frame, width=255)
        gauss_frame.pack(side="left", fill="y", padx=(4, 2), pady=4)
        gauss_frame.pack_propagate(False)

        ttk.Label(gauss_frame, text="Gaussians:").pack(anchor="w")

        lb_frame = ttk.Frame(gauss_frame)
        lb_frame.pack(fill="x")
        self._gauss_lb = tk.Listbox(lb_frame, height=7, selectmode="single",
                                    exportselection=False, font=("Consolas", 8))
        self._gauss_lb.pack(side="left", fill="x", expand=True)
        vsb = ttk.Scrollbar(lb_frame, orient="vertical", command=self._gauss_lb.yview)
        vsb.pack(side="right", fill="y")
        self._gauss_lb.config(yscrollcommand=vsb.set)
        self._gauss_lb.bind("<<ListboxSelect>>", self._on_gauss_list_select)

        btn_row = ttk.Frame(gauss_frame)
        btn_row.pack(fill="x", pady=2)
        ttk.Button(btn_row, text="+ Add",    width=7,  command=self._add_gaussian).pack(side="left")
        ttk.Button(btn_row, text="− Remove", width=8,  command=self._remove_gaussian).pack(side="left", padx=2)
        ttk.Button(btn_row, text="↑", width=3, command=lambda: self._move_gaussian(-1)).pack(side="left")
        ttk.Button(btn_row, text="↓", width=3, command=lambda: self._move_gaussian(1)).pack(side="left", padx=1)

        # Parameter form for the selected gaussian
        gf = ttk.LabelFrame(gauss_frame, text="Selected Gaussian")
        gf.pack(fill="x", pady=(4, 0))
        self._gauss_vars: dict = {}
        for key, label, default, lo, hi, step in [
            ("center_lat", "Center Lat:",  0.0, -9999, 9999,  50.0),
            ("center_ap",  "Center AP:",   0.0, -9999, 9999,  50.0),
            ("sigma_lat",  "Sigma Lat:", 200.0,     1, 9999,  10.0),
            ("sigma_ap",   "Sigma AP:",  200.0,     1, 9999,  10.0),
            ("peak",       "Peak:",        5.0,  -500,  500,   0.5),
            ("trough",     "Trough:",      0.0,  -500,  500,   0.1),
        ]:
            row = ttk.Frame(gf)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=12, anchor="e").pack(side="left")
            var = tk.DoubleVar(value=default)
            ttk.Spinbox(row, from_=lo, to=hi, increment=step,
                        textvariable=var, width=9, format="%.1f").pack(side="left", padx=(4, 0))
            var.trace_add("write", self._on_gauss_form_changed)
            self._gauss_vars[key] = var

        # LUT global settings — next to the gaussian list they affect
        lg = ttk.LabelFrame(gauss_frame, text="LUT Global Settings")
        lg.pack(fill="x", pady=(4, 0))
        self._lut_offset_var = self._add_spinrow(lg, "Offset:",         -0.05, -100, 100,  0.05)
        self._lut_scale_var  = self._add_spinrow(lg, "Scale (output):",   2.5,    0, 100,  0.25)
        self._lut_offset_var.trace_add("write", lambda *_: self._schedule_lut_update())
        self._lut_scale_var.trace_add("write",  lambda *_: self._schedule_lut_update())

        rg = ttk.LabelFrame(gauss_frame, text="Force Input Range")
        rg.pack(fill="x", pady=(4, 0))
        self._lat_min_var = self._add_spinrow(rg, "Lat min:", -2000, -9999, 9999, 100)
        self._lat_max_var = self._add_spinrow(rg, "Lat max:",  2000, -9999, 9999, 100)
        self._ap_min_var  = self._add_spinrow(rg, "AP min:",  -2000, -9999, 9999, 100)
        self._ap_max_var  = self._add_spinrow(rg, "AP max:",   2000, -9999, 9999, 100)
        for v in (self._lat_min_var, self._lat_max_var, self._ap_min_var, self._ap_max_var):
            v.trace_add("write", lambda *_: self._schedule_lut_update())

        # Matplotlib LUT preview (right side)
        canvas_frame = ttk.Frame(lut_frame)
        canvas_frame.pack(side="left", fill="both", expand=True, padx=(2, 4), pady=4)

        self._lut_fig = Figure(figsize=(4.8, 4.0), tight_layout=True)
        self._lut_ax  = self._lut_fig.add_subplot(111)
        # Create imshow and colorbar exactly once so colorbar never steals extra space
        _blank = np.zeros((100, 100))
        self._lut_im = self._lut_ax.imshow(
            _blank, cmap="viridis", origin="lower", aspect="auto",
            extent=[-2000, 2000, -2000, 2000],
        )
        self._lut_cbar = self._lut_fig.colorbar(self._lut_im, ax=self._lut_ax,
                                                 fraction=0.046, pad=0.04)
        self._lut_cbar.set_label("Speed (mm/s)", fontsize=8)
        self._lut_ax.set_xlabel("Lateral Force (au)", fontsize=8)
        self._lut_ax.set_ylabel("AP Force (au)", fontsize=8)
        self._lut_canvas = FigureCanvasTkAgg(self._lut_fig, master=canvas_frame)
        self._lut_canvas.get_tk_widget().pack(fill="both", expand=True)

        self._refresh_gauss_list()
        self._update_lut_preview()

    # ── Mouse profile helpers ──────────────────────────────────────────────────

    def _rig_camera_defaults(self) -> dict:
        """Read per-camera exposure/gain/gamma from the rig JSON, keyed as camera1/camera2.
        Returns DEFAULT_PARAMS values if the rig JSON is missing or unreadable."""
        rig_path = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"
        try:
            with open(rig_path, encoding="utf-8") as f:
                rig = json.load(f)
            items = list(rig.get("triggered_camera_controller", {})
                            .get("cameras", {}).items())
        except Exception:
            items = []
        _fallback = [
            ("camera_exposure",  "camera_gain",  "camera_gamma"),
            ("camera2_exposure", "camera2_gain", "camera2_gamma"),
        ]
        def _cam(idx):
            exp_k, gain_k, gamma_k = _fallback[min(idx, 1)]
            if idx < len(items):
                cam = items[idx][1]
                return {
                    "exposure": cam.get("exposure", DEFAULT_PARAMS[exp_k]),
                    "gain":     cam.get("gain",     DEFAULT_PARAMS[gain_k]),
                    "gamma":    cam.get("gamma",    DEFAULT_PARAMS[gamma_k]),
                }
            return {
                "exposure": DEFAULT_PARAMS[exp_k],
                "gain":     DEFAULT_PARAMS[gain_k],
                "gamma":    DEFAULT_PARAMS[gamma_k],
            }
        c1, c2 = _cam(0), _cam(1)
        return {
            "camera_exposure":  c1["exposure"],
            "camera_gain":      c1["gain"],
            "camera_gamma":     c1["gamma"],
            "camera2_exposure": c2["exposure"],
            "camera2_gain":     c2["gain"],
            "camera2_gamma":    c2["gamma"],
        }

    def _refresh_mouse_list(self):
        names = ["TEST_MOUSE"] + [n for n in sorted(self._profiles) if n != "TEST_MOUSE"]
        self._mouse_combo["values"] = names
        if not self._mouse_var.get() or self._mouse_var.get() not in names:
            self._mouse_var.set("TEST_MOUSE")

    def _on_mouse_selected(self, _=None):
        name = self._mouse_var.get()
        self._params = deepcopy(self._profiles.get(name, DEFAULT_PARAMS))
        # Back-fill any missing camera keys from the rig JSON so existing profiles
        # that were saved before these fields existed still behave correctly.
        rig_cams = self._rig_camera_defaults()
        for k, v in rig_cams.items():
            if k not in self._params:
                self._params[k] = v
        self._apply_params_to_ui()

    def _on_new_mouse(self):
        name = simpledialog.askstring("New Mouse", "Enter mouse name:", parent=self)
        if name and name.strip():
            name = name.strip()
            new_profile = deepcopy(DEFAULT_PARAMS)
            # Seed camera settings from the current rig JSON so Generate Config
            # doesn't silently change the camera from its intended configuration.
            new_profile.update(self._rig_camera_defaults())
            self._profiles[name] = new_profile
            save_mouse_profile(name, self._profiles[name])
            self._refresh_mouse_list()
            self._mouse_combo.set(name)
            self._on_mouse_selected()   # reset self._params to this new mouse's defaults

    def _on_delete_mouse(self):
        name = self._mouse_var.get()
        if name == "TEST_MOUSE":
            messagebox.showwarning("Cannot Delete", "Cannot delete the default TEST_MOUSE profile.", parent=self)
            return
        if messagebox.askyesno("Delete", f"Delete profile '{name}'?", parent=self):
            self._profiles.pop(name, None)
            p = MICE_DIR / f"{name}.json"
            if p.exists():
                p.unlink()
            self._refresh_mouse_list()
            self._mouse_combo.set("TEST_MOUSE")
            self._on_mouse_selected()

    def _apply_params_to_ui(self):
        self._params_loading = True
        try:
            for key, var in self._param_vars.items():
                var.set(self._params.get(key, DEFAULT_PARAMS.get(key, 0)))
            self._lut_offset_var.set(self._params.get("lut_offset", -0.05))
            self._lut_scale_var.set(self._params.get("lut_scale",  2.5))
            self._lat_min_var.set(self._params.get("lat_range_min", -2000))
            self._lat_max_var.set(self._params.get("lat_range_max",  2000))
            self._ap_min_var.set(self._params.get("ap_range_min",  -2000))
            self._ap_max_var.set(self._params.get("ap_range_max",   2000))
            gamma_val = self._params.get("camera_gamma")
            enabled   = gamma_val is not None
            self._gamma_en_var.set(enabled)
            self._gamma_var.set(gamma_val if enabled else 1.0)
            self._gamma_spin.config(state="normal" if enabled else "disabled")
            self._is_operant_var.set(bool(self._params.get("is_operant", False)))
            gamma2_val = self._params.get("camera2_gamma")
            enabled2   = gamma2_val is not None
            self._gamma2_en_var.set(enabled2)
            self._gamma2_var.set(gamma2_val if enabled2 else 1.0)
            self._gamma2_spin.config(state="normal" if enabled2 else "disabled")
            self._refresh_gauss_list()
            self._update_lut_preview()
        finally:
            self._params_loading = False

    def _on_task_param_changed(self, *_):
        if self._params_loading:
            return
        for key, var in self._param_vars.items():
            try:
                self._params[key] = var.get()
            except Exception:
                pass
        self._params["is_operant"] = self._is_operant_var.get()

    def _on_gamma_toggled(self, cam: int = 1):
        if cam == 1:
            self._gamma_spin.config(state="normal" if self._gamma_en_var.get() else "disabled")
        else:
            self._gamma2_spin.config(state="normal" if self._gamma2_en_var.get() else "disabled")

    # ── Gaussian list helpers ──────────────────────────────────────────────────

    def _gauss_label(self, i: int, g: dict) -> str:
        return (f"G{i+1}  c=({g['center_lat']:.0f}, {g['center_ap']:.0f})"
                f"  σ=({g['sigma_lat']:.0f},{g['sigma_ap']:.0f})"
                f"  pk={g['peak']:.1f}")

    def _refresh_gauss_list(self, keep_selection: bool = False):
        saved = self._gauss_lb.curselection()
        self._gauss_lb.delete(0, tk.END)
        for i, g in enumerate(self._params.get("lut_gaussians", [])):
            self._gauss_lb.insert(tk.END, self._gauss_label(i, g))
        if keep_selection and saved:
            idx = min(saved[0], self._gauss_lb.size() - 1)
            if idx >= 0:
                self._gauss_lb.selection_set(idx)

    def _on_gauss_list_select(self, _=None):
        sel = self._gauss_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        self._sel_gauss_idx = idx
        g = self._params["lut_gaussians"][idx]
        self._gauss_loading = True
        for key, var in self._gauss_vars.items():
            var.set(g.get(key, 0.0))
        self._gauss_loading = False

    def _on_gauss_form_changed(self, *_):
        if self._gauss_loading:
            return
        idx = self._sel_gauss_idx
        gaussians = self._params.get("lut_gaussians", [])
        if idx is None or not (0 <= idx < len(gaussians)):
            return
        try:
            gaussians[idx] = {k: v.get() for k, v in self._gauss_vars.items()}
        except Exception:
            return
        # Refresh only the changed item label (avoids full list rebuild)
        self._gauss_lb.delete(idx)
        self._gauss_lb.insert(idx, self._gauss_label(idx, gaussians[idx]))
        self._gauss_lb.selection_set(idx)
        self._schedule_lut_update()

    def _add_gaussian(self):
        new_g = {"center_lat": 0.0, "center_ap": 0.0,
                 "sigma_lat": 200.0, "sigma_ap": 200.0,
                 "peak": 5.0, "trough": 0.0}
        self._params.setdefault("lut_gaussians", []).append(new_g)
        self._refresh_gauss_list()
        new_idx = len(self._params["lut_gaussians"]) - 1
        self._gauss_lb.selection_set(new_idx)
        self._sel_gauss_idx = new_idx
        self._on_gauss_list_select()
        self._schedule_lut_update()

    def _remove_gaussian(self):
        idx = self._sel_gauss_idx
        gaussians = self._params.get("lut_gaussians", [])
        if idx is not None and 0 <= idx < len(gaussians):
            gaussians.pop(idx)
            self._sel_gauss_idx = max(0, idx - 1) if gaussians else None
            self._refresh_gauss_list(keep_selection=True)
            if self._sel_gauss_idx is not None:
                self._on_gauss_list_select()
            self._schedule_lut_update()

    def _move_gaussian(self, direction: int):
        idx = self._sel_gauss_idx
        gaussians = self._params.get("lut_gaussians", [])
        if idx is None or not gaussians:
            return
        new_idx = idx + direction
        if 0 <= new_idx < len(gaussians):
            gaussians[idx], gaussians[new_idx] = gaussians[new_idx], gaussians[idx]
            self._sel_gauss_idx = new_idx
            self._refresh_gauss_list()
            self._gauss_lb.selection_set(new_idx)
            self._on_gauss_list_select()
            self._schedule_lut_update()

    # ── LUT preview ────────────────────────────────────────────────────────────

    def _schedule_lut_update(self):
        """Rate-limit preview redraws to avoid UI stutter while typing."""
        if hasattr(self, "_lut_update_job"):
            self.after_cancel(self._lut_update_job)
        self._lut_update_job = self.after(80, self._update_lut_preview)

    def _update_lut_preview(self):
        try:
            p = self._build_current_params()
            matrix, lat_vec, ap_vec = compute_lut_matrix(p)
            # Update existing imshow in-place — never recreate axes or colorbar
            self._lut_im.set_data(matrix)
            self._lut_im.set_extent([lat_vec[0], lat_vec[-1], ap_vec[0], ap_vec[-1]])
            self._lut_im.set_clim(matrix.min(), matrix.max())
            self._lut_cbar.update_normal(self._lut_im)
            self._lut_ax.set_title(
                f"Speed LUT  |  offset={p['lut_offset']:.2f}  scale×{p['lut_scale']:.2f}  "
                f"N={len(p.get('lut_gaussians', []))} gaussians",
                fontsize=8,
            )
            self._lut_canvas.draw_idle()
        except Exception:
            pass

    # ── Config generation ──────────────────────────────────────────────────────

    def _build_current_params(self) -> dict:
        p = deepcopy(self._params)
        for key, var in self._param_vars.items():
            try:
                p[key] = var.get()
            except Exception:
                pass
        for attr, key in [
            ("_lut_offset_var",  "lut_offset"),
            ("_lut_scale_var",   "lut_scale"),
            ("_lat_min_var",     "lat_range_min"),
            ("_lat_max_var",     "lat_range_max"),
            ("_ap_min_var",      "ap_range_min"),
            ("_ap_max_var",      "ap_range_max"),
        ]:
            try:
                p[key] = getattr(self, attr).get()
            except Exception:
                pass
        p["camera_gamma"]  = self._gamma_var.get()  if self._gamma_en_var.get()  else None
        p["camera2_gamma"] = self._gamma2_var.get() if self._gamma2_en_var.get() else None
        p["is_operant"]    = self._is_operant_var.get()
        return p

    def _on_save_profile(self):
        name = self._mouse_var.get()
        p = self._build_current_params()
        self._profiles[name] = deepcopy(p)
        save_mouse_profile(name, p)
        messagebox.showinfo("Saved", f"Profile '{name}' saved to:\n{MICE_DIR / name}.json", parent=self)

    def _on_generate_config(self):
        try:
            self._generate_config()
            messagebox.showinfo("Config Generated",
                                f"Config files written to:\n{LOCAL_DIR}", parent=self)
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self)

    def _generate_config(self):
        p         = self._build_current_params()
        mouse_name = self._mouse_var.get()

        # 1. LUT image
        lut_path = LOCAL_DIR / "2d_gaussian.tiff"
        save_lut_image(p, lut_path)

        # 2. Session JSON – use a microsecond-precision session_name so two runs in
        #    the same second still produce different data folders in Bonsai.
        from aind_behavior_services.session import Session as _Session
        now          = datetime.datetime.now(tz=datetime.timezone.utc)
        session_name = f"{mouse_name}_{now.strftime('%Y-%m-%dT%H%M%S.%f')}Z"
        session = _Session(
            date=now,
            session_name=session_name,
            experiment="Isometric Task",
            subject=mouse_name,
            notes=self._notes_var.get(),
            allow_dirty_repo=True,
            skip_hardware_validation=False,
            experimenter=[self._exp_var.get()],
        )
        with open(LOCAL_DIR / "Session.json", "w", encoding="utf-8") as f:
            f.write(session.model_dump_json(indent=2))

        # 3. TaskLogic JSON via pydantic models
        self._generate_task_logic(p, LOCAL_DIR / "AindBehaviorTelekinesisTaskLogic.json")

        # 4. Patch per-mouse camera settings into all cameras in rig JSON
        rig_path = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"
        if rig_path.exists():
            with open(rig_path, encoding="utf-8") as f:
                rig_json = json.load(f)
            cameras = (rig_json.get("triggered_camera_controller", {})
                               .get("cameras", {}))
            for i, cam in enumerate(cameras.values()):
                if i == 0:
                    cam["exposure"] = int(p.get("camera_exposure", 10000))
                    cam["gain"]     = float(p.get("camera_gain", 18.0))
                    cam["gamma"]    = p.get("camera_gamma")
                else:
                    cam["exposure"] = int(p.get("camera2_exposure", 10000))
                    cam["gain"]     = float(p.get("camera2_gain", 18.0))
                    cam["gamma"]    = p.get("camera2_gamma")
            with open(rig_path, "w", encoding="utf-8") as f:
                json.dump(rig_json, f, indent=2)

    def _generate_task_logic(self, p: dict, path: Path):
        import aind_behavior_telekinesis.task_logic as tl  # noqa: F401 (keep local import)

        far_pos   = p["far_position"]
        close_pos = p["close_position"]
        lut_path  = str(LOCAL_DIR / "2d_gaussian.tiff")

        prototype_trial = tl.Action(
            reward_probability=tl.scalar_value(1),
            reward_amount=tl.scalar_value(p["reward_size"]),
            reward_delay=tl.scalar_value(0),
            action_duration=tl.scalar_value(0.1),
            is_operant=bool(p.get("is_operant", False)),
            time_to_collect=tl.scalar_value(p["lick_response_time"]),
            lower_action_threshold=tl.scalar_value(0),
            upper_action_threshold=tl.scalar_value(1),
            continuous_feedback=tl.ManipulatorFeedback(
                converter_lut_input=[0, 1],
                converter_lut_output=[far_pos, close_pos],
            ),
        )

        task_logic = tl.AindBehaviorTelekinesisTaskLogic(
            task_parameters=tl.AindTelekinesisTaskParameters(
                rng_seed=None,
                environment=tl.Environment(
                    block_statistics=[
                        tl.BlockGenerator(
                            block_size=tl.scalar_value(1000),
                            trial_statistics=tl.Trial(
                                inter_trial_interval=tl.scalar_value(p["inter_trial_interval"]),
                                quiescence_period=tl.QuiescencePeriod(
                                    duration=tl.scalar_value(p.get("quiescence_duration", 0.5)),
                                    action_threshold=p.get("quiescence_threshold", 1),
                                ),
                                response_period=tl.ResponsePeriod(
                                    duration=tl.scalar_value(p["trial_length"]),
                                    has_cue=True,
                                    action=prototype_trial,
                                ),
                                action_source_0=tl.LoadCellActionSource(channel=0),
                                action_source_1=tl.LoadCellActionSource(channel=1),
                                sampler=tl.LutSampler2D(lut_reference="gaussian_2d"),
                            ),
                        )
                    ]
                ),
                operation_control=tl.OperationControl(
                    action_luts={
                        "gaussian_2d": tl.ActionLookUpTableFactory(
                            path=lut_path,
                            offset=0,
                            scale=p["lut_scale"],
                            action0_max=p["lat_range_max"],
                            action0_min=p["lat_range_min"],
                            action1_max=p["ap_range_max"],
                            action1_min=p["ap_range_min"],
                        )
                    },
                    spout=tl.SpoutOperationControl(
                        default_retracted_position=far_pos,
                        default_extended_position=close_pos,
                        enabled=False,
                    ),
                ),
            )
        )

        with open(path, "w", encoding="utf-8") as f:
            f.write(task_logic.model_dump_json(indent=2))

    def _on_start_task(self):
        try:
            self._generate_config()
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc), parent=self)
            return
        # Auto-save the profile so camera settings and other changes persist
        name = self._mouse_var.get()
        p    = self._build_current_params()
        self._profiles[name] = deepcopy(p)
        save_mouse_profile(name, p)

        run_bat = PROJECT_ROOT / "set_up_task" / "run.bat"
        if run_bat.exists():
            subprocess.Popen([str(run_bat)], shell=True, cwd=str(PROJECT_ROOT))
        else:
            messagebox.showwarning(
                "Launch Failed",
                f"Config written to {LOCAL_DIR}\nbut run.bat not found.\n"
                "Please launch Bonsai manually.",
                parent=self,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 2 – Live Monitor
# ═══════════════════════════════════════════════════════════════════════════════

class MonitorTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._subscriber: ZmqSubscriberThread | None = None
        self._event_queue: queue.Queue               = queue.Queue()
        self._trial_num:  int                        = 0
        self._resp_start: float | None               = None
        # trial_num → {"response_start": float, "reward_ts": float|None, "miss": bool}
        self._records: dict = {}
        self._build_ui()
        self._poll_queue()   # start the 100ms polling loop

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Connection bar ─────────────────────────────────────────────────────
        cf = ttk.LabelFrame(self, text="ZMQ Connection")
        cf.pack(fill="x", padx=6, pady=(6, 3))

        row = ttk.Frame(cf)
        row.pack(fill="x", padx=4, pady=4)

        self._host_var  = tk.StringVar(value="localhost")
        self._port_var  = tk.StringVar(value="5556")
        self._topic_var = tk.StringVar(value="Telekinesis")

        for label, var, width in [
            ("Host:",  self._host_var,  11),
            ("Port:",  self._port_var,   6),
            ("Topic:", self._topic_var, 12),
        ]:
            ttk.Label(row, text=label).pack(side="left")
            ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(2, 8))

        self._conn_btn = ttk.Button(row, text="Connect", command=self._toggle_connection)
        self._conn_btn.pack(side="left")

        self._status_lbl = ttk.Label(row, text="●  Disconnected", foreground="gray")
        self._status_lbl.pack(side="left", padx=10)

        self._debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Raw Debug", variable=self._debug_var).pack(side="left", padx=8)

        ttk.Button(row, text="Clear Data", command=self._clear_data).pack(side="right", padx=4)

        # ── Main area: plot + stats ────────────────────────────────────────────
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=6, pady=3)

        plot_lf = ttk.LabelFrame(mid, text="Time to Reward per Trial")
        plot_lf.pack(side="left", fill="both", expand=True)

        self._fig = Figure(figsize=(8, 4.2), tight_layout=True)
        self._ax  = self._fig.add_subplot(111)
        self._ax.set_xlabel("Trial Number")
        self._ax.set_ylabel("Time to Reward (s)")
        self._ax.grid(True, alpha=0.3, linestyle="--")
        self._ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

        # Plot objects: filled hit dots, miss crosses, rolling average
        (self._hit_line,)  = self._ax.plot([], [], "o", color="#1565c0", ms=5, alpha=0.7,  label="Hit")
        (self._miss_line,) = self._ax.plot([], [], "x", color="#c62828", ms=8, mew=2.0,    label="Miss / Timeout")
        (self._roll_line,) = self._ax.plot([], [], "-", color="#e65100", lw=2, alpha=0.9,  label="Rolling avg (10)")
        self._ax.legend(loc="upper right", fontsize=8)

        self._plot_canvas = FigureCanvasTkAgg(self._fig, master=plot_lf)
        self._plot_canvas.get_tk_widget().pack(fill="both", expand=True)

        # Stats panel
        stats_lf = ttk.LabelFrame(mid, text="Stats", width=170)
        stats_lf.pack(side="left", fill="y", padx=(4, 0))
        stats_lf.pack_propagate(False)

        self._stat_vars: dict = {}
        for key, label in [
            ("total",    "Total Trials"),
            ("hits",     "Hits"),
            ("misses",   "Misses"),
            ("hit_rate", "Hit Rate"),
            ("avg",      "Mean Time (s)"),
            ("med",      "Median Time (s)"),
            ("last",     "Last Time (s)"),
        ]:
            ttk.Label(stats_lf, text=label, font=("TkDefaultFont", 8)).pack(
                anchor="w", padx=6, pady=(5, 0))
            var = tk.StringVar(value="—")
            ttk.Label(stats_lf, textvariable=var, font=("TkDefaultFont", 14, "bold"),
                      foreground="#0d47a1").pack(anchor="w", padx=6, pady=(0, 2))
            self._stat_vars[key] = var

        # ── Event log ──────────────────────────────────────────────────────────
        log_lf = ttk.LabelFrame(self, text="Event Log")
        log_lf.pack(fill="x", padx=6, pady=(3, 6))

        self._log = tk.Text(log_lf, height=5, font=("Consolas", 8),
                            state="disabled", bg="#1e1e1e", fg="#d4d4d4",
                            insertbackground="white")
        log_scroll = ttk.Scrollbar(log_lf, command=self._log.yview)
        self._log.config(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self._log.pack(fill="x", expand=True)

    # ── Connection management ──────────────────────────────────────────────────

    def _toggle_connection(self):
        if self._subscriber and self._subscriber.is_alive():
            self._subscriber.stop()
            self._subscriber = None
            self._conn_btn.config(text="Connect")
            self._status_lbl.config(text="●  Disconnected", foreground="gray")
        else:
            try:
                host  = self._host_var.get().strip()
                port  = int(self._port_var.get().strip())
                topic = self._topic_var.get().strip()
            except ValueError:
                messagebox.showerror("Bad Settings", "Port must be an integer.", parent=self)
                return
            self._subscriber = ZmqSubscriberThread(
                host, port, topic, self._event_queue,
                debug=self._debug_var.get(),
            )
            self._subscriber.start()
            self._conn_btn.config(text="Disconnect")
            self._status_lbl.config(text="●  Connecting…", foreground="orange")

    # ── Queue polling (runs on main tkinter thread) ───────────────────────────

    def _poll_queue(self):
        try:
            while True:
                event_name, ts, payload = self._event_queue.get_nowait()
                self._handle_event(event_name, ts, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_event(self, name: str, ts: float, payload):
        # Internal control events
        if name == "__connected__":
            self._status_lbl.config(text="●  Connected", foreground="#2e7d32")
            self._log_event("__connected__", payload)
            return
        if name in ("__disconnected__", "__error__"):
            msg = f"●  {payload}" if name == "__error__" else "●  Disconnected"
            self._status_lbl.config(text=msg, foreground="#c62828")
            self._conn_btn.config(text="Connect")
            self._log_event(name, payload)
            return
        if name == "__raw__":
            self._log_event("RAW", payload)
            return
        if name == "__parse_err__":
            self._log_event("PARSE_ERR", payload)
            return

        self._log_event(name, payload)

        if name == "TrialNumber":
            try:
                self._trial_num = int(payload)
            except Exception:
                self._trial_num += 1

        elif name == "ResponsePeriod":
            self._resp_start = ts
            self._records[self._trial_num] = {
                "response_start": ts,
                "reward_ts": None,
                "miss": False,
            }

        elif name == "GiveReward":
            if self._resp_start is not None:
                rec = self._records.get(self._trial_num)
                if rec is not None:
                    rec["reward_ts"] = ts
                self._refresh_plot()
                self._refresh_stats()

        elif name == "IsValidTrial":
            # data=False means the trial timed out / was invalid (no reward collected)
            if payload is False:
                rec = self._records.get(self._trial_num)
                if rec is not None:
                    rec["miss"] = True
                self._refresh_plot()
                self._refresh_stats()

    # ── Plot and stats ─────────────────────────────────────────────────────────

    def _refresh_plot(self):
        sorted_recs = sorted(self._records.items())

        hit_xs = [t for t, r in sorted_recs if r.get("reward_ts") is not None]
        hit_ys = [r["reward_ts"] - r["response_start"]
                  for t, r in sorted_recs if r.get("reward_ts") is not None]
        miss_xs = [t for t, r in sorted_recs if r.get("miss")]

        self._hit_line.set_data(hit_xs, hit_ys)

        if miss_xs:
            self._miss_line.set_data(miss_xs, [0] * len(miss_xs))
        else:
            self._miss_line.set_data([], [])

        if len(hit_ys) >= 2:
            window = min(10, len(hit_ys))
            roll   = np.convolve(hit_ys, np.ones(window) / window, mode="valid")
            self._roll_line.set_data(hit_xs[window - 1:], roll)
        else:
            self._roll_line.set_data([], [])

        # Compute limits explicitly — calling set_ylim() disables autoscaling,
        # so relim()/autoscale_view() silently stops working after the first call.
        all_xs = hit_xs + miss_xs
        if all_xs:
            x_span = max(max(all_xs) - min(all_xs), 1)
            xpad   = x_span * 0.04
            self._ax.set_xlim(min(all_xs) - xpad, max(all_xs) + xpad)
        if hit_ys:
            ypad = max(max(hit_ys) * 0.08, 0.5)
            self._ax.set_ylim(0, max(hit_ys) + ypad)

        self._plot_canvas.draw_idle()

    def _refresh_stats(self):
        hit_times = [
            r["reward_ts"] - r["response_start"]
            for r in self._records.values()
            if r.get("reward_ts") is not None
        ]
        n_miss  = sum(1 for r in self._records.values() if r.get("miss"))
        n_hits  = len(hit_times)
        total   = n_hits + n_miss

        self._stat_vars["total"].set(str(total))
        self._stat_vars["hits"].set(str(n_hits))
        self._stat_vars["misses"].set(str(n_miss))
        self._stat_vars["hit_rate"].set(f"{n_hits / total * 100:.1f}%" if total else "—")
        self._stat_vars["avg"].set(f"{np.mean(hit_times):.2f}" if hit_times else "—")
        self._stat_vars["med"].set(f"{np.median(hit_times):.2f}" if hit_times else "—")
        self._stat_vars["last"].set(f"{hit_times[-1]:.2f}" if hit_times else "—")

    def _log_event(self, name: str, payload):
        ts   = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {name}: {str(payload)[:120]}\n"
        self._log.config(state="normal")
        self._log.insert(tk.END, line)
        # keep log bounded to ~300 lines
        if int(self._log.index(tk.END).split(".")[0]) > 310:
            self._log.delete("1.0", "50.0")
        self._log.see(tk.END)
        self._log.config(state="disabled")

    def _clear_data(self):
        self._records.clear()
        self._trial_num  = 0
        self._resp_start = None
        for v in self._stat_vars.values():
            v.set("—")
        for line in (self._hit_line, self._miss_line, self._roll_line):
            line.set_data([], [])
        self._plot_canvas.draw_idle()
        self._log.config(state="normal")
        self._log.delete("1.0", tk.END)
        self._log.config(state="disabled")


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 3 – Rig Configuration
# ═══════════════════════════════════════════════════════════════════════════════

class RigTab(ttk.Frame):
    """Edit global hardware settings and write them back to AindBehaviorTelekinesisRig.json."""

    RIG_PATH = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"

    MOTOR_MODES = {"QUIET": 0, "DYNAMIC": 1}

    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()
        self._load_rig()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="both", expand=True, padx=8, pady=6)

        # ── Left column ────────────────────────────────────────────────────────
        left = ttk.Frame(top)
        left.pack(side="left", fill="y", padx=(0, 8))

        # Device ports
        pg = ttk.LabelFrame(left, text="Device COM Ports")
        pg.pack(fill="x", pady=(0, 6))
        self._port_vars: dict = {}
        for key, label, default in [
            ("port_behavior",    "Harp Behavior:",    "COM3"),
            ("port_load_cells",  "Harp Load Cells:",  "COM4"),
            ("port_lickometer",  "Harp Lickometer:",  "COM6"),
            ("port_clock",       "Harp Clock:",       "COM9"),
            ("port_manipulator", "Manipulator:",      "COM10"),
        ]:
            row = ttk.Frame(pg)
            row.pack(fill="x", pady=2, padx=4)
            ttk.Label(row, text=label, width=18, anchor="e").pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Entry(row, textvariable=var, width=10).pack(side="left", padx=(4, 0))
            self._port_vars[key] = var

        # Motor mode
        mg = ttk.LabelFrame(left, text="Manipulator (Y1)")
        mg.pack(fill="x", pady=(0, 6))
        row = ttk.Frame(mg)
        row.pack(fill="x", padx=4, pady=4)
        ttk.Label(row, text="Operation Mode:", width=18, anchor="e").pack(side="left")
        self._motor_mode_var = tk.StringVar(value="QUIET")
        ttk.Combobox(row, textvariable=self._motor_mode_var,
                     values=list(self.MOTOR_MODES.keys()),
                     state="readonly", width=10).pack(side="left", padx=(4, 0))

        # Load cell calibration channels
        lcg = ttk.LabelFrame(left, text="Load Cell Calibration Channels")
        lcg.pack(fill="x", pady=(0, 6))
        hdr = ttk.Frame(lcg)
        hdr.pack(fill="x", padx=4, pady=(2, 0))
        ttk.Label(hdr, text="",        width=2).pack(side="left")
        ttk.Label(hdr, text="Ch",      width=3, font=("TkDefaultFont", 8, "bold")).pack(side="left")
        ttk.Label(hdr, text="Offset",  width=7, font=("TkDefaultFont", 8, "bold")).pack(side="left")
        ttk.Label(hdr, text="Baseline",width=9, font=("TkDefaultFont", 8, "bold")).pack(side="left")
        ttk.Label(hdr, text="Slope",   width=7, font=("TkDefaultFont", 8, "bold")).pack(side="left")
        self._lc_channel_vars: list[dict] = []
        for _ch in range(8):
            _row = ttk.Frame(lcg)
            _row.pack(fill="x", padx=4, pady=1)
            _en  = tk.BooleanVar(value=_ch < 3)
            ttk.Checkbutton(_row, variable=_en).pack(side="left")
            ttk.Label(_row, text=str(_ch), width=3).pack(side="left")
            _off = tk.IntVar(value=0)
            ttk.Spinbox(_row, from_=-255, to=255, textvariable=_off, width=6).pack(side="left")
            _base = tk.StringVar(value="0.0")
            ttk.Entry(_row, textvariable=_base, width=8).pack(side="left", padx=(2, 0))
            _slope = tk.StringVar(value="1.0")
            ttk.Entry(_row, textvariable=_slope, width=7).pack(side="left", padx=(2, 0))
            self._lc_channel_vars.append({
                "enabled": _en, "offset": _off,
                "baseline": _base, "slope": _slope,
            })

        # Networking
        ng = ttk.LabelFrame(left, text="Networking (ZMQ)")
        ng.pack(fill="x", pady=(0, 6))
        self._zmq_conn_var  = self._entry_row(ng, "Connection:", "@tcp://localhost:5556")
        self._zmq_topic_var = self._entry_row(ng, "Topic:",      "Telekinesis")

        # Data
        dg = ttk.LabelFrame(left, text="Output")
        dg.pack(fill="x", pady=(0, 6))
        self._data_dir_var = self._entry_row(dg, "Data Directory:", "C:\\Data")

        # ── Right column ───────────────────────────────────────────────────────
        right = ttk.Frame(top)
        right.pack(side="left", fill="y")

        # Main camera (global settings — not per-mouse)
        cg = ttk.LabelFrame(right, text="Main Camera (Global Settings)")
        cg.pack(fill="x", pady=(0, 6))
        self._cam_name_var   = self._entry_row(cg, "Camera Name:", "MainCamera")
        self._cam_serial_var = self._entry_row(cg, "Serial Number:", "25312141")
        row = ttk.Frame(cg)
        row.pack(fill="x", pady=2, padx=4)
        ttk.Label(row, text="Frame Rate (fps):", width=20, anchor="e").pack(side="left")
        self._cam_fps_var = tk.DoubleVar(value=80)
        ttk.Spinbox(row, from_=1, to=500, increment=5,
                    textvariable=self._cam_fps_var, width=9, format="%.0f").pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(cg)
        row2.pack(fill="x", pady=2, padx=4)
        ttk.Label(row2, text="Binning:", width=20, anchor="e").pack(side="left")
        self._cam_bin_var = tk.IntVar(value=2)
        ttk.Spinbox(row2, from_=1, to=8, increment=1,
                    textvariable=self._cam_bin_var, width=9).pack(side="left", padx=(4, 0))

        # Second camera
        c2g = ttk.LabelFrame(right, text="Second Camera")
        c2g.pack(fill="x", pady=(0, 6))
        en_row = ttk.Frame(c2g)
        en_row.pack(fill="x", pady=2, padx=4)
        self._cam2_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(en_row, text="Enable second camera",
                        variable=self._cam2_enabled_var).pack(side="left")
        self._cam2_name_var   = self._entry_row(c2g, "Camera Name:", "SecondCamera")
        self._cam2_serial_var = self._entry_row(c2g, "Serial Number:", "25380286")

        # Info label
        info = ttk.Label(right,
            text="Exposure, gain, and gamma are per-mouse\n"
                 "settings — edit them in the Task Config tab.",
            foreground="gray", justify="left")
        info.pack(anchor="w", padx=4, pady=(4, 0))

        # ── Bottom buttons ─────────────────────────────────────────────────────
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Button(bf, text="Reload from Rig JSON", command=self._load_rig).pack(side="left")
        ttk.Button(bf, text="Save Rig JSON",         command=self._on_save).pack(side="left", padx=6)
        self._status_lbl = ttk.Label(bf, text="", foreground="gray")
        self._status_lbl.pack(side="left", padx=8)

    @staticmethod
    def _entry_row(parent, label: str, default: str) -> tk.StringVar:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2, padx=4)
        ttk.Label(row, text=label, width=20, anchor="e").pack(side="left")
        var = tk.StringVar(value=default)
        ttk.Entry(row, textvariable=var, width=22).pack(side="left", padx=(4, 0))
        return var

    # ── Load / Save ────────────────────────────────────────────────────────────

    def _load_rig(self):
        if not self.RIG_PATH.exists():
            self._status_lbl.config(text=f"Not found: {self.RIG_PATH.name}", foreground="red")
            return
        try:
            with open(self.RIG_PATH, encoding="utf-8") as f:
                rig = json.load(f)

            def _port(section):
                return rig.get(section, {}).get("port_name", "")

            self._port_vars["port_behavior"].set(_port("harp_behavior"))
            self._port_vars["port_load_cells"].set(_port("harp_load_cells"))
            self._port_vars["port_lickometer"].set(_port("harp_lickometer"))
            self._port_vars["port_clock"].set(_port("harp_clock_generator"))
            self._port_vars["port_manipulator"].set(_port("manipulator"))

            # Motor mode: read Y1 axis (axis==2) from axis_configuration
            ax_cfg = rig.get("manipulator", {}).get("calibration", {}).get("axis_configuration", [])
            y1_cfg = next((a for a in ax_cfg if a.get("axis") == 2), None)
            if y1_cfg is not None:
                mode_int = y1_cfg.get("motor_operation_mode", 0)
                mode_str = "DYNAMIC" if mode_int == 1 else "QUIET"
                self._motor_mode_var.set(mode_str)

            zmq = rig.get("networking", {}).get("zmq_publisher", {})
            self._zmq_conn_var.set(zmq.get("connection_string", "@tcp://localhost:5556"))
            self._zmq_topic_var.set(zmq.get("topic", "Telekinesis"))

            self._data_dir_var.set(rig.get("data_directory", "C:\\Data"))

            cam_ctrl  = rig.get("triggered_camera_controller", {})
            cameras   = cam_ctrl.get("cameras", {})
            cam_items = list(cameras.items())
            if cam_items:
                cam1_name, cam1 = cam_items[0]
                self._cam_name_var.set(cam1_name)
                self._cam_serial_var.set(cam1.get("serial_number", ""))
                self._cam_fps_var.set(cam_ctrl.get("frame_rate", 80))
                self._cam_bin_var.set(cam1.get("binning", 2))
            else:
                self._cam_fps_var.set(cam_ctrl.get("frame_rate", 80))

            if len(cam_items) >= 2:
                cam2_name, cam2 = cam_items[1]
                self._cam2_enabled_var.set(True)
                self._cam2_name_var.set(cam2_name)
                self._cam2_serial_var.set(cam2.get("serial_number", "25380286"))
            else:
                self._cam2_enabled_var.set(False)

            lc_channels = (rig.get("harp_load_cells") or {}).get("calibration", {}).get("channels", [])
            ch_map = {entry.get("channel"): entry for entry in lc_channels if entry.get("channel") is not None}
            for ch_idx, ch_vars in enumerate(self._lc_channel_vars):
                entry = ch_map.get(ch_idx)
                if entry:
                    ch_vars["enabled"].set(True)
                    ch_vars["offset"].set(int(entry.get("offset", 0)))
                    ch_vars["baseline"].set(str(entry.get("baseline", 0.0)))
                    ch_vars["slope"].set(str(entry.get("slope", 1.0)))
                else:
                    ch_vars["enabled"].set(False)
                    ch_vars["offset"].set(0)
                    ch_vars["baseline"].set("0.0")
                    ch_vars["slope"].set("1.0")

            self._status_lbl.config(text="Loaded.", foreground="gray")
        except Exception as exc:
            self._status_lbl.config(text=f"Load error: {exc}", foreground="red")

    def _on_save(self):
        try:
            self._save_rig()
            self._status_lbl.config(text="Saved.", foreground="#2e7d32")
        except Exception as exc:
            self._status_lbl.config(text=f"Error: {exc}", foreground="red")
            messagebox.showerror("Rig Save Error", str(exc), parent=self)

    def _save_rig(self):
        if not self.RIG_PATH.exists():
            raise FileNotFoundError(f"{self.RIG_PATH} not found")
        with open(self.RIG_PATH, encoding="utf-8") as f:
            rig = json.load(f)

        def _set_port(section, value):
            if section in rig and rig[section] is not None:
                rig[section]["port_name"] = value

        _set_port("harp_behavior",       self._port_vars["port_behavior"].get())
        _set_port("harp_load_cells",     self._port_vars["port_load_cells"].get())
        _set_port("harp_lickometer",     self._port_vars["port_lickometer"].get())
        _set_port("harp_clock_generator", self._port_vars["port_clock"].get())
        _set_port("manipulator",         self._port_vars["port_manipulator"].get())

        mode_int = self.MOTOR_MODES.get(self._motor_mode_var.get(), 0)
        ax_cfg   = rig.get("manipulator", {}).get("calibration", {}).get("axis_configuration", [])
        for ax in ax_cfg:
            ax["motor_operation_mode"] = mode_int  # apply to all axes

        zmq = rig.setdefault("networking", {}).setdefault("zmq_publisher", {})
        zmq["connection_string"] = self._zmq_conn_var.get()
        zmq["topic"]             = self._zmq_topic_var.get()

        rig["data_directory"] = self._data_dir_var.get()

        lc = rig.get("harp_load_cells")
        if lc is not None:
            cal = lc.setdefault("calibration", {})
            cal["channels"] = [
                {
                    "channel":  ch_idx,
                    "offset":   int(ch_vars["offset"].get()),
                    "baseline": float(ch_vars["baseline"].get()),
                    "slope":    float(ch_vars["slope"].get()),
                }
                for ch_idx, ch_vars in enumerate(self._lc_channel_vars)
                if ch_vars["enabled"].get()
            ]

        cam_ctrl = rig.setdefault("triggered_camera_controller", {})
        old_cameras = cam_ctrl.get("cameras", {})
        old_items   = list(old_cameras.items())

        cam1_name = self._cam_name_var.get().strip() or "MainCamera"
        # Preserve existing camera data for cam1 (use old first key data as base)
        cam1 = dict(old_items[0][1]) if old_items else {}
        cam1["serial_number"]  = self._cam_serial_var.get()
        cam1["binning"]        = int(self._cam_bin_var.get())
        cam_ctrl["frame_rate"] = int(self._cam_fps_var.get())

        new_cameras: dict = {cam1_name: cam1}

        if self._cam2_enabled_var.get():
            cam2_name = self._cam2_name_var.get().strip() or "SecondCamera"
            cam2 = dict(old_items[1][1]) if len(old_items) >= 2 else deepcopy(cam1)
            cam2["serial_number"] = self._cam2_serial_var.get()
            new_cameras[cam2_name] = cam2

        cam_ctrl["cameras"] = new_cameras

        with open(self.RIG_PATH, "w", encoding="utf-8") as f:
            json.dump(rig, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# Application entry point
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telekinesis Task Setup")
        self.geometry("1300x900")
        self.minsize(1050, 700)

        style = ttk.Style(self)
        style.theme_use("clam")
        # Slightly larger default font for readability
        style.configure(".", font=("TkDefaultFont", 9))
        style.configure("TLabelframe.Label", font=("TkDefaultFont", 9, "bold"))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=4, pady=4)

        self._config_tab  = ConfigTab(nb)
        self._monitor_tab = MonitorTab(nb)
        self._rig_tab     = RigTab(nb)
        nb.add(self._config_tab,  text="   Task Configuration   ")
        nb.add(self._monitor_tab, text="   Live Monitor   ")
        nb.add(self._rig_tab,     text="   Rig Config   ")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # Stop any running ZMQ subscriber cleanly
        sub = getattr(self._monitor_tab, "_subscriber", None)
        if sub and sub.is_alive():
            sub.stop()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
