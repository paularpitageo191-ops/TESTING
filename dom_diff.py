#!/usr/bin/env python3
"""
dom_diff.py — UC-4: DOM Snapshot Diff
======================================
Compares the two most recent live_dom_elements JSON snapshots for a project
and categorises changes into three buckets:

  selector_drift  — element exists but id/selector changed
                    → selector_healer.py
  structural      — element disappeared or tag/role changed
                    → step_generator re-run flag
  new_elements    — new elements added
                    → informational only

Outputs
───────
  docs/{PROJECT}_dom_diff_{timestamp}.json  — full diff report
  stdout                                     — summary

Usage
─────
  python3 dom_diff.py --project SCRUM-70
  python3 dom_diff.py --project SCRUM-70 --old snap1.json --new snap2.json
"""

from __future__ import annotations

import os
import re
import json
import glob
import argparse
import datetime
import sqlite3
from typing import Dict, List, Tuple, Optional

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB   = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DOCS_DIR     = os.path.join(PROJECT_ROOT, "docs")


# ══════════════════════════════════════════════════════════════════════════════
# §1  SNAPSHOT LOADER
# ══════════════════════════════════════════════════════════════════════════════

def find_snapshots(project_key: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (older, newer) snapshot paths for this project."""
    patterns = [
        os.path.join(DOCS_DIR, f"live_dom_elements_{project_key}_*.json"),
        os.path.join(DOCS_DIR, f"live_dom_elements_{project_key}.json"),
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    found = sorted(set(found), key=os.path.getmtime)
    if len(found) >= 2:
        return found[-2], found[-1]
    if len(found) == 1:
        return None, found[0]
    return None, None


def load_snapshot(path: str) -> Dict[str, Dict]:
    """
    Load snapshot and build a flat dict keyed by a stable element fingerprint.
    Fingerprint = label|placeholder|text|name (lowercased, normalised).
    Value = full element dict.
    """
    with open(path) as f:
        data = json.load(f)

    elements: Dict[str, Dict] = {}
    groups = [
        "input_elements", "button_elements", "textarea_elements",
        "dropdown_elements", "custom_dropdown_elements",
        "output_elements", "display_elements", "text_elements", "elements",
    ]
    for group in groups:
        for el in data.get(group, []):
            fp = _fingerprint(el)
            if fp:
                elements[fp] = {**el, "_group": group}
    return elements


def _fingerprint(el: Dict) -> str:
    """Stable key that survives selector changes but tracks same logical element."""
    parts = [
        str(el.get("label")       or ""),
        str(el.get("placeholder") or ""),
        str(el.get("text")        or ""),
        str(el.get("name")        or ""),
        str(el.get("ariaRole")    or el.get("role") or ""),
    ]
    key = "|".join(p.lower().strip() for p in parts if p.strip())
    return key or str(el.get("id", ""))


def _best_selector(el: Dict) -> str:
    el_id = el.get("id", "")
    if el_id:
        return f"#{el_id}"
    return el.get("selector", el.get("xpath", "unknown"))


# ══════════════════════════════════════════════════════════════════════════════
# §2  DIFF ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def diff_snapshots(old: Dict[str, Dict], new: Dict[str, Dict]) -> Dict:
    """
    Returns:
    {
      "selector_drift":  [ {fingerprint, old_selector, new_selector, element} ],
      "structural":      [ {fingerprint, change_type, old_el, new_el} ],
      "new_elements":    [ {fingerprint, element} ],
      "unchanged":       int
    }
    """
    drift      = []
    structural = []
    added      = []
    unchanged  = 0

    old_keys = set(old.keys())
    new_keys = set(new.keys())

    # Elements in both snapshots — check for selector changes
    for fp in old_keys & new_keys:
        old_el  = old[fp]
        new_el  = new[fp]
        old_sel = _best_selector(old_el)
        new_sel = _best_selector(new_el)

        old_tag  = str(old_el.get("tagName") or old_el.get("type") or "").lower()
        new_tag  = str(new_el.get("tagName") or new_el.get("type") or "").lower()

        if old_tag != new_tag:
            structural.append({
                "fingerprint": fp,
                "change_type": "tag_changed",
                "old_tag":     old_tag,
                "new_tag":     new_tag,
                "old_selector": old_sel,
                "new_selector": new_sel,
                "old_el":      old_el,
                "new_el":      new_el,
            })
        elif old_sel != new_sel:
            drift.append({
                "fingerprint":  fp,
                "old_selector": old_sel,
                "new_selector": new_sel,
                "element":      new_el,
            })
        else:
            unchanged += 1

    # Elements removed
    for fp in old_keys - new_keys:
        structural.append({
            "fingerprint": fp,
            "change_type": "removed",
            "old_selector": _best_selector(old[fp]),
            "new_selector": None,
            "old_el":      old[fp],
            "new_el":      None,
        })

    # New elements
    for fp in new_keys - old_keys:
        added.append({"fingerprint": fp, "element": new[fp]})

    return {
        "selector_drift": drift,
        "structural":     structural,
        "new_elements":   added,
        "unchanged":      unchanged,
    }


# ══════════════════════════════════════════════════════════════════════════════
# §3  REPORT WRITER
# ══════════════════════════════════════════════════════════════════════════════

def write_diff_report(
    diff:        Dict,
    project_key: str,
    old_path:    Optional[str],
    new_path:    str,
) -> str:
    ts       = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(DOCS_DIR, f"{project_key}_dom_diff_{ts}.json")
    report   = {
        "project_key":     project_key,
        "generated_at":    datetime.datetime.utcnow().isoformat(),
        "old_snapshot":    old_path or "none",
        "new_snapshot":    new_path,
        "summary": {
            "selector_drift": len(diff["selector_drift"]),
            "structural":     len(diff["structural"]),
            "new_elements":   len(diff["new_elements"]),
            "unchanged":      diff["unchanged"],
        },
        "diff": diff,
    }
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return out_path


# ══════════════════════════════════════════════════════════════════════════════
# §4  DB — record snapshot
# ══════════════════════════════════════════════════════════════════════════════

def record_snapshot(project_key: str, snap_path: str,
                    element_count: int, db_path: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT INTO dom_snapshots (project_key, snapshot_file, captured_at, element_count)
            VALUES (?, ?, ?, ?)
            """,
            (project_key, snap_path,
             datetime.datetime.utcnow().isoformat(), element_count),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"  ⚠ DB snapshot record failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §5  DISPATCH HEALERS
# ══════════════════════════════════════════════════════════════════════════════

def dispatch_healers(diff: Dict, project_key: str, diff_report_path: str,
                     db_path: str, dry_run: bool) -> None:
    import subprocess

    if diff["selector_drift"]:
        print(f"\n  → Dispatching selector_healer.py "
              f"({len(diff['selector_drift'])} drifted selectors)")
        if not dry_run:
            subprocess.Popen([
                "python3", "selector_healer.py",
                "--project",     project_key,
                "--diff-report", diff_report_path,
                "--db",          db_path,
            ], cwd=PROJECT_ROOT)

    if diff["structural"]:
        print(f"\n  → {len(diff['structural'])} structural change(s) detected.")
        print("    Flagging for step_generator re-run on affected scenarios.")
        removed = [s for s in diff["structural"] if s["change_type"] == "removed"]
        if removed:
            print(f"    ⚠ {len(removed)} element(s) REMOVED — manual review needed:")
            for s in removed[:5]:
                print(f"      - {s['fingerprint'][:60]}  (was {s['old_selector']})")

    if diff["new_elements"]:
        print(f"\n  ℹ {len(diff['new_elements'])} new element(s) added to DOM.")
        print("    No action needed — Qdrant will pick these up on next re-vectorize.")


# ══════════════════════════════════════════════════════════════════════════════
# §6  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-4 DOM Diff")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--old",      default=None, help="Older snapshot path")
    parser.add_argument("--new",      default=None, help="Newer snapshot path")
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    project_key = args.project

    print(f"\n{'='*60}")
    print(f"DOM Diff — {project_key}")
    print(f"{'='*60}")

    if args.old and args.new:
        old_path = args.old
        new_path = args.new
    else:
        old_path, new_path = find_snapshots(project_key)

    if not new_path:
        print(f"  ✗ No DOM snapshots found for {project_key}")
        return

    if not old_path:
        print(f"  ⚠ Only one snapshot found — nothing to diff yet.")
        print(f"    Recording snapshot and exiting.")
        new_snap = load_snapshot(new_path)
        record_snapshot(project_key, new_path, len(new_snap), args.db)
        return

    print(f"  Old snapshot : {os.path.basename(old_path)}")
    print(f"  New snapshot : {os.path.basename(new_path)}")

    old_snap = load_snapshot(old_path)
    new_snap = load_snapshot(new_path)

    print(f"  Old elements : {len(old_snap)}")
    print(f"  New elements : {len(new_snap)}")

    diff = diff_snapshots(old_snap, new_snap)

    print(f"\n  Diff summary:")
    print(f"    Selector drift : {len(diff['selector_drift'])}")
    print(f"    Structural     : {len(diff['structural'])}")
    print(f"    New elements   : {len(diff['new_elements'])}")
    print(f"    Unchanged      : {diff['unchanged']}")

    diff_path = write_diff_report(diff, project_key, old_path, new_path)
    print(f"\n  ✓ Diff report written → {diff_path}")

    record_snapshot(project_key, new_path, len(new_snap), args.db)

    dispatch_healers(diff, project_key, diff_path, args.db, args.dry_run)

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
