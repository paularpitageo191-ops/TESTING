#!/usr/bin/env python3
"""
selector_healer.py — SELF-LEARNING SAFE HEALER
"""

from __future__ import annotations

import os
import re
import json
import argparse
import datetime
import sqlite3
from typing import Dict, List, Tuple, Optional

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

DEFAULT_DB = "failure_history.sqlite"
CONFIDENCE_THRESHOLD = 0.7

# ─────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────

def is_valid_selector(sel: str) -> bool:
    if not sel:
        return False
    if "TODO" in sel or "PLACEHOLDER" in sel:
        return False
    if "/*" in sel:
        return False
    if len(sel.strip()) < 2:
        return False
    return True


# ─────────────────────────────────────────────────────────────
# 🔥 LEARNING MEMORY (SQLite-based)
# ─────────────────────────────────────────────────────────────

def _search_healing_memory(
    project_key: str,
    old_selector: str,
    db_path: str
) -> Optional[str]:

    try:
        conn = sqlite3.connect(db_path)

        rows = conn.execute(
            """
            SELECT new_selector, success
            FROM healing_log
            WHERE project_key = ? AND old_selector = ?
            ORDER BY healed_at DESC
            """,
            (project_key, old_selector),
        ).fetchall()

        conn.close()

        if not rows:
            return None

        # 🧠 Prefer successful fixes
        for new_sel, success in rows:
            if success == 1:
                return new_sel

        # 🚫 If latest is failed → skip completely
        latest_sel, latest_success = rows[0]
        if latest_success == 0:
            print(f"  ⚠ Known failed fix skipped: {old_selector}")
            return None

        return latest_sel

    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# PATCH SPEC FILE
# ─────────────────────────────────────────────────────────────

def patch_spec_file(
    spec_file: str,
    replacements: Dict[str, Tuple[str, float]],
    project_key: str,
    db_path: str,
) -> int:

    with open(spec_file, encoding="utf-8") as f:
        content = f.read()

    count = 0
    heals = []

    for old_sel, (new_sel, confidence) in replacements.items():

        if old_sel not in content:
            continue

        # 🧠 MEMORY FIRST (SELF-LEARNING)
        memory_fix = _search_healing_memory(project_key, old_sel, db_path)
        if memory_fix:
            print(f"  🧠 Reusing successful fix: {old_sel} → {memory_fix}")
            new_sel = memory_fix
            confidence = 1.0

        # 🚫 HARD STOPS
        if confidence <= 0:
            continue

        if not new_sel or "TODO" in new_sel:
            continue

        if confidence < CONFIDENCE_THRESHOLD:
            continue

        if not is_valid_selector(new_sel):
            continue

        if old_sel == new_sel:
            continue

        print(f"  ✓ Healing: {old_sel} → {new_sel} (conf={confidence:.2f})")

        patterns = [
            rf'locator\(["\']({re.escape(old_sel)})["\']',
            rf'\.locator\(["\']({re.escape(old_sel)})["\']'
        ]

        for pattern in patterns:
            new_content = re.sub(
                pattern,
                lambda m: m.group(0).replace(m.group(1), new_sel),
                content,
            )

            if new_content != content:
                count += 1
                content = new_content
                heals.append((old_sel, new_sel))

    if count > 0:
        with open(spec_file, "w", encoding="utf-8") as f:
            f.write(content)

        _log_heals(heals, spec_file, project_key, db_path)

    return count


# ─────────────────────────────────────────────────────────────
# LOGGING + LEARNING
# ─────────────────────────────────────────────────────────────

def _log_heals(
    heals: List[Tuple[str, str]],
    spec_file: str,
    project_key: str,
    db_path: str,
) -> None:

    try:
        conn = sqlite3.connect(db_path)
        ts = datetime.datetime.utcnow().isoformat()

        conn.executemany(
            """
            INSERT INTO healing_log
            (project_key, spec_file, old_selector, new_selector, healed_at, success)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            [(project_key, spec_file, old, new, ts) for old, new in heals],
        )

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"⚠ Heal logging failed: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN FLOW
# ─────────────────────────────────────────────────────────────

def heal_all(project_key: str, diff: Dict, db_path: str) -> List[str]:

    patched_files = []

    for spec_file, mappings in diff.get("changes", {}).items():

        if not os.path.exists(spec_file):
            continue

        replacements = {}

        for item in mappings:
            old_sel = item.get("old")
            new_sel = item.get("new")
            conf = item.get("confidence", 0.0)

            if old_sel and new_sel:
                replacements[old_sel] = (new_sel, conf)

        count = patch_spec_file(spec_file, replacements, project_key, db_path)

        if count > 0:
            patched_files.append(spec_file)

    return patched_files


# ─────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--diff-report")
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(f"Selector Healer — {args.project}")
    print("=" * 60)

    try:
        diff = load_diff_report(args.project, args.diff_report)
    except Exception:
        print("  ℹ No diff report — skipping healing")
        return

    patched = heal_all(args.project, diff, args.db)

    print(f"\n  Done — patched {len(patched)} files\n")


if __name__ == "__main__":
    main()
