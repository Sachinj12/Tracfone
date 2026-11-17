"""
tracfone_shortcut_icon.py
-------------------------
Gives the desktop "Tracfone" shortcut the Tracfone icon, automatically, on every user's PC.

Nothing to run by hand: the console calls install() once at start-up (one hook line in
tracfone_web_ui.py, like the other add-ons) and this module, in a background thread:

  1. turns  working/assets/tracfone_icon.png  into  working/assets/tracfone.ico   (needs Pillow)
  2. looks at the Desktop shortcuts named "Tracfone*.lnk"
  3. changes the icon ONLY of shortcuts that start THIS project's RUN_TRACFONE_WEB_UI.bat,
     and only when the icon is not already the Tracfone one.

Safe by design: Windows only, never raises, never blocks the console, touches nothing else.
Turn it off with the environment variable TRACFONE_NO_ICON=1.
Can also be run by hand:   python tracfone_shortcut_icon.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)                         # ...\working
ROOT = os.path.dirname(WORKING)                         # project folder (has RUN_TRACFONE_WEB_UI.bat)
PNG = os.path.join(WORKING, "assets", "tracfone_icon.png")
ICO = os.path.join(WORKING, "assets", "tracfone.ico")
LAUNCHER = os.path.join(ROOT, "RUN_TRACFONE_WEB_UI.bat")
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ico = $env:TF_ICO; $bat = $env:TF_BAT
$w = New-Object -ComObject WScript.Shell
$changed = 0
$dirs = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
foreach ($d in $dirs) {
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $d -Filter 'Tracfone*.lnk') {
        try {
            $s = $w.CreateShortcut($f.FullName)
            if ($s.TargetPath -and ($s.TargetPath -ieq $bat)) {
                $want = $ico + ',0'
                if ($s.IconLocation -ine $want) { $s.IconLocation = $want; $s.Save(); $changed++ }
            }
        } catch { }
    }
}
Write-Output $changed
"""


def _make_ico() -> bool:
    """Create / refresh tracfone.ico from the PNG. True when a usable .ico exists afterwards."""
    if not os.path.isfile(PNG):
        return os.path.isfile(ICO)
    try:
        if os.path.isfile(ICO) and os.path.getmtime(ICO) >= os.path.getmtime(PNG):
            return True
        from PIL import Image                           # installed by Setup (pillow)
        im = Image.open(PNG).convert("RGBA")
        im.save(ICO, format="ICO", sizes=[s for s in SIZES if s[0] <= max(im.size)])
        return True
    except Exception:                                    # Pillow missing / file locked: just skip
        return os.path.isfile(ICO)


def apply() -> int:
    """Returns how many shortcuts were changed (0 when nothing to do / not Windows)."""
    if sys.platform != "win32" or os.environ.get("TRACFONE_NO_ICON") == "1":
        return 0
    if not os.path.isfile(LAUNCHER) or not _make_ico():
        return 0
    env = dict(os.environ, TF_ICO=os.path.normpath(ICO), TF_BAT=os.path.normpath(LAUNCHER))
    try:
        r = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS],
            capture_output=True, text=True, timeout=40, env=env, creationflags=NOWIN)
        n = int((r.stdout or "0").strip().splitlines()[-1])
    except Exception:
        return 0
    if n:
        try:                                             # ask Windows to refresh its icon cache
            subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "ie4uinit.exe"), "-show"],
                           timeout=20, creationflags=NOWIN)
        except Exception:
            pass
    return n


def _worker():
    try:
        n = apply()
        if n:
            print(f"Tracfone icon applied to {n} desktop shortcut(s).")
    except Exception:
        pass


def install(mod=None):
    """Called once from tracfone_web_ui.py. Runs in the background; never delays or breaks start-up."""
    threading.Thread(target=_worker, daemon=True, name="tracfone-icon").start()


if __name__ == "__main__":
    print(f"Tracfone icon applied to {apply()} desktop shortcut(s).")
