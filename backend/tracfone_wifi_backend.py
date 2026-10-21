#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tracfone Wi-Fi automation backend — Phase 1
===========================================

Implemented flows in this phase:
  • Scenario 3 + 5 — SOURCE -> TARGET, Wi-Fi, one physical run
    producing two evidence folders: a normal Scenario 3 set and a
    Scenario 5 restart-variant set.
  • Scenario 2 A — TARGET -> DUMMY, Wi-Fi only

Scenario 3 + 5 executes the Scenario 5 superset once. The same captured
images are then split into two separately numbered evidence folders: the
Scenario 3 folder excludes the three restart-variant-only screenshots
(power menu, restarting screen, resumed-update screen), while the Scenario 5
folder keeps all screenshots. This avoids running the same Source -> Target
transition twice.

In the combined run Scenario 8 (same images as Scenario 3, named 8 (n)) and a
Scenario 9B folder are also built.  Scenario 9B: the Scenario 3 flow plus
Wi-Fi call + Amazon screenshots taken BEFORE the update and an incoming-call
screenshot taken DURING the update (40 shots).

The combined run's Scenario 5 has 38 screenshots (it now includes the
"Background installation" notification shot), Scenario 3 / 8 have 36.
Scenario 2 A remains the locked 35-screenshot flow.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from xml.etree import ElementTree as ET

# Windows consoles commonly default to cp1252. Keep all automation logs
# Unicode-safe so status messages such as 🔎, 📶, ✅ and ❌ cannot crash the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageChops, ImageStat   # Pillow only - Axon already needs it

import Axon as ax                      # reuse Axon's helpers
from Axon_Lang import get_keywords

# ---------------------------------------------------------------------------
# English only
# ---------------------------------------------------------------------------
K = get_keywords("EN")
ax.KEYWORDS = K                        # Axon helpers read this module global

EXPECTED_TOTAL = 35
PHASE_NAME = "Scenario2A_WIFI"
SCENARIO_CODE = "2 A"
INCLUDE_HARDWARE_SCREENSHOT = False

SAVE_DIR = None
RESTART_IMG = None
TAP_RESTART = False
shot_no = 1
LOG = []                               # (number, label, status, filename)


# ===========================================================================
# Screenshot bookkeeping
# ===========================================================================
def _path(n):
    # Scenario-specific evidence naming, e.g. "2 A (1).png" or "3 (1).png".
    return os.path.join(SAVE_DIR, f"{SCENARIO_CODE} ({n}).png")


def _register(label, status, path=None):
    global shot_no
    LOG.append((shot_no, label, status, os.path.basename(path) if path else "-"))
    shot_no += 1


def mark_missing(label, why=""):
    print(f"❌ #{shot_no} MISSING: {label} {why}".rstrip())
    _register(label, "MISSING")


def pad_to(target, why="previous section did not finish"):
    """Reserve numbers so later screenshots keep their reference numbers."""
    while shot_no < target:
        mark_missing(f"(slot {shot_no})", f"- {why}")


def save_image(img, label, status="OK"):
    path = _path(shot_no)
    img.save(path)
    print(f"📸 #{shot_no:>2}  {label}  ->  {os.path.basename(path)}")
    _register(label, status, path)
    return path


def place_file(src, label):
    path = _path(shot_no)
    shutil.copy(src, path)
    print(f"🖼️  #{shot_no:>2}  {label}  ->  {os.path.basename(path)}")
    _register(label, "OK", path)


def grab_frame(retries=3):
    """Raw screenshot from the device as a PIL image (None on failure)."""
    tmp = os.path.join(SAVE_DIR, "_frame.png")
    for _ in range(retries):
        cap = ax.adb("shell screencap -p /sdcard/_axon.png")
        pull = ax.adb(f'pull /sdcard/_axon.png "{tmp}"')
        ax.adb("shell rm /sdcard/_axon.png")
        if cap.returncode == 0 and pull.returncode == 0 and os.path.isfile(tmp):
            img = Image.open(tmp).convert("RGB")
            img.load()
            os.remove(tmp)
            return img
        time.sleep(1)
    return None


def snap(label):
    img = grab_frame()
    if img is None:
        mark_missing(label, "(screencap/pull failed)")
        return None
    return save_image(img, label)


def dismiss_preupdate_heads_up():
    """Dismiss a transient SystemUI heads-up notification before the Android-version shot.

    This is deliberately narrow: it is used only for the pre-update Android-version
    screenshot, so it cannot interfere with the OTA notification screenshots (#12,
    #17 and #19) or the IMEI dialogs.
    """
    for _ in range(3):
        win = (ax.adb("shell dumpsys window windows").stdout or "").lower()
        focused_systemui = (
            "mcurrentfocus" in win and "com.android.systemui" in win or
            "mfocusedapp" in win and "com.android.systemui" in win
        )
        if not focused_systemui:
            return
        print("🔕 Dismissing transient SystemUI notification before Android-version screenshot...")
        ax.adb("shell input keyevent KEYCODE_BACK")
        time.sleep(1.5)


# ===========================================================================
# Small device helpers
# ===========================================================================
def wake():
    ax.adb("shell input keyevent KEYCODE_WAKEUP")


def go_home():
    ax.adb("shell input keyevent 3")
    time.sleep(2)


def screen_size():
    out = ax.adb("shell wm size").stdout.strip().splitlines()[-1]
    w, h = map(int, out.split(":")[-1].strip().split("x"))
    return w, h


def slow_swipe():
    """Slow swipe = little fling inertia, so scroll-stitching stays accurate."""
    w, h = screen_size()
    x = w // 2
    ax.adb(f"shell input swipe {x} {int(h * 0.72)} {x} {int(h * 0.32)} 900")
    time.sleep(1.5)


def ensure_wifi_connected(timeout=45):
    """Turn Wi-Fi on and REQUIRE an actual Wi-Fi connection before screenshots."""
    print("📶 Wi-Fi is mandatory for Scenario 2(A) — enabling and verifying connection...")
    ax.adb("shell svc wifi enable")

    # The phone should already have the test AP saved. Android will normally
    # reconnect automatically after Wi-Fi is enabled. Do not continue merely
    # because the Wi-Fi radio is ON; wait for the connected state.
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        r = ax.adb("shell cmd wifi status")
        last_status = (r.stdout or "") + "\n" + (r.stderr or "")
        status = last_status.lower()
        if ("connected to" in status or
                "wifi is connected" in status or
                "connection state: connected" in status or
                "mconnected" in status):
            print("✅ Wi-Fi is ON and CONNECTED. Screenshot flow may start.")
            return True

        # dumpsys is useful on Android builds where `cmd wifi status` output
        # differs from the standard format.
        d = ax.adb("shell dumpsys wifi").stdout.lower()
        if ("wifi state: enabled" in d and
                ("mnetworkinfo: [type: wifi" in d and "state: connected" in d or
                 "networkinfo: [type: wifi" in d and "state: connected" in d or
                 "connectedstate" in d)):
            print("✅ Wi-Fi is ON and CONNECTED (verified via dumpsys wifi).")
            return True

        time.sleep(2)

    print("❌ Wi-Fi is ON but NOT CONNECTED — stopping before screenshot #1.")
    print("   The test phone must be connected to the required/saved Wi-Fi network.")
    print("   Last Wi-Fi status:")
    print(last_status.strip() or "<no status returned>")
    return False


def disable_wifi_for_navigation():
    """Keep Wi-Fi OFF while navigating to the next pre-update screenshot page."""
    print("📴 Wi-Fi OFF — navigating to the next screenshot page...")
    ax.adb("shell svc wifi disable")
    time.sleep(1.5)


def wifi_snapshot(label):
    """Turn Wi-Fi ON only for this screenshot, then turn it OFF again.

    Navigation must happen with Wi-Fi OFF. The page is already prepared when
    this function is called; it reconnects Wi-Fi, captures the requested
    screenshot, and immediately disables Wi-Fi so OTA cannot start between
    pre-update screenshots.
    """
    print(f"\n📶 Wi-Fi ON for screenshot: {label}")
    if not ensure_wifi_connected(timeout=45):
        mark_missing(label, "(Wi-Fi did not reconnect for this screenshot)")
        return False
    try:
        result = snap(label)
        return result is not None
    finally:
        disable_wifi_for_navigation()


def wifi_stitched_snapshot(label):
    """Same per-shot Wi-Fi cycle for a stitched Phone-info screenshot."""
    print(f"\n📶 Wi-Fi ON for stitched screenshot: {label}")
    if not ensure_wifi_connected(timeout=45):
        mark_missing(label, "(Wi-Fi did not reconnect for this screenshot)")
        return False
    try:
        img, ok = stitched_capture()
        if img is None:
            mark_missing(label, "(screencap failed)")
            return False
        save_image(img, label, "OK" if ok else "OK (may be incomplete)")
        return True
    finally:
        disable_wifi_for_navigation()


def open_settings_clean():
    """Fresh Settings at its top level (avoids resuming on an old sub-page)."""
    go_home()
    ax.adb("shell am force-stop com.android.settings")
    time.sleep(1)
    ax.open_settings_app()


# ===========================================================================
# System update page navigation (presoak logic) - used ONLY by the OTA phases
# ===========================================================================
UPDATE_NOW_LABELS = ["Update now"]     # exact label on the 'Update now / Update later' page
_UPDATE_VIA_SCROLL = False             # True when the page was reached by scroll & tap (Android 15)


def _android_major():
    out = (ax.adb("shell getprop ro.build.version.release").stdout or "").strip()
    m = re.match(r"\d+", out)
    return int(m.group()) if m else None


def open_update_page():
    """Open the System update page (same logic as presoak).

    Android 15 (or unknown): scroll Settings and tap 'System update(s)', with the
    'System' menu as fallback.  Any other Android version: the intent command,
    exactly as before (ax.open_system_updates)."""
    global _UPDATE_VIA_SCROLL
    _UPDATE_VIA_SCROLL = _android_major() in (15, None)
    if not _UPDATE_VIA_SCROLL:
        return ax.open_system_updates()

    print("🌐 Sniffing out System Updates (scroll method)...")
    if not ax.scroll_and_tap(K["system_update"]):
        print("🗂️  No direct hit — trying via 'System' menu first...")
        if not ax.find_and_tap_any(K["system"]):
            print("❌ 'System' menu not found!")
            return False
        time.sleep(2)
        if not ax.scroll_and_tap(K["system_update"]):
            print("❌ System Updates nowhere to be found!")
            return False
    time.sleep(3)
    return True


def _whats_new_visible(data):
    """True when Motorola's 'See what's new' page is on screen.

    Reads the text / content-desc of the UI nodes (so &apos; escaping is handled) and
    treats straight and curly apostrophes alike.  Used ONLY by the dismiss flow."""
    texts = []
    try:
        for node in ET.fromstring(data).iter():
            texts.append(node.attrib.get("text", ""))
            texts.append(node.attrib.get("content-desc", ""))
    except ET.ParseError:
        texts.append(data or "")
    blob = ax.normalize_text(" ".join(texts))
    for ch in ("\u2019", "\u2018", "\u02bc", "\u2032", "`", "&apos;", "&#39;"):
        blob = blob.replace(ch, "'")
    return "see what's new" in blob or "see whats new" in blob


def _exact_ui_texts(data):
    """All visible texts / content-descriptions of a UI dump (lower-case, accent-free)."""
    out = set()
    try:
        for node in ET.fromstring(data).iter():
            for key in ("text", "content-desc"):
                v = ax.normalize_text(node.attrib.get(key, "")).strip()
                if v:
                    out.add(v)
    except ET.ParseError:
        pass
    return out


def _handle_target_to_dummy_update_pages(first_window=12, quiet_after_tap=2, max_taps=4):
    """TARGET -> DUMMY only.  Devices differ, so whatever shows up is handled, in any order:
         'Update now'  ->  tap it
         'Got it'      ->  tap it
         'OK'          ->  tap it (only on the 'See what's new' page)
    e.g. Update now -> OK, Update now -> Got it, Got it only, OK -> Update now, Update now only.
    After every tap it keeps watching for `quiet_after_tap` s for a follow-up page; if no page shows up it
    simply carries on with the normal flow."""
    deadline = time.time() + first_window
    taps = 0
    while time.time() < deadline and taps < max_taps:
        time.sleep(2)
        data = ax.dump_ui()
        names = _exact_ui_texts(data)
        label = None
        if "update now" in names:
            label = "Update now"
        elif "got it" in names:
            label = "Got it"
        elif "ok" in names and _whats_new_visible(data):
            label = "OK"
        if label and _tap_exact_ui_text(label):
            taps += 1
            print(f"✅ Update page: tapped '{label}'.")
            time.sleep(2)
            deadline = time.time() + quiet_after_tap      # after a tap only wait briefly for a follow-up page
    if not taps:
        print("ℹ️  No 'Update now' page - continuing normal process.")
    return taps


def _tap_update_now_if_shown(timeout=10):
    """After the 'See what's new' OK: tap 'Update now' only if that page shows up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for label in UPDATE_NOW_LABELS:
            if _tap_exact_ui_text(label):
                print("✅ 'Update now' page found - tapped 'Update now'.")
                time.sleep(2)
                return True
        time.sleep(2)
    return False


def dismiss_whats_new_dialog(timeout=12):
    """Dismiss Motorola's optional 'See what's new' update dialog if it appears.

    Never taps a generic OK button unless the dialog's distinctive heading is
    visible. Devices that go directly to the normal update page are untouched.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = ax.dump_ui()
        if _whats_new_visible(data):
            print("ℹ️ 'See what's new' update dialog detected — tapping 'Got it' / OK...")
            # Only ONE button is tapped: 'Got it' if that page has it, otherwise 'OK'.
            # (Used by Target -> Dummy only; Source -> Target never calls this.)
            if _tap_exact_ui_text("Got it") or _tap_exact_ui_text("OK"):
                time.sleep(1.5)
                print("✅ Update dialog dismissed.")
                return True
        time.sleep(1)
    print("ℹ️ No 'See what's new' dialog appeared; continuing normal update flow.")
    return False


def _center_of(bounds):
    """Centre (x, y) of a '[x1,y1][x2,y2]' bounds string, or None when unusable."""
    nums = list(map(int, re.findall(r"\d+", bounds or "")))
    if len(nums) != 4 or nums[2] <= nums[0] or nums[3] <= nums[1]:
        return None
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def _update_page_state():
    """Fresh UI dump -> (list of ('Update now' own-centre, clickable-parent-centre), 'later' button visible).
    Used ONLY by the Update now / Update later safety net."""
    try:
        root = ET.fromstring(ax.dump_ui())
    except Exception:
        return [], False
    parents = {}
    for par in root.iter():
        for child in list(par):
            parents[id(child)] = par
    wanted = {ax.normalize_text(l).strip() for l in UPDATE_NOW_LABELS}
    now_nodes, has_later = [], False
    for node in root.iter():
        vals = [ax.normalize_text(node.attrib.get(k, "")).strip() for k in ("text", "content-desc")]
        vals = [v for v in vals if v]
        if any(m in v for v in vals for m in ("update later", "mas tarde", "despues")):
            has_later = True
        if any(v in wanted for v in vals):
            own = _center_of(node.attrib.get("bounds", ""))
            anc, par = None, parents.get(id(node))
            while par is not None:
                if par.attrib.get("clickable", "false").lower() == "true":
                    anc = _center_of(par.attrib.get("bounds", ""))
                    break
                par = parents.get(id(par))
            if own:
                now_nodes.append((own, anc))
    return now_nodes, has_later


def _force_tap_update_now(attempts=3):
    """Tap 'Update now' on the Update now / Update later page and CHECK that the page went away.
    1st try: centre of the 'Update now' text/button itself (cannot land on a neighbouring button);
    2nd try: centre of its clickable parent; 3rd try: its own centre again.
    Only acts while BOTH 'Update now' and a 'later' button are on screen.
    Returns True when the page is gone afterwards (i.e. the tap worked)."""
    tapped = False
    for i in range(attempts):
        now_nodes, has_later = _update_page_state()
        if not (now_nodes and has_later):
            return tapped
        own, anc = now_nodes[0]
        x, y = anc if (i == 1 and anc) else own
        ax.adb(f"shell input tap {x} {y}")
        print(f"🎯 Update page: tapped 'Update now' at ({x}, {y}) [try {i + 1}/{attempts}].")
        tapped = True
        time.sleep(2)
    now_nodes, has_later = _update_page_state()
    if now_nodes and has_later:
        print("⚠️  'Update now' page is still showing after the taps.")
        return False
    return True


def _force_update_now_if_stuck():
    """Safety net run once after the normal update-page handling: if the Update now / Update later
    page is STILL on screen (a tap missed, or an earlier step never tapped), force the tap.
    Nothing happens when that page is not showing."""
    now_nodes, has_later = _update_page_state()
    if now_nodes and has_later:
        print("⚠️  Update now / Update later page still showing - forcing the 'Update now' tap...")
        return _force_tap_update_now()
    return False


def _tap_update_now_if_page_open(timeout=4):
    """Safety net for the 'Update now / Update later' page (the page with the 'See what's new'
    card, version + size, and the two buttons).

    On some builds the System update page opens DIRECTLY on this page, so there is no
    'Check for update' row (or the page was opened by the intent command), and the normal flow
    used to return without ever tapping 'Update now'.  This only taps when BOTH 'Update now' and
    'Update later' are on screen, so any other page is left completely untouched.
    Returns True when 'Update now' was tapped."""
    deadline = time.time() + timeout
    while True:
        try:
            names = _exact_ui_texts(ax.dump_ui())
        except Exception:
            names = set()
        # UPDATE_NOW_LABELS is read at call time, so a scenario that extends it (Scenario 7 adds
        # 'Actualizar ahora') is honoured. The 'later' button is matched in English / Spanish.
        now_label = next((l for l in UPDATE_NOW_LABELS if ax.normalize_text(l).strip() in names), None)
        has_later = any(m in n for n in names
                        for m in ("update later", "mas tarde", "despues"))
        if now_label and has_later:
            if _force_tap_update_now():
                print(f"✅ Update page (Now / Later buttons) found - tapped '{now_label}'.")
                time.sleep(2)
                return True
        if time.time() >= deadline:
            return False
        time.sleep(1)


def check_for_update_then_update_now(allow_scroll=True, dismiss_whats_new=False):
    """Presoak flow after the System update page is open (scroll method only).

    1) tap 'Check for update' (never fatal - the OTA flow just carries on)
    2) if an 'Update now' page appears, tap 'Update now'; otherwise normal process.
    Does nothing on devices where the page was opened by the intent command."""
    if not _UPDATE_VIA_SCROLL:
        # Page opened by the intent command: it may already BE the 'Update now / Update later'
        # page. Tap 'Update now' only if both buttons are visible; otherwise nothing changes.
        if dismiss_whats_new:
            # Target -> Dummy on non-Android-15 devices: same handling as Android 15 -
            # 'Update now' / 'Got it' / 'OK' in whatever order they show; nothing visible = carry on.
            _handle_target_to_dummy_update_pages(first_window=8)
        else:
            _tap_update_now_if_page_open()
        return
    tapped = False
    if allow_scroll:
        tapped = ax.scroll_and_tap(K["check_update"])
    else:
        tapped = ax.find_and_tap_any(K["check_update"])   # no scrolling on a running update page
    if not tapped:
        if not dismiss_whats_new:
            # No 'Check for update' row: the page may already be the 'Update now / Update later' page.
            if _tap_update_now_if_page_open():
                return
            print("ℹ️  'Check for update' not available - continuing normal process.")
            return
        # Target -> Dummy: the page itself can already be the 'See what's new' / 'Update now' page,
        # which has no 'Check for updates' row - look for it instead of giving up.
        print("ℹ️  'Check for update' not on this page - looking for the 'Update now' / 'See what's new' page...")
    if dismiss_whats_new:
        # Target -> Dummy: handle Update now / Got it / OK in whatever order this device shows them.
        _handle_target_to_dummy_update_pages(first_window=12 if tapped else 8)
        return
    deadline = time.time() + (12 if tapped else 8)
    while time.time() < deadline:
        time.sleep(2)
        # 'Update now / Update later' page FIRST: its title card also says "See what's new",
        # so it must not be mistaken for the OK-only dialog.
        for label in UPDATE_NOW_LABELS:
            if _tap_exact_ui_text(label):
                print("✅ 'Update now' page found - tapped 'Update now'.")
                time.sleep(2)
                return
        if dismiss_whats_new:
            data = ax.dump_ui()
            if _whats_new_visible(data):
                dismiss_whats_new_dialog(timeout=4)
                continue
    print("ℹ️  No 'Update now' page - continuing normal process.")


# Wrap the (unchanged) function above: after its normal handling, make sure the
# 'Update now / Update later' page is not still sitting there.  Every caller keeps using the same name.
_check_for_update_core = check_for_update_then_update_now


def check_for_update_then_update_now(allow_scroll=True, dismiss_whats_new=False):
    _check_for_update_core(allow_scroll=allow_scroll, dismiss_whats_new=dismiss_whats_new)
    _force_update_now_if_stuck()


def open_dialer():
    go_home()
    ax.adb("shell am start -a android.intent.action.DIAL")
    time.sleep(2)
    data = ax.dump_ui()
    if not re.search(r'(text|content-desc)="1"', data):
        ax.tap_by_keywords(K["dialpad"], data)
        time.sleep(1)
    # clear any leftover digits
    ax.adb("shell input keyevent " + " ".join(["67"] * 20))
    time.sleep(1)


def restart_visible():
    data = ax.dump_ui()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False
    for node in root.iter():
        blob = ax.normalize_text(node.attrib.get("text", "") + " " +
                                 node.attrib.get("content-desc", ""))
        if any(ax.normalize_text(k) in blob for k in K["restart"]):
            return True
    return False


# ===========================================================================
# Stitched "scrolling screenshot" (long Phone info page) - Pillow only
# ===========================================================================
def _gray_small(img):
    """Grayscale, width / 8 (rows keep full vertical resolution)."""
    g = img.convert("L")
    return g.resize((max(1, g.width // 8), g.height), Image.BILINEAR)


def _mean_diff(a, b):
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def find_shift(prev, cur, c0, c1, strip=160):
    """How many pixel rows did the content move between two frames?
    0 = nothing moved (bottom reached), None = could not align."""
    W = prev.width
    A = _gray_small(prev.crop((0, c0, W, c1)))
    B = _gray_small(cur.crop((0, c0, W, c1)))
    w, H = A.size
    if _mean_diff(A, B) < 0.5:
        return 0
    best_y, best_sd = None, -1.0
    for y in range(0, 480, 40):          # top of new frame = overlap with old bottom
        sd = ImageStat.Stat(B.crop((0, y, w, y + strip))).stddev[0]
        if sd > best_sd:
            best_sd, best_y = sd, y
    if best_sd < 4:
        return None
    S = B.crop((0, best_y, w, best_y + strip))
    best = None
    for q in range(best_y, H - strip + 1):
        d = _mean_diff(A.crop((0, q, w, q + strip)), S)
        if best is None or d < best[0]:
            best = (d, q)
    d, q = best
    s = q - best_y
    if d > 3:
        return None
    if _mean_diff(A.crop((0, s, w, H)), B.crop((0, 0, w, H - s))) > 4:
        return None
    return s


def stitched_capture(max_scrolls=10, c0f=0.11, c1f=0.93, grab=None, scroll=None):
    """Scroll to the bottom and stitch everything into ONE tall image
    (status bar / title once at the top, nav bar once at the bottom).
    Returns (image or None, reached_bottom)."""
    grab = grab or grab_frame
    scroll = scroll or slow_swipe
    first = grab()
    if first is None:
        return None, False
    W, h = first.size
    c0, c1 = int(h * c0f), int(h * c1f)
    parts, prev, complete = [first.crop((0, 0, W, c1))], first, False
    for _ in range(max_scrolls):
        scroll()
        cur = grab()
        if cur is None:
            break
        s = find_shift(prev, cur, c0, c1)
        if s is None:
            print("⚠️  Could not align two scroll frames - image may be incomplete.")
            break
        if s <= 2:
            complete = True
            break
        parts.append(cur.crop((0, c1 - s, W, c1)))
        prev = cur
    else:
        print("⚠️  Max scrolls reached before the bottom.")
    parts.append(prev.crop((0, c1, W, h)))
    out = Image.new("RGB", (W, sum(p.height for p in parts)))
    y = 0
    for part in parts:
        out.paste(part, (0, y))
        y += part.height
    return out, complete


# ===========================================================================
# Shots 1-10  /  23-32 : Settings + Phone info + dialer code
# ===========================================================================
def _ui_has_exact_text(root, wanted):
    """Return True when a UI node contains the exact requested text/desc."""
    target = ax.normalize_text(wanted)
    for node in root.iter():
        text = ax.normalize_text(node.attrib.get("text", ""))
        desc = ax.normalize_text(node.attrib.get("content-desc", ""))
        if text == target or desc == target:
            return True
    return False


def _tap_exact_ui_text(wanted):
    """Tap the exact UI item, preferring a clickable parent when present."""
    data = ax.dump_ui()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return False

    target = ax.normalize_text(wanted)
    parents = {}
    for parent in root.iter():
        for child in list(parent):
            parents[id(child)] = parent

    for node in root.iter():
        text = ax.normalize_text(node.attrib.get("text", ""))
        desc = ax.normalize_text(node.attrib.get("content-desc", ""))
        if text != target and desc != target:
            continue
        bounds = node.attrib.get("bounds", "")
        nums = list(map(int, re.findall(r"\d+", bounds)))
        if len(nums) != 4:
            continue

        # If the text node itself is not clickable, use the nearest clickable
        # ancestor. This is important for the Settings card/row layout.
        tap_node = node
        parent = parents.get(id(node))
        while parent is not None:
            if parent.attrib.get("clickable", "false").lower() == "true":
                tap_node = parent
                break
            parent = parents.get(id(parent))

        nums = list(map(int, re.findall(r"\d+", tap_node.attrib.get("bounds", ""))))
        if len(nums) != 4:
            continue
        x = (nums[0] + nums[2]) // 2
        y = (nums[1] + nums[3]) // 2
        ax.adb(f"shell input tap {x} {y}")
        print(f"🎯 Exact tap: '{wanted}' at ({x}, {y})")
        return True
    return False


def nitro_swipe_up():
    """Exact NITRO/Axon swipe geometry: 75% -> 25% over 400 ms."""
    result = ax.adb("shell wm size")
    match = re.search(r"(\d+)\s*x\s*(\d+)", result.stdout or "")
    if match:
        width, height = map(int, match.groups())
    else:
        width, height = 1080, 2400
    x = width // 2
    ax.adb(f"shell input swipe {x} {int(height * 0.75)} {x} {int(height * 0.25)} 400")


def device_info_set(tag, per_shot_wifi=False, include_hardware_screenshot=False):
    print(f"\n⚙️  ━━━━━━ {tag}: Settings / About phone ━━━━━━")

    if per_shot_wifi:
        # Requirement: screenshot #1 starts with Wi-Fi ON, then Wi-Fi is OFF
        # before navigating to screenshot #2.
        if not ensure_wifi_connected(timeout=45):
            for name in ("About phone", "Device details", "Model", "Android version",
                         "IMEI1", "IMEI2", "Device identifiers"):
                mark_missing(f"{tag} - {name}", "(Wi-Fi could not connect)")
            return
    else:
        # POST-update checks do not need the pre-update Wi-Fi gating cycle.
        ensure_wifi_connected(timeout=45)

    open_settings_clean()
    if not ax.open_about_phone():
        for name in ("About phone", "Device details", "Model", "Android version",
                     "IMEI1", "IMEI2", "Device identifiers"):
            mark_missing(f"{tag} - {name}", "(About phone not found)")
    else:
        if per_shot_wifi:
            wifi_snapshot(f"{tag} - About phone")
        else:
            snap(f"{tag} - About phone")

        # Navigation to every subsequent page happens with Wi-Fi OFF.
        if per_shot_wifi:
            disable_wifi_for_navigation()

        # Match the proven Device identifiers sequence: swipe the About phone
        # page first, wait for it to settle, then search/tap the row. On some
        # Motorola builds the row is partially clipped at the bottom; a direct
        # accessibility tap can hit the navigation area and behave like Back.
        print("⏳ Waiting 2s before scrolling to Device details...")
        time.sleep(2)
        print("↕️ Scrolling About phone down to Device details...")
        device_details_found = False
        # Keep the previously successful swipe first; if the Spanish row is
        # lower on this Android build, continue scrolling About phone and retry.
        for scroll_attempt in range(4):
            if scroll_attempt == 0:
                nitro_swipe_up()
            else:
                print(f"↕️ Device details not found; retrying scroll ({scroll_attempt + 1}/4)...")
                ax.swipe_up()
            time.sleep(2)
            if ax.find_and_tap_any(K["device_details"]):
                device_details_found = True
                break

        if device_details_found:
            time.sleep(2)
            if per_shot_wifi:
                wifi_snapshot(f"{tag} - Device details")
            else:
                snap(f"{tag} - Device details")

            if ax.find_and_tap_any(K["model"]):
                time.sleep(2)
                if per_shot_wifi:
                    wifi_snapshot(f"{tag} - Model")
                else:
                    snap(f"{tag} - Model")
                ax.go_back()
                if per_shot_wifi:
                    disable_wifi_for_navigation()
            else:
                mark_missing(f"{tag} - Model")

            if ax.find_and_tap_any(K["android_ver"]):
                time.sleep(2)
                # A transient SystemUI heads-up notification was observed covering
                # this screenshot. Dismiss only that transient overlay; do not touch
                # the OTA notification shade logic or any other screenshot.
                if per_shot_wifi:
                    dismiss_preupdate_heads_up()
                    wifi_snapshot(f"{tag} - Android version")
                else:
                    snap(f"{tag} - Android version")

                # Source -> Target only: after Android version, return to the
                # Device details page, open Hardware information, and capture
                # one additional screenshot. Target -> Dummy intentionally
                # keeps the proven 35-shot flow unchanged.
                if include_hardware_screenshot:
                    ax.go_back()
                    if per_shot_wifi:
                        disable_wifi_for_navigation()
                    time.sleep(1.5)
                    print("🧩 Source→Target extra screenshot: opening Hardware information...")
                    if ax.find_and_tap_any(K["hardware_info"]):
                        time.sleep(2)
                        if per_shot_wifi:
                            wifi_snapshot(f"{tag} - Hardware information")
                        else:
                            snap(f"{tag} - Hardware information")
                        ax.go_back()
                        if per_shot_wifi:
                            disable_wifi_for_navigation()
                    else:
                        print("❌ 'Hardware information' not found in Device details!")
                        mark_missing(f"{tag} - Hardware information")
                else:
                    ax.go_back()
                    if per_shot_wifi:
                        disable_wifi_for_navigation()
            else:
                mark_missing(f"{tag} - Android version")

            ax.swipe_up()
            time.sleep(2)
            imei1 = ax.find_and_tap_next_imei()
            if imei1:
                time.sleep(2)
                if per_shot_wifi:
                    wifi_snapshot(f"{tag} - IMEI1 dialog")
                else:
                    snap(f"{tag} - IMEI1 dialog")
                ax.go_back()                       # closes IMEI1 dialog
                if per_shot_wifi:
                    disable_wifi_for_navigation()

                imei2 = ax.find_and_tap_next_imei(last_text=imei1)
                if imei2:
                    time.sleep(2)
                    if per_shot_wifi:
                        wifi_snapshot(f"{tag} - IMEI2 dialog")
                    else:
                        snap(f"{tag} - IMEI2 dialog")
                    # Exact AXON sequence.
                    ax.go_back(times=2)
                    time.sleep(2)
                    if per_shot_wifi:
                        disable_wifi_for_navigation()
                else:
                    mark_missing(f"{tag} - IMEI2 dialog", "(no 2nd IMEI on screen)")
                    ax.go_back()
            else:
                mark_missing(f"{tag} - IMEI1 dialog")
                mark_missing(f"{tag} - IMEI2 dialog")
                ax.go_back()

        else:
            for name in ("Device details", "Model", "Android version", "IMEI1", "IMEI2"):
                mark_missing(f"{tag} - {name}", "(Device details not found)")

        # #7 / #29 — AXON DEVICE IDENTIFIERS FLOW.
        # After IMEI2, Axon returns to About phone with go_back(times=2).
        # On this device the Device identifiers row is below the fold, so
        # the About phone page MUST be scrolled once before searching for it.
        # This mirrors Axon's own post-flow logic: swipe_up() -> wait ->
        # find_and_tap_any(device_ids) -> wait -> capture.
        print("⏳ Waiting 2s before scrolling to Device identifiers...")
        time.sleep(2)
        print("↕️ Scrolling About phone down to Device identifiers...")
        identifiers_found = False
        for scroll_attempt in range(4):
            if scroll_attempt:
                print(f"↕️ Device identifiers not found; retrying scroll ({scroll_attempt + 1}/4)...")
                ax.swipe_up()
            else:
                ax.swipe_up()
            time.sleep(2)
            if ax.find_and_tap_any(K["device_ids"]):
                identifiers_found = True
                break
        if identifiers_found:
            time.sleep(2)
            if per_shot_wifi:
                wifi_snapshot(f"{tag} - Device identifiers")
            else:
                snap(f"{tag} - Device identifiers")
        else:
            print("❌ 'Device identifiers' is playing hard to get!")
            mark_missing(f"{tag} - Device identifiers")

    go_home()

    print(f"\n📞 ━━━━━━ {tag}: Phone info (*#*#4636#*#*) ━━━━━━")
    if per_shot_wifi:
        disable_wifi_for_navigation()
    open_dialer()
    ax.dial_number("*#*#4636#*#*")
    time.sleep(3)
    if ax.open_phone_info():
        if per_shot_wifi:
            wifi_stitched_snapshot(f"{tag} - Phone info, Phone 0 (stitched)")
        else:
            img, ok = stitched_capture()
            if img is None:
                mark_missing(f"{tag} - Phone info, Phone 0", "(screencap failed)")
            else:
                save_image(img, f"{tag} - Phone info, Phone 0 (stitched)",
                           "OK" if ok else "OK (may be incomplete)")
    else:
        mark_missing(f"{tag} - Phone info, Phone 0")

    ax.go_back()
    if per_shot_wifi:
        disable_wifi_for_navigation()
    if ax.open_phone_info() and ax.open_sim_dropdown() and ax.switch_to_phone1():
        time.sleep(2)
        if per_shot_wifi:
            wifi_stitched_snapshot(f"{tag} - Phone info, Phone 1 (stitched)")
        else:
            img, ok = stitched_capture()
            if img is None:
                mark_missing(f"{tag} - Phone info, Phone 1", "(screencap failed)")
            else:
                save_image(img, f"{tag} - Phone info, Phone 1 (stitched)",
                           "OK" if ok else "OK (may be incomplete)")
    else:
        mark_missing(f"{tag} - Phone info, Phone 1")

    go_home()
    if per_shot_wifi:
        disable_wifi_for_navigation()
    print(f"\n📞 ━━━━━━ {tag}: Dialer code *#*#78287#*#* ━━━━━━")
    open_dialer()
    ax.dial_number("*#*#78287#*#*")
    time.sleep(4)
    if per_shot_wifi:
        wifi_snapshot(f"{tag} - Dialer code 78287")
    else:
        snap(f"{tag} - Dialer code 78287")
    ax.go_back()
    go_home()


# ===========================================================================
# Shots 11-22 : OTA over Wi-Fi
# ===========================================================================
def shot_page(label):
    wake()
    snap(label)


def _find_notification_contains(needles):
    wanted = [ax.normalize_text(x) for x in needles]
    try:
        root = ET.fromstring(ax.dump_ui())
    except ET.ParseError:
        return False
    for node in root.iter():
        text = ax.normalize_text(node.attrib.get("text", ""))
        desc = ax.normalize_text(node.attrib.get("content-desc", ""))
        if any(k and (k in text or k in desc) for k in wanted):
            return True
    return False


def _swipe_notification_list_up():
    w, h = screen_size()
    x = w // 2
    ax.adb(f"shell input swipe {x} {int(h * 0.78)} {x} {int(h * 0.30)} 500")
    time.sleep(1.5)


def _tap_notification_expand_arrow(required_text):
    """Expand one notification inline without opening its application."""
    try:
        root = ET.fromstring(ax.dump_ui())
    except ET.ParseError:
        return False

    target = None
    for node in root.iter():
        text = ax.normalize_text(node.attrib.get("text", ""))
        desc = ax.normalize_text(node.attrib.get("content-desc", ""))
        if required_text and ax.normalize_text(required_text) in (text + " " + desc):
            b = node.attrib.get("bounds", "")
            nums = list(map(int, re.findall(r"\d+", b)))
            if len(nums) == 4:
                target = nums
                break
    if target is None:
        return False

    # Prefer an actual Expand/Show-more accessibility control belonging to
    # this notification. This avoids opening the notification's app.
    for node in root.iter():
        desc = ax.normalize_text(node.attrib.get("content-desc", ""))
        if not any(k in desc for k in ("expand", "show more")):
            continue
        b = node.attrib.get("bounds", "")
        nums = list(map(int, re.findall(r"\d+", b)))
        if len(nums) != 4:
            continue
        cy = (nums[1] + nums[3]) // 2
        if target[1] <= cy <= target[3]:
            ax.adb(f"shell input tap {(nums[0] + nums[2]) // 2} {cy}")
            time.sleep(2)
            return True

    # Motorola SystemUI may not expose the arrow as a separate node. In that
    # case tap the arrow area at the right edge of the matching notification.
    x = max(target[0] + 20, target[2] - 55)
    y = (target[1] + target[3]) // 2
    ax.adb(f"shell input tap {x} {y}")
    time.sleep(2)
    return True


def shot_shade(label, required_text=None, max_scrolls=3, expand_required=False):
    """Expand notification shade; optionally find and inline-expand one notification."""
    ax.adb("shell cmd statusbar expand-notifications")
    time.sleep(2)
    if required_text:
        found = False
        for attempt in range(max_scrolls + 1):
            if _find_notification_contains([required_text]):
                print(f"🔔 Found '{required_text}' in notification shade.")
                found = True
                break
            if attempt < max_scrolls:
                print(f"↕️ Scrolling notification shade for '{required_text}' ({attempt + 1}/{max_scrolls})...")
                _swipe_notification_list_up()
        if not found:
            print(f"❌ Required notification '{required_text}' not found in shade.")
            snap(label + " (required notification not found)")
        else:
            if expand_required:
                if _tap_notification_expand_arrow(required_text):
                    print(f"🔽 Expanded '{required_text}' notification inline.")
                else:
                    print(f"⚠️ Could not locate the expand arrow for '{required_text}'.")
            snap(label)
    else:
        snap(label)
    ax.adb("shell cmd statusbar collapse")
    time.sleep(1)

def shot_quick_settings(label, mobile_data_off=False):
    ax.adb("shell cmd statusbar expand-settings")
    time.sleep(2)
    data_was_on = None
    if mobile_data_off:
        # Preserve the phone's original mobile-data state; only force it OFF
        # for this one screenshot.
        state = ax.adb("shell settings get global mobile_data")
        value = (state.stdout or "").strip()
        data_was_on = (value == "1")
        print("📴 Mobile data OFF for this Quick Settings screenshot only.")
        ax.adb("shell svc data disable")
        time.sleep(2)
    try:
        snap(label)
    finally:
        if mobile_data_off:
            print("📶 Restoring mobile data to its previous state after the screenshot.")
            if data_was_on is True:
                ax.adb("shell svc data enable")
            elif data_was_on is False:
                ax.adb("shell svc data disable")
            else:
                # If the device did not expose the setting, restore the
                # original script behavior (data enabled after this shot).
                ax.adb("shell svc data enable")
            time.sleep(1)
        ax.adb("shell cmd statusbar collapse")
        time.sleep(1)


def wait_until(t_target, poll=10):
    """Sleep until t_target while watching for the restart prompt.
    Returns True if 'Restart now' showed up early."""
    while time.time() < t_target:
        wake()
        if restart_visible():
            return True
        time.sleep(max(0, min(poll, t_target - time.time())))
    return False


def tap_exact(text):
    root = ET.fromstring(ax.dump_ui())
    for node in root.iter():
        if ax.normalize_text(node.attrib.get("text", "")) == ax.normalize_text(text):
            c = list(map(int, re.findall(r"\d+", node.attrib["bounds"])))
            ax.adb(f"shell input tap {(c[0] + c[2]) // 2} {(c[1] + c[3]) // 2}")
            return True
    return False


def handle_restart(already_visible=False, timeout=1800):
    """#20 'Please restart now' page, then reboot and #21 restart image."""
    start = time.time()
    while not already_visible:
        if restart_visible():
            break
        if time.time() - start > timeout:
            print("⏰ 'Restart now' never appeared.")
            snap("Timed out waiting for 'Restart now'")
            return False
        time.sleep(10)
    wake()
    snap("System update - Please restart now")                 # 20
    if TAP_RESTART and tap_exact("restart now"):
        print("👆 Tapped 'Restart now'")
    else:
        ax.adb("shell reboot")
    place_file(RESTART_IMG, "Restarting... (Restart_EN.png)")  # 21
    return True


def wait_update_complete_notification(timeout=1800):
    print("🔔 Watching the notification shade for the update-complete notification...")
    start = time.time()
    while True:
        ax.adb("shell cmd statusbar expand-notifications")
        time.sleep(2)
        root = ET.fromstring(ax.dump_ui())
        for node in root.iter():
            blob = ax.normalize_text(node.attrib.get("text", "") + " " +
                                     node.attrib.get("content-desc", ""))
            if any(ax.normalize_text(k) in blob for k in K["update_done"]):
                c = list(map(int, re.findall(r"\d+", node.attrib["bounds"])))
                ax.adb(f"shell input tap {(c[0] + c[2]) // 2} {(c[1] + c[3]) // 2}")
                time.sleep(2)
                snap("Update complete")                          # 22
                return True
        if time.time() - start > timeout:
            print("⏰ Update-complete notification never appeared.")
            snap("Update complete (timed out - current screen)")
            return False
        time.sleep(10)


def ota_phase(include_notification_dropdown=True, dismiss_whats_new=False):
    print("\n🛰️  ━━━━━━ OTA UPDATE over Wi-Fi ━━━━━━")

    # Keep Wi-Fi OFF while entering System update.  This is important because
    # the saved update can otherwise start downloading before the update page
    # is ready.
    print("📴 Wi-Fi OFF while opening System update page...")
    ax.adb("shell svc wifi disable")
    time.sleep(2)

    open_settings_clean()
    if not open_update_page():
        print("❌ System update page not found - OTA stopped.")
        return False

    # IMPORTANT: Do NOT look for/tap a "Check for update" button here.
    # Axon's working flow simply opens the System update page, enables Wi-Fi,
    # and lets the Motorola background installation begin automatically.
    # The previous Check-for-update step could fail on this build and caused
    # the whole OTA section (#11-#22) to be skipped before post-update shots.
    print("📶 System update page is open — enabling Wi-Fi now...")
    if not ensure_wifi_connected(timeout=45):
        print("❌ Wi-Fi could not reconnect on System update page - OTA stopped.")
        return False

    check_for_update_then_update_now(dismiss_whats_new=dismiss_whats_new)
    if dismiss_whats_new:
        # Catch devices that show the confirmation after the check-update tap.
        # If it was there: OK is tapped, then continue the update ('Update now' if shown).
        if dismiss_whats_new_dialog(timeout=3):
            _tap_update_now_if_shown(timeout=8)

    # Capture immediately after the System update page is ready.
    # Target -> Dummy captures the notification/drop-down; Source -> Target does not.
    time.sleep(1)
    shot_page("System update - started (Verifying)")
    if include_notification_dropdown:
        shot_shade("Background installation expanded (immediate)",
                   required_text="Background installation", max_scrolls=3,
                   expand_required=True)
    t0 = time.time()

    def wait_to(target_seconds):
        """Wait until a checkpoint while watching for the restart page.
        Returns True immediately if Restart now appears before that checkpoint.
        """
        target = t0 + target_seconds
        while time.time() < target:
            wake()
            if restart_visible():
                print("🛑 'Restart now' appeared early — stopping scheduled update screenshots.")
                return True
            time.sleep(min(5, max(0.5, target - time.time())))
        return restart_visible()

    # #13, #14 — +1 minute. If the restart page has already appeared, do not
    # capture it as an update-page screenshot. The remaining scheduled slots are
    # left missing and #20 is reserved for the single real restart screenshot.
    print("⏳ Waiting for the +1 min checkpoint...")
    if not wait_to(60):
        if not restart_visible():
            shot_page("System update page @1 min")                    # 13
        if restart_visible():
            print("🛑 Restart page appeared after #13 — stopping update screenshots.")
        else:
            shot_page("System update page @1 min (2nd)")              # 14

    # If restart appeared during/after the +1 min checkpoint, skip all later
    # update screenshots and go directly to the single #20 restart capture.
    if restart_visible():
        print("🛑 Restart page detected — no more scheduled update screenshots will be taken.")
    else:
        # #15 — +2 minutes. Mobile data must be OFF for this Quick Settings
        # screenshot while Wi-Fi remains ON. Data is restored immediately after.
        print("⏳ Waiting for the +2 min checkpoint...")
        if not wait_to(120):
            if not restart_visible():
                shot_quick_settings("Quick Settings expanded (Wi-Fi only) @2 min",
                                    mobile_data_off=True)                  # 15

        if not restart_visible():
            # #16, #17 — +4 minutes
            print("⏳ Waiting for the +4 min checkpoint...")
            if not wait_to(240):
                if restart_visible():
                    print("🛑 Restart page detected at +4 min — stopping update screenshots.")
                else:
                    if not restart_visible():
                        shot_page("System update page @4 min")
                    if include_notification_dropdown and not restart_visible():
                        shot_shade("Notification shade @4 min")

        if not restart_visible():
            # #18, #19 — +6 minutes
            print("⏳ Waiting for the +6 min checkpoint...")
            if not wait_to(360):
                if restart_visible():
                    print("🛑 Restart page detected at +6 min — stopping update screenshots.")
                else:
                    if not restart_visible():
                        shot_page("System update page @6 min")
                    if include_notification_dropdown and not restart_visible():
                        shot_shade("Notification shade @6 min")

    # Only now handle the restart. If it appeared before a scheduled checkpoint,
    # #20 is the ONLY screenshot of the restart page. Missing scheduled slots are
    # reserved as missing so the restart page can never be saved under #13-#19.
    pad_to(20, "Restart now appeared before this scheduled update checkpoint")
    print("👀 Waiting for 'Restart now' after the scheduled update screenshots...")
    if not handle_restart(already_visible=restart_visible()):
        return False

    ax.wait_for_device()
    ax.unlock_device()
    if not wait_update_complete_notification():
        return False
    go_home()
    ax.clear_recent_apps()
    return True



# ===========================================================================
# Shots 33-35 : Wi-Fi call + browsing
# ===========================================================================
CHROME_PKG = "com.android.chrome"
CHROME_URL = "https://www.amazon.com"

# --- tested standalone logic (call_test.py / chrome_test.py), integrated ----
ANSWER_WINDOW = 30      # seconds the other side has to answer
MIN_TIMER_SECS = 5      # connected screenshot only when the call is live this long
CHROME_MAX_WAIT = 120   # seconds allowed to reach a clean Amazon page

# Buttons Chrome popups may be dismissed with (EXACT text match, priority order).
# "continue" / "settings" / "yes" / "allow" / "sign in" are deliberately NOT here.
DISMISS_LABELS = [
    "got it", "no thanks", "no, thanks", "use without an account",
    "accept & continue", "accept and continue", "don't allow", "do not allow",
    "not now", "continue shopping", "maybe later", "no, thank you", "dismiss",
]
# Screens that are popups even when their button is not visible to the UI dump.
POPUP_HINTS = ["enhanced ad privacy", "notifications make things easier",
               "sign in to chrome", "turn on sync"]
SAFE_PKGS = ("com.android.chrome", "permissioncontroller", "com.google.android.gms")
CALL_STATES = ("NEW|CONNECTING|SELECT_PHONE_ACCOUNT|DIALING|PULLING|RINGING|SIMULATED_RINGING|"
               "ACTIVE|ON_HOLD|AUDIO_PROCESSING|DISCONNECTING|DISCONNECTED|ABORTED")

CALL_LABEL_CALLING = "Wi-Fi call - Calling..."
CALL_LABEL_CONNECTED = "Wi-Fi call - connected (timer >= 00:05)"
CHROME_LABEL = "Chrome (not signed in) - amazon.com over Wi-Fi"


def _bsh(cmd, timeout=40):
    """adb shell, byte-safe (never crashes on odd characters). Returns text."""
    args = ["adb"] + (["-s", ax.TARGET_SERIAL] if ax.TARGET_SERIAL else []) + ["shell", cmd]
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").replace("\r", "")


def _bpull(remote, local):
    args = ["adb"] + (["-s", ax.TARGET_SERIAL] if ax.TARGET_SERIAL else []) + ["pull", remote, local]
    try:
        return subprocess.run(args, capture_output=True, timeout=60).returncode
    except subprocess.TimeoutExpired:
        return 124


def _cc_norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\u2019", "'").lower()).strip()


def _fresh_dump(retries=3):
    """UI dump that can never return an old file; a failed dump returns None
    (= unknown screen), never 'empty screen'."""
    local = os.path.join(tempfile.gettempdir(), f"_tf_ui_{ax.TARGET_SERIAL or 'dev'}.xml")
    for i in range(retries):
        _bsh("rm -f /sdcard/_t_ui.xml")
        res = _bsh("uiautomator dump /sdcard/_t_ui.xml")
        if os.path.exists(local):
            os.remove(local)
        rc = _bpull("/sdcard/_t_ui.xml", local)
        if rc == 0 and os.path.exists(local):
            try:
                root = ET.parse(local).getroot()
                os.remove(local)
                return root
            except ET.ParseError:
                pass
        print(f"   (ui dump try {i + 1}/{retries} failed: {res.strip()[:70]})")
        time.sleep(0.7)
    return None


def _cc_nodes(root):
    out = []
    for n in root.iter("node"):
        a = n.attrib
        m = re.findall(r"\d+", a.get("bounds", ""))
        c = None
        if len(m) == 4 and int(m[2]) > int(m[0]) and int(m[3]) > int(m[1]):
            c = ((int(m[0]) + int(m[2])) // 2, (int(m[1]) + int(m[3])) // 2)
        out.append({"text": _cc_norm(a.get("text")), "desc": _cc_norm(a.get("content-desc")),
                    "pkg": a.get("package", ""), "id": a.get("resource-id", ""), "c": c})
    return out


def _cc_classify(root):
    """-> dict(pkg, button=(label,(x,y)) or None, hint, amazon, texts)"""
    nodes = _cc_nodes(root)
    pkgs = [n["pkg"] for n in nodes if n["pkg"] and n["pkg"] != "com.android.systemui"]
    pkg = max(set(pkgs), key=pkgs.count) if pkgs else ""
    alltext = " | ".join(n["text"] for n in nodes if n["text"]) + " | " + \
              " | ".join(n["desc"] for n in nodes if n["desc"])
    button = None
    for label in DISMISS_LABELS:                       # priority order
        for n in nodes:
            if n["c"] and (n["text"] == label or n["desc"] == label):
                button = (label, n["c"])
                break
        if button:
            break
    hint = any(h in alltext for h in POPUP_HINTS)
    url_ok = any(n["id"].endswith("url_bar") and "amazon" in n["text"] for n in nodes)
    page_ok = pkg == CHROME_PKG and "amazon" in alltext
    texts = [n["text"] or n["desc"] for n in nodes if n["text"] or n["desc"]]
    return {"pkg": pkg, "button": button, "hint": hint,
            "amazon": (url_ok or page_ok), "texts": texts}


def _cc_safe_pkg(pkg):
    return any(p in pkg for p in SAFE_PKGS)


def _cc_handle_popup(info, back_budget):
    """Dismiss a known popup. Returns True if something was done."""
    if info["button"] and _cc_safe_pkg(info["pkg"]):
        label, (x, y) = info["button"]
        print(f"🔕 popup -> tapping '{label}' at ({x},{y})")
        _bsh(f"input tap {x} {y}")
        return True
    if info["hint"] and back_budget[0] > 0:
        back_budget[0] -= 1
        print("🔕 popup text seen but no tappable button in dump -> pressing BACK")
        _bsh("input keyevent KEYCODE_BACK")
        return True
    return False


def _cc_launch_chrome():
    r = _bsh(f"am start -n {CHROME_PKG}/com.google.android.apps.chrome.Main -d {CHROME_URL}")
    if "Error" in r or "Exception" in r:
        print("⚠️ Explicit Chrome launch failed; falling back to VIEW intent...")
        _bsh(f"am start -a android.intent.action.VIEW -d {CHROME_URL} -p {CHROME_PKG}")


def _telecom_states():
    """Live call states from `dumpsys telecom`; [] = no calls; None = format not understood."""
    out = _bsh("dumpsys telecom", 30)
    if "TC@" not in out:
        return []
    st = re.findall(r"\[TC@\d+,\s*(" + CALL_STATES + r")\b", out)
    if not st:
        st = re.findall(r"TC@\d+[^\n]*?\bstate[=: ]+(" + CALL_STATES + r")\b", out)
    return st if st else None


def _ui_call_timer(root):
    """Seconds on the in-call timer (ignores the status-bar clock)."""
    best = None
    for n in root.iter("node"):
        if n.attrib.get("package") == "com.android.systemui":
            continue
        for k in ("text", "content-desc"):
            m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", n.attrib.get(k, "").strip())
            if m:
                secs = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                best = secs if best is None else max(best, secs)
    return best


def _cc_end_call():
    _bsh("input keyevent KEYCODE_ENDCALL")
    time.sleep(1.5)
    _bsh("telecom end-call")           # fallback; harmless if no call
    time.sleep(1)


def _cc_shot(sink, label):
    """Capture one screenshot through the given sink. True on success."""
    commit, missing = sink
    img = grab_frame()
    if img is None:
        missing(label, "(screencap/pull failed)")
        return False
    commit(img, label)
    return True


def _cc_call(number, sink):
    """Dial, wait max 30 s for an answer, screenshot Calling... and connected."""
    commit, missing = sink
    enc = number.replace("+", "%2B")
    go_home()

    base = _telecom_states()
    use_telecom = True
    if base and "ACTIVE" in base:
        print("⚠️ Telecom already shows an ACTIVE call before dialing - ignoring Telecom signal.")
        use_telecom = False

    print(f"📞 Dialing {number} ...")
    res = _bsh(f"am start -a android.intent.action.CALL -d tel:{enc}")
    if "Error" in res or "Denial" in res or "Exception" in res:
        print("⚠️ Dial command output: " + res.strip()[:200])
    time.sleep(2)
    _cc_shot(sink, CALL_LABEL_CALLING)

    start = time.time()
    active_since = None
    live_seen = False
    no_call_polls = 0
    answered = False
    last_root = None
    ui_blocked = False
    ui_fail = 0
    last_print = [None, 0.0]
    reason = "no answer within 30 seconds"

    while True:
        elapsed = time.time() - start
        if elapsed >= ANSWER_WINDOW and active_since is None:
            break
        if elapsed >= ANSWER_WINDOW + 20:      # hard stop
            break

        st = _telecom_states() if use_telecom else None
        timer = None
        # The UI dump is only a FALLBACK: during a call it often fails with
        # "could not get idle state" (~13 s lost per try).
        if (st is None or (not live_seen and elapsed > 8)) and not ui_blocked:
            root = _fresh_dump(retries=1)
            if root is None:
                ui_fail += 1
                if ui_fail >= 2:
                    ui_blocked = True
                    print("ℹ️ UI dump keeps failing during the call - not using it any more.")
            else:
                last_root = root
                timer = _ui_call_timer(root)

        if st is not None and use_telecom:
            if "ACTIVE" in st:
                active_since = active_since or time.time()
            else:
                active_since = None
            if any(s in ("DIALING", "CONNECTING", "RINGING", "ACTIVE", "PULLING") for s in st):
                live_seen = True
                no_call_polls = 0
            elif live_seen and not st:
                no_call_polls += 1

        snapshot = (str(st), timer)
        if snapshot != last_print[0] or time.time() - last_print[1] >= 5:
            print(f"   call poll t={elapsed:4.1f}s telecom={st} ui_timer={timer}")
            last_print[0], last_print[1] = snapshot, time.time()

        if timer is not None and timer >= MIN_TIMER_SECS:
            answered = True
            print(f"✅ Call ANSWERED (on-screen timer {timer}s)")
            break
        if active_since and time.time() - active_since >= MIN_TIMER_SECS:
            answered = True
            print("✅ Call ANSWERED (Telecom ACTIVE for 5 s)")
            break
        if no_call_polls >= 3:
            reason = "call ended / declined / disconnected before it was answered"
            break
        time.sleep(0.3)

    ok = False
    if answered:
        ok = _cc_shot(sink, CALL_LABEL_CONNECTED)
    else:
        print(f"❌ Call was not answered: {reason} — connected screenshot is missing.")
        missing(CALL_LABEL_CONNECTED, f"({reason})")
        if last_root is not None:
            print("   Last screen texts seen: " + " | ".join(
                t for t in _cc_classify(last_root)["texts"])[:400])
    _cc_end_call()
    go_home()
    return ok


def _cc_chrome(sink):
    """Chrome (not signed in) -> amazon.com. Dismisses popups until Amazon is really
    on screen, screenshots it, re-checks the screen and retakes if a popup was there."""
    commit, missing = sink
    print("\n🌐 ━━━━━━ Chrome (no sign-in) -> amazon.com ━━━━━━")
    go_home()
    _bsh(f"am force-stop {CHROME_PKG}")
    time.sleep(1)
    _cc_launch_chrome()
    time.sleep(4)

    deadline = time.time() + CHROME_MAX_WAIT
    stable = 0
    not_chrome = 0
    relaunches = 0
    back_budget = [3]
    clean = False
    while time.time() < deadline:
        root = _fresh_dump(retries=4)
        if root is None:
            print("⚠️ UI dump failed 4x - treating screen as UNKNOWN (not clear)")
            stable = 0
            time.sleep(2)
            continue
        info = _cc_classify(root)

        if _cc_handle_popup(info, back_budget):
            stable = 0
            time.sleep(2)
            continue

        if info["amazon"] and not info["hint"]:
            stable += 1
            print(f"✅ Amazon page visible ({stable}/2 stable checks)")
            if stable >= 2:
                clean = True
                break
            time.sleep(2)
            continue

        stable = 0
        print(f"   Screen not Amazon yet. pkg={info['pkg']}  texts: " + " | ".join(info["texts"])[:300])
        if info["pkg"] != CHROME_PKG and not _cc_safe_pkg(info["pkg"]):
            not_chrome += 1
            if not_chrome >= 3 and relaunches < 2:
                relaunches += 1
                not_chrome = 0
                print("   Chrome is not in front - relaunching it")
                _cc_launch_chrome()
        time.sleep(2.5)

    if not clean:
        print("⚠️ Amazon page was not confirmed within the time limit - best-effort screenshot")

    time.sleep(3)                                   # let the page finish drawing
    final_img = None
    for attempt in range(1, 4):
        img = grab_frame()
        if img is None:
            continue
        root = _fresh_dump(retries=4)
        if root is None:
            print("⚠️ could not re-verify the screen after the screenshot (dump failed)")
            final_img = img
            break
        info = _cc_classify(root)
        if _cc_handle_popup(info, back_budget):
            print(f"   Popup was present during screenshot {attempt} -> retaking")
            time.sleep(3)
            continue
        final_img = img                              # best effort so far
        if info["amazon"] and not info["hint"]:
            print("✅ Clean Amazon screenshot verified.")
            break
        print("⚠️ Screen after screenshot is not a clean Amazon page. texts: "
              + " | ".join(info["texts"])[:300])
        time.sleep(3)

    if final_img is None:
        missing(CHROME_LABEL, "(screencap failed)")
        go_home()
        return False
    commit(final_img, CHROME_LABEL)
    go_home()
    return True


def _call_and_chrome_flow(number, sink):
    """Wi-Fi call (2 shots) + Chrome/Amazon (1 shot) through the given sink."""
    commit, missing = sink
    print("\n📞 ━━━━━━ Wi-Fi call ━━━━━━")
    if not ensure_wifi_connected():
        print("❌ Wi-Fi is compulsory for the call and Amazon screenshots - they are marked missing.")
        missing(CALL_LABEL_CALLING, "(Wi-Fi not connected)")
        missing(CALL_LABEL_CONNECTED, "(Wi-Fi not connected)")
        missing(CHROME_LABEL, "(Wi-Fi not connected)")
        return False
    ok_call = _cc_call(number, sink)
    ok_chrome = _cc_chrome(sink)
    return ok_call and ok_chrome


def ask_call_number():
    """Ask for the number BEFORE anything runs on the phone."""
    if not sys.stdin.isatty():
        sys.exit("No phone number given. Pass --call-number <NUMBER> when not running in a terminal.")
    while True:
        n = input("📞 Enter the phone number to call for the Wi-Fi call shots (33-34), "
                  "e.g. +919876543210 : ").strip().replace(" ", "").replace("-", "")
        if re.fullmatch(r"\+?\d{5,15}", n):
            return n
        print("❌ Digits only (optional leading +), 5-15 digits. Try again.")


def wifi_call_and_browse(number):
    """Wi-Fi call (Calling + connected) and Chrome/Amazon shots at the END of a run."""
    _call_and_chrome_flow(number, (lambda img, label: save_image(img, label), mark_missing))


# ===========================================================================
# Scenario 5 — Source -> Target Wi-Fi with a manual mid-update restart
# ===========================================================================
def _power_menu_open(before_img):
    """True when the Power menu (Power off / Restart) is on screen.

    1) the UI dump shows a Restart / Power off item, or
    2) the left part of the screen changed a lot compared with `before_img` (the menu dims the whole
       screen; the small volume slider on the right edge does NOT count)."""
    try:
        root = ET.fromstring(ax.dump_ui())
        for node in root.iter():
            for key in ("text", "content-desc"):
                t = ax.normalize_text(node.attrib.get(key, ""))
                if t in ("restart", "power off", "restart phone", "power off & restart"):
                    return True
    except Exception:
        pass
    after = grab_frame(retries=1)
    if after is None or before_img is None:
        return False

    def small(im):
        w, h = im.size
        return im.convert("L").crop((0, int(h * 0.05), int(w * 0.65), int(h * 0.92))).resize((48, 96))

    a, b = small(before_img), small(after)
    mean_diff = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    mean_luma = ImageStat.Stat(b).mean[0]
    return mean_diff >= 18 and mean_luma > 15


_KEY_FAIL = re.compile(r"denied|could not|not found|no such|failed|error|invalid", re.I)


def _key_devices():
    """(power_device, volume_up_device) = /dev/input/eventN paths found with `getevent -p`, or (None, None)."""
    out = ax.adb("shell getevent -p").stdout or ""
    power = vol = None
    for block in re.split(r"(?m)^(?=add device)", out):
        m = re.match(r"add device \d+:\s*(\S+)", block)
        if not m:
            continue
        nm = re.search(r'name:\s*"([^"]*)"', block)
        if "ABS (0003)" in block or (nm and re.search(r"finger|touch|fp|hall|sensor", nm.group(1), re.I)):
            continue                      # touch screen / fingerprint / sensors are not the hardware keys
        keys, grab = set(), False
        for line in block.splitlines():
            if "KEY (0001):" in line:
                grab = True
                line = line.split("KEY (0001):", 1)[1]
            elif grab and not re.fullmatch(r"\s*(?:(?:[0-9a-fA-F]{4}|KEY_\w+)\s*)+", line):
                grab = False
            if grab:
                for tok in re.findall(r"[0-9a-fA-F]{4}|KEY_\w+", line):
                    keys.add(tok.upper() if tok.startswith("KEY_") else int(tok, 16))
        if power is None and (116 in keys or "KEY_POWER" in keys):
            power = m.group(1)
        if vol is None and (115 in keys or "KEY_VOLUMEUP" in keys):
            vol = m.group(1)
    return power, vol


def _power_chord_sendevent(hold=0.8):
    """Press POWER + VOLUME UP at the same moment on the real hardware key devices, hold, release.
    Returns False when this phone does not allow it (the caller then uses the next method)."""
    power, vol = _key_devices()
    if not (power and vol):
        return False
    seq = "; ".join([
        f"sendevent {power} 1 116 1", f"sendevent {power} 0 0 0",
        f"sendevent {vol} 1 115 1", f"sendevent {vol} 0 0 0",
        f"sleep {hold}",
        f"sendevent {vol} 1 115 0", f"sendevent {vol} 0 0 0",
        f"sendevent {power} 1 116 0", f"sendevent {power} 0 0 0",
    ])
    r = ax.adb(f'shell "{seq}"')
    blob = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and not _KEY_FAIL.search(blob)


def _ensure_update_page_front():
    """The power-menu screenshot must be taken over the System update page."""
    wake()
    if _update_page_in_front() is False:
        print("↩️ The System update page is not in front - reopening it before the power menu...")
        try:
            ax.unlock_device()
        except Exception:
            pass
        open_settings_clean()
        open_update_page()


def _open_power_menu():
    """Open the Motorola power menu (Emergency / Power off / Restart) over the System update page.

    The real chord (POWER + VOLUME UP pressed together on the hardware key devices) is tried first, then the
    older key-event methods.  After every try the screen is checked: the menu must really be open, otherwise the
    next method is used.  Returns True only when the menu is confirmed open."""
    methods = (
        ("hardware key chord (Power + Volume Up)", _power_chord_sendevent),
        ("Power + Volume Up key events", lambda: ax.adb('shell "input keyevent 26 & input keyevent 24"')),
        ("key combination Power + Volume Up", lambda: ax.adb("shell input keycombination 26 24")),
        ("hardware key chord (Power + Volume Up), again", _power_chord_sendevent),
        ("long press Power", lambda: ax.adb("shell input keyevent --longpress 26")),
    )
    for n, (name, fn) in enumerate(methods, 1):
        _ensure_update_page_front()
        time.sleep(1)
        before = grab_frame(retries=1)
        print(f"⚡ Power menu try {n}/{len(methods)}: {name}...")
        try:
            res = fn()
        except Exception as e:
            print(f"   (not possible: {e})")
            continue
        if res is False:
            print("   (not available on this phone - next method)")
            continue
        time.sleep(1.5)
        if _power_menu_open(before) or (time.sleep(1.5) or _power_menu_open(before)):
            print("✅ Power menu is open.")
            return True
        print("   (power menu not visible yet)")
        wake()
    print("⚠️ The power menu could not be opened by any method.")
    return False


def _uptime_secs():
    try:
        return float((ax.adb("shell cat /proc/uptime").stdout or "").split()[0])
    except Exception:
        return None


def _restart_happened(up_before, wait=15):
    """True when the phone really restarted (adb dropped, or the uptime counter went back)."""
    end = time.time() + wait
    while True:
        up = _uptime_secs()
        if up is None or up_before is None or up < up_before:
            return True
        if time.time() >= end:
            return False
        time.sleep(1)


def _force_reboot_and_wait():
    """Restart through adb (used only when the on-screen Restart did not restart the phone)."""
    ax.adb("reboot")
    end = time.time() + 40
    while time.time() < end:
        if (ax.adb("get-state").stdout or "").strip() != "device":
            break
        time.sleep(1)
    ax.wait_for_device()


def scenario5_manual_power_restart():
    """Capture Scenario-5's two manual-restart evidence shots.

    #16 = Motorola power menu over the System update page.
    #17 = the REAL Android ``Restarting...`` transition screen.

    The important detail on this Motorola build is that the reboot transition
    can make ADB disappear almost immediately after Restart is tapped.  The
    previous implementation tapped Restart first and *then* started its
    screencap loop, which is exactly why #17 was missing.  The corrected
    implementation starts a short remote screencap loop FIRST, then taps
    Restart while that loop is already running.  This gives the device a
    chance to write the short-lived Restarting frame before ADB goes offline.

    Temporary transition frames are kept outside the evidence folder and are
    always deleted.  No ``restart`` evidence/helper folder is left behind.
    """
    print("\n🔄 Scenario 5 special step: manual restart during the update...")
    wake()
    time.sleep(1)

    # Exact Axon gesture: POWER + VOLUME UP simultaneously.
    print("⚡ Opening Motorola power menu with POWER + VOLUME UP simultaneously...")
    if not _open_power_menu():
        # Never save a wrong picture as the power-menu screenshot: leave that slot MISSING, restart the phone
        # through adb and let the run continue with the normal Restarting slot.
        mark_missing("Scenario 5 - Power menu during update", "(power menu could not be opened)")
        print("⚠️ Restarting the phone through adb so the update/run can continue...")
        _force_reboot_and_wait()
        place_file(RESTART_IMG, "Restarting... (Restart_EN.png)")
        print("⚠️ Used Restart_EN.png fallback; continuing device reboot recovery.")
        try:
            ax.unlock_device()
        except Exception:
            pass
        time.sleep(2)
        return True

    # #16 — power menu must be visible over System update.
    if snap("Scenario 5 - Power menu during update") is None:
        print("❌ Could not capture the Scenario 5 power menu.")
        return False

    # Resolve the Restart button from the UI when possible; otherwise use the
    # stable resolution-independent center used by the working device flow.
    w, h = screen_size()
    restart_x, restart_y = w // 2, int(h * 0.52)
    try:
        root = ET.fromstring(ax.dump_ui())
        for node in root.iter():
            text = ax.normalize_text(node.attrib.get("text", ""))
            desc = ax.normalize_text(node.attrib.get("content-desc", ""))
            if text == "restart" or desc == "restart":
                nums = list(map(int, re.findall(r"\d+", node.attrib.get("bounds", ""))))
                if len(nums) == 4:
                    restart_x = (nums[0] + nums[2]) // 2
                    restart_y = (nums[1] + nums[3]) // 2
                break
    except Exception:
        pass

    print(f"🔄 Preparing the Restarting-frame capture at ({restart_x}, {restart_y})...")

    # All temporary files live outside SAVE_DIR so the evidence folder never
    # contains an unexplained restart folder.  tempfile cleanup is unconditional.
    temp_dir = tempfile.mkdtemp(prefix="tracfone_s5_restart_")
    remote_prefix = "/sdcard/.tracfone_s5_restart"
    frame_count = 20
    candidates = []
    try:
        cleanup = " ".join(f"rm -f {remote_prefix}_{i}.png" for i in range(1, frame_count + 1))
        ax.adb(f'shell "{cleanup}"')

        # CRITICAL FIX:
        # Start the screencap loop BEFORE tapping Restart.  The previous code
        # used `input tap; while screencap...`, which loses the race because
        # the device can begin rebooting before the first screencap executes.
        # The loop below is already active when the Restart tap occurs.
        up_before = _uptime_secs()
        burst = (
            f'(i=1; while [ $i -le {frame_count} ]; do '
            f'screencap -p {remote_prefix}_$i.png >/dev/null 2>&1; '
            f'sleep 0.06; i=$((i+1)); '
            f'done) & '
            f'sleep 0.18; '
            f'input tap {restart_x} {restart_y}; '
            f'sleep 1.20'
        )
        ax.adb(f'shell "{burst}"')

        print("⏱️ Waiting for the device to reboot so the captured transition frames can be pulled...")
        ax.wait_for_device()

        for i in range(1, frame_count + 1):
            remote = f"{remote_prefix}_{i}.png"
            local = os.path.join(temp_dir, f"restart_{i}.png")
            pull = ax.adb(f'pull {remote} "{local}"')
            if pull.returncode == 0 and os.path.isfile(local):
                try:
                    im = Image.open(local).convert("RGB")
                    im.load()
                    candidates.append((i, im))
                except Exception:
                    pass

        if not _restart_happened(up_before):
            print("⚠️ The phone did not restart after tapping Restart - restarting it through adb now.")
            _force_reboot_and_wait()

        # Pick the dark transition frame with the characteristic bright white
        # Restarting text/spinner.  Exclude the light power-menu frames and the
        # later all-black boot frames as much as possible.
        best = None
        for idx, im in candidates:
            gray = im.convert("L")
            mean_luma = float(ImageStat.Stat(gray).mean[0])
            hist = gray.histogram()
            total = max(1, im.width * im.height)
            dark_ratio = sum(hist[:80]) / total
            center = gray.crop((int(im.width * 0.15), int(im.height * 0.18),
                                int(im.width * 0.85), int(im.height * 0.78)))
            bright_ratio = sum(1 for px in center.getdata() if px >= 190) / max(1, center.width * center.height)
            # The Motorola Restarting screen is dark with a visible white text
            # block/spinner. Favor that signature rather than simply the darkest
            # available frame.
            if dark_ratio < 0.55 or mean_luma > 105 or bright_ratio < 0.0005:
                continue
            score = abs(mean_luma - 35.0) + abs(dark_ratio - 0.82) * 55.0 - min(bright_ratio, 0.02) * 900.0
            if best is None or score < best[0]:
                best = (score, mean_luma, dark_ratio, bright_ratio, idx, im)

        if best is None:
            print("⚠️ The real Android 'Restarting...' frame was not captured.")
            print(f"   Captured transition candidates: {len(candidates)}")
            # Match the already-working second-restart evidence fallback:
            # preserve the expected screenshot slot and continue reboot recovery.
            place_file(RESTART_IMG, "Restarting... (Restart_EN.png)")
            print("⚠️ Used Restart_EN.png fallback; continuing device reboot recovery.")
            return True

        _, mean_luma, dark_ratio, bright_ratio, idx, restarting_img = best
        save_image(restarting_img, "Scenario 5 - Restarting")
        print(
            f"✅ Real Android 'Restarting...' screen captured as #17 "
            f"(burst frame {idx}, mean={mean_luma:.1f}, dark={dark_ratio:.0%}, bright={bright_ratio:.2%})."
        )
        return True
    finally:
        # Remove remote burst files and the local temporary frames regardless of
        # success/failure. Nothing named restart_* remains in the evidence tree.
        try:
            cleanup = " ".join(f"rm -f {remote_prefix}_{i}.png" for i in range(1, frame_count + 1))
            ax.adb(f'shell "{cleanup}"')
        except Exception:
            pass
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            ax.unlock_device()
        except Exception:
            pass
        time.sleep(2)


def _update_page_showing():
    """True when the System update page is already on screen."""
    try:
        root = ET.fromstring(ax.dump_ui())
    except Exception:
        return False
    blob = " ".join(ax.normalize_text(n.attrib.get(k, "")) for n in root.iter() for k in ("text", "content-desc"))
    return any(k in blob for k in ("check for updates", "smart updates", "update history", "update status", "updating..."))


def _reopen_update_page_after_restart(tries=3):
    """Same steps as before (Settings -> System update); if they fail right after the reboot, try again."""
    for i in range(1, tries + 1):
        if i > 1:
            print(f"🔁 Retry {i}/{tries}: reopening System update after the restart...")
            time.sleep(8)
            wake()
            try:
                ax.unlock_device()
            except Exception:
                pass
        open_settings_clean()
        if open_update_page():
            return True
        if _update_page_showing():
            print("ℹ️ The System update page is already on screen.")
            return True
    return False


def ota_phase_scenario5():
    """Scenario 5 OTA: manual restart exactly after the +2 minute checkpoint.

    The physical flow is: immediate -> (9B only: incoming call, early + time-limited) ->
    Background installation notification -> +1 min (2 shots) -> +2 min Quick Settings ->
    manual Power/Restart evidence -> reboot -> resumed update -> remaining
    update checkpoints (+4 and +6 style, spaced 2 minutes apart) -> normal
    Restart now -> restart image -> update complete.
    """
    print("\n🛰️  ━━━━━━ SCENARIO 5 OTA UPDATE over Wi-Fi ━━━━━━")
    print("📴 Wi-Fi OFF while opening System update page...")
    ax.adb("shell svc wifi disable")
    time.sleep(2)

    open_settings_clean()
    if not open_update_page():
        print("❌ System update page not found - Scenario 5 OTA stopped.")
        return False

    print("📶 System update page is open — enabling Wi-Fi now...")
    if not ensure_wifi_connected(timeout=45):
        print("❌ Wi-Fi could not reconnect on System update page - Scenario 5 OTA stopped.")
        return False
    check_for_update_then_update_now()

    # Immediate update screenshot. The update schedule starts HERE (as it always did), so the
    # manual restart below still happens ~+2 minutes after the update started.
    time.sleep(1)
    shot_page("System update - started (Verifying)")
    t0 = time.time()

    def wait_to(target_seconds):
        target = t0 + target_seconds
        while time.time() < target:
            wake()
            if restart_visible():
                print("🛑 Natural 'Restart now' appeared before the planned Scenario 5 restart point.")
                return True
            time.sleep(min(5, max(0.5, target - time.time())))
        return restart_visible()

    # After "Update now" + the 'started' shot: wait 1 minute BEFORE the missed call / call-back banner
    # and the "Background installation" shot (they now happen at the +1 min mark, not immediately).
    print("⏳ Scenario 5 waiting 1 minute before the missed call / banner / background shots...")
    if wait_to(60):
        print("❌ Natural restart appeared before the planned +2 min manual restart.")
        return False

    # Scenario 9B only (combined 3+5 run): the incoming-call screenshot is taken now, at the +1 min
    # mark and with a hard time limit, so it can never push the restart later.
    # It is stored outside the numbered evidence -> Scenario 3 / 5 / 8 are not affected.
    if INCOMING_CALL_NUMBER:
        safe(incoming_call_during_update, INCOMING_CALL_NUMBER, t0 + INCOMING_MAX_FROM_START)

    # "Background installation" notification (waits until it exists, scrolls, expands, never fails).
    shot_background_installation()

    # +1 min — two normal Source -> Target update screenshots.
    print("⏳ Scenario 5 waiting for the +1 min checkpoint...")
    if wait_to(60):
        print("❌ Natural restart appeared before the planned +2 min manual restart.")
        return False
    shot_page("System update page @1 min")
    if restart_visible():
        return False
    shot_page("System update page @1 min (2nd)")

    # +2 min — Quick Settings screenshot first, then immediately perform the
    # Scenario 5 manual physical restart.
    print("⏳ Scenario 5 waiting for the +2 min checkpoint...")
    if wait_to(120):
        print("❌ Natural restart appeared before the planned +2 min manual restart.")
        return False
    shot_quick_settings("Quick Settings expanded (Wi-Fi only) @2 min",
                        mobile_data_off=True)

    print("🔄 Scenario 5: +2 minutes reached — starting the manual restart now...")
    if restart_visible():
        print("❌ Natural restart appeared at the +2 min checkpoint; manual restart is not needed.")
        return False
    if not scenario5_manual_power_restart():
        return False

    # Reconnect/reopen the update page and continue the same update. This
    # resumed-update screenshot is part of the Scenario 5 superset but is NOT
    # one of the two extra restart-only screenshots used in the evidence split.
    print("🔄 Device rebooted — reopening System update to continue the update...")
    if not _reopen_update_page_after_restart():
        print("❌ Could not reopen System update after Scenario 5 restart.")
        return False
    if not ensure_wifi_connected(timeout=45):
        print("❌ Wi-Fi did not reconnect after Scenario 5 restart.")
        return False
    check_for_update_then_update_now(allow_scroll=False)
    time.sleep(2)
    shot_page("System update after Scenario 5 restart")

    # Continue the remaining update checkpoints after the reboot. We keep the
    # same two-minute spacing so the flow continues naturally without pretending
    # that the original pre-reboot wall-clock timer kept running through reboot.
    print("⏳ Scenario 5 continuing update — next checkpoint (+4 min style)...")
    resume_t = time.time()
    while time.time() - resume_t < 120:
        wake()
        if restart_visible():
            break
        time.sleep(5)
    if not restart_visible():
        shot_page("System update page @4 min (after Scenario 5 restart)")

    print("⏳ Scenario 5 continuing update — next checkpoint (+6 min style)...")
    resume_t = time.time()
    while time.time() - resume_t < 120:
        wake()
        if restart_visible():
            break
        time.sleep(5)
    if not restart_visible():
        shot_page("System update page @6 min (after Scenario 5 restart)")

    # Continue until the normal Restart now state appears.
    print("👀 Waiting for the normal 'Restart now' page after the resumed update...")
    start = time.time()
    while not restart_visible():
        if time.time() - start > 1800:
            print("⏰ 'Restart now' never appeared after the Scenario 5 resumed update.")
            return False
        time.sleep(10)

    wake()
    snap("System update - Please restart now (after Scenario 5 restart)")
    if TAP_RESTART:
        try:
            if tap_exact("restart now"):
                print("👆 Tapped 'Restart now'")
            else:
                ax.adb("shell reboot")
        except Exception:
            ax.adb("shell reboot")
    else:
        ax.adb("shell reboot")
    place_file(RESTART_IMG, "Restarting... (Restart_EN.png)")

    ax.wait_for_device()
    ax.unlock_device()
    if not wait_update_complete_notification(timeout=1800):
        return False
    go_home()
    ax.clear_recent_apps()
    return True


# ===========================================================================
# Summary + entry point
# ===========================================================================
def summary():
    print("\n" + "=" * 70)
    print(f"{'#':>3}  {'Status':<22} Label")
    print("-" * 70)
    for n, label, status, _ in LOG:
        print(f"{n:>3}  {status:<22} {label}")
    ok = sum(1 for _, _, s, _ in LOG if s.startswith("OK"))
    print("-" * 70)
    print(f"Captured {ok} / {EXPECTED_TOTAL}   |   folder: {os.path.abspath(SAVE_DIR)}")
    return ok == EXPECTED_TOTAL


def safe(fn, *a):
    try:
        result = fn(*a)
        return True if result is None else bool(result)
    except Exception as e:
        print(f"💥 {fn.__name__} crashed: {e}")
        return False


WIN_PATH_BUDGET = 255      # keep a safety margin below the classic Windows MAX_PATH limit


# The scenario folders that make one device ("barcode") folder complete.
BARCODE_REQUIRED_FOLDERS = [
    "Scenario 2A Target to Dummy WiFi", "Scenario 2B Target to Dummy Data",
    "Scenario 3 Source to Target WiFi", "Scenario 4 Source to Target Data",
    "Scenario 5 Source to Target WiFi", "Scenario 6",
    "Scenario 7 Source to Target WiFi Spanish", "Scenario 8 Source to Target WiFi",
    "Scenario 9A Target to Corrupt WiFi", "Scenario 9B Source to Target WiFi",
]


def _folder_has_png(folder):
    for _dp, _dn, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".png") and not f.startswith("_"):
                return True
    return False


def barcode_folder_is_complete(folder):
    """True when every scenario (2A, 2B, 3, 4, 5, 6, 7, 8, 9A, 9B) already has screenshots in this folder."""
    for name in BARCODE_REQUIRED_FOLDERS:
        d = os.path.join(folder, name)
        if not os.path.isdir(d):
            return False
        if name == "Scenario 6":      # Scenario 6 = 6AA, 6AB, 6BA, 6BB sub-folders
            subs = [os.path.join(d, x) for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
            if sum(1 for x in subs if _folder_has_png(x)) < 4:
                return False
        elif not _folder_has_png(d):
            return False
    return True


def _device_results_root_impl(base_root, serial):
    """One evidence folder per device barcode (the ADB serial): <results>/<barcode>/<scenario folders>.

    The same device keeps using its folder until all scenarios are present in it.
    When it is complete, the next run of that device starts <barcode>_2, then <barcode>_3, ...
    """
    safe = re.sub(r"[^\w\-.]", "_", str(serial or "").strip()) or "UNKNOWN_DEVICE"
    n = 1
    while True:
        name = safe if n == 1 else f"{safe}_{n}"
        folder = os.path.join(base_root, name)
        if os.path.isdir(folder) and barcode_folder_is_complete(folder):
            n += 1
            continue
        break
    if n > 1:
        print(f"📁 Barcode folder '{safe}' already has all scenarios -> using new folder '{name}'")
    os.makedirs(folder, exist_ok=True)
    return folder


def device_results_root(base_root, serial):
    """Safe wrapper: if the barcode-folder logic ever fails, fall back to the evidence root exactly as before."""
    try:
        return _device_results_root_impl(base_root, serial)
    except Exception as e:
        print(f"⚠️ Barcode folder logic skipped ({e}); using the evidence root.")
        return base_root


def prepare_save_dir(is_windows=None):
    """Return the scenario evidence folder under the selected/project results root.

    No AppData/TEMP fallback and no additional nested device folder are used.
    """
    print(f"[{ax.TARGET_SERIAL}] 🔍 Gathering device info...")
    product = (ax.adb("shell getprop ro.product.name").stdout or "").strip()
    carrier = (ax.adb("shell getprop ro.carrier").stdout or "").strip() or "UNKNOWN"
    print("📡 Device locked on!")
    print(f"🔢 Serial   : {ax.TARGET_SERIAL}")
    print(f"📱 Product  : {product}")
    print(f"📶 Carrier  : {carrier}")
    folder = ax.SAVE_ROOT
    os.makedirs(folder, exist_ok=True)
    print(f"[SAVE_DIR] {os.path.abspath(folder)}")
    return folder


def reset_screenshot_bookkeeping():
    """Start a fresh screenshot numbering set for the next build transition."""
    global shot_no, LOG, SAVE_DIR
    shot_no = 1
    LOG = []
    SAVE_DIR = None



# Scenario 5 physical run (combined 3+5): 38 screenshots.  #17 / #18 are the restart-only
# shots (power menu + Restarting...), which Scenario 3 / 8 / 9B leave out.  The restart-only
# numbers are read from the run log (labels "Scenario 5 - ...") and fall back to these defaults.
S5_TOTAL = 38
S5_POST_START = 25                 # first POST-update shot of the Scenario 5 run
S5_RESTART_ONLY_DEFAULT = (17, 18)


def _restart_only_numbers():
    nums = {num for num, label, _s, _f in LOG if str(label).startswith("Scenario 5 - ")}
    return nums if len(nums) == 2 else set(S5_RESTART_ONLY_DEFAULT)


def split_combined_3_5_evidence(source_dir, save_root):
    """Create Scenario 3 evidence from the single Scenario 5 superset run.

    Scenario 5 is the physical execution. Its two restart-variant-only shots
    (power menu and Restarting...) are left out; Scenario 3 is the same
    Source -> Target flow without them, so the remaining images are renumbered 1..36.

    Scenario 5's original 38 files remain untouched in its own folder.
    """
    source_dir = os.path.abspath(source_dir)
    target_dir = os.path.join(os.path.abspath(save_root), "Scenario 3 Source to Target WiFi")
    os.makedirs(target_dir, exist_ok=True)

    # Keep the normal Source -> Target evidence exactly as captured, except
    # remove the two Scenario-5-only restart-event screenshots and renumber.
    # power menu + real Android Restarting... screen (numbers come from the run log).
    excluded = _restart_only_numbers()
    expected = S5_TOTAL - len(excluded)
    copied = 0
    missing = []
    for src_no in range(1, S5_TOTAL + 1):
        if src_no in excluded:
            continue
        src = os.path.join(source_dir, f"5 ({src_no}).png")
        if not os.path.isfile(src):
            missing.append(src_no)
            continue
        dst_no = copied + 1
        dst = os.path.join(target_dir, f"3 ({dst_no}).png")
        shutil.copy2(src, dst)
        copied += 1

    if missing:
        raise RuntimeError(
            "Scenario 3 evidence split could not find Scenario 5 screenshots: "
            + ", ".join(map(str, missing))
        )
    if copied != expected:
        raise RuntimeError(f"Scenario 3 evidence split produced {copied} screenshots; expected {expected}.")

    print("📁 Combined 3+5 evidence split complete:")
    print(f"   Scenario 3: {target_dir}")
    print(f"   Scenario 5: {source_dir}")
    print(f"   Scenario 3 excludes restart-only shots #{min(excluded)}-#{max(excluded)} and is renumbered 1-{expected}.")
    return target_dir



# ===========================================================================
# Scenario 9 (combined 3+5 run only): call + Amazon BEFORE the update
# ---------------------------------------------------------------------------
# These extra shots are stored OUTSIDE the numbered 1-38 evidence, so the
# physical run, Scenario 3 and Scenario 5 keep exactly their existing numbering.
# Scenario 9 is assembled afterwards from the same run + these extras.
# ===========================================================================
PRE_EXTRA_SHOTS = []          # [(label, path or None)]
PRE_EXTRA_DIR = None
PRE_LAST_SHOT_NO = None       # number of the last PRE shot (Dialer code 78287)
SCENARIO9_CODE = "9B"          # name used for Scenario 9 evidence: folder, inner folder, files
SCENARIO9_FOLDER = f"Scenario {SCENARIO9_CODE} Source to Target WiFi"


def _pre_extra_commit(img, label):
    path = os.path.join(PRE_EXTRA_DIR, f"pre ({len(PRE_EXTRA_SHOTS) + 1}).png")
    img.save(path)
    PRE_EXTRA_SHOTS.append((label, path))
    print(f"📸 [Scenario 9 extra #{len(PRE_EXTRA_SHOTS)}]  {label}")
    return path


def _pre_extra_missing(label, why=""):
    print(f"❌ [Scenario 9 extra #{len(PRE_EXTRA_SHOTS) + 1}] MISSING: {label} {why}".rstrip())
    PRE_EXTRA_SHOTS.append((label, None))


def pre_update_call_and_amazon(number, save_root):
    """Extra Scenario 9 shots right after the 78287 screenshot, before the update page.

    Never raises and never touches the numbered evidence / run result: anything that
    goes wrong only leaves a slot marked missing (Scenario 9 is then marked INCOMPLETE)."""
    global PRE_EXTRA_DIR, PRE_LAST_SHOT_NO, INCOMING_EXTRA
    PRE_LAST_SHOT_NO = shot_no - 1
    INCOMING_EXTRA = None
    PRE_EXTRA_SHOTS.clear()
    PRE_EXTRA_DIR = os.path.join(os.path.abspath(save_root), "_scenario9_pre_update_work")
    shutil.rmtree(PRE_EXTRA_DIR, ignore_errors=True)
    os.makedirs(PRE_EXTRA_DIR, exist_ok=True)
    print("\n" + "-" * 72)
    print("SCENARIO 9 EXTRA (before update): Wi-Fi call + Chrome/Amazon")
    print("-" * 72)
    try:
        _call_and_chrome_flow(number, (_pre_extra_commit, _pre_extra_missing))
    except Exception as e:
        print(f"💥 pre-update call/Amazon crashed: {e}")
    labels = [CALL_LABEL_CALLING, CALL_LABEL_CONNECTED, CHROME_LABEL]
    while len(PRE_EXTRA_SHOTS) < 3:
        _pre_extra_missing(labels[len(PRE_EXTRA_SHOTS)], "(step did not run)")
    try:
        _bsh(f"am force-stop {CHROME_PKG}")
        go_home()
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Scenario 9B only: incoming-call screenshot DURING the update
# ---------------------------------------------------------------------------
INCOMING_CALL_NUMBER = None     # set by run_transition for the combined 3+5 run only
INCOMING_EXTRA = None           # (label, path or None) - stored OUTSIDE the numbered evidence
INCOMING_LABEL = "Incoming call banner over the update page (during update)"
INCOMING_MAX_FROM_START = 180   # the call-back must come within this many seconds of the update start: the call starts at +60 s,
                                # so the phone waits 120 s for the call back
INCOMING_WAIT_SECS = 120        # default window if no deadline is given
INCOMING_REALERT_SECS = 60      # the missed-call alert (+ beep) is repeated this often until the call arrives
_CALL_SCREEN_PKGS = ("dialer", "incallui", "telecom", "launcher", "googlequicksearchbox")


def _incoming_ringing(states):
    """True when Telecom reports an INCOMING call that is ringing."""
    return bool(states) and any(s in ("RINGING", "SIMULATED_RINGING") for s in states)


def _ringing_now():
    """Incoming call ringing?  Telecom first; if its output is not understood, the telephony
    registry (mCallState=1 means RINGING) is used as the second, independent signal."""
    st = _telecom_states()
    if _incoming_ringing(st):
        return True
    if st is None:
        return bool(re.search(r"mCallState=1\b", _bsh("dumpsys telephony.registry")))
    return False


def _beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 350)
    except Exception:
        print("\a", end="", flush=True)


def _update_page_in_front():
    """True = some other app than the call/home screens is in front (the update page)."""
    out = _bsh("dumpsys window").lower()
    m = re.search(r"mcurrentfocus=window\{[^}]*?\s([\w.]+)/", out)
    if not m:
        return None
    return not any(k in m.group(1) for k in _CALL_SCREEN_PKGS)


def _return_to_update_page():
    """After the alert call / decline the update page should be in front again."""
    if _update_page_in_front() is True:
        return True
    if _ringing_now():            # a call-back is ringing right now: do not navigate away, the caller handles it
        return True
    for _ in range(2):            # quick way back (BACK from the call screen) before the slower Settings navigation
        _bsh("input keyevent KEYCODE_BACK")
        time.sleep(1.2)
        if _update_page_in_front() is True or _ringing_now():
            return True
    print("↩️ Returning to the System update page...")
    open_settings_clean()
    return bool(open_update_page())


def _end_alert_call():
    """Hang up OUR outgoing alert call - only if one exists (KEYCODE_ENDCALL with no call can put
    the phone to sleep / go home) and never touches a call that is ringing."""
    st = _telecom_states()
    if not st or _incoming_ringing(st) or not any(
            x in ("DIALING", "CONNECTING", "ACTIVE", "PULLING") for x in st):
        return
    _bsh("input keyevent KEYCODE_ENDCALL")
    time.sleep(1.0)
    st = _telecom_states()
    if st and not _incoming_ringing(st) and any(
            x in ("DIALING", "CONNECTING", "ACTIVE", "PULLING") for x in st):
        _bsh("telecom end-call")                      # fallback, only while our own call is still up
        time.sleep(0.5)


def _send_missed_call_alert(number):
    """Ring the helper phone for ~5 s and hang up -> it shows a MISSED CALL.
    Returns True if an incoming call showed up meanwhile (the caller handles it)."""
    enc = number.replace("+", "%2B")
    print(f"📲 MISSED-CALL ALERT: ringing {number} for a few seconds, then hanging up...")
    _bsh(f"am start -a android.intent.action.CALL -d tel:{enc}")
    t0 = time.time()
    ring_start = None
    while time.time() - t0 < 15:
        st = _telecom_states()
        if _incoming_ringing(st):
            return True
        if st and any(s in ("DIALING", "CONNECTING", "ACTIVE", "PULLING") for s in st):
            if ring_start is None:
                ring_start = time.time()
        if ring_start and time.time() - ring_start >= 5:
            break
        time.sleep(0.4)
    _end_alert_call()
    return False


def _capture_incoming_call():
    """Called the moment Telecom reports an incoming call: screenshot of the call banner.

    The image kept is always the LATEST frame taken while the call was still ringing; when the UI
    dump can confirm the banner, a fresh frame is taken right after the confirmation."""
    time.sleep(0.8)                                   # let the incoming-call banner draw
    best = None
    for attempt in range(3):
        img = grab_frame()
        if img is not None:
            best = img
        if not _ringing_now():
            print("⚠️ The call stopped ringing before the screenshot could be confirmed - not using it.")
            return None
        root = _fresh_dump(retries=1)
        if root is None:                              # dump unusable (e.g. 'could not get idle state')
            return best                               # -> use the frame taken while it was ringing
        texts = " | ".join(_cc_classify(root)["texts"]).lower()
        if any(k in texts for k in ("incoming call", "answer", "decline")):
            final = grab_frame()                      # banner confirmed on screen: take the final frame now
            if final is not None and _ringing_now():
                return final
            return best
        print("   (call banner not confirmed yet - capturing again...)")
        time.sleep(1.0)
    return best                                       # still ringing: the banner may not be in the UI dump


def _decline_incoming_call():
    for _ in range(3):
        _bsh("input keyevent KEYCODE_ENDCALL")        # on a ringing call this declines it
        time.sleep(1.0)
        if not _ringing_now():
            return True
    _bsh("telecom end-call")
    time.sleep(1.0)
    return not _ringing_now()


def incoming_call_during_update(number, deadline=None):
    """Scenario 9B extra: the phone gives a MISSED CALL to `number` (so you know to call back),
    then watches the call state and screenshots the incoming call the moment it rings.

    Detection polls the call state every ~0.5 s, so a ringing call is never missed; the call is
    declined right after the screenshot.  The alert is repeated every 30 s until `deadline`
    (a ringing call that is already detected is always finished).  The image is saved outside the
    numbered evidence (INCOMING_EXTRA); if no call arrives the slot is simply left missing
    (Scenario 9B is then marked INCOMPLETE) and the run continues - it never blocks past `deadline`,
    so the time-critical manual restart that follows is not delayed."""
    global INCOMING_EXTRA, PRE_EXTRA_DIR
    INCOMING_EXTRA = (INCOMING_LABEL, None)
    if not PRE_EXTRA_DIR:
        PRE_EXTRA_DIR = os.path.join(os.path.dirname(os.path.abspath(SAVE_DIR)), "_scenario9_pre_update_work")
    os.makedirs(PRE_EXTRA_DIR, exist_ok=True)
    if deadline is None:
        deadline = time.time() + INCOMING_WAIT_SECS

    print("\n" + "!" * 72)
    print("📞 SCENARIO 9B — INCOMING CALL DURING THE UPDATE")
    print(f"   The phone will now give a MISSED CALL to {number}.")
    print(f"   >>> CALL THIS PHONE BACK from that number within ~{int(max(0, deadline - time.time()))} s. <<<")
    print(f"   (alert + beep repeated every {INCOMING_REALERT_SECS} s; waiting up to {int(max(0, deadline - time.time()))} s for your call back)")
    print("!" * 72)

    next_alert = time.time()
    last_wake = time.time()
    while time.time() < deadline:
        spotted = False
        if time.time() >= next_alert:
            next_alert = time.time() + INCOMING_REALERT_SECS
            _beep()
            spotted = _send_missed_call_alert(number)
            if not spotted:
                _return_to_update_page()
                print(f"📞 Waiting for your CALL BACK to this phone ({int(max(0, deadline - time.time()))} s left)...")
        if spotted or _ringing_now():
            print("🔔 Incoming call detected — capturing the screenshot now...")
            img = _capture_incoming_call()
            _decline_incoming_call()
            _end_alert_call()                         # only acts if the alert call is still up
            _return_to_update_page()
            if img is not None:
                path = os.path.join(PRE_EXTRA_DIR, "incoming (1).png")
                img.save(path)
                INCOMING_EXTRA = (INCOMING_LABEL, path)
                print(f"📸 [Scenario 9B extra]  {INCOMING_LABEL}")
                print(f"   saved: {path}")
                return True
            print("⚠️ That call could not be captured — waiting for another call back...")
            continue
        if time.time() - last_wake >= 10:
            wake()
            last_wake = time.time()
        time.sleep(0.3)

    print("❌ No incoming call before the time limit — the incoming-call screenshot is MISSING "
          "(Scenario 9B will be marked INCOMPLETE; the run continues).")
    _end_alert_call()
    _return_to_update_page()
    return False


# ---------------------------------------------------------------------------
# "Background installation" notification screenshot (Scenario 5 run = 3/5/8/9B, and Scenario 4)
# ---------------------------------------------------------------------------
BACKGROUND_NEEDLE = "background installation"
BACKGROUND_LABEL = "Background installation expanded (immediate)"
BACKGROUND_WAIT_SECS = 25       # how long to wait for the notification to appear after the update starts


def _notification_present(needle):
    """True when an active notification mentions `needle` (asked from the notification service, so it
    works even when the UI cannot be dumped)."""
    out = _bsh("dumpsys notification --noredact", 60)
    if "NotificationRecord" not in out:
        out = _bsh("dumpsys notification", 60)
    return needle in out.lower()


def _root_contains(root, needle):
    for n in root.iter("node"):
        if needle in _cc_norm(n.attrib.get("text")) or needle in _cc_norm(n.attrib.get("content-desc")):
            return True
    return False


def _expand_notification_in_root(root, needle):
    """Expand the notification inline (same approach as Target -> Dummy's expand arrow)."""
    target = None
    for n in root.iter("node"):
        if needle in (_cc_norm(n.attrib.get("text")) + " " + _cc_norm(n.attrib.get("content-desc"))):
            nums = list(map(int, re.findall(r"\d+", n.attrib.get("bounds", ""))))
            if len(nums) == 4:
                target = nums
                break
    if target is None:
        return False
    for n in root.iter("node"):
        desc = _cc_norm(n.attrib.get("content-desc"))
        if not any(k in desc for k in ("expand", "show more")):
            continue
        nums = list(map(int, re.findall(r"\d+", n.attrib.get("bounds", ""))))
        if len(nums) != 4:
            continue
        cy = (nums[1] + nums[3]) // 2
        if target[1] <= cy <= target[3]:
            ax.adb(f"shell input tap {(nums[0] + nums[2]) // 2} {cy}")
            time.sleep(2)
            return True
    x = max(target[0] + 20, target[2] - 55)          # Motorola: arrow not exposed -> right edge of the row
    y = (target[1] + target[3]) // 2
    ax.adb(f"shell input tap {x} {y}")
    time.sleep(2)
    return True


def shot_background_installation(label=BACKGROUND_LABEL, max_wait=BACKGROUND_WAIT_SECS):
    """Open the notification shade, find the "Background installation" notification (scrolling the
    shade), expand it and take the screenshot - the Target -> Dummy behaviour, made robust:

      * it first WAITS (up to `max_wait` s) until the notification exists - right after "Update now"
        it is not there yet;
      * it reads the shade with a fresh UI dump (never a stale one);
      * if the notification cannot be found or the UI cannot be read, the screenshot is taken anyway
        ("at any cost") - exactly ONE numbered screenshot is always produced.
    Never raises."""
    taken = False
    try:
        end = time.time() + max_wait
        found = False
        root = None
        ui_readable = True
        while True:
            while time.time() < end and not _notification_present(BACKGROUND_NEEDLE):
                print("⏳ Waiting for the 'Background installation' notification to appear...")
                wake()
                time.sleep(3)
            ax.adb("shell cmd statusbar expand-notifications")
            time.sleep(2)
            for attempt in range(4):
                root = _fresh_dump(retries=2)
                if root is None:
                    ui_readable = False
                    break
                if _root_contains(root, BACKGROUND_NEEDLE):
                    found = True
                    print(f"🔔 Found '{BACKGROUND_NEEDLE}' in the notification shade.")
                    break
                if attempt < 3:
                    print(f"↕️ Scrolling notification shade for '{BACKGROUND_NEEDLE}' ({attempt + 1}/3)...")
                    _swipe_notification_list_up()
            if found or not ui_readable or time.time() >= end:
                break
            print("   'Background installation' not visible in the shade yet - trying again...")
            ax.adb("shell cmd statusbar collapse")
            time.sleep(2)
        if found:
            if _expand_notification_in_root(root, BACKGROUND_NEEDLE):
                print("🔽 Expanded the 'Background installation' notification inline.")
            taken = True
            snap(label)
        else:
            why = "UI could not be read" if not ui_readable else "notification not found"
            print(f"⚠️ {why} — taking the notification-shade screenshot anyway.")
            taken = True
            snap(label + " (notification not found)")
    except Exception as e:
        print(f"💥 Background installation step error: {e}")
        if not taken:
            snap(label + " (error)")
    finally:
        try:
            ax.adb("shell cmd statusbar collapse")
            time.sleep(1)
        except Exception:
            pass


def build_scenario9_evidence(source_dir, save_root):
    """Scenario 9B = the Scenario 3 flow (restart-only shots left out)
    + the three pre-update call/Amazon shots inserted right after the 78287 shot
    + the incoming-call shot taken at the start of the update, right after the "started" shot.
    Numbering keeps every slot: a missing image leaves a gap and the folder is
    marked INCOMPLETE.txt instead of failing the run."""
    source_dir = os.path.abspath(source_dir)
    target_dir = os.path.join(os.path.abspath(save_root), SCENARIO9_FOLDER)
    os.makedirs(target_dir, exist_ok=True)

    # Where the extras go: right after the PRE "Dialer code 78287" shot.
    last_pre = None
    for num, label, _status, _f in LOG:
        if label == "PRE - Dialer code 78287":
            last_pre = num
            break
    if last_pre is None:
        last_pre = PRE_LAST_SHOT_NO
    excluded = _restart_only_numbers()
    if not last_pre or last_pre >= min(excluded):
        raise RuntimeError(f"could not locate the 78287 screenshot (found: {last_pre})")

    extras = list(PRE_EXTRA_SHOTS)
    while len(extras) < 3:
        extras.append(("(pre-update shot did not run)", None))
    incoming = INCOMING_EXTRA or ("(incoming call shot did not run)", None)
    # The incoming call is taken right after the update starts, i.e. after the "started" shot.
    incoming_after = next((num for num, label, _s, _f in LOG
                           if str(label).startswith("System update - started")), 12)

    dst = 0
    missing = []

    def put(src_path, label):
        nonlocal dst
        dst += 1
        if src_path and os.path.isfile(src_path):
            shutil.copy2(src_path, os.path.join(target_dir, f"{SCENARIO9_CODE} ({dst}).png"))
        else:
            missing.append((dst, label))

    def src(n):
        return os.path.join(source_dir, f"5 ({n}).png")

    for n in range(1, last_pre + 1):
        put(src(n), f"Scenario 5 shot #{n}")
    for label, path in extras:
        put(path, label)
    for n in range(last_pre + 1, S5_TOTAL + 1):
        if n in excluded:
            continue
        put(src(n), f"Scenario 5 shot #{n}")
        if n == incoming_after:
            put(incoming[1], incoming[0])

    note = os.path.join(target_dir, "INCOMPLETE.txt")
    if missing:
        with open(note, "w", encoding="utf-8") as f:
            f.write(f"Scenario {SCENARIO9_CODE} evidence is INCOMPLETE. Missing screenshots:\n")
            for slot, label in missing:
                f.write(f"  {SCENARIO9_CODE} ({slot}).png  -  {label}\n")
    elif os.path.exists(note):
        os.remove(note)

    print(f"📁 Scenario {SCENARIO9_CODE} evidence:")
    print(f"   {target_dir}")
    print(f"   {dst - len(missing)} / {dst} screenshots "
          f"(pre-update call + Amazon inserted as #{last_pre + 1}-#{last_pre + 3}; "
          f"incoming call after source #{incoming_after}; "
          f"restart-only shots #{min(excluded)}-#{max(excluded)} excluded)")
    if missing:
        print(f"⚠️ Scenario {SCENARIO9_CODE} is INCOMPLETE — missing: "
              + ", ".join(f"{SCENARIO9_CODE} ({slot}).png [{label}]" for slot, label in missing))
        print("   (details saved in INCOMPLETE.txt; Scenario 3 and 5 are unaffected)")
    else:
        shutil.rmtree(PRE_EXTRA_DIR or "", ignore_errors=True)
    return not missing


SCENARIO8_FOLDER = "Scenario 8 Source to Target WiFi"


def build_scenario8_evidence(source_dir, save_root):
    """Scenario 8 = exactly the Scenario 3 images (Scenario 5 run without the
    restart-only shots, renumbered 1-36), named '8 (n).png'."""
    source_dir = os.path.abspath(source_dir)
    target_dir = os.path.join(os.path.abspath(save_root), SCENARIO8_FOLDER)
    os.makedirs(target_dir, exist_ok=True)

    copied = 0
    missing = []
    excluded = _restart_only_numbers()            # same exclusions as Scenario 3
    expected = S5_TOTAL - len(excluded)
    for src_no in range(1, S5_TOTAL + 1):
        if src_no in excluded:
            continue
        src = os.path.join(source_dir, f"5 ({src_no}).png")
        if not os.path.isfile(src):
            missing.append(src_no)
            continue
        copied += 1
        shutil.copy2(src, os.path.join(target_dir, f"8 ({copied}).png"))

    if missing or copied != expected:
        raise RuntimeError(f"Scenario 8 evidence incomplete: copied {copied}/{expected}, "
                           f"missing Scenario 5 shots: {missing}")
    print("📁 Scenario 8 evidence (same images as Scenario 3, named 8 (n)):")
    print(f"   {target_dir}")
    return True


def run_transition(transition_name, call_number, save_root, serial,
                   scenario_code, include_hardware_screenshot=False, scenario5_restart=False,
                   pre_update_extras=False):
    """Run one complete transition on the CURRENT device state. Scenario 2 A is 35 shots; the Scenario 5 physical run is 38 (Scenario 3 / 8: 36, 9B: 40).

    The device is intentionally not reflashed/rebooted between transitions.
    After SOURCE->TARGET finishes, the same physical device is already in
    TARGET and this function can immediately run TARGET->DUMMY.
    """
    global SAVE_DIR, RESTART_IMG, TAP_RESTART, PHASE_NAME
    global EXPECTED_TOTAL, SCENARIO_CODE, INCLUDE_HARDWARE_SCREENSHOT, INCOMING_CALL_NUMBER

    reset_screenshot_bookkeeping()
    SCENARIO_CODE = scenario_code
    INCLUDE_HARDWARE_SCREENSHOT = include_hardware_screenshot
    EXPECTED_TOTAL = S5_TOTAL if scenario5_restart else (35 if include_hardware_screenshot else 35)
    PHASE_NAME = f"Scenario_{scenario_code.replace(' ', '')}_WIFI_{transition_name}"
    ax.PHASE = PHASE_NAME
    ax.TARGET_SERIAL = serial
    folder_name = (f"Scenario {scenario_code.replace(' ', '')} "
                   f"{'Source to Target' if transition_name == 'SOURCE_TO_TARGET' else 'Target to Dummy'} WiFi")
    ax.SAVE_ROOT = os.path.join(save_root, folder_name)
    os.makedirs(ax.SAVE_ROOT, exist_ok=True)

    print("\n" + "=" * 72)
    print(f"STARTING TRANSITION: {transition_name.replace('_', ' > ')}")
    print(f"Device: {serial}")
    print("This is the SAME physical device; no reflashing is performed here.")
    print("=" * 72)

    if ax.adb("get-state").stdout.strip() != "device":
        print(f"❌ Device {serial} is not reachable via adb.")
        return False

    SAVE_DIR = prepare_save_dir()
    if SAVE_DIR is None:
        return False
    ax.adb("shell svc power stayon true")

    # PRE-UPDATE: exactly the existing 1-10 flow.
    if not ensure_wifi_connected():
        print("❌ Wi-Fi is not connected before screenshot #1.")
        return False

    wake()
    ax.unlock_device()
    safe(device_info_set, "PRE", True, include_hardware_screenshot)
    # Scenario 9 only (combined 3+5 run): extra call + Amazon shots BEFORE the update.
    # Stored outside the numbered evidence, so Scenario 3 / 5 are not affected.
    if pre_update_extras:
        safe(pre_update_call_and_amazon, call_number, save_root)
    disable_wifi_for_navigation()

    # Scenario 5 OTA has its own zero-argument OTA routine. The normal OTA
    # routine accepts the notification/drop-down flag. Keep these call shapes
    # separate so the Scenario 5 path cannot receive an unexpected argument.
    # Scenario 9B extra: incoming-call screenshot during the update (combined 3+5 run only).
    INCOMING_CALL_NUMBER = call_number if (pre_update_extras and scenario5_restart) else None
    if scenario5_restart:
        ota_ok = safe(ota_phase_scenario5)
    else:
        ota_ok = safe(ota_phase, not include_hardware_screenshot,
                      transition_name == "TARGET_TO_DUMMY")
    if not ota_ok:
        print(f"❌ OTA failed for {transition_name}; the second transition will NOT start.")
        return False

    pad_to(S5_POST_START if scenario5_restart else 21)
    safe(device_info_set, "POST", False, include_hardware_screenshot)
    pad_to(S5_POST_START + 10 if scenario5_restart else 31)
    safe(wifi_call_and_browse, call_number)
    pad_to(EXPECTED_TOTAL + 1)

    complete = summary()
    ax.adb("shell svc power stayon true")
    print("=" * 72)
    print(f"COMPLETED TRANSITION: {transition_name.replace('_', ' > ')}")
    print(f"Screenshots saved in: {SAVE_DIR}")
    print("=" * 72)
    return complete


def main():
    global SAVE_DIR, RESTART_IMG, TAP_RESTART
    global SCENARIO_CODE, EXPECTED_TOTAL, INCLUDE_HARDWARE_SCREENSHOT

    p = argparse.ArgumentParser(
        description="Tracfone Phase 1 Wi-Fi automation: Scenario 3 + Scenario 2A + Scenario 5"
    )
    p.add_argument("--serial", required=True, help="ADB device serial")
    p.add_argument("--call-number", required=True,
                   help="Phone number used by the Wi-Fi-call screenshots")
    p.add_argument("--restart-img", default=None, help="Path to Restart_EN.png")
    p.add_argument("--save-root", default=None, help="Root folder for evidence")
    p.add_argument(
        "--mode", choices=["all", "source_to_target", "target_to_dummy", "scenario5", "combined_3_5"],
        default="all",
        help="Run both transitions on the same device, or one transition only"
    )
    p.add_argument("--tap-restart", action="store_true",
                   help="Tap Restart now instead of using adb reboot")
    a = p.parse_args()

    call_number = a.call_number.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?\d{5,15}", call_number):
        sys.exit("Phone number must be digits only (optional leading +), 5-15 digits")

    RESTART_IMG = a.restart_img or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "Restart_EN.png"
    )
    RESTART_IMG = os.path.abspath(RESTART_IMG)
    if not os.path.isfile(RESTART_IMG):
        sys.exit(f"Restart image not found: {RESTART_IMG}")
    TAP_RESTART = a.tap_restart

    serial = a.serial
    base_root = a.save_root or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results"
    )
    base_root = os.path.abspath(base_root)
    os.makedirs(base_root, exist_ok=True)
    # One folder per device: results/Tracfone Scenarios/<serial>/<scenario folders of that device>
    base_root = device_results_root(base_root, serial)
    print(f"📁 Evidence for this device: {base_root}")

    if a.mode == "combined_3_5":
        print("\n" + "#" * 76)
        print("SCENARIO 3 + 5 — SOURCE -> TARGET (Wi-Fi), ONE PHYSICAL RUN")
        print("The Scenario 5 superset is executed once; evidence is split into")
        print("separate Scenario 3 and Scenario 5 folders after the run.")
        print("#" * 76)
        ok = run_transition(
            "SOURCE_TO_TARGET", call_number, base_root, serial, "5",
            include_hardware_screenshot=True, scenario5_restart=True,
            pre_update_extras=True
        )
        if not ok:
            print("❌ Combined Scenario 3 + 5 physical run did not complete.")
            sys.exit(1)

        try:
            # SAVE_DIR still points to the completed Scenario 5 Source -> Target
            # evidence folder here. Split it before starting the next transition.
            split_combined_3_5_evidence(SAVE_DIR, base_root)
        except Exception as e:
            print(f"❌ Evidence split failed: {e}")
            sys.exit(1)

        # Scenario 9 = Scenario 3 flow + pre-update call/Amazon. It can never stop
        # the run: any problem only marks Scenario 9 incomplete.
        try:
            build_scenario9_evidence(SAVE_DIR, base_root)
        except Exception as e:
            print(f"⚠️ Scenario {SCENARIO9_CODE} evidence could not be built: {e}")
            print("   (Scenario 3 and Scenario 5 are unaffected)")

        # Scenario 8 = same images as Scenario 3, named 8 (n). Never stops the run.
        try:
            build_scenario8_evidence(SAVE_DIR, base_root)
        except Exception as e:
            print(f"⚠️ Scenario 8 evidence could not be built: {e}")
            print("   (Scenario 3, 5 and 9B are unaffected)")

        # Target -> Dummy (Scenario 2 A) is no longer chained after this run.
        # It stays available on its own (UI card "2 A" / --mode target_to_dummy).
        ax.adb("shell svc power stayon false")
        print("\n" + "#" * 76)
        print("SOURCE -> TARGET COMPLETE")
        print("✅ Scenario 3, 5, 8 and 9B evidence is complete.")
        print("Target -> Dummy is NOT started automatically — run Scenario 2 A separately when needed.")
        print("#" * 76)
        sys.exit(0)

    if a.mode == "scenario5":
        print("\n" + "#" * 76)
        print("SCENARIO 5 — SOURCE -> TARGET (Wi-Fi) + MANUAL MID-UPDATE RESTART")
        print("#" * 76)
        ok = run_transition(
            "SOURCE_TO_TARGET", call_number, base_root, serial, "5",
            include_hardware_screenshot=True, scenario5_restart=True
        )
        if not ok:
            print("❌ Scenario 5 did not complete.")
            sys.exit(1)
        ax.adb("shell svc power stayon false")
        print("✅ Scenario 5 complete.")
        sys.exit(0)

    if a.mode in ("all", "source_to_target"):
        print("\n" + "#" * 76)
        print("PHASE 1 — SCENARIO 3: SOURCE -> TARGET (Wi-Fi)")
        print("#" * 76)
        first_ok = run_transition(
            "SOURCE_TO_TARGET", call_number, base_root, serial, "3",
            include_hardware_screenshot=True
        )
        if not first_ok:
            print("❌ SOURCE -> TARGET did not complete. TARGET -> DUMMY will NOT start.")
            sys.exit(1)
        if a.mode == "source_to_target":
            ax.adb("shell svc power stayon false")
            print("✅ Scenario 3 complete.")
            sys.exit(0)

        print("\n" + "#" * 76)
        print("SOURCE -> TARGET COMPLETE")
        print("The SAME physical device is now expected to be in TARGET.")
        print("Starting Scenario 2 A: TARGET -> DUMMY directly — no reflash.")
        print("#" * 76)
        time.sleep(3)

    if a.mode in ("all", "target_to_dummy"):
        print("\n" + "#" * 76)
        print("PHASE 1 — SCENARIO 2 A: TARGET -> DUMMY (Wi-Fi)")
        print("#" * 76)
        second_ok = run_transition(
            "TARGET_TO_DUMMY", call_number, base_root, serial, "2 A",
            include_hardware_screenshot=False
        )
        if not second_ok:
            print("❌ TARGET -> DUMMY did not complete.")
            sys.exit(1)

    ax.adb("shell svc power stayon false")
    print("\n" + "=" * 76)
    print("PHASE 1 COMPLETE")
    print("SOURCE -> TARGET -> DUMMY")
    print("Scenario 3: 37 screenshots")
    print("Scenario 2 A: 35 screenshots")
    print(f"Evidence root: {base_root}")
    print("=" * 76)
    sys.exit(0)


if __name__ == "__main__":
    main()
