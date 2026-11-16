"""
tracfone_autoflash_backend.py
-----------------------------
Backend for the console's "Autoflash" page: put a phone through

    1 Fastboot      phone ON -> adb reboot bootloader -> wait for fastboot
    2 Wipe          fastboot -w, then fastboot erase frp
    3 Flash         flashall.bat /d <serial>            (Device Setup's own flash code, unchanged)
    4 Boot          wait for the phone; the "Allow USB debugging?" box is tapped BY YOU on the phone
    5 Skip setup    device_ops.skip_setup_wizard()      (English, unchanged)
    6 Wi-Fi         default saved network               (Wi-Fi page's wifi_manager, unchanged)
    7 Finish        Wi-Fi off, wake the screen, swipe up the lock screen

Nothing existing is edited. This module only IMPORTS (unchanged):
    device_ops.py, tracfone_devsetup_backend.py (flash), tracfone_wifimgr_backend.py + wifi_manager.py (Wi-Fi).

    GET  /api/autoflash/state     config + phones (adb / fastboot) + running jobs + default Wi-Fi
    GET  /api/autoflash/job       one job's steps and log lines  (?id=&since=)
    POST /api/autoflash/config    save build folders / options
    POST /api/autoflash/pick      native folder dialog ({"which": "source"|"target"})
    POST /api/autoflash/start     {serials:[...], kind:"source"|"target", options:{...}, confirm:"FLASH"}
    POST /api/autoflash/stop      {serial}

Plugged into tracfone_web_ui.py by one line calling install(), exactly like the Wi-Fi add-on.
A scenario and Autoflash may never use the same phone at the same time.
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
import tracfone_devsetup_backend as DV
import tracfone_wifimgr_backend as WMG
import wifi_manager as WM

PREFIX = "/api/autoflash"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SERIAL_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
CONFIRM = "FLASH"
LANGUAGE = "English (United States)"

BOOT_TIMEOUT = 420          # s to wait for the phone after a flash / reboot
FASTBOOT_WAIT = 90          # s to wait for fastboot after "adb reboot bootloader"
ALLOW_WAIT = 900            # s to wait for you to accept "Allow USB debugging?" on the phone
WIFI_TRIES = 3
MAX_LINES = 5000
KEEP_JOBS = 30

STEPS = ["Fastboot", "Wipe", "Flash", "Boot + USB allow", "Skip setup", "Default Wi-Fi", "Wi-Fi off + unlock"]

_LOCK = threading.Lock()
_JOBS: dict = {}
_ACTIVE: dict = {}                 # serial -> job id
_scenario = {"serial": None}
_mod = None


class Busy(Exception):
    pass


class _Stop(Exception):
    pass


# ── settings (saved next to the console's other data) ───────────────────────
DEFAULTS = {"source_build": "", "target_build": "", "kind": "source",
            "erase": True, "skip_setup": True, "wifi": True, "finish": True}


def _cfg_path():
    root = getattr(_mod, "ROOT", os.getcwd()) if _mod else os.getcwd()
    d = os.path.join(root, "console_data")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "autoflash_settings.json")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved and type(saved[k]) is type(DEFAULTS[k]):
                    cfg[k] = saved[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(data):
    cfg = load_config()
    for k in DEFAULTS:
        if k in data and type(data[k]) is type(DEFAULTS[k]):
            cfg[k] = data[k].strip().strip('"') if isinstance(data[k], str) else data[k]
    if cfg["kind"] not in ("source", "target"):
        cfg["kind"] = "source"
    with open(_cfg_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


# ── small adb / fastboot helpers ────────────────────────────────────────────
def _run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NOWIN)
    except Exception as e:                                         # tool missing / timeout
        class R:                                                   # noqa: N801
            returncode, stdout, stderr = 1, b"", str(e).encode()
        return R()


def _txt(r):
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def _adb(serial, *args, timeout=30):
    return _run(["adb", "-s", serial, *args], timeout)


def _beep():
    """Three short beeps on the PC itself (Windows speaker), so it is heard even if the browser is muted."""
    try:
        import winsound
        for f in (1000, 1300, 1000):
            winsound.Beep(f, 170)
            time.sleep(0.06)
    except Exception:                                              # not Windows / no sound device
        pass


def adb_states():
    res = {}
    for ln in _txt(_run(["adb", "devices"], 10)).splitlines()[1:]:
        p = ln.split()
        if len(p) >= 2:
            res[p[0]] = p[1]
    return res


def fastboot_serials():
    return [ln.split()[0] for ln in _txt(_run(["fastboot", "devices"], 10)).splitlines() if ln.strip()]


# ── job ─────────────────────────────────────────────────────────────────────
class _Job:
    def __init__(self, serial, kind, build, opts):
        self.id = uuid.uuid4().hex[:10]
        self.serial, self.kind, self.build, self.opts = serial, kind, build, opts
        self.lines: list = []
        self.dropped = 0
        self.steps = [{"name": n, "status": "pending"} for n in STEPS]
        self.status = "running"            # running | done | failed | stopped
        self.waiting_allow = False
        self.stop = threading.Event()
        self.t0 = time.time()
        self.t1 = None
        self.lock = threading.Lock()

    def log(self, line):
        with self.lock:
            for part in str(line).splitlines() or [""]:
                self.lines.append(time.strftime("%H:%M:%S  ") + part)
            if len(self.lines) > MAX_LINES:
                cut = len(self.lines) - MAX_LINES
                del self.lines[:cut]
                self.dropped += cut

    def check(self):
        if self.stop.is_set():
            raise _Stop()

    def nap(self, secs):
        end = time.time() + secs
        while time.time() < end:
            self.check()
            time.sleep(min(0.5, max(0.0, end - time.time())))

    def view(self, since=0):
        with self.lock:
            since = max(0, int(since) - self.dropped)
            return {"id": self.id, "serial": self.serial, "kind": self.kind, "status": self.status,
                    "steps": [dict(s) for s in self.steps], "waiting_allow": self.waiting_allow,
                    "lines": self.lines[since:], "next": len(self.lines) + self.dropped,
                    "seconds": int((self.t1 or time.time()) - self.t0)}


def _set_step(job, i, status):
    with job.lock:
        job.steps[i]["status"] = status


# ── the seven steps ─────────────────────────────────────────────────────────
def s_fastboot(job):
    serial = job.serial
    if serial in fastboot_serials():
        job.log("Phone is already in fastboot.")
        return
    st = adb_states().get(serial)
    if st != "device":
        raise RuntimeError(f"Phone is not usable over ADB (state: {st or 'not found'}). Check cable / USB debugging.")
    job.log("Phone is ON - rebooting it to the bootloader (fastboot)...")
    _adb(serial, "reboot", "bootloader")
    end = time.time() + FASTBOOT_WAIT
    while time.time() < end:
        job.check()
        if serial in fastboot_serials():
            job.nap(2)
            job.log("Phone is in fastboot.")
            return
        job.nap(2)
    raise RuntimeError(f"Phone did not appear in fastboot within {FASTBOOT_WAIT}s.")


def s_wipe(job):
    if not job.opts.get("erase", True):
        job.log("Erase skipped (option off).")
        return "skipped"
    for args in (["-w"], ["erase", "frp"]):
        job.check()
        cmd = ["fastboot", "-s", job.serial, *args]
        job.log("$ " + " ".join(cmd))
        r = _run(cmd, 300)
        out = (_txt(r) + "\n" + (r.stderr or b"").decode("utf-8", "replace")).strip()
        if out:
            job.log(out)
        if r.returncode != 0:
            raise RuntimeError(f"'{' '.join(cmd)}' failed (exit {r.returncode}).")
    job.log("Wipe done.")


def s_flash(job):
    job.check()
    build = DV._clean_folder(job.build)                # same validation as Flash Devices
    DV._flash_folder(job.serial, build, job.log)       # same recipe, line for line (raises on a non-zero exit)
    job.check()
    if job.serial in fastboot_serials():
        job.log("Phone still in fastboot after the flash - rebooting it.")
        _run(["fastboot", "-s", job.serial, "reboot"], 60)


def wait_boot(job, timeout=BOOT_TIMEOUT):
    """ADB reachable AND sys.boot_completed == 1. While the phone shows "Allow USB debugging?" (state
    'unauthorized') nothing can be tapped from the PC, so we wait for the user and flag it to the page."""
    serial = job.serial
    job.log(f"Waiting for {serial} to finish booting...")
    end = time.time() + timeout
    allow_end, last_msg, last_beep = None, 0.0, 0.0
    try:
        while True:
            job.check()
            st = adb_states().get(serial)
            if st == "unauthorized":
                job.waiting_allow = True
                if allow_end is None:
                    allow_end = time.time() + ALLOW_WAIT
                if time.time() > allow_end:
                    raise RuntimeError("USB debugging was not allowed on the phone in time.")
                if time.time() - last_msg > 20:
                    job.log(">>> ON THE PHONE: tick 'Always allow from this computer' and tap ALLOW (waiting...)")
                    last_msg = time.time()
                if time.time() - last_beep > 6:
                    last_beep = time.time()
                    threading.Thread(target=_beep, daemon=True).start()
                end = max(end, time.time() + 60)               # the allow wait does not use up the boot time
            else:
                job.waiting_allow = False
                allow_end = None
                if st == "device":
                    r = _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=15)
                    if r.returncode == 0 and _txt(r) == "1":
                        job.nap(3)
                        job.log("Phone is up.")
                        return
            if time.time() > end:
                raise RuntimeError(f"{serial} did not finish booting within {timeout}s.")
            job.nap(2)
    finally:
        job.waiting_allow = False


def s_boot(job):
    wait_boot(job)


def s_skip_setup(job):
    if not job.opts.get("skip_setup", True):
        job.log("Skip setup turned off (option).")
        return "skipped"
    device_ops.skip_setup_wizard(job.serial, LANGUAGE, job.log)       # unchanged logic
    job.check()
    wait_boot(job, timeout=120)                                      # the bypass reboots; make sure it is really back


def s_wifi(job):
    if not job.opts.get("wifi", True):
        job.log("Default Wi-Fi turned off (option).")
        return "skipped"
    nid = WMG._get_default()
    if nid is None:
        raise RuntimeError("No default Wi-Fi network is set (Wi-Fi page -> mark one as default).")
    ssid = (WM.get_credentials(nid) or {}).get("ssid")
    job.log(f"Connecting to default Wi-Fi '{ssid}'...")
    last = ""
    for i in range(1, WIFI_TRIES + 1):
        job.check()
        res = WM.connect_device(job.serial, nid)                     # unchanged wifi_manager logic
        if res.get("success"):
            job.log(f"Wi-Fi connected: {res.get('ssid')}  ip={res.get('ip_address')}")
            return
        last = res.get("error", "unknown error")
        job.log(f"Wi-Fi attempt {i}/{WIFI_TRIES} failed: {last}")
        job.nap(5)
    raise RuntimeError(f"Could not connect to the default Wi-Fi: {last}")


def s_finish(job):
    if not job.opts.get("finish", True):
        job.log("Finish step turned off (option).")
        return "skipped"
    serial = job.serial
    job.log("Turning Wi-Fi OFF...")
    _adb(serial, "shell", "svc wifi disable")
    end = time.time() + 15
    while True:
        job.check()
        if re.search(r"Wifi is disabled", _txt(_adb(serial, "shell", "cmd wifi status", timeout=20)), re.I):
            job.log("Wi-Fi is off.")
            break
        if time.time() > end:
            raise RuntimeError("Wi-Fi did not turn off.")
        job.nap(1.5)
    job.log("Waking the screen and swiping up the lock screen...")
    m = re.search(r"(\d+)x(\d+)", _txt(_adb(serial, "shell", "wm", "size", timeout=15)))
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    for i in range(2):
        job.check()
        _adb(serial, "shell", "input", "keyevent", "224")                      # wake
        job.nap(1)
        _adb(serial, "shell", "input", "swipe", str(w // 2), str(int(h * 0.85)), str(w // 2), str(int(h * 0.2)), "300")
        job.nap(1.5)
        shown = _txt(_adb(serial, "shell", "dumpsys window policy", timeout=20))
        if not re.search(r"(isKeyguardShowing|mShowingLockscreen|keyguardShowing)\s*=\s*true", shown, re.I):
            job.log("Lock screen is swiped away.")
            return
        job.log(f"Lock screen still reported as showing (try {i + 1}/2).")
    job.log("Swipe sent (could not confirm the lock screen is gone - check the phone).")


STEP_FUNCS = [s_fastboot, s_wipe, s_flash, s_boot, s_skip_setup, s_wifi, s_finish]


def _worker(job):
    try:
        for i, fn in enumerate(STEP_FUNCS):
            job.check()
            _set_step(job, i, "running")
            job.log(f"━━━ STEP {i + 1}: {STEPS[i]} ━━━")
            res = fn(job)
            _set_step(job, i, "skipped" if res == "skipped" else "ok")
        job.status = "done"
        job.log("READY - phone is flashed, set up and unlocked.")
    except _Stop:
        job.status = "stopped"
        job.log("■ Stopped by user.")
        for s in job.steps:
            if s["status"] == "running":
                s["status"] = "stopped"
    except Exception as e:                                         # noqa: BLE001
        job.status = "failed"
        job.log(f"✗ FAILED: {e}")
        for s in job.steps:
            if s["status"] == "running":
                s["status"] = "failed"
    finally:
        job.t1 = time.time()
        job.waiting_allow = False
        with _LOCK:
            if _ACTIVE.get(job.serial) == job.id:
                _ACTIVE.pop(job.serial, None)


def _prune():
    done = [j for j in _JOBS.values() if j.status != "running"]
    done.sort(key=lambda j: j.t1 or 0)
    for j in done[:max(0, len(done) - KEEP_JOBS)]:
        _JOBS.pop(j.id, None)


# ── interlocks with the rest of the console ─────────────────────────────────
def _scenario_uses(serial):
    if not (_mod and _mod.S.get("run")):
        return False
    return _scenario["serial"] in (None, "", serial)


def _other_addon_busy(serial):
    for mod, name in ((DV, "Device Setup"), (WMG, "Wi-Fi Manager")):
        try:
            if mod.is_busy(serial):
                return name
        except Exception:                                          # noqa: BLE001
            pass
    return None


def is_busy(serial):
    return serial in _ACTIVE


def start_jobs(serials, kind, opts, confirm):
    if confirm != CONFIRM:
        raise ValueError("Start was not confirmed.")
    if kind not in ("source", "target"):
        raise ValueError("Choose the Source or Target build.")
    if not isinstance(serials, list) or not serials:
        raise ValueError("Select at least one phone.")
    if len(serials) > 16:
        raise ValueError("At most 16 phones at once.")
    cfg = load_config()
    build = cfg["source_build"] if kind == "source" else cfg["target_build"]
    if not build:
        raise ValueError(f"Set the {kind.upper()} build folder first.")
    build = DV._clean_folder(build)                                # validates the folder + flashall.bat
    o = {k: bool(opts.get(k, cfg[k])) for k in ("erase", "skip_setup", "wifi", "finish")}
    seen, plan = set(), []
    for s in serials:
        s = str(s or "").strip()
        if not _SERIAL_OK.match(s):
            raise ValueError("Invalid device serial.")
        if s in seen:
            raise ValueError(f"{s} appears twice.")
        seen.add(s)
        plan.append(s)
    with _LOCK:
        for s in plan:
            if s in _ACTIVE:
                raise Busy(f"{s} is already running Autoflash.")
            if _scenario_uses(s):
                raise Busy(f"A scenario run is using {s}. Wait for it to finish or stop it first.")
            other = _other_addon_busy(s)
            if other:
                raise Busy(f"{other} is working on {s}.")
        jobs = []
        for s in plan:
            j = _Job(s, kind, build, o)
            _JOBS[j.id] = j
            _ACTIVE[s] = j.id
            jobs.append(j)
        _prune()
    for j in jobs:
        j.log(f"Autoflash starting on {j.serial}  |  {kind.upper()} build: {build}")
        threading.Thread(target=_worker, args=(j,), daemon=True).start()
    return [{"serial": j.serial, "job_id": j.id} for j in jobs]


def stop_job(serial):
    jid = _ACTIVE.get(serial)
    job = _JOBS.get(jid) if jid else None
    if not job:
        raise KeyError("Nothing is running on that phone.")
    job.stop.set()
    job.log("Stop requested...")
    try:                                                           # end an in-flight flashall.bat / adb command
        proc = DV._PROCS.get(serial)
        if proc:
            device_ops.kill_process_tree(proc.pid)
        device_ops.terminate_serial(serial)
    except Exception:                                              # noqa: BLE001
        pass
    return {"ok": True}


def state():
    cfg = load_config()
    states = adb_states()
    fb = fastboot_serials()
    phones = {}
    for s, st in states.items():
        phones[s] = {"serial": s, "state": "ADB" if st == "device" else st.upper()}
    for s in fb:
        phones[s] = {"serial": s, "state": "FASTBOOT"}
    for s, jid in list(_ACTIVE.items()):
        phones.setdefault(s, {"serial": s, "state": "BUSY"})
    for s, p in phones.items():
        jid = _ACTIVE.get(s)
        p["job_id"] = jid
        if not jid:                                                # last finished job of this phone, for the page
            last = [j for j in _JOBS.values() if j.serial == s]
            last.sort(key=lambda j: j.t0)
            p["last_job_id"] = last[-1].id if last else None
    ssid = None
    try:
        nid = WMG._get_default()
        ssid = (WM.get_credentials(nid) or {}).get("ssid") if nid is not None else None
    except Exception:                                              # noqa: BLE001
        pass
    return {"config": cfg, "phones": sorted(phones.values(), key=lambda p: p["serial"]), "steps": STEPS,
            "default_wifi": ssid, "confirm": CONFIRM}


# ── HTTP routing (framework neutral, like the other add-ons) ────────────────
def _json(status, obj):
    return status, "application/json", json.dumps(obj).encode("utf-8")


def owns(raw_path):
    p = urlparse(raw_path).path
    return p == PREFIX or p.startswith(PREFIX + "/")


def _body(raw):
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("Request body must be JSON.")
    if not isinstance(d, dict):
        raise ValueError("Request body must be a JSON object.")
    return d


def handle(method, raw_path, body=b""):
    u = urlparse(raw_path)
    path, q = u.path, parse_qs(u.query)
    try:
        if method == "GET" and path == PREFIX + "/state":
            return _json(200, state())
        if method == "GET" and path == PREFIX + "/job":
            job = _JOBS.get((q.get("id") or [""])[0])
            if not job:
                return _json(404, {"error": "Unknown job."})
            return _json(200, job.view((q.get("since") or ["0"])[0]))
        if method == "POST":
            d = _body(body)
            if path == PREFIX + "/config":
                return _json(200, {"config": save_config(d)})
            if path == PREFIX + "/pick":
                which = d.get("which")
                if which not in ("source", "target"):
                    raise ValueError("Choose source or target.")
                folder = DV.pick_folder()
                if folder:
                    save_config({f"{which}_build": folder})
                return _json(200, {"folder": folder, "config": load_config()})
            if path == PREFIX + "/start":
                opts = d.get("options") if isinstance(d.get("options"), dict) else {}
                return _json(200, {"jobs": start_jobs(d.get("serials"), d.get("kind"), opts, d.get("confirm"))})
            if path == PREFIX + "/stop":
                return _json(200, stop_job(str(d.get("serial") or "")))
    except Busy as e:
        return _json(409, {"error": str(e)})
    except KeyError as e:
        return _json(404, {"error": e.args[0] if e.args else "Not found"})
    except (ValueError, TypeError) as e:
        return _json(400, {"error": str(e) or "Invalid request."})
    except Exception as e:                                         # never take the console down
        return _json(500, {"error": f"Unexpected error: {e}"})
    return _json(404, {"error": "Not found"})


# ── plug-in (mirrors tracfone_wifimgr_backend.install) ──────────────────────
def install(mod):
    """Called once from tracfone_web_ui.py:  AF.install(sys.modules[__name__])

    Adds, without editing the server's code:
      * /api/autoflash/* routes
      * the Autoflash page + nav button (ui/tracfone_autoflash_ui.html), injected into "/"
      * scenario <-> Autoflash interlock for the phone a scenario is using
    """
    global _mod
    if getattr(mod, "_autoflash_installed", False):
        return
    mod._autoflash_installed = True
    _mod = mod
    get0, post0, start0 = mod.H.do_GET, mod.H.do_POST, mod.start
    page_part = os.path.join(mod.HERE, "tracfone_autoflash_ui.html")

    def inject(html):
        try:
            with open(page_part, "rb") as f:
                part = f.read()
        except OSError:
            return html                                  # add-on page missing: serve the normal page
        i = html.lower().rfind(b"</body>")
        return html[:i] + part + html[i:] if i != -1 else html + part

    def guard(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost"):
            return "Blocked: unexpected Host header."
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return "Blocked: request came from another site."
        if self.command == "POST" and self.headers.get("X-Autoflash") != "1":
            return "Blocked: missing X-Autoflash header."
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
            self.send = send                             # only for this request
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
            if n > 1_000_000:
                return self.send(json.dumps({"error": "Request too large."}).encode(), "application/json", 413)
            status, ctype, data = handle("POST", self.path, self.rfile.read(n) if n else b"")
            return self.send(data, ctype, status)
        return post0(self)

    def start(d):                                        # a scenario may not start on a phone Autoflash is using
        serial = (d.get("serial") or "").strip()
        if serial and is_busy(serial):
            return "Autoflash is working on this phone. Wait for it to finish or stop it first."
        err = start0(d)
        if not err:
            _scenario["serial"] = serial or None
        return err

    mod.H.do_GET, mod.H.do_POST, mod.start = do_GET, do_POST, start
