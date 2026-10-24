# -*- coding: utf-8 -*-
"""Tracfone Console + Device Setup add-on launcher.

Runs the EXISTING tracfone_web_ui.py server unchanged and plugs the Device Setup
feature (carrier / SKU / SIM / radio / erase / setup-wizard / battery / reboot modes)
in around it:
  * serves /api/devcfg/* from backend/tracfone_devsetup_backend.py
  * injects ui/tracfone_devsetup_ui.html into the page at "/"
  * stops a scenario and a Device Setup task from using the same phone at once
Nothing in tracfone_web_ui.py / tracfone_web_ui.html / the scenario backends is edited.
"""
import os, sys, threading, webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tracfone_web_ui as w                       # existing server module (adds backend/ to sys.path)
import tracfone_devsetup_backend as dev

FRAGMENT = os.path.join(HERE, "tracfone_devsetup_ui.html")
PAGE = os.path.join(HERE, "tracfone_web_ui.html")


def _page():
    with open(PAGE, "rb") as f:
        html = f.read()
    try:
        with open(FRAGMENT, "rb") as f:
            frag = f.read()
    except OSError:
        return html                               # add-on file missing: serve the normal page
    i = html.lower().rfind(b"</body>")
    return html[:i] + frag + html[i:] if i != -1 else html + frag


def install():
    get0, post0, start0 = w.H.do_GET, w.H.do_POST, w.start

    def do_GET(self):
        if urlparse(self.path).path == "/":
            return self.send(_page(), "text/html; charset=utf-8")
        if dev.owns(self.path):
            status, ctype, data = dev.handle("GET", self.path)
            return self.send(data, ctype, status)
        return get0(self)

    def do_POST(self):
        if dev.owns(self.path):
            n = int(self.headers.get("Content-Length", 0) or 0)
            status, ctype, data = dev.handle("POST", self.path, self.rfile.read(n) if n else b"")
            return self.send(data, ctype, status)
        return post0(self)

    def start(d):                                 # scenario start: refuse while Device Setup uses the phone
        serial = (d.get("serial") or "").strip()
        busy = dev.is_busy(serial) if serial else dev.any_active()
        if busy:
            return "Device Setup is working on this phone. Wait for it to finish or press Stop there."
        return start0(d)

    w.H.do_GET, w.H.do_POST, w.start = do_GET, do_POST, start
    dev.set_busy_checker(lambda serial: "A scenario run is active. Stop it or wait until it finishes." if w.S["run"] else None)


install()

if __name__ == "__main__":
    port = 8780; srv = ThreadingHTTPServer(("127.0.0.1", port), w.H)
    print(f"Tracfone Console + Device Setup: http://127.0.0.1:{port}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
