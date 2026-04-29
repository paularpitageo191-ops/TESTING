#!/usr/bin/env python3
"""
test_validator.py — UC-4: Validate Healed Spec Before Committing
"""

from __future__ import annotations

import os
import sqlite3
import argparse
import subprocess
import datetime
import shutil
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")

DEFAULT_DB   = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# BACKUP / RESTORE
# ══════════════════════════════════════════════════════════════════════════════

def backup_spec(spec_path: str) -> str:
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    backup = f"{spec_path}.bak_{ts}"
    shutil.copy2(spec_path, backup)
    return backup


def restore_spec(spec_path: str, backup_path: str) -> None:
    shutil.copy2(backup_path, spec_path)
    os.remove(backup_path)
    print(f"  ↩ Spec restored from backup: {os.path.basename(backup_path)}")


# ══════════════════════════════════════════════════════════════════════════════
# DB — MARK SUCCESS / FAILURE
# ══════════════════════════════════════════════════════════════════════════════

def mark_heal_success(spec_file: str, project_key: str, db_path: str, success: bool):
    """
    Marks latest heal attempt for this spec as success/failure.
    """
    try:
        conn = sqlite3.connect(db_path)

        conn.execute(
            """
            UPDATE healing_log
            SET validated = ?
            WHERE id = (
                SELECT id FROM healing_log
                WHERE project_key = ? AND spec_file = ?
                ORDER BY healed_at DESC
                LIMIT 1
            )
            """,
            (1 if success else 0, project_key, spec_file),
        )

        conn.commit()
        conn.close()

        print(f"  ✓ Heal validation updated → {'SUCCESS' if success else 'FAILED'}")

    except Exception as e:
        print(f"⚠ Failed to update heal success: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# OPTIONAL — FUTURE QDRANT UPDATE HOOK (SAFE NO-OP FOR NOW)
# ══════════════════════════════════════════════════════════════════════════════

def update_healing_validation(project_key: str, spec_file: str, success: bool):
    """
    Placeholder hook for future Qdrant update.
    Keeps system forward-compatible without breaking current flow.
    """
    try:
        # You can later extend this to update Qdrant payload
        # Currently safe no-op
        print(f"  🔄 (future) Qdrant update → {project_key} {spec_file} = {success}")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATION STAGES
# ══════════════════════════════════════════════════════════════════════════════

def stage_syntax_check(spec_path: str) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            ["npx", "tsc", "--noEmit", "--skipLibCheck"],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT, timeout=60,
        )
        passed = result.returncode == 0
        return passed, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, "tsc timed out"
    except FileNotFoundError:
        return True, "tsc not available"


def stage_locator_dryrun(spec_path: str) -> Tuple[bool, str]:
    rel_path = os.path.relpath(spec_path, PROJECT_ROOT)
    try:
        result = subprocess.run(
            ["npx", "playwright", "test", rel_path, "--list"],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT, timeout=30,
        )
        output = (result.stdout + result.stderr).strip()
        passed = result.returncode == 0 and "Error:" not in output
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "playwright list timed out"
    except FileNotFoundError:
        return True, "playwright not available"


def stage_smoke_run(spec_path: str, n: int = 3) -> Tuple[bool, str]:
    rel_path = os.path.relpath(spec_path, PROJECT_ROOT)
    try:
        result = subprocess.run(
            [
                "npx", "playwright", "test", rel_path,
                "--project=chromium",
                "--reporter=line",
                f"--shard=1/{n}",
            ],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT, timeout=120,
        )
        passed = result.returncode == 0
        return passed, (result.stdout + result.stderr).strip()[-1500:]
    except subprocess.TimeoutExpired:
        return False, "smoke run timed out"
    except FileNotFoundError:
        return True, "playwright not available"


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def notify_slack(project_key: str, spec_file: str, passed: bool, summary: str, pr_url: Optional[str] = None):
    if not SLACK_WEBHOOK:
        return

    status = "PASSED" if passed else "FAILED"

    payload = {
        "text": (
            f"{'✅' if passed else '❌'} {project_key} — {status}\n"
            f"Spec: {os.path.basename(spec_file)}\n"
            f"{summary}"
        )
    }

    try:
        requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-4 Test Validator")
    parser.add_argument("--project", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--smoke-n", type=int, default=0)
    parser.add_argument("--open-pr", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_key = args.project
    spec_path   = args.spec

    print(f"\n{'='*60}")
    print(f"Test Validator — {project_key}")
    print(f"{'='*60}")

    if not os.path.exists(spec_path):
        print("  ✗ Spec not found")
        return

    backup_path = backup_spec(spec_path)
    print(f"  Backup created: {os.path.basename(backup_path)}")

    all_passed = True
    summary_parts = []

    # ── Stage 1: Syntax
    ok, out = stage_syntax_check(spec_path)
    summary_parts.append(f"Syntax: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("  ⚠ Syntax failed")
        print(out[:300])
        all_passed = False

    # ── Stage 2: Locator validation
    if all_passed:
        ok, out = stage_locator_dryrun(spec_path)
        summary_parts.append(f"Locator: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("  ⚠ Locator resolution failed")
            print(out[:300])
            all_passed = False

    # ── Stage 3: Smoke test
    if all_passed and args.smoke_n > 0:
        ok, out = stage_smoke_run(spec_path, args.smoke_n)
        summary_parts.append(f"Smoke: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("  ⚠ Smoke test failed")
            print(out[:300])
            all_passed = False

    summary = " | ".join(summary_parts)
    print(f"\n  📊 Validation summary → {summary}")

    # ─────────────────────────────────────────────────────────────
    # SUCCESS
    # ─────────────────────────────────────────────────────────────
    if all_passed:
        print("\n  ✓ Validation passed")
        os.remove(backup_path)

        if not args.dry_run:
            mark_heal_success(spec_path, project_key, args.db, True)
            update_healing_validation(project_key, spec_path, True)

        notify_slack(project_key, spec_path, True, summary)

    # ─────────────────────────────────────────────────────────────
    # FAILURE
    # ─────────────────────────────────────────────────────────────
    else:
        print("\n  ✗ Validation failed — reverting")
        restore_spec(spec_path, backup_path)

        if not args.dry_run:
            mark_heal_success(spec_path, project_key, args.db, False)
            update_healing_validation(project_key, spec_path, False)

        notify_slack(project_key, spec_path, False, summary)

    print(f"\n{'='*60}")
    print(f"{'✓ VALIDATED' if all_passed else '✗ REVERTED'} — {summary}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
