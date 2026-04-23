#!/usr/bin/env python3
"""Stateless DOM MCP tools backed by captured JSON snapshots."""

import glob
import json
import os
from typing import Any, Dict, Iterable, List, Optional

DOCS_DIR = "docs"


def _response_payload(ok: bool, data: Any = None, error: str = "") -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error}


def _latest_dom_file(project_key: Optional[str] = None) -> Optional[str]:
    if project_key:
        candidates = glob.glob(os.path.join(DOCS_DIR, f"live_dom_elements_{project_key}_*.json"))
        if candidates:
            return max(candidates, key=os.path.getmtime)
    candidates = glob.glob(os.path.join(DOCS_DIR, "live_dom_elements_*.json"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def _load_dom_snapshot(project_key: Optional[str] = None) -> Dict[str, Any]:
    path = _latest_dom_file(project_key)
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["_snapshot_path"] = path
    return data


def _iter_candidate_elements(snapshot: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    keys = [
        "all_interactive_elements",
        "buttons",
        "inputs",
        "links",
        "dropdowns",
        "forms",
        "tables",
    ]
    seen = set()
    for key in keys:
        for element in snapshot.get(key, []) or []:
            marker = json.dumps(element, sort_keys=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            yield element


def get_dom_snapshot(project_key: Optional[str] = None) -> Dict[str, Any]:
    """Return the latest DOM snapshot metadata and data."""
    snapshot = _load_dom_snapshot(project_key)
    if not snapshot:
        return _response_payload(False, error="No DOM snapshot found")

    data = {
        "project_key": project_key,
        "snapshot_path": snapshot.get("_snapshot_path"),
        "url": snapshot.get("url") or snapshot.get("page_url"),
        "captured_at": snapshot.get("captured_at") or snapshot.get("timestamp"),
        "element_counts": {
            "interactive": len(snapshot.get("all_interactive_elements", []) or []),
            "buttons": len(snapshot.get("buttons", []) or []),
            "inputs": len(snapshot.get("inputs", []) or []),
            "links": len(snapshot.get("links", []) or []),
        },
        "snapshot": snapshot,
    }
    return _response_payload(True, data=data)


def find_element(
    query: str,
    project_key: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Find DOM elements by text/label/selector heuristics."""
    if not query:
        return _response_payload(False, error="query is required")

    snapshot = _load_dom_snapshot(project_key)
    if not snapshot:
        return _response_payload(False, error="No DOM snapshot found")

    query_lower = query.lower()
    matches: List[Dict[str, Any]] = []
    for element in _iter_candidate_elements(snapshot):
        search_fields = [
            element.get("text"),
            element.get("label"),
            element.get("name"),
            element.get("placeholder"),
            element.get("selector"),
            element.get("xpath"),
            element.get("role"),
            element.get("type"),
            element.get("aria_label"),
            element.get("data-testid"),
            element.get("data_testid"),
        ]
        haystack = " ".join(str(value) for value in search_fields if value).lower()
        if query_lower in haystack:
            matches.append(element)
        if len(matches) >= limit:
            break

    return _response_payload(
        True,
        data={
            "query": query,
            "snapshot_path": snapshot.get("_snapshot_path"),
            "matches": matches,
            "match_count": len(matches),
        },
    )

