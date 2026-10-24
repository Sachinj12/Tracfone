#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tracfone — Scenario 4: SOURCE -> TARGET over DATA   (separate phase)
====================================================================

Same flow as Scenario 3 (36 screenshots) + notification shade @4 / @6 min (38), but every screenshot is taken with
DATA only — the Wi-Fi symbol must not be visible in any of them.

This file is deliberately SEPARATE from tracfone_wifi_backend.py.  It imports
that module only to re-use its screenshot / navigation functions, and changes
the Wi-Fi handling of those functions INSIDE THIS PROCESS ONLY.  Nothing in
tracfone_wifi_backend.py is edited, so the Wi-Fi scenarios (2 A, 3, 5, 8, 9B)
cannot be affected by this file.

Wi-Fi / data rules
  1-11   PRE-update shots   : Wi-Fi OFF, mobile data ON for the whole section.
  12-22  OTA update         : the update itself runs on Wi-Fi (the SIM has no
                              data plan), but EVERY screenshot is taken with
                              Wi-Fi switched OFF for a moment (data only) and
                              Wi-Fi is switched back ON right after it.
                              Checkpoints: start, Background installation (notification
                              shade), +1 (2 shots), +2 Quick Settings, +4, +6, +8, then
                              Restart now / Restarting / Update complete.
  23-33  POST-update shots  : Wi-Fi OFF, mobile data ON (after the update the
                              script first waits for the mobile network to settle).
  34-36  Call + Amazon      : Wi-Fi may be ON while the page loads / the call
                              is being placed, but each screenshot is taken
                              with Wi-Fi OFF (data only).
Evidence:
  Scenario 4: results/Tracfone Scenarios/<device serial>/Scenario 4 Source to Target Data/4 (n).png
  Scenario 2B: results/Tracfone Scenarios/<device serial>/Scenario 2B Target to Dummy Data/2B (n).png
"""
import argparse
import os
import re
import sys
import time

import tracfone_wifi_backend as W      # re-used helpers; its main() is guarded by __name__

ax = W.ax

SCENARIO_CODE = "4"
EXPECTED_TOTAL = 38                      # Scenario 3 flow incl. the Background installation shot + notification shade @4 / @6 min
PHASE_NAME = "Scenario_4_DATA_SOURCE_TO_TARGET"
FOLDER_NAME = "Scenario 4 Source to Target Data"
TARGET_DUMMY_SCENARIO_CODE = "2B"
TARGET_DUMMY_EXPECTED_TOTAL = 35
TARGET_DUMMY_PHASE_NAME = "Scenario_2B_DATA_TARGET_TO_DUMMY"
TARGET_DUMMY_FOLDER_NAME = "Scenario 2B Target to Dummy Data"

CALL_LABEL_CALLING = "Call (data only) - Calling..."
CALL_LABEL_CONNECTED = "Call (data only) - connected (timer >= 00:05)"
CHROME_LABEL = "Chrome (not signed in) - amazon.com (screenshot with data only)"

# Phase switches (read by the replacement functions below)
_STATE = {
    "capture_wifi_off": False,   # True: every screenshot is taken with Wi-Fi OFF, Wi-Fi restored after it
    "wifi_for_update": False,    # True: ensure_wifi_connected() really turns Wi-Fi ON (OTA / loading)
}
_ORIG = {}                       # original functions of tracfone_wifi_backend, saved by install()


# ===========================================================================
# Wi-Fi / data primitives
# ===========================================================================
def _wifi_enabled():
    out = (ax.adb("shell cmd wifi status").stdout or "").lower()
    # Newer builds print a 2nd line, "Wifi scanning is only available when wifi is enabled",
    # even while Wi-Fi is OFF - so when the FIRST status line is clear, it alone decides.
    first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    if "wifi is disabled" in first:
        return False
    if "wifi is enabled" in first:
        return True
    if "wifi is enabled" in out:
        return True
    if "wifi is disabled" in out:
        return False
    val = (ax.adb("shell settings get global wifi_on").stdout or "").strip()
    return val == "1"


def _set_wifi_off(settle=2.0, timeout=10):
    """Wi-Fi OFF and verified; short settle so the status-bar icon is gone."""
    ax.adb("shell svc wifi disable")
    end = time.time() + timeout
    while time.time() < end:
        if not _wifi_enabled():
            break
        time.sleep(0.5)
    time.sleep(settle)
    return not _wifi_enabled()


def _data_on():
    ax.adb("shell svc data enable")
    val = (ax.adb("shell settings get global mobile_data").stdout or "").strip()
    if val not in ("1", ""):
        print(f"⚠️ Mobile data setting reads '{val}' (expected 1) — check the SIM / data toggle.")
    return True


def s4_data_only(timeout=45):
    """Data scenario: Wi-Fi OFF + mobile data ON (replaces 'ensure Wi-Fi connected')."""
    print("📶 Data scenario — Wi-Fi OFF, mobile data ON...")
    _data_on()
    if not _set_wifi_off(settle=1.0):
        print("❌ Could not switch Wi-Fi OFF — screenshots would show the Wi-Fi symbol.")
        return False
    print("✅ Data only: Wi-Fi is OFF, mobile data is ON.")
    return True


# ===========================================================================
# Replacement functions (installed into tracfone_wifi_backend for THIS process)
# ===========================================================================
def s4_ensure_wifi_connected(timeout=45):
    """Router: real Wi-Fi connect only when this phase needs Wi-Fi (the update / page loading)."""
    if _STATE["wifi_for_update"]:
        return _ORIG["ensure_wifi_connected"](timeout)
    return s4_data_only(timeout)


def s4_disable_wifi_for_navigation():
    print("📴 Data scenario — Wi-Fi stays OFF while navigating...")
    ax.adb("shell svc wifi disable")
    time.sleep(1.5)


def s4_wifi_snapshot(label):
    """Original pre-update shots cycle Wi-Fi ON for each shot — here Wi-Fi is never switched on."""
    return W.snap(label) is not None


def s4_wifi_stitched_snapshot(label):
    img, ok = W.stitched_capture()
    if img is None:
        W.mark_missing(label, "(screencap failed)")
        return False
    W.save_image(img, label, "OK" if ok else "OK (may be incomplete)")
    return True


def s4_grab_frame(retries=3):
    """The single choke point of every screenshot in this scenario.

    When the phase needs Wi-Fi in between screenshots (update / page loading),
    Wi-Fi is switched OFF just for the capture and ON again afterwards."""
    if not _STATE["capture_wifi_off"]:
        return _ORIG["grab_frame"](retries)
    was_on = _wifi_enabled()
    if was_on:
        print("📴 Wi-Fi OFF for this screenshot (data only)...")
        _set_wifi_off(settle=2.0)
    try:
        return _ORIG["grab_frame"](retries)
    finally:
        if was_on:
            print("📶 Wi-Fi back ON (update / page keeps running on Wi-Fi).")
            ax.adb("shell svc wifi enable")


def s4_shot_quick_settings(label, mobile_data_off=False):
    """Data scenario: Quick Settings shows DATA ON with Wi-Fi OFF (not the 'Wi-Fi only' variant)."""
    _data_on()
    return _ORIG["shot_quick_settings"](label.replace("(Wi-Fi only)", "(Data only)"),
                                        mobile_data_off=False)


_PATCHES = {
    "ensure_wifi_connected": s4_ensure_wifi_connected,
    "disable_wifi_for_navigation": s4_disable_wifi_for_navigation,
    "wifi_snapshot": s4_wifi_snapshot,
    "wifi_stitched_snapshot": s4_wifi_stitched_snapshot,
    "grab_frame": s4_grab_frame,
    "shot_quick_settings": s4_shot_quick_settings,
}


def install():
    for name, fn in _PATCHES.items():
        _ORIG[name] = getattr(W, name)
        setattr(W, name, fn)
    _ORIG["CHROME_LABEL"] = W.CHROME_LABEL
    W.CHROME_LABEL = CHROME_LABEL


def uninstall():
    for name, fn in _ORIG.items():
        setattr(W, name, fn)
    _ORIG.clear()


# ===========================================================================
# Mobile network after the update
# ===========================================================================
_NET_2G = ("EDGE", "GPRS", "GSM", "1XRTT", "CDMA", "IDEN", "UNKNOWN")   # 2G-class / not registered


def _network_types():
    out = (ax.adb("shell getprop gsm.network.type").stdout or "").strip()
    return [t.strip().upper() for t in out.split(",") if t.strip()]


def _network_ok(types):
    """True = a real data network, False = only 2G / not registered, None = cannot tell."""
    if not types:
        return None
    return any(t not in _NET_2G for t in types)


def s4_wait_for_data_network(max_wait=120, toggle_after=60):
    """After the update/reboot the phone can sit on 'E' (2G) for a while.  Wait (never fail) for
    the data network to settle; once, nudge it by switching mobile data OFF/ON."""
    _data_on()
    types = _network_types()
    ok = _network_ok(types)
    if ok is None:
        print("ℹ️ Mobile network type is not readable on this phone — continuing.")
        return True
    if ok:
        print(f"✅ Mobile network is up: {','.join(types)}")
        return True
    print(f"⏳ Mobile network shows {','.join(types)} (2G / not registered) — waiting up to "
          f"{max_wait}s for it to settle before the next screenshots...")
    start = time.time()
    toggled = False
    while time.time() - start < max_wait:
        time.sleep(5)
        types = _network_types()
        if _network_ok(types):
            print(f"✅ Mobile network settled: {','.join(types)}")
            return True
        if toggle_after and not toggled and time.time() - start >= toggle_after:
            print("🔁 Still 2G — switching mobile data OFF/ON once to force a fresh registration...")
            ax.adb("shell svc data disable")
            time.sleep(3)
            ax.adb("shell svc data enable")
            toggled = True
    print(f"⚠️ Mobile network still {','.join(types) or 'unknown'} after {max_wait}s — continuing; "
          "the screenshots will show what the phone reports.")
    return False


# ===========================================================================
# Shots 12-21 : OTA update (Scenario 4 has its own checkpoints, incl. +8 min)
# ===========================================================================
def s4_ota_phase():
    """Same logic as the Source -> Target update flow of tracfone_wifi_backend.ota_phase
    (no notification drop-down shots), with one extra checkpoint at +8 minutes.

    Shots: start #12, Background installation (notification shade) #13, +1 min #14/#15,
    +2 min Quick Settings #16, +4 #17, +6 #18, +8 #19, Please restart now #20,
    Restarting #21, Update complete #22 — no empty slots."""
    print("\n🛰️  ━━━━━━ OTA UPDATE over Wi-Fi (screenshots with data only) ━━━━━━")
    print("📴 Wi-Fi OFF while opening System update page...")
    ax.adb("shell svc wifi disable")
    time.sleep(2)

    W.open_settings_clean()
    if not W.open_update_page():
        print("❌ System update page not found - OTA stopped.")
        return False

    print("📶 System update page is open — enabling Wi-Fi now (the update runs on Wi-Fi)...")
    if not W.ensure_wifi_connected(timeout=45):
        print("❌ Wi-Fi could not reconnect on System update page - OTA stopped.")
        return False

    W.check_for_update_then_update_now()

    time.sleep(1)
    W.shot_page("System update - started (Verifying)")                     # 12
    t0 = time.time()                       # the checkpoint schedule starts here (as before)
    # "Background installation": waits until the notification exists, scrolls the shade, expands it
    # and takes the screenshot - always exactly one shot (taken with Wi-Fi OFF = data only).
    W.shot_background_installation()                                       # 13

    # Fast-update helper from tracfone_wifi_backend (absent in an older wifi file -> old behaviour).
    _fast_cls = getattr(W, "FastUpdateWatch", None)
    _fast_watch = _fast_cls() if _fast_cls else None

    def wait_to(target_seconds, allow_fast=False):
        """Wait until a checkpoint while watching for the restart page.
        allow_fast (+4/+6/+8 min only): return False EARLY when the update will clearly finish
        before the checkpoint, so its screenshot is taken now instead of being lost."""
        target = t0 + target_seconds
        while time.time() < target:
            W.wake()
            if W.restart_visible():
                print("🛑 'Restart now' appeared early — stopping scheduled update screenshots.")
                return True
            if allow_fast and _fast_watch and _fast_watch.fast_now(target - time.time()):
                print("⚡ Fast update detected - taking this checkpoint's screenshot now.")
                return False
            time.sleep(min(5, max(0.5, target - time.time())))
        return W.restart_visible()

    print("⏳ Waiting for the +1 min checkpoint...")
    if not wait_to(60):
        if not W.restart_visible():
            W.shot_page("System update page @1 min")                       # 14
        if W.restart_visible():
            print("🛑 Restart page appeared after #14 — stopping update screenshots.")
        else:
            W.shot_page("System update page @1 min (2nd)")                 # 15

    if W.restart_visible():
        print("🛑 Restart page detected — no more scheduled update screenshots will be taken.")
    else:
        print("⏳ Waiting for the +2 min checkpoint...")
        if not wait_to(120):
            if not W.restart_visible():
                W.shot_quick_settings("Quick Settings expanded (Data only) @2 min")   # 16

        for minutes in (4, 6, 8):                                          # 17, 18, 19
            if W.restart_visible():
                break
            print(f"⏳ Waiting for the +{minutes} min checkpoint...")
            if not wait_to(minutes * 60, True):
                if W.restart_visible():
                    print(f"🛑 Restart page detected at +{minutes} min — stopping update screenshots.")
                else:
                    W.shot_page(f"System update page @{minutes} min")
                    if minutes in (4, 6) and not W.restart_visible():
                        W.shot_shade(f"Notification shade @{minutes} min")

    # Restart page is always #22, even if it appeared before a scheduled checkpoint
    # (the skipped checkpoints stay reserved as missing so the numbers never shift).
    # Timed update-progress slots that were skipped (fast update) are SKIPPED, not failures;
    # the +2 min Quick Settings slot stays mandatory. (Older wifi file -> plain pad_to as before.)
    getattr(W, "pad_ota_slots", W.pad_to)(22, "Restart now appeared before this scheduled update checkpoint")
    print("👀 Waiting for 'Restart now' after the scheduled update screenshots...")
    if not W.handle_restart(already_visible=W.restart_visible()):          # 22, 23
        return False

    ax.wait_for_device()
    ax.unlock_device()
    if not W.wait_update_complete_notification():                          # 22
        return False
    W.go_home()
    ax.clear_recent_apps()
    return True


# ===========================================================================
# Shots 33-35 : call + Chrome/Amazon (screenshots with data only)
# ===========================================================================
def _call_attempt(number, wifi_first):
    """One dial attempt.  Returns dict(status, img_calling, img_connected).

    status: 'answered' | 'no_answer' | 'ended' | 'not_started'
    wifi_first=False : Wi-Fi OFF the whole time (call over the SIM).
    wifi_first=True  : Wi-Fi ON while the call is set up; Wi-Fi OFF for the screenshots."""
    enc = number.replace("+", "%2B")
    W.go_home()
    if wifi_first:
        _STATE["capture_wifi_off"] = True
        if not _ORIG["ensure_wifi_connected"](45):
            return {"status": "not_started", "img_calling": None, "img_connected": None,
                    "why": "Wi-Fi not connected"}
    else:
        _STATE["capture_wifi_off"] = False
        if not s4_data_only():
            return {"status": "not_started", "img_calling": None, "img_connected": None,
                    "why": "could not switch Wi-Fi off"}

    base = W._telecom_states()
    use_telecom = True
    if base and "ACTIVE" in base:
        print("⚠️ Telecom already shows an ACTIVE call before dialing - ignoring Telecom signal.")
        use_telecom = False

    print(f"📞 Dialing {number} ...")
    res = W._bsh(f"am start -a android.intent.action.CALL -d tel:{enc}")
    if "Error" in res or "Denial" in res or "Exception" in res:
        print("⚠️ Dial command output: " + res.strip()[:200])
    time.sleep(2)
    img_calling = W.grab_frame()

    start = time.time()
    active_since = None
    live_seen = False
    no_call_polls = 0
    answered = False
    status = "no_answer"
    ui_blocked = False
    ui_fail = 0
    last_print = [None, 0.0]

    while True:
        elapsed = time.time() - start
        if elapsed >= W.ANSWER_WINDOW and active_since is None:
            break
        if elapsed >= W.ANSWER_WINDOW + 20:
            break

        st = W._telecom_states() if use_telecom else None
        timer = None
        if (st is None or (not live_seen and elapsed > 8)) and not ui_blocked:
            root = W._fresh_dump(retries=1)
            if root is None:
                ui_fail += 1
                if ui_fail >= 2:
                    ui_blocked = True
            else:
                timer = W._ui_call_timer(root)

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

        if timer is not None and timer >= W.MIN_TIMER_SECS:
            answered = True
            print(f"✅ Call ANSWERED (on-screen timer {timer}s)")
            break
        if active_since and time.time() - active_since >= W.MIN_TIMER_SECS:
            answered = True
            print("✅ Call ANSWERED (Telecom ACTIVE for 5 s)")
            break
        if no_call_polls >= 3:
            # Disconnected almost immediately and never answered = the call never really started.
            status = "not_started" if elapsed < 6 else "ended"
            break
        if use_telecom and st == [] and not live_seen and elapsed >= 10:
            status = "not_started"
            break
        time.sleep(0.3)

    img_connected = None
    if answered:
        status = "answered"
        img_connected = W.grab_frame()
        if wifi_first and use_telecom:
            # Switching Wi-Fi off can drop a Wi-Fi call; never keep a misleading screenshot.
            st_after = W._telecom_states()
            if st_after is not None and "ACTIVE" not in st_after:
                print("⚠️ The call dropped when Wi-Fi was switched off — connected screenshot discarded.")
                img_connected = None
    W._cc_end_call()
    W.go_home()
    return {"status": status, "img_calling": img_calling, "img_connected": img_connected}


def s4_call(number, sink):
    """Calling + connected screenshots, both with data only.

    1st try: Wi-Fi OFF the whole time (call over the SIM).
    Only if that call never starts: 2nd try with Wi-Fi ON until connected, Wi-Fi OFF for the shots."""
    commit, missing = sink
    print("\n📞 ━━━━━━ Call (data only) ━━━━━━")
    res = None
    for idx, (name, wifi_first) in enumerate((("Wi-Fi OFF, call over the SIM", False),
                                              ("Wi-Fi ON until connected, OFF for the screenshots", True)), 1):
        print(f"📞 Call attempt {idx}: {name}")
        res = _call_attempt(number, wifi_first)
        if res["status"] != "not_started":
            break
        print(f"⚠️ Call attempt {idx} did not start ({res.get('why', 'no call registered')}).")

    if res["img_calling"] is not None:
        commit(res["img_calling"], CALL_LABEL_CALLING)
    else:
        missing(CALL_LABEL_CALLING, "(screencap failed / call not started)")
    if res["img_connected"] is not None:
        commit(res["img_connected"], CALL_LABEL_CONNECTED)
    else:
        reason = {"no_answer": "no answer within 30 seconds",
                  "ended": "call ended / declined before it was answered",
                  "not_started": "call could not be started",
                  "answered": "call answered but the screenshot could not be kept"}[res["status"]]
        print(f"❌ Connected screenshot is missing: {reason}.")
        missing(CALL_LABEL_CONNECTED, f"({reason})")
    return res["img_connected"] is not None


def s4_chrome(sink):
    """Amazon: Wi-Fi ON until the page has landed, screenshot with Wi-Fi OFF."""
    commit, missing = sink
    print("\n🌐 ━━━━━━ Chrome (no sign-in) -> amazon.com ━━━━━━")
    _STATE["capture_wifi_off"] = True
    _STATE["wifi_for_update"] = True
    try:
        if not _ORIG["ensure_wifi_connected"](45):
            print("❌ Wi-Fi is needed to load Amazon — screenshot marked missing.")
            missing(CHROME_LABEL, "(Wi-Fi not connected)")
            return False
        return W._cc_chrome(sink)
    finally:
        _STATE["wifi_for_update"] = False


def s4_call_and_browse(number):
    sink = (lambda img, label: W.save_image(img, label), W.mark_missing)
    ok_call = s4_call(number, sink)
    ok_chrome = s4_chrome(sink)
    return ok_call and ok_chrome


# ===========================================================================
# The Scenario 4 run
# ===========================================================================
def run_data_transition(call_number, save_root, serial):
    W.reset_screenshot_bookkeeping()
    W.SCENARIO_CODE = SCENARIO_CODE
    W.INCLUDE_HARDWARE_SCREENSHOT = True
    W.EXPECTED_TOTAL = EXPECTED_TOTAL
    W.PHASE_NAME = PHASE_NAME
    ax.PHASE = PHASE_NAME
    ax.TARGET_SERIAL = serial
    ax.SAVE_ROOT = os.path.join(save_root, FOLDER_NAME)
    os.makedirs(ax.SAVE_ROOT, exist_ok=True)

    print("\n" + "=" * 72)
    print("STARTING SCENARIO 4: SOURCE > TARGET (DATA)")
    print(f"Device: {serial}")
    print("=" * 72)

    if ax.adb("get-state").stdout.strip() != "device":
        print(f"❌ Device {serial} is not reachable via adb.")
        return False

    W.SAVE_DIR = W.prepare_save_dir()
    if W.SAVE_DIR is None:
        return False
    ax.adb("shell svc power stayon true")

    # ---- PRE-update shots (1-11): Wi-Fi OFF, data ON --------------------------
    _STATE["capture_wifi_off"] = False
    _STATE["wifi_for_update"] = False
    if not s4_data_only():
        print("❌ Could not set 'Wi-Fi OFF + data ON' before screenshot #1.")
        return False
    W.wake()
    ax.unlock_device()
    W.safe(W.device_info_set, "PRE", True, True)
    s4_disable_wifi_for_navigation()

    # ---- OTA (12-22): update over Wi-Fi, every screenshot with Wi-Fi OFF ------
    _STATE["capture_wifi_off"] = True
    _STATE["wifi_for_update"] = True
    try:
        ota_ok = W.safe(s4_ota_phase)             # Source -> Target: no drop-down shots, +8 min added
    finally:
        _STATE["capture_wifi_off"] = False
        _STATE["wifi_for_update"] = False
    if not ota_ok:
        print("❌ OTA failed for Scenario 4.")
        return False

    # ---- POST-update shots (25-35): Wi-Fi OFF, data ON ------------------------
    W.pad_to(25)
    W.safe(s4_wait_for_data_network, 120, 60)   # after the update the phone may sit on 'E' for a while
    W.safe(W.device_info_set, "POST", False, True)

    # ---- Call + Amazon (36-38) ------------------------------------------------
    W.pad_to(36)
    W.safe(s4_wait_for_data_network, 45, 0)
    W.safe(s4_call_and_browse, call_number)
    W.pad_to(EXPECTED_TOTAL + 1)

    complete = W.summary()
    ax.adb("shell svc power stayon true")
    print("=" * 72)
    print("COMPLETED SCENARIO 4: SOURCE > TARGET (DATA)")
    print(f"Screenshots saved in: {W.SAVE_DIR}")
    print("=" * 72)
    return complete



# ===========================================================================
# Scenario 2B — Target -> Dummy over DATA (kept separate from Scenario 4)
# ===========================================================================
def run_target_dummy_data(call_number, save_root, serial):
    """Scenario 2B mirrors the exact Target -> Dummy Wi-Fi shot sequence (35 shots),
    while reusing Scenario 4's data-only screenshot capture behavior.

    The OTA timing/checkpoints and screenshot labels come from W.ota_phase(True),
    the same routine used by the Target -> Dummy Wi-Fi flow. The installed screenshot
    hooks briefly switch Wi-Fi off only while each OTA screenshot is captured.
    """
    global SCENARIO_CODE, EXPECTED_TOTAL, PHASE_NAME, FOLDER_NAME

    SCENARIO_CODE = TARGET_DUMMY_SCENARIO_CODE
    EXPECTED_TOTAL = TARGET_DUMMY_EXPECTED_TOTAL
    PHASE_NAME = TARGET_DUMMY_PHASE_NAME
    FOLDER_NAME = TARGET_DUMMY_FOLDER_NAME

    W.reset_screenshot_bookkeeping()
    W.SCENARIO_CODE = SCENARIO_CODE
    W.INCLUDE_HARDWARE_SCREENSHOT = False
    W.EXPECTED_TOTAL = EXPECTED_TOTAL
    W.PHASE_NAME = PHASE_NAME
    ax.PHASE = PHASE_NAME
    ax.TARGET_SERIAL = serial
    ax.SAVE_ROOT = os.path.join(save_root, FOLDER_NAME)
    os.makedirs(ax.SAVE_ROOT, exist_ok=True)

    print("\n" + "=" * 72)
    print("STARTING SCENARIO 2B: TARGET > DUMMY (DATA)")
    print(f"Device: {serial}")
    print("Screenshot order matches Scenario 2A; OTA screenshot capture uses data-only hooks.")
    print("=" * 72)

    if ax.adb("get-state").stdout.strip() != "device":
        print(f"❌ Device {serial} is not reachable via adb.")
        return False

    W.SAVE_DIR = W.prepare_save_dir()
    if W.SAVE_DIR is None:
        return False
    ax.adb("shell svc power stayon true")

    # PRE-update: same Target -> Dummy Wi-Fi sequence, but with Wi-Fi OFF/data ON.
    _STATE["capture_wifi_off"] = False
    _STATE["wifi_for_update"] = False
    if not s4_data_only():
        print("❌ Could not set Wi-Fi OFF + mobile data ON before screenshot #1.")
        return False
    W.wake()
    ax.unlock_device()
    W.safe(W.device_info_set, "PRE", True, False)
    s4_disable_wifi_for_navigation()

    # OTA: use the exact Target -> Dummy Wi-Fi OTA routine and its checkpoints.
    # The existing data-mode grab_frame/Quick Settings hooks ensure evidence is
    # captured with Wi-Fi hidden, while Wi-Fi can be restored between checkpoints.
    _STATE["capture_wifi_off"] = True
    _STATE["wifi_for_update"] = True
    try:
        ota_ok = W.safe(W.ota_phase, True, True)  # Target -> Dummy only: dismiss optional update dialog
    finally:
        _STATE["capture_wifi_off"] = False
        _STATE["wifi_for_update"] = False
    if not ota_ok:
        print("❌ OTA failed for Scenario 2B; post-update steps will NOT start.")
        return False

    # POST-update: same Target -> Dummy post-update sequence, data-only screenshots.
    W.pad_to(21)
    W.safe(s4_wait_for_data_network, 120, 60)
    W.safe(W.device_info_set, "POST", False, False)

    # Final call + Amazon shots mirror Scenario 2A, with Scenario 4's data-mode capture.
    W.pad_to(32)
    W.safe(s4_wait_for_data_network, 45, 0)
    W.safe(s4_call_and_browse, call_number)
    W.pad_to(EXPECTED_TOTAL + 1)

    complete = W.summary()
    ax.adb("shell svc power stayon true")
    print("=" * 72)
    print("COMPLETED SCENARIO 2B: TARGET > DUMMY (DATA)")
    print(f"Screenshots saved in: {W.SAVE_DIR}")
    print("=" * 72)
    return complete


def main():
    p = argparse.ArgumentParser(description="Tracfone data scenarios: Source -> Target (Scenario 4) and Target -> Dummy (Scenario 2B)")
    p.add_argument("--serial", required=True, help="ADB device serial")
    p.add_argument("--call-number", required=True, help="Phone number used by the call screenshots")
    p.add_argument("--restart-img", default=None, help="Path to Restart_EN.png")
    p.add_argument("--save-root", default=None, help="Root folder for evidence")
    p.add_argument("--mode", choices=["source_to_target_data", "target_to_dummy_data"], default="source_to_target_data",
                   help="Run Scenario 4 Source -> Target Data or Scenario 2B Target -> Dummy Data")
    p.add_argument("--tap-restart", action="store_true",
                   help="Tap Restart now instead of using adb reboot")
    a = p.parse_args()

    call_number = a.call_number.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?\d{5,15}", call_number):
        sys.exit("Phone number must be digits only (optional leading +), 5-15 digits")

    W.RESTART_IMG = os.path.abspath(a.restart_img or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "Restart_EN.png"))
    if not os.path.isfile(W.RESTART_IMG):
        sys.exit(f"Restart image not found: {W.RESTART_IMG}")
    W.TAP_RESTART = a.tap_restart

    base_root = os.path.abspath(a.save_root or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results"))
    os.makedirs(base_root, exist_ok=True)
    # One folder per device: results/Tracfone Scenarios/<serial>/<scenario folders of that device>
    base_root = W.device_results_root(base_root, a.serial)
    print(f"📁 Evidence for this device: {base_root}")

    print("\n" + "#" * 76)
    if a.mode == "target_to_dummy_data":
        print("SCENARIO 2B — TARGET -> DUMMY (DATA)")
    else:
        print("SCENARIO 4 — SOURCE -> TARGET (DATA)")
    print("Screenshots with data only (no Wi-Fi symbol); the update itself runs on Wi-Fi.")
    print("#" * 76)

    ax.TARGET_SERIAL = a.serial
    initial_wifi = _wifi_enabled()
    install()
    try:
        if a.mode == "target_to_dummy_data":
            ok = run_target_dummy_data(call_number, base_root, a.serial)
        else:
            ok = run_data_transition(call_number, base_root, a.serial)
    finally:
        uninstall()
        if initial_wifi:                      # leave the phone the way we found it
            ax.adb("shell svc wifi enable")
        ax.adb("shell svc power stayon false")

    if not ok:
        label = "Scenario 2B" if a.mode == "target_to_dummy_data" else "Scenario 4"
        print(f"❌ {label} did not complete.")
        sys.exit(1)
    label = "Scenario 2B" if a.mode == "target_to_dummy_data" else "Scenario 4"
    print(f"✅ {label} complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
