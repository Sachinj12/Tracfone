"""
tracfone_devsetup_backend.py
----------------------------
Backend service for the console's "Device setup" panel (carrier, SKU, SIM/eSIM,
radio, fastboot erase, setup-wizard bypass, battery, reboot modes).

It wraps the unchanged logic in device_ops.py. Nothing here touches the
scenario runner: this module only knows how to
  * list ADB / fastboot devices,
  * run ONE device_ops action per device in a background thread,
  * stream that action's log to the browser (cursor based polling),
  * stop it.

It is plugged into tracfone_web_ui.py by one line calling install(); see the bottom of this file.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

import device_ops

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

MAX_LINES = 5000          # log lines kept per job
KEEP_JOBS = 40            # finished jobs kept in memory

_SERIAL_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
_VALUE_OK = re.compile(r"^[A-Za-z0-9._+\- ]{1,64}$")   # no quotes / shell characters

NUMERIC_ACTIONS = {"Change Battery Level": (0, 100)}

# Shown in the UI and confirmed with a second click before running.
WARNINGS = {
    "Fastboot Erase": "Erases user data, FRP and modem state. The phone is wiped.",
    "Change ro.carrier": "Wipes user data and modem state, then sets the new carrier.",
    "Change SKU": "Resets modem state after changing the SKU.",
    "Change SIM / eSIM Config": "Wipes user data and modem state, then applies the SIM setting.",
    "Change RADIO": "Resets modem state after changing the radio.",
}
WIPES_DATA = {"Fastboot Erase", "Change ro.carrier", "Change SIM / eSIM Config"}


class Busy(Exception):
    """Device already has a task (or a scenario) running."""


class _StopRequested(Exception):
    pass


_LOCK = threading.Lock()
_JOBS: dict[str, "_Job"] = {}
_ACTIVE: dict[str, str] = {}      # serial -> job id
_busy_checker = None


def set_busy_checker(fn):
    """fn(serial) -> falsy when free, or a message string when the device is in use."""
    global _busy_checker
    _busy_checker = fn


# ── Jobs ────────────────────────────────────────────────────────────────────
class _Job:
    def __init__(self, serial, action, param):
        self.id = uuid.uuid4().hex[:12]
        self.serial, self.action, self.param = serial, action, param
        self.status = "running"          # running | done | warn | error | stopped
        self.lines: list[str] = []
        self.dropped = 0
        self.stop_requested = False
        self.kind = "action"
        self.started = time.time()
        self.ended = None
        self._lk = threading.Lock()

    def _append(self, line):
        with self._lk:
            for part in (str(line).splitlines() or [""]):
                self.lines.append(part)
            extra = len(self.lines) - MAX_LINES
            if extra > 0:
                del self.lines[:extra]
                self.dropped += extra

    def log(self, line):
        """Callback handed to device_ops. device_ops echoes every command through
        it *before* spawning, so raising here is how Stop aborts the sequence
        without editing device_ops.py."""
        if self.stop_requested:
            raise _StopRequested()
        self._append(line)

    def read(self, since):
        with self._lk:
            start = max(0, int(since) - self.dropped)
            return list(self.lines[start:]), self.dropped + len(self.lines)

    def saw_failure(self):
        with self._lk:
            return any("FAILED" in l.upper() or "fastboot: error" in l.lower() or "error: cannot" in l.lower()
                       for l in self.lines)


def _worker(job, fn, needs_param):
    status = "done"
    try:
        if job.kind != "flash":
            job._append(f"Starting: {job.action} on {job.serial}" + (f" ({job.param})" if needs_param else ""))
        if needs_param:
            fn(job.serial, job.param, job.log)
        else:
            fn(job.serial, job.log)
        status = "warn" if (job.kind != "flash" and job.saw_failure()) else "done"
    except _StopRequested:
        status = "stopped"
        job._append("Stopped by user.")
    except Exception as e:                      # device_ops raises RuntimeError on failures
        status = "stopped" if job.stop_requested else "error"
        job._append("Stopped by user." if job.stop_requested else f"[ERROR] {e}")
    finally:
        with _LOCK:
            job.status, job.ended = status, time.time()
            if _ACTIVE.get(job.serial) == job.id:
                del _ACTIVE[job.serial]


def _prune():
    done = sorted((j for j in _JOBS.values() if j.status != "running"), key=lambda j: j.started)
    for j in done[:-KEEP_JOBS] if len(done) > KEEP_JOBS else []:
        _JOBS.pop(j.id, None)


def _clean_param(action, needs_param, param):
    if not needs_param:
        return None
    param = "" if param is None else str(param).strip()
    options = device_ops.PARAM_OPTIONS.get(action)
    if options is not None:
        if param not in options:
            raise ValueError("Pick one of the listed options.")
        return param
    if action in NUMERIC_ACTIONS:
        lo, hi = NUMERIC_ACTIONS[action]
        try:
            n = int(param)
        except ValueError:
            raise ValueError(f"Enter a whole number from {lo} to {hi}.")
        if not lo <= n <= hi:
            raise ValueError(f"Enter a whole number from {lo} to {hi}.")
        return str(n)
    if not _VALUE_OK.match(param):
        raise ValueError("Use letters, digits and . _ + - or space only (max 64 characters).")
    return param


def _spawn_many(tasks):
    """tasks: [(serial, action, param, fn, needs_param, kind)]. All-or-nothing: if ANY phone is busy, none starts."""
    if _busy_checker:
        for t in tasks:
            reason = _busy_checker(t[0])
            if reason:
                raise Busy(reason if isinstance(reason, str) else "A scenario run is using this device.")
    started = []
    with _LOCK:
        for t in tasks:
            if t[0] in _ACTIVE:
                raise Busy(f"Another device task is already running on {t[0]}.")
        for serial, action, param, fn, needs_param, kind in tasks:
            job = _Job(serial, action, param)
            job.kind = kind
            _JOBS[job.id] = job
            _ACTIVE[serial] = job.id
            started.append((job, fn, needs_param))
        _prune()
    for job, fn, needs_param in started:
        threading.Thread(target=_worker, args=(job, fn, needs_param), daemon=True).start()
    return [j.id for j, _, _ in started]


def start_job(serial, action, param=None):
    serial = (serial or "").strip()
    if not _SERIAL_OK.match(serial):
        raise ValueError("Invalid device serial.")
    spec = device_ops.ACTIONS.get(action)
    if not spec:
        raise ValueError("Unknown action.")
    needs_param, _prompt, fn = spec
    param = _clean_param(action, needs_param, param)
    return _spawn_many([(serial, action, param, fn, needs_param, "action")])[0]


# ── Full flash: same recipe as Axon's start_flash(), one build folder per phone, all phones in parallel ──
FLASH_LABEL = "Full Flash"
FLASH_CONFIRM = "FLASH"
_FLASH_SCRIPT = "flashall.bat" if os.name == "nt" else "flashall.sh"      # .sh only so the logic can be tested off Windows
_PROCS: dict = {}                                                         # serial -> running flash process


def _clean_folder(path):
    p = str(path or "").strip().strip('"')
    if not p or len(p) > 260 or "\x00" in p:
        raise ValueError("Enter the build folder.")
    if not os.path.isabs(p):
        raise ValueError("Use the full folder path, for example C:\\Builds\\build_01")
    p = os.path.normpath(p)
    if not os.path.isdir(p):
        raise ValueError("That folder does not exist.")
    if not os.path.isfile(os.path.join(p, _FLASH_SCRIPT)):
        raise ValueError(f"{_FLASH_SCRIPT} not found in that folder.")
    return p


def inspect_flash_folder(path):
    """Read-only summary of what the folder's script will do, shown before the user starts a flash."""
    folder = _clean_folder(path)
    with open(os.path.join(folder, _FLASH_SCRIPT), "r", encoding="utf-8", errors="replace") as f:
        lines = f.read(2_000_000).splitlines()
    fb = [l.strip() for l in lines
          if re.search(r"\bfastboot(\.exe)?\b", l, re.I) and not l.strip().lower().startswith(("rem ", "::", "echo", "#"))]
    flashes = [l for l in fb if re.search(r"\bflash\b", l, re.I)]
    notes = []
    if any(re.search(r"erase\s+(userdata|frp|cache|modemst)|\s-w\b|\bformat\b", l, re.I) for l in fb):
        notes.append("Erases user data.")
    if any(re.search(r"oem\s+lock|flashing\s+lock", l, re.I) for l in fb):
        notes.append("Re-locks the bootloader.")
    return {"folder": folder, "script": _FLASH_SCRIPT, "flash_steps": len(flashes), "notes": notes}


def _flash_folder(serial, build, log):
    """Axon's recipe, line for line:
      * `call flashall.bat /d <serial>` inside the build folder. WITHOUT /d the stock Motorola script flashes
        EVERY connected fastboot phone, which double-flashed devices. AXON_FLASH_SERIAL is also set for custom scripts.
      * stdin is a real pipe, not closed: the script has an earlier input checkpoint (around `super`) that aborts
        with exit 1 if it gets EOF. We only press a key when the final 'Press any key' prompt appears.
      * output is read character by character on its own thread: that prompt has no newline, and a background
        `adb start-server` keeps the pipe open after the script ends, so we wait on the PROCESS, not the pipe.
      * success is judged by the exit code only."""
    fa = os.path.join(build, _FLASH_SCRIPT)
    log(f"▶ Flash starting from {build}")
    env = dict(os.environ, AXON_FLASH_SERIAL=serial)
    if os.name == "nt":
        cmd, extra = ["cmd.exe", "/c", "call", fa, "/d", serial], {}
    else:                                                                  # test-only path (no Windows here)
        cmd, extra = ["sh", fa, "/d", serial], {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=build, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **extra)
    _PROCS[serial] = proc

    def pump():
        buf = ""
        try:
            while True:
                ch = proc.stdout.read(1)
                if ch == "":
                    break
                buf += ch
                if ch == "\n":
                    log(buf.rstrip("\n"))
                    buf = ""
                elif "press any key" in buf.lower():
                    log(buf)
                    buf = ""
                    try:
                        proc.stdin.write("\n")
                        proc.stdin.flush()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if buf:
                try:
                    log(buf)
                except Exception:
                    pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        code = proc.wait()
    finally:
        _PROCS.pop(serial, None)
    t.join(timeout=1.5)                                                    # let the last lines arrive
    log(f"[flash exited with code {code}]")
    if code != 0:
        raise RuntimeError(f"Flash exited with code {code}.")


def start_flash(rows, confirm):
    """rows: [{serial, build}]. Validates every phone first (like Axon), then starts them all in parallel."""
    if confirm != FLASH_CONFIRM:
        raise ValueError("Flash was not confirmed.")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Select at least one device.")
    if len(rows) > 16:
        raise ValueError("At most 16 phones per flash.")
    tasks, seen = [], set()
    for r in rows:
        r = r if isinstance(r, dict) else {}
        serial = str(r.get("serial") or "").strip()
        if not _SERIAL_OK.match(serial):
            raise ValueError("Invalid device serial.")
        if serial in seen:
            raise ValueError(f"{serial} appears twice.")
        seen.add(serial)
        try:
            build = _clean_folder(r.get("build"))
        except ValueError as e:
            raise ValueError(f"{serial}: {e}")
        tasks.append((serial, FLASH_LABEL, build, _flash_folder, True, "flash"))
    ids = _spawn_many(tasks)
    return [{"serial": t[0], "job_id": i} for t, i in zip(tasks, ids)]


def pick_folder():
    """Native folder dialog, run in a child process so it cannot disturb the server. '' means cancelled."""
    code = ("import tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
            "print(filedialog.askdirectory(parent=r,title='Choose the build folder (contains flashall.bat)',mustexist=True) or '')")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=300, creationflags=NOWIN,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except subprocess.TimeoutExpired:
        raise ValueError("The folder dialog timed out.")
    if r.returncode != 0:
        raise ValueError("The folder dialog is not available here. Paste the folder path instead.")
    path = r.stdout.decode("utf-8", "replace").strip()
    return os.path.normpath(path) if path else ""


def get_job(job_id, since=0):
    job = _JOBS.get(job_id)
    if not job:
        raise KeyError("Job not found (the server may have restarted).")
    lines, nxt = job.read(since)
    return {"id": job.id, "serial": job.serial, "action": job.action, "kind": job.kind,
            "status": job.status, "lines": lines, "next": nxt}


def stop_job(serial):
    with _LOCK:
        job_id = _ACTIVE.get(serial)
        job = _JOBS.get(job_id) if job_id else None
    if not job:
        return False
    job.stop_requested = True
    proc = _PROCS.get(serial)
    if proc and proc.poll() is None:
        device_ops.kill_process_tree(proc.pid)      # a flash: stop the whole script tree, as Axon's End Task does
    device_ops.terminate_serial(serial)             # a device_ops command: kill the one running now
    return True


def stop_all():
    with _LOCK:
        serials = list(_ACTIVE)
    return [x for x in serials if stop_job(x)]


# ── Catalogue + devices ─────────────────────────────────────────────────────
def list_actions():
    out = []
    for label, (needs_param, prompt, _fn) in device_ops.ACTIONS.items():
        options = device_ops.PARAM_OPTIONS.get(label)
        kind = "none"
        if needs_param:
            kind = "choice" if options else ("number" if label in NUMERIC_ACTIONS else "text")
        out.append({
            "label": label,
            "modes": device_ops.ACTION_MODES.get(label, ["ADB", "FASTBOOT"]),
            "kind": kind,
            "prompt": prompt,
            "options": options or [],
            "explicit_choice": label in device_ops.REQUIRES_EXPLICIT_CHOICE,
            "warn": WARNINGS.get(label),
            "wipes": label in WIPES_DATA,
            "min": NUMERIC_ACTIONS.get(label, (None, None))[0],
            "max": NUMERIC_ACTIONS.get(label, (None, None))[1],
        })
    return out


def _tool_lines(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8, creationflags=NOWIN)
        return [l.split() for l in r.stdout.splitlines() if l.strip()], None
    except FileNotFoundError:
        return [], f"{cmd[0]} was not found on PATH"
    except subprocess.TimeoutExpired:
        return [], f"{cmd[0]} timed out"


def list_devices():
    found, errors = {}, []
    rows, err = _tool_lines(["adb", "devices"])
    if err:
        errors.append(err)
    for parts in rows:
        if parts[0] == "List" or len(parts) < 2:
            continue
        state = parts[1]
        found[parts[0]] = "ADB" if state == "device" else state.upper()   # UNAUTHORIZED / OFFLINE
    rows, err = _tool_lines(["fastboot", "devices"])
    if err:
        errors.append(err)
    for parts in rows:
        if len(parts) >= 2 and parts[1].lower().startswith("fastboot"):
            found[parts[0]] = "FASTBOOT"
    with _LOCK:
        active = dict(_ACTIVE)
    devices = [{"serial": s, "mode": m, "ready": m in ("ADB", "FASTBOOT"), "job_id": active.get(s)}
               for s, m in sorted(found.items())]
    return {"devices": devices, "errors": errors}


def is_busy(serial):
    with _LOCK:
        return serial in _ACTIVE


def any_active():
    with _LOCK:
        return next(iter(_ACTIVE), None)


# ── HTTP routing (framework-neutral; the launcher adapts it to tracfone_web_ui.H) ──
PREFIX = "/api/devsetup"


def _json(status, obj):
    return status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8")


def owns(raw_path):
    return urlparse(raw_path).path.startswith(PREFIX + "/")


def handle(method, raw_path, body=b""):
    """Returns (status, content_type, bytes)."""
    u = urlparse(raw_path)
    path, query = u.path.rstrip("/"), parse_qs(u.query)
    try:
        if method == "GET" and path == PREFIX + "/actions":
            return _json(200, {"actions": list_actions()})
        if method == "GET" and path == PREFIX + "/devices":
            return _json(200, list_devices())
        if method == "GET" and path == PREFIX + "/job":
            return _json(200, get_job((query.get("id") or [""])[0], int((query.get("since") or ["0"])[0] or 0)))
        if method == "GET" and path == PREFIX + "/inspect":
            return _json(200, inspect_flash_folder((query.get("path") or [""])[0]))
        if method == "POST" and path == PREFIX + "/pick-folder":
            return _json(200, {"path": pick_folder()})
        if method == "POST" and path == PREFIX + "/flash":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return _json(400, {"error": "Request body must be JSON."})
            return _json(200, {"jobs": start_flash(data.get("devices"), data.get("confirm"))})
        if method == "POST" and path in (PREFIX + "/run", PREFIX + "/stop"):
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return _json(400, {"error": "Request body must be JSON."})
            if path.endswith("/run"):
                return _json(200, {"job_id": start_job(data.get("serial"), data.get("action"), data.get("param"))})
            if data.get("all"):
                return _json(200, {"stopped": stop_all()})
            return _json(200, {"stopped": stop_job((data.get("serial") or "").strip())})
    except Busy as e:
        return _json(409, {"error": str(e)})
    except KeyError as e:
        return _json(404, {"error": e.args[0] if e.args else "Not found"})
    except ValueError as e:
        return _json(400, {"error": str(e)})
    except Exception as e:                        # never take the console down
        return _json(500, {"error": f"Unexpected error: {e}"})
    return _json(404, {"error": "Not found"})


# ── Plug-in point ───────────────────────────────────────────────────────────
def install(mod):
    """Called once from tracfone_web_ui.py:  DV.install(sys.modules[__name__])

    Adds, without editing the server's code:
      * /api/devsetup/* routes
      * the Device Setup page (ui/tracfone_devsetup_ui.html), injected into "/"
      * scenario <-> Device Setup interlock (neither may use the phone while the other is active)
    """
    if getattr(mod, "_devsetup_installed", False):
        return
    mod._devsetup_installed = True
    get0, post0, start0 = mod.H.do_GET, mod.H.do_POST, mod.start
    page_part = os.path.join(mod.HERE, "tracfone_devsetup_ui.html")

    def inject(html):
        try:
            with open(page_part, "rb") as f:
                part = f.read()
        except OSError:
            return html                              # add-on page missing: serve the normal page
        i = html.lower().rfind(b"</body>")
        return html[:i] + part + html[i:] if i != -1 else html + part

    def guard(self):
        """Another website must not be able to start a flash/erase through the browser: POSTs need our own header
        (that forces a CORS preflight this server never answers) and Host/Origin must be local."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return "Blocked: unexpected Host header."
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return "Blocked: request came from another site."
        if self.command == "POST" and self.headers.get("X-Devsetup") != "1":
            return "Blocked: missing X-Devsetup header."
        return None

    def do_GET(self):
        if owns(self.path):
            bad = guard(self)
            if bad:
                return self.send(json.dumps({"error": bad}).encode(), "application/json", 403)
            status, ctype, data = handle("GET", self.path)
            return self.send(data, ctype, status)
        if urlparse(self.path).path == "/":
            real = self.send

            def send(body, ct="application/json", code=200):
                if isinstance(body, bytes) and ct.startswith("text/html"):
                    body = inject(body)
                return real(body, ct, code)
            self.send = send                         # only for this request
            try:
                return get0(self)
            finally:
                self.__dict__.pop("send", None)
        return get0(self)

    def do_POST(self):
        if owns(self.path):
            bad = guard(self)
            if bad:
                return self.send(json.dumps({"error": bad}).encode(), "application/json", 403)
            n = int(self.headers.get("Content-Length", 0) or 0)
            status, ctype, data = handle("POST", self.path, self.rfile.read(n) if n else b"")
            return self.send(data, ctype, status)
        return post0(self)

    def start(d):                                    # a scenario may not start while Device Setup uses the phone
        serial = (d.get("serial") or "").strip()
        if (is_busy(serial) if serial else any_active()):
            return "Device Setup is working on this phone. Wait for it to finish or press Stop there."
        return start0(d)

    mod.H.do_GET, mod.H.do_POST, mod.start = do_GET, do_POST, start
    set_busy_checker(lambda serial: "A scenario run is active. Stop it or wait until it finishes." if mod.S["run"] else None)
