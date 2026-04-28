#!/usr/bin/env python3
"""
Phase 0: Jira Sync Agent (BMM Architecture)
Pulls stories, epics, and attachments from Jira for a given TARGET_ISSUE_ID.
Parses all attachments (CSV, Excel, PDF, text) and creates a consolidated requirements.json.
"""

import os
import json
import csv
import argparse
import requests
from datetime import datetime
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "https://your-company.atlassian.net")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "SCRUM")

# Path standardization
INBOX_DIR = "docs/inbox"
DOCS_DIR = "docs"

class JiraSyncAgent:
    def __init__(self, project_key: str = JIRA_PROJECT_KEY):
        self.project_key = project_key
        self.base_url = JIRA_BASE_URL
        self.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
    def get_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Fetch a single Jira issue by key."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"
        
        try:
            response = requests.get(url, headers=self.headers, auth=self.auth)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching issue {issue_key}: {e}")
            return None
    
    def get_issue_attachments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get all attachments from a Jira issue."""
        issue = self.get_issue(issue_key)
        if not issue:
            return []
        
        fields = issue.get("fields", {})
        attachments = fields.get("attachment", [])
        
        return attachments
    
    def download_attachment(self, attachment_url: str, filename: str) -> str:
        """Download an attachment and save it locally."""
        # Create attachments directory
        attachments_dir = os.path.join(INBOX_DIR, "attachments")
        os.makedirs(attachments_dir, exist_ok=True)
        
        filepath = os.path.join(attachments_dir, filename)
        
        try:
            response = requests.get(attachment_url, headers=self.headers, auth=self.auth)
            response.raise_for_status()
            
            with open(filepath, 'wb') as f:
                f.write(response.content)
            
            print(f"  ✓ Downloaded: {filename}")
            return filepath
            
        except Exception as e:
            print(f"  ✗ Error downloading {filename}: {e}")
            return ""
    
    def parse_csv_file(self, filepath: str) -> List[Dict[str, Any]]:
        """Parse a CSV file and extract requirements."""
        requirements = []
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    req = {
                        "type": "requirement",
                        "source": os.path.basename(filepath),
                        "data": dict(row)
                    }
                    requirements.append(req)
        except Exception as e:
            print(f"  ✗ Error parsing CSV {filepath}: {e}")
        
        return requirements
    
    def parse_excel_file(self, filepath: str) -> List[Dict[str, Any]]:
        """Parse an Excel file and extract requirements."""
        try:
            import pandas as pd
        except ImportError:
            print("  ✗ pandas not installed for Excel parsing")
            return []
        
        requirements = []
        
        try:
            excel_file = pd.ExcelFile(filepath)
            
            for sheet_name in excel_file.sheet_names:
                df = pd.read_excel(filepath, sheet_name=sheet_name)
                
                for _, row in df.iterrows():
                    req = {
                        "type": "requirement",
                        "source": f"{os.path.basename(filepath)} - {sheet_name}",
                        "data": row.to_dict()
                    }
                    requirements.append(req)
                    
        except Exception as e:
            print(f"  ✗ Error parsing Excel {filepath}: {e}")
        
        return requirements
    
    def parse_pdf_file(self, filepath: str) -> List[Dict[str, Any]]:
        """Parse a PDF file and extract text content."""
        try:
            import PyPDF2
        except ImportError:
            print("  ✗ PyPDF2 not installed for PDF parsing")
            return []
        
        requirements = []
        text_content = []
        
        try:
            with open(filepath, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                
                for page_num, page in enumerate(reader.pages):
                    text = page.extract_text()
                    if text and text.strip():
                        text_content.append(f"Page {page_num + 1}:\n{text}")
            
            if text_content:
                req = {
                    "type": "requirement",
                    "source": os.path.basename(filepath),
                    "data": {
                        "content": "\n\n".join(text_content),
                        "pages": len(text_content)
                    }
                }
                requirements.append(req)
                
        except Exception as e:
            print(f"  ✗ Error parsing PDF {filepath}: {e}")
        
        return requirements
    
    def parse_text_file(self, filepath: str) -> List[Dict[str, Any]]:
        """Parse a text file."""
        requirements = []
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                
            if content.strip():
                req = {
                    "type": "requirement",
                    "source": os.path.basename(filepath),
                    "data": {"content": content}
                }
                requirements.append(req)
                
        except Exception as e:
            print(f"  ✗ Error parsing text file {filepath}: {e}")
        
        return requirements
    
    def parse_attachment(self, filepath: str, filename: str) -> List[Dict[str, Any]]:
        """Parse an attachment based on its file type."""
        ext = os.path.splitext(filename)[1].lower()
        
        parsers = {
            '.csv': self.parse_csv_file,
            '.xlsx': self.parse_excel_file,
            '.xls': self.parse_excel_file,
            '.pdf': self.parse_pdf_file,
            '.txt': self.parse_text_file,
            '.md': self.parse_text_file,
            '.json': self.parse_text_file,
        }
        
        parser = parsers.get(ext)
        if parser:
            return parser(filepath)
        else:
            print(f"  ⚠ No parser available for {ext}")
            return []
    
    def get_epic_issues(self, epic_key: str) -> List[str]:
        """Get all issues linked to an epic."""
        url = f"{self.base_url}/rest/api/3/search"
        jql = f'parent = {epic_key} OR "Epic Link" = {epic_key}'
        
        params = {
            "jql": jql,
            "maxResults": 100,
            "fields": "key,summary"
        }
        
        try:
            response = requests.get(url, headers=self.headers, params=params, auth=self.auth)
            response.raise_for_status()
            data = response.json()
            
            issues = [issue['key'] for issue in data.get('issues', [])]
            return issues
            
        except Exception as e:
            print(f"  ✗ Error getting epic issues: {e}")
            return []
    
    def create_requirements_json(self, target_issue_id: str, output_dir: str = DOCS_DIR):
        """
        Main method: Create a consolidated requirements.json for a target issue.
        This includes the issue itself, its epic (if any), all stories, and parsed attachments.
        """
        print(f"\n{'='*60}")
        print(f"Jira Sync Agent - Creating requirements for {target_issue_id}")
        print(f"{'='*60}")
        
        # Get the target issue
        print(f"\n[1/5] Fetching target issue: {target_issue_id}")
        target_issue = self.get_issue(target_issue_id)
        
        if not target_issue:
            print(f"  ✗ Issue {target_issue_id} not found")
            return
        
        fields = target_issue.get("fields", {})
        issue_type = fields.get("issuetype", {}).get("name", "")
        
        print(f"  ✓ Found: {fields.get('summary', 'N/A')}")
        print(f"  ✓ Type: {issue_type}")
        
        # Collect all related issues
        all_issue_keys = [target_issue_id]
        epic_key = None
        
        # If it's a story, find its epic
        if issue_type in ["Story", "Task"]:
            # Try multiple possible Epic Link field IDs (varies by Jira instance)
            possible_epic_fields = [
                "customfield_10014",  # Common Epic Link field
                "customfield_10001",  # Alternative
                "customfield_10000",  # Another alternative
                "epic_link",          # Some instances use this
                "parent"              # Parent issue
            ]
            
            for field_id in possible_epic_fields:
                epic_link_field = fields.get(field_id)
                if epic_link_field:
                    # Handle both string and dict formats
                    if isinstance(epic_link_field, dict):
                        epic_key = epic_link_field.get("key")
                    else:
                        epic_key = str(epic_link_field)
                    
                    if epic_key:
                        all_issue_keys.append(epic_key)
                        print(f"  ✓ Epic Link found via {field_id}: {epic_key}")
                        break
            
            if not epic_key:
                print(f"  ℹ No epic link found for this story")
        
        # If it's an epic, get all child stories
        if issue_type == "Epic":
            child_issues = self.get_epic_issues(target_issue_id)
            all_issue_keys.extend(child_issues)
            print(f"  ✓ Found {len(child_issues)} child issues")
        
        # Fetch all related issues
        print(f"\n[2/5] Fetching {len(all_issue_keys)} related issues")
        all_issues = []
        for issue_key in all_issue_keys:
            issue = self.get_issue(issue_key)
            if issue:
                all_issues.append(issue)
                print(f"  ✓ {issue_key}: {issue['fields'].get('summary', 'N/A')}")
        
        # Download and parse attachments
        print(f"\n[3/5] Processing attachments")
        all_requirements = []
        total_attachments = 0
        
        for issue in all_issues:
            issue_key = issue['key']
            attachments = issue['fields'].get('attachment', [])
            
            if attachments:
                print(f"\n  Processing attachments for {issue_key}:")
                
                for attachment in attachments:
                    filename = attachment['filename']
                    attachment_url = attachment['content']
                    total_attachments += 1
                    
                    # Download attachment
                    filepath = self.download_attachment(attachment_url, filename)
                    
                    if filepath:
                        # Parse attachment
                        parsed_reqs = self.parse_attachment(filepath, filename)
                        all_requirements.extend(parsed_reqs)
        
        print(f"\n  Total attachments processed: {total_attachments}")
        print(f"  Total requirements extracted: {len(all_requirements)}")
        
        # Create consolidated requirements.json
        print(f"\n[4/5] Creating consolidated requirements.json")
        
        # Build the consolidated structure
        requirements_data = {
            "project_key": self.project_key,
            "target_issue_id": target_issue_id,
            "created_at": datetime.now().isoformat(),
            "epic": None,
            "story": None,
            "related_issues": [],
            "acceptance_criteria": {},
            "attachments_parsed": total_attachments,
            "requirements": all_requirements,
            "consolidated_text": ""
        }
        
        # Extract key information from target issue
        if issue_type == "Epic":
            requirements_data["epic"] = {
                "key": target_issue_id,
                "summary": fields.get("summary", ""),
                "description": fields.get("description", "")
            }
        elif issue_type in ["Story", "Task"]:
            requirements_data["story"] = {
                "key": target_issue_id,
                "summary": fields.get("summary", ""),
                "description": fields.get("description", "")
            }
            
            if epic_key:
                epic_issue = self.get_issue(epic_key)
                if epic_issue:
                    requirements_data["epic"] = {
                        "key": epic_key,
                        "summary": epic_issue['fields'].get("summary", ""),
                        "description": epic_issue['fields'].get("description", "")
                    }
        
        # Add related issues
        for issue in all_issues:
            if issue['key'] != target_issue_id:
                requirements_data["related_issues"].append({
                    "key": issue['key'],
                    "summary": issue['fields'].get("summary", ""),
                    "type": issue['fields'].get("issuetype", {}).get("name", "")
                })
        
        # Extract acceptance criteria
        description = fields.get("description", "")
        if "Acceptance Criteria" in description or "AC:" in description:
            requirements_data["acceptance_criteria"]["main"] = description
        
        # Create consolidated text
        consolidated_parts = []
        if requirements_data["epic"]:
            consolidated_parts.append(f"Epic: {requirements_data['epic']['summary']}")
            if requirements_data["epic"]["description"]:
                consolidated_parts.append(str(requirements_data["epic"]["description"]))
        if requirements_data["story"]:
            consolidated_parts.append(f"Story: {requirements_data['story']['summary']}")
            if requirements_data["story"]["description"]:
                consolidated_parts.append(str(requirements_data["story"]["description"]))
        for req in all_requirements:
            if req["data"].get("content"):
                consolidated_parts.append(str(req["data"]["content"]))
        
        requirements_data["consolidated_text"] = "\n\n".join(consolidated_parts)
        
        # Save to file
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"requirements_{target_issue_id}.json")
        
        with open(output_path, 'w') as f:
            json.dump(requirements_data, f, indent=2)
        
        print(f"  ✓ Requirements saved to: {output_path}")
        
        # Also copy to inbox for Phase 1 processing
        inbox_path = os.path.join(INBOX_DIR, f"requirements_{target_issue_id}.json")
        with open(inbox_path, 'w') as f:
            json.dump(requirements_data, f, indent=2)
        
        print(f"  ✓ Copy saved to inbox: {inbox_path}")
        
        print(f"\n[5/5] Summary")
        print(f"  ✓ Project: {self.project_key}")
        print(f"  ✓ Target Issue: {target_issue_id}")
        print(f"  ✓ Issue Type: {issue_type}")
        print(f"  ✓ Related Issues: {len(all_issues) - 1}")
        print(f"  ✓ Attachments Parsed: {total_attachments}")
        print(f"  ✓ Requirements Extracted: {len(all_requirements)}")
        print(f"  ✓ Consolidated Text Length: {len(requirements_data['consolidated_text'])} chars")
        
        return requirements_data


def main():
    parser = argparse.ArgumentParser(description="Jira Sync Agent - Pull requirements from Jira")
    parser.add_argument("--issue", type=str, required=True, help="Target Jira issue key (e.g., SCRUM-86)")
    parser.add_argument("--project", type=str, default=JIRA_PROJECT_KEY, help="Jira project key")
    args = parser.parse_args()
    
    agent = JiraSyncAgent(project_key=args.project)
    agent.create_requirements_json(target_issue_id=args.issue)


if __name__ == "__main__":
    main()