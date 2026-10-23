#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scenario 9A — Target -> Corrupt (Wi-Fi).

Dedicated evidence flow:
  1. About phone, Device details, Model, Android version, IMEI 1/2 if present,
     and Device identifiers (no Phone-info screens).
  2. System update: capture the "Update now / Update later" page, tap Update now,
     capture "Updating...", then wait for and capture "Package verification failed".
The failure dialog is captured first. After saving that critical screenshot, the
dialog is dismissed so the same device-information evidence can be captured POST.
"""
import argparse
import os
import re
import sys
import time

import tracfone_wifi_backend as W
import Axon as ax


def _wifi_evidence(label):
    """Use the existing Source -> Target Wi-Fi screenshot cycle."""
    W.wifi_snapshot(label)


def capture_about_phone_through_identifiers(tag="PRE"):
    """Capture the same About phone evidence before or after the update attempt."""
    print("\n⚙️  ━━━━━━ Scenario 9A: Settings / About phone ━━━━━━")
    if not W.ensure_wifi_connected(timeout=45):
        for name in ("About phone", "Device details", "Model", "Android version",
                     "IMEI1", "IMEI2", "Device identifiers"):
            W.mark_missing(f"{tag} - {name}", "(Wi-Fi could not connect)")
        return False

    W.open_settings_clean()
    if not ax.open_about_phone():
        for name in ("About phone", "Device details", "Model", "Android version",
                     "IMEI1", "IMEI2", "Device identifiers"):
            W.mark_missing(f"{tag} - {name}", "(About phone not found)")
        return False

    _wifi_evidence(f"{tag} - About phone")
    W.disable_wifi_for_navigation()

    # Same swipe-first pattern as the working Device identifiers navigation.
    print("⏳ Waiting 2s before scrolling to Device details...")
    time.sleep(2)
    print("↕️ Scrolling About phone down to Device details...")
    W.nitro_swipe_up()
    time.sleep(2)
    if not ax.find_and_tap_any(W.K["device_details"]):
        for name in ("Device details", "Model", "Android version", "IMEI1", "IMEI2"):
            W.mark_missing(f"{tag} - {name}", "(Device details not found after swipe)")
        W.mark_missing(f"{tag} - Device identifiers", "(Device details not found)")
        ax.adb("shell input keyevent 3")
        return False

    time.sleep(2)
    _wifi_evidence(f"{tag} - Device details")

    if ax.find_and_tap_any(W.K["model"]):
        time.sleep(2)
        _wifi_evidence(f"{tag} - Model")
        ax.go_back()
        W.disable_wifi_for_navigation()
    else:
        W.mark_missing(f"{tag} - Model")

    if ax.find_and_tap_any(W.K["android_ver"]):
        time.sleep(2)
        W.dismiss_preupdate_heads_up()
        _wifi_evidence(f"{tag} - Android version")
        ax.go_back()
        W.disable_wifi_for_navigation()
    else:
        W.mark_missing(f"{tag} - Android version")

    # On the Device details page, scroll to the IMEI rows just as the shared flow does.
    ax.swipe_up()
    time.sleep(2)
    imei1 = ax.find_and_tap_next_imei()
    if imei1:
        time.sleep(2)
        _wifi_evidence(f"{tag} - IMEI1 dialog")
        ax.go_back()
        W.disable_wifi_for_navigation()

        imei2 = ax.find_and_tap_next_imei(last_text=imei1)
        if imei2:
            time.sleep(2)
            _wifi_evidence(f"{tag} - IMEI2 dialog")
            ax.go_back(times=2)
            time.sleep(2)
            W.disable_wifi_for_navigation()
        else:
            W.mark_missing(f"{tag} - IMEI2 dialog", "(no 2nd IMEI on screen)")
            ax.go_back()
    else:
        W.mark_missing(f"{tag} - IMEI1 dialog")
        W.mark_missing(f"{tag} - IMEI2 dialog")
        ax.go_back()

    # Back on About phone: match the established identifiers swipe sequence.
    print("⏳ Waiting 2s before scrolling to Device identifiers...")
    time.sleep(2)
    print("↕️ Scrolling About phone down to Device identifiers...")
    ax.swipe_up()
    time.sleep(2)
    if ax.find_and_tap_any(W.K["device_ids"]):
        time.sleep(2)
        _wifi_evidence(f"{tag} - Device identifiers")
    else:
        W.mark_missing(f"{tag} - Device identifiers")

    ax.adb("shell input keyevent 3")
    return True


def _ui_text():
    """Return normalized UI hierarchy text; empty means screen state is unknown."""
    try:
        return ax.normalize_text(ax.dump_ui()).lower()
    except Exception as exc:
        print(f"⚠️ UI hierarchy read failed while waiting for update screen: {exc}")
        return ""


_VERIFY_NEEDLES = ["package verification failed",
                   "not compatible with the current software version"]


def _wait_for_text(needles, timeout, label, abort_if=None):
    """Poll frequently so short-lived update states are captured as soon as visible.

    abort_if: optional list of texts; if one of them is already on screen the wait stops
    at once (returns False) - used so OPTIONAL screens never delay the compulsory
    'Package verification failed' capture."""
    if isinstance(needles, str):
        needles = [needles]
    deadline = time.time() + timeout
    last_log = 0
    while time.time() < deadline:
        text = _ui_text()
        if any(needle.lower() in text for needle in needles):
            print(f"✅ Detected {label}.")
            return True
        if abort_if and any(a.lower() in text for a in abort_if):
            print(f"ℹ️ Package-verification dialog is already showing; not waiting for {label}.")
            return False
        if time.time() - last_log >= 10:
            print(f"⏳ Waiting for {label}...")
            last_log = time.time()
        W.wake()
        time.sleep(1)
    return False


def _handle_update_screens(timeout=120):
    """Handle the OPTIONAL update screens in ONE polling loop, whichever order they show up in.

    Depending on the device/OS build the flow can be (any of these, in any order):
      * 'Update now / Update later' page  -> screenshot, tap Update now
      * 'See what's new' page with OK     -> screenshot, tap OK
      * 'Updating...' page                -> screenshot
    The loop stops as soon as the compulsory 'Package verification failed' dialog is on screen
    (the caller captures that). Screens that never appear are only recorded as missing - this
    function never fails the scenario."""
    done = {"choice": False, "info": False, "updating": False}
    choice_taps, last_choice_tap = 0, 0.0
    deadline = time.time() + timeout
    last_log = 0
    while time.time() < deadline:
        text = _ui_text()
        if any(n in text for n in _VERIFY_NEEDLES):
            print("✅ Package-verification dialog is showing - optional update screens are finished.")
            break

        on_choice_page = "update now" in text and "update later" in text
        if on_choice_page and not done["choice"]:
            done["choice"] = True
            print("✅ Detected 'Update now / Update later' page.")
            time.sleep(0.5)
            if W.snap("9A - Update now screen") is None:
                print("⚠️ Could not capture the optional Update now screen; continuing.")
                W.mark_missing("9A - Update now screen",
                               "(choice page was detected but screenshot capture failed)")
            if W._tap_exact_ui_text("Update now") or ax.find_and_tap_any(["Update now"]):
                print("✅ Tapped Update now; waiting for the next screen...")
                choice_taps, last_choice_tap = 1, time.time()
            else:
                print("⚠️ Update now button was not tappable; will keep trying while the page is shown.")
                W.mark_missing("9A - Update now action", "(Update now button was not tappable)")
            time.sleep(1)
            continue

        if on_choice_page and done["choice"] and choice_taps < 4 and time.time() - last_choice_tap >= 4:
            # The page is still there after the tap: tap again (stronger tap if the shared helper has it).
            choice_taps += 1
            last_choice_tap = time.time()
            print(f"⚠️ Update now page still showing - tapping again ({choice_taps}/4).")
            force = getattr(W, "_force_tap_update_now", None)
            if not ((force and force()) or W._tap_exact_ui_text("Update now")
                    or ax.find_and_tap_any(["Update now"])):
                print("⚠️ Retry tap found no Update now button.")
            time.sleep(1)
            continue

        info_visible = ("see what's new" in text or "what's new" in text) and "update now" not in text
        if info_visible and not done["info"]:
            done["info"] = True
            print("✅ Detected 'See what's new' information page.")
            time.sleep(0.4)
            if W.snap("9A - See what's new") is None:
                print("⚠️ Could not capture the optional See what's new page; still attempting OK.")
                W.mark_missing("9A - See what's new", "(screenshot capture failed)")

            ok_tapped = W._tap_exact_ui_text("OK") or ax.find_and_tap_any(["OK"])
            if not ok_tapped:
                print("⚠️ OK button was not found by text; trying a safe center-lower screen tap.")
                ax.adb("shell input tap 420 1690")
            dismissed = False
            end = time.time() + 20
            while time.time() < end:
                time.sleep(1)
                current = _ui_text().lower()
                if (("see what's new" not in current and "what's new" not in current)
                        or "update now" in current
                        or any(n in current for n in _VERIFY_NEEDLES)):
                    dismissed = True
                    break
            if not dismissed:
                print("⚠️ See what's new page is still visible; retrying OK once.")
                if not (W._tap_exact_ui_text("OK") or ax.find_and_tap_any(["OK"])):
                    ax.adb("shell input tap 420 1690")
                end = time.time() + 15
                while time.time() < end:
                    time.sleep(1)
                    current = _ui_text().lower()
                    if (("see what's new" not in current and "what's new" not in current)
                            or "update now" in current
                            or any(n in current for n in _VERIFY_NEEDLES)):
                        dismissed = True
                        break
            if not dismissed:
                print("❌ The See what's new page did not dismiss after OK; waiting for the next expected screen anyway.")
                W.mark_missing("9A - See what's new OK", "(page did not visibly dismiss after OK attempts)")
            continue

        if "updating" in text and not done["updating"]:
            done["updating"] = True
            print("✅ Detected 'Updating...' page.")
            time.sleep(0.5)
            if W.snap("9A - Updating") is None:
                print("⚠️ Could not capture the Updating screen; continuing to wait for package verification.")
                W.mark_missing("9A - Updating", "(screenshot capture failed)")
            continue

        if time.time() - last_log >= 10:
            print("⏳ Waiting for the next update screen...")
            last_log = time.time()
        W.wake()
        time.sleep(1)

    if not done["choice"]:
        print("⚠️ Update now / Update later page did not appear; recording it as missing and continuing.")
        W.mark_missing("9A - Update now screen",
                       "(optional choice page did not appear after Check for updates)")
    if not done["updating"]:
        W.mark_missing("9A - Updating",
                       "(Updating page was not detected before waiting for verification failure)")


def _tap_check_for_updates():
    """Tap Check for updates unless the update-choice page is already displayed."""
    current = _ui_text()
    if "update now" in current and "update later" in current:
        print("ℹ️ Update now / Update later is already visible; no extra check tap needed.")
        return True
    labels = W.K["check_update"]
    print("🔎 Looking for 'Check for updates' on System update...")
    if ax.find_and_tap_any(labels):
        print("✅ Tapped Check for updates.")
        return True
    print("↕️ Check for updates not immediately visible; scrolling to find it...")
    if ax.scroll_and_tap(labels, max_scrolls=3):
        print("✅ Tapped Check for updates after scrolling.")
        return True
    print("❌ Could not find/tap Check for updates.")
    return False


def run(save_root, serial):
    W.reset_screenshot_bookkeeping()
    W.SCENARIO_CODE = "9A"
    W.PHASE_NAME = "Scenario_9A_TARGET_TO_CORRUPT_WIFI"
    W.EXPECTED_TOTAL = 0
    W.INCLUDE_HARDWARE_SCREENSHOT = False
    ax.TARGET_SERIAL = serial
    ax.KEYWORDS = W.K
    W.ax.TARGET_SERIAL = serial

    folder = os.path.join(save_root, "Scenario 9A Target to Corrupt WiFi")
    os.makedirs(folder, exist_ok=True)
    W.SAVE_DIR = folder
    ax.SAVE_ROOT = folder

    state = ax.adb("get-state")
    if state.returncode != 0 or state.stdout.strip() != "device":
        print(f"❌ Device {serial} is not reachable via ADB.")
        return False

    ax.adb("shell svc power stayon true")
    try:
        if not capture_about_phone_through_identifiers():
            print("⚠️ About phone evidence had missing items; continuing with the requested update flow.")

        print("\n🛰️  ━━━━━━ Scenario 9A: System update / corrupt package ━━━━━━")
        ax.adb("shell svc wifi disable")
        time.sleep(2)
        W.open_settings_clean()
        if not W.open_update_page():
            print("❌ System update page could not be opened.")
            return False

        if not W.ensure_wifi_connected(timeout=45):
            print("❌ Wi-Fi could not reconnect on System update page.")
            return False

        # Do not call W.check_for_update_then_update_now(): it taps Update now
        # before we can capture the required intermediate screen.
        if not _tap_check_for_updates():
            # The update check can start by itself once Wi-Fi is back ("checking if device
            # memory is sufficient"), so there is no Check for updates row to tap. That is
            # not a failure: keep going and capture whichever update screens appear.
            print("ℹ️ Check for updates row not available - the update flow may already be running; continuing.")

        # Update now / OK ("See what's new") / Updating are OPTIONAL and device/OS dependent:
        # capture each one that appears, in whatever order, and never fail here.
        _handle_update_screens()

        # Critical evidence: wait for the real verification-failure dialog and
        # capture it immediately. Do not dismiss OK or navigate away.
        failed = _wait_for_text(
            ["package verification failed",
             "not compatible with the current software version"],
            timeout=180,
            label="'Package verification failed' dialog")
        if not failed:
            print("❌ Package verification failed did not appear within 180 seconds; no substitute screenshot taken.")
            W.mark_missing("9A - Package verification failed",
                           "(dialog not detected within 180 seconds)")
            return False

        time.sleep(0.25)
        if W.snap("9A - Package verification failed") is None:
            print("❌ Could not capture Package verification failed.")
            return False

        # The failure screen has now been captured as required. Continue with
        # the requested POST device-information evidence; dismiss only after
        # the critical screenshot has been saved.
        print("\n📱 Package-verification evidence saved. Starting POST About phone evidence...")
        if W._tap_exact_ui_text("OK"):
            time.sleep(2)
        else:
            print("ℹ️ Failure dialog OK button not found by text; using Android Back after evidence capture.")
            ax.go_back()
            time.sleep(2)

        post_ok = capture_about_phone_through_identifiers(tag="POST")
        if not post_ok:
            print("⚠️ POST About phone evidence had missing items; the failure screenshot was already captured.")

        print("\n✅ Scenario 9A evidence capture complete.")
        print(f"Evidence folder: {os.path.abspath(folder)}")
        print(f"Captured screenshots: {W.shot_no - 1}")
        return True
    finally:
        ax.adb("shell svc power stayon false")


def main():
    parser = argparse.ArgumentParser(
        description="Scenario 9A: Target -> Corrupt over Wi-Fi")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--call-number", required=True)
    parser.add_argument("--restart-img", default=None)
    parser.add_argument("--save-root", default=None)
    parser.add_argument("--mode", choices=["target_to_corrupt_wifi_9a"],
                        default="target_to_corrupt_wifi_9a")
    args = parser.parse_args()

    number = args.call_number.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?\d{5,15}", number):
        sys.exit("Phone number must be digits only (optional leading +), 5-15 digits")

    results = os.path.abspath(args.save_root or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results"))
    os.makedirs(results, exist_ok=True)
    results = W.device_results_root(results, args.serial)

    print("SCENARIO 9A — TARGET -> CORRUPT • WI-FI")
    if not run(results, args.serial):
        sys.exit("Scenario 9A did not complete.")
    print("Scenario 9A complete.")


if __name__ == "__main__":
    main()
