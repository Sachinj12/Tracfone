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


def _wait_for_text(needles, timeout, label):
    """Poll frequently so short-lived update states are captured as soon as visible."""
    if isinstance(needles, str):
        needles = [needles]
    deadline = time.time() + timeout
    last_log = 0
    while time.time() < deadline:
        text = _ui_text()
        if any(needle.lower() in text for needle in needles):
            print(f"✅ Detected {label}.")
            return True
        if time.time() - last_log >= 10:
            print(f"⏳ Waiting for {label}...")
            last_log = time.time()
        W.wake()
        time.sleep(1)
    return False


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
            return False

        if not _wait_for_text(["Update now", "Update later"], timeout=45,
                              label="'Update now / Update later' page"):
            print("❌ Update-now choice page did not appear after Check for updates.")
            return False

        time.sleep(0.5)
        if W.snap("9A - Update now screen") is None:
            print("❌ Could not capture the Update now screen.")
            return False

        if not W._tap_exact_ui_text("Update now"):
            if not ax.find_and_tap_any(["Update now"]):
                print("❌ Could not tap Update now.")
                return False
        print("✅ Tapped Update now; waiting for the Updating screen...")

        if not _wait_for_text(["updating"], timeout=30, label="'Updating...' page"):
            print("❌ The Updating screen was not detected.")
            return False
        time.sleep(0.5)
        if W.snap("9A - Updating") is None:
            print("❌ Could not capture the Updating screen.")
            return False

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
