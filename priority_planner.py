#!/usr/bin/env python3
"""
priority_planner.py — UC-2: Risk-Weighted Test Plan
====================================================
Reads risk_scores from SQLite + Qdrant failure history and produces an
ordered list of tests (with --grep tag filters) that fits within a time
budget, ensuring maximum coverage of high-risk areas first.

Outputs
───────
  tests/steps/{PROJECT}_test_plan.json   — ordered plan for jenkins_trigger
  stdout                                  — human summary

Usage
─────
  python3 priority_planner.py --project SCRUM-70
  python3 priority_planner.py --project SCRUM-70 --budget-minutes 15
  python3 priority_planner.py --project SCRUM-70 --top-n 10
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

DEFAULT_DB   = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")

# Conservative estimate: avg test duration when no history exists (seconds)
DEFAULT_TEST_DURATION_S = 30


# ══════════════════════════════════════════════════════════════════════════════
# §1  DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

def load_risk_scores(project_key: str, db_path: str) -> List[Dict]:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM risk_scores WHERE project_key = ? ORDER BY composite DESC",
            (project_key,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        print(f"  ⚠ Could not load risk scores: {exc}")
        return []


def load_avg_durations(project_key: str, db_path: str) -> Dict[str, float]:
    """Return {spec_file: avg_duration_ms} from historical results."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT spec_file, AVG(duration_ms) as avg_ms
            FROM test_results
            WHERE project_key = ? AND status != 'skipped'
            GROUP BY spec_file
            """,
            (project_key,),
        ).fetchall()
        conn.close()
        return {r[0]: float(r[1]) for r in rows}
    except Exception:
        return {}


def load_risk_json(project_key: str) -> List[Dict]:
    """Fallback: read risk scores from JSON file if DB is empty."""
    path = os.path.join(STEPS_DIR, f"{project_key}_risk_scores.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


# ══════════════════════════════════════════════════════════════════════════════
# §2  PLAN BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def extract_test_entries(spec_file: str) -> List[Dict]:
    """
    Parse spec file and return list of {title, tags, grep_tag} per test.
    grep_tag is the most specific unique tag suitable for --grep filtering.
    """
    entries = []
    if not os.path.exists(spec_file):
        return entries
    with open(spec_file) as f:
        content = f.read()
    for m in re.finditer(r'test\(["\'](.+?)["\']', content):
        title = m.group(1)
        tags  = re.findall(r'@(AC\d+|SCRUM[-_]\d+)', title)
        # Prefer AC tag for grep (most specific)
        ac_tags = [t for t in tags if t.startswith("AC")]
        grep_tag = ac_tags[0] if ac_tags else (tags[0] if tags else "")
        entries.append({"title": title, "tags": tags, "grep_tag": grep_tag})
    return entries


def build_plan(
    risk_scores:   List[Dict],
    avg_durations: Dict[str, float],
    budget_s:      Optional[float],
    top_n:         Optional[int],
) -> List[Dict]:
    """
    Build an ordered test plan.  Each entry describes one test with its
    priority rank and estimated duration.

    Selection logic:
      1. Sort by composite risk score DESC
      2. If budget_s given: accumulate until budget exhausted
      3. If top_n given: take first top_n after budget filter
      4. Both absent: return all
    """
    plan      = []
    budget_ms = budget_s * 1000 if budget_s else None
    elapsed   = 0.0

    for rank, rec in enumerate(risk_scores, start=1):
        sf        = rec["spec_file"]
        entries   = extract_test_entries(sf)
        avg_dur   = avg_durations.get(sf, DEFAULT_TEST_DURATION_S * 1000)

        for entry in entries:
            est_dur = avg_dur  # per-test estimate (use spec average)
            if budget_ms and elapsed + est_dur > budget_ms:
                continue       # skip if over budget

            plan.append({
                "rank":          rank,
                "spec_file":     sf,
                "test_title":    entry["title"],
                "grep_tag":      entry["grep_tag"],
                "ac_tags":       entry["tags"],
                "composite":     rec["composite"],
                "change_impact": rec["change_impact"],
                "fail_rate":     rec["fail_rate"],
                "criticality":   rec["criticality"],
                "est_duration_s": round(est_dur / 1000, 1),
            })
            elapsed += est_dur

        if top_n and len(plan) >= top_n:
            break

    return plan[:top_n] if top_n else plan


# ══════════════════════════════════════════════════════════════════════════════
# §3  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-2 Priority Planner")
    parser.add_argument("--project",         required=True)
    parser.add_argument("--budget-minutes",  type=float, default=None,
                        help="Max total execution time in minutes")
    parser.add_argument("--top-n",           type=int, default=None,
                        help="Maximum number of tests to include")
    parser.add_argument("--db",              default=DEFAULT_DB)
    parser.add_argument("--dry-run",         action="store_true")
    args = parser.parse_args()

    project_key = args.project
    budget_s    = args.budget_minutes * 60 if args.budget_minutes else None

    print(f"\n{'='*60}")
    print(f"Priority Planner — {project_key}")
    print(f"{'='*60}")

    risk_scores = load_risk_scores(project_key, args.db)
    if not risk_scores:
        print("  ⚠ No DB risk scores — falling back to JSON file")
        risk_scores = load_risk_json(project_key)
    print(f"  Risk score entries: {len(risk_scores)}")

    avg_durations = load_avg_durations(project_key, args.db)
    plan = build_plan(risk_scores, avg_durations, budget_s, args.top_n)

    total_est = sum(e["est_duration_s"] for e in plan)
    print(f"  Tests in plan:       {len(plan)}")
    print(f"  Estimated total:     {total_est:.0f}s ({total_est/60:.1f}m)")
    if budget_s:
        print(f"  Budget:              {budget_s:.0f}s")
        print(f"  Utilisation:         {min(total_est/budget_s*100,100):.1f}%")

    out = {
        "project_key":    project_key,
        "generated_at":   datetime.datetime.utcnow().isoformat(),
        "budget_s":       budget_s,
        "top_n":          args.top_n,
        "total_tests":    len(plan),
        "est_total_s":    total_est,
        "ordered_tests":  plan,
        # Convenience: unique ordered list of grep tags for Jenkins --grep
        "grep_tags":      list(dict.fromkeys(
            e["grep_tag"] for e in plan if e["grep_tag"]
        )),
        # Unique spec files in priority order (for parallel shard assignment)
        "spec_files":     list(dict.fromkeys(e["spec_file"] for e in plan)),
    }

    out_path = os.path.join(STEPS_DIR, f"{project_key}_test_plan.json")
    if not args.dry_run:
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\n  ✓ Test plan written → {out_path}")
    else:
        print("\n  [DRY RUN]")
        print(json.dumps(out, indent=2))

    print(f"\n  Top 5 tests by risk:")
    for e in plan[:5]:
        print(f"    [{e['rank']}] {e['grep_tag'] or e['test_title'][:50]}  "
              f"composite={e['composite']:.3f}")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
