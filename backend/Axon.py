import subprocess
import time
import os
import re
import shutil
import hashlib
import argparse
import sys
import unicodedata
from xml.etree import ElementTree as ET
from PIL import Image
from Axon_Lang import get_keywords

# Make Axon safe when launched from a Windows cp1252 console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =============================================
# ========== GLOBALS ==========================
# =============================================

PHASE = "PreSoak"
screenshot_index = 1
last_hash = None
repeat_count = 0
SAVE_DIR = None
SAVE_ROOT = None
TARGET_SERIAL = None
RESTART_IMG = None

# ── Unicode normalizer (strips accents for accent-insensitive matching) ───────
def normalize_text(text):
    text = text.lower()
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return text

# ── KEYWORDS loaded at runtime from Axon_Lang.py based on language choice ─
# Set in __main__ via: KEYWORDS = get_keywords(lang_code)
KEYWORDS = get_keywords("EN")  # default until user selects at startup

# =============================================
# ========== ADB UTILITIES ====================
# =============================================

def adb(cmd):
    if TARGET_SERIAL:
        return subprocess.run(f"adb -s {TARGET_SERIAL} {cmd}", shell=True, capture_output=True, text=True)
    return subprocess.run(f"adb {cmd}", shell=True, capture_output=True, text=True)

def get_device_info():
    print(f"[{TARGET_SERIAL}] 🔍 Gathering device info...")
    serial = TARGET_SERIAL
    product = adb("shell getprop ro.product.name").stdout.strip()
    carrier = adb("shell getprop ro.carrier").stdout.strip()
    if not carrier:
        carrier = "UNKNOWN"
    print(f"📡 Device locked on!")
    print(f"🔢 Serial   : {serial}")
    print(f"📱 Product  : {product}")
    print(f"📶 Carrier  : {carrier}")
    folder = f"{PHASE}_{product}_{carrier}_Barcode_{serial}_NS"
    folder = re.sub(r'[^\w\-]', '_', folder)
    if SAVE_ROOT:
        folder = os.path.join(SAVE_ROOT, folder)
    os.makedirs(folder, exist_ok=True)
    # Tell the server (which is watching this process's stdout) exactly
    # where screenshots are landing, instead of making it guess later by
    # glob-matching folder names against the serial. This is a
    # machine-parseable marker line — server.py's log_for() looks for it.
    print(f"[SAVE_DIR] {os.path.abspath(folder)}")
    return folder

def is_device_ready():
    result = adb("shell getprop sys.boot_completed")
    return result.stdout.strip() == "1"

def wait_for_device():
    print("⏳ Device went dark — waiting for it to resurface...")
    adb("wait-for-device")

    printed = False

    while not is_device_ready():
        if not printed:
            print("🔄 Boot sequence in progress... hang tight!")
            printed = True
        time.sleep(5)

    print("🚀 Device is fully booted and ready to rock!")
   
    # ✅ IMPORTANT: stabilization delay
    print("⏳ Giving device some time to settle...")
    time.sleep(5)

def unlock_device():
    print("🔓 Preparing to unlock...")

    # Wake up device properly
    adb("shell input keyevent 26")
    time.sleep(1)
    adb("shell input keyevent KEYCODE_WAKEUP")
    time.sleep(2)

    # Get screen resolution
    result = adb("shell wm size")

    size = result.stdout.strip().split(":")[-1].strip()
    width, height = map(int, size.split("x"))

    # Calculate swipe dynamically
    x = width // 2
    start_y = int(height * 0.8)
    end_y   = int(height * 0.3)

    # Swipe twice (more reliable)
    adb(f"shell input swipe {x} {start_y} {x} {end_y} 300")

    time.sleep(3)
    print("✅ Unlock attempt done!")

# =============================================
# ========== SCREENSHOT UTILITIES =============
# =============================================

def capture_screen(temp_file=None):
    """Single screenshot function used across both flows — saves as PreSoak_XXX.png.
    Every step is checked and logged, so a failure shows up in the log instead
    of silently producing a missing/blank screenshot."""
    global screenshot_index
    try:
        if temp_file is None:
            cap = adb("shell screencap -p /sdcard/temp.png")
            if cap.returncode != 0:
                print(f"❌ Screenshot FAILED (screencap): {cap.stderr.strip() or cap.stdout.strip() or 'unknown error'}")
                return None

            temp_file = f"{SAVE_DIR}/temp_{screenshot_index:03}.png"
            pull = adb(f"pull /sdcard/temp.png {temp_file}")
            if pull.returncode != 0 or not os.path.isfile(temp_file):
                print(f"❌ Screenshot FAILED (pull): {pull.stderr.strip() or pull.stdout.strip() or 'file not received'}")
                return None

            adb("shell rm /sdcard/temp.png")

        final_file = f"{SAVE_DIR}/{PHASE}_{screenshot_index:03}.png"
        os.rename(temp_file, final_file)
        print(f"📸 Snap! Saved: {PHASE}_{screenshot_index:03}.png")
        screenshot_index += 1
        return final_file
    except Exception as e:
        print(f"❌ Screenshot FAILED (unexpected error): {e}")
        return None

def get_center_hash(img_path):
    img = Image.open(img_path)
    w, h = img.size
    crop = img.crop((0, int(h * 0.25), w, int(h * 0.75)))
    return hashlib.md5(crop.tobytes()).hexdigest()

# =============================================
# ========== UI / NAVIGATION ==================
# =============================================

def dump_ui():
    ui_file = f"ui_{TARGET_SERIAL}.xml" if TARGET_SERIAL else "ui.xml"
    adb("shell uiautomator dump /sdcard/ui.xml")
    adb(f"pull /sdcard/ui.xml {ui_file}")
    
    # Read the XML data, then delete the file immediately so it doesn't clutter the folder
    try:
        with open(ui_file, "r", encoding="utf-8") as f:
            data = f.read()
        os.remove(ui_file)
        return data
    except FileNotFoundError:
        return "<hierarchy></hierarchy>"

def swipe_up():
    result = adb("shell wm size")
    size = result.stdout.strip().split(":")[-1].strip()
    width, height = map(int, size.split("x"))

    x = width // 2
    start_y = int(height * 0.75)
    end_y   = int(height * 0.25)

    adb(f"shell input swipe {x} {start_y} {x} {end_y} 400")

def go_back(times=1):
    for _ in range(times):
        adb("shell input keyevent 4")
        time.sleep(2)

def scroll_n(n):
    for _ in range(n):
        swipe_up()
        time.sleep(2)

def tap_by_keywords(keywords, data=None):
    if data is None:
        data = dump_ui()
    norm_data = normalize_text(data)
    for key in keywords:
        norm_key = normalize_text(key)
        match = re.search(
            rf'(text="{re.escape(norm_key)}"|content-desc="{re.escape(norm_key)}").*?bounds="(\[\d+,\d+\]\[\d+,\d+\])"',
            norm_data
        )
        if match:
            orig_match = re.search(
                rf'(?:text="{re.escape(key)}"|content-desc="{re.escape(key)}").*?bounds="(\[\d+,\d+\]\[\d+,\d+\])"',
                data, re.IGNORECASE
            )
            bounds_str = orig_match.group(1) if orig_match else match.group(2)
            coords = list(map(int, re.findall(r"\d+", bounds_str)))
            x = (coords[0] + coords[2]) // 2
            y = (coords[1] + coords[3]) // 2
            adb(f"shell input tap {x} {y}")
            print(f"👆 Tapped: '{key}' — bullseye!")
            return True
    return False

def find_and_tap_any(keywords):
    data = dump_ui()
    root = ET.fromstring(data)
    for node in root.iter():
        node_text = normalize_text(node.attrib.get("text", ""))
        for key in keywords:
            if normalize_text(key) in node_text:
                coords = list(map(int, re.findall(r'\d+', node.attrib["bounds"])))
                x = (coords[0] + coords[2]) // 2
                y = (coords[1] + coords[3]) // 2
                adb(f"shell input tap {x} {y}")
                print(f"🎯 Nailed it: '{node.attrib.get('text', '')}' — tapped!")
                return True
    return False

def find_and_tap_next_imei(last_text=None):
    data = dump_ui()
    root = ET.fromstring(data)
    for node in root.iter():
        text = node.attrib.get("text", "").lower()
        if text.startswith("imei") and text != (last_text or "").lower():
            coords = list(map(int, re.findall(r'\d+', node.attrib["bounds"])))
            x = (coords[0] + coords[2]) // 2
            y = (coords[1] + coords[3]) // 2
            adb(f"shell input tap {x} {y}")
            print(f"📲 IMEI acquired: '{node.attrib.get('text', '')}' — tapped!")
            return node.attrib.get("text")
    return None

def scroll_and_tap(keywords, max_scrolls=6):
    for _ in range(max_scrolls):
        if find_and_tap_any(keywords):
            return True
        swipe_up()
        time.sleep(2)
    print(f"❌ {keywords} vanished after {max_scrolls} scroll attempts — it was never there 👻")
    return False

# =============================================
# ========== DIALER ===========================
# =============================================

# Fallback guess for Phone-app package names (used only if we could not
# learn the launcher's packages before tapping the Phone icon)
PHONE_PKG_RE = re.compile(r"dialer|contacts|phone|telecom|incallui", re.I)

# content-desc words we must NEVER tap while hunting for the keypad icon
BAD_DESC_RE = re.compile(
    r"call|voice|video|delete|backspace|search|more|menu|setting|option|"
    r"contact|recent|favou?rite|history|mic|back|close|add|new|message",
    re.I,
)

_launcher_pkgs = set()   # packages on screen BEFORE the Phone icon was tapped


def _is_phone_pkg(pkg):
    if not pkg or "systemui" in pkg:
        return False
    if _launcher_pkgs:
        return pkg not in _launcher_pkgs     # anything new after the tap = the Phone app
    return bool(PHONE_PKG_RE.search(pkg))


def get_ui_nodes():
    """Dump UI and return a flat list of nodes with parsed bounds."""
    try:
        root = ET.fromstring(dump_ui())
    except ET.ParseError:
        return []
    nodes = []
    for n in root.iter("node"):
        nums = re.findall(r"\d+", n.attrib.get("bounds", ""))
        if len(nums) != 4:
            continue
        l, t, r, b = map(int, nums)
        if r <= l or b <= t:
            continue
        nodes.append({
            "text": n.attrib.get("text", ""),
            "desc": n.attrib.get("content-desc", ""),
            "rid": n.attrib.get("resource-id", ""),
            "cls": n.attrib.get("class", ""),
            "pkg": n.attrib.get("package", ""),
            "clickable": n.attrib.get("clickable") == "true",
            "b": (l, t, r, b),
            "cx": (l + r) // 2,
            "cy": (t + b) // 2,
        })
    return nodes


def is_dialpad_open(nodes):
    """Dial pad is open when the digit keys 0-9 are on screen.
    Works for text="5" and content-desc="5" / "5,JKL" styles - no language needed."""
    digits = set()
    for n in nodes:
        for s in (n["text"], n["desc"]):
            m = re.fullmatch(r"([0-9])(?:[,\n].*)?", s.strip(), re.S)
            if m:
                digits.add(m.group(1))
    return len(digits) >= 8


def find_dialpad_icon_candidates(nodes):
    """Icon-like nodes that could be the keypad button, best guess first.

    Tier 3   : resource-id / content-desc says dialpad / keypad
    Tier 2   : floating action button (FAB) in lower half
    Tier 1.5 : lone clickable icon in the bottom strip (FAB with no id)
    Tier 1   : icon-only bottom tab bar -> leftmost first (Samsung style)
    """
    nodes = [n for n in nodes if "systemui" not in n["pkg"]]
    if not nodes:
        return []
    W = max(n["b"][2] for n in nodes)
    H = max(n["b"][3] for n in nodes)

    scored = []
    strip = []  # generic clickable icons in the bottom strip

    for n in nodes:
        l, t, r, b = n["b"]
        w, h = r - l, b - t
        rid = n["rid"].lower()
        desc = n["desc"].strip()
        cls = n["cls"].lower()

        # explicit hint - but skip big containers/fragments (not the button itself)
        if (re.search(r"dial\s*_?pad|key\s*_?pad", rid) or re.search(r"dial\s*_?pad|key\s*_?pad", desc, re.I)) \
                and not re.search(r"container|fragment|layout|root|pager|frame", rid) \
                and w <= W * 0.5 and h <= H * 0.2:
            scored.append((3, 0, n))
            continue

        if ("fab" in rid or "floating" in rid or "floatingactionbutton" in cls) \
                and n["cy"] > H * 0.5 and not BAD_DESC_RE.search(desc):
            scored.append((2, 0, n))
            continue

        if (n["clickable"] and not n["text"].strip()
                and n["cy"] > H * 0.72
                and w <= W * 0.30 and h <= H * 0.15
                and not BAD_DESC_RE.search(desc)
                and re.search(r"image|button|view", cls)):
            strip.append(n)

    rows = {}
    for n in strip:
        rows.setdefault(round(n["cy"] / 60), []).append(n)
    for row in rows.values():
        if len(row) == 1:
            scored.append((1.5, 0, row[0]))
        else:
            for i, n in enumerate(sorted(row, key=lambda x: x["cx"])):
                scored.append((1, i, n))   # leftmost first

    scored.sort(key=lambda s: (-s[0], s[1]))
    return [s[2] for s in scored]


def wait_phone_foreground(timeout=6):
    """Wait until the Phone app is on screen. Returns its UI nodes, or None."""
    end = time.time() + timeout
    while True:
        nodes = get_ui_nodes()
        if any(_is_phone_pkg(n["pkg"]) for n in nodes):
            return nodes
        if time.time() >= end:
            return None
        time.sleep(1.5)


def open_dialpad_by_icon(max_taps=4):
    """Open the keypad by tapping its icon. Verifies after every tap."""
    nodes = wait_phone_foreground()
    if nodes is None:
        return False

    tried = set()
    for _ in range(max_taps):
        if is_dialpad_open(nodes):
            return True

        cands = [c for c in find_dialpad_icon_candidates(nodes)
                 if _is_phone_pkg(c["pkg"]) and (c["cx"], c["cy"]) not in tried]
        if not cands:
            break

        c = cands[0]
        tried.add((c["cx"], c["cy"]))
        adb(f"shell input tap {c['cx']} {c['cy']}")
        time.sleep(1.2)
        nodes = get_ui_nodes()

    if is_dialpad_open(nodes):
        return True

    # last chance: this model may label the keypad after all
    if tap_by_keywords(KEYWORDS["dialpad"]):
        time.sleep(1.2)
        return is_dialpad_open(get_ui_nodes())
    return False


def open_phone_and_dialpad():
    global _launcher_pkgs
    print("📞 Launching the Phone app — time to dial in...")

    # Open Phone app by command (what the call key does, but it can never place a call)
    adb("shell input keyevent 3")
    time.sleep(1)
    _launcher_pkgs = {n["pkg"] for n in get_ui_nodes()}   # apps on screen now (launcher)
    adb("shell am start -a android.intent.action.CALL_BUTTON")
    time.sleep(2)
    # Open keypad: tap its icon
    if open_dialpad_by_icon():
        print("🔢 Dial pad opened — ready to punch in codes!")
        return True

    # Fallback 1: tap the Phone icon by label
    adb("shell input keyevent 3")
    time.sleep(1)
    _launcher_pkgs = {n["pkg"] for n in get_ui_nodes()}
    if tap_by_keywords(KEYWORDS["phone_app"]):
        time.sleep(2)
        if open_dialpad_by_icon():
            print("🔢 Dial pad opened — ready to punch in codes!")
            return True

    # Fallback 2: DIAL intent
    _launcher_pkgs = set()
    adb("shell input keyevent 3")
    time.sleep(1)
    adb("shell am start -a android.intent.action.DIAL")
    time.sleep(2)
    if open_dialpad_by_icon():
        print("🔢 Dial pad opened via fallback — ready to punch in codes!")
        return True

    print("⚠️  Could not confirm dial pad is open — continuing anyway")
    return True


def dial_number(sequence):
    print(f"📡 Dialling secret code: {sequence}")
    data = dump_ui()
    for ch in sequence:
        keys = ["*", "star"] if ch == "*" else ["#", "hash"] if ch == "#" else [ch]
        for key in keys:
            match = re.search(
                rf'(text="{re.escape(key)}"|content-desc="{re.escape(key)}").*?bounds="(\[\d+,\d+\]\[\d+,\d+\])"',
                data, re.IGNORECASE
            )
            if match:
                coords = list(map(int, re.findall(r"\d+", match.group(2))))
                x = (coords[0] + coords[2]) // 2
                y = (coords[1] + coords[3]) // 2
                adb(f"shell input tap {x} {y}")
                break
    # Dial only — do NOT tap Call/Send

# =============================================
# ========== PHONE INFO =======================
# =============================================

def find_and_tap_phone_info(keywords):
    """Tap 'Phone information' but NEVER 'Phone information V2'.
    Prefers an exact label match, then a partial match; anything with 'V2' is skipped."""
    root = ET.fromstring(dump_ui())
    keys = [normalize_text(k).strip() for k in keywords]
    exact = partial = None
    for node in root.iter():
        text = normalize_text(node.attrib.get("text", "")).strip()
        if not text or "bounds" not in node.attrib:
            continue
        if re.search(r"\bv\s*2\b", text):          # skip "Phone information V2"
            continue
        if text in keys:
            if exact is None:
                exact = node
        elif any(k in text for k in keys):
            if partial is None:
                partial = node
    node = exact if exact is not None else partial
    if node is None:
        return False
    coords = list(map(int, re.findall(r"\d+", node.attrib["bounds"])))
    x = (coords[0] + coords[2]) // 2
    y = (coords[1] + coords[3]) // 2
    adb(f"shell input tap {x} {y}")
    print(f"🎯 Nailed it: '{node.attrib.get('text', '')}' — tapped!")
    return True

def open_phone_info():
    print("📋 Searching for 'Phone information' — digging into device secrets...")
    for _ in range(5):
        if find_and_tap_phone_info(KEYWORDS["phone_info"]):
            time.sleep(3)
            return True
        swipe_up()
        time.sleep(2)
    print("❌ 'Phone information' is MIA — couldn't locate it!")
    return False

def open_sim_dropdown():
    print("📟 Popping open SIM dropdown (Phone 0)...")
    return find_and_tap_any(KEYWORDS["phone_0"])

def switch_to_phone1():
    print("🔄 Flipping over to Phone 1...")
    return find_and_tap_any(KEYWORDS["phone_1"])

def scroll_capture():
    """Scrolls and captures only unique screens — skips duplicate frames."""
    global last_hash, repeat_count
    last_hash = None
    repeat_count = 0
    print("🎞️  Scroll-capture engaged — hunting for new content...")
    swipe_up()
    time.sleep(2)
    while True:
        time.sleep(1)
        adb("shell screencap -p /sdcard/temp.png")
        temp_file = f"{SAVE_DIR}/temp_scroll.png"
        adb(f"pull /sdcard/temp.png {temp_file}")
        adb("shell rm /sdcard/temp.png")

        current_hash = get_center_hash(temp_file)

        if current_hash == last_hash:
            repeat_count += 1
            os.remove(temp_file)
            print("🔁 Same frame detected — skipping the déjà vu screenshot")
        else:
            repeat_count = 0
            last_hash = current_hash
            capture_screen(temp_file)

        if repeat_count >= 1:
            print("🏁 Bottom of the scroll — nothing new under the sun!")
            break

        swipe_up()
        time.sleep(2)

# =============================================
# ========== SETTINGS / ABOUT / IMEI ==========
# =============================================

def open_settings_app():
    print("⚙️  Launching Settings — going deep into the matrix...")
    adb("shell input keyevent 3")
    time.sleep(1)
    adb("shell input keyevent 176")
    time.sleep(3)

def open_about_phone():
    print("🔎 Hunting for 'About phone'...")
    scroll_n(2)
    for _ in range(6):
        if find_and_tap_any(KEYWORDS["about_phone"]):
            time.sleep(2)
            return True
        swipe_up()
        time.sleep(2)
    print("❌ 'About phone' pulled a Houdini — not found after all scrolls!")
    return False

def open_and_capture(name):
    print(f"🗂️  Stepping into '{name}'...")
    if find_and_tap_any(KEYWORDS["model"]):
        time.sleep(2)
        capture_screen()
        return True
    print(f"❌ '{name}' is playing hide and seek — not found!")
    return False

def capture_device_details(scroll_check=True):
    print("🧾 Cracking open 'Device details'...")
    # First use the existing direct lookup. Some devices show this row below
    # the fold, so scroll the About phone page only when the direct lookup fails.
    details_found = find_and_tap_any(KEYWORDS["device_details"])
    if not details_found:
        for scroll_attempt in range(3):
            print(f"↕️ Device details not visible; scrolling About phone (attempt {scroll_attempt + 1}/3)...")
            swipe_up()
            time.sleep(2)
            if find_and_tap_any(KEYWORDS["device_details"]):
                details_found = True
                break
    if details_found:
        time.sleep(2)
        capture_screen()
        if scroll_check:
            print("🔭 Peeking below the fold for bonus content...")
            swipe_up()
            time.sleep(2)
            adb("shell screencap -p /sdcard/temp.png")
            temp_file = f"{SAVE_DIR}/temp_scroll.png"
            adb(f"pull /sdcard/temp.png {temp_file}")
            adb("shell rm /sdcard/temp.png")

            current_hash = get_center_hash(temp_file)
            last_file = f"{SAVE_DIR}/{PHASE}_{screenshot_index - 1:03}.png"
            prev_hash = get_center_hash(last_file)

            if current_hash != prev_hash:
                capture_screen(temp_file)
            else:
                os.remove(temp_file)
                print("🙅 No new content below — skipping redundant screenshot")
        return True
    print("❌ 'Device details' went ghost — not found!")
    return False

# =============================================
# ========== OTA: SYSTEM UPDATE ===============
# =============================================

def open_system_updates():
    print("🌐 Sniffing out System Updates...")
    r = adb("shell am start -a android.settings.SYSTEM_UPDATE_SETTINGS")
    if "unable to resolve" in ((r.stdout or "") + (r.stderr or "")).lower():
        print("❌ This phone can't open System update by command!")
        return False
    time.sleep(3)
    return True

def wait_for_restart_and_reboot(timeout=1800, already_triggered=False):
    global screenshot_index
    # If restart was already tapped in the early-poll phase, just wait for reboot
    if already_triggered:
        print("🔁 Restart already triggered — rebooting device now...")
        adb("shell reboot")
        dest = f"{SAVE_DIR}/{PHASE}_{screenshot_index:03}.png"
        shutil.copy(RESTART_IMG, dest)
        print(f"🖼️  Restart image slotted in: {PHASE}_{screenshot_index:03}.png")
        screenshot_index += 1
        return True

    print("👀 Eyeing the screen for a 'Restart now' button...")
    start = time.time()
    printed = False

    while True:
        data = dump_ui()
        root = ET.fromstring(data)
        for node in root.iter():
            text = normalize_text(node.attrib.get("text", ""))
            desc = normalize_text(node.attrib.get("content-desc", ""))
            if any(normalize_text(kw) in text or normalize_text(kw) in desc for kw in KEYWORDS["restart"]):
                print("🔁 'Restart' spotted — screenshotting and pulling the trigger!")
                capture_screen()
                adb("shell reboot")

                dest = f"{SAVE_DIR}/{PHASE}_{screenshot_index:03}.png"
                shutil.copy(RESTART_IMG, dest)
                print(f"🖼️  Restart image slotted in: {PHASE}_{screenshot_index:03}.png")
                screenshot_index += 1
                return True

        if not printed:
            print("🕵️  'Restart' not yet visible — polling quietly in the background...")
            printed = True
        if time.time() - start > timeout:
            print("⏰ Timed out! 'Restart now' never showed up — giving up the wait!")
            capture_screen()
            return False
        time.sleep(10)

def clear_recent_apps():
    print("🧹 Sweeping out the recent apps — spring cleaning time!")
    adb("shell input keyevent 187")
    time.sleep(2)
    if find_and_tap_any(KEYWORDS["clear_all"]):
        print("✅ Recent apps wiped clean — fresh slate!")
    else:
        print("🤷 'Clear all' wasn't there — skipping the sweep")

# =============================================
# ========== SHARED: STEPS 1 & 2 =============
# =============================================

def run_dialer_and_imei_steps():
    """Reusable Steps 1 & 2 — called by PreSoak flow and again after OTA completes.
    screenshot_index continues from wherever it currently is."""

    # --- STEP 1: Dialer Codes ---
    print("\n📞 ━━━━━━━━━━ Dialer Codes ━━━━━━━━━━")
    if open_phone_and_dialpad():
        dial_number("*#*#78287#*#*")
        time.sleep(0.5)
        capture_screen()

    go_back()
    dial_number("*#*#4636#*#*")

    if open_phone_info():
        capture_screen()
        scroll_capture()

    go_back()
    if open_phone_info():
        if open_sim_dropdown():
            if switch_to_phone1():
                time.sleep(2)
                capture_screen()
                scroll_capture()
            else:
                print("ℹ️  Phone 1 not in the picture — rolling on without it...")
        else:
            print("❌ SIM dropdown nowhere to be found — pushing forward anyway...")

    # --- STEP 2: Settings / About Phone / IMEI ---
    print("\n⚙️ ━━━━━━━━━━ Settings ━━━━━━━━━━")
    open_settings_app()

    if not open_about_phone():
        print("❌ 'About phone' is AWOL — skipping entire IMEI section!")
    else:
        swipe_up()
        time.sleep(2)
        capture_screen()

        if capture_device_details(scroll_check=True):
            go_back()
            details_found = find_and_tap_any(KEYWORDS["device_details"])
            if not details_found:
                for scroll_attempt in range(3):
                    print(f"↕️ Device details not visible; scrolling About phone (attempt {scroll_attempt + 1}/3)...")
                    swipe_up()
                    time.sleep(2)
                    if find_and_tap_any(KEYWORDS["device_details"]):
                        details_found = True
                        break
            if details_found:
                time.sleep(2)
                open_and_capture("Model")
                go_back()
                if find_and_tap_any(KEYWORDS["android_ver"]):
                    time.sleep(2)
                    capture_screen()
                    go_back()
                    print("⏳ Pausing 2s before cruising to the IMEI section...")
                    time.sleep(2)
                    swipe_up()
                    time.sleep(2)

                    print("🔍 Seeking SIM 1 IMEI — first identity, please...")
                    imei1_text = find_and_tap_next_imei()
                    if imei1_text:
                        time.sleep(2)
                        capture_screen()
                        go_back()
                    else:
                        print("❌ IMEI SIM slot 1 is a ghost — aborting IMEI section!")
                        return

                    print("🔍 Seeking SIM 2 / eSIM IMEI — second identity incoming...")
                    imei2_text = find_and_tap_next_imei(last_text=imei1_text)
                    if imei2_text:
                        time.sleep(2)
                        capture_screen()
                        go_back(times=2)
                    else:
                        print("ℹ️  SIM slot 2 / eSIM not present — backing out once")
                        go_back(times=1)

                    print("⏳ Waiting 2s before opening Device identifiers...")
                    time.sleep(2)
                    if find_and_tap_any(KEYWORDS["device_ids"]):
                        time.sleep(2)
                        capture_screen()
                    else:
                        print("❌ 'Device identifiers' is playing hard to get!")

# =============================================
# ========== PRESOAK MAIN =====================
# =============================================

def run_script1():
    global SAVE_DIR
    print("\n🧪 ━━━━━━━━━━ PRESOAK START ━━━━━━━━━━")
    SAVE_DIR = get_device_info()
    run_dialer_and_imei_steps()

def wait_for_update_notification(timeout=1800):
    """After reboot: pull down notification shade and poll for an
    update-complete style notification. Once found, tap it, screenshot, return."""
    print("🔔 Pulling down notification bar — watching for update-complete notification...")

    start = time.time()
    while True:
        # Expand notification shade
        adb("shell cmd statusbar expand-notifications")
        time.sleep(2)

        # Dump UI and scan for update notification
        data = dump_ui()
        root = ET.fromstring(data)
        for node in root.iter():
            text = normalize_text(node.attrib.get("text", "").strip())
            desc = normalize_text(node.attrib.get("content-desc", "").strip())
            for kw in KEYWORDS["update_done"]:
                if normalize_text(kw) in text or normalize_text(kw) in desc:
                    print(f"🎉 Update notification spotted: '{node.attrib.get('text', '') or node.attrib.get('content-desc', '')}'")
                    coords = list(map(int, re.findall(r'\d+', node.attrib["bounds"])))
                    x = (coords[0] + coords[2]) // 2
                    y = (coords[1] + coords[3]) // 2
                    adb(f"shell input tap {x} {y}")
                    time.sleep(2)
                    capture_screen()
                    return True

        if time.time() - start > timeout:
            print("⏰ Timed out waiting for update notification!")
            capture_screen()
            return False
        time.sleep(10)


# =============================================
# ========== OTA UPDATE MAIN ==================
# =============================================

def run_script2():
    print("\n🛰️  ━━━━━━━━━━ OTA UPDATE START ━━━━━━━━━━")

    print("📶 Flipping Wi-Fi on — connecting to the mothership...")
    adb("shell svc wifi enable")
    time.sleep(2)

    if not open_system_updates():
        print("❌ System Updates not found. Ejecting from OTA flow!")
        return

    # ── 1-minute screenshot ──────────────────────────────────────────────────
    print("⏳ Sitting tight for 1 minute — update train is leaving the station...")
    time.sleep(60)
    capture_screen()

    # ── 2-minute screenshot ──────────────────────────────────────────────────
    print("⏳ Waiting 2 more minutes — update is chugging along...")
    time.sleep(120)
    capture_screen()

    print("⏳ Waiting 5 more minutes — almost there, probably...")
    # ── 2–5 min: poll for restart every 10 s in the background ──────────────
    # If restart appears before 5 min → screenshot + tap it and continue
    # If not found by 5 min → take 5-min screenshot, then keep waiting normally
    poll_start = time.time()
    restart_found_early = False
    while time.time() - poll_start < 180:          # 180 s = remaining 3 min
        data = dump_ui()
        root = ET.fromstring(data)
        for node in root.iter():
            txt  = normalize_text(node.attrib.get("text", ""))
            desc = normalize_text(node.attrib.get("content-desc", ""))
            if any(normalize_text(kw) in txt or normalize_text(kw) in desc for kw in KEYWORDS["restart"]):
                print("🔁 Restart appeared early — capturing!")
                capture_screen()
                restart_found_early = True
                break
        if restart_found_early:
            break
        time.sleep(10)

    if not restart_found_early:
        # 5-min mark screenshot
        capture_screen()

    if not wait_for_restart_and_reboot(already_triggered=restart_found_early):
        print("❌ Reboot never triggered. Ejecting from OTA flow!")
        return

    wait_for_device()

    unlock_device()

    # ── Pull down notification bar and wait for update-complete notification ──
    if not wait_for_update_notification():
        print("❌ Update notification never appeared. Ejecting from OTA flow!")
        return

    print("🏠 Heading back to home screen — mission (almost) accomplished!")
    adb("shell input keyevent 3")
    time.sleep(2)

    clear_recent_apps()

# =============================================
# ========== POST-FLOW: POWER & DEVICE ID ===
# =============================================

def run_post_flow():
    """Final flow after all scripts complete:
    Home → Power menu screenshot → Reboot → Restart image → Unlock →
    Settings → About phone → Device identifiers screenshot → Home."""
    global screenshot_index

    # Step 1: Go to home screen
    print("\n🏁 ━━━━━━━━━━ POST FLOW ━━━━━━━━━━")
    print("🏠 Navigating to home screen — base camp first!")
    adb("shell input keyevent 3")
    time.sleep(5)

    # Step 2: Trigger power/restart interface (power btn + vol up)
    print("⚡ Conjuring the power menu...")
    adb('shell "input keyevent 26 & input keyevent 24"')
    time.sleep(2)

    # Step 3: Screenshot of power menu
    capture_screen()

    # Step 4: Reboot device
    print("🔁 Sending the device for a quick nap (reboot)...")
    adb("shell reboot")

    # Step 5: Copy external Restart image into sequence
    dest = f"{SAVE_DIR}/{PHASE}_{screenshot_index:03}.png"
    shutil.copy(RESTART_IMG, dest)
    print(f"🖼️  Restart image slotted in: {PHASE}_{screenshot_index:03}.png")
    screenshot_index += 1

    # Step 6: Wait for device and unlock
    wait_for_device()
    unlock_device()

    # Step 7: Open Settings
    open_settings_app()

    # Step 8: Scroll and find About phone
    if not open_about_phone():
        print("❌ 'About phone' MIA — skipping Device identifiers!")
        adb("shell input keyevent 3")
        return

    # Step 9: Scroll down inside About phone to find Device identifiers
    print("🔎 Hunting for 'Device identifiers' in About phone...")
    swipe_up()
    time.sleep(2)
    if find_and_tap_any(KEYWORDS["device_ids"]):
        time.sleep(2)
        capture_screen()
    else:
        print("❌ 'Device identifiers' gave us the slip!")

    # Step 10: Back to home screen
    print("🏠 Wrapping up — heading home!")
    adb("shell input keyevent 3")
    time.sleep(2)

# =============================================
# ========== ENTRY POINT ======================
# =============================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Axon — Automated Device Testing Framework")
    parser.add_argument("--serial", required=True, help="Device serial number")
    parser.add_argument("--lang", default="EN", help="Language code (EN, ES, PT_BR)")
    parser.add_argument(
        "--restart-img",
        default=None,
        help="Path to Restart.png image (overrides AXON_RESTART_IMG env var)"
    )
    parser.add_argument(
        "--save-root",
        default=None,
        help="Root directory to store the PreSoak result folder in (defaults to the current directory)"
    )
    args = parser.parse_args()

    TARGET_SERIAL = args.serial
    lang_code = args.lang
    SAVE_ROOT = args.save_root
    if SAVE_ROOT:
        os.makedirs(SAVE_ROOT, exist_ok=True)

    # ── Load keywords for the selected language ───────────────────────────────
    KEYWORDS = get_keywords(lang_code)

    # Resolve and set RESTART_IMG globally
    RESTART_IMG = args.restart_img or os.environ.get("AXON_RESTART_IMG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "Restart.png")
    if not os.path.isfile(RESTART_IMG):
        print(f"⚠️  Restart image not found at: {RESTART_IMG}")
        print("   Set --restart-img <path> or export AXON_RESTART_IMG=<path>")
        exit(1)
    print(f"🖼️  Restart image loaded from: {RESTART_IMG}")

    # ── Main test sequence ────────────────────────────────────────────────────
    run_script1()
    run_script2()
    run_dialer_and_imei_steps()
    #run_post_flow()
    print(f"\n🧪 ━━━━━━━━━━ [{TARGET_SERIAL}] PRESOAK COMPLETED ━━━━━━━━━━")
    print(f"\n🥳 ━━━━  [{TARGET_SERIAL}] AXON SIGNING OFF — GREAT SUCCESS!  ━━━━ 🎊")
