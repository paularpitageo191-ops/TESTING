#!/usr/bin/env python3
"""
TEA Self-Healing Agent
======================
Orchestrates the full pipeline after a Playwright test run:

  1. Parse Playwright JSON results
  2. Detect UI drift per-step via Qdrant semantic similarity
  3. Classify each result: PASS | FAIL | DRIFT | FLAKY
  4. Report to Zephyr Scale  → Test Cycle + per-test Executions + Screenshots
  5. Create Jira Bugs for failures, linked to the parent Story/Epic
  6. Persist flakiness history and run summaries for trend analysis

UI Drift Detection (no wireframe change needed)
───────────────────────────────────────────────
Each failing test's smartAction intents are embedded and searched in Qdrant.
A low cosine similarity score means the DOM element has drifted:

  ≥ 0.55  → no drift (element found confidently)
  0.40–0.54 → cosmetic drift   (label/text changed)
  0.25–0.39 → structural drift (element moved/replaced)
  < 0.25  → functional drift  (element removed entirely)

Usage
─────
  # Full run (push to Zephyr + Jira)
  python3 self_healing_agent.py --project SCRUM-86

  # Preview only — no network calls to Zephyr/Jira
  python3 self_healing_agent.py --project SCRUM-86 --dry-run

Prerequisite: run Playwright with JSON reporter
  npx playwright test --reporter=json > test-results/results.json
  # or in playwright.config.ts:
  # reporter: [['json', { outputFile: 'test-results/results.json' }]]
"""

import os
import sys
import json
import re
import glob
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Configuration ──────────────────────────────────────────────────────────

QDRANT_URL      = os.getenv("QDRANT_URL",       "http://localhost:6333")
OLLAMA_HOST     = os.getenv("OLLAMA_HOST",       "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL",   "mxbai-embed-large:latest")

ZEPHYR_BASE     = os.getenv("ZEPHYR_BASE_URL",   "https://api.zephyrscale.smartbear.com/v2")
ZEPHYR_TOKEN    = os.getenv("ZEPHYR_TOKEN",      "")

JIRA_BASE       = os.getenv("JIRA_BASE_URL",     "")
JIRA_EMAIL      = os.getenv("JIRA_EMAIL",        "")
JIRA_TOKEN      = os.getenv("JIRA_API_TOKEN",    "")

TEST_RESULTS_DIR = "test-results"
DOCS_DIR         = "docs"
HEALING_LOGS_DIR = os.path.join(DOCS_DIR, "healing-logs")

# Similarity thresholds for drift classification
DRIFT_NONE        = 0.55   # above this → element found, no drift
DRIFT_COSMETIC    = 0.40   # 0.40–0.54 → label/text changed
DRIFT_STRUCTURAL  = 0.25   # 0.25–0.39 → element moved/replaced
# below 0.25 → element removed entirely (functional drift)

# Playwright status → Zephyr Scale status name
PW_TO_ZEPHYR: Dict[str, str] = {
    "passed":      "Pass",
    "failed":      "Fail",
    "timedOut":    "Fail",
    "skipped":     "Not Executed",
    "interrupted": "Blocked",
}


# ══════════════════════════════════════════════════════════════════════════
# Zephyr Scale API v2 Client
# ══════════════════════════════════════════════════════════════════════════

class ZephyrClient:
    """
    Thin wrapper around the Zephyr Scale Cloud REST API v2.
    Docs: https://support.smartbear.com/zephyr-scale-cloud/api-docs/
    """

    def __init__(self, base_url: str, token: str, project_key: str):
        self.base = base_url.rstrip("/")
        self.proj = project_key
        self._h   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

    # ── HTTP helpers ───────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> Optional[dict]:
        try:
            r = requests.get(f"{self.base}{path}", headers=self._h,
                             params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"    [Zephyr GET {path}] {e}")
            return None

    def _post(self, path: str, payload: dict = None,
              files=None) -> Optional[dict]:
        try:
            if files:
                # Let requests set Content-Type (multipart boundary)
                h = {k: v for k, v in self._h.items() if k != "Content-Type"}
                r = requests.post(f"{self.base}{path}", headers=h,
                                  data=payload, files=files, timeout=30)
            else:
                r = requests.post(f"{self.base}{path}", headers=self._h,
                                  json=payload, timeout=15)
            r.raise_for_status()
            return r.json() if r.content else {}
        except requests.RequestException as e:
            print(f"    [Zephyr POST {path}] {e}")
            return None

    # ── Credential check ───────────────────────────────────────────────────

    def validate(self) -> bool:
        """Ping the projects endpoint to confirm token works."""
        data = self._get("/projects", params={"projectKey": self.proj})
        return data is not None

    # ── Test Cases ─────────────────────────────────────────────────────────

    def find_test_case(self, name: str) -> Optional[str]:
        """Return the key of an existing test case with this exact name."""
        data = self._get("/testcases", params={
            "projectKey": self.proj,
            "maxResults": 50,
            # Zephyr Scale supports JQL-like query param on some versions
        })
        if data and "values" in data:
            for tc in data["values"]:
                if tc.get("name", "").strip() == name.strip():
                    return tc["key"]
        return None

    def create_test_case(self, name: str, objective: str = "") -> Optional[str]:
        payload = {
            "projectKey": self.proj,
            "name":       name[:255],
            "objective":  objective[:1000] if objective else "",
            "status":     {"name": "Approved"},
            "priority":   {"name": "Normal"},
            "labels":     ["TEA", "automated"],
        }
        data = self._post("/testcases", payload)
        return data.get("key") if data else None

    def get_or_create_test_case(self, name: str,
                                 objective: str = "") -> Optional[str]:
        return self.find_test_case(name) or self.create_test_case(
            name, objective=objective)

    # ── Test Cycles ────────────────────────────────────────────────────────

    def create_test_cycle(self, name: str,
                           description: str = "") -> Optional[str]:
        """
        A Test Cycle groups all executions for one run.
        One cycle is created per self_healing_agent.py invocation.
        """
        payload = {
            "projectKey":  self.proj,
            "name":        name[:255],
            "description": description,
            "status":      {"name": "In Progress"},
        }
        data = self._post("/testcycles", payload)
        return data.get("key") if data else None

    def complete_test_cycle(self, cycle_key: str):
        """Mark cycle as Done after all executions are submitted."""
        # Zephyr Scale v2 uses PUT /testcycles/{key}
        try:
            r = requests.put(
                f"{self.base}/testcycles/{cycle_key}",
                headers=self._h,
                json={"status": {"name": "Done"}},
                timeout=10,
            )
        except Exception:
            pass  # Non-fatal

    # ── Test Executions ────────────────────────────────────────────────────

    def create_execution(
        self,
        test_case_key: str,
        cycle_key:     str,
        status:        str,
        duration_ms:   int  = 0,
        comment:       str  = "",
        environment:   str  = "Chromium",
    ) -> Optional[int]:
        """
        Create a test execution.
        Returns the numeric execution ID (needed for attachments).
        status: "Pass" | "Fail" | "Not Executed" | "Blocked" | "In Progress"
        """
        payload = {
            "projectKey":    self.proj,
            "testCaseKey":   test_case_key,
            "testCycleKey":  cycle_key,
            "statusName":    status,
            "executionTime": duration_ms,
            "comment":       comment[:5000] if comment else "",
            "environmentName": environment,
        }
        data = self._post("/testexecutions", payload)
        return data.get("id") if data else None

    def attach_to_execution(self, execution_id: int,
                             file_path: str) -> bool:
        """Attach a file (screenshot, log) to a test execution."""
        if not os.path.exists(file_path):
            return False
        fname = os.path.basename(file_path)
        mime  = "image/png" if fname.endswith(".png") else "application/octet-stream"
        with open(file_path, "rb") as fh:
            result = self._post(
                "/attachments",
                payload={"testExecutionId": str(execution_id)},
                files={"file": (fname, fh, mime)},
            )
        return result is not None

    def link_execution_to_issue(self, execution_id: int,
                                 issue_key: str) -> bool:
        """
        Link a test execution to a Jira issue for bidirectional traceability.
        In Zephyr Scale this makes the test result visible on the Jira issue.
        """
        result = self._post(
            f"/testexecutions/{execution_id}/links/issues",
            {"issueKey": issue_key},
        )
        return result is not None


# ══════════════════════════════════════════════════════════════════════════
# UI Change / Drift Detector
# ══════════════════════════════════════════════════════════════════════════

class UIChangeDetector:
    """
    Detects UI drift by comparing step intents against the Qdrant DOM memory.

    When the app's UI changes (without a wireframe/requirements change) the
    stored DOM embeddings no longer match the live page.  By measuring the
    cosine similarity for each step intent we can:
      • Flag which tests are at risk before they run
      • Classify HOW severe the change is (cosmetic vs structural vs functional)
      • Surface the specific steps that will break

    This works because BasePage.smartAction() stores every matched element
    in Qdrant with its semantic description.  A low match score means the
    element has changed or disappeared.
    """

    def __init__(self, project_key: str):
        self.proj       = project_key
        self.collection = f"{project_key}_ui_memory"

    def _embed(self, text: str) -> List[float]:
        """Generate an embedding via Ollama."""
        try:
            r = requests.post(
                f"{OLLAMA_HOST}/api/embeddings",
                json={"model": EMBEDDING_MODEL, "prompt": text},
                timeout=15,
            )
            return r.json().get("embedding", []) if r.ok else []
        except Exception:
            return []

    def similarity_for_intent(self, intent: str) -> float:
        """
        Search Qdrant for the intent and return the best similarity score.
        Returns 1.0 (assume OK) if Qdrant or Ollama is unreachable.
        """
        vector = self._embed(intent)
        if not vector:
            return 1.0
        try:
            r = requests.post(
                f"{QDRANT_URL}/collections/{self.collection}/points/search",
                json={
                    "vector":       vector,
                    "limit":        1,
                    "with_payload": False,
                    "filter": {"must": [
                        {"key": "project_key",
                         "match": {"value": self.proj}}
                    ]},
                },
                timeout=10,
            )
            results = r.json().get("result", []) if r.ok else []
            return results[0]["score"] if results else 0.0
        except Exception:
            return 1.0

    def classify_drift(self, score: float) -> str:
        """Map a similarity score to a human-readable drift category."""
        if score >= DRIFT_NONE:
            return "none"
        if score >= DRIFT_COSMETIC:
            return "cosmetic"     # label/text changed
        if score >= DRIFT_STRUCTURAL:
            return "structural"   # element moved or replaced
        return "functional"       # element removed entirely

    def scan_scenarios(self, scenarios: List[Dict]) -> Dict[str, Any]:
        """
        Scan a list of {title, steps[]} dicts.
        Returns a drift report keyed by scenario title.
        """
        report: Dict[str, Any] = {}
        for scenario in scenarios:
            name  = scenario.get("title", "unknown")
            steps = scenario.get("steps", [])
            if not steps:
                report[name] = {"drift_detected": False, "drifted_steps": [],
                                 "average_similarity": 1.0, "worst_drift_type": "none"}
                continue

            step_scores = []
            for step_intent in steps:
                score = self.similarity_for_intent(step_intent)
                step_scores.append({
                    "intent": step_intent,
                    "score":  score,
                    "drift":  self.classify_drift(score),
                })

            drifted  = [s for s in step_scores if s["drift"] != "none"]
            avg      = sum(s["score"] for s in step_scores) / len(step_scores)
            severity = {"functional": 3, "structural": 2, "cosmetic": 1, "none": 0}
            worst    = max(drifted, key=lambda s: severity[s["drift"]],
                           default=None)

            report[name] = {
                "average_similarity": round(avg, 3),
                "drifted_steps":      drifted,
                "drift_detected":     len(drifted) > 0,
                "worst_drift_type":   worst["drift"] if worst else "none",
            }
        return report


# ══════════════════════════════════════════════════════════════════════════
# Self-Healing Agent — main orchestrator
# ══════════════════════════════════════════════════════════════════════════

class SelfHealingAgent:
    """
    Orchestrates: parse results → detect drift → classify → Zephyr → Jira.

    Additional capabilities beyond basic reporting
    ───────────────────────────────────────────────
    • Flakiness tracking  — persists pass/fail history per test; flags tests
                            that fail intermittently (15–85 % failure rate)
    • Drift classification — distinguishes cosmetic / structural / functional
                            UI changes so the team knows what to fix
    • Traceability         — every Zephyr execution is linked back to the
                            Jira Story so the board shows live test status
    • Run summaries        — JSON snapshots in docs/healing-logs/ for trends
    """

    def __init__(self, project_key: str):
        self.project_key = project_key
        # Extract bare project key: "SCRUM-86" → "SCRUM"
        m = re.match(r'^([A-Z]+)', project_key)
        self.jira_proj   = m.group(1) if m else project_key

        self.zephyr   = (ZephyrClient(ZEPHYR_BASE, ZEPHYR_TOKEN, self.jira_proj)
                         if ZEPHYR_TOKEN else None)
        self.detector = UIChangeDetector(project_key)

        self.playwright_results: List[Dict] = []
        self.drift_report:       Dict       = {}
        self.cycle_key:          Optional[str] = None
        self.flakiness_db:       Dict       = self._load_flakiness_db()

    # ── Flakiness DB ───────────────────────────────────────────────────────

    def _flakiness_path(self) -> str:
        return os.path.join(HEALING_LOGS_DIR,
                            f"{self.project_key}-flakiness.json")

    def _load_flakiness_db(self) -> Dict:
        p = self._flakiness_path()
        try:
            return json.load(open(p)) if os.path.exists(p) else {}
        except Exception:
            return {}

    def _save_flakiness_db(self):
        os.makedirs(HEALING_LOGS_DIR, exist_ok=True)
        json.dump(self.flakiness_db, open(self._flakiness_path(), "w"), indent=2)

    def _record_result(self, name: str, passed: bool):
        e = self.flakiness_db.setdefault(
            name, {"passes": 0, "failures": 0, "consecutive_failures": 0})
        if passed:
            e["passes"] += 1
            e["consecutive_failures"] = 0
        else:
            e["failures"] += 1
            e["consecutive_failures"] += 1
        e["last_seen"] = datetime.now().isoformat()

    def _is_flaky(self, name: str) -> bool:
        e     = self.flakiness_db.get(name, {})
        total = e.get("passes", 0) + e.get("failures", 0)
        if total < 3:
            return False
        rate = e.get("failures", 0) / total
        return 0.15 < rate < 0.85   # intermittent — not always failing

    # ── Playwright result loading ──────────────────────────────────────────

    def load_playwright_results(self) -> List[Dict]:
        """
        Load the Playwright JSON reporter output.
        Searches test-results/*.json and test-results.json for the latest file.
        """
        candidates = sorted(
            glob.glob(os.path.join(TEST_RESULTS_DIR, "*.json")),
            key=os.path.getmtime, reverse=True,
        )
        if os.path.exists("test-results.json"):
            candidates.insert(0, "test-results.json")

        for path in candidates:
            try:
                data = json.load(open(path))
                if "suites" in data or "stats" in data:
                    print(f"  ✓ Results loaded from: {path}")
                    return self._flatten(data)
            except Exception:
                continue

        print("  ⚠ No Playwright JSON results found.")
        print("    Configure in playwright.config.ts:")
        print("      reporter: [['json', {outputFile: 'test-results/results.json'}]]")
        print("    Then run: npx playwright test")
        return []

    def _flatten(self, data: dict) -> List[Dict]:
        flat: List[Dict] = []

        def walk(suites: list, parent: str = ""):
            for suite in suites:
                title = suite.get("title", "")
                full  = f"{parent} › {title}".strip(" › ") if parent else title
                for spec in suite.get("specs", []):
                    for test in spec.get("tests", []):
                        all_results = test.get("results", [{}])
                        last        = all_results[-1]
                        status      = last.get("status", "failed")
                        flat.append({
                            "title":       spec.get("title", ""),
                            "suite":       full,
                            "status":      status,
                            "duration_ms": last.get("duration", 0),
                            "error":       (last.get("error") or {}).get("message", ""),
                            "screenshot":  self._screenshot_for(spec.get("title", "")),
                            "retry_count": len(all_results) - 1,
                        })
                walk(suite.get("suites", []), full)

        walk(data.get("suites", []))
        return flat

    def _screenshot_for(self, test_title: str) -> Optional[str]:
        safe = re.sub(r"[^a-zA-Z0-9]+", "-", test_title)[:30].lower()
        for d in glob.glob(os.path.join(TEST_RESULTS_DIR, f"*{safe}*")):
            for png in glob.glob(os.path.join(d, "**", "*.png"), recursive=True):
                return png
        # Fallback: any PNG in a dir with "failed" in its name
        for d in glob.glob(os.path.join(TEST_RESULTS_DIR, "*failed*")):
            for png in glob.glob(os.path.join(d, "**", "*.png"), recursive=True):
                return png
        return None

    # ── Drift detection ────────────────────────────────────────────────────

    def detect_ui_changes(self):
        """
        For each failing test, extract smartAction intents from the error
        message or spec file, then scan them against Qdrant.
        """
        failures = [r for r in self.playwright_results if r["status"] != "passed"]
        if not failures:
            print("  ✓ No failures to scan.")
            return

        scenarios = []
        for f in failures:
            steps = self._intents_from(f["error"], f["title"])
            scenarios.append({"title": f["title"], "steps": steps})

        self.drift_report = self.detector.scan_scenarios(scenarios)

        drifted = sum(1 for v in self.drift_report.values() if v["drift_detected"])
        print(f"  ✓ Scanned {len(scenarios)} failing scenario(s): "
              f"{drifted} with UI drift")
        if drifted:
            print("  ─ Drift details:")
            for name, info in self.drift_report.items():
                if info["drift_detected"]:
                    print(f"    ⚡ [{info['worst_drift_type'].upper()}] {name[:55]}")
                    for s in info["drifted_steps"][:2]:
                        print(f"        \"{s['intent'][:50]}\"  "
                              f"sim={s['score']:.2f}")

    def _intents_from(self, error_msg: str, test_title: str) -> List[str]:
        """Extract smartAction intent strings from an error message or spec file."""
        # Pattern: smartAction("intent here", ...)
        intents = re.findall(r'smartAction\("([^"]+)"', error_msg)
        if intents:
            return intents
        # Search spec files for the test title
        for f in glob.glob("tests/**/*.spec.ts", recursive=True):
            try:
                content = open(f).read()
                if test_title[:40] in content:
                    found = re.findall(r'smartAction\("([^"]+)"', content)
                    if found:
                        return found
            except Exception:
                pass
        return [test_title]

    # ── Classification ─────────────────────────────────────────────────────

    def _classify(self, flaky_names: List[str]) -> List[Dict]:
        """
        Assign a TEA status to each result:
          PASS  — test passed
          FLAKY — intermittently failing (not UI-related)
          DRIFT — failed AND Qdrant shows UI change
          FAIL  — failed, no UI drift detected
        """
        classified = []
        sev = {"functional": 3, "structural": 2, "cosmetic": 1, "none": 0}

        for r in self.playwright_results:
            drift  = self.drift_report.get(r["title"], {})
            passed = r["status"] == "passed"
            flaky  = r["title"] in flaky_names and not passed

            if passed:
                tea = "PASS"
            elif drift.get("drift_detected"):
                tea = "DRIFT"
            elif flaky:
                tea = "FLAKY"
            else:
                tea = "FAIL"

            zephyr_status = {
                "PASS": "Pass", "DRIFT": "Fail",
                "FLAKY": "Blocked", "FAIL": "Fail",
            }[tea]

            classified.append({
                **r,
                "tea_status":    tea,
                "drift_info":    drift,
                "is_flaky":      flaky,
                "zephyr_status": zephyr_status,
            })
        return classified

    # ── Zephyr reporting ───────────────────────────────────────────────────

    def _push_to_zephyr(self, classified: List[Dict],
                         cycle_name: str, dry_run: bool) -> Optional[str]:
        if dry_run:
            print(f"  [DRY RUN] Would create cycle: \"{cycle_name}\"")
            for r in classified:
                print(f"    {r['zephyr_status']:15}  {r['title'][:55]}")
            return None

        cycle_key = self.zephyr.create_test_cycle(
            cycle_name,
            description=f"TEA automated run for {self.project_key}",
        )
        if not cycle_key:
            print("  ✗ Could not create Zephyr test cycle — check token/project key")
            return None
        print(f"  ✓ Cycle created: {cycle_key}")

        for r in classified:
            tc_key = self.zephyr.get_or_create_test_case(
                name=r["title"],
                objective=f"Automated: {r['suite']}",
            )
            if not tc_key:
                print(f"  ⚠ Test case unavailable for: {r['title'][:40]}")
                continue

            # Build a rich execution comment
            lines = [f"TEA Status: {r['tea_status']}"]
            if r["error"]:
                lines.append(f"Error: {r['error'][:400]}")
            if r["drift_info"].get("drift_detected"):
                lines.append(f"UI Drift: {r['drift_info']['worst_drift_type']}")
                for s in r["drift_info"].get("drifted_steps", [])[:3]:
                    lines.append(f"  • \"{s['intent'][:50]}\"  "
                                 f"similarity={s['score']:.2f} ({s['drift']})")
            if r["is_flaky"]:
                e     = self.flakiness_db.get(r["title"], {})
                total = e.get("passes", 0) + e.get("failures", 0)
                lines.append(f"Flakiness: {e.get('failures',0)}/{total} runs failed")

            exec_id = self.zephyr.create_execution(
                test_case_key=tc_key,
                cycle_key=cycle_key,
                status=r["zephyr_status"],
                duration_ms=r["duration_ms"],
                comment="\n".join(lines),
            )

            if exec_id:
                # Attach screenshot for non-passing tests
                icon = "-"
                if r["zephyr_status"] != "Pass" and r.get("screenshot"):
                    icon = "✓" if self.zephyr.attach_to_execution(
                        exec_id, r["screenshot"]) else "⚠"

                # Link execution → Jira Story (traceability)
                self.zephyr.link_execution_to_issue(exec_id, self.project_key)

                print(f"  {r['zephyr_status']:15}  {tc_key}  "
                      f"[ss:{icon}]  {r['title'][:40]}")
            else:
                print(f"  ⚠ Execution failed for: {r['title'][:40]}")

        self.zephyr.complete_test_cycle(cycle_key)
        return cycle_key

    # ── Jira bug creation ──────────────────────────────────────────────────

    def _create_jira_bugs(self, failures: List[Dict], dry_run: bool):
        auth    = (JIRA_EMAIL, JIRA_TOKEN)
        base    = JIRA_BASE.rstrip("/")
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json"}

        for r in failures:
            drift_type = r["drift_info"].get("worst_drift_type", "none")
            tag        = "DRIFT" if r["tea_status"] == "DRIFT" else "TEA"
            summary    = (
                f"[{tag}] {r['error'][:90]}"
                if r["error"] else
                f"[{tag}] Test failure: {r['title'][:70]}"
            )

            desc_parts = [
                "h2. Automated Test Failure Report",
                "",
                f"*Story:*     [{self.project_key}|{base}/browse/{self.project_key}]",
                f"*Test:*      {r['title']}",
                f"*Suite:*     {r['suite']}",
                f"*TEA Status:* {r['tea_status']}",
                f"*Duration:*  {r['duration_ms']}ms",
                f"*Detected:*  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            ]
            if self.cycle_key:
                desc_parts.append(f"*Zephyr Cycle:* {self.cycle_key}")

            if drift_type != "none":
                desc_parts += [
                    "",
                    "h3. UI Drift Analysis",
                    f"*Drift Type:* {drift_type}",
                    "",
                    "||Step Intent||Similarity||Drift Type||",
                ]
                for s in r["drift_info"].get("drifted_steps", []):
                    desc_parts.append(
                        f"|{s['intent'][:60]}|{s['score']:.2f}|{s['drift']}|"
                    )
                desc_parts.append("")
                desc_parts.append(
                    "_The Qdrant DOM memory may need re-indexing for this project._"
                )

            if r["error"]:
                desc_parts += ["", "h3. Error", "{code}", r["error"][:3000], "{code}"]

            if r["is_flaky"]:
                e     = self.flakiness_db.get(r["title"], {})
                total = e.get("passes", 0) + e.get("failures", 0)
                desc_parts.append(
                    f"\n*⚠ Flaky test:* failed {e.get('failures',0)}/{total} runs"
                )

            description = "\n".join(desc_parts)

            if dry_run:
                print(f"  [DRY RUN] Bug: {summary[:65]}")
                continue

            # Create Bug
            resp = requests.post(
                f"{base}/rest/api/2/issue",
                json={
                    "fields": {
                        "project":     {"key": self.jira_proj},
                        "summary":     summary[:255],
                        "description": description,
                        "issuetype":   {"name": "Bug"},
                        "priority":    {"name": "High" if drift_type != "none"
                                        else "Medium"},
                        "labels":      (
                            ["TEA", "ui-drift", "automated-failure"]
                            if drift_type != "none"
                            else ["TEA", "automated-failure"]
                        ),
                    }
                },
                auth=auth, headers=headers, timeout=15,
            )

            if not resp.ok:
                print(f"  ✗ Bug creation failed ({resp.status_code}): "
                      f"{resp.text[:120]}")
                continue

            bug_key = resp.json()["key"]

            # Link: Bug blocks Story
            requests.post(
                f"{base}/rest/api/2/issueLink",
                json={
                    "type":         {"name": "Blocks"},
                    "inwardIssue":  {"key": bug_key},
                    "outwardIssue": {"key": self.project_key},
                },
                auth=auth, headers=headers, timeout=10,
            )

            # Comment on Story
            zephyr_line = (f"\n|Zephyr Cycle|{self.cycle_key}|"
                           if self.cycle_key else "")
            requests.post(
                f"{base}/rest/api/2/issue/{self.project_key}/comment",
                json={"body": (
                    f"*🤖 TEA Self-Healing Agent — failure detected*\n\n"
                    f"||Field||Value||\n"
                    f"|Bug|[{bug_key}|{base}/browse/{bug_key}]|\n"
                    f"|Type|{r['tea_status']}|\n"
                    f"|Drift|{drift_type}|"
                    f"{zephyr_line}"
                )},
                auth=auth, headers=headers, timeout=10,
            )

            # Attach screenshot to Bug
            if r.get("screenshot") and os.path.exists(r["screenshot"]):
                fname = os.path.basename(r["screenshot"])
                with open(r["screenshot"], "rb") as fh:
                    requests.post(
                        f"{base}/rest/api/2/issue/{bug_key}/attachments",
                        auth=auth,
                        headers={"X-Atlassian-Token": "no-check"},
                        files={"file": (fname, fh, "image/png")},
                        timeout=30,
                    )

            print(f"  ✓ Bug [{r['tea_status']}]  {bug_key}  — {r['title'][:45]}")

    # ── Run summary ────────────────────────────────────────────────────────

    def _save_summary(self, classified: List[Dict], ts: str):
        os.makedirs(HEALING_LOGS_DIR, exist_ok=True)
        slug = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(HEALING_LOGS_DIR,
                            f"{self.project_key}-run-{slug}.json")
        summary = {
            "project_key": self.project_key,
            "timestamp":   ts,
            "cycle_key":   self.cycle_key,
            "totals": {
                s: sum(1 for r in classified if r["tea_status"] == s)
                for s in ("PASS", "FAIL", "DRIFT", "FLAKY")
            },
            "results": [
                {
                    "title":      r["title"],
                    "tea_status": r["tea_status"],
                    "duration_ms": r["duration_ms"],
                    "drift_type": r["drift_info"].get("worst_drift_type", "none"),
                }
                for r in classified
            ],
        }
        json.dump(summary, open(path, "w"), indent=2)
        print(f"  Run summary → {path}")

    # ── Main entry ─────────────────────────────────────────────────────────

    def run(self, dry_run: bool = False):
        print("=" * 60)
        print("TEA Self-Healing Agent")
        print("=" * 60)
        if dry_run:
            print("  (DRY RUN — no Zephyr/Jira calls will be made)")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 1. Load results
        print("\n[1/6] Loading Playwright test results...")
        self.playwright_results = self.load_playwright_results()
        if not self.playwright_results:
            sys.exit(1)
        total  = len(self.playwright_results)
        passed = sum(1 for r in self.playwright_results if r["status"] == "passed")
        print(f"  Total: {total}  |  Passed: {passed}  |  Failed: {total - passed}")

        # 2. Flakiness tracking
        print("\n[2/6] Updating flakiness tracker...")
        for r in self.playwright_results:
            self._record_result(r["title"], r["status"] == "passed")
        flaky_names = [r["title"] for r in self.playwright_results
                       if self._is_flaky(r["title"])]
        if flaky_names:
            print(f"  ⚠ Flaky ({len(flaky_names)}): "
                  + ", ".join(f[:30] for f in flaky_names[:3]))
        else:
            print("  ✓ No flakiness detected")

        # 3. UI drift detection
        print("\n[3/6] Scanning for UI drift via Qdrant...")
        self.detect_ui_changes()

        # 4. Classify
        print("\n[4/6] Classifying results...")
        classified = self._classify(flaky_names)
        icons = {"PASS": "✓", "FAIL": "✗", "DRIFT": "⚡", "FLAKY": "〰"}
        for r in classified:
            print(f"  {icons[r['tea_status']]} [{r['tea_status']:5}] "
                  f"{r['title'][:55]}")

        # 5. Zephyr reporting
        print("\n[5/6] Reporting to Zephyr Scale...")
        if not ZEPHYR_TOKEN:
            print("  ⚠ ZEPHYR_TOKEN not set — skipping")
        elif not dry_run and not self.zephyr.validate():
            print("  ✗ Zephyr token invalid — skipping")
        else:
            cycle_name = (f"TEA  {self.project_key}  {ts}")
            self.cycle_key = self._push_to_zephyr(
                classified, cycle_name, dry_run=dry_run)
            if self.cycle_key:
                print(f"  ✓ Cycle complete: {self.cycle_key}")

        # 6. Jira bugs for failures
        print("\n[6/6] Creating Jira Bugs for failures...")
        failures = [r for r in classified
                    if r["tea_status"] in ("FAIL", "DRIFT")]
        if not failures:
            print("  ✓ No failures to report to Jira")
        elif not all([JIRA_BASE, JIRA_EMAIL, JIRA_TOKEN]):
            print("  ⚠ Jira credentials incomplete — skipping")
        else:
            self._create_jira_bugs(failures, dry_run=dry_run)

        # Persist state
        self._save_flakiness_db()
        self._save_summary(classified, ts)

        # Final summary
        totals = {s: sum(1 for r in classified if r["tea_status"] == s)
                  for s in ("PASS", "FAIL", "DRIFT", "FLAKY")}
        print("\n" + "=" * 60)
        print("TEA Self-Healing Agent — Run Complete")
        print(f"  ✓ PASS  {totals['PASS']:>3}   "
              f"✗ FAIL  {totals['FAIL']:>3}   "
              f"⚡ DRIFT {totals['DRIFT']:>3}   "
              f"〰 FLAKY {totals['FLAKY']:>3}")
        if self.cycle_key:
            print(f"  Zephyr Cycle: {self.cycle_key}")
        print("=" * 60)


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="TEA Self-Healing Agent — parse Playwright results, "
                    "detect UI drift, report to Zephyr Scale and Jira"
    )
    parser.add_argument("--project", required=True,
                        help="Jira Story or Epic key, e.g. SCRUM-86")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and classify without pushing to Zephyr/Jira")
    args = parser.parse_args()

    SelfHealingAgent(args.project).run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
