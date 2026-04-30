#!/usr/bin/env python3
"""
selector_healer.py — SAFE / HARDENED VERSION
"""
print("🔥 HARDENED HEALER LOADED")
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
# VALIDATION HELPERS (CRITICAL)
# ─────────────────────────────────────────────────────────────

def is_valid_selector(sel: str) -> bool:
    """
    Prevent invalid / dangerous selector injection
    """
    if not sel:
        return False
    if "TODO" in sel:
        return False
    if "/*" in sel:
        return False
    if len(sel.strip()) < 2:
        return False
    return True


# ─────────────────────────────────────────────────────────────
# LOAD DIFF REPORT (SAFE)
# ─────────────────────────────────────────────────────────────

def load_diff_report(project_key: str, explicit_path: Optional[str] = None) -> Dict:
    if explicit_path and os.path.exists(explicit_path):
        with open(explicit_path) as f:
            return json.load(f)

    docs_dir = "docs"
    if not os.path.exists(docs_dir):
        raise FileNotFoundError("docs folder missing")

    files = [f for f in os.listdir(docs_dir) if f.startswith(f"{project_key}_dom_diff")]
    if not files:
        raise FileNotFoundError("No diff report found")

    latest = sorted(files)[-1]
    with open(os.path.join(docs_dir, latest)) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────
# MEMORY (OPTIONAL SAFE STUB)
# ─────────────────────────────────────────────────────────────

def _search_healing_memory(project_key: str, old_selector: str) -> Optional[str]:
    """
    Stub — safe fallback
    Replace with Qdrant later
    """
    return None


# ─────────────────────────────────────────────────────────────
# PATCH SPEC FILE (SAFE CORE)
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
    
        # 🚫 Skip early if selector not present
        if old_sel not in content:
            continue
    
        # 🧠 MEMORY FIRST
        try:
            memory_fix = _search_healing_memory(project_key, old_sel)
            if memory_fix:
                print(f"  🧠 Memory reuse: {old_sel} → {memory_fix}")
                new_sel = memory_fix
                confidence = 1.0
        except Exception:
            pass
    
        # 🚫 HARD STOPS
        if confidence <= 0:
            print(f"  ⚠ Skipping {old_sel} (zero confidence)")
            continue
    
        if not new_sel or "TODO" in new_sel:
            print(f"  ⚠ Skipping placeholder selector: {new_sel}")
            continue
    
        # 🚫 CONFIDENCE GATE
        if confidence < CONFIDENCE_THRESHOLD:
            print(f"  ⚠ Skipping {old_sel} (low confidence={confidence:.2f})")
            continue
    
        # 🚫 FINAL VALIDATION
        if not is_valid_selector(new_sel):
            print(f"  ⚠ Invalid selector skipped: {new_sel}")
            continue
    
        # 🚫 Skip useless replacement
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

    # ── WRITE FILE
    if count > 0:
        with open(spec_file, "w", encoding="utf-8") as f:
            f.write(content)

        _log_heals(heals, spec_file, project_key, db_path)

    return count


# ─────────────────────────────────────────────────────────────
# LOGGING
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
                (project_key, spec_file, old_selector, new_selector, healed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(project_key, spec_file, old, new, ts) for old, new in heals],
        )

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"⚠ Heal logging failed: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN HEALING FLOW
# ─────────────────────────────────────────────────────────────

def heal_all(project_key: str, diff: Dict, db_path: str, dry_run: bool) -> List[str]:

    patched_files = []

    changes = diff.get("changes", {})

    for spec_file, mappings in changes.items():

        if not os.path.exists(spec_file):
            continue

        # Expect: {old_selector: (new_selector, confidence)}
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
# MAIN ENTRY (SAFE)
# ─────────────────────────────────────────────────────────────

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--diff-report")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(f"Selector Healer — {args.project}")
    print("=" * 60)

    # 🔥 SAFE LOAD
    try:
        diff = load_diff_report(args.project, args.diff_report)
    except FileNotFoundError:
        print("  ℹ No DOM diff report found — skipping healing")
        return

    patched = heal_all(args.project, diff, args.db, args.dry_run)

    print(f"\n  Done — patched {len(patched)} files\n")


if __name__ == "__main__":
    main()
