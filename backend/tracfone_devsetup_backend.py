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

It is wired in by ui/tracfone_web_ui_devsetup.py; tracfone_web_ui.py is not edited.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
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
            return any("FAILED" in l.upper() for l in self.lines)


def _worker(job, fn, needs_param):
    status = "done"
    try:
        job._append(f"Starting: {job.action} on {job.serial}" + (f" ({job.param})" if needs_param else ""))
        if needs_param:
            fn(job.serial, job.param, job.log)
        else:
            fn(job.serial, job.log)
        status = "warn" if job.saw_failure() else "done"
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


def start_job(serial, action, param=None):
    serial = (serial or "").strip()
    if not _SERIAL_OK.match(serial):
        raise ValueError("Invalid device serial.")
    spec = device_ops.ACTIONS.get(action)
    if not spec:
        raise ValueError("Unknown action.")
    needs_param, _prompt, fn = spec
    param = _clean_param(action, needs_param, param)

    if _busy_checker:
        reason = _busy_checker(serial)
        if reason:
            raise Busy(reason if isinstance(reason, str) else "A scenario run is using this device.")

    with _LOCK:
        if serial in _ACTIVE:
            raise Busy("Another device task is already running on this device.")
        job = _Job(serial, action, param)
        _JOBS[job.id] = job
        _ACTIVE[serial] = job.id
        _prune()
    threading.Thread(target=_worker, args=(job, fn, needs_param), daemon=True).start()
    return job.id


def get_job(job_id, since=0):
    job = _JOBS.get(job_id)
    if not job:
        raise KeyError("Job not found (the server may have restarted).")
    lines, nxt = job.read(since)
    return {"id": job.id, "serial": job.serial, "action": job.action,
            "status": job.status, "lines": lines, "next": nxt}


def stop_job(serial):
    with _LOCK:
        job_id = _ACTIVE.get(serial)
        job = _JOBS.get(job_id) if job_id else None
    if not job:
        return False
    job.stop_requested = True
    device_ops.terminate_serial(serial)      # kills the command currently running
    return True


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
PREFIX = "/api/devcfg"


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
        if method == "POST" and path in (PREFIX + "/run", PREFIX + "/stop"):
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return _json(400, {"error": "Request body must be JSON."})
            if path.endswith("/run"):
                return _json(200, {"job_id": start_job(data.get("serial"), data.get("action"), data.get("param"))})
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
