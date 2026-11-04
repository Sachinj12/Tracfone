"""
wifi_manager.py
────────────────
Wi-Fi Manager feature for Axon: save Wi-Fi network configurations (manually
or from a QR code) and connect/disconnect ADB devices to/from them.

This is a separate, self-contained feature — it does not touch or modify
any existing Axon workflow (Configure / Flash / PreSoak / CM Validation /
APK Push). Device *detection* is intentionally NOT duplicated here: the
server passes in the list of already-known ADB serials (see
`server.py`'s `wifi_adb_serials()`), which come from Axon's existing
scan/connectivity-monitor mechanism.

Optional dependencies
----------------------
* `cryptography` — required to store Wi-Fi passwords encrypted at rest.
* `opencv-python-headless` (+ numpy) — required only for "Add from QR".

Both are imported lazily so the rest of Axon keeps working even if these
haven't been installed yet; each feature reports a clear, actionable error
instead of crashing the server. Run `Run_Axon.bat` (which now installs
these automatically) or `py -m pip install -r requirements-wifi.txt`.

Storage
-------
Saved networks live in `backend/wifi_data/networks.json`, with passwords
and usernames encrypted using a key file created on first run at
`backend/wifi_data/secret.key` (readable only by your account, like any
other locally-stored Axon data). Passwords are never written to the log,
never returned to the browser, and never re-displayed in the UI once saved.
This mirrors the standalone Wi-Fi Manager reference implementation's
storage design.
"""

import json
import os
import re
import shlex
import stat
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "wifi_data"
NETWORKS_FILE = DATA_DIR / "networks.json"
KEY_FILE = DATA_DIR / "secret.key"

SECURITY_TYPES = ["WPA2", "WPA", "WPA3", "WEP", "EAP", "nopass"]

ADB_TIMEOUT = 15
WIFI_CONNECT_TIMEOUT = 25
WIFI_DISCONNECT_TIMEOUT = 20

_file_lock = threading.Lock()


class WifiFeatureUnavailable(Exception):
    """Raised when an optional dependency (cryptography / opencv) is
    missing. The message is meant to be shown to the user as-is."""


# ---------------------------------------------------------------------
# Encryption (lazy import of `cryptography`)
# ---------------------------------------------------------------------

_fernet = None
_fernet_error = None


def _get_fernet():
    global _fernet, _fernet_error
    if _fernet is not None:
        return _fernet
    if _fernet_error is not None:
        raise WifiFeatureUnavailable(_fernet_error)
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _fernet_error = (
            "Wi-Fi Manager needs the 'cryptography' package to store passwords "
            "securely. Install it with:  py -m pip install cryptography  "
            "(or just re-run Run_Axon.bat, which installs it automatically)."
        )
        raise WifiFeatureUnavailable(_fernet_error)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    _fernet = Fernet(key)
    return _fernet


def _encrypt(value):
    if not value:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value):
    if not value:
        return None
    return _get_fernet().decrypt(value.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------
# Storage: saved Wi-Fi networks (JSON file, passwords encrypted)
# ---------------------------------------------------------------------

def _read_all():
    if not NETWORKS_FILE.exists():
        return []
    with open(NETWORKS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_all(networks):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(NETWORKS_FILE, "w", encoding="utf-8") as f:
        json.dump(networks, f, indent=2)


def _public(n):
    """Never include the actual password/username in anything sent to the browser."""
    return {
        "id": n["id"],
        "label": n["label"],
        "ssid": n["ssid"],
        "security_type": n["security_type"],
        "has_username": bool(n.get("username_enc")),
        "has_password": bool(n.get("password_enc")),
        "source": n.get("source", "manual"),
        "hidden": bool(n.get("hidden")),
    }


def list_networks():
    return [_public(n) for n in _read_all()]


def get_credentials(network_id):
    for n in _read_all():
        if n["id"] == network_id:
            return {
                "id": n["id"],
                "label": n["label"],
                "ssid": n["ssid"],
                "security_type": n["security_type"],
                "username": _decrypt(n.get("username_enc")),
                "password": _decrypt(n.get("password_enc")),
                "hidden": bool(n.get("hidden")),
            }
    return None


def _validate(label, ssid, security_type, password, require_password=True):
    errors = []
    if not label or not label.strip():
        errors.append("Wi-Fi label/name is required.")
    if not ssid or not ssid.strip():
        errors.append("SSID is required.")
    if security_type not in SECURITY_TYPES:
        errors.append(f"Unsupported security type '{security_type}'.")
    if require_password and security_type != "nopass" and not password:
        errors.append("Password is required for secured networks.")
    return errors


def create_network(label, ssid, security_type, username, password, source="manual", hidden=False):
    errors = _validate(label, ssid, security_type, password)
    if errors:
        return None, errors

    with _file_lock:
        networks = _read_all()
        next_id = max((n["id"] for n in networks), default=0) + 1
        entry = {
            "id": next_id,
            "label": label.strip(),
            "ssid": ssid.strip(),
            "security_type": security_type,
            "username_enc": _encrypt(username),
            "password_enc": _encrypt(password),
            "source": source,
            "hidden": bool(hidden),
        }
        networks.append(entry)
        _write_all(networks)
    return _public(entry), None


def update_network(network_id, label, ssid, security_type, username, password,
                    password_provided, username_provided, hidden=None):
    with _file_lock:
        networks = _read_all()
        entry = next((n for n in networks if n["id"] == network_id), None)
        if entry is None:
            return None, ["Saved network not found."]

        new_label = label if label is not None else entry["label"]
        new_ssid = ssid if ssid is not None else entry["ssid"]
        new_security = security_type if security_type is not None else entry["security_type"]
        existing_password = _decrypt(entry.get("password_enc"))

        errors = _validate(new_label, new_ssid, new_security,
                            password if password_provided else existing_password)
        if errors:
            return None, errors

        entry["label"] = new_label.strip()
        entry["ssid"] = new_ssid.strip()
        entry["security_type"] = new_security
        if password_provided:
            entry["password_enc"] = _encrypt(password)
        if username_provided:
            entry["username_enc"] = _encrypt(username)
        if hidden is not None:
            entry["hidden"] = bool(hidden)

        _write_all(networks)
    return _public(entry), None


def delete_network(network_id):
    with _file_lock:
        networks = _read_all()
        remaining = [n for n in networks if n["id"] != network_id]
        if len(remaining) == len(networks):
            return False
        _write_all(remaining)
    return True


# ---------------------------------------------------------------------
# Wi-Fi QR code decoding (lazy import of OpenCV; fully in-memory)
# ---------------------------------------------------------------------

def _unescape(value):
    return re.sub(r"\\(.)", r"\1", value)


def _parse_wifi_qr_payload(payload):
    if not payload.startswith("WIFI:"):
        raise ValueError("QR code does not contain Wi-Fi configuration data.")
    body = payload[len("WIFI:"):]
    fields = re.split(r"(?<!\\);", body)

    parsed = {}
    for field in fields:
        if not field:
            continue
        m = re.match(r"^([A-Za-z]):(.*)$", field, re.DOTALL)
        if m:
            parsed[m.group(1).upper()] = _unescape(m.group(2))

    ssid = parsed.get("S")
    if not ssid:
        raise ValueError("QR code is missing an SSID.")

    security_map = {"WPA": "WPA", "WPA2": "WPA2", "WPA3": "WPA3", "WEP": "WEP",
                     "SAE": "WPA3", "NOPASS": "nopass", "": "nopass"}
    security_type = security_map.get((parsed.get("T") or "nopass").upper(), "WPA")

    hidden_raw = (parsed.get("H") or "").lower().strip()
    hidden = hidden_raw in ("true", "1", "yes")

    return {"ssid": ssid, "password": parsed.get("P") or None, "security_type": security_type, "hidden": hidden}


def decode_wifi_qr(image_bytes):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise WifiFeatureUnavailable(
            "Adding networks from a QR code needs the 'opencv-python-headless' "
            "and 'numpy' packages. Install with:  "
            "py -m pip install opencv-python-headless numpy  "
            "(or re-run Run_Axon.bat, which installs them automatically). "
            "Adding a network manually still works without them."
        )

    np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Unsupported or corrupted image format.")

    detector = cv2.QRCodeDetector()
    texts = []
    try:
        ok, decoded_texts, _points, _ = detector.detectAndDecodeMulti(image)
        if ok:
            texts = [t for t in decoded_texts if t]
    except Exception:
        pass
    if not texts:
        text, _points, _ = detector.detectAndDecode(image)
        if text:
            texts = [text]

    if not texts:
        raise ValueError("No QR code could be detected in the provided image.")

    wifi_payloads = [t for t in texts if t.startswith("WIFI:")]
    if not wifi_payloads:
        raise ValueError("A QR code was found, but it doesn't contain Wi-Fi configuration data.")

    return _parse_wifi_qr_payload(wifi_payloads[0])


# ---------------------------------------------------------------------
# ADB: Wi-Fi connect / disconnect
#
# Every command targets exactly one device via `-s <serial>` — there is
# no code path that can act on "whichever device is first".
#
# IMPORTANT: `adb shell <arg1> <arg2> ...` does not pass arguments to the
# device untouched — adb joins them and hands the result to the device's
# OWN shell (/system/bin/sh) for re-parsing. An SSID containing spaces or
# characters like parentheses (e.g. "iPhone (2)") would otherwise break
# the remote command. Every argument here is POSIX-shell-quoted with
# shlex.quote before being joined into one command string, so the remote
# shell always sees each one as a single literal value.
# ---------------------------------------------------------------------

def _adb_shell(serial, parts, timeout=ADB_TIMEOUT, secret_indices=None):
    if not serial:
        raise ValueError("A device serial is required.")
    secret_indices = set(secret_indices or [])
    quoted = [shlex.quote(str(p)) for p in parts]
    command_str = " ".join(quoted)
    try:
        return subprocess.run(
            ["adb", "-s", serial, "shell", command_str],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        raise RuntimeError("adb was not found on PATH. Install Android Platform Tools.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb command timed out after {timeout}s.")


def _parse_wifi_status(raw):
    enabled = bool(re.search(r"Wifi is enabled", raw, re.IGNORECASE))
    connected, ssid = False, None
    # SSID may be quoted or unquoted and can be followed by extra diagnostic
    # text on the same line, depending on the Android build / device language
    # settings. Prefer the quoted form so a multi-word/unicode name (e.g. a
    # Spanish or Portuguese SSID) is captured atomically.
    m = re.search(r'connected to\s*"([^"]+)"', raw, re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        if name:
            connected, ssid = True, name
    else:
        m = re.search(r"connected to\s+([^\n]+)", raw, re.IGNORECASE)
        name = m.group(1).strip().strip('"').strip() if m else ""
        if name:
            connected, ssid = True, name
    return {"enabled": enabled, "connected": connected, "ssid": ssid}


def _get_ip_address(serial):
    try:
        proc = _adb_shell(serial, ["ip", "-f", "inet", "addr", "show", "wlan0"])
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", proc.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def get_device_wifi_status(serial):
    """Best-effort live Wi-Fi status for one already-known ADB device."""
    try:
        proc = _adb_shell(serial, ["cmd", "wifi", "status"])
        status = _parse_wifi_status(proc.stdout)
        return {
            "serial": serial,
            "wifi_status": "wifi_connected" if status["connected"] else "connected",
            "wifi_ssid": status["ssid"],
            "last_error": None,
        }
    except Exception as exc:
        return {"serial": serial, "wifi_status": "connected", "wifi_ssid": None, "last_error": str(exc)}


def get_device_saved_networks(serial):
    """List the networks currently *saved* on the device itself via
    `cmd wifi list-networks`. Pure read — never changes anything. Used to
    prove that a network stays configured on the device (SSID + config)
    across a radio-off toggle / reboot: shell-added configs are sometimes
    treated as transient by the device, so we read the real saved list
    instead of assuming `connect-network` persisted."""
    try:
        proc = _adb_shell(serial, ["cmd", "wifi", "list-networks"])
        if proc.returncode != 0:
            return []
        saved = []
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("Network Id") or line.lower().startswith("no networks"):
                continue
            m = re.match(r"^(\d+)\s+(.*?)\s{2,}(\S+)$", line)
            if not m:
                continue
            saved.append({"id": m.group(1), "ssid": m.group(2).strip().strip('"')})
        return saved
    except Exception:
        return []


def _ssid_in_scan(serial, ssid):
    """Best-effort check of the device's latest Wi-Fi scan results for the
    SSID. Returns True/False, or None when the scan couldn't be read. Hidden
    networks show no SSID in the scan, so a False here is still only a hint
    — the device's own saved config (HIDDEN flag) is authoritative."""
    try:
        proc = _adb_shell(serial, ["cmd", "wifi", "list-scan-results"])
        raw = proc.stdout or ""
        return bool(re.search(rf"\b{re.escape(ssid)}\b", raw))
    except Exception:
        return None


def connect_device(serial, network_id):
    """Connect exactly one device to exactly one saved network. Never
    raises — always returns a result dict, so a multi-device batch can
    keep going even if one device fails."""
    result = {"serial": serial, "success": False}
    creds = get_credentials(network_id)
    if creds is None:
        result["error"] = "Saved network not found."
        return result

    ssid = creds["ssid"]
    security_map = {"nopass": "open", "WEP": "open", "WPA": "wpa2",
                     "WPA2": "wpa2", "WPA3": "wpa3", "EAP": "wpa2"}
    android_security = security_map.get(creds["security_type"], "wpa2")

    try:
        _adb_shell(serial, ["svc", "wifi", "enable"])

        hidden = bool(creds.get("hidden"))
        last_status = {"enabled": False, "connected": False, "ssid": None}

        # Up to two attempts: first with the stored settings and the given
        # hidden flag. A hidden SSID the app doesn't know about yet will not
        # associate without `-h` (no directed probe is sent), so if the first
        # attempt fails while the SSID is invisible in the scan, fall back to
        # connecting it as hidden once.
        for attempt in range(2):
            connect_parts = ["cmd", "wifi", "connect-network", ssid, android_security]
            secret_indices = []
            if android_security != "open" and creds["password"]:
                # The passphrase is positional on modern Android builds; `-p`
                # marks the network private (not shared) and `-hidden` is not
                # a recognized flag, so neither can carry the passphrase.
                connect_parts.append(creds["password"])
                secret_indices = [len(connect_parts) - 1]
            if hidden:
                connect_parts.append("-h")

            proc = _adb_shell(serial, connect_parts, timeout=WIFI_CONNECT_TIMEOUT,
                               secret_indices=secret_indices)
            if proc.returncode != 0:
                result["error"] = (proc.stderr or "").strip() or "Wi-Fi connect command failed."
                return result

            # Ensure the config actually landed in the device's *saved* network
            # list. Shell-added configs are transient on some builds — if
            # connect-network didn't persist it (e.g. freshly-flashed device in
            # setup wizard), add it explicitly via the dedicated saved-network
            # command ('cmd wifi add-network'), then connect again using the
            # saved config.
            if not any(s["ssid"] == ssid for s in get_device_saved_networks(serial)):
                add_parts = ["cmd", "wifi", "add-network", ssid, android_security]
                add_secret_indices = []
                if android_security != "open" and creds["password"]:
                    add_parts.append(creds["password"])
                    add_secret_indices = [len(add_parts) - 1]
                if hidden:
                    add_parts.append("-h")
                add_proc = _adb_shell(serial, add_parts, timeout=WIFI_CONNECT_TIMEOUT,
                                      secret_indices=add_secret_indices)
                if add_proc.returncode != 0:
                    result["error"] = "Device connected to the network but refused to save it."
                    return result
                time.sleep(1.0)
                if not any(s["ssid"] == ssid for s in get_device_saved_networks(serial)):
                    result["error"] = "Device still did not save the network configuration."
                    return result
                connect_proc = _adb_shell(serial, connect_parts, timeout=WIFI_CONNECT_TIMEOUT,
                                          secret_indices=secret_indices)
                if connect_proc.returncode != 0:
                    result["error"] = (connect_proc.stderr or "").strip() or "Reconnect to saved network failed."
                    return result

            # Verify against real device state — never trust the exit code alone.
            deadline = time.time() + WIFI_CONNECT_TIMEOUT
            while time.time() < deadline:
                status_proc = _adb_shell(serial, ["cmd", "wifi", "status"])
                last_status = _parse_wifi_status(status_proc.stdout)
                if last_status["connected"] and (last_status["ssid"] or "").strip('"') == ssid:
                    result["success"] = True
                    result["ssid"] = ssid
                    result["ip_address"] = _get_ip_address(serial)
                    result["saved_networks"] = get_device_saved_networks(serial)
                    result["saved"] = any(s["ssid"] == ssid for s in result["saved_networks"])
                    return result
                time.sleep(1.5)

            # First attempt failed and the network looks hidden: retry once
            # with the hidden flag so the device sends directed probes.
            if attempt == 0 and not hidden and _ssid_in_scan(serial, ssid) is False:
                hidden = True
                continue
            break

        if last_status["connected"]:
            result["error"] = f"Device connected to '{last_status['ssid']}' instead of '{ssid}'."
        elif not last_status["enabled"]:
            result["error"] = "Wi-Fi is not enabled on the device."
        else:
            result["error"] = "Device did not associate to the network in time."
            visible = _ssid_in_scan(serial, ssid)
            if visible is False:
                result["error"] += (f" '{ssid}' was not found in the device's latest scan — "
                                    "check that the access point is powered on and in range.")
            elif visible is True:
                result["error"] += (f" '{ssid}' is visible in the scan, so the password/security "
                                    "settings may not match the access point.")
    except Exception as exc:
        result["error"] = str(exc)

    return result


def _radio_enabled(serial):
    """Whether the Wi-Fi radio is on, from the persisted system setting
    Android itself uses (updated immediately by svc wifi enable/disable) —
    far more reliable across OEM skins than parsing the free-text output of
    `cmd wifi status`, which varies by manufacturer/Android version and can
    still say things like "scanning is always available" while the radio
    is off. Returns True/False, or None if the setting couldn't be read
    (falls back to the text parser in that case)."""
    try:
        proc = _adb_shell(serial, ["settings", "get", "global", "wifi_on"])
        val = (proc.stdout or "").strip()
        if val in ("0", "1"):
            return val == "1"
    except Exception:
        pass
    return None


def disconnect_device(serial):
    """Turn Wi-Fi OFF on the device — the radio is disabled only, the saved
    network stays on the device. Nothing is forgotten or removed, so toggling
    Wi-Fi back on (manually or via the app) reconnects to the same network."""
    result = {"serial": serial, "success": False}
    try:
        saved_before = get_device_saved_networks(serial)
        result["saved_before"] = [s["ssid"] for s in saved_before]

        status_proc = _adb_shell(serial, ["cmd", "wifi", "status"])
        status = _parse_wifi_status(status_proc.stdout)
        already_off = _radio_enabled(serial)
        if already_off is None:
            already_off = not status["enabled"]
        else:
            already_off = not already_off
        if status["connected"]:
            result["previous_ssid"] = (status["ssid"] or "").strip('"')
        if already_off:
            result["error"] = "Wi-Fi is already off on the device."
            result["saved_after"] = [s["ssid"] for s in get_device_saved_networks(serial)]
            return result

        disable_proc = _adb_shell(serial, ["svc", "wifi", "disable"])
        if disable_proc.returncode != 0:
            result["error"] = (disable_proc.stderr or "").strip() or "Failed to turn Wi-Fi off."
            return result

        # Verify against real device state — never trust the exit code alone,
        # but give the radio a realistic amount of time to actually go down
        # instead of judging it off a single check a moment after the
        # command returns (the radio can take a few seconds to report back).
        # Prefer the persisted `wifi_on` setting (see _radio_enabled); only
        # fall back to parsing `cmd wifi status` text if that setting isn't
        # readable on this device/Android build.
        deadline = time.time() + WIFI_DISCONNECT_TIMEOUT
        enabled_now = True
        last_status_text = status_proc.stdout or ""
        while time.time() < deadline:
            radio_state = _radio_enabled(serial)
            if radio_state is not None:
                enabled_now = radio_state
            else:
                post_proc = _adb_shell(serial, ["cmd", "wifi", "status"])
                last_status_text = post_proc.stdout or ""
                enabled_now = _parse_wifi_status(last_status_text)["enabled"]
            if not enabled_now:
                break
            time.sleep(1.0)

        if enabled_now:
            result["error"] = "Device still reports Wi-Fi enabled after turn-off attempt."
            # Surface what the device actually said so this is debuggable
            # instead of a dead end — some OEM builds (or an MDM/"auto Wi-Fi"
            # policy on the phone) re-enable the radio right after it's
            # turned off, and this line makes that visible.
            first_line = (last_status_text.strip().splitlines() or [""])[0]
            if first_line:
                result["error"] += f" (device reported: \"{first_line}\")"
            return result

        result["success"] = True
        result["wifi_off"] = True
        saved_after = get_device_saved_networks(serial)
        result["saved_after"] = [s["ssid"] for s in saved_after]
        # Prove nothing was forgotten: same saved set before and after the toggle.
        result["saved_network_kept"] = set(result["saved_before"]) == set(result["saved_after"])
    except Exception as exc:
        result["error"] = str(exc)

    return result
