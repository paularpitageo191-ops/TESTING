#!/usr/bin/env python3
"""
true_failure_rca.py — UC-3: True Failure Root Cause Analysis
============================================================
For a confirmed true failure (application bug):
  1. LLM reads stack trace + git blame to explain root cause
  2. Creates a Jira bug with full context
  3. Notifies the dev team (Slack webhook or email)

Usage
─────
  python3 true_failure_rca.py --project SCRUM-70 --test-id 42
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import subprocess
import datetime
from typing import Dict, Optional

import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config

load_dotenv()

AGENT_NAME = "true_failure_rca_v1"

JIRA_URL      = os.getenv("JIRA_BASE_URL",    "")   # https://paularpitaseis.atlassian.net
JIRA_USER     = os.getenv("JIRA_EMAIL",        "")   # paularpitaseis@gmail.com
JIRA_TOKEN    = os.getenv("JIRA_API_TOKEN",    "")   # Atlassian API token
JIRA_PROJECT  = os.getenv("JIRA_PROJECT_KEY",  "SCRUM")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK", "")
DEFAULT_DB    = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT  = os.path.abspath(os.path.dirname(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# §1  LOAD TEST RECORD
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
# §2  GIT BLAME  — tie failure to a recent commit
# ══════════════════════════════════════════════════════════════════════════════

def _extract_file_from_stack(stack: str) -> Optional[str]:
    """Find the first application source file referenced in the stack trace."""
    for line in stack.splitlines():
        # Skip node_modules and test infra files
        if "node_modules" in line or "_bmad" in line or ".spec.ts" in line:
            continue
        m = re.search(r'([\w/.-]+\.(ts|js|py))', line)
        if m:
            return m.group(1)
    return None


def get_git_blame_context(stack: str) -> str:
    """Return last N commits that touched the suspected source file."""
    src_file = _extract_file_from_stack(stack)
    if not src_file:
        return ""
    abs_path = os.path.join(PROJECT_ROOT, src_file)
    if not os.path.exists(abs_path):
        return ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-5", "--", src_file],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# §3  LLM  — detailed RCA narrative
# ══════════════════════════════════════════════════════════════════════════════

def _call_ollama(prompt: str, system: str) -> str:
    return call_llm(AGENT_NAME, prompt, system=system)

def generate_rca_report(rec: Dict, git_context: str) -> Dict:
    """
    LLM produces a structured RCA report in JSON:
    {
      "summary":          "one-line bug description",
      "root_cause":       "paragraph",
      "impacted_area":    "component / feature area",
      "recommended_fix":  "actionable paragraph",
      "priority":         "P1"|"P2"|"P3"|"P4",
      "jira_description": "full Jira description in plain text"
    }
    """
    system = """You are a senior software engineer performing root cause analysis on a
failed automated test. Produce a concise, actionable defect report.
Reply ONLY with valid JSON matching the schema provided in the prompt."""

    prompt = f"""Test title  : {rec['test_title']}
AC tags     : {rec.get('ac_tags', '')}
Page URL    : {rec.get('page_url', 'unknown')}
Error       :
{rec.get('error_message','')[:800]}

Stack trace :
{rec.get('stack_trace','')[:1200]}

Recent git history for suspected file:
{git_context or '(not available)'}

Produce root cause analysis in this exact JSON schema:
{{
  "summary":          "<one-line bug description>",
  "root_cause":       "<paragraph: what went wrong and why>",
  "impacted_area":    "<component or feature>",
  "recommended_fix":  "<actionable paragraph>",
  "priority":         "P1|P2|P3|P4",
  "jira_description": "<full Jira description>"
}}"""

    raw = _call_ollama(prompt, system)
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        return json.loads(raw)
    except Exception:
        return {
            "summary":         f"Test failure: {rec['test_title'][:80]}",
            "root_cause":      rec.get("error_message", "")[:300],
            "impacted_area":   "Unknown",
            "recommended_fix": "Investigate stack trace manually.",
            "priority":        "P3",
            "jira_description": rec.get("rca_summary", "See test failure details."),
        }


# ══════════════════════════════════════════════════════════════════════════════
# §4  JIRA  — create bug ticket
# ══════════════════════════════════════════════════════════════════════════════

_PRIORITY_MAP = {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"}


def create_jira_bug(report: Dict, project_key: str, rec: Dict) -> Optional[str]:
    if not all([JIRA_URL, JIRA_USER, JIRA_TOKEN]):
        print("  ⚠ Jira credentials not configured — skipping bug creation")
        return None

    payload = {
        "fields": {
            "project":     {"key": JIRA_PROJECT},
            "summary":     f"[AUTO-RCA] {report['summary']}",
            "description": {
                "type":    "doc",
                "version": 1,
                "content": [{
                    "type":    "paragraph",
                    "content": [{"type": "text",
                                 "text": report["jira_description"]}],
                }],
            },
            "issuetype":   {"name": "Bug"},
            "priority":    {"name": _PRIORITY_MAP.get(report["priority"], "Medium")},
            "labels":      [project_key, "auto-rca", "test-failure"],
        }
    }

    try:
        r = requests.post(
            f"{JIRA_URL}/rest/api/3/issue",
            auth=(JIRA_USER, JIRA_TOKEN),
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.ok:
            key = r.json().get("key", "")
            print(f"  ✓ Jira bug created: {key}")
            return key
        else:
            print(f"  ✗ Jira error {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"  ✗ Jira request failed: {exc}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §5  SLACK NOTIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def notify_slack(report: Dict, jira_key: Optional[str], rec: Dict) -> None:
    if not SLACK_WEBHOOK:
        print("  ⚠ SLACK_WEBHOOK not configured — skipping notification")
        return

    jira_link = f"{JIRA_URL}/browse/{jira_key}" if jira_key else "No Jira ticket"
    message = {
        "text": f":red_circle: *True Test Failure — {report['priority']}*",
        "blocks": [
            {"type": "header", "text": {
                "type": "plain_text",
                "text": f"🐛 {report['summary']}"
            }},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Test:*\n{rec['test_title'][:80]}"},
                {"type": "mrkdwn", "text": f"*Priority:*\n{report['priority']}"},
                {"type": "mrkdwn", "text": f"*Area:*\n{report['impacted_area']}"},
                {"type": "mrkdwn", "text": f"*Jira:*\n{jira_link}"},
            ]},
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause:*\n{report['root_cause'][:300]}"
            }},
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Recommended Fix:*\n{report['recommended_fix'][:300]}"
            }},
        ],
    }
    try:
        r = requests.post(SLACK_WEBHOOK, json=message, timeout=10)
        print(f"  ✓ Slack notified (status={r.status_code})")
    except Exception as exc:
        print(f"  ⚠ Slack notification failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §6  DB UPDATE
# ══════════════════════════════════════════════════════════════════════════════

def update_rca_summary(test_id: int, summary: str, db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE test_results SET rca_summary=? WHERE id=?",
        (summary, test_id),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# §7  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-3 True Failure RCA")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--test-id",  type=int, required=True)
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()
    log_agent_config(AGENT_NAME)

    print(f"\n{'='*60}")
    print(f"True Failure RCA — {args.project} / test_id={args.test_id}")
    print(f"{'='*60}")

    rec          = load_test_record(args.test_id, args.db)
    git_context  = get_git_blame_context(rec.get("stack_trace", ""))
    report       = generate_rca_report(rec, git_context)

    print(f"  Summary   : {report['summary']}")
    print(f"  Priority  : {report['priority']}")
    print(f"  Root cause: {report['root_cause'][:120]}…")

    jira_key = None
    if not args.dry_run:
        jira_key = create_jira_bug(report, args.project, rec)
        notify_slack(report, jira_key, rec)
        update_rca_summary(args.test_id, report["root_cause"][:500], args.db)
    else:
        print("\n  [DRY RUN] Jira + Slack skipped.")
        print(json.dumps(report, indent=2))

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()