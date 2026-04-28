#!/usr/bin/env python3
"""
jenkins_trigger.py — UC-2: Trigger Ordered Test Run in Jenkins
==============================================================
Reads the test plan produced by priority_planner.py and triggers
a Jenkins pipeline job with the correct --grep filter and spec list,
ensuring high-risk tests run first.

Usage
─────
  python3 jenkins_trigger.py --project SCRUM-70
  python3 jenkins_trigger.py --project SCRUM-70 --dry-run
  python3 jenkins_trigger.py --project SCRUM-70 --plan path/to/plan.json
"""

from __future__ import annotations

import os
import re
import json
import argparse
import datetime
import sqlite3
import uuid
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

JENKINS_URL  = os.getenv("JENKINS_URL",  "http://localhost:8080")
JENKINS_USER = os.getenv("JENKINS_USER", "admin")
JENKINS_TOKEN= os.getenv("JENKINS_TOKEN","")
JENKINS_JOB  = os.getenv("JENKINS_JOB", "playwright-test-run")
DEFAULT_DB   = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")


# ══════════════════════════════════════════════════════════════════════════════
# §1  PLAN LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_plan(project_key: str, plan_path: Optional[str] = None) -> Dict:
    path = plan_path or os.path.join(STEPS_DIR, f"{project_key}_test_plan.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Test plan not found: {path}\n"
            f"Run: python3 priority_planner.py --project {project_key}"
        )
    with open(path) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# §2  JENKINS API
# ══════════════════════════════════════════════════════════════════════════════

def get_crumb() -> Optional[Dict[str, str]]:
    """Fetch Jenkins CSRF crumb (required for POST requests)."""
    try:
        r = requests.get(
            f"{JENKINS_URL}/crumbIssuer/api/json",
            auth=(JENKINS_USER, JENKINS_TOKEN),
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return {data["crumbRequestField"]: data["crumb"]}
    except Exception as exc:
        print(f"  ⚠ Could not fetch Jenkins crumb: {exc}")
    return None


def build_playwright_command(plan: Dict, project_key: str) -> str:
    """
    Construct the npx playwright test command from the plan.
    Uses --grep to filter to prioritised AC tags, ordered spec files first.
    """
    spec_files  = plan.get("spec_files", [])
    grep_tags   = plan.get("grep_tags",  [])

    # Spec list — relative paths for portability
    spec_args = " ".join(
        os.path.relpath(sf, PROJECT_ROOT) for sf in spec_files
    ) if spec_files else f"tests/steps/{project_key}.spec.ts"

    # Build grep pattern from AC tags (OR-joined)
    grep_arg = ""
    if grep_tags:
        pattern  = "|".join(re.escape(t) for t in grep_tags)
        grep_arg = f'--grep "{pattern}"'

    return f"npx playwright test {spec_args} {grep_arg} --project=chromium --reporter=json".strip()


def _is_parameterized_job(job_name: str, crumb: Dict) -> bool:
    """
    Check whether the Jenkins job accepts parameters.
    Returns True if parameterized, False if plain freestyle.
    """
    try:
        headers = {}
        headers.update(crumb)
        r = requests.get(
            f"{JENKINS_URL}/job/{job_name}/api/json?tree=property[parameterDefinitions[name]]",
            auth=(JENKINS_USER, JENKINS_TOKEN),
            headers=headers,
            timeout=10,
        )
        if r.ok:
            props = r.json().get("property", [])
            for p in props:
                if p.get("parameterDefinitions"):
                    return True
    except Exception:
        pass
    return False


def trigger_jenkins(
    project_key: str,
    pw_command:  str,
    run_id:      str,
    plan:        Dict,
    dry_run:     bool = False,
) -> Optional[str]:
    """
    Trigger Jenkins job — handles both parameterized and plain builds.

    Strategy:
      1. Check if the job is parameterized via Jenkins API.
      2. If YES  → POST to /buildWithParameters with all params.
      3. If NO   → POST to /build (plain trigger).
         In this case the pw_command and run metadata are written to
         a JSON sidecar file that the Jenkins pipeline can read via
         a "Read file" step, OR the Jenkinsfile can run
         `cat tests/steps/{PROJECT}_test_plan.json` directly.
    """
    params = {
        "PW_COMMAND":   pw_command,
        "PROJECT_KEY":  project_key,
        "RUN_ID":       run_id,
        "BRANCH":       os.getenv("GIT_BRANCH", "main"),
        "TOTAL_TESTS":  str(plan.get("total_tests", 0)),
        "EST_DURATION": str(plan.get("est_total_s", 0)),
    }

    print(f"\n  Jenkins job  : {JENKINS_JOB}")
    print(f"  Run ID       : {run_id}")
    print(f"  PW command   : {pw_command}")
    print(f"  Parameters   : {json.dumps(params, indent=4)}")

    if dry_run:
        print("\n  [DRY RUN] Jenkins trigger skipped.")
        return None

    crumb   = get_crumb() or {}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(crumb)

    is_parameterized = _is_parameterized_job(JENKINS_JOB, crumb)
    print(f"  Job type     : {'parameterized' if is_parameterized else 'plain (not parameterized)'}")

    endpoint = (
        f"{JENKINS_URL}/job/{JENKINS_JOB}/buildWithParameters"
        if is_parameterized
        else f"{JENKINS_URL}/job/{JENKINS_JOB}/build"
    )
    post_data = params if is_parameterized else {}

    try:
        r = requests.post(
            endpoint,
            auth=(JENKINS_USER, JENKINS_TOKEN),
            headers=headers,
            data=post_data,
            timeout=15,
        )
        if r.ok or r.status_code == 201:
            location = r.headers.get("Location", "")
            print(f"\n  ✓ Jenkins build queued: {location}")
            if not is_parameterized:
                _write_trigger_sidecar(project_key, run_id, pw_command, plan)
            return location
        else:
            print(f"\n  ✗ Jenkins returned {r.status_code}: {r.text[:300]}")
            print("\n  ℹ  If the job is parameterized in Jenkins, add these parameters:")
            for k, v in params.items():
                print(f"       {k} = (String Parameter)")
            print("\n  ℹ  Or make the Jenkinsfile read tests/steps/{PROJECT}_test_plan.json directly.")
            return None
    except Exception as exc:
        print(f"\n  ✗ Jenkins trigger failed: {exc}")
        return None


def _write_trigger_sidecar(
    project_key: str, run_id: str, pw_command: str, plan: Dict
) -> None:
    """
    For non-parameterized jobs: write trigger context to a JSON sidecar
    that the Jenkinsfile can read with `readJSON` or a shell `cat` step.

    Jenkinsfile snippet to consume this:
        script {
            def ctx = readJSON file: "tests/steps/${PROJECT_KEY}_trigger.json"
            sh ctx.pw_command
        }
    """
    sidecar_path = os.path.join(
        STEPS_DIR, f"{project_key}_trigger.json"
    )
    sidecar = {
        "run_id":        run_id,
        "project_key":   project_key,
        "pw_command":    pw_command,
        "generated_at":  datetime.datetime.utcnow().isoformat(),
        "total_tests":   plan.get("total_tests", 0),
        "est_total_s":   plan.get("est_total_s", 0),
        "grep_tags":     plan.get("grep_tags", []),
        "spec_files":    plan.get("spec_files", []),
    }
    with open(sidecar_path, "w") as f:
        json.dump(sidecar, f, indent=2)
    print(f"  ✓ Trigger sidecar written → {sidecar_path}")
    print(f"    Jenkinsfile can read this with: readJSON file: \"{sidecar_path}\"")


# ══════════════════════════════════════════════════════════════════════════════
# §3  DB — record the run
# ══════════════════════════════════════════════════════════════════════════════

def record_run(project_key: str, run_id: str, plan: Dict,
               db_path: str, pw_command: str) -> None:
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            INSERT OR IGNORE INTO runs
                (run_id, project_key, triggered_by, branch,
                 commit_sha, started_at, total_tests)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_key,
                "jenkins_trigger.py",
                os.getenv("GIT_BRANCH", "main"),
                os.getenv("GIT_COMMIT", ""),
                datetime.datetime.utcnow().isoformat(),
                plan.get("total_tests", 0),
            ),
        )
        conn.commit()
        conn.close()
        print(f"  ✓ Run recorded in DB: {run_id}")
    except Exception as exc:
        print(f"  ⚠ DB record failed: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# §4  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-2 Jenkins Trigger")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--plan",     default=None, help="Path to test plan JSON")
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    project_key = args.project
    run_id      = f"{project_key}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    print(f"\n{'='*60}")
    print(f"Jenkins Trigger — {project_key}")
    print(f"{'='*60}")

    plan       = load_plan(project_key, args.plan)
    pw_command = build_playwright_command(plan, project_key)
    queue_url  = trigger_jenkins(project_key, pw_command, run_id, plan, args.dry_run)

    if not args.dry_run:
        record_run(project_key, run_id, plan, args.db, pw_command)

    print(f"\n{'='*60}")
    print(f"✓ UC-2 complete — run_id: {run_id}")
    if queue_url:
        print(f"  Track at: {JENKINS_URL}/job/{JENKINS_JOB}/")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()