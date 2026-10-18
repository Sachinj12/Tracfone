# -*- coding: utf-8 -*-
"""Best-effort 'clear recent apps' helper. Separate file; only called by the new web UI AFTER a run has
finished and its screenshots are saved. Never raises, never changes any evidence."""
import re, subprocess, time
import xml.etree.ElementTree as ET


def _adb(serial, *a, timeout=15):
    return subprocess.run(["adb", "-s", serial, *a], capture_output=True, text=True, timeout=timeout)


def clear_recents(serial):
    try:
        _adb(serial, "shell", "input", "keyevent", "187")            # open Recents
        time.sleep(1.8)
        _adb(serial, "shell", "uiautomator", "dump", "/sdcard/_recents.xml")
        xml = _adb(serial, "shell", "cat", "/sdcard/_recents.xml").stdout
        tapped = False
        for n in ET.fromstring(xml[xml.find("<?xml"):] if "<?xml" in xml else xml).iter():
            label = (n.attrib.get("text", "") + " " + n.attrib.get("content-desc", "")).strip().lower()
            if label in ("clear all", "close all", "clear everything"):
                x1, y1, x2, y2 = map(int, re.findall(r"\d+", n.attrib.get("bounds", "0,0,0,0")))
                _adb(serial, "shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                tapped = True
                break
        time.sleep(1)
        _adb(serial, "shell", "input", "keyevent", "3")              # back to Home
        _adb(serial, "shell", "rm", "-f", "/sdcard/_recents.xml")
        return "Recent apps cleared." if tapped else "Recents opened - no 'Clear all' button found (left as is)."
    except Exception as e:                                           # never affects the run
        return f"Clear recents skipped ({e.__class__.__name__})."
