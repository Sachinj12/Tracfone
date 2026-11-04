"""
tracfone_wifimgr_backend.py
---------------------------
Backend for the console's "Wi-Fi" page: save Wi-Fi networks (manually or from a QR
image) and connect / turn Wi-Fi off on ADB phones.

All real work is done by wifi_manager.py, which is used UNCHANGED (copied as given).
This file only adds web routes around it:

    GET  /api/wifimgr/status           saved networks + which optional packages are installed
    GET  /api/wifimgr/devices          ADB phones with live Wi-Fi status
    GET  /api/wifimgr/device-networks  networks saved on one phone (?serial=)
    POST /api/wifimgr/networks         add a network
    POST /api/wifimgr/networks/update  edit a network
    POST /api/wifimgr/networks/delete  delete a network
    POST /api/wifimgr/qr               add a network from a QR image (base64 JSON)
    POST /api/wifimgr/connect          connect ONE phone to ONE saved network
    POST /api/wifimgr/disconnect       turn Wi-Fi off on ONE phone

Passwords are never sent to the browser (wifi_manager.list_networks() only reports
"has_password").  It is plugged into tracfone_web_ui.py by one line calling install(),
exactly like the Device Setup add-on.  Nothing here touches the scenario runner, except
that a scenario and a Wi-Fi action may not use the same phone at the same time.
"""
from __future__ import annotations

import base64
import binascii
import importlib.util
import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import wifi_manager as WM

PREFIX = "/api/wifimgr"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_SERIAL_OK = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
MAX_BODY = 16 * 1024 * 1024          # QR images arrive base64-encoded inside the JSON body

_LOCK = threading.Lock()
_ACTIVE = {}                          # serial -> what the phone is doing right now
_scenario = {"serial": None}          # phone of the scenario started from the console
_mod = None


class Busy(Exception):
    pass


def _json(status, obj):
    return status, "application/json", json.dumps(obj).encode("utf-8")


# ── helpers ────────────────────────────────────────────────────────────────
def _serial(value):
    s = (value or "").strip() if isinstance(value, str) else ""
    if not _SERIAL_OK.match(s):
        raise ValueError("A valid device serial is required.")
    return s


def _adb_serials():
    """Same detection the console uses: `adb devices`, state == device."""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=10, creationflags=NOWIN).stdout
    except Exception:
        return []
    return [l.split()[0] for l in out.splitlines()[1:] if len(l.split()) >= 2 and l.split()[1] == "device"]


def _features():
    return {"cryptography": importlib.util.find_spec("cryptography") is not None,
            "opencv": importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None}


def _scenario_uses(serial):
    if not (_mod and _mod.S.get("run")):
        return False
    return _scenario["serial"] in (None, "", serial)   # unknown phone -> be safe and block


def _claim(serial, what):
    with _LOCK:
        if serial in _ACTIVE:
            raise Busy(f"This phone is already busy ({_ACTIVE[serial]}).")
        if _scenario_uses(serial):
            raise Busy("A scenario run is using this phone. Wait for it to finish or stop it first.")
        try:                                           # Device Setup (flash / carrier ...) on the same phone
            import tracfone_devsetup_backend as DV
            if DV.is_busy(serial):
                raise Busy("Device Setup is working on this phone.")
        except ImportError:
            pass
        _ACTIVE[serial] = what


def _release(serial):
    with _LOCK:
        _ACTIVE.pop(serial, None)


def is_busy(serial):
    return serial in _ACTIVE


def _errors(errs):
    return "; ".join(errs) if isinstance(errs, (list, tuple)) else str(errs)


def _body(raw):
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("Request body must be JSON.")
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")
    return data


def _s(data, key):
    v = data.get(key)
    return v if isinstance(v, str) else None


# ── route handling ─────────────────────────────────────────────────────────
def owns(raw_path):
    return urlparse(raw_path).path.startswith(PREFIX + "/")


def handle(method, raw_path, raw_body=b""):
    """Returns (status, content_type, bytes)."""
    u = urlparse(raw_path)
    path, query = u.path.rstrip("/"), parse_qs(u.query)
    try:
        if method == "GET":
            if path == PREFIX + "/status":
                return _json(200, {"networks": WM.list_networks(), "security_types": WM.SECURITY_TYPES,
                                   "features": _features()})
            if path == PREFIX + "/devices":
                serials = _adb_serials()
                if not serials:
                    return _json(200, {"devices": []})
                with ThreadPoolExecutor(max_workers=min(8, len(serials))) as ex:
                    rows = list(ex.map(WM.get_device_wifi_status, serials))
                for r in rows:
                    r["busy"] = _ACTIVE.get(r["serial"]) or ("scenario running" if _scenario_uses(r["serial"]) else None)
                return _json(200, {"devices": rows})
            if path == PREFIX + "/device-networks":
                serial = _serial((query.get("serial") or [""])[0])
                return _json(200, {"serial": serial, "saved": WM.get_device_saved_networks(serial)})

        if method == "POST":
            data = _body(raw_body)
            if path == PREFIX + "/networks":
                net, errs = WM.create_network(_s(data, "label"), _s(data, "ssid"), _s(data, "security_type"),
                                              _s(data, "username"), _s(data, "password"),
                                              source="manual", hidden=bool(data.get("hidden")))
                if errs:
                    return _json(400, {"error": _errors(errs)})
                return _json(200, {"network": net})
            if path == PREFIX + "/networks/update":
                net, errs = WM.update_network(
                    int(data.get("id")), _s(data, "label"), _s(data, "ssid"), _s(data, "security_type"),
                    _s(data, "username"), _s(data, "password"),
                    password_provided=bool(data.get("password_provided")),
                    username_provided=bool(data.get("username_provided")),
                    hidden=bool(data["hidden"]) if "hidden" in data else None)
                if errs:
                    return _json(400, {"error": _errors(errs)})
                return _json(200, {"network": net})
            if path == PREFIX + "/networks/delete":
                if not WM.delete_network(int(data.get("id"))):
                    return _json(404, {"error": "Saved network not found."})
                return _json(200, {"ok": True})
            if path == PREFIX + "/qr":
                b64 = _s(data, "image") or ""
                b64 = b64.split(",", 1)[1] if b64.startswith("data:") and "," in b64 else b64
                try:
                    image = base64.b64decode(b64, validate=False)
                except (binascii.Error, ValueError):
                    return _json(400, {"error": "The image could not be read."})
                if not image:
                    return _json(400, {"error": "No image received."})
                found = WM.decode_wifi_qr(image)            # {"ssid","password","security_type","hidden"}
                label = (_s(data, "label") or "").strip() or found["ssid"]
                net, errs = WM.create_network(label, found["ssid"], found["security_type"], None,
                                              found["password"], source="qr", hidden=found["hidden"])
                if errs:
                    return _json(400, {"error": _errors(errs)})
                return _json(200, {"network": net})
            if path == PREFIX + "/connect":
                serial = _serial(data.get("serial"))
                _claim(serial, "connecting to Wi-Fi")
                try:
                    return _json(200, WM.connect_device(serial, int(data.get("network_id"))))
                finally:
                    _release(serial)
            if path == PREFIX + "/disconnect":
                serial = _serial(data.get("serial"))
                _claim(serial, "turning Wi-Fi off")
                try:
                    return _json(200, WM.disconnect_device(serial))
                finally:
                    _release(serial)
    except Busy as e:
        return _json(409, {"error": str(e)})
    except WM.WifiFeatureUnavailable as e:
        return _json(503, {"error": str(e)})
    except (ValueError, TypeError) as e:
        return _json(400, {"error": str(e) or "Invalid request."})
    except Exception as e:                               # never take the console down
        return _json(500, {"error": f"Unexpected error: {e}"})
    return _json(404, {"error": "Not found"})


# ── plug-in (mirrors tracfone_devsetup_backend.install) ────────────────────
def install(mod):
    """Called once from tracfone_web_ui.py:  WM_.install(sys.modules[__name__])

    Adds, without editing the server's code:
      * /api/wifimgr/* routes
      * the Wi-Fi page + nav button (ui/tracfone_wifimgr_ui.html), injected into "/"
      * scenario <-> Wi-Fi interlock for the phone a scenario is using
    """
    global _mod
    if getattr(mod, "_wifimgr_installed", False):
        return
    mod._wifimgr_installed = True
    _mod = mod
    get0, post0, start0 = mod.H.do_GET, mod.H.do_POST, mod.start
    page_part = os.path.join(mod.HERE, "tracfone_wifimgr_ui.html")

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
        if self.command == "POST" and self.headers.get("X-Wifimgr") != "1":
            return "Blocked: missing X-Wifimgr header."
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
            if n > MAX_BODY:
                return self.send(json.dumps({"error": "Request too large."}).encode(), "application/json", 413)
            status, ctype, data = handle("POST", self.path, self.rfile.read(n) if n else b"")
            return self.send(data, ctype, status)
        return post0(self)

    def start(d):                                        # a scenario may not start on a phone Wi-Fi Manager is using
        serial = (d.get("serial") or "").strip()
        if serial and is_busy(serial):
            return f"Wi-Fi Manager is working on this phone ({_ACTIVE[serial]}). Wait for it to finish."
        err = start0(d)
        if not err:
            _scenario["serial"] = serial or None
        return err

    mod.H.do_GET, mod.H.do_POST, mod.start = do_GET, do_POST, start
