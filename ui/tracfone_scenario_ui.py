#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tracfone Automation Console — Phase 1 UI.

The UI is intentionally separate from the automation backend. Scenario 3 + 5
(Source -> Target, Wi-Fi) is enabled in this phase. One physical run executes
the Scenario 5 superset and automatically creates separate Scenario 3 and
Scenario 5 evidence folders. Scenario 2 A remains enabled. The remaining scenario cards are visible but marked Coming Soon.
"""
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend", "tracfone_wifi_backend.py")
BACKEND_DATA = os.path.join(ROOT, "backend", "tracfone_data_backend.py")   # Scenario 4 / 2B data flows
BACKEND_SCENARIO7 = os.path.join(ROOT, "backend", "tracfone_source_target_wifi_es_backend.py")
BACKEND_SCENARIO9A = os.path.join(ROOT, "backend", "tracfone_target_corrupt_wifi_9a_backend.py")
BACKEND_SCENARIO6 = os.path.join(ROOT, "backend", "tracfone_scenario6_backend.py")   # Scenario 6: copies 3 / 2A / 4 / 2B evidence
RESTART_IMG = os.path.join(ROOT, "assets", "Restart_EN.png")
RESTART_IMG_ES = os.path.join(ROOT, "assets", "Restart_ES.png")
DEFAULT_RESULTS = os.path.join(ROOT, "results")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

BG = "#0b1220"
PANEL = "#111a2b"
PANEL_2 = "#172238"
BORDER = "#263653"
TEXT = "#eef4ff"
MUTED = "#9aabc4"
ACCENT = "#36a3ff"
ACCENT_2 = "#66d7b5"
DANGER = "#ff6b7a"
DISABLED = "#56657d"

PHONE_RE = re.compile(r"^\+?\d{5,15}$")
SHOT_RE = re.compile(r"(?:📸|🖼️|❌)\s*#?\s*(\d+)")

SCENARIOS = [
    ("2 A", "Target → Dummy", "Wi-Fi", "35 shots", True, "TARGET → DUMMY"),
    ("2 B", "Target → Dummy", "Data", "35 shots", True, "DATA ONLY • NO WI-FI SYMBOL"),
    ("3 + 5", "Source → Target", "Wi-Fi", "36 + 38 shots", True, "ONE RUN • SPLIT EVIDENCE"),
    ("4", "Source → Target", "Data", "38 shots", True, "DATA ONLY • NO WI-FI SYMBOL"),
    ("6", "6AA • 6AB • 6BA • 6BB", "Wi-Fi + Data", None, True, "BUILT FROM 3 • 2A • 4 • 2B"),
    ("7", "Source → Target", "Wi-Fi (Spanish)", "Focused evidence", True, "SPANISH UI • PRE / OTA / POST"),
    ("8", "Source → Target", "Wi-Fi", None, False, "NO NOTIFICATION SHOT"),
    ("9 A", "Target → Corrupt", "Wi-Fi", "Device + 3 update shots", True, "PACKAGE VERIFICATION FAILURE"),
    ("9 B", "Source → Target", "Wi-Fi", None, False, "CALL-BANNER VARIANT"),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tracfone Automation Console")
        self.geometry("1220x820")
        self.minsize(1040, 720)
        self.configure(bg=BG)

        self.proc = None
        self.reader_thread = None
        self.events = queue.Queue()
        self.running = False
        self.started_shots = 0
        self.last_total = 37

        self.serial_var = tk.StringVar()
        self.phone_var = tk.StringVar()
        self.results_var = tk.StringVar(value=DEFAULT_RESULTS)
        self.status_var = tk.StringVar(value="Ready — connect one device and select a flow.")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text = tk.StringVar(value="0 / 37")
        self.mode_var = tk.StringVar(value="Full Wi-Fi chain")

        self._configure_style()
        self._build()
        self.after(150, self.poll_events)
        self.after(700, self.refresh_devices)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _configure_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=BORDER)
        style.configure("Dark.Horizontal.TProgressbar", troughcolor="#202c42",
                        background=ACCENT, bordercolor="#202c42", lightcolor=ACCENT,
                        darkcolor=ACCENT)

    def _label(self, parent, text, size=10, color=MUTED, weight="normal"):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Segoe UI", size, weight))

    def _button(self, parent, text, command, enabled=True, primary=False, width=18):
        bg = ACCENT if primary else PANEL_2
        fg = "#07111f" if primary else TEXT
        disabled = "#1d2a3f"
        b = tk.Button(parent, text=text, command=command, width=width, height=2,
                      bg=bg if enabled else disabled, fg=fg if enabled else DISABLED,
                      activebackground="#58b0ff", activeforeground="#07111f",
                      relief="flat", bd=0, cursor="hand2" if enabled else "arrow",
                      font=("Segoe UI", 10, "bold"), disabledforeground=DISABLED)
        if not enabled:
            b.configure(state="disabled")
        return b

    def _build(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)

        # Header
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", padx=28, pady=(22, 12))
        tk.Label(header, text="TRACFONE", bg=BG, fg=TEXT,
                 font=("Segoe UI", 24, "bold")).pack(side="left")
        tk.Label(header, text="  AUTOMATION CONSOLE", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 15, "bold")).pack(side="left", pady=(7, 0))
        self.status_dot = tk.Label(header, text="●  READY", bg=BG, fg=ACCENT_2,
                                   font=("Segoe UI", 10, "bold"))
        self.status_dot.pack(side="right", pady=(8, 0))

        # Device/control strip
        control = tk.Frame(outer, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        control.pack(fill="x", padx=28, pady=(0, 14))
        control.grid_columnconfigure(1, weight=1)
        control.grid_columnconfigure(4, weight=1)

        self._label(control, "ADB DEVICE", 9, MUTED, "bold").grid(row=0, column=0, padx=(18, 8), pady=(14, 4), sticky="w")
        self.device_combo = ttk.Combobox(control, textvariable=self.serial_var,
                                         state="readonly", width=30, style="Dark.TCombobox")
        self.device_combo.grid(row=1, column=0, padx=(18, 8), pady=(0, 14), sticky="ew")
        self._button(control, "REFRESH", self.refresh_devices, True, False, 10).grid(row=1, column=1, padx=4, pady=(0, 14), sticky="w")

        self._label(control, "CALL NUMBER", 9, MUTED, "bold").grid(row=0, column=2, padx=14, pady=(14, 4), sticky="w")
        tk.Entry(control, textvariable=self.phone_var, bg=PANEL_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10),
                 width=20).grid(row=1, column=2, padx=14, pady=(0, 14), sticky="ew")

        self._label(control, "EVIDENCE ROOT", 9, MUTED, "bold").grid(row=0, column=3, padx=14, pady=(14, 4), sticky="w")
        tk.Entry(control, textvariable=self.results_var, bg=PANEL_2, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=("Segoe UI", 10)).grid(row=1, column=3, padx=14, pady=(0, 14), sticky="ew")
        self._button(control, "BROWSE", self.choose_results, True, False, 10).grid(row=1, column=4, padx=(0, 18), pady=(0, 14), sticky="w")

        # Main content: scenario grid + live run panel
        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=28)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        scenarios = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        scenarios.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        scenarios.grid_rowconfigure(1, weight=1)
        scenarios.grid_columnconfigure(0, weight=1)

        title = tk.Frame(scenarios, bg=PANEL)
        title.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        tk.Label(title, text="SCENARIOS", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(title, text="Phase 1: Wi-Fi • 3+5 one-run split • Scroll for all scenarios", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="right", pady=(4, 0))

        # The scenario list is deliberately scrollable.  On a normal 1080p
        # Windows display the first 8 cards fit in the viewport, while 9A/9B
        # are below them.  A fixed grid clipped those cards, making it look
        # as though Scenario 9 did not exist.  Keep every scenario card in the
        # same list and let the user scroll instead of shrinking the cards.
        list_wrap = tk.Frame(scenarios, bg=PANEL)
        list_wrap.grid(row=1, column=0, sticky="nsew", padx=(10, 8), pady=(0, 10))
        list_wrap.grid_rowconfigure(0, weight=1)
        list_wrap.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(list_wrap, bg=PANEL, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(list_wrap, orient="vertical", command=canvas.yview,
                                 bg=PANEL, troughcolor=PANEL_2, activebackground=ACCENT,
                                 highlightthickness=0, bd=0, width=12)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        card_area = tk.Frame(canvas, bg=PANEL)
        card_area.grid_columnconfigure(0, weight=1)
        card_area.grid_columnconfigure(1, weight=1)
        card_area.grid_columnconfigure(2, weight=1)
        canvas_window = canvas.create_window((0, 0), window=card_area, anchor="nw")

        def sync_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(canvas_window, width=canvas.winfo_width())

        card_area.bind("<Configure>", sync_scroll_region)
        canvas.bind("<Configure>", sync_scroll_region)

        def mousewheel(event):
            if event.delta:
                canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind_all("<MouseWheel>", mousewheel)

        for i, item in enumerate(SCENARIOS):
            self._scenario_card(card_area, i, *item)

        live = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        live.grid(row=0, column=1, sticky="nsew")
        live.grid_rowconfigure(5, weight=1)
        live.grid_columnconfigure(0, weight=1)

        tk.Label(live, text="LIVE RUN", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).grid(row=0, column=0, padx=18, pady=(16, 2), sticky="w")
        self.run_label = tk.Label(live, text="No run started", bg=PANEL, fg=MUTED,
                                  font=("Segoe UI", 10))
        self.run_label.grid(row=1, column=0, padx=18, sticky="w")

        self.progress = ttk.Progressbar(live, variable=self.progress_var, maximum=37,
                                        style="Dark.Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, padx=18, pady=(18, 5), sticky="ew")
        self.progress_count = tk.Label(live, textvariable=self.progress_text, bg=PANEL,
                                       fg=TEXT, font=("Segoe UI", 9, "bold"))
        self.progress_count.grid(row=3, column=0, padx=18, sticky="w")

        action = tk.Frame(live, bg=PANEL)
        action.grid(row=4, column=0, padx=18, pady=12, sticky="ew")
        action.grid_columnconfigure(0, weight=1)
        self.start_button = self._button(action, "START 3 + 5 • ONE PHYSICAL RUN", self.start_combined_3_5,
                                         True, True, 24)
        self.start_button.grid(row=0, column=0, sticky="ew", pady=2)
        self.stop_button = self._button(action, "STOP", self.stop_run,
                                        False, False, 24)
        self.stop_button.grid(row=1, column=0, sticky="ew", pady=2)

        self.log = ScrolledText(live, bg="#0a1020", fg="#dce8fb", insertbackground=TEXT,
                                relief="flat", borderwidth=0, font=("Consolas", 9),
                                wrap="word")
        self.log.grid(row=5, column=0, padx=18, pady=(4, 18), sticky="nsew")
        self.log.insert("end", "Ready. Phase 1: Scenario 3 + 5 one-run split + Scenario 2 A, Wi-Fi only.\n")
        self.log.configure(state="disabled")

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", padx=28, pady=(10, 16))
        tk.Label(footer, textvariable=self.status_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        tk.Label(footer, text="One device • Source → Target → Dummy • Separate evidence folders",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")

    def _scenario_card(self, parent, row, code, direction, network, total, enabled, badge):
        col = row % 3
        r = 1 + row // 3
        card = tk.Frame(parent, bg=PANEL_2, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=r, column=col, padx=8, pady=8, sticky="nsew")

        head = tk.Frame(card, bg=PANEL_2)
        head.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(head, text=f"Scenario {code}", bg=PANEL_2, fg=TEXT,
                 font=("Segoe UI", 12, "bold")).pack(side="left")
        tk.Label(head, text=badge, bg=PANEL_2, fg=ACCENT_2 if enabled else DISABLED,
                 font=("Segoe UI", 7, "bold")).pack(side="right")

        tk.Label(card, text=direction, bg=PANEL_2, fg=MUTED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12)
        tk.Label(card, text=f"Network: {network}", bg=PANEL_2, fg=TEXT,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(4, 0))
        count = f"{total} screenshots" if total else "Flow to be integrated"
        tk.Label(card, text=count, bg=PANEL_2, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(2, 8))

        if enabled:
            if code == "3 + 5":
                cmd = self.start_combined_3_5
                text = "RUN 3 + 5 • ONE DEVICE"
            elif code == "4":
                cmd = self.start_scenario4
                text = "RUN SCENARIO 4 • DATA"
            elif code == "2 B":
                cmd = self.start_target_dummy_data
                text = "RUN TARGET → DUMMY • DATA"
            elif code == "6":
                cmd = self.start_scenario6
                text = "BUILD SCENARIO 6 • FROM 3 / 2A / 4 / 2B"
            elif code == "7":
                cmd = self.start_scenario7
                text = "RUN SCENARIO 7 • SOURCE → TARGET • WI-FI (ES)"
            elif code == "9 A":
                cmd = self.start_scenario9a
                text = "RUN SCENARIO 9A • TARGET → CORRUPT • WI-FI"
            else:
                cmd = self.start_target_dummy
                text = "RUN TARGET → DUMMY"
            self._button(card, text, cmd, True, True, 20).pack(fill="x", padx=12, pady=(0, 12))
        else:
            self._button(card, "COMING SOON", lambda: None, False, False, 20).pack(fill="x", padx=12, pady=(0, 12))

    def refresh_devices(self):
        try:
            p = subprocess.run("adb devices", shell=True, capture_output=True, text=True,
                               timeout=10, creationflags=NO_WINDOW)
            devices = []
            for line in p.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
            self.device_combo["values"] = devices
            if devices and self.serial_var.get() not in devices:
                self.serial_var.set(devices[0])
            if devices:
                self.status_var.set(f"Device connected: {devices[0]}")
                self.status_dot.configure(text="●  DEVICE READY", fg=ACCENT_2)
            else:
                self.status_var.set("No ADB device detected.")
                self.status_dot.configure(text="●  NO DEVICE", fg=DANGER)
        except Exception as e:
            self.status_var.set(f"ADB check failed: {e}")
        if not self.running:
            self.after(3000, self.refresh_devices)

    def choose_results(self):
        path = filedialog.askdirectory(initialdir=self.results_var.get() or ROOT,
                                       title="Choose evidence root")
        if path:
            self.results_var.set(path)

    def validate(self, backend=BACKEND):
        serial = self.serial_var.get().strip()
        number = self.phone_var.get().strip().replace(" ", "").replace("-", "")
        if not serial:
            messagebox.showerror("Device required", "Select a connected ADB device first.")
            return None
        if not PHONE_RE.fullmatch(number):
            messagebox.showerror("Call number required", "Enter 5–15 digits, optionally starting with +.")
            return None
        if not os.path.isfile(backend):
            messagebox.showerror("Backend missing", backend)
            return None
        return serial, number

    def command(self, mode):
        backend = (BACKEND_SCENARIO9A if mode == "target_to_corrupt_wifi_9a" else
                   BACKEND_SCENARIO7 if mode == "source_to_target_wifi_es" else
                   BACKEND_DATA if mode in ("source_to_target_data", "target_to_dummy_data") else BACKEND)
        values = self.validate(backend)
        if not values:
            return
        serial, number = values
        results = os.path.abspath(self.results_var.get() or DEFAULT_RESULTS)
        os.makedirs(results, exist_ok=True)
        cmd = [sys.executable, "-u", backend,
               "--serial", serial,
               "--call-number", number,
               "--restart-img", RESTART_IMG_ES if mode == "source_to_target_wifi_es" else RESTART_IMG,
               "--save-root", results,
               "--mode", mode]
        self._start_process(cmd, mode)

    def start_full(self):
        self.command("all")

    def start_source_target(self):
        self.command("source_to_target")

    def start_target_dummy(self):
        self.command("target_to_dummy")

    def start_scenario5(self):
        self.command("scenario5")

    def start_combined_3_5(self):
        self.command("combined_3_5")

    def start_scenario4(self):
        self.command("source_to_target_data")

    def start_target_dummy_data(self):
        self.command("target_to_dummy_data")

    def start_scenario7(self):
        self.command("source_to_target_wifi_es")

    def start_scenario9a(self):
        self.command("target_to_corrupt_wifi_9a")

    def start_scenario6(self):
        """Scenario 6 needs no device: it only copies finished Scenario 3 / 2A / 4 / 2B evidence
        into results/Scenario 6 (6AA, 6AB, 6BA, 6BB). Runs the separate Scenario 6 backend."""
        if self.running:
            messagebox.showinfo("Busy", "Wait for the running automation to finish first.")
            return
        if not os.path.isfile(BACKEND_SCENARIO6):
            messagebox.showerror("Backend missing", BACKEND_SCENARIO6)
            return
        results = os.path.abspath(self.results_var.get() or DEFAULT_RESULTS)
        os.makedirs(results, exist_ok=True)
        cmd = [sys.executable, "-u", BACKEND_SCENARIO6, "--save-root", results]
        self._write_log("\n" + "=" * 64 + "\nSCENARIO 6: " + " ".join(cmd) + "\n" + "=" * 64)
        try:
            child_env = os.environ.copy()
            child_env["PYTHONUTF8"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300, creationflags=NO_WINDOW, env=child_env)
        except Exception as e:
            self.status_var.set(f"Scenario 6 failed: {e}")
            self._write_log(f"Scenario 6 failed: {e}")
            return
        for line in (p.stdout or "").splitlines():
            self._write_log(line)
        if p.stderr:
            self._write_log(p.stderr)
        ok = p.returncode == 0
        self.status_dot.configure(text="●  SCENARIO 6 READY" if ok else "●  SCENARIO 6 INCOMPLETE",
                                  fg=ACCENT_2 if ok else DANGER)
        self.status_var.set("Scenario 6 created in the results folder." if ok else
                            "Scenario 6 incomplete — see the log for the missing scenarios.")

    def _start_process(self, cmd, mode):
        if self.running:
            return
        self.running = True
        self.started_shots = 0
        self.last_total = 38 if mode in ("combined_3_5", "scenario5") else (38 if mode == "source_to_target_data" else (35 if mode in ("target_to_dummy", "target_to_dummy_data") else (17 if mode == "target_to_corrupt_wifi_9a" else (24 if mode == "source_to_target_wifi_es" else 37))))
        self.progress.configure(maximum=self.last_total)
        self.progress_var.set(0)
        self.progress_text.set(f"0 / {self.last_total}")
        self.run_label.configure(text={
            "all": "Source → Target → Dummy • Wi-Fi",
            "source_to_target": "Source → Target • Wi-Fi",
            "target_to_dummy": "Scenario 2 A • Target → Dummy • Wi-Fi",
            "scenario5": "Scenario 5 • Source → Target • Wi-Fi • Manual restart",
            "combined_3_5": "Scenario 3 + 5 • Source → Target • Wi-Fi • One physical run",
            "source_to_target_data": "Scenario 4 • Source → Target • Data only",
            "target_to_dummy_data": "Scenario 2B • Target → Dummy • Data only",
            "source_to_target_wifi_es": "Scenario 7 • Source → Target • Wi-Fi • Spanish restart",
            "target_to_corrupt_wifi_9a": "Scenario 9A • Target → Corrupt • Wi-Fi",
        }[mode], fg=TEXT)
        self.status_dot.configure(text="●  RUNNING", fg=ACCENT)
        self.status_var.set("Automation running — do not disconnect the device.")
        self.start_button.configure(state="disabled", bg="#1d2a3f")
        self.stop_button.configure(state="normal", bg=DANGER, fg="#16070a")
        self._write_log("\n" + "=" * 64 + "\nSTART: " + " ".join(cmd) + "\n" + "=" * 64)

        try:
            # Force UTF-8 for the backend process on Windows. The automation
            # intentionally uses Unicode status markers (🔎, 📶, ✅, ❌, etc.).
            # Without this, a cp1252 console can crash the backend before the
            # first screenshot with UnicodeEncodeError.
            child_env = os.environ.copy()
            child_env["PYTHONUTF8"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace",
                                         bufsize=1, creationflags=NO_WINDOW,
                                         env=child_env)
        except Exception as e:
            self.running = False
            self.proc = None
            self._finish(False, f"Could not start backend: {e}")
            return

        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()

    def _reader(self):
        proc = self.proc
        if not proc:
            return
        try:
            for line in proc.stdout:
                self.events.put(("log", line.rstrip("\n")))
        finally:
            rc = proc.wait()
            self.events.put(("done", rc))

    def poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._write_log(payload)
                    m = SHOT_RE.search(payload)
                    if m:
                        n = int(m.group(1))
                        if "MISSING" not in payload:
                            self.started_shots = max(self.started_shots, n)
                        self.progress_var.set(min(n, self.last_total))
                        self.progress_text.set(f"{min(n, self.last_total)} / {self.last_total}")
                elif kind == "done":
                    self._finish(payload == 0, f"Backend exited with code {payload}.")
        except queue.Empty:
            pass
        self.after(150, self.poll_events)

    def _write_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def stop_run(self):
        if self.proc and self.running:
            if messagebox.askyesno("Stop automation", "Stop the running automation?\n\nThe current transition will be incomplete."):
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                self._write_log("STOP requested by user.")

    def _finish(self, success, message):
        if not self.running:
            return
        self.running = False
        self.proc = None
        self.start_button.configure(state="normal", bg=ACCENT)
        self.stop_button.configure(state="disabled", bg="#1d2a3f", fg=DISABLED)
        self.status_dot.configure(text="●  COMPLETE" if success else "●  STOPPED / FAILED",
                                  fg=ACCENT_2 if success else DANGER)
        self.status_var.set(message)
        if success:
            self.progress_var.set(self.last_total)
            self.progress_text.set(f"{self.last_total} / {self.last_total}")
        self._write_log("\n" + message + "\n")

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Exit", "Automation is running. Stop it and exit?"):
                return
            try:
                if self.proc:
                    self.proc.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
