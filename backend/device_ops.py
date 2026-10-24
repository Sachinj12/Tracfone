"""
device_ops.py
──────────────
Fastboot / ADB device-configuration operations, factored out of the old
console menu (run_devices.py) so the GUI can call them directly and stream
their output into a per-device log panel instead of printing to a shared
terminal.

Every function takes the target `serial` and a `log(line: str)` callback.
Nothing here talks to Tkinter directly, so it can also be reused from a
future CLI or test harness.
"""

import os
import re
import subprocess
import threading
import time

# ── Process tracking, so every command this module spawns (including ones
# that could hang, like `adb wait-for-device`) can be forcibly killed when
# the web console asks the server to stop/restart. Each entry also records
# which device serial the command targeted (parsed from its "-s <serial>"
# argument) so a single device's "End Task" can kill only that device's
# in-flight command without touching any other device's quick action. ──────
_PROC_LOCK = threading.Lock()
_ACTIVE_PROCS = []  # list of {"proc": Popen, "serial": str|None}


def _register(proc, serial=None):
    with _PROC_LOCK:
        _ACTIVE_PROCS.append({"proc": proc, "serial": serial})


def _unregister(proc):
    with _PROC_LOCK:
        _ACTIVE_PROCS[:] = [e for e in _ACTIVE_PROCS if e["proc"] is not proc]


def kill_process_tree(pid):
    """Kill a process and every process it spawned (children/grandchildren),
    not just the top-level handle. A plain terminate()/kill() only signals
    the immediate process — on Windows especially, `cmd.exe` or `adb`
    wrappers can leave their real child process running behind."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass


def terminate_all():
    """Kill every subprocess currently spawned by this module."""
    with _PROC_LOCK:
        entries = list(_ACTIVE_PROCS)
    for e in entries:
        p = e["proc"]
        if p.poll() is None:
            kill_process_tree(p.pid)


def terminate_serial(serial):
    """Kill only the quick-action command(s) currently running for this one
    device's serial, leaving every other device's in-flight command
    untouched. Used by "End Task" and by the auto-disconnect handler."""
    if not serial:
        return
    with _PROC_LOCK:
        entries = [e for e in _ACTIVE_PROCS if e["serial"] == serial]
    for e in entries:
        p = e["proc"]
        if p.poll() is None:
            kill_process_tree(p.pid)


_SERIAL_RE = re.compile(r"-s\s+(\S+)")


def _popen(cmd):
    kwargs = dict(
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    m = _SERIAL_RE.search(cmd)
    _register(proc, m.group(1) if m else None)
    return proc


def _wait(proc):
    try:
        out = proc.communicate()[0]
    finally:
        _unregister(proc)
    return out or "", proc.returncode


def _run(cmd, log):
    """Run a shell command, echo it and its output through the log callback."""
    log(f"$ {cmd}")
    out, code = _wait(_popen(cmd))
    out = out.strip()
    if out:
        log(out)

    class _Result:
        pass

    result = _Result()
    result.stdout = out
    result.stderr = ""
    result.returncode = code
    return result


def _fastboot_devices():
    out, _ = _wait(_popen("fastboot devices"))
    return out


def ensure_fastboot(serial, log):
    if serial not in _fastboot_devices():
        log(f"⚠️  {serial} is not in Fastboot mode. Rebooting to bootloader...")
        _wait(_popen(f"adb -s {serial} reboot bootloader"))
        time.sleep(6)


def ensure_adb(serial, log):
    out, _ = _wait(_popen(f"adb -s {serial} get-state"))
    state = out.strip()
    if state != "device":
        log(f"⚠️  {serial} is not fully turned on. Rebooting to system...")
        _wait(_popen(f"fastboot -s {serial} reboot"))
        log(f"⏳ Waiting for {serial} to fully boot (may take a minute)...")
        _wait(_popen(f"adb -s {serial} wait-for-device"))
        time.sleep(10)
    _wait_for_boot(serial, log, timeout=120)


# ── Individual operations (each mirrors the original run_devices.py logic) ──

def fastboot_erase(serial, log):
    ensure_fastboot(serial, log)
    cmds = [
        f"fastboot -s {serial} erase frp",
        f"fastboot -s {serial} -w",
        f"fastboot -s {serial} erase userdata",
        f"fastboot -s {serial} erase cache",
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f"fastboot -s {serial} oem fb_mode_clear",
        f'fastboot -s {serial} oem config bootmode ""',
        f"fastboot -s {serial} reboot bootloader",
    ]
    for c in cmds:
        _run(c, log)
    log(f"✅ Fastboot erase done for {serial}.")


def change_carrier(serial, carrier, log):
    ensure_fastboot(serial, log)
    cmds = [
        f"fastboot -s {serial} -w",
        f'fastboot -s {serial} oem config fsg-id ""',
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f'fastboot -s {serial} oem config carrier "{carrier}"',
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f"fastboot -s {serial} -w",
        f"fastboot -s {serial} reboot bootloader",
    ]
    for c in cmds:
        _run(c, log)
    log(f"✅ Carrier changed for {serial}.")


def change_sku(serial, sku, log):
    ensure_fastboot(serial, log)
    cmds = [
        f'fastboot -s {serial} oem config sku "{sku}"',
        f'fastboot -s {serial} oem config carrier_sku "{sku}"',
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f"fastboot -s {serial} reboot bootloader",
    ]
    for c in cmds:
        _run(c, log)
    log(f"✅ SKU changed for {serial}.")


SIM_OPTIONS = {
    "1 - Single SIM":  "oem hw dualsim false",
    "2 - Dual SIM":    "oem hw dualsim true",
    "3 - Soft SIM":    "oem hw dualsim soft",
    "4 - eSIM true":   "oem hw esim true",
    "5 - eSIM false":  "oem hw esim false",
}


def change_sim(serial, option_label, log):
    ensure_fastboot(serial, log)
    sim_cmd = SIM_OPTIONS[option_label]
    cmds = [
        f"fastboot -s {serial} {sim_cmd}",
        f"fastboot -s {serial} erase frp",
        f"fastboot -s {serial} -w",
        f"fastboot -s {serial} erase cache",
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f"fastboot -s {serial} erase mdm1m9kefs1",
        f"fastboot -s {serial} erase mdm1m9kefs2",
        f"fastboot -s {serial} oem fb_mode_clear",
        f'fastboot -s {serial} oem config bootmode ""',
        f"fastboot -s {serial} reboot bootloader",
    ]
    for c in cmds:
        _run(c, log)
    log(f"✅ SIM configuration applied for {serial}.")


def change_radio(serial, radio, log):
    ensure_fastboot(serial, log)
    cmds = [
        f'fastboot -s {serial} oem hw radio "{radio}"',
        f"fastboot -s {serial} erase modemst1",
        f"fastboot -s {serial} erase modemst2",
        f"fastboot -s {serial} reboot bootloader",
    ]
    for c in cmds:
        _run(c, log)
    log(f"✅ RADIO changed for {serial}.")


# ── Skip Setup Wizard prerequisites: Developer Options / Stay Awake / ──────
# Language, all gating the actual bypass below. Each raises RuntimeError on
# failure/verification-mismatch, which start_action() in server.py already
# turns into a logged [ERROR] + a stopped, retryable device state — so a
# failure at any of these steps means the bypass below never runs.

LANGUAGE_LOCALES = {
    "English (United States)": "en-US",
    "Spanish (Spain)":         "es-ES",
    "Portuguese (Brazil)":     "pt-BR",
}

STAY_AWAKE_ENABLED = "7"   # stay_on_while_plugged_in: all charging sources
STAY_AWAKE_DISABLED = "0"


def _wait_for_boot(serial, log, timeout=180, settle=3):
    """Block until the device is reachable over ADB *and* sys.boot_completed
    is 1. Polls (instead of `adb wait-for-device`, which has no timeout and
    returns instantly if the device hasn't gone offline yet)."""
    log(f"⏳ Waiting for {serial} to finish booting...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, code = _wait(_popen(f"adb -s {serial} shell getprop sys.boot_completed"))
        if code == 0 and out.strip() == "1":
            time.sleep(settle)   # let the settings provider settle
            log(f"Device {serial} is up.")
            return
        time.sleep(2)
    raise RuntimeError(f"{serial} did not finish booting within {timeout}s.")


def _settings_cmd(serial, args, log, retries=3):
    """Run `adb shell settings ...`. If the device is mid-reboot (Broken pipe,
    device offline, etc.) wait for it to come back and try again, instead of
    failing the whole task on a transient error."""
    cmd = f"adb -s {serial} shell settings {args}"
    res = None
    for attempt in range(1, retries + 1):
        res = _run(cmd, log)
        if res.returncode == 0 and "Failure calling service" not in res.stdout:
            return res
        if attempt < retries:
            log(f"⚠️  settings service not ready (attempt {attempt}/{retries}); waiting for device...")
            time.sleep(8)        # let a device that is shutting down actually go down
            _wait_for_boot(serial, log)
    return res


def _get_global_setting(serial, key, log):
    res = _settings_cmd(serial, f"get global {key}", log)
    if res.returncode != 0 or "Failure calling service" in res.stdout:
        raise RuntimeError(f"Could not read '{key}' on {serial}: {res.stdout or 'unknown ADB error'}")
    return res.stdout.strip()


def _put_global_setting(serial, key, value, log):
    res = _settings_cmd(serial, f"put global {key} {value}", log)
    if res.returncode != 0 or "Failure calling service" in res.stdout:
        raise RuntimeError(f"Could not set '{key}' to '{value}' on {serial}: {res.stdout or 'unknown ADB error'}")


def enable_developer_options(serial, log):
    log(f"— Enabling Developer Options on {serial}...")
    if _get_global_setting(serial, "development_settings_enabled", log) == "1":
        log("Developer Options already enabled.")
        return
    _put_global_setting(serial, "development_settings_enabled", "1", log)
    time.sleep(0.5)
    if _get_global_setting(serial, "development_settings_enabled", log) != "1":
        raise RuntimeError(
            f"Developer Options could not be enabled on {serial}. "
            f"This device/OEM may block this setting via ADB."
        )
    log(f"✅ Developer Options enabled for {serial}.")


def enable_stay_awake(serial, log):
    log(f"— Enabling Stay Awake on {serial}...")
    _put_global_setting(serial, "stay_on_while_plugged_in", STAY_AWAKE_ENABLED, log)
    if _get_global_setting(serial, "stay_on_while_plugged_in", log) != STAY_AWAKE_ENABLED:
        raise RuntimeError(f"Stay Awake could not be enabled on {serial}.")
    log(f"✅ Stay Awake enabled for {serial}.")


def _get_system_locale(serial, log):
    res = _run(f"adb -s {serial} shell settings get system system_locales", log)
    val = res.stdout.strip() if res.returncode == 0 else ""
    if val and val.lower() != "null":
        return val
    res = _run(f"adb -s {serial} shell getprop persist.sys.locale", log)
    return res.stdout.strip() if res.returncode == 0 else ""


def _locale_matches(output, expected):
    normalized = output.strip().replace("_", "-").lower()
    first = normalized.splitlines()[0].strip().split(",")[0].strip() if normalized else ""
    return first == expected.lower() or normalized == expected.lower()


def apply_device_language(serial, language_label, log):
    locale = LANGUAGE_LOCALES.get(language_label)
    if not locale:
        raise RuntimeError("No language was selected.")

    log(f"— Setting device language to {language_label} ({locale}) on {serial}...")

    current = _get_system_locale(serial, log)
    if current and _locale_matches(current, locale):
        log(f"Device is already set to {language_label}.")
        return

    res = _run(f"adb -s {serial} shell settings put system system_locales {locale}", log)
    if res.returncode != 0:
        raise RuntimeError(f"Android rejected the language change on {serial}: {res.stdout or 'unknown ADB error'}")

    time.sleep(2)
    actual = _get_system_locale(serial, log)
    if not actual or not _locale_matches(actual, locale):
        raise RuntimeError(
            f"Language change could not be verified on {serial}. "
            f"Expected {locale}, but the device reports: {actual or 'unknown'}"
        )

    log(f"✅ Language verified as {language_label} ({locale}) on {serial}.")


def _bypass_setup_wizard(serial, log):
    """The original, unchanged Skip Setup Wizard bypass: mark setup complete
    and reboot. Kept as its own step so it is never duplicated — it is only
    ever invoked, as the final step, by skip_setup_wizard() below."""
    _run(
        f'adb -s {serial} shell content insert --uri content://settings/secure '
        f'--bind name:s:user_setup_complete --bind value:s:1',
        log,
    )
    _run(f"adb -s {serial} reboot", log)
    time.sleep(8)                       # let the device actually go down
    _wait_for_boot(serial, log)         # ...then wait for it to come back
    log(f"✅ Setup Wizard bypass done for {serial}.")


def skip_setup_wizard(serial, language_label, log):
    """Full Skip Setup Wizard workflow, in required order:
    device already resolved to `serial` by the caller (server.py only ever
    targets the one device the user selected/quick-configured) →
    Developer Options → Stay Awake → apply + verify language → bypass setup
    wizard. Any failure raises, which stops the workflow before the bypass
    ever runs."""
    ensure_adb(serial, log)
    enable_developer_options(serial, log)
    enable_stay_awake(serial, log)
    apply_device_language(serial, language_label, log)
    _bypass_setup_wizard(serial, log)


def set_battery_level(serial, level, log):
    ensure_adb(serial, log)
    _run(f"adb -s {serial} shell dumpsys battery set level {level}", log)
    log(f"✅ Battery level set to {level} for {serial}.")


def reboot_normal(serial, log):
    if serial in _fastboot_devices():
        _run(f"fastboot -s {serial} reboot", log)
    else:
        _run(f"adb -s {serial} reboot", log)
    log(f"✅ Normal reboot triggered for {serial}.")


def reboot_bootloader(serial, log):
    _run(f"adb -s {serial} reboot bootloader", log)
    log(f"✅ Rebooted {serial} to bootloader.")


# Motorola boot modes set through `fastboot oem config bootmode <mode>`.
BOOTMODE_FACTORY  = "factory"
BOOTMODE_BP_TOOLS = "bp-tools"


def _reboot_into_bootmode(serial, bootmode, label, log):
    """Works from both ADB and Fastboot: from ADB the unit is first rebooted to
    the bootloader (ensure_fastboot), then
        fastboot oem config bootmode <mode>
        fastboot reboot
    are run, so the unit boots straight into the requested mode."""
    ensure_fastboot(serial, log)
    if serial not in _fastboot_devices():
        raise RuntimeError(f"{serial} did not enter Fastboot, so it could not be rebooted to {label}.")
    res = _run(f"fastboot -s {serial} oem config bootmode {bootmode}", log)
    if res.returncode != 0 or "FAILED" in (res.stdout or "").upper():
        raise RuntimeError(f"Could not set bootmode '{bootmode}' on {serial}: {res.stdout or 'unknown fastboot error'}")
    res = _run(f"fastboot -s {serial} reboot", log)
    if res.returncode != 0 or "FAILED" in (res.stdout or "").upper():
        raise RuntimeError(f"Bootmode was set but the reboot failed on {serial}: {res.stdout or 'unknown fastboot error'}")
    log(f"✅ {serial} is rebooting to {label}.")


def reboot_factory(serial, log):
    _reboot_into_bootmode(serial, BOOTMODE_FACTORY, "Factory mode", log)


def reboot_bp_tools(serial, log):
    _reboot_into_bootmode(serial, BOOTMODE_BP_TOOLS, "BP_Tools mode", log)


# ── Registry used by the GUI to build the action dropdown ───────────────────
# Each entry: label -> (needs_param, param_prompt, function)
ACTIONS = {
    "Fastboot Erase":              (False, None, fastboot_erase),
    "Change ro.carrier":           (True,  "New ro.carrier value", change_carrier),
    "Change SKU":                  (True,  "New SKU value", change_sku),
    "Change SIM / eSIM Config":    (True,  "SIM option", change_sim),
    "Change RADIO":                (True,  "RADIO value", change_radio),
    "Skip Setup Wizard (Bypass)":  (True, "Select device language", skip_setup_wizard),
    "Change Battery Level":        (True,  "Battery level (0-100)", set_battery_level),
    "Reboot Normal":               (False, None, reboot_normal),
    "Reboot to Bootloader":        (False, None, reboot_bootloader),
    "Reboot Factory":              (False, None, reboot_factory),
    "Reboot BP_Tools":             (False, None, reboot_bp_tools),
}

# ── Which mode(s) each action is valid for, used by the GUI to filter the
# quick per-device configuration menu down to what actually applies. ──────
ACTION_MODES = {
    "Fastboot Erase":              ["FASTBOOT"],
    "Change ro.carrier":           ["FASTBOOT"],
    "Change SKU":                  ["FASTBOOT"],
    "Change SIM / eSIM Config":    ["FASTBOOT"],
    "Change RADIO":                ["FASTBOOT"],
    "Skip Setup Wizard (Bypass)":  ["ADB"],
    "Change Battery Level":        ["ADB"],
    "Reboot Normal":               ["ADB", "FASTBOOT"],
    "Reboot to Bootloader":        ["ADB"],
    "Reboot Factory":              ["ADB", "FASTBOOT"],
    "Reboot BP_Tools":             ["ADB", "FASTBOOT"],
}

# ── Actions whose parameter is a fixed set of choices rather than free text,
# used by the GUI to render tick/select options instead of a text box. Any
# action not listed here (but with needs_param=True) still gets a free-text
# input — e.g. "Change ro.carrier" has no fixed list of valid values. ──────
PARAM_OPTIONS = {
    "Change SIM / eSIM Config":    list(SIM_OPTIONS.keys()),
    "Skip Setup Wizard (Bypass)":  list(LANGUAGE_LOCALES.keys()),
}

# Actions that must NOT come pre-selected in the UI's option list, because a
# choice is required from the user rather than defaulted (unlike e.g. SIM
# config, where defaulting to the first option is fine). The frontend reads
# this via /api/actions to decide whether to pre-check the first radio.
REQUIRES_EXPLICIT_CHOICE = {
    "Skip Setup Wizard (Bypass)",
}



