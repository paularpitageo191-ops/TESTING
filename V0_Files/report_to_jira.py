#!/usr/bin/env python3

import os
import json
import glob
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

HEALING_LOGS_DIR = "docs/healing-logs"
JIRA_REPORT_DIR = "docs/jira-reports"
TEST_RESULTS_DIR = "test-results"

JIRA_CONFIG = {
    "url": os.getenv("JIRA_BASE_URL"),
    "username": os.getenv("JIRA_EMAIL"),
    "api_token": os.getenv("JIRA_API_TOKEN"),
}


class ReportingAgent:

    def __init__(self, project_key):
        self.project_key = project_key

        self.all_tests = []
        self.passed_tests = []
        self.failed_tests = []
        self.created_bugs = []

    # ─────────────────────────────
    # MAIN
    # ─────────────────────────────

    def run(self, dry_run=False):
        print("=" * 60)
        print("TEA Reporting Agent — FINAL")
        print("=" * 60)

        if not self._load_healing():
            print("❌ No healing logs found")
            return

        if not dry_run and not self._validate_jira():
            return

        for t in self.failed_tests:
            self._handle_failure(t, dry_run)

        for t in self.passed_tests:
            print(f"✅ {t['title']}")

        self._post_success_summary(dry_run)
        self._generate_report()

    # ─────────────────────────────
    # LOAD
    # ─────────────────────────────

    def _load_healing(self):
        files = sorted(glob.glob(f"{HEALING_LOGS_DIR}/*.json"), reverse=True)
        if not files:
            return False

        data = json.load(open(files[0]))
        status_map = {"PASS": "passed", "FAIL": "failed"}

        for r in data.get("results", []):
            test = {
                "title": r["title"],
                "status": status_map.get(r["tea_status"])
            }

            self.all_tests.append(test)

            if test["status"] == "passed":
                self.passed_tests.append(test)
            else:
                self.failed_tests.append(test)

        print(f"Loaded {len(self.all_tests)} tests")
        return True

    # ─────────────────────────────
    # JIRA VALIDATION
    # ─────────────────────────────

    def _validate_jira(self):
        r = requests.get(
            f"{JIRA_CONFIG['url']}/rest/api/2/myself",
            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"])
        )

        if not r.ok:
            print("❌ Jira auth failed")
            return False

        print("✅ Jira authenticated")
        print(f"🔗 {JIRA_CONFIG['url']}/browse/{self.project_key}")
        return True

    # ─────────────────────────────
    # FAILURE FLOW
    # ─────────────────────────────

    def _handle_failure(self, test, dry_run):
        print(f"❌ {test['title']}")

        artifacts = self._find_artifacts()

        if dry_run:
            return

        base = JIRA_CONFIG["url"]
        proj = self.project_key.split("-")[0]

        desc = f"""
h2. Automated Test Failure

*Story:* [{self.project_key}|{base}/browse/{self.project_key}]
*Test:* {test['title']}

h3. Error
{{code}}
{artifacts.get("error", "")[:1000]}
{{code}}

h3. Analysis
Failure detected via automation. Needs investigation.
"""

        payload = {
            "fields": {
                "project": {"key": proj},
                "summary": f"[TEA] {test['title']} failed",
                "description": desc,
                "issuetype": {"name": "Bug"}
            }
        }

        r = requests.post(
            f"{base}/rest/api/2/issue",
            json=payload,
            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"])
        )

        bug_key = r.json()["key"]
        bug_url = f"{base}/browse/{bug_key}"

        print(f"🔗 {bug_url}")

        # attach screenshot
        if artifacts.get("screenshot"):
            with open(artifacts["screenshot"], "rb") as f:
                requests.post(
                    f"{base}/rest/api/2/issue/{bug_key}/attachments",
                    headers={"X-Atlassian-Token": "no-check"},
                    auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"]),
                    files={"file": f}
                )

        # link bug
        requests.post(
            f"{base}/rest/api/2/issueLink",
            json={
                "type": {"name": "Blocks"},
                "inwardIssue": {"key": bug_key},
                "outwardIssue": {"key": self.project_key}
            },
            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"])
        )

        # story comment
        requests.post(
            f"{base}/rest/api/2/issue/{self.project_key}/comment",
            json={"body": f"❌ Bug created: {bug_key}"},
            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"])
        )

        self.created_bugs.append(bug_url)

    # ─────────────────────────────
    # SUCCESS FLOW
    # ─────────────────────────────

    def _post_success_summary(self, dry_run):
        if dry_run:
            return

        base = JIRA_CONFIG["url"]

        tests = "\n".join([f"✔ {t['title']}" for t in self.passed_tests])

        summary = f"""
✅ Automation Execution Report

*Story:* [{self.project_key}|{base}/browse/{self.project_key}]

Total: {len(self.all_tests)}
Passed: {len(self.passed_tests)}
Failed: {len(self.failed_tests)}

Tests:
{tests}

Analysis:
System stable. No failures detected.

Time: {datetime.now()}
"""

        requests.post(
            f"{base}/rest/api/2/issue/{self.project_key}/comment",
            json={"body": summary},
            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"])
        )

        self._attach_screenshot_to_story()

    def _attach_screenshot_to_story(self):
        base = JIRA_CONFIG["url"]

        for root, _, files in os.walk(TEST_RESULTS_DIR):
            for f in files:
                if f.endswith(".png"):
                    path = os.path.join(root, f)

                    with open(path, "rb") as fh:
                        requests.post(
                            f"{base}/rest/api/2/issue/{self.project_key}/attachments",
                            headers={"X-Atlassian-Token": "no-check"},
                            auth=(JIRA_CONFIG["username"], JIRA_CONFIG["api_token"]),
                            files={"file": fh}
                        )
                    return

    # ─────────────────────────────
    # ARTIFACTS
    # ─────────────────────────────

    def _find_artifacts(self):
        artifacts = {}

        for root, _, files in os.walk(TEST_RESULTS_DIR):
            for f in files:
                path = os.path.join(root, f)

                if f == "error-context.md":
                    artifacts["error"] = open(path).read()
                elif f.endswith(".png"):
                    artifacts["screenshot"] = path

        return artifacts

    # ─────────────────────────────
    # LOCAL REPORT
    # ─────────────────────────────

    def _generate_report(self):
        os.makedirs(JIRA_REPORT_DIR, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        filename = f"{self.project_key}-report-{datetime.now().strftime('%H%M')}.md"
        path = os.path.join(JIRA_REPORT_DIR, filename)

        overall_status = "FAILED" if self.failed_tests else "PASSED"
        icon = "❌" if self.failed_tests else "✅"

        lines = []

        # ─────────────────────────────
        # HEADER
        # ─────────────────────────────
        lines.append(f"# {icon} Execution Report: {self.project_key}")
        lines.append("")
        lines.append(f"## Requirement Status: [{overall_status}]")
        lines.append("")
        lines.append(f"**SCRUM ID:** {self.project_key}")
        lines.append(f"**Date:** {timestamp}")
        lines.append(f"**Status:** {overall_status}")
        lines.append("")

        # ─────────────────────────────
        # SUMMARY
        # ─────────────────────────────
        lines.append("## 📊 Execution Summary")
        lines.append("")
        lines.append(f"- Total Tests: {len(self.all_tests)}")
        lines.append(f"- Passed: {len(self.passed_tests)}")
        lines.append(f"- Failed: {len(self.failed_tests)}")
        lines.append("")

        # ─────────────────────────────
        # TEST DETAILS
        # ─────────────────────────────
        lines.append("## 🧪 Test Results")
        lines.append("")

        for t in self.all_tests:
            status_icon = "✅" if t["status"] == "passed" else "❌"
            lines.append(f"- {status_icon} {t['title']}")

        lines.append("")

        # ─────────────────────────────
        # ANALYSIS
        # ─────────────────────────────
        lines.append("## 🔍 Analysis")
        lines.append("")

        if self.failed_tests:
            lines.append("- One or more tests failed")
            lines.append("- Possible regression or locator issue detected")
            lines.append("- Requires investigation using artifacts below")
        else:
            lines.append("- All scenarios executed successfully")
            lines.append("- No regressions detected")
            lines.append("- System behavior is stable")

        lines.append("")

        # ─────────────────────────────
        # FAILURE DETAILS
        # ─────────────────────────────
        if self.failed_tests:
            lines.append("## ❌ Failure Details")
            lines.append("")

            for t in self.failed_tests:
                artifacts = self._find_artifacts()

                lines.append(f"### Test: {t['title']}")
                lines.append("")

                # Screenshot
                if artifacts.get("screenshot"):
                    rel_path = artifacts["screenshot"]
                    lines.append(f"![Failure Screenshot]({rel_path})")
                    lines.append("")

                # Error block
                if artifacts.get("error"):
                    lines.append("<details>")
                    lines.append("<summary>Error Context</summary>\n")
                    lines.append("```")
                    lines.append(artifacts["error"][:2000])
                    lines.append("```")
                    lines.append("</details>")
                    lines.append("")

        # ─────────────────────────────
        # SUCCESS EVIDENCE
        # ─────────────────────────────
        else:
            lines.append("## 📸 Execution Evidence")
            lines.append("")

            # attach one screenshot
            artifacts = self._find_artifacts()

            if artifacts.get("screenshot"):
                lines.append(f"![Execution Screenshot]({artifacts['screenshot']})")
                lines.append("")

        # ─────────────────────────────
        # BUSINESS IMPACT
        # ─────────────────────────────
        lines.append("## 💼 Business Impact")
        lines.append("")

        if self.failed_tests:
            lines.append("- Feature behavior is impacted")
            lines.append("- May affect user workflows")
            lines.append("- Immediate attention required")
        else:
            lines.append("- Feature validated successfully")
            lines.append("- No impact on user journey")
            lines.append("- Safe for release")

        lines.append("")

        # ─────────────────────────────
        # RECOMMENDATIONS
        # ─────────────────────────────
        lines.append("## 🛠 Recommended Actions")
        lines.append("")

        if self.failed_tests:
            lines.append("- Investigate failure logs and screenshots")
            lines.append("- Verify selectors and DOM structure")
            lines.append("- Re-run after fixes")
        else:
            lines.append("- Proceed with deployment")
            lines.append("- Monitor in production")
            lines.append("- Maintain regression suite")

        lines.append("")

        # ─────────────────────────────
        # JIRA LINKS
        # ─────────────────────────────
        base = JIRA_CONFIG["url"]

        lines.append("## 🔗 References")
        lines.append("")
        lines.append(f"- Story: {base}/browse/{self.project_key}")

        if self.created_bugs:
            for bug in self.created_bugs:
                lines.append(f"- Bug: {bug}")

        lines.append("")

        lines.append("---")
        lines.append("*Generated by TEA Reporting Agent*")

        # write file
        with open(path, "w") as f:
            f.write("\n".join(lines))

        print(f"\n📄 Markdown report generated:")
        print(f"   {path}")


# ENTRY
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    ReportingAgent(args.project).run(args.dry_run)