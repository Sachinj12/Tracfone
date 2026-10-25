# -*- coding: utf-8 -*-
"""Starts the normal Tracfone Console and plugs the Device Setup add-on into it.

This file does not exist in the GitHub repo, so the auto-updater never replaces it, and it works
whether tracfone_web_ui.py has the old 2-line hook or is the plain repo version.
If the add-on fails to load, the console still starts normally (without the Device Setup tab)."""
import os, sys, threading, webbrowser
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tracfone_web_ui as w                      # the existing server module (adds backend/ to sys.path)

try:
    import tracfone_devsetup_backend as DV
    DV.install(w)                                # safe to call twice: it ignores a second install
except Exception as _e:
    print("Device Setup add-on not loaded:", _e)

if __name__ == "__main__":
    port = int(os.environ.get("TRACFONE_PORT", getattr(w, "PORT", 8780)))
    srv = ThreadingHTTPServer(("127.0.0.1", port), w.H)
    print(f"Tracfone Console: http://127.0.0.1:{port}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
