#!/usr/bin/env python3
"""Stateless Jira MCP tools."""

import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")


def _auth() -> tuple[str, str]:
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _response_payload(ok: bool, data: Any = None, error: str = "") -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error}


def get_issue(issue_key: str, fields: Optional[str] = None) -> Dict[str, Any]:
    """Fetch a Jira issue by key."""
    if not issue_key:
        return _response_payload(False, error="issue_key is required")
    if not JIRA_BASE_URL:
        return _response_payload(False, error="JIRA_BASE_URL is not configured")

    params = {"fields": fields} if fields else None
    try:
        response = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}",
            headers=_headers(),
            auth=_auth(),
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return _response_payload(
            True,
            data={
                "key": payload.get("key"),
                "summary": payload.get("fields", {}).get("summary"),
                "status": (payload.get("fields", {}).get("status") or {}).get("name"),
                "issue": payload,
            },
        )
    except Exception as exc:
        return _response_payload(False, error=str(exc))


def add_comment(issue_key: str, body: str) -> Dict[str, Any]:
    """Add a comment to a Jira issue."""
    if not issue_key:
        return _response_payload(False, error="issue_key is required")
    if not body:
        return _response_payload(False, error="body is required")
    if not JIRA_BASE_URL:
        return _response_payload(False, error="JIRA_BASE_URL is not configured")

    payload = {"body": body}
    try:
        response = requests.post(
            f"{JIRA_BASE_URL}/rest/api/2/issue/{issue_key}/comment",
            json=payload,
            headers=_headers(),
            auth=_auth(),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return _response_payload(
            True,
            data={"issue_key": issue_key, "comment_id": data.get("id"), "comment": data},
        )
    except Exception as exc:
        return _response_payload(False, error=str(exc))


def create_bug(
    summary: str,
    description: str,
    project_key: Optional[str] = None,
    issue_type: str = "Bug",
    parent_issue_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Jira bug, optionally linking it to a parent issue."""
    if not summary:
        return _response_payload(False, error="summary is required")
    if not JIRA_BASE_URL:
        return _response_payload(False, error="JIRA_BASE_URL is not configured")

    resolved_project = project_key or JIRA_PROJECT_KEY
    if not resolved_project:
        return _response_payload(False, error="project_key is required")

    payload: Dict[str, Any] = {
        "fields": {
            "project": {"key": resolved_project},
            "summary": summary,
            "description": description or "",
            "issuetype": {"name": issue_type},
        }
    }

    try:
        response = requests.post(
            f"{JIRA_BASE_URL}/rest/api/2/issue",
            json=payload,
            headers=_headers(),
            auth=_auth(),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        bug_key = data.get("key")

        link_result = None
        if bug_key and parent_issue_key:
            link_payload = {
                "type": {"name": "Blocks"},
                "inwardIssue": {"key": bug_key},
                "outwardIssue": {"key": parent_issue_key},
            }
            link_response = requests.post(
                f"{JIRA_BASE_URL}/rest/api/2/issueLink",
                json=link_payload,
                headers=_headers(),
                auth=_auth(),
                timeout=30,
            )
            link_result = {
                "ok": link_response.ok,
                "status_code": link_response.status_code,
            }

        return _response_payload(
            True,
            data={
                "key": bug_key,
                "url": f"{JIRA_BASE_URL}/browse/{bug_key}" if bug_key else "",
                "parent_issue_key": parent_issue_key,
                "link_result": link_result,
            },
        )
    except Exception as exc:
        return _response_payload(False, error=str(exc))

