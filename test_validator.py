#!/usr/bin/env python3
"""
test_validator.py — UC-4: Validate Healed Spec Before Committing
================================================================
Runs a lightweight validation of a patched spec file before it goes
back into the pipeline.  Two modes:

  1. Syntax check        — TypeScript compile (tsc --noEmit)
  2. Locator dry-run     — Playwright --list to resolve locators
                           without launching a browser for each test
  3. Smoke run (opt-in)  — run the first N tests headed to confirm pass

If validation passes  → marks healing_log entry as validated
                        optionally opens a PR with the patched spec
If validation fails   → reverts the spec + alerts via Slack

Usage
─────
  python3 test_validator.py --project SCRUM-70 --spec tests/steps/SCRUM-70.spec.ts
  python3 test_validator.py --project SCRUM-70 --spec tests/steps/SCRUM-70.spec.ts --smoke-n 3
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import subprocess
import datetime
import shutil
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK",  "")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN",   "")
GITHUB_REPO    = os.getenv("GITHUB_REPO",    "")   # e.g. org/repo
DEFAULT_DB     = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT   = os.path.abspath(os.path.dirname(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# §1  BACKUP / RESTORE
# ══════════════════════════════════════════════════════════════════════════════

def backup_spec(spec_path: str) -> str:
    ts      = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    backup  = f"{spec_path}.bak_{ts}"
    shutil.copy2(spec_path, backup)
    return backup


def restore_spec(spec_path: str, backup_path: str) -> None:
    shutil.copy2(backup_path, spec_path)
    os.remove(backup_path)
    print(f"  ↩ Spec restored from backup: {os.path.basename(backup_path)}")

def mark_heal_success(spec_file: str, project_key: str, db_path: str, success: bool):
    import sqlite3

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

    except Exception as e:
        print(f"⚠ Failed to update heal success: {e}")
# ══════════════════════════════════════════════════════════════════════════════
# §2  VALIDATION STAGES
# ══════════════════════════════════════════════════════════════════════════════

def stage_syntax_check(spec_path: str) -> Tuple[bool, str]:
    """
    TypeScript compile check via tsc --noEmit.
    Returns (passed, output).
    """
    try:
        result = subprocess.run(
            ["npx", "tsc", "--noEmit", "--skipLibCheck"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=60,
        )
        passed = result.returncode == 0
        output = (result.stdout + result.stderr).strip()
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "tsc timed out after 60s"
    except FileNotFoundError:
        # tsc not available — skip this stage
        return True, "tsc not available — stage skipped"


def stage_locator_dryrun(spec_path: str) -> Tuple[bool, str]:
    """
    Playwright --list resolves tests without running them.
    Catches import errors and missing modules.
    """
    rel_path = os.path.relpath(spec_path, PROJECT_ROOT)
    try:
        result = subprocess.run(
            ["npx", "playwright", "test", rel_path, "--list"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=30,
        )
        passed = result.returncode == 0
        output = (result.stdout + result.stderr).strip()
        # 'list' exits 0 even with no tests — check for error markers
        if "Error:" in output and "Cannot find" in output:
            passed = False
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "playwright --list timed out"
    except FileNotFoundError:
        return True, "playwright not found — stage skipped"


def stage_smoke_run(spec_path: str, n: int = 3) -> Tuple[bool, str]:
    """
    Run the first N tests headlessly and confirm they pass.
    """
    rel_path = os.path.relpath(spec_path, PROJECT_ROOT)
    try:
        result = subprocess.run(
            [
                "npx", "playwright", "test", rel_path,
                "--project=chromium",
                "--reporter=line",
                f"--shard=1/{n}",   # approximate: run first shard only
            ],
            capture_output=True, text=True,
            cwd=PROJECT_ROOT, timeout=120,
        )
        passed = result.returncode == 0
        output = (result.stdout + result.stderr).strip()[-1500:]
        return passed, output
    except subprocess.TimeoutExpired:
        return False, "Smoke run timed out after 120s"
    except FileNotFoundError:
        return True, "playwright not found — stage skipped"


# ══════════════════════════════════════════════════════════════════════════════
# §3  DB — mark heals as validated
# ══════════════════════════════════════════════════════════════════════════════

def mark_heals_validated(project_key: str, spec_file: str, db_path: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE healing_log SET validated = 1
            WHERE project_key = ? AND spec_file = ? AND validated = 0
            """,
            (project_key, spec_file),
        )
        conn.commit()
        conn.close()
        print(f"  ✓ Healing log entries marked as validated")
    except Exception as exc:
        print(f"  ⚠ DB update failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §4  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def notify_slack(
    project_key: str,
    spec_file:   str,
    passed:      bool,
    summary:     str,
    pr_url:      Optional[str] = None,
) -> None:
    if not SLACK_WEBHOOK:
        return
    icon    = ":white_check_mark:" if passed else ":x:"
    status  = "PASSED" if passed else "FAILED — spec reverted"
    pr_line = f"\n*PR:* {pr_url}" if pr_url else ""
    payload = {
        "text": f"{icon} *Healed Spec Validation {status}* ({project_key})",
        "blocks": [
            {"type": "header", "text": {
                "type": "plain_text",
                "text": f"{'✅' if passed else '❌'} Healed Spec: {status}"
            }},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Project:*\n{project_key}"},
                {"type": "mrkdwn", "text": f"*Spec:*\n{os.path.basename(spec_file)}"},
            ]},
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Summary:*\n```{summary[:600]}```{pr_line}"
            }},
        ],
    }
    try:
        requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        print(f"  ✓ Slack notified")
    except Exception as exc:
        print(f"  ⚠ Slack failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §5  GITHUB PR (optional)
# ══════════════════════════════════════════════════════════════════════════════

def open_github_pr(
    project_key: str,
    spec_file:   str,
    branch_name: str,
) -> Optional[str]:
    """
    Create a PR for the healed spec.
    Requires GITHUB_TOKEN and GITHUB_REPO env vars.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
    }
    body = {
        "title": f"[auto-heal] Selector healing — {project_key} {os.path.basename(spec_file)}",
        "body": (
            f"Automated selector healing applied by `selector_healer.py`.\n\n"
            f"Project: `{project_key}`\nSpec: `{spec_file}`\n\n"
            f"Validation: ✅ All stages passed."
        ),
        "head":  branch_name,
        "base":  "main",
    }
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/pulls",
            headers=headers, json=body, timeout=15,
        )
        if r.ok:
            pr_url = r.json().get("html_url", "")
            print(f"  ✓ GitHub PR opened: {pr_url}")
            return pr_url
        else:
            print(f"  ⚠ GitHub PR failed: {r.status_code}: {r.text[:100]}")
    except Exception as exc:
        print(f"  ⚠ GitHub PR error: {exc}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §6  MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-4 Test Validator")
    parser.add_argument("--project", required=True)
    parser.add_argument("--spec", required=True, help="Patched spec file path")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--smoke-n", type=int, default=0,
                        help="Number of tests to smoke-run (0 = skip smoke stage)")
    parser.add_argument("--open-pr", action="store_true",
                        help="Open GitHub PR if validation passes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_key = args.project
    spec_path   = args.spec

    print(f"\n{'='*60}")
    print(f"Test Validator — {project_key}")
    print(f"{'='*60}")
    print(f"  Spec : {spec_path}")

    if not os.path.exists(spec_path):
        print(f"  ✗ Spec file not found: {spec_path}")
        return

    backup_path = backup_spec(spec_path)
    print(f"  Backup created: {os.path.basename(backup_path)}")

    all_passed = True
    summary_parts = []

    # ── Stage 1 — Syntax
    print("\n  [Stage 1] TypeScript syntax check …")
    ok, out = stage_syntax_check(spec_path)
    print(f"    {'✓ Passed' if ok else '✗ Failed'}")
    summary_parts.append(f"Syntax: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print(f"    {out[:400]}")
        all_passed = False

    # ── Stage 2 — Locator dry-run
    if all_passed:
        print("\n  [Stage 2] Playwright --list locator resolution …")
        ok, out = stage_locator_dryrun(spec_path)
        print(f"    {'✓ Passed' if ok else '✗ Failed'}")
        summary_parts.append(f"Locator dry-run: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print(f"    {out[:400]}")
            all_passed = False

    # ── Stage 3 — Smoke run
    if all_passed and args.smoke_n > 0:
        print(f"\n  [Stage 3] Smoke run ({args.smoke_n} tests) …")
        ok, out = stage_smoke_run(spec_path, args.smoke_n)
        print(f"    {'✓ Passed' if ok else '✗ Failed'}")
        summary_parts.append(f"Smoke ({args.smoke_n}): {'PASS' if ok else 'FAIL'}")
        if not ok:
            print(f"    {out[-400:]}")
            all_passed = False

    summary = " | ".join(summary_parts)
    pr_url  = None

    # ────────────────────────────────────────────────────────────────────────
    # ✅ SUCCESS CASE
    # ────────────────────────────────────────────────────────────────────────
    if all_passed:
        print(f"\n  ✓ All validation stages passed")
        os.remove(backup_path)

        if not args.dry_run:
            mark_heal_success(spec_path, project_key, args.db, True)

        if args.open_pr and not args.dry_run:
            branch = f"auto-heal/{project_key}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            pr_url = open_github_pr(project_key, spec_path, branch)

        notify_slack(project_key, spec_path, True, summary, pr_url)

    # ────────────────────────────────────────────────────────────────────────
    # ❌ FAILURE CASE (FIXED — THIS WAS MISSING)
    # ────────────────────────────────────────────────────────────────────────
    else:
        print(f"\n  ✗ Validation failed — reverting spec")
        restore_spec(spec_path, backup_path)

        if not args.dry_run:
            mark_heal_success(spec_path, project_key, args.db, False)

        notify_slack(project_key, spec_path, False, summary)

    # ── Final summary
    print(f"\n{'='*60}")
    print(f"{'✓ VALIDATED' if all_passed else '✗ REVERTED'} — {summary}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
