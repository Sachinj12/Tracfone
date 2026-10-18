#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tracfone - Scenario 6 (evidence builder, NO device needed)

Scenario 6 does not run anything on the phone.  After Scenarios 3, 4, 2A and
2B have been created, it copies their finished screenshots into a separate
"Scenario 6" folder inside the evidence root:

    Scenario 6/
      Scenario 6AA Source to Target WiFi/     <- same screenshots as Scenario 3   (6AA (n).png)
      Scenario 6AB Target to Dummy WiFi/      <- same screenshots as Scenario 2A  (6AB (n).png)
      Scenario 6BA Source to Target Data/     <- same screenshots as Scenario 4   (6BA (n).png)
      Scenario 6BB Target to Dummy Data/      <- same screenshots as Scenario 2B  (6BB (n).png)

This file is deliberately SEPARATE: it imports nothing from the other backends,
never touches the original Scenario 3 / 4 / 2A / 2B folders (read-only copy),
and is only started by the Scenario 6 button of the UI.
"""
import argparse
import os
import re
import shutil
import sys

SCENARIO6_FOLDER = "Scenario 6"

# (new code, new folder, source folder, source file prefix)
SOURCES = [
    ("6AA", "Scenario 6AA Source to Target WiFi", "Scenario 3 Source to Target WiFi",  "3"),
    ("6AB", "Scenario 6AB Target to Dummy WiFi",  "Scenario 2A Target to Dummy WiFi",   "2 A"),
    ("6BA", "Scenario 6BA Source to Target Data", "Scenario 4 Source to Target Data",   "4"),
    ("6BB", "Scenario 6BB Target to Dummy Data",  "Scenario 2B Target to Dummy Data",   "2B"),
]


def _numbered_files(folder, prefix):
    """Return [(number, filename)] for '<prefix> (n).png' files, sorted by number."""
    pat = re.compile(r"^" + re.escape(prefix) + r" \((\d+)\)\.png$", re.IGNORECASE)
    found = []
    for name in os.listdir(folder):
        m = pat.match(name)
        if m:
            found.append((int(m.group(1)), name))
    return sorted(found)


def _latest_evidence_folder(save_root):
    """Evidence now lives in <root>/<barcode>/Scenario ... folders.  Pick the folder (a barcode folder, or the
    root itself for old-style layouts) that holds the most recent screenshots; fall back to the root."""
    def activity(d):
        newest = 0
        try:
            for e in os.listdir(d):
                sub = os.path.join(d, e)
                if e.startswith("Scenario ") and os.path.isdir(sub):
                    for dp, _dn, fs in os.walk(sub):
                        for f in fs:
                            newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
        except OSError:
            pass
        return newest

    cands = []
    a = activity(save_root)
    if a:
        cands.append((a, save_root))
    for e in os.listdir(save_root):
        p = os.path.join(save_root, e)
        if os.path.isdir(p) and not e.startswith(("Scenario", "_")):
            a = activity(p)
            if a:
                cands.append((a, p))
    return max(cands, key=lambda c: c[0])[1] if cands else save_root


def build_scenario6(save_root):
    """Copy the finished Scenario 3 / 2A / 4 / 2B evidence into Scenario 6.  Returns True if all four were built."""
    save_root = os.path.abspath(save_root)
    out_root = os.path.join(save_root, SCENARIO6_FOLDER)
    all_ok = True
    built = 0

    print(f"📁 Evidence root : {save_root}")
    print(f"📁 Scenario 6    : {out_root}")

    for code, new_folder, src_folder, prefix in SOURCES:
        src_dir = os.path.join(save_root, src_folder)
        if not os.path.isdir(src_dir):
            print(f"⚠️ Scenario {code}: source folder not found -> '{src_folder}'. Run that scenario first.")
            all_ok = False
            continue
        files = _numbered_files(src_dir, prefix)
        if not files:
            print(f"⚠️ Scenario {code}: no '{prefix} (n).png' screenshots in '{src_folder}'. Run that scenario first.")
            all_ok = False
            continue

        dst_dir = os.path.join(out_root, new_folder)
        os.makedirs(dst_dir, exist_ok=True)
        # Re-running Scenario 6 refreshes only its own old '<code> (n).png' files.
        for _n, old in _numbered_files(dst_dir, code):
            os.remove(os.path.join(dst_dir, old))

        for number, name in files:
            shutil.copy2(os.path.join(src_dir, name), os.path.join(dst_dir, f"{code} ({number}).png"))
        built += 1
        print(f"✅ Scenario {code}: {len(files)} screenshots copied from '{src_folder}' -> '{new_folder}'")

    if built:
        print(f"📂 Scenario 6 folder ready: {out_root}")
    if all_ok:
        print("✅ Scenario 6 complete (6AA, 6AB, 6BA, 6BB).")
    else:
        print("⚠️ Scenario 6 is incomplete - create the missing scenarios above, then click Scenario 6 again.")
    return all_ok


def main():
    p = argparse.ArgumentParser(description="Tracfone Scenario 6: build 6AA / 6AB / 6BA / 6BB from Scenario 3 / 2A / 4 / 2B evidence")
    p.add_argument("--save-root", default=None, help="Evidence root (the folder that holds the Scenario folders)")
    a = p.parse_args()

    save_root = a.save_root or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
    if not os.path.isdir(save_root):
        print(f"⚠️ Evidence root not found: {os.path.abspath(save_root)}")
        sys.exit(1)
    save_root = _latest_evidence_folder(os.path.abspath(save_root))
    sys.exit(0 if build_scenario6(save_root) else 1)


if __name__ == "__main__":
    main()
