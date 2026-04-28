#!/usr/bin/env python3
"""
false_failure_rca.py — UC-3: False Failure Root Cause Analysis
==============================================================
For a confirmed false failure (infrastructure / env / selector):
  1. LLM sub-classifies into: selector | timeout | env | data | config
  2. Routes to the correct remediation:
       selector  → calls spec_fixer.py (selector healing)
       timeout   → patches wait strategy in spec
       env/data  → raises alert with instructions
       config    → raises alert with diff suggestion
  3. Re-queues the test after fixing

Usage
─────
  python3 false_failure_rca.py --project SCRUM-70 --test-id 42
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import subprocess
import datetime
from typing import Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config

load_dotenv()

AGENT_NAME = "false_failure_rca_v1"

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
DEFAULT_DB    = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT  = os.path.abspath(os.path.dirname(__file__))

# Sub-categories and their remediation routes
CATEGORY_ROUTES = {
    "selector": "spec_fixer",
    "timeout":  "patch_wait",
    "env":      "alert",
    "data":     "alert",
    "config":   "alert",
}


# ══════════════════════════════════════════════════════════════════════════════
# §1  LOAD RECORD
# ══════════════════════════════════════════════════════════════════════════════

def load_test_record(test_id: int, db_path: str) -> Dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row  = conn.execute(
        "SELECT * FROM test_results WHERE id = ?", (test_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"test_id={test_id} not found in DB")
    return dict(row)


# ══════════════════════════════════════════════════════════════════════════════
# §2  LAYER 1 — RULE-BASED SUB-CLASSIFICATION (fast path)
# ══════════════════════════════════════════════════════════════════════════════

SELECTOR_PATTERNS = [
    r"strict mode violation",
    r"locator resolved to \d+ elements",
    r"waiting for locator",
    r"no element found for intent",
    r"smartaction (fill|failed)",
    r"element is not (enabled|stable|visible)",
]
TIMEOUT_PATTERNS = [
    r"timeouterror",
    r"navigation timeout",
    r"timeout \d+ms exceeded",
    r"target closed",
    r"context was destroyed",
]
ENV_PATTERNS = [
    r"net::err_",
    r"econnrefused",
    r"enotfound",
    r"ssl_error",
    r"browser has been closed",
    r"page\.goto.*failed",
]


def rule_subcategory(error_msg: str, stack: str) -> Optional[str]:
    haystack = (error_msg + " " + stack).lower()
    if any(re.search(p, haystack) for p in SELECTOR_PATTERNS):
        return "selector"
    if any(re.search(p, haystack) for p in TIMEOUT_PATTERNS):
        return "timeout"
    if any(re.search(p, haystack) for p in ENV_PATTERNS):
        return "env"
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §3  LAYER 2 — LLM SUB-CLASSIFICATION + FIX SUGGESTION
# ══════════════════════════════════════════════════════════════════════════════

def _call_ollama(prompt: str, system: str) -> str:
    return call_llm(AGENT_NAME, prompt, system=system)

def llm_subcategory(
    rec: Dict, rule_hint: Optional[str]
) -> Tuple[str, str, str]:
    """
    Returns (category, fix_instructions, rca_summary).
    category ∈ {selector, timeout, env, data, config}
    """
    system = """You are a QA infrastructure specialist. A Playwright test failed
for a NON-application reason (infrastructure, selector, env, data, or config).
Identify the sub-category and provide a concrete fix.

Reply ONLY with valid JSON:
{
  "category":         "selector"|"timeout"|"env"|"data"|"config",
  "confidence":       0.0-1.0,
  "fix_instructions": "<concrete step-by-step fix — be specific>",
  "rca_summary":      "<2-3 sentences>"
}"""

    prompt = f"""Test title : {rec['test_title']}
Error      :
{rec.get('error_message','')[:600]}

Stack      :
{rec.get('stack_trace','')[:1000]}

Rule-based hint: {rule_hint or 'none'}

Sub-classify this FALSE failure and suggest a fix."""

    raw = _call_ollama(prompt, system)
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        obj = json.loads(raw)
        return (
            obj.get("category",         rule_hint or "selector"),
            obj.get("fix_instructions", "Review the error manually."),
            obj.get("rca_summary",      rec.get("rca_summary", "")),
        )
    except Exception:
        return rule_hint or "selector", "Review error manually.", ""


# ══════════════════════════════════════════════════════════════════════════════
# §4  REMEDIATION ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def remediate_selector(project_key: str, rec: Dict, db_path: str) -> None:
    """Route to spec_fixer.py for selector/strict-mode issues."""
    print(f"  → Routing to spec_fixer.py for selector healing")
    cmd = [
        "python3", "spec_fixer.py",
        "--project", project_key,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        print(f"  spec_fixer exit={result.returncode}")
        if result.stdout:
            print(f"  {result.stdout[:300]}")
    except Exception as exc:
        print(f"  ⚠ spec_fixer failed: {exc}")


def remediate_timeout(rec: Dict) -> None:
    """
    Patch wait strategy: find the step in the spec and inject
    an explicit waitFor before the timed-out action.
    Simple pattern — for complex cases, spec_fixer handles it.
    """
    spec_file = rec.get("spec_file", "")
    if not spec_file or not os.path.exists(spec_file):
        print(f"  ⚠ Spec file not found for timeout patch: {spec_file}")
        return

    # Extract the line number from the stack trace
    m = re.search(rf'{re.escape(os.path.basename(spec_file))}:(\d+):\d+', rec.get("stack_trace",""))
    if not m:
        print("  ⚠ Could not extract line number from stack — skipping timeout patch")
        return

    line_no = int(m.group(1)) - 1
    with open(spec_file) as f:
        lines = f.readlines()

    if 0 <= line_no < len(lines):
        indent = re.match(r'^(\s*)', lines[line_no]).group(1)
        wait_line = f"{indent}await basePage.page.waitForLoadState('networkidle');  // FIX: timeout guard added by false_failure_rca\n"
        lines.insert(line_no, wait_line)
        with open(spec_file, "w") as f:
            f.writelines(lines)
        print(f"  ✓ Injected waitForLoadState at line {line_no+1} in {os.path.basename(spec_file)}")
    else:
        print(f"  ⚠ Line {line_no} out of range in {spec_file}")


def remediate_alert(
    category:         str,
    fix_instructions: str,
    rec:              Dict,
    project_key:      str,
) -> None:
    """Send a Slack alert for env/data/config issues that need human action."""
    msg = (
        f"\n  ⚠ FALSE FAILURE — {category.upper()} ISSUE\n"
        f"  Test   : {rec['test_title'][:80]}\n"
        f"  Fix    : {fix_instructions[:300]}\n"
    )
    print(msg)

    if not SLACK_WEBHOOK:
        return
    try:
        payload = {
            "text": f":warning: *False Failure — {category.upper()}* ({project_key})",
            "blocks": [
                {"type": "header", "text": {
                    "type": "plain_text",
                    "text": f"⚠ False Failure: {category.upper()} Issue"
                }},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Test:*\n{rec['test_title'][:80]}"},
                    {"type": "mrkdwn", "text": f"*Category:*\n{category}"},
                ]},
                {"type": "section", "text": {
                    "type": "mrkdwn",
                    "text": f"*Fix Instructions:*\n{fix_instructions[:500]}"
                }},
            ],
        }
        requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        print("  ✓ Slack alert sent")
    except Exception as exc:
        print(f"  ⚠ Slack alert failed: {exc}")


def requeue_test(project_key: str, rec: Dict) -> None:
    """Re-run just the fixed test via playwright CLI."""
    spec_file = rec.get("spec_file", "")
    ac_tags   = rec.get("ac_tags", "")
    grep_tag  = ac_tags.split(",")[0] if ac_tags else ""
    grep_arg  = f'--grep "{grep_tag}"' if grep_tag else ""

    cmd_str = (
        f"npx playwright test {spec_file} {grep_arg} "
        f"--project=chromium --reporter=json"
    ).strip()

    print(f"  → Re-queuing: {cmd_str}")
    try:
        subprocess.Popen(cmd_str, shell=True, cwd=PROJECT_ROOT)
    except Exception as exc:
        print(f"  ⚠ Re-queue failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §5  DB UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def update_rca(test_id: int, category: str, summary: str, db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE test_results SET failure_class=?, rca_summary=? WHERE id=?",
        (f"false:{category}", summary, test_id),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# §6  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-3 False Failure RCA")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--test-id",  type=int, required=True)
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--no-requeue", action="store_true")
    args = parser.parse_args()
    log_agent_config(AGENT_NAME)

    print(f"\n{'='*60}")
    print(f"False Failure RCA — {args.project} / test_id={args.test_id}")
    print(f"{'='*60}")

    rec         = load_test_record(args.test_id, args.db)
    err_msg     = rec.get("error_message", "")
    stack       = rec.get("stack_trace",   "")

    # Sub-classify
    rule_hint                        = rule_subcategory(err_msg, stack)
    category, fix_instructions, rca  = llm_subcategory(rec, rule_hint)

    print(f"  Category    : {category}")
    print(f"  Fix preview : {fix_instructions[:120]}…")

    if not args.dry_run:
        update_rca(args.test_id, category, rca, args.db)

        route = CATEGORY_ROUTES.get(category, "alert")
        if route == "spec_fixer":
            remediate_selector(args.project, rec, args.db)
        elif route == "patch_wait":
            remediate_timeout(rec)
        else:
            remediate_alert(category, fix_instructions, rec, args.project)

        if not args.no_requeue and route in ("spec_fixer", "patch_wait"):
            requeue_test(args.project, rec)
    else:
        print("\n  [DRY RUN]")
        print(f"  Route       : {CATEGORY_ROUTES.get(category, 'alert')}")
        print(f"  RCA         : {rca}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
