#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scenario 7 — Source to Target Wi-Fi with Spanish restart evidence.

This dedicated flow intentionally captures only:
  • About phone, Device details, Model, Android version, IMEI 1/2 when present,
    and Device identifiers.
  • The existing OTA/update evidence sequence, including background-installation
    notification, timed update checkpoints, restart handling, and completion notice.

It reuses shared navigation and OTA helpers without calling run_transition(),
so the full 35-screenshot Target -> Dummy sequence is not executed.
"""
import argparse
import os
import re
import sys
import time
import tracfone_wifi_backend as W
import Axon as ax
from Axon_Lang import get_keywords


def run(call_number, save_root, serial, restart_img):
    W.reset_screenshot_bookkeeping()
    W.SCENARIO_CODE = "7"
    W.PHASE_NAME = "Scenario_7_SOURCE_TO_TARGET_WIFI_ES"
    W.EXPECTED_TOTAL = 0  # this is a focused evidence set, not the 35-shot transition
    W.INCLUDE_HARDWARE_SCREENSHOT = False
    W.RESTART_IMG = os.path.abspath(restart_img)
    ax.TARGET_SERIAL = serial
    W.K = get_keywords("ES")
    ax.KEYWORDS = W.K
    W.ax.TARGET_SERIAL = serial
    W.ax.KEYWORDS = W.K

    folder = os.path.join(save_root, "Scenario 7 Source to Target WiFi Spanish")
    os.makedirs(folder, exist_ok=True)
    ax.SAVE_ROOT = folder
    W.SAVE_DIR = folder

    if ax.adb("get-state").stdout.strip() != "device":
        print(f"Device {serial} is not reachable via ADB.")
        return False

    ax.adb("shell svc power stayon true")
    try:
        # Keep the proven per-screenshot Wi-Fi cycling for device evidence.
        if not W.ensure_wifi_connected(timeout=45):
            return False
        W.wake()
        ax.unlock_device()
        W.device_info_set("PRE", per_shot_wifi=True, include_hardware_screenshot=False)

        # Existing OTA logic handles Wi-Fi enablement, background installation,
        # update checkpoints, restart image handling, reboot, and completion notice.
        # Target -> Dummy includes the notification shade screenshot.
        ota_ok = W.ota_phase(include_notification_dropdown=True)
        if not ota_ok:
            print("Scenario 7 OTA flow failed; see logs above.")
            return False

        # POST-update device evidence: repeat the same About phone -> Device
        # identifiers sequence captured before the update, using Spanish labels.
        print("\nScenario 7 POST-update device information (Spanish UI)...")
        W.device_info_set("POST", per_shot_wifi=False, include_hardware_screenshot=False)

        print("\nScenario 7 focused evidence capture complete.")
        print(f"Evidence folder: {os.path.abspath(folder)}")
        print(f"Captured screenshots: {W.shot_no - 1}")
        return True
    finally:
        ax.adb("shell svc power stayon false")


def main():
    parser = argparse.ArgumentParser(
        description="Scenario 7: Source -> Target Wi-Fi (Spanish UI and restart evidence)")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--call-number", required=True)
    parser.add_argument("--restart-img", default=None)
    parser.add_argument("--save-root", default=None)
    parser.add_argument("--mode", choices=["source_to_target_wifi_es"],
                        default="source_to_target_wifi_es")
    args = parser.parse_args()

    number = args.call_number.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+?\d{5,15}", number):
        sys.exit("Phone number must be digits only (optional leading +), 5-15 digits")

    restart_img = args.restart_img or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "assets", "Restart_ES.png")
    if not os.path.isfile(restart_img):
        sys.exit(f"Spanish restart image not found: {restart_img}")

    results = os.path.abspath(args.save_root or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "results"))
    os.makedirs(results, exist_ok=True)
    results = W.device_results_root(results, args.serial)

    print("SCENARIO 7 — SOURCE -> TARGET • WI-FI • SPANISH UI • PRE/OTA/POST EVIDENCE")
    if not run(number, results, args.serial, restart_img):
        sys.exit("Scenario 7 did not complete.")
    print("Scenario 7 complete.")


if __name__ == "__main__":
    main()
