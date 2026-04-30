#!/usr/bin/env python3
"""
selector_healer.py — SELF-LEARNING SAFE HEALER
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

DEFAULT_DB = "failure_history.sqlite"
CONFIDENCE_THRESHOLD = 0.7
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")


def is_valid_selector(selector: str) -> bool:
    candidate = (selector or "").strip()
    if not candidate:
        return False
    if len(candidate) < 2:
        return False
    upper = candidate.upper()
    if "TODO" in upper or "PLACEHOLDER" in upper:
        return False
    if "/*" in candidate or "*/" in candidate:
        return False
    return True


def has_usable_confidence(confidence: float) -> bool:
    return confidence > 0 and confidence >= CONFIDENCE_THRESHOLD


def detect_contradictory_expectations(content: str) -> List[str]:
    positive = set(
        re.findall(
            r'locator\(\s*["\']([^"\']+)["\']\s*\)\s*\.\s*toBeVisible\s*\(',
            content,
        )
    )
    negative = set(
        re.findall(
            r'locator\(\s*["\']([^"\']+)["\']\s*\)\s*\.\s*not\s*\.\s*toBeVisible\s*\(',
            content,
        )
    )
    return sorted(sel for sel in (positive & negative) if is_valid_selector(sel))


def ensure_healing_log_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(healing_log)").fetchall()
        }

        if "success" not in columns:
            conn.execute("ALTER TABLE healing_log ADD COLUMN success INTEGER DEFAULT NULL")

        if "validated" not in columns:
            conn.execute("ALTER TABLE healing_log ADD COLUMN validated INTEGER DEFAULT 0")

        conn.commit()
    finally:
        conn.close()


def _latest_diff_report(project_key: str) -> Optional[str]:
    pattern = os.path.join(DOCS_DIR, f"{project_key}_dom_diff_*.json")
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def load_diff_report(project_key: str, explicit_path: Optional[str]) -> Dict:
    report_path = explicit_path or _latest_diff_report(project_key)
    if not report_path or not os.path.exists(report_path):
        raise FileNotFoundError("diff report not found")

    with open(report_path, encoding="utf-8") as fh:
        payload = json.load(fh)

    if "changes" in payload:
        return payload

    selector_drift = payload.get("diff", {}).get("selector_drift", [])
    changes: Dict[str, List[Dict]] = {}

    for item in selector_drift:
        spec_file = item.get("spec_file")
        old_sel = item.get("old_selector") or item.get("old")
        new_sel = item.get("new_selector") or item.get("new")
        confidence = float(item.get("confidence", 1.0 if new_sel else 0.0))

        if not spec_file or not old_sel or not new_sel:
            continue

        changes.setdefault(spec_file, []).append({
            "old": old_sel,
            "new": new_sel,
            "confidence": confidence,
        })

    return {"changes": changes}


def _search_healing_memory(
    project_key: str,
    old_selector: str,
    db_path: str,
) -> Optional[str]:
    try:
        ensure_healing_log_schema(db_path)
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT new_selector, success
            FROM healing_log
            WHERE project_key = ? AND old_selector = ?
            ORDER BY healed_at DESC, id DESC
            """,
            (project_key, old_selector),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        successful = [
            new_sel for new_sel, success in rows
            if success == 1 and is_valid_selector(new_sel)
        ]
        if successful:
            return successful[0]

        failed = [
            new_sel for new_sel, success in rows
            if success == 0 and is_valid_selector(new_sel)
        ]
        if failed:
            print(f"  ⚠ Known failed fix skipped: {old_selector}")
            return None

        return None
    except Exception:
        return None


def patch_spec_file(
    spec_file: str,
    replacements: Dict[str, Tuple[str, float]],
    project_key: str,
    db_path: str,
) -> int:
    with open(spec_file, encoding="utf-8") as f:
        content = f.read()

    contradictions = detect_contradictory_expectations(content)
    if contradictions:
        print(
            f"  ⚠ Skipping {os.path.basename(spec_file)} — contradictory assertions for: "
            f"{', '.join(contradictions)}"
        )
        return 0

    count = 0
    heals: List[Tuple[str, str]] = []

    for old_sel, (candidate_sel, confidence) in replacements.items():
        if old_sel not in content:
            continue

        new_sel = candidate_sel

        memory_fix = _search_healing_memory(project_key, old_sel, db_path)
        if memory_fix:
            print(f"  🧠 Reusing successful fix: {old_sel} → {memory_fix}")
            new_sel = memory_fix
            confidence = 1.0

        if old_sel == new_sel:
            continue

        if not has_usable_confidence(confidence):
            print(f"  ⚠ Skipping {old_sel} — unusable confidence {confidence:.2f}")
            continue

        if not is_valid_selector(new_sel):
            print(f"  ⚠ Skipping {old_sel} — invalid healed selector: {new_sel}")
            continue

        print(f"  ✓ Healing: {old_sel} → {new_sel} (conf={confidence:.2f})")

        patterns = [
            rf'locator\(["\']({re.escape(old_sel)})["\']',
            rf'\.locator\(["\']({re.escape(old_sel)})["\']',
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


def _log_heals(
    heals: List[Tuple[str, str]],
    spec_file: str,
    project_key: str,
    db_path: str,
) -> None:
    if not heals:
        return

    try:
        ensure_healing_log_schema(db_path)
        conn = sqlite3.connect(db_path)
        ts = datetime.datetime.now(datetime.UTC).isoformat()

        conn.executemany(
            """
            INSERT INTO healing_log
            (project_key, spec_file, old_selector, new_selector, healed_at, success, validated)
            VALUES (?, ?, ?, ?, ?, NULL, 0)
            """,
            [(project_key, spec_file, old, new, ts) for old, new in heals],
        )

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠ Heal logging failed: {e}")


def heal_all(project_key: str, diff: Dict, db_path: str) -> List[str]:
    patched_files = []
    ensure_healing_log_schema(db_path)

    for spec_file, mappings in diff.get("changes", {}).items():
        resolved_spec = spec_file
        if not os.path.isabs(resolved_spec):
            resolved_spec = os.path.join(PROJECT_ROOT, spec_file)

        if not os.path.exists(resolved_spec):
            continue

        replacements: Dict[str, Tuple[str, float]] = {}
        for item in mappings:
            old_sel = item.get("old")
            new_sel = item.get("new")
            conf = float(item.get("confidence", 0.0))
            if old_sel and new_sel:
                replacements[old_sel] = (new_sel, conf)

        count = patch_spec_file(resolved_spec, replacements, project_key, db_path)
        if count > 0:
            patched_files.append(resolved_spec)

    return patched_files


def main() -> None:
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
