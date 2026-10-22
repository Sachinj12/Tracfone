# -*- coding: utf-8 -*-
"""Tracfone Console - ADDITIVE backend for the new UI features.

What this file is
  * A separate, optional module. It never imports, edits or calls any scenario/backend file
    (tracfone_wifi_backend, data, 6, 7, 9A, Axon, Axon_Lang are untouched).
  * It only READS the shared run state (S) that tracfone_web_ui.py already keeps, and adds new
    /api/x/* endpoints. If this file is missing or fails to load, the console runs exactly as before.

Endpoints (all under /api/x/)
  GET  progress     stage tracker (Pre-flight ... Report) derived from the live log
  GET  preflight    ADB / device / evidence-path checks for the status bar
  GET  device       read-only device info (model, Android, build, battery) - skipped while a run is active
  GET  history      finished runs        POST history/clear
  GET  settings     POST settings        theme, profiles, sound, welcome flag
  GET  evidence     evidence folders (and PNGs of the current run with ?run=1)
  GET  file?path=   serve one PNG from inside the evidence root only
  POST open         open an evidence folder in the file explorer (inside evidence root only)
  POST browse       native folder picker for "Evidence root"
"""
import json, os, re, subprocess, sys, threading, time
from urllib.parse import parse_qs, urlparse

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
STAGES = ["Pre-flight", "Before update", "Restart", "Update complete", "After update", "Report"]
THEMES = ("classic", "dark", "light", "royal")
SHOT = re.compile(r"(?:📸|🖼️)\s*#?\s*\d+")
R_RESTART = re.compile(r"restart now|restarting|power menu|manual restart", re.I)
R_DONE = re.compile(r"update complete|update installed|updated successfully", re.I)
SERIAL = re.compile(r"[A-Za-z0-9._:\-]{3,64}")

_S = _L = None
_ROOT = ""
_DATA = ""
_IO = threading.Lock()
_pending = {}
_P = {"t0": None, "idx": 0, "stage": 0, "marks": {}}
_devcache = {}


# ───────────────────────── storage ─────────────────────────
def _path(name):
    os.makedirs(_DATA, exist_ok=True)
    return os.path.join(_DATA, name)


def _load(name, default):
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(name, obj):
    with _IO:
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _path(name))


DEFAULTS = {"theme": "classic", "sound": True, "welcomed": False, "active": "Work",
            "profiles": [{"name": "Work", "number": "", "root": "", "clear": True}]}


def get_settings():
    s = dict(DEFAULTS); s.update(_load("console_settings.json", {}))
    if s["theme"] not in THEMES: s["theme"] = "classic"
    if not s["profiles"]: s["profiles"] = list(DEFAULTS["profiles"])
    return s


def set_settings(d):
    s = get_settings()
    if d.get("theme") in THEMES: s["theme"] = d["theme"]
    for k in ("sound", "welcomed"):
        if isinstance(d.get(k), bool): s[k] = d[k]
    if isinstance(d.get("profiles"), list):
        out = []
        for p in d["profiles"][:10]:
            if not isinstance(p, dict): continue
            name = str(p.get("name", "")).strip()[:24]
            if not name or any(o["name"] == name for o in out): continue
            out.append({"name": name, "number": re.sub(r"[^\d+]", "", str(p.get("number", "")))[:16],
                        "root": str(p.get("root", ""))[:400], "clear": bool(p.get("clear", True))})
        if out: s["profiles"] = out
    if isinstance(d.get("active"), str): s["active"] = d["active"]
    if s["active"] not in [p["name"] for p in s["profiles"]]: s["active"] = s["profiles"][0]["name"]
    _save("console_settings.json", s)
    return s


# ───────────────────────── run history ─────────────────────────
def _watch():
    """Records a history entry for every finished run (keyed by the run's start time, so even a run that
    ends within a second is caught). Only reads the shared state."""
    done_t0 = 0
    while True:
        time.sleep(0.5)
        try:
            with _L:
                run, t0, mode, shots, total, status = _S["run"], _S["t0"], _S["mode"], _S["shots"], _S["total"], _S["status"]
                stopped = any("STOP requested by user" in x for x in _S["log"][-40:])
            if run or not t0 or t0 == done_t0 or status not in ("complete", "failed"):
                continue
            done_t0 = t0
            num = _pending.get("number", "")
            h = _load("run_history.json", [])
            h.insert(0, {"id": int(t0 * 1000), "mode": mode, "serial": _pending.get("serial", ""),
                         "number": ("••••" + num[-4:]) if len(num) > 4 else "",
                         "root": _pending.get("root", ""), "start": t0, "seconds": max(1, int(time.time() - t0)),
                         "shots": shots, "total": total, "status": "stopped" if stopped else status})
            _save("run_history.json", h[:200])
        except Exception:
            pass


# ───────────────────────── stage tracker ─────────────────────────
def progress():
    with _L:
        lines, run, status, t0, mode = list(_S["log"]), _S["run"], _S["status"], _S["t0"], _S["mode"]
    if t0 != _P["t0"]:
        _P.update(t0=t0, idx=0, stage=0, marks={0: 0})
    for ln in lines[_P["idx"]:]:
        st = _P["stage"]
        if SHOT.search(ln) and "MISSING" not in ln:
            st = max(st, 4 if st >= 3 else 1)
        if st >= 1 and R_RESTART.search(ln): st = max(st, 2)
        if st >= 2 and R_DONE.search(ln): st = max(st, 3)
        if st != _P["stage"]:
            for i in range(_P["stage"] + 1, st + 1): _P["marks"].setdefault(i, int(time.time() - t0))
            _P["stage"] = st
    _P["idx"] = len(lines)
    stage = _P["stage"]
    if not run and status == "complete": stage = 5
    steps = []
    for i, n in enumerate(STAGES):
        if not run and status == "complete": s = "done"
        elif i < stage: s = "done"
        elif i == stage and (run or status == "failed"): s = "failed" if status == "failed" and not run else "active"
        else: s = "pending"
        steps.append({"name": n, "state": s, "at": _P["marks"].get(i)})
    if not run and status == "ready": steps = [{"name": n, "state": "active" if i == 0 else "pending", "at": None} for i, n in enumerate(STAGES)]
    return {"steps": steps, "stage": stage, "run": run, "status": status, "mode": mode}


# ───────────────────────── device / checks ─────────────────────────
def _adb(*a, timeout=8):
    return subprocess.run(["adb", *a], capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)


def preflight(root):
    out = {"adb": False, "devices": [], "root_ok": False, "root_writable": False, "root": root}
    try:
        r = _adb("devices", timeout=10)
        out["adb"] = r.returncode == 0
        out["devices"] = [l.split()[0] for l in r.stdout.splitlines()[1:] if len(l.split()) >= 2 and l.split()[1] == "device"]
    except Exception:
        pass
    try:
        out["root_ok"] = os.path.isdir(root)
        out["root_writable"] = out["root_ok"] and os.access(root, os.W_OK)
    except Exception:
        pass
    return out


def device_info(serial):
    if not SERIAL.fullmatch(serial or ""): return {"error": "Select a device first."}
    with _L: busy = _S["run"]
    if busy: return dict(_devcache.get(serial) or {}, busy=True)      # never touch the phone during a run
    info = {"serial": serial}
    props = {"model": "ro.product.model", "brand": "ro.product.manufacturer", "android": "ro.build.version.release",
             "build": "ro.build.display.id", "patch": "ro.build.version.security_patch", "carrier": "gsm.operator.alpha"}
    try:
        for k, p in props.items():
            info[k] = _adb("-s", serial, "shell", "getprop", p).stdout.strip()
        m = re.search(r"level:\s*(\d+)", _adb("-s", serial, "shell", "dumpsys", "battery").stdout)
        info["battery"] = int(m.group(1)) if m else None
    except Exception as e:
        return {"error": f"Could not read device ({e.__class__.__name__})."}
    _devcache[serial] = info
    return info


# ───────────────────────── evidence ─────────────────────────
def _inside(p, root):
    p, root = os.path.realpath(p), os.path.realpath(root)
    return p == root or p.startswith(root + os.sep)


def evidence(root, current_run):
    out = {"folders": [], "recent": []}
    t0 = _S["t0"] or 0
    for dp, dn, fs in os.walk(root):
        if dp[len(root):].count(os.sep) > 4: dn[:] = []; continue
        pngs = [f for f in fs if f.lower().endswith(".png")]
        if not pngs: continue
        mt = max(os.path.getmtime(os.path.join(dp, f)) for f in pngs)
        out["folders"].append({"path": dp, "name": os.path.relpath(dp, root), "count": len(pngs), "mtime": mt})
        if current_run:
            out["recent"] += [{"path": os.path.join(dp, f), "name": f, "mtime": os.path.getmtime(os.path.join(dp, f))}
                              for f in pngs if os.path.getmtime(os.path.join(dp, f)) >= t0 - 1]
        if len(out["folders"]) >= 400: break
    out["folders"].sort(key=lambda x: -x["mtime"])
    out["recent"] = sorted(out["recent"], key=lambda x: -x["mtime"])[:24]
    return out


def browse():
    code = ("import tkinter as t;from tkinter import filedialog as f;r=t.Tk();r.withdraw();r.attributes('-topmost',True);"
            "print(f.askdirectory(title='Evidence root') or '')")
    try:
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, creationflags=NOWIN).stdout.strip()
    except Exception:
        return ""


def open_folder(p):
    if os.name == "nt": os.startfile(p)
    else: subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])


# ───────────────────────── router ─────────────────────────
def install(S, L, ROOT):
    global _S, _L, _ROOT, _DATA
    _S, _L, _ROOT, _DATA = S, L, ROOT, os.path.join(ROOT, "console_data")
    threading.Thread(target=_watch, daemon=True).start()


def handle(h, method, raw, body):
    """Returns True when the request was answered here. Anything not under /api/x/ is left to the old server
    (including /api/run, which is only *noted* for the history and then continues unchanged)."""
    u = urlparse(raw); p, q = u.path, parse_qs(u.query)
    if method == "POST" and p == "/api/run":
        if not _S["run"]:
            _pending.update(serial=str(body.get("serial", "")), number=re.sub(r"\D", "", str(body.get("number", ""))),
                            root=str(body.get("root") or _S["root"]))
        return False
    if not p.startswith("/api/x/"): return False
    root = _S["root"]
    try:
        if method == "GET":
            if p == "/api/x/progress": return h.send(progress()) or True
            if p == "/api/x/preflight": return h.send(preflight(root)) or True
            if p == "/api/x/device": return h.send(device_info(q.get("serial", [""])[0])) or True
            if p == "/api/x/history": return h.send(_load("run_history.json", [])) or True
            if p == "/api/x/settings": return h.send(get_settings()) or True
            if p == "/api/x/evidence": return h.send(evidence(root, q.get("run", ["0"])[0] == "1")) or True
            if p == "/api/x/file":
                f = q.get("path", [""])[0]
                if f.lower().endswith(".png") and _inside(f, root) and os.path.isfile(f):
                    return h.send(open(f, "rb").read(), "image/png") or True
                return h.send(b"", "text/plain", 404) or True
        else:
            if p == "/api/x/settings": return h.send(set_settings(body)) or True
            if p == "/api/x/history/clear": _save("run_history.json", []); return h.send({"ok": 1}) or True
            if p == "/api/x/browse": return h.send({"path": browse()}) or True
            if p == "/api/x/open":
                f = str(body.get("path", ""))
                if os.path.isdir(f) and _inside(f, root): open_folder(f); return h.send({"ok": 1}) or True
                return h.send({"error": "Folder is outside the evidence root."}, code=400) or True
    except Exception as e:                                   # extras must never break the console
        return h.send({"error": f"{e.__class__.__name__}: {e}"}, code=500) or True
    return h.send({}, code=404) or True
