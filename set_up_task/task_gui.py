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
import io
import json
import re
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import copy
import matplotlib.ticker as ticker
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from PIL import Image

# Colormap for instantaneous mode: viridis for [0,1], vivid red above threshold
_POS_CMAP = copy.copy(matplotlib.colormaps["viridis"])
_POS_CMAP.set_over("#d32f2f")

import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

# ── High-DPI awareness on Windows ─────────────────────────────────────────────
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
LOCAL_DIR    = PROJECT_ROOT / "local"


def _install_crash_handlers(app: "tk.Tk", log_tab: "LogTab") -> None:
    """Show unhandled exceptions in the Log tab and switch to it automatically."""

    def _show(text: str) -> None:
        try:
            log_tab.append(text)
            idx = log_tab._tab_index()
            if idx is not None:
                log_tab._nb.select(idx)
        except Exception:
            pass

    def _cb_exc(exc_type, exc_val, exc_tb):
        # Already on the main thread — call directly, no after() needed.
        msg = (f"\n{'='*60}\n{datetime.datetime.now()}  [callback]\n"
               + "".join(traceback.format_exception(exc_type, exc_val, exc_tb)))
        _show(msg)

    def _thread_exc(args):
        # Background thread — must schedule on the event loop.
        msg = (f"\n{'='*60}\n{datetime.datetime.now()}  [thread: {args.thread.name}]\n"
               + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        try:
            app.after(0, lambda: _show(msg))
        except Exception:
            pass

    # Belt-and-suspenders: tee sys.stderr so anything written there by
    # third-party code (or the Python default excepthook) also appears.
    _orig_stderr = sys.stderr

    class _StderrTee:
        def write(self, text: str) -> None:
            _orig_stderr.write(text)
            if text and text.strip():
                try:
                    app.after(0, lambda t=text: log_tab.append(t))
                except Exception:
                    pass

        def flush(self) -> None:
            _orig_stderr.flush()

        def fileno(self) -> int:
            return _orig_stderr.fileno()

    sys.stderr = _StderrTee()

    app.report_callback_exception = _cb_exc
    threading.excepthook = _thread_exc


MICE_DIR     = LOCAL_DIR / "mice"
MICE_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_DIR  = LOCAL_DIR / "presets"
PRESETS_DIR.mkdir(parents=True, exist_ok=True)

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
    "action_duration":        0.1,
    "is_operant":             False,
    "instantaneous_mode":     False,
    "motor_feedback":         True,
    "experimenter":           "rozmar",
    "notes":                  "",
    "lut_gaussians": [
        {"center_lat":  750, "center_ap": -750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
        {"center_lat":  750, "center_ap":  750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
        {"center_lat": -750, "center_ap": -750, "sigma_lat": 200, "sigma_ap": 200, "peak": 5.0, "trough": 0.0},
    ],
    "lut_steps": [],
    "trial_number":          100,
    "lut_offset":    -0.05,
    "lut_scale":      2.5,
    "lat_range_min": -2000.0,
    "lat_range_max":  2000.0,
    "ap_range_min":  -2000.0,
    "ap_range_max":   2000.0,
    # Camera parameters — per mouse
    # Mouse position on rig — per mouse, not in LUT presets
    "x_position":       0.0,
    "z_position":       0.0,
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
    "rig_name":         "Behavior_0",
    "backup_root":      r"Z:\Data\Behavior",
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


def step_2d(x_lat, x_ap, peak, trough, center_lat, center_ap, width_lat, width_ap):
    inside = (
        (np.abs(x_lat[np.newaxis, :] - center_lat) <= width_lat / 2) &
        (np.abs(x_ap[:, np.newaxis] - center_ap) <= width_ap / 2)
    )
    return np.where(inside, peak, trough).astype(float)


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
    for s in params.get("lut_steps", []):
        try:
            matrix += step_2d(
                lat_vec, ap_vec,
                s["peak"], s["trough"],
                s["center_lat"], s["center_ap"],
                s["width_lat"],  s["width_ap"],
            )
        except Exception:
            pass
    matrix += params.get("lut_offset", 0.0)
    return matrix, lat_vec, ap_vec


def save_lut_image(params: dict, path: Path) -> tuple:
    """Save LUT as float32 TIFF with values in [0, 512].

    The display matrix (matrix * lut_scale) is normalised so its minimum maps to 0
    and its maximum maps to 512.  converter_lut_input=[0, 512] in Bonsai matches
    this range directly — no offset/scale encoding needed (offset=0, scale=1).
    """
    matrix, _, _ = compute_lut_matrix(params)
    display = matrix * params.get("lut_scale", 1.0)
    tiff_max = float(display.max())
    if tiff_max > 0:
        float32 = (display / tiff_max * 512).astype(np.float32)
    else:
        float32 = np.zeros_like(display, dtype=np.float32)
    Image.fromarray(float32).save(str(path))
    return 0.0, 1.0, tiff_max  # offset, scale, lut_max


# ── Load-cell calibration ─────────────────────────────────────────────────────

_LC_CAL_CSV = Path(r"Z:\NDNF_metadata\NDNF experimenters_Calibration.csv")
_LAT_DIRS   = {"LR", "RL"}
_AP_DIRS    = {"AP", "PA"}


def load_lc_calibration(rig_name: str) -> "dict | None":
    """Read the calibration CSV for the given rig.

    Returns a dict with keys:
      'date'  – calibration date string (e.g. '2026/07/16')
      'lat'   – (vals_arr, g_arr) for the lateral axis
      'ap'    – (vals_arr, g_arr) for the AP axis
      'axes'  – list of dicts per axis:
                  {idx, direction, slope (g/au), baseline (au), vals, g}
    Returns None if the rig is not found or the CSV is unreadable.
    """
    import csv
    try:
        rows: list[dict] = []
        with open(_LC_CAL_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
        rows = [r for r in rows
                if r.get("Rig ID", "").strip() == rig_name
                and "loadcell" in r.get("Device name", "").lower()]
        if not rows:
            return None

        def _parse_date(r):
            try:
                return datetime.datetime.strptime(r.get("Calibration date", ""), "%Y/%m/%d")
            except Exception:
                return datetime.datetime.min

        row = max(rows, key=_parse_date)
        date_str = row.get("Calibration date", "").strip()
        result: dict = {"date": date_str, "axes": []}
        for axis_idx in range(3):
            direction = row.get(f"Axis {axis_idx} direction", "").strip().upper()
            try:
                g_arr = np.array([float(x) for x in
                                  row.get(f"Axis {axis_idx} g", "").strip("[]").split(",")])
                v_arr = np.array([float(x) for x in
                                  row.get(f"Axis {axis_idx} vals", "").strip("[]").split(",")])
            except Exception:
                result["axes"].append({"idx": axis_idx, "direction": direction})
                continue
            # Linear fit: g = slope * val + intercept  →  baseline = -intercept / slope
            slope_fit, intercept_fit = np.polyfit(v_arr, g_arr, 1)
            baseline_fit = -intercept_fit / slope_fit if slope_fit != 0 else 0.0
            result["axes"].append({
                "idx": axis_idx, "direction": direction,
                "slope": slope_fit, "baseline": baseline_fit,
                "vals": v_arr, "g": g_arr,
            })
            if direction in _LAT_DIRS:
                result["lat"] = (v_arr, g_arr)
            elif direction in _AP_DIRS:
                result["ap"] = (v_arr, g_arr)
        return result if ("lat" in result or "ap" in result) else None
    except Exception:
        return None


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


# ── Preset I/O ─────────────────────────────────────────────────────────────────

def load_presets() -> dict:
    presets = {}
    for f in sorted(PRESETS_DIR.glob("*.json")):
        try:
            with open(f) as fp:
                presets[f.stem] = json.load(fp)
        except Exception:
            pass
    return presets


def save_preset(name: str, params: dict):
    with open(PRESETS_DIR / f"{name}.json", "w") as f:
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
# Backup
# ═══════════════════════════════════════════════════════════════════════════════

class BackupDaemon(threading.Thread):
    """Periodically rsyncs all session folders to the backup drive using robocopy."""

    def __init__(self, data_dir: Path, rig_name: str, backup_root: str,
                 interval_s: int, log_cb, status_cb):
        super().__init__(daemon=True)
        self._data_dir    = Path(data_dir)
        self._rig_name    = rig_name
        self._backup_root = Path(backup_root)
        self._interval    = interval_s
        self._log_cb      = log_cb
        self._status_cb   = status_cb
        self._stop        = threading.Event()

    def run(self):
        self._status_cb("Running")
        while not self._stop.is_set():
            self._sync_all()
            ts   = datetime.datetime.now().strftime("%H:%M")
            mins = self._interval // 60
            self._status_cb(f"Last sync: {ts}  ·  next in {mins} min")
            self._stop.wait(self._interval)
        self._status_cb("Stopped")

    def _sync_all(self):
        if not self._data_dir.exists():
            self._log_cb(f"[ERROR] Data directory not found: {self._data_dir}\n")
            return
        sessions = []
        for d in sorted(self._data_dir.iterdir()):
            if not d.is_dir():
                continue
            if not (d / "behavior").is_dir():
                continue
            # Everything before the date stamp is the subject (e.g. "human_11_2025-07-09T...")
            m = re.search(r'_\d{4}-\d{2}-\d{2}', d.name)
            subject = d.name[:m.start()] if m else d.name
            if "test" in subject.lower():
                continue
            sessions.append((d, subject))

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_cb(f"\n[{ts}] Syncing {len(sessions)} session(s)\n")
        for session_path, subject in sessions:
            if self._stop.is_set():
                break
            dst = self._backup_root / self._rig_name / subject / session_path.name
            self._log_cb(f"  {session_path.name}\n    → {dst}\n")
            try:
                dst.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    ["robocopy", str(session_path), str(dst),
                     "/E", "/Z", "/NP", "/NDL", "/NFL", "/R:3", "/W:5"],
                    capture_output=True, text=True, timeout=7200,
                )
                if proc.returncode < 8:
                    self._log_cb("    ✓ OK\n")
                else:
                    self._log_cb(f"    ✗ robocopy exit {proc.returncode}\n")
                    if proc.stdout.strip():
                        self._log_cb(f"    {proc.stdout.strip()[-400:]}\n")
            except Exception as exc:
                self._log_cb(f"    ✗ {exc}\n")

    def stop(self):
        self._stop.set()


class BackupTab(ttk.Frame):
    """Tab that controls the BackupDaemon."""

    def __init__(self, parent):
        super().__init__(parent)
        self._daemon: BackupDaemon | None = None
        self._queue: queue.Queue = queue.Queue()
        self._auto_var = tk.BooleanVar(value=False)
        self._interval_var = tk.IntVar(value=5)
        self._build_ui()
        self._poll()

    def _read_rig(self):
        rig_path = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"
        try:
            with open(rig_path, encoding="utf-8") as f:
                rig = json.load(f)
            return (
                Path(rig.get("data_directory", "C:\\Data")),
                rig.get("rig_name",    DEFAULT_RIG["rig_name"]),
                rig.get("backup_root", DEFAULT_RIG["backup_root"]),
            )
        except Exception:
            return Path("C:\\Data"), DEFAULT_RIG["rig_name"], DEFAULT_RIG["backup_root"]

    def _build_ui(self):
        _, rig_name, backup_root = self._read_rig()

        info = ttk.Frame(self)
        info.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(info, text="Rig:").pack(side="left")
        ttk.Label(info, text=rig_name,
                  font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=(2, 16))
        ttk.Label(info, text="Destination:").pack(side="left")
        ttk.Label(info, text=f"{backup_root}\\{rig_name}\\<mouse>\\<session>",
                  foreground="gray").pack(side="left", padx=(2, 0))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=10, pady=6)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Checkbutton(
            ctrl, text="Auto-backup", variable=self._auto_var,
            command=self._on_auto_toggle,
        ).pack(side="left")

        ttk.Label(ctrl, text="Interval (min):").pack(side="left", padx=(16, 0))
        ttk.Spinbox(ctrl, from_=1, to=120, increment=1,
                    textvariable=self._interval_var, width=5).pack(side="left", padx=(4, 12))

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(ctrl, textvariable=self._status_var, foreground="gray",
                  font=("TkDefaultFont", 8)).pack(side="left", padx=(4, 0))

        log_lf = ttk.LabelFrame(self, text="Log")
        log_lf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._log = tk.Text(log_lf, font=("Consolas", 8), state="disabled",
                            bg="#1e1e1e", fg="#d4d4d4", wrap="none")
        scy = ttk.Scrollbar(log_lf, command=self._log.yview)
        scx = ttk.Scrollbar(log_lf, orient="horizontal", command=self._log.xview)
        self._log.config(yscrollcommand=scy.set, xscrollcommand=scx.set)
        scy.pack(side="right", fill="y")
        scx.pack(side="bottom", fill="x")
        self._log.pack(fill="both", expand=True)

    def _on_auto_toggle(self):
        if self._auto_var.get():
            self._start()
        else:
            self._stop()

    def _start(self):
        if self._daemon and self._daemon.is_alive():
            return
        data_dir, rig_name, backup_root = self._read_rig()
        interval_s = max(1, self._interval_var.get()) * 60
        self._daemon = BackupDaemon(
            data_dir, rig_name, backup_root, interval_s,
            log_cb=lambda msg: self._queue.put(("log", msg)),
            status_cb=lambda s: self._queue.put(("status", s)),
        )
        self._daemon.start()

    def _stop(self):
        if self._daemon:
            self._daemon.stop()
            self._daemon = None

    def stop_daemon(self):
        """Call on app close."""
        self._stop()

    def _poll(self):
        try:
            while True:
                kind, text = self._queue.get_nowait()
                if kind == "status":
                    self._status_var.set(text)
                    if "Stopped" in text and not self._auto_var.get():
                        self._status_var.set("Idle")
                else:
                    self._log.config(state="normal")
                    self._log.insert(tk.END, text)
                    if int(self._log.index(tk.END).split(".")[0]) > 1200:
                        self._log.delete("1.0", "200.0")
                    self._log.see(tk.END)
                    self._log.config(state="disabled")
        except queue.Empty:
            pass
        except Exception:
            pass
        self.after(200, self._poll)


# ═══════════════════════════════════════════════════════════════════════════════
# Tab 1 – Task Configuration
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._profiles: dict         = load_mouse_profiles()
        self._presets: dict          = load_presets()
        self._params: dict           = deepcopy(DEFAULT_PARAMS)
        self._blocks: list           = [deepcopy({k: DEFAULT_PARAMS[k] for k in self._BLOCK_KEYS if k in DEFAULT_PARAMS})]
        self._current_block: int     = 0
        self._sel_gauss_idx: int | None = None
        self._gauss_loading: bool    = False   # guard against re-entrant gaussian trace
        self._sel_step_idx: int | None  = None
        self._step_loading: bool     = False
        self._params_loading: bool   = False   # guard: suppress param traces while loading a profile
        self._lc_calibration: "dict | None" = None
        self._lc_calibration_rig: "str | None" = None
        self._lc_calibration_time: float = 0.0
        self._lut_display:  "np.ndarray | None" = None
        self._lut_lat_disp: "np.ndarray | None" = None
        self._lut_ap_disp:  "np.ndarray | None" = None
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
        self._exp_var.trace_add("write", self._on_task_param_changed)
        self._notes_var.trace_add("write", self._on_task_param_changed)

        # Task parameters
        pg = ttk.LabelFrame(parent, text="Task Parameters")
        pg.pack(fill="x", pady=(0, 5))

        self._param_vars: dict = {}
        for key, label, default, lo, hi, step in [
            ("trial_number",          "Trial Number",          100,    1,    10000,  10),
            ("trial_length",          "Trial Length (s)",    1000.0,  1,    10000, 100.0),
            ("action_duration",       "Hold Duration (s)",      0.1,  0.0,     10,  0.05),
            ("lick_response_time",    "Lick Response (s)",      4.0,  0.1,     60,   0.5),
            ("inter_trial_interval",  "ITI (s)",                2.0,  0.1,     60,   0.5),
            ("reward_size",           "Reward (µL)",            1.0,  0.1,     20,   0.1),
            ("far_position",          "Far Position (mm)",      5.0,  0,       30,   0.5),
            ("close_position",        "Close Position (mm)",   14.5,  0,       30,   0.5),
            ("mouse_motor_hard_limit","Motor Limit (mm)",      15.0,  0,       30,   0.5),
            ("x_position",           "X Position (mm)",         0.0, -100,    100,   0.5),
            ("z_position",           "Z Position (mm)",         0.0, -100,    100,   0.5),
            ("quiescence_duration",  "Quiescence (s)",          0.5,  0.0,    60,   0.1),
            ("quiescence_threshold", "Quiescence Threshold",    1.0,  0.0,  1000,   0.5),
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

        # instantaneous_mode
        inst_row = ttk.Frame(pg)
        inst_row.pack(fill="x", pady=1, padx=4)
        ttk.Label(inst_row, text="Instantaneous Mode:", width=20, anchor="e").pack(side="left")
        self._instantaneous_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(inst_row, variable=self._instantaneous_mode_var,
                        command=lambda: (self._on_task_param_changed(),
                                        self._schedule_lut_update())).pack(side="left", padx=(4, 0))
        ttk.Label(inst_row, text="(use port position, not speed)",
                  foreground="gray", font=("TkDefaultFont", 7)).pack(side="left", padx=(6, 0))

        # motor_feedback
        mf_row = ttk.Frame(pg)
        mf_row.pack(fill="x", pady=1, padx=4)
        ttk.Label(mf_row, text="Motor Feedback:", width=20, anchor="e").pack(side="left")
        self._motor_feedback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(mf_row, variable=self._motor_feedback_var,
                        command=self._on_task_param_changed).pack(side="left", padx=(4, 0))
        ttk.Label(mf_row, text="(move spout with force signal)",
                  foreground="gray", font=("TkDefaultFont", 7)).pack(side="left", padx=(6, 0))

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
        # ttk.Label(bf,
        #           text="Camera settings are written to the rig JSON\n"
        #                "on Generate Config / Start Task, then take\n"
        #                "effect the next time Bonsai starts.",
        #           foreground="gray", justify="left", font=("TkDefaultFont", 7),
        #           ).pack(fill="x", padx=4, pady=(0, 2))
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
        # ── Presets + Blocks strip ─────────────────────────────────────────────
        strip = ttk.Frame(parent)
        strip.pack(fill="x", pady=(0, 4))

        prg = ttk.LabelFrame(strip, text="Presets")
        prg.pack(side="left", fill="y", padx=(0, 6))

        pr_row = ttk.Frame(prg)
        pr_row.pack(fill="x", padx=4, pady=(4, 2))
        self._preset_var   = tk.StringVar()
        self._preset_combo = ttk.Combobox(pr_row, textvariable=self._preset_var, width=18, state="readonly")
        self._preset_combo.pack(side="left", padx=(0, 4), fill="x", expand=True)
        ttk.Button(pr_row, text="Load", width=6, command=self._on_load_preset).pack(side="left")

        pr_row2 = ttk.Frame(prg)
        pr_row2.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(pr_row2, text="Save As…", command=self._on_save_preset_as).pack(side="left")
        ttk.Button(pr_row2, text="Delete",   command=self._on_delete_preset).pack(side="left", padx=4)

        self._refresh_preset_list()

        blkg = ttk.LabelFrame(strip, text="Blocks")
        blkg.pack(side="left", fill="y")

        blk_row1 = ttk.Frame(blkg)
        blk_row1.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(blk_row1, text="Current:").pack(side="left")
        self._block_var   = tk.StringVar()
        self._block_combo = ttk.Combobox(blk_row1, textvariable=self._block_var,
                                         width=10, state="readonly")
        self._block_combo.pack(side="left", padx=(4, 2))
        self._block_combo.bind("<<ComboboxSelected>>", self._on_block_selected)
        ttk.Button(blk_row1, text="▲", width=3, command=self._move_block_up).pack(side="left")
        ttk.Button(blk_row1, text="▼", width=3, command=self._move_block_down).pack(side="left", padx=(1, 0))

        blk_row2 = ttk.Frame(blkg)
        blk_row2.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(blk_row2, text="Add Before", command=self._add_block_before).pack(side="left")
        ttk.Button(blk_row2, text="Add After",  command=self._add_block_after).pack(side="left", padx=2)
        ttk.Button(blk_row2, text="Delete",     command=self._delete_block).pack(side="left")

        self._refresh_block_selector()

        # ── LUT editor ─────────────────────────────────────────────────────────
        lut_frame = ttk.LabelFrame(parent, text="LUT Editor – 2D Speed Map")
        lut_frame.pack(fill="both", expand=True)

        # ── Top row: 3 columns ─────────────────────────────────────────────────
        controls = ttk.Frame(lut_frame)
        controls.pack(fill="x", padx=4, pady=(4, 2))

        # Column 1 – Global settings + Force input range
        col1 = ttk.Frame(controls)
        col1.pack(side="left", fill="y", padx=(0, 8))

        lg = ttk.LabelFrame(col1, text="LUT Global Settings")
        lg.pack(fill="x", pady=(0, 4))
        self._lut_offset_var = self._add_spinrow(lg, "Offset:",        -0.05, -100, 100,  0.05)
        self._lut_scale_var  = self._add_spinrow(lg, "Scale (output):",  2.5,    0, 100,  0.25)
        self._lut_offset_var.trace_add("write", lambda *_: self._schedule_lut_update())
        self._lut_scale_var.trace_add("write",  lambda *_: self._schedule_lut_update())

        rg = ttk.LabelFrame(col1, text="Force Input Range")
        rg.pack(fill="x")
        self._lat_min_var = self._add_spinrow(rg, "Lat min:", -2000, -9999, 9999, 100)
        self._lat_max_var = self._add_spinrow(rg, "Lat max:",  2000, -9999, 9999, 100)
        self._ap_min_var  = self._add_spinrow(rg, "AP min:",  -2000, -9999, 9999, 100)
        self._ap_max_var  = self._add_spinrow(rg, "AP max:",   2000, -9999, 9999, 100)
        for v in (self._lat_min_var, self._lat_max_var, self._ap_min_var, self._ap_max_var):
            v.trace_add("write", lambda *_: self._schedule_lut_update())

        # Column 2 – Gaussians
        col2 = ttk.LabelFrame(controls, text="Gaussians")
        col2.pack(side="left", fill="y", padx=(0, 8))

        lb_frame = ttk.Frame(col2)
        lb_frame.pack(fill="x", padx=4, pady=(2, 0))
        self._gauss_lb = tk.Listbox(lb_frame, height=6, selectmode="single",
                                    exportselection=False, font=("Consolas", 8))
        self._gauss_lb.pack(side="left", fill="x", expand=True)
        vsb = ttk.Scrollbar(lb_frame, orient="vertical", command=self._gauss_lb.yview)
        vsb.pack(side="right", fill="y")
        self._gauss_lb.config(yscrollcommand=vsb.set)
        self._gauss_lb.bind("<<ListboxSelect>>", self._on_gauss_list_select)

        btn_row = ttk.Frame(col2)
        btn_row.pack(fill="x", padx=4, pady=2)
        ttk.Button(btn_row, text="+ Add",    width=7,  command=self._add_gaussian).pack(side="left")
        ttk.Button(btn_row, text="− Remove", width=8,  command=self._remove_gaussian).pack(side="left", padx=2)
        ttk.Button(btn_row, text="↑", width=3, command=lambda: self._move_gaussian(-1)).pack(side="left")
        ttk.Button(btn_row, text="↓", width=3, command=lambda: self._move_gaussian(1)).pack(side="left", padx=1)

        gf = ttk.LabelFrame(col2, text="Selected")
        gf.pack(fill="x", padx=4, pady=(0, 4))
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
            ttk.Label(row, text=label, width=11, anchor="e").pack(side="left")
            var = tk.DoubleVar(value=default)
            ttk.Spinbox(row, from_=lo, to=hi, increment=step,
                        textvariable=var, width=9, format="%.1f").pack(side="left", padx=(4, 0))
            var.trace_add("write", self._on_gauss_form_changed)
            self._gauss_vars[key] = var

        # Column 3 – Step functions
        col3 = ttk.LabelFrame(controls, text="Step Functions")
        col3.pack(side="left", fill="y")

        slb_frame = ttk.Frame(col3)
        slb_frame.pack(fill="x", padx=4, pady=(2, 0))
        self._step_lb = tk.Listbox(slb_frame, height=6, selectmode="single",
                                   exportselection=False, font=("Consolas", 8))
        self._step_lb.pack(side="left", fill="x", expand=True)
        svsb = ttk.Scrollbar(slb_frame, orient="vertical", command=self._step_lb.yview)
        svsb.pack(side="right", fill="y")
        self._step_lb.config(yscrollcommand=svsb.set)
        self._step_lb.bind("<<ListboxSelect>>", self._on_step_list_select)

        sbtn_row = ttk.Frame(col3)
        sbtn_row.pack(fill="x", padx=4, pady=2)
        ttk.Button(sbtn_row, text="+ Add",    width=7,  command=self._add_step).pack(side="left")
        ttk.Button(sbtn_row, text="− Remove", width=8,  command=self._remove_step).pack(side="left", padx=2)
        ttk.Button(sbtn_row, text="↑", width=3, command=lambda: self._move_step(-1)).pack(side="left")
        ttk.Button(sbtn_row, text="↓", width=3, command=lambda: self._move_step(1)).pack(side="left", padx=1)

        sf = ttk.LabelFrame(col3, text="Selected")
        sf.pack(fill="x", padx=4, pady=(0, 4))
        self._step_vars: dict = {}
        for key, label, default, lo, hi, step in [
            ("center_lat", "Center Lat:",  0.0, -9999, 9999,  50.0),
            ("center_ap",  "Center AP:",   0.0, -9999, 9999,  50.0),
            ("width_lat",  "Width Lat:", 400.0,     1, 9999,  50.0),
            ("width_ap",   "Width AP:",  400.0,     1, 9999,  50.0),
            ("peak",       "Peak:",        5.0,  -500,  500,   0.5),
            ("trough",     "Trough:",      0.0,  -500,  500,   0.1),
        ]:
            srow = ttk.Frame(sf)
            srow.pack(fill="x", pady=1)
            ttk.Label(srow, text=label, width=11, anchor="e").pack(side="left")
            var = tk.DoubleVar(value=default)
            ttk.Spinbox(srow, from_=lo, to=hi, increment=step,
                        textvariable=var, width=9, format="%.1f").pack(side="left", padx=(4, 0))
            var.trace_add("write", self._on_step_form_changed)
            self._step_vars[key] = var

        # ── Bottom: matplotlib LUT preview (full width) ────────────────────────
        iso_row = ttk.Frame(lut_frame)
        iso_row.pack(fill="x", padx=4, pady=(0, 2))
        ttk.Label(iso_row, text="Iso-lines:").pack(side="left")
        self._iso_lines_var = tk.StringVar()
        iso_entry = ttk.Entry(iso_row, textvariable=self._iso_lines_var, width=30)
        iso_entry.pack(side="left", padx=(4, 0))
        ttk.Label(iso_row, text="(comma-separated values)", foreground="gray").pack(side="left", padx=(4, 0))
        self._iso_lines_var.trace_add("write", lambda *_: self._schedule_lut_update())
        self._lut_contour_artists = []

        canvas_frame = ttk.Frame(lut_frame)
        canvas_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._lut_fig = Figure(tight_layout=True)
        self._lut_ax  = self._lut_fig.add_subplot(111)
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

        self._lut_hover_var = tk.StringVar(value="")
        ttk.Label(canvas_frame, textvariable=self._lut_hover_var,
                  foreground="gray", font=("TkFixedFont", 8)).pack(anchor="w", padx=4)
        self._lut_canvas.mpl_connect("motion_notify_event", self._on_lut_hover)

        self._refresh_gauss_list()
        self._refresh_step_list()
        self._update_lut_preview()

    # ── Preset helpers ─────────────────────────────────────────────────────────

    def _refresh_preset_list(self):
        names = sorted(self._presets)
        self._preset_combo["values"] = names
        if names and self._preset_var.get() not in names:
            self._preset_var.set(names[0])

    def _on_load_preset(self):
        name = self._preset_var.get()
        if not name or name not in self._presets:
            return
        preset = self._presets[name]
        if "blocks" in preset:
            # New format: replace all blocks, load block 0
            self._save_current_block()
            self._blocks = deepcopy(preset["blocks"])
            if not self._blocks:
                return
            self._current_block = 0
            for k in self._BLOCK_KEYS:
                if k in self._blocks[0]:
                    self._params[k] = deepcopy(self._blocks[0][k])
            self._refresh_block_selector()
        else:
            # Old format: merge LUT keys into current block only
            for k, v in preset.items():
                if k in self._LUT_PRESET_KEYS:
                    self._params[k] = deepcopy(v)
        self._apply_params_to_ui()

    _LUT_PRESET_KEYS = {
        "action_duration", "lick_response_time", "inter_trial_interval",
        "quiescence_duration", "quiescence_threshold",
        "is_operant", "instantaneous_mode", "motor_feedback",
        "lut_offset", "lut_scale",
        "lat_range_min", "lat_range_max", "ap_range_min", "ap_range_max",
        "lut_gaussians", "lut_steps",
    }

    # Keys saved per-block in presets — excludes mouse-specific physical params
    # (far_position, close_position, reward_size, trial_length)
    _PRESET_BLOCK_KEYS = _LUT_PRESET_KEYS | {"trial_number"}

    _BLOCK_KEYS = {
        "trial_number", "trial_length", "action_duration", "lick_response_time",
        "inter_trial_interval", "reward_size", "far_position", "close_position",
        "quiescence_duration", "quiescence_threshold",
        "is_operant", "instantaneous_mode", "motor_feedback",
        "lut_offset", "lut_scale",
        "lat_range_min", "lat_range_max", "ap_range_min", "ap_range_max",
        "lut_gaussians", "lut_steps",
    }

    # ── Block management ───────────────────────────────────────────────────────

    def _save_current_block(self):
        """Snapshot current UI into self._blocks[self._current_block]."""
        if not self._blocks or self._current_block >= len(self._blocks):
            return
        p = self._build_current_params()
        self._blocks[self._current_block] = {
            k: deepcopy(p[k]) for k in self._BLOCK_KEYS if k in p
        }

    def _load_block(self, idx: int):
        if not (0 <= idx < len(self._blocks)):
            return
        for k in self._BLOCK_KEYS:
            if k in self._blocks[idx]:
                self._params[k] = deepcopy(self._blocks[idx][k])
        self._current_block = idx
        self._apply_params_to_ui()

    def _refresh_block_selector(self):
        values = [f"Block {i}" for i in range(len(self._blocks))]
        self._block_combo["values"] = values
        self._block_var.set(f"Block {self._current_block}")

    def _on_block_selected(self, _=None):
        sel = self._block_var.get()
        try:
            idx = int(sel.split()[-1])
        except (ValueError, IndexError):
            return
        if idx == self._current_block:
            return
        self._save_current_block()
        self._load_block(idx)
        self._refresh_block_selector()

    def _add_block_before(self):
        """Insert a copy of the current block before it; stay on current (shifted right)."""
        self._save_current_block()
        new_block = deepcopy(self._blocks[self._current_block])
        self._blocks.insert(self._current_block, new_block)
        self._current_block += 1
        self._load_block(self._current_block)
        self._refresh_block_selector()

    def _add_block_after(self):
        """Insert a copy of the current block after it; switch to the new block."""
        self._save_current_block()
        new_block = deepcopy(self._blocks[self._current_block])
        insert_at = self._current_block + 1
        self._blocks.insert(insert_at, new_block)
        self._current_block = insert_at
        self._load_block(self._current_block)
        self._refresh_block_selector()

    def _delete_block(self):
        if len(self._blocks) <= 1:
            messagebox.showwarning("Cannot Delete", "Must keep at least one block.", parent=self)
            return
        self._blocks.pop(self._current_block)
        self._current_block = max(0, self._current_block - 1)
        self._load_block(self._current_block)
        self._refresh_block_selector()

    def _move_block_up(self):
        if self._current_block <= 0:
            return
        self._save_current_block()
        i = self._current_block
        self._blocks[i], self._blocks[i - 1] = self._blocks[i - 1], self._blocks[i]
        self._current_block -= 1
        self._load_block(self._current_block)
        self._refresh_block_selector()

    def _move_block_down(self):
        if self._current_block >= len(self._blocks) - 1:
            return
        self._save_current_block()
        i = self._current_block
        self._blocks[i], self._blocks[i + 1] = self._blocks[i + 1], self._blocks[i]
        self._current_block += 1
        self._load_block(self._current_block)
        self._refresh_block_selector()

    def _on_save_preset_as(self):
        name = simpledialog.askstring("Save Preset", "Preset name:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        self._save_current_block()
        preset = {
            "blocks": [
                {k: deepcopy(blk[k]) for k in self._PRESET_BLOCK_KEYS if k in blk}
                for blk in self._blocks
            ]
        }
        self._presets[name] = preset
        save_preset(name, preset)
        self._refresh_preset_list()
        self._preset_var.set(name)

    def _on_delete_preset(self):
        name = self._preset_var.get()
        if not name or name not in self._presets:
            return
        if messagebox.askyesno("Delete Preset", f"Delete preset '{name}'?", parent=self):
            self._presets.pop(name, None)
            p = PRESETS_DIR / f"{name}.json"
            if p.exists():
                p.unlink()
            self._refresh_preset_list()

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
        raw = self._profiles.get(name, DEFAULT_PARAMS)
        rig_cams = self._rig_camera_defaults()
        if "blocks" in raw:
            # New multi-block profile format
            self._blocks = deepcopy(raw["blocks"])
            if not self._blocks:
                self._blocks = [deepcopy({k: DEFAULT_PARAMS[k] for k in self._BLOCK_KEYS if k in DEFAULT_PARAMS})]
            self._current_block = 0
            self._params = deepcopy(DEFAULT_PARAMS)
            for k, v in raw.items():
                if k != "blocks":
                    self._params[k] = deepcopy(v)
            for k, v in rig_cams.items():
                if k not in raw:
                    self._params[k] = v
            for k in self._BLOCK_KEYS:
                if k in self._blocks[0]:
                    self._params[k] = deepcopy(self._blocks[0][k])
        else:
            # Old flat profile format — treat as single-block
            self._params = deepcopy(raw)
            for k, v in rig_cams.items():
                if k not in self._params:
                    self._params[k] = v
            block0 = {k: deepcopy(self._params[k]) for k in self._BLOCK_KEYS if k in self._params}
            block0.setdefault("trial_number", DEFAULT_PARAMS.get("trial_number", 100))
            self._blocks = [block0]
            self._current_block = 0
        self._refresh_block_selector()
        self._apply_params_to_ui()

    def _on_new_mouse(self):
        name = simpledialog.askstring("New Mouse", "Enter mouse name:", parent=self)
        if name and name.strip():
            name = name.strip()
            new_params = deepcopy(DEFAULT_PARAMS)
            new_params.update(self._rig_camera_defaults())
            block0 = {k: deepcopy(new_params[k]) for k in self._BLOCK_KEYS if k in new_params}
            profile = {
                "blocks": [block0],
                **{k: v for k, v in new_params.items() if k not in self._BLOCK_KEYS},
            }
            self._profiles[name] = profile
            save_mouse_profile(name, profile)
            self._refresh_mouse_list()
            self._mouse_combo.set(name)
            self._on_mouse_selected()   # reset self._params / self._blocks to this mouse

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
            self._instantaneous_mode_var.set(bool(self._params.get("instantaneous_mode", False)))
            self._motor_feedback_var.set(bool(self._params.get("motor_feedback", True)))
            self._exp_var.set(self._params.get("experimenter", "rozmar"))
            self._notes_var.set(self._params.get("notes", ""))
            gamma2_val = self._params.get("camera2_gamma")
            enabled2   = gamma2_val is not None
            self._gamma2_en_var.set(enabled2)
            self._gamma2_var.set(gamma2_val if enabled2 else 1.0)
            self._gamma2_spin.config(state="normal" if enabled2 else "disabled")
            self._refresh_gauss_list()
            self._refresh_step_list()
            self._update_lut_preview()
        finally:
            self._params_loading = False

    def _on_task_param_changed(self, *_):
        if self._params_loading:
            return
        for key, var in self._param_vars.items():
            try:
                v = var.get()
                self._params[key] = v
                if key in self._BLOCK_KEYS and self._blocks:
                    self._blocks[self._current_block][key] = v
            except Exception:
                pass
        for key, val in [
            ("is_operant", self._is_operant_var.get()),
            ("instantaneous_mode", self._instantaneous_mode_var.get()),
            ("motor_feedback", self._motor_feedback_var.get()),
            ("experimenter", self._exp_var.get()),
            ("notes", self._notes_var.get()),
        ]:
            self._params[key] = val
            if key in self._BLOCK_KEYS and self._blocks:
                self._blocks[self._current_block][key] = val

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
        if self._blocks:
            self._blocks[self._current_block]["lut_gaussians"] = deepcopy(gaussians)
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
        if self._blocks:
            self._blocks[self._current_block]["lut_gaussians"] = deepcopy(self._params["lut_gaussians"])
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
            if self._blocks:
                self._blocks[self._current_block]["lut_gaussians"] = deepcopy(gaussians)
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
            if self._blocks:
                self._blocks[self._current_block]["lut_gaussians"] = deepcopy(gaussians)
            self._sel_gauss_idx = new_idx
            self._refresh_gauss_list()
            self._gauss_lb.selection_set(new_idx)
            self._on_gauss_list_select()
            self._schedule_lut_update()

    # ── Step function list helpers ─────────────────────────────────────────────

    def _step_label(self, i: int, s: dict) -> str:
        return (f"S{i+1}  c=({s['center_lat']:.0f}, {s['center_ap']:.0f})"
                f"  w=({s['width_lat']:.0f},{s['width_ap']:.0f})"
                f"  pk={s['peak']:.1f}")

    def _refresh_step_list(self, keep_selection: bool = False):
        saved = self._step_lb.curselection()
        self._step_lb.delete(0, tk.END)
        for i, s in enumerate(self._params.get("lut_steps", [])):
            self._step_lb.insert(tk.END, self._step_label(i, s))
        if keep_selection and saved:
            idx = min(saved[0], self._step_lb.size() - 1)
            if idx >= 0:
                self._step_lb.selection_set(idx)

    def _on_step_list_select(self, _=None):
        sel = self._step_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        self._sel_step_idx = idx
        s = self._params["lut_steps"][idx]
        self._step_loading = True
        for key, var in self._step_vars.items():
            var.set(s.get(key, 0.0))
        self._step_loading = False

    def _on_step_form_changed(self, *_):
        if self._step_loading:
            return
        idx = self._sel_step_idx
        steps = self._params.get("lut_steps", [])
        if idx is None or not (0 <= idx < len(steps)):
            return
        try:
            steps[idx] = {k: v.get() for k, v in self._step_vars.items()}
        except Exception:
            return
        if self._blocks:
            self._blocks[self._current_block]["lut_steps"] = deepcopy(steps)
        self._step_lb.delete(idx)
        self._step_lb.insert(idx, self._step_label(idx, steps[idx]))
        self._step_lb.selection_set(idx)
        self._schedule_lut_update()

    def _add_step(self):
        new_s = {"center_lat": 0.0, "center_ap": 0.0,
                 "width_lat": 400.0, "width_ap": 400.0,
                 "peak": 5.0, "trough": 0.0}
        self._params.setdefault("lut_steps", []).append(new_s)
        if self._blocks:
            self._blocks[self._current_block]["lut_steps"] = deepcopy(self._params["lut_steps"])
        self._refresh_step_list()
        new_idx = len(self._params["lut_steps"]) - 1
        self._step_lb.selection_set(new_idx)
        self._sel_step_idx = new_idx
        self._on_step_list_select()
        self._schedule_lut_update()

    def _remove_step(self):
        idx = self._sel_step_idx
        steps = self._params.get("lut_steps", [])
        if idx is not None and 0 <= idx < len(steps):
            steps.pop(idx)
            if self._blocks:
                self._blocks[self._current_block]["lut_steps"] = deepcopy(steps)
            self._sel_step_idx = max(0, idx - 1) if steps else None
            self._refresh_step_list(keep_selection=True)
            if self._sel_step_idx is not None:
                self._on_step_list_select()
            self._schedule_lut_update()

    def _move_step(self, direction: int):
        idx = self._sel_step_idx
        steps = self._params.get("lut_steps", [])
        if idx is None or not steps:
            return
        new_idx = idx + direction
        if 0 <= new_idx < len(steps):
            steps[idx], steps[new_idx] = steps[new_idx], steps[idx]
            if self._blocks:
                self._blocks[self._current_block]["lut_steps"] = deepcopy(steps)
            self._sel_step_idx = new_idx
            self._refresh_step_list()
            self._step_lb.selection_set(new_idx)
            self._on_step_list_select()
            self._schedule_lut_update()

    # ── LUT preview ────────────────────────────────────────────────────────────

    def _on_lut_hover(self, event):
        if event.inaxes is not self._lut_ax or self._lut_display is None:
            self._lut_hover_var.set("")
            return
        try:
            x, y = event.xdata, event.ydata
            # Find the nearest grid point in the same coordinate arrays the contour uses
            col = int(np.argmin(np.abs(self._lut_lat_disp - x)))
            row = int(np.argmin(np.abs(self._lut_ap_disp  - y)))
            val = self._lut_display[row, col]
            self._lut_hover_var.set(f"x={x:.3g}   y={y:.3g}   value={val:.4g}")
        except Exception:
            self._lut_hover_var.set("")

    def _schedule_lut_update(self):
        """Rate-limit preview redraws to avoid UI stutter while typing."""
        if not self._params_loading and self._blocks:
            blk = self._blocks[self._current_block]
            for attr, key in [
                ("_lut_offset_var", "lut_offset"), ("_lut_scale_var", "lut_scale"),
                ("_lat_min_var", "lat_range_min"), ("_lat_max_var", "lat_range_max"),
                ("_ap_min_var", "ap_range_min"), ("_ap_max_var", "ap_range_max"),
            ]:
                try:
                    blk[key] = getattr(self, attr).get()
                except Exception:
                    pass
        if hasattr(self, "_lut_update_job"):
            self.after_cancel(self._lut_update_job)
        self._lut_update_job = self.after(80, self._update_lut_preview)

    _CAL_TTL = 30.0  # seconds before re-reading the calibration CSV

    def _get_calibration(self) -> "dict | None":
        rig_path = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"
        try:
            with open(rig_path, encoding="utf-8") as f:
                rig_name = json.load(f).get("rig_name", DEFAULT_RIG["rig_name"])
        except Exception:
            rig_name = DEFAULT_RIG["rig_name"]
        age = time.monotonic() - self._lc_calibration_time
        if rig_name != self._lc_calibration_rig or age > self._CAL_TTL:
            self._lc_calibration = load_lc_calibration(rig_name)
            self._lc_calibration_rig = rig_name
            self._lc_calibration_time = time.monotonic()
        return self._lc_calibration

    def _update_lut_preview(self):
        try:
            p = self._build_current_params()
            matrix, lat_vec, ap_vec = compute_lut_matrix(p)

            # Convert raw load-cell units to grams using rig calibration
            cal = self._get_calibration()
            if cal and "lat" in cal:
                lv, lg = cal["lat"]
                idx = np.argsort(lv)
                lat_disp = np.interp(np.abs(lat_vec), lv[idx], lg[idx]) * np.sign(lat_vec)
                lat_label = "Lateral Force (g, Left→Right)"
            else:
                lat_disp, lat_label = lat_vec, "Lateral Force (au, Left→Right)"
            if cal and "ap" in cal:
                av, ag = cal["ap"]
                idx = np.argsort(av)
                ap_disp = np.interp(np.abs(ap_vec), av[idx], ag[idx]) * np.sign(ap_vec)
                ap_label = "AP Force (g, Posterior→Anterior)"
            else:
                ap_disp, ap_label = ap_vec, "AP Force (au, Posterior→Anterior)"

            self._lut_ax.set_xlabel(lat_label, fontsize=8)
            self._lut_ax.set_ylabel(ap_label, fontsize=8)
            inst = self._instantaneous_mode_var.get()
            if inst:
                display = matrix * p.get("lut_scale", 1.0)
                self._lut_im.set_cmap(_POS_CMAP)
                self._lut_im.set_data(display)
                self._lut_im.set_extent([lat_disp[0], lat_disp[-1], ap_disp[-1], ap_disp[0]])
                self._lut_im.set_clim(0, 1)
                self._lut_cbar.set_label("Lickport Position (0→1)", fontsize=8)
                n_g = len(p.get("lut_gaussians", []))
                n_s = len(p.get("lut_steps", []))
                self._lut_ax.set_title(
                    f"Position LUT  |  scale×{p['lut_scale']:.2f}  "
                    f"{n_g}G + {n_s}S  [red = above threshold]",
                    fontsize=8,
                )
            else:
                n_g = len(p.get("lut_gaussians", []))
                n_s = len(p.get("lut_steps", []))
                display = matrix * p.get("lut_scale", 1.0)
                self._lut_im.set_cmap("viridis")
                self._lut_im.set_data(display)
                self._lut_im.set_extent([lat_disp[0], lat_disp[-1], ap_disp[-1], ap_disp[0]])
                self._lut_im.set_clim(display.min(), display.max())
                self._lut_cbar.set_label("Speed (mm/s)", fontsize=8)
                self._lut_ax.set_title(
                    f"Speed LUT  |  offset={p['lut_offset']:.2f}  scale×{p['lut_scale']:.2f}  "
                    f"{n_g}G + {n_s}S",
                    fontsize=8,
                )
            self._lut_cbar.update_normal(self._lut_im)
            self._lut_ax.set_xlim(lat_disp[0], lat_disp[-1])
            self._lut_ax.set_ylim(ap_disp[-1], ap_disp[0])  # inverted: posterior(+) at bottom, anterior(−) at top
            self._lut_display  = display
            self._lut_lat_disp = lat_disp
            self._lut_ap_disp  = ap_disp

            # Iso-lines — remove each stored artist individually so a single
            # failure never blocks the rest.
            for artist in self._lut_contour_artists:
                try:
                    artist.remove()
                except Exception:
                    pass
            self._lut_contour_artists = []
            iso_text = self._iso_lines_var.get()
            if iso_text.strip():
                try:
                    levels = sorted(float(v) for v in iso_text.split(",") if v.strip())
                    if levels:
                        before_c = set(id(c) for c in self._lut_ax.collections)
                        before_t = set(id(t) for t in self._lut_ax.texts)
                        cs = self._lut_ax.contour(
                            lat_disp, ap_disp, display, levels=levels,
                            colors="white", linewidths=0.8, linestyles="dashed",
                        )
                        self._lut_ax.clabel(cs, fmt="%.2g", fontsize=7)
                        self._lut_contour_artists = (
                            [c for c in self._lut_ax.collections if id(c) not in before_c] +
                            [t for t in self._lut_ax.texts    if id(t) not in before_t]
                        )
                except Exception:
                    pass

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
        p["is_operant"]           = self._is_operant_var.get()
        p["instantaneous_mode"]   = self._instantaneous_mode_var.get()
        p["motor_feedback"]       = self._motor_feedback_var.get()
        p["experimenter"]         = self._exp_var.get()
        p["notes"]                = self._notes_var.get()
        return p

    def _on_save_profile(self):
        name = self._mouse_var.get()
        self._save_current_block()
        p = self._build_current_params()
        profile = {
            "blocks": deepcopy(self._blocks),
            **{k: v for k, v in p.items() if k not in self._BLOCK_KEYS},
        }
        self._profiles[name] = profile
        save_mouse_profile(name, profile)
        messagebox.showinfo("Saved", f"Profile '{name}' saved to:\n{MICE_DIR / name}.json", parent=self)

    def _on_generate_config(self):
        try:
            session_folder = self._generate_config()
            self._show_block_summary(session_folder)
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self)

    # ── Block summary popup ────────────────────────────────────────────────────

    _SUMMARY_PARAMS = [
        ("trial_number",         "Trials"),
        ("trial_length",         "Trial length (s)"),
        ("action_duration",      "Action dur (s)"),
        ("lick_response_time",   "Response time (s)"),
        ("inter_trial_interval", "ITI (s)"),
        ("reward_size",          "Reward (µL)"),
        ("far_position",         "Far pos (mm)"),
        ("close_position",       "Close pos (mm)"),
        ("quiescence_duration",  "Quiescence dur (s)"),
        ("quiescence_threshold", "Quiescence thr"),
        ("is_operant",           "Operant"),
        ("instantaneous_mode",   "Instantaneous"),
        ("motor_feedback",       "Motor feedback"),
        ("lut_offset",           "LUT offset"),
        ("lut_scale",            "LUT scale"),
        ("lat_range_min",        "Lat min"),
        ("lat_range_max",        "Lat max"),
        ("ap_range_min",         "AP min"),
        ("ap_range_max",         "AP max"),
    ]

    def _show_block_summary(self, session_folder: str = ""):
        p      = self._build_current_params()
        blocks = self._blocks
        if not blocks:
            return

        cal = self._get_calibration()
        n   = len(blocks)

        # ── Compute per-block LUT data ─────────────────────────────────────────
        all_displays, all_lat_disp, all_ap_disp = [], [], []
        lat_lbl = "Lateral Force (au, Left→Right)"
        ap_lbl  = "AP Force (au, Posterior→Anterior)"
        for block in blocks:
            bp = {**p, **block}
            matrix, lat_vec, ap_vec = compute_lut_matrix(bp)
            display = matrix * bp.get("lut_scale", 1.0)
            if cal and "lat" in cal:
                lv, lg = cal["lat"]
                ix = np.argsort(lv)
                lat_d = np.interp(np.abs(lat_vec), lv[ix], lg[ix]) * np.sign(lat_vec)
                lat_lbl = "Lateral Force (g, Left→Right)"
            else:
                lat_d = lat_vec
            if cal and "ap" in cal:
                av, ag = cal["ap"]
                ix = np.argsort(av)
                ap_d = np.interp(np.abs(ap_vec), av[ix], ag[ix]) * np.sign(ap_vec)
                ap_lbl = "AP Force (g, Posterior→Anterior)"
            else:
                ap_d = ap_vec
            all_displays.append(display)
            all_lat_disp.append(lat_d)
            all_ap_disp.append(ap_d)

        # Shared colour + spatial scales
        vmin = min(d.min() for d in all_displays)
        vmax = max(d.max() for d in all_displays)
        x_lo = min(ld[0]  for ld in all_lat_disp)
        x_hi = max(ld[-1] for ld in all_lat_disp)
        # y axis is inverted: posterior (positive) at bottom, anterior (negative) at top
        y_bot = max(ad[-1] for ad in all_ap_disp)   # most posterior  → bottom
        y_top = min(ad[0]  for ad in all_ap_disp)   # most anterior   → top

        inst_mode = ({**p, **blocks[0]}).get("instantaneous_mode", False)
        cbar_lbl  = "Position (0→1)" if inst_mode else "Speed (mm/s)"

        # ── Window ─────────────────────────────────────────────────────────────
        win = tk.Toplevel(self)
        win.title(f"Block Summary  —  {session_folder}" if session_folder else "Block Summary")
        win.geometry(f"{min(220*n + 120, 1600)}x750")

        # ── Matplotlib figure ──────────────────────────────────────────────────
        fig_frame = ttk.Frame(win)
        fig_frame.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        fig = Figure(figsize=(max(3.2 * n + 0.8, 5), 3.8), tight_layout=True)
        axes = [fig.add_subplot(1, n, i + 1) for i in range(n)]
        last_im = None
        for i, (ax, display, lat_d, ap_d) in enumerate(
                zip(axes, all_displays, all_lat_disp, all_ap_disp)):
            im = ax.imshow(
                display, cmap="viridis", vmin=vmin, vmax=vmax,
                extent=[lat_d[0], lat_d[-1], ap_d[-1], ap_d[0]],
                aspect="auto",
            )
            ax.set_xlim(x_lo, x_hi)
            ax.set_ylim(y_bot, y_top)
            ax.set_title(f"Block {i + 1}", fontsize=9)
            ax.set_xlabel(lat_lbl, fontsize=7)
            if i == 0:
                ax.set_ylabel(ap_lbl, fontsize=7)
            else:
                ax.tick_params(labelleft=False)
            last_im = im

        fig_canvas = FigureCanvasTkAgg(fig, master=fig_frame)
        fig_canvas.get_tk_widget().pack(fill="both", expand=True)
        fig_canvas.draw()

        # ── Bottom row: parameter table + colorbar ─────────────────────────────
        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=6, pady=(4, 6))

        tbl_outer = ttk.Frame(bottom)
        tbl_outer.pack(side="left", fill="x", expand=True)
        h_sb = ttk.Scrollbar(tbl_outer, orient="horizontal")
        h_sb.pack(side="bottom", fill="x")
        tbl_cv = tk.Canvas(tbl_outer, xscrollcommand=h_sb.set,
                           height=min(len(self._SUMMARY_PARAMS) * 18 + 26, 260),
                           highlightthickness=0, bg="white")
        tbl_cv.pack(fill="x")
        h_sb.config(command=tbl_cv.xview)

        tbl = tk.Frame(tbl_cv, bg="white")
        tbl_cv.create_window((0, 0), window=tbl, anchor="nw")
        tbl.bind("<Configure>", lambda _: tbl_cv.configure(scrollregion=tbl_cv.bbox("all")))

        # Header row
        LABEL_W, VAL_W = 20, 14
        tk.Label(tbl, text="Parameter", font=("TkDefaultFont", 8, "bold"),
                 anchor="w", bg="#e0e0e0", width=LABEL_W, padx=4).grid(
                 row=0, column=0, sticky="ew", padx=(0, 1), pady=(0, 1))
        for i in range(n):
            tk.Label(tbl, text=f"Block {i + 1}", font=("TkDefaultFont", 8, "bold"),
                     anchor="center", bg="#e0e0e0", width=VAL_W).grid(
                     row=0, column=i + 1, padx=1, pady=(0, 1), sticky="ew")

        for row_i, (key, label) in enumerate(self._SUMMARY_PARAMS, start=1):
            row_bg = "#f5f5f5" if row_i % 2 else "white"
            tk.Label(tbl, text=label, font=("TkFixedFont", 8), anchor="w",
                     bg=row_bg, width=LABEL_W, padx=4).grid(
                     row=row_i, column=0, sticky="ew", padx=(0, 1))
            for i, block in enumerate(blocks):
                bp     = {**p, **block}
                val    = bp.get(key)
                val_str = f"{val:.4g}" if isinstance(val, float) else ("—" if val is None else str(val))
                changed = False
                if i > 0:
                    prev_val = ({**p, **blocks[i - 1]}).get(key)
                    changed  = (val != prev_val)
                cell_bg = "#ffe082" if changed else row_bg
                tk.Label(tbl, text=val_str, font=("TkFixedFont", 8), anchor="center",
                         bg=cell_bg, width=VAL_W).grid(
                         row=row_i, column=i + 1, padx=1, sticky="ew")

        # Colorbar — separate narrow figure to the right of the table
        if last_im is not None:
            tbl_h = min(len(self._SUMMARY_PARAMS) * 18 + 26, 260)
            cbar_fig = Figure(figsize=(1.6, tbl_h / 96))
            cbar_ax  = cbar_fig.add_axes([0.05, 0.08, 0.22, 0.84])
            import matplotlib
            matplotlib.colorbar.ColorbarBase(
                cbar_ax, cmap=matplotlib.cm.viridis,
                norm=matplotlib.colors.Normalize(vmin=vmin, vmax=vmax),
                orientation="vertical", label=cbar_lbl,
            )
            cbar_canvas = FigureCanvasTkAgg(cbar_fig, master=bottom)
            cbar_canvas.get_tk_widget().pack(side="left", padx=(6, 0))
            cbar_canvas.draw()

    def _generate_config(self):
        self._save_current_block()
        p          = self._build_current_params()  # mouse-level + current block params
        mouse_name = self._mouse_var.get()

        # 1. LUT images — one per block; collect per-block speed ranges (mm/s)
        lut_ranges = []
        for i, block in enumerate(self._blocks):
            bp = {**p, **block}
            lut_ranges.append(save_lut_image(bp, LOCAL_DIR / f"2d_gaussian_block{i}.tiff"))

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
            allow_dirty_repo=False,
            skip_hardware_validation=False,
            experimenter=[self._exp_var.get()],
        )
        with open(LOCAL_DIR / "Session.json", "w", encoding="utf-8") as f:
            f.write(session.model_dump_json(indent=2))

        # 3. TaskLogic JSON via pydantic models
        self._generate_task_logic(p, self._blocks, LOCAL_DIR / "AindBehaviorTelekinesisTaskLogic.json", lut_ranges)

        # 4. Patch per-mouse camera settings into all cameras in rig JSON
        rig_path = LOCAL_DIR / "AindBehaviorTelekinesisRig.json"
        data_directory = "C:\\Data"
        # Block 0 drives initial motor position (spout starts at close_pos)
        block0 = {**p, **self._blocks[0]}
        if rig_path.exists():
            with open(rig_path, encoding="utf-8") as f:
                rig_json = json.load(f)
            data_directory = rig_json.get("data_directory", data_directory)
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
            # Patch per-mouse motor hard limit into Y1 axis (axis == 2)
            ax_cfg = (rig_json.get("manipulator", {})
                              .get("calibration", {})
                              .get("axis_configuration", []))
            for ax in ax_cfg:
                if ax.get("axis") == 2:
                    ax["max_limit"] = float(p.get("mouse_motor_hard_limit", 15.0))
            # y1 = close_pos from block 0; y2 = 0.0 (axis not configured — non-zero triggers error)
            manip_cal = rig_json.get("manipulator", {}).get("calibration", {})
            if "initial_position" in manip_cal:
                manip_cal["initial_position"]["x"]  = float(p.get("x_position", 0.0))
                manip_cal["initial_position"]["z"]  = float(p.get("z_position", 0.0))
                manip_cal["initial_position"]["y1"] = float(block0.get("close_position", 14.5))
                manip_cal["initial_position"]["y2"] = 0.0
            with open(rig_path, "w", encoding="utf-8") as f:
                json.dump(rig_json, f, indent=2)

        # 5. Copy rig JSON and mouse profile into the Bonsai session folder
        session_folder = Path(data_directory) / session_name
        session_folder.mkdir(parents=True, exist_ok=True)
        if rig_path.exists():
            shutil.copy2(rig_path, session_folder / rig_path.name)
        mouse_profile_path = MICE_DIR / f"{mouse_name}.json"
        if mouse_profile_path.exists():
            shutil.copy2(mouse_profile_path, session_folder / mouse_profile_path.name)

        return session_folder

    def _generate_task_logic(self, mouse_p: dict, blocks: list, path: Path, lut_ranges: list):
        import aind_behavior_telekinesis.task_logic as tl  # noqa: F401 (keep local import)

        block_generators = []
        action_luts: dict = {}

        for i, block_dict in enumerate(blocks):
            bp        = {**mouse_p, **block_dict}
            far_pos   = bp["far_position"]
            close_pos = bp["close_position"]
            lut_ref   = f"gaussian_2d_block{i}"
            lut_path  = str(LOCAL_DIR / f"2d_gaussian_block{i}.tiff")

            # TIFF is float32 [0, 512]; converter_lut_input matches this range.
            # output[0]=far-close → motor at far_pos (retracted) at pixel 0,
            # output[1]=0        → motor at close_pos (extended)  at pixel 512.
            _, _, lut_max = lut_ranges[i]
            feedback = (
                tl.ManipulatorFeedback(
                    converter_lut_input=[0, 1],
                    converter_lut_output=[far_pos - close_pos, 0],
                )
                if bp.get("motor_feedback", True)
                else None
            )

            prototype_trial = tl.Action(
                reward_probability=tl.scalar_value(1),
                reward_amount=tl.scalar_value(bp["reward_size"]),
                reward_delay=tl.scalar_value(0),
                action_duration=tl.scalar_value(bp.get("action_duration", 0.1)),
                is_operant=bool(bp.get("is_operant", False)),
                time_to_collect=tl.scalar_value(bp["lick_response_time"]),
                lower_action_threshold=tl.scalar_value(0),
                upper_action_threshold=tl.scalar_value(512 * (close_pos - far_pos) / lut_max),
                continuous_feedback=feedback,
                action_type="instantaneous" if bp.get("instantaneous_mode") else "integrated",
            )

            block_generators.append(tl.BlockGenerator(
                block_size=tl.scalar_value(int(bp.get("trial_number", 100))),
                trial_statistics=tl.Trial(
                    inter_trial_interval=tl.scalar_value(bp["inter_trial_interval"]),
                    quiescence_period=tl.QuiescencePeriod(
                        duration=tl.scalar_value(bp.get("quiescence_duration", 0.5)),
                        action_threshold=bp.get("quiescence_threshold", 1),
                    ),
                    response_period=tl.ResponsePeriod(
                        duration=tl.scalar_value(bp["trial_length"]),
                        has_cue=True,
                        action=prototype_trial,
                    ),
                    action_source_0=tl.LoadCellActionSource(channel=0),
                    action_source_1=tl.LoadCellActionSource(channel=1),
                    sampler=tl.LutSampler2D(lut_reference=lut_ref),
                ),
            ))

            action_luts[lut_ref] = tl.ActionLookUpTableFactory(
                path=lut_path,
                offset=0,
                scale=1,
                action0_max=bp["lat_range_max"],
                action0_min=bp["lat_range_min"],
                action1_max=bp["ap_range_max"],
                action1_min=bp["ap_range_min"],
            )

        # Spout retraction offset uses block 0 (applies at task initialisation)
        b0 = {**mouse_p, **blocks[0]}
        task_logic = tl.AindBehaviorTelekinesisTaskLogic(
            task_parameters=tl.AindTelekinesisTaskParameters(
                rng_seed=None,
                environment=tl.Environment(
                    block_statistics=block_generators,
                ),
                operation_control=tl.OperationControl(
                    action_luts=action_luts,
                    spout=tl.SpoutOperationControl(
                        default_retraction_offset=b0["far_position"] - b0["close_position"],
                        enabled=True,
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
        profile = {
            "blocks": deepcopy(self._blocks),
            **{k: v for k, v in p.items() if k not in self._BLOCK_KEYS},
        }
        self._profiles[name] = profile
        save_mouse_profile(name, profile)

        bonsai_exe      = PROJECT_ROOT / ".bonsai" / "Bonsai.exe"
        bonsai_workflow = PROJECT_ROOT / "src" / "main.bonsai"
        if not bonsai_exe.exists():
            messagebox.showwarning(
                "Launch Failed",
                f"Config written to {LOCAL_DIR}\n"
                f"but Bonsai.exe not found at:\n{bonsai_exe}\n"
                "Please launch Bonsai manually.",
                parent=self,
            )
            return
        proc = subprocess.Popen(
            [
                str(bonsai_exe),
                str(bonsai_workflow),
                "-p", f"RigPath={LOCAL_DIR / 'AindBehaviorTelekinesisRig.json'}",
                "-p", f"SessionPath={LOCAL_DIR / 'Session.json'}",
                "-p", f"TaskPath={LOCAL_DIR / 'AindBehaviorTelekinesisTaskLogic.json'}",
            ],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        def _stream_bonsai(p):
            try:
                for line in io.TextIOWrapper(p.stdout, encoding="utf-8", errors="replace"):
                    sys.stderr.write("[Bonsai] " + line)
                    sys.stderr.flush()
            except Exception:
                pass

        threading.Thread(target=_stream_bonsai, args=(proc,),
                         daemon=True, name="bonsai-log").start()


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
                try:
                    self._handle_event(event_name, ts, payload)
                except Exception:
                    pass
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

        # Rig identity
        ig = ttk.LabelFrame(left, text="Rig Identity")
        ig.pack(fill="x", pady=(0, 6))
        self._rig_name_var = self._entry_row(ig, "Rig Name:", DEFAULT_RIG["rig_name"])

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
        self._data_dir_var    = self._entry_row(dg, "Data Directory:", "C:\\Data")
        self._backup_root_var = self._entry_row(dg, "Backup Root:", DEFAULT_RIG["backup_root"])

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

        # CSV calibration info
        calg = ttk.LabelFrame(right, text="Load Cell Calibration (from CSV)")
        calg.pack(fill="x", pady=(6, 0))
        date_row = ttk.Frame(calg)
        date_row.pack(fill="x", padx=4, pady=(4, 2))
        ttk.Label(date_row, text="Latest date:", width=14, anchor="e").pack(side="left")
        self._cal_date_var = tk.StringVar(value="—")
        ttk.Label(date_row, textvariable=self._cal_date_var,
                  font=("TkDefaultFont", 9, "bold")).pack(side="left", padx=(4, 0))

        axes_hdr = ttk.Frame(calg)
        axes_hdr.pack(fill="x", padx=4)
        for txt, w in [("Axis", 4), ("Dir", 5), ("Slope (g/au)", 14), ("Baseline (au)", 14)]:
            ttk.Label(axes_hdr, text=txt, width=w,
                      font=("TkDefaultFont", 8, "bold")).pack(side="left")
        self._cal_axis_labels: list[dict] = []
        for _ in range(3):
            fr = ttk.Frame(calg)
            fr.pack(fill="x", padx=4, pady=1)
            d: dict = {}
            for key, w in [("idx", 4), ("dir", 5), ("slope", 14), ("baseline", 14)]:
                v = tk.StringVar(value="—")
                ttk.Label(fr, textvariable=v, width=w).pack(side="left")
                d[key] = v
            self._cal_axis_labels.append(d)

        ttk.Button(calg, text="Refresh from CSV",
                   command=lambda: self._refresh_cal_display(self._rig_name_var.get().strip())
                   ).pack(anchor="w", padx=4, pady=(4, 4))

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
            self._rig_name_var.set(rig.get("rig_name", DEFAULT_RIG["rig_name"]))
            self._backup_root_var.set(rig.get("backup_root", DEFAULT_RIG["backup_root"]))

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
            self._refresh_cal_display(rig.get("rig_name", DEFAULT_RIG["rig_name"]))
        except Exception as exc:
            self._status_lbl.config(text=f"Load error: {exc}", foreground="red")

    def _refresh_cal_display(self, rig_name: str):
        cal = load_lc_calibration(rig_name)
        if cal is None:
            self._cal_date_var.set("not found")
            for row in self._cal_axis_labels:
                for v in row.values():
                    v.set("—")
            return
        self._cal_date_var.set(cal.get("date", "—"))
        for axis_info in cal.get("axes", []):
            idx = axis_info.get("idx")
            if idx is None or idx >= len(self._cal_axis_labels):
                continue
            row = self._cal_axis_labels[idx]
            row["idx"].set(str(idx))
            row["dir"].set(axis_info.get("direction", "—"))
            slope = axis_info.get("slope")
            baseline = axis_info.get("baseline")
            row["slope"].set(f"{slope:.6f}" if slope is not None else "—")
            row["baseline"].set(f"{baseline:.1f}" if baseline is not None else "—")

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
            if ax.get("axis") == 2:  # Y1 only
                ax["motor_operation_mode"] = mode_int

        zmq = rig.setdefault("networking", {}).setdefault("zmq_publisher", {})
        zmq["connection_string"] = self._zmq_conn_var.get()
        zmq["topic"]             = self._zmq_topic_var.get()

        rig["data_directory"] = self._data_dir_var.get()
        rig["rig_name"]       = self._rig_name_var.get()
        rig["backup_root"]    = self._backup_root_var.get()

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
# Log tab
# ═══════════════════════════════════════════════════════════════════════════════

class LogTab(ttk.Frame):
    """Tab that displays unhandled Python exceptions caught by the crash handler."""

    _LABEL_NORMAL = "   Log   "
    _LABEL_ERROR  = "   Log (!)   "

    def __init__(self, parent: ttk.Notebook):
        super().__init__(parent)
        self._nb = parent

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(toolbar, text="Clear", command=self._clear).pack(side="left")

        self._text = scrolledtext.ScrolledText(
            self, wrap="word", state="disabled",
            font=("Courier New", 9), relief="flat",
        )
        self._text.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── Public API called from the crash handler ─────────────────────────────

    def append(self, text: str) -> None:
        self._text.configure(state="normal")
        self._text.insert("end", text)
        self._text.see("end")
        self._text.configure(state="disabled")
        if not self._is_selected():
            idx = self._tab_index()
            if idx is not None:
                self._nb.tab(idx, text=self._LABEL_ERROR)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def _tab_index(self):
        for i, tab_id in enumerate(self._nb.tabs()):
            if self._nb.nametowidget(tab_id) is self:
                return i
        return None

    def _is_selected(self) -> bool:
        try:
            return self._nb.index("current") == self._tab_index()
        except Exception:
            return False

    def _on_tab_changed(self, _) -> None:
        if self._is_selected():
            idx = self._tab_index()
            if idx is not None:
                self._nb.tab(idx, text=self._LABEL_NORMAL)


# ═══════════════════════════════════════════════════════════════════════════════
# Application entry point
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Telekinesis Task Setup")
        self.geometry("1300x1200")
        self.minsize(1050, 1200)

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
        self._backup_tab  = BackupTab(nb)
        self._log_tab     = LogTab(nb)
        nb.add(self._config_tab,  text="   Task Configuration   ")
        nb.add(self._monitor_tab, text="   Live Monitor   ")
        nb.add(self._rig_tab,     text="   Rig Config   ")
        nb.add(self._backup_tab,  text="   Backup   ")
        nb.add(self._log_tab,     text=LogTab._LABEL_NORMAL)

        _install_crash_handlers(self, self._log_tab)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        # Stop any running ZMQ subscriber cleanly
        sub = getattr(self._monitor_tab, "_subscriber", None)
        if sub and sub.is_alive():
            sub.stop()
        self._backup_tab.stop_daemon()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
