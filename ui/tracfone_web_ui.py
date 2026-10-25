# -*- coding: utf-8 -*-
"""Tracfone Console - web UI server (stdlib only). Launches the SAME backend commands as the old tkinter UI."""
import json, os, re, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
from tracfone_clear_recents import clear_recents
B = lambda n: os.path.join(ROOT, "backend", n)
BK = {"target_to_corrupt_wifi_9a": "tracfone_target_corrupt_wifi_9a_backend.py",
      "source_to_target_wifi_es": "tracfone_source_target_wifi_es_backend.py",
      "source_to_target_data": "tracfone_data_backend.py", "target_to_dummy_data": "tracfone_data_backend.py",
      "scenario6": "tracfone_scenario6_backend.py"}
TOT = {"combined_3_5": 38, "scenario5": 38, "source_to_target_data": 38, "target_to_dummy": 35,
       "target_to_dummy_data": 35, "target_to_corrupt_wifi_9a": 17, "source_to_target_wifi_es": 24, "scenario6": 4}
SHOT = re.compile(r"(?:📸|🖼️|❌)\s*#?\s*(\d+)")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
S = {"run": False, "mode": "", "log": [], "shots": 0, "total": 0, "status": "ready", "t0": 0, "root": os.path.join(ROOT, "results"), "proc": None}
L = threading.Lock()
try:    # additive console features (history, settings, evidence browser...). Optional: if it fails, the console runs as before.
    import tracfone_console_api as X; X.install(S, L, ROOT)
except Exception as _e: X = None; print("Console extras not loaded:", _e)


def log(t):
    with L: S["log"].append(t)


def worker(cmd, mode, serial, clear):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1, creationflags=NOWIN, env=env)
    except Exception as e:
        log(f"❌ could not start: {e}"); S.update(run=False, status="failed"); return
    S["proc"] = p
    for line in p.stdout:
        line = line.rstrip("\n"); log(line)
        m = SHOT.search(line)
        if m and "MISSING" not in line: S["shots"] = max(S["shots"], int(m.group(1)))
    rc = p.wait()
    if rc == 0 and clear and mode != "scenario6":     # evidence is already saved; best-effort tidy-up
        log("🧹 " + clear_recents(serial))
    S.update(run=False, status="complete" if rc == 0 else "failed")
    if mode == "scenario6" and rc == 0: S["shots"] = S["total"]


def start(d):
    mode, serial, num, root = d["mode"], d.get("serial", ""), d.get("number", "").replace(" ", "").replace("-", ""), d.get("root") or S["root"]
    if S["run"]: return "A run is already active."
    if mode not in TOT: return "Unknown scenario."
    if mode != "scenario6" and (not serial or not re.fullmatch(r"\+?\d{5,15}", num)): return "Select a device and enter a 5-15 digit call number."
    root = os.path.abspath(root); os.makedirs(root, exist_ok=True); S["root"] = root
    be = B(BK.get(mode, "tracfone_wifi_backend.py"))
    if mode == "scenario6": cmd = [sys.executable, "-u", be, "--save-root", root]
    else:
        img = os.path.join(ROOT, "assets", "Restart_ES.png" if mode == "source_to_target_wifi_es" else "Restart_EN.png")
        cmd = [sys.executable, "-u", be, "--serial", serial, "--call-number", num, "--restart-img", img, "--save-root", root, "--mode", mode]
    with L: S.update(log=[], shots=0, total=TOT[mode], status="running", run=True, mode=mode, t0=time.time())
    log("START: " + " ".join(cmd))
    threading.Thread(target=worker, args=(cmd, mode, serial, bool(d.get("clear"))), daemon=True).start()
    return ""


def latest():
    best = (0, None)
    for dp, _, fs in os.walk(S["root"]):
        for f in fs:
            if f.lower().endswith(".png"):
                p = os.path.join(dp, f); t = os.path.getmtime(p)
                if t >= S["t0"] - 1 and t > best[0]: best = (t, p)
    return best[1]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ct="application/json", code=200):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ct); self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if X and X.handle(self, "GET", self.path, None): return
        u = urlparse(self.path)
        if u.path == "/":
            return self.send(open(os.path.join(HERE, "tracfone_web_ui.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/api/devices":
            try: out = subprocess.run("adb devices", shell=True, capture_output=True, text=True, timeout=10, creationflags=NOWIN).stdout
            except Exception: out = ""
            return self.send({"devices": [l.split()[0] for l in out.splitlines()[1:] if len(l.split()) >= 2 and l.split()[1] == "device"], "root": S["root"]})
        if u.path == "/api/state":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with L: return self.send({"run": S["run"], "mode": S["mode"], "shots": S["shots"], "total": S["total"], "status": S["status"], "lines": S["log"][since:], "next": len(S["log"])})
        if u.path == "/api/latest":
            p = latest()
            return self.send(open(p, "rb").read(), "image/png") if p else self.send(b"", "text/plain", 404)
        self.send(b"", "text/plain", 404)
    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if X and X.handle(self, "POST", self.path, d): return
        if self.path == "/api/run": return self.send({"error": start(d)})
        if self.path == "/api/stop":
            if S["proc"] and S["run"]: S["proc"].terminate(); log("STOP requested by user.")
            return self.send({"ok": 1})
        self.send({}, code=404)


try: import tracfone_devsetup_backend as DV; DV.install(sys.modules[__name__])   # Device Setup add-on (optional)
except Exception as _e: print("Device Setup add-on not loaded:", _e)


if __name__ == "__main__":
    port = 8780; srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Tracfone Console: http://127.0.0.1:{port}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
