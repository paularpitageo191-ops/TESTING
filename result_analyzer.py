#!/usr/bin/env python3
"""
result_analyzer.py — UC-3: Parse Playwright JSON Reporter Output
================================================================
Reads the JSON file produced by --reporter=json, writes every test
result into test_results table, then hands failures to classifier.py.

Usage
─────
  # Generate report first:
  npx playwright test tests/steps/SCRUM-70.spec.ts --reporter=json > pw_report.json

  python3 result_analyzer.py --project SCRUM-70 --report pw_report.json
  python3 result_analyzer.py --project SCRUM-70 --report pw_report.json --run-id my-run-001
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import datetime
import subprocess
from typing import Dict, List, Optional, Tuple
import sys
from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB   = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

# ══════════════════════════════════════════════════════════════════════════════
# §1  REPORT PARSER
# ══════════════════════════════════════════════════════════════════════════════
def parse_pw_report(report_path: str) -> Tuple[Dict, List[Dict]]:
    """
    Parse Playwright JSON report.
    Returns (summary_dict, list_of_test_records).

    Playwright JSON structure:
      {
        "suites": [ {
          "title": "...",
          "specs":  [ {
            "title": "...",
            "tests": [ {
              "status":  "passed"|"failed"|"skipped"|"flaky",
              "results": [ { "duration": ms, "error": {...} } ]
            } ]
          } ]
        } ]
      }

    Note: with retries=2 each test can have up to 3 result entries.
    We record ONE row per test using only the FINAL result attempt.
    """
    with open(report_path) as f:
        data = json.load(f)

    records = []

    def _normalize_status(raw: str) -> str:
        mapping = {
            "passed":      "passed",
            "failed":      "failed",
            "timedout":    "failed",
            "skipped":     "skipped",
            "interrupted": "failed",
            "flaky":       "flaky",
        }
        return mapping.get(raw.lower(), "failed")

    def _extract_page_url(text: str) -> str:
        m = re.search(r'https?://[^\s"\')\]]+', text)
        return m.group(0) if m else ""

    def _walk_suite(suite: Dict, parent_file: str = "") -> None:
        spec_file = suite.get("file", parent_file) or parent_file
        for spec in suite.get("specs", []):
            title = spec.get("title", "")
            tags  = re.findall(r'@(AC\d+|SCRUM[-_]\d+)', title)
            for test in spec.get("tests", []):
                status  = test.get("status", "unknown")
                results = test.get("results", [{}])

                # ── Use FINAL attempt only (last item in results list) ──
                # With retries=2, results has up to 3 entries.
                # The last entry is the conclusive one.
                final   = results[-1] if results else {}
                dur_ms  = final.get("duration", 0)
                error   = final.get("error", {}) or {}
                err_msg = error.get("message", "")
                stack   = error.get("stack",   "")

                records.append({
                    "spec_file":     spec_file,
                    "test_title":    title,
                    "ac_tags":       ",".join(tags),
                    "status":        _normalize_status(status),
                    "duration_ms":   dur_ms,
                    "error_message": err_msg,
                    "stack_trace":   stack,
                    "timestamp":     datetime.datetime.utcnow().isoformat(),
                    "page_url":      _extract_page_url(err_msg + stack),
                    "retry_count":   len(results) - 1,
                })

        for child in suite.get("suites", []):
            _walk_suite(child, spec_file)

    for top_suite in data.get("suites", []):
        _walk_suite(top_suite)

    # Use stats block from report (accurate counts)
    stats = data.get("stats", {})
    summary = {
        "total":   stats.get("expected", 0) + stats.get("unexpected", 0),
        "passed":  stats.get("expected",  0),
        "failed":  stats.get("unexpected", 0),
        "skipped": stats.get("skipped",   0),
        "flaky":   stats.get("flaky",     0),
    }
    # Fallback: recount from records if stats block is missing
    if summary["total"] == 0:
        for s in ("passed", "failed", "skipped", "flaky"):
            summary[s] = sum(1 for r in records if r["status"] == s)
        summary["total"] = len(records)

    return summary, records
# ══════════════════════════════════════════════════════════════════════════════
# §2  DB WRITE
# ══════════════════════════════════════════════════════════════════════════════

def persist_results(
    records:     List[Dict],
    project_key: str,
    run_id:      str,
    db_path:     str,
) -> None:
    conn = sqlite3.connect(db_path)

    # Ensure run row exists
    conn.execute(
        """
        INSERT OR IGNORE INTO runs
            (run_id, project_key, triggered_by, started_at,
             total_tests, passed, failed, skipped)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, project_key, "result_analyzer.py",
            datetime.datetime.utcnow().isoformat(),
            len(records),
            sum(1 for r in records if r["status"] == "passed"),
            sum(1 for r in records if r["status"] == "failed"),
            sum(1 for r in records if r["status"] == "skipped"),
        ),
    )

    for rec in records:
        conn.execute(
            """
            INSERT INTO test_results
                (run_id, project_key, spec_file, test_title, ac_tags,
                 status, duration_ms, error_message, stack_trace, timestamp, page_url)
            VALUES
                (:run_id, :project_key, :spec_file, :test_title, :ac_tags,
                 :status, :duration_ms, :error_message, :stack_trace, :timestamp, :page_url)
            """,
            {**rec, "run_id": run_id, "project_key": project_key},
        )

    conn.commit()
    conn.close()
    print(f"  ✓ {len(records)} results persisted (run_id={run_id})")


# ══════════════════════════════════════════════════════════════════════════════
# §3  HAND-OFF TO CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════

def invoke_classifier(project_key: str, run_id: str, db_path: str) -> None:
    """
    Trigger classifier.py as a subprocess for each failed test.
 
    KEY FIX: pass env=os.environ.copy() so that Jenkins environment
    variables (QDRANT_URL, OLLAMA_HOST) are inherited by the subprocess.
    Without this, the child process loses those vars and falls back to
    localhost, causing connection refused errors in Docker.
    """
    cmd = [
        sys.executable, "classifier.py",
        "--project", project_key,
        "--run-id",  run_id,
        "--db",      db_path,
    ]
    print(f"  → Handing off to classifier: {' '.join(cmd)}")
    try:
        subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),   # ← inherits QDRANT_URL, OLLAMA_HOST from Jenkins
        )
    except FileNotFoundError:
        print("  ⚠ classifier.py not found — skipping handoff")

# ══════════════════════════════════════════════════════════════════════════════
# §4  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-3 Result Analyzer")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--report",   required=True, help="Path to Playwright JSON report")
    parser.add_argument("--run-id",   default=None)
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--no-classify", action="store_true",
                        help="Skip classifier handoff")
    args = parser.parse_args()

    project_key = args.project
    run_id = args.run_id or (
        f"{project_key}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    )

    print(f"\n{'='*60}")
    print(f"Result Analyzer — {project_key}")
    print(f"{'='*60}")
    print(f"  Report  : {args.report}")
    print(f"  Run ID  : {run_id}")

    summary, records = parse_pw_report(args.report)

    print(f"\n  Results: total={summary['total']}  "
          f"passed={summary['passed']}  "
          f"failed={summary['failed']}  "
          f"skipped={summary['skipped']}")

    persist_results(records, project_key, run_id, args.db)

    failed = [r for r in records if r["status"] == "failed"]
    print(f"  Failures to classify: {len(failed)}")

    if failed and not args.no_classify:
        invoke_classifier(project_key, run_id, args.db)

    print(f"\n{'='*60}")
    print(f"✓ Result Analyzer complete — run_id: {run_id}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
