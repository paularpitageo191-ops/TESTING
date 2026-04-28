#!/usr/bin/env python3
"""
Jira Sync Agent (Enhanced - Production Ready)

Pulls:
- Story
- Related Epic
- Subtasks
- Comments (all levels)
- Attachments (all levels)

Stores in:
docs/jira_sync/{ISSUE}_{TIMESTAMP}/
"""

import os
import json
import argparse
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

# Load env
load_dotenv()

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")


class JiraSyncAgent:

    def __init__(self):
        self.base_url = JIRA_BASE_URL
        self.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        self.headers = {
            "Accept": "application/json"
        }

    # --------------------------------------------------
    # Folder Management
    # --------------------------------------------------
    def create_run_folder(self, issue_key: str) -> Dict[str, str]:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = f"docs/jira_sync/{issue_key}_{timestamp}"

        paths = {
            "base": base,
            "story": f"{base}/story",
            "epic": f"{base}/epic",
            "subtasks": f"{base}/subtasks",
            "comments": f"{base}/comments",
            "attachments": f"{base}/attachments"
        }

        for p in paths.values():
            os.makedirs(p, exist_ok=True)

        return paths

    # --------------------------------------------------
    # Jira Fetch
    # --------------------------------------------------
    def get_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"

        params = {
            "expand": "renderedFields,comment"
        }

        try:
            r = requests.get(url, headers=self.headers, params=params, auth=self.auth)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"✗ Failed to fetch {issue_key}: {e}")
            return None

    # --------------------------------------------------
    # Epic Resolution (Generic — works across Classic and Next-Gen projects)
    # --------------------------------------------------
    def get_epic_key(self, issue: Dict[str, Any]) -> Optional[str]:
        fields = issue.get("fields", {})

        # 1. Next-Gen / Team-managed: parent field holds the epic directly
        parent = fields.get("parent")
        if isinstance(parent, dict):
            parent_key = parent.get("key")
            parent_type = (parent.get("fields", {}).get("issuetype") or {}).get("name", "")
            if parent_key and parent_type.lower() == "epic":
                return parent_key
            # Even if type isn't epic, a parent reference is valid context
            if parent_key:
                return parent_key

        # 2. Classic projects: scan ALL fields for epic-link patterns
        #    (customfield IDs vary by Jira instance — don't hardcode just a few)
        for field_name, val in fields.items():
            if val is None:
                continue
            # String fields that look like a Jira issue key (e.g. "PROJ-123")
            if isinstance(val, str) and re.match(r'^[A-Z][A-Z0-9]+-\d+$', val):
                if "epic" in field_name.lower() or "link" in field_name.lower():
                    return val
            # Dict fields with a key sub-field (e.g. customfield_10014: {key: "PROJ-1"})
            if isinstance(val, dict) and val.get("key"):
                if "epic" in field_name.lower() or "link" in field_name.lower() or "parent" in field_name.lower():
                    return val["key"]

        return None

    # --------------------------------------------------
    # Subtasks
    # --------------------------------------------------
    def get_subtasks(self, issue: Dict[str, Any]) -> List[str]:
        return [s["key"] for s in issue.get("fields", {}).get("subtasks", [])]

    # --------------------------------------------------
    # Comments
    # --------------------------------------------------
    def extract_comments(self, issue: Dict[str, Any]) -> List[Dict[str, Any]]:
        comments_data = issue.get("fields", {}).get("comment", {})
        comments = comments_data.get("comments", [])

        extracted = []

        for c in comments:
            extracted.append({
                "author": c["author"]["displayName"],
                "body": c["body"],
                "created": c["created"]
            })

        return extracted

    # --------------------------------------------------
    # Attachments
    # --------------------------------------------------
    def download_attachments(self, issue: Dict[str, Any], save_dir: str) -> List[str]:
        attachments = issue.get("fields", {}).get("attachment", [])
        downloaded = []

        for att in attachments:
            filename = att["filename"]
            url = att["content"]

            path = os.path.join(save_dir, f"{issue['key']}_{filename}")

            try:
                r = requests.get(url, headers=self.headers, auth=self.auth)
                r.raise_for_status()

                with open(path, "wb") as f:
                    f.write(r.content)

                downloaded.append(path)
                print(f"  ✓ {filename}")

            except Exception as e:
                print(f"  ✗ Failed {filename}: {e}")

        return downloaded

    # --------------------------------------------------
    # Main Sync
    # --------------------------------------------------
    def sync(self, issue_key: str):

        print(f"\n{'='*60}")
        print(f"Jira Sync Agent — {issue_key}")
        print(f"{'='*60}")

        paths = self.create_run_folder(issue_key)

        # ---------------------------
        # STEP 1: Story
        # ---------------------------
        story = self.get_issue(issue_key)
        if not story:
            return

        # Save story
        with open(f"{paths['story']}/{issue_key}.json", "w") as f:
            json.dump(story, f, indent=2)

        # ---------------------------
        # STEP 2: Epic
        # ---------------------------
        epic_key = self.get_epic_key(story)
        epic = None

        if epic_key:
            print(f"\n[+] Found Epic: {epic_key}")
            epic = self.get_issue(epic_key)

            if epic:
                with open(f"{paths['epic']}/{epic_key}.json", "w") as f:
                    json.dump(epic, f, indent=2)

        # ---------------------------
        # STEP 3: Subtasks
        # ---------------------------
        subtask_keys = self.get_subtasks(story)
        subtasks = []

        for sk in subtask_keys:
            print(f"[+] Subtask: {sk}")
            sub = self.get_issue(sk)
            if sub:
                subtasks.append(sub)

                with open(f"{paths['subtasks']}/{sk}.json", "w") as f:
                    json.dump(sub, f, indent=2)

        # ---------------------------
        # STEP 4: Process ALL Issues
        # ---------------------------
        all_issues = [story] + ([epic] if epic else []) + subtasks

        all_comments = []
        all_attachments = []

        print(f"\n[+] Processing {len(all_issues)} issues")

        for issue in all_issues:
            key = issue["key"]

            print(f"\n--- {key} ---")

            # COMMENTS
            comments = self.extract_comments(issue)
            all_comments.extend(comments)

            with open(f"{paths['comments']}/{key}_comments.json", "w") as f:
                json.dump(comments, f, indent=2)

            # ATTACHMENTS
            atts = self.download_attachments(issue, paths["attachments"])
            all_attachments.extend(atts)

        # ---------------------------
        # STEP 5: Consolidated Output
        # ---------------------------
        output = {
            "issue": issue_key,
            "epic": epic_key,
            "subtasks": subtask_keys,
            "total_comments": len(all_comments),
            "total_attachments": len(all_attachments),
            "created_at": datetime.now().isoformat()
        }

        with open(f"{paths['base']}/summary.json", "w") as f:
            json.dump(output, f, indent=2)

        print(f"\n{'='*60}")
        print("✓ Sync Completed")
        print(f"✓ Folder: {paths['base']}")
        print(f"✓ Comments: {len(all_comments)}")
        print(f"✓ Attachments: {len(all_attachments)}")
        # ── Copy story to inbox so quality_alignment can read it ───────────
        inbox_dir  = os.path.join("docs", "inbox")
        os.makedirs(inbox_dir, exist_ok=True)
        inbox_path = os.path.join(inbox_dir, f"{issue_key}.json")

        # Build a flat inbox-compatible dict from the story fields
        story_fields = story.get("fields", {}) if story else {}

        def _adf_to_text(node) -> str:
            if isinstance(node, str):  return node
            if isinstance(node, list): return "\n".join(_adf_to_text(n) for n in node)
            if isinstance(node, dict):
                if node.get("text"): return node["text"]
                return _adf_to_text(node.get("content", []))
            return ""

        
        # Build a flat inbox-compatible dict from the story fields
        story_fields = story.get("fields", {}) if story else {}

        def _adf_to_text(node) -> str:
            if isinstance(node, str):  return node
            if isinstance(node, list): return "\n".join(_adf_to_text(n) for n in node)
            if isinstance(node, dict):
                if node.get("text"): return node["text"]
                return _adf_to_text(node.get("content", []))
            return ""

        # Extract full description text first — needed by both AC extraction and fallback
        full_description = _adf_to_text(story_fields.get("description", ""))

        # ── Extract acceptance criteria — format-agnostic ─────────────────
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        ac_text = ""

        # Step 1: scan ALL fields for anything that looks like acceptance criteria
        # (customfield IDs vary per Jira instance — don't rely on specific IDs)
        AC_FIELD_HINTS = ("acceptance", "criteria", "ac_", "_ac", "definition_of_done", "dod")
        for field_name, val in story_fields.items():
            if not val:
                continue
            if any(hint in field_name.lower() for hint in AC_FIELD_HINTS):
                candidate = _adf_to_text(val).strip()
                if candidate and len(candidate) > 20:
                    ac_text = candidate
                    print(f"  ✓ AC found in field '{field_name}'")
                    break

        # Step 2: if no custom field, use LLM to extract from description
        if not ac_text and full_description.strip():
            try:
                from llm_gateway import get_llm_gateway
                gateway = get_llm_gateway()
                ac_text = gateway.chat(
                    f"Extract ONLY the acceptance criteria from this Jira story "
                    f"description. Return the full AC text verbatim, preserving "
                    f"all AC numbers, bullet points and test data. "
                    f"If there are no acceptance criteria, return the empty string.\n\n"
                    f"Description:\n{full_description[:4000]}",
                    system_prompt=(
                        "You are a requirements analyst. Extract acceptance criteria "
                        "from Jira story descriptions. The format varies — look for "
                        "sections labelled 'Acceptance Criteria', 'AC1/AC2', "
                        "'Given/When/Then', checkbox lists '- [ ]', or numbered lists. "
                        "Return ONLY the extracted AC text, no commentary."
                    ),
                    temperature=0.0,
                    timeout=60,
                )
                if ac_text and len(ac_text.strip()) > 20:
                    ac_text = ac_text.strip()
                    print(f"  ✓ AC extracted via LLM ({len(ac_text)} chars)")
                else:
                    ac_text = full_description
                    print(f"  ⚠ No AC found — using full description as context")
            except Exception as exc:
                print(f"  ⚠ LLM AC extraction failed: {exc} — using full description")
                ac_text = full_description

        # ── Build epic context for inbox ───────────────────────────────────
        epic_inbox = epic_key or ""
        if epic and isinstance(epic, dict):
            ef = epic.get("fields", {})

            # Scan all fields for epic name and goal — IDs vary per Jira instance
            epic_name = ef.get("summary", "")
            epic_goal = ""
            for field_name, val in ef.items():
                if not val:
                    continue
                fl = field_name.lower()
                if "epic_name" in fl or ("epic" in fl and "name" in fl):
                    candidate = _adf_to_text(val).strip()
                    if candidate:
                        epic_name = candidate
                if "goal" in fl or "objective" in fl or "vision" in fl:
                    candidate = _adf_to_text(val).strip()
                    if candidate and len(candidate) > 10:
                        epic_goal = candidate

            epic_inbox = {
                "key":         epic_key or "",
                "summary":     ef.get("summary", ""),
                "description": _adf_to_text(ef.get("description", "")),
                "status":      ef.get("status", {}).get("name", "") if isinstance(ef.get("status"), dict) else "",
                "epic_name":   epic_name,
                "epic_goal":   epic_goal,
            }

        # ── Enrich inbox item with subtasks and all comments ──────────────
        # Subtasks: include key + summary + description of each
        inbox_subtasks = []
        for sub in subtasks:
            sf = sub.get("fields", {})
            inbox_subtasks.append({
                "key":         sub.get("key", ""),
                "summary":     sf.get("summary", ""),
                "description": _adf_to_text(sf.get("description", "")),
                "status":      sf.get("status", {}).get("name", "") if isinstance(sf.get("status"), dict) else "",
            })

        # All comments across story + subtasks, flattened
        inbox_comments = []
        for issue in all_issues:
            key = issue.get("key", "")
            c_data = issue.get("fields", {}).get("comment", {})
            for c in (c_data.get("comments", []) if isinstance(c_data, dict) else []):
                body = _adf_to_text(c.get("body", "")).strip()
                if body:
                    inbox_comments.append({
                        "issue":   key,
                        "author":  c.get("author", {}).get("displayName", ""),
                        "body":    body,
                        "created": c.get("created", ""),
                    })

        inbox_item = {
            "key":                 issue_key,
            "summary":             story_fields.get("summary", ""),
            "description":         full_description,
            "acceptance_criteria": ac_text,
            "project_key":         issue_key,
            "epic":                epic_inbox,
            "subtasks":            inbox_subtasks,
            "comments":            inbox_comments,
        }

        with open(inbox_path, "w") as f:
            json.dump([inbox_item], f, indent=2)
        print(f"✓ Story + {len(inbox_subtasks)} subtasks + {len(inbox_comments)} comments copied to inbox → {inbox_path}")
        print(f"{'='*60}\n")

# --------------------------------------------------
# CLI
# --------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", required=True, help="Jira Issue Key (e.g., SCRUM-86)")
    args = parser.parse_args()

    agent = JiraSyncAgent()
    agent.sync(args.issue)


if __name__ == "__main__":
    main()