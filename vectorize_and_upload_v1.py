#!/usr/bin/env python3
"""
Step 1b: Vectorization Script — v2
====================================
Generates vectors from various file formats (.pdf, .xlsx, .json, .csv)
using the configured embedding model and uploads them to Qdrant with
project-specific isolation.

Pipeline context
────────────────
  Phase 0  jira_sync_agent.py      →  docs/inbox/*.json
  Phase 1  dom_capture.py          →  docs/live_dom_elements_*.json
  Phase 2  vectorize_and_upload.py →  Qdrant: {COLLECTION_NAME}_requirements
                                       Qdrant: {COLLECTION_NAME}_ui_memory
                                       docs/requirements/{PROJECT_KEY}_PRD.md
  Phase 3  quality_alignment.py   →  tests/features/{PROJECT_KEY}.feature
                                       docs/quality_alignment_report_{PROJECT_KEY}.json

Key conventions (v2)
─────────────────────
  PROJECT_KEY      "SCRUM-70"  — always uses the original hyphenated form.
                                 Used as the project_key payload value in
                                 every Qdrant point so filtering is consistent
                                 across all pipeline phases.

  Collection names are the ONLY place where the key is sanitised
  (hyphens → underscores) because Qdrant forbids hyphens in names:
      SCRUM-70  →  SCRUM_70_requirements
                   SCRUM_70_ui_memory

  This means quality_alignment.py must filter with the RAW key "SCRUM-70",
  NOT the sanitised form.  Both scripts now call collection_name_for() /
  sanitize_collection_name() for the collection name and keep PROJECT_KEY
  raw for every payload / filter value.

Fix log
-------
v2:
  * project_key stored as raw "SCRUM-70" (not sanitised) everywhere in
    payloads.  Fixes the filter mismatch that caused 0 results in Phase 3.

  * sanitize_collection_name() is now the single place that converts the
    project key to underscore form; called only when building collection
    names.

  * requirement type tagging: every point now carries a `content_type`
    field ("ui_spec" | "process" | "acceptance_criteria" | "test_data" |
    "general") so Phase 3 can filter by type when building DOM queries.
    Previously all records were typed identically making process-level
    subtask text (e.g. "Activities: Execute full test suite…") indistinguishable
    from UI field specs ("Module: Text Box | Field: Email …").

  * DOM points: project_key stored raw; url field always present.
"""

import csv
import glob
import hashlib
import json
import os
import re
import shutil
import argparse
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from dotenv import load_dotenv

from llm_gateway import get_llm_gateway

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
OLLAMA_HOST     = os.getenv("OLLAMA_HOST",     "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "mxbai-embed-large:latest")
QDRANT_URL      = os.getenv("QDRANT_URL",      "http://localhost:6333")
BASE_URL        = os.getenv("BASE_URL",        "").rstrip("/")
VECTOR_SIZE     = 1024

# Overridden by --project at startup.
# ALWAYS kept in its original hyphenated form, e.g. "SCRUM-70".
PROJECT_KEY: str = "SCRUM-103"

REQUIREMENTS_COLLECTION: Optional[str] = None
UI_MEMORY_COLLECTION:    Optional[str] = None

INBOX_DIR = "docs/inbox"
DOCS_DIR  = "docs"


# ══════════════════════════════════════════════════════════════════════════════
# Naming helpers
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_collection_name(name: str) -> str:
    """
    Convert an arbitrary string into a valid Qdrant collection name.

    Qdrant forbids hyphens and most special characters in collection names.
    Rule: keep only [a-zA-Z0-9_].  Everything else → '_'.

    This is called ONLY when building collection names, never for
    project_key payload values.

    Examples
    --------
    collection_name_for("SCRUM-70", "requirements")  →  "SCRUM_70_requirements"
    collection_name_for("my-project--v2", "ui_memory") → "my_project__v2_ui_memory"
    """
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return sanitized.strip('_') or 'collection'


def collection_name_for(project_key: str, suffix: str) -> str:
    """Build the Qdrant collection name for a given project key and suffix."""
    return sanitize_collection_name(f"{project_key}_{suffix}")


# ══════════════════════════════════════════════════════════════════════════════
# Content-type classification
# ══════════════════════════════════════════════════════════════════════════════

def classify_content_type(text: str, section: str = "") -> str:
    """
    Classify requirement text into a content_type used by Phase 3 to
    decide which records are useful for DOM cross-referencing.

    content_type values
    ───────────────────
    "ui_spec"               — pipe-delimited field/module specs from Excel;
                              best for DOM label matching.
    "acceptance_criteria"   — AC blocks from Jira descriptions; useful for
                              Gherkin generation and drift analysis.
    "test_data"             — test data rows, input examples.
    "process"               — execution/review process instructions;
                              NOT useful for DOM matching.
    "general"               — everything else.
    """
    t   = text.lower()
    sec = section.lower()

    # Pipe-delimited Excel rows: "Module: ... | Field: ... | ..."
    if "|" in text and re.search(r'\bmodule\b|\bfield\b|\binput type\b', t):
        return "ui_spec"

    # Acceptance criteria blocks
    if re.search(r'\bac\d+\b|acceptance criteria|expected outcome', t):
        return "acceptance_criteria"

    # Test data rows
    if re.search(r'\btest data\b|\binvalid email\b|\bnon.numeric\b|\bempty.null\b', t):
        return "test_data"

    # Process/execution instructions — generated by QA subtasks like SCRUM-118
    if re.search(
        r'\bactivities\b|\bexecute\b.*\btest suite\b|\bvalidate results\b'
        r'|\bpeer review\b|\bcheck execution logs\b|\bexpected outcome\b.*\btest cases\b',
        t
    ):
        return "process"

    # Section-name hints
    if "jira_main" in sec and re.search(r'\bactivities\b|\bexecute\b|\breview\b', t):
        return "process"

    return "general"


def classify_qa_type(text: str) -> str:
    """Classify requirement text into a QA action type."""
    t = text.lower()
    if any(k in t for k in ["must", "should not", "should be", "only", "allowed",
                              "invalid", "valid", "cannot", "restriction", "format"]):
        return "validation"
    if any(k in t for k in ["click", "enter", "input", "select", "submit",
                              "navigate", "open", "choose", "fill"]):
        return "action"
    if any(k in t for k in ["should display", "should show", "should see",
                              "verify", "ensure", "expect", "error message",
                              "success", "visible", "disabled"]):
        return "assertion"
    return "requirement"


# ══════════════════════════════════════════════════════════════════════════════
# ADF / text helpers
# ══════════════════════════════════════════════════════════════════════════════

def extract_plain_text(value) -> str:
    """Recursively convert ADF dict or nested structure to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "text":
            return value.get("text", "")
        parts = [extract_plain_text(child) for child in value.get("content", [])]
        return " ".join(p for p in parts if p.strip())
    if isinstance(value, list):
        return " ".join(extract_plain_text(v) for v in value if v)
    return str(value)


def extract_urls_from_text(text: str) -> List[str]:
    return re.findall(r'https?://[^\s\'"<>]+', text)


# ══════════════════════════════════════════════════════════════════════════════
# Embedding helpers
# ══════════════════════════════════════════════════════════════════════════════

def generate_embedding(text: str) -> List[float]:
    gateway = get_llm_gateway()
    model_override = gateway.resolve_model_for_agent(
        "vectorize_and_upload_v1",
        purpose="embedding",
        fallback_model=None,
    )
    return gateway.generate_embedding(text, model_override=model_override)


def normalize_business_intent(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r'\s+', ' ', text.strip())
    sl = cleaned.lower()
    if any(k in sl for k in ("should", "must", "shall")):
        intent_type = "requirement"
    elif any(k in sl for k in ("can", "able to", "capability")):
        intent_type = "capability"
    elif any(k in sl for k in ("test", "verify", "validate")):
        intent_type = "test_case"
    else:
        intent_type = "requirement"
    return {
        "type":         intent_type,
        "description":  cleaned,
        "cleaned_text": cleaned,
        "length":       len(cleaned),
        "keywords":     extract_keywords(cleaned),
    }


def extract_keywords(text: str) -> List[str]:
    common = {
        'the','and','or','but','for','nor','yet','so','this','that',
        'these','those','with','from','are','was','were','been','have',
        'has','had','will','would','could','should','shall','must',
    }
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    return list(dict.fromkeys(w for w in words if w not in common))[:10]


def generate_id(text: str, filename: str, page_or_row: int) -> int:
    combined = f"{filename}_{page_or_row}_{text}"
    return int(hashlib.md5(combined.encode()).hexdigest()[:8], 16)


def smart_chunk_text(text: str) -> List[str]:
    text = text.replace("\n", " ").strip()
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, buffer = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if any(k in s.lower() for k in [
            "should", "must", "error", "invalid", "valid",
            "click", "enter", "submit", "display", "allow", "not",
        ]):
            if buffer:
                chunks.append(buffer.strip())
                buffer = ""
            chunks.append(s)
        else:
            buffer += " " + s
    if buffer.strip():
        chunks.append(buffer.strip())
    return [c.strip() for c in chunks if len(c.strip()) > 10]


# ══════════════════════════════════════════════════════════════════════════════
# Requirements processing
# ══════════════════════════════════════════════════════════════════════════════

def process_raw_requirements(filepath: str) -> List[Dict]:
    """Process Jira + structured requirement JSON into vector-ready chunks."""
    with open(filepath, 'r') as f:
        data = json.load(f)

    filename = os.path.basename(filepath)
    points   = []

    def safe_text(x):
        return extract_plain_text(x).strip() if x else ""

    items = data if isinstance(data, list) else [data]

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        fields   = item.get("fields", item)
        sections = []

        summary     = safe_text(fields.get("summary"))
        description = safe_text(fields.get("description"))
        combined    = "\n".join(filter(None, [summary, description])).strip()
        if combined:
            sections.append(("jira_main", combined))

        if "body" in item:
            ct = safe_text(item.get("body"))
            if ct:
                sections.append(("comment", ct))

        comments = item.get("comments")
        if isinstance(comments, list):
            ct = " ".join(safe_text(c.get("body")) for c in comments if isinstance(c, dict))
            if ct.strip():
                sections.append(("comments_bulk", ct))

        if "story" in item:
            story = item.get("story", {})
            st = "\n".join(filter(None, [
                f"Story {story.get('key','')}: {story.get('summary','')}",
                safe_text(story.get("description"))
            ]))
            if st:
                sections.append(("story", st))

        # Epic — two cases:
        # (a) inbox item has epic as a string key → just record the key reference
        # (b) the file IS the epic JSON (full Jira issue with fields.issuetype=Epic)
        # (c) item has an "epic" dict with summary/description (custom enriched format)
        epic_val = item.get("epic")
        issue_type = (fields.get("issuetype") or {})
        issue_type_name = issue_type.get("name", "").lower() if isinstance(issue_type, dict) else ""
        is_epic_file = issue_type_name == "epic"

        if is_epic_file:
            # This file is itself the epic — extract all its fields
            # Scan all fields generically for epic name and goal (IDs vary per Jira instance)
            epic_name = safe_text(fields.get("summary", ""))
            epic_goal = ""
            for field_name, val in fields.items():
                if not val:
                    continue
                fl = field_name.lower()
                if "epic_name" in fl or ("epic" in fl and "name" in fl):
                    candidate = safe_text(val)
                    if candidate:
                        epic_name = candidate
                if "goal" in fl or "objective" in fl or "vision" in fl:
                    candidate = safe_text(val)
                    if candidate and len(candidate) > 10:
                        epic_goal = candidate

            epic_desc   = safe_text(fields.get("description", ""))
            epic_status = (fields.get("status") or {}).get("name", "") if isinstance(fields.get("status"), dict) else ""
            et = "\n".join(filter(None, [
                f"Epic {item.get('key', fields.get('key', ''))}: {epic_name}",
                epic_desc,
                f"Status: {epic_status}" if epic_status else "",
                f"Goal: {epic_goal}" if epic_goal else "",
            ])).strip()
            if et:
                sections.append(("epic", et))

        elif isinstance(epic_val, dict):
            # Enriched dict format — may come from inbox (has epic_name, epic_goal)
            # or from a custom format with key/summary/description
            et = "\n".join(filter(None, [
                f"Epic {epic_val.get('key','')}: {safe_text(epic_val.get('epic_name') or epic_val.get('summary'))}",
                safe_text(epic_val.get("description")),
                f"Status: {epic_val.get('status')}" if epic_val.get("status") else "",
                f"Goal: {safe_text(epic_val.get('epic_goal'))}" if epic_val.get("epic_goal") else "",
            ])).strip()
            if et:
                sections.append(("epic", et))

        elif isinstance(epic_val, str) and epic_val.strip():
            # Just the epic key string — record it as context
            sections.append(("epic", f"Epic: {epic_val.strip()}"))

        # Subtasks embedded in the story JSON (fields.subtasks list)
        raw_subtasks = fields.get("subtasks", [])
        if isinstance(raw_subtasks, list):
            for st in raw_subtasks:
                if not isinstance(st, dict):
                    continue
                st_fields = st.get("fields", st)
                st_text = "\n".join(filter(None, [
                    f"Subtask {st.get('key', st_fields.get('key', ''))}: {safe_text(st_fields.get('summary'))}",
                    safe_text(st_fields.get("description")),
                    safe_text(st_fields.get("status", {}).get("name") if isinstance(st_fields.get("status"), dict) else st_fields.get("status")),
                ])).strip()
                if st_text:
                    sections.append(("subtask", st_text))

        # Subtasks from enriched inbox format (list of plain dicts with key/summary/description)
        inbox_subtasks = item.get("subtasks", [])
        if isinstance(inbox_subtasks, list) and inbox_subtasks and isinstance(inbox_subtasks[0], dict) and "summary" in inbox_subtasks[0]:
            for st in inbox_subtasks:
                st_text = "\n".join(filter(None, [
                    f"Subtask {st.get('key', '')}: {safe_text(st.get('summary'))}",
                    safe_text(st.get("description")),
                    st.get("status", ""),
                ])).strip()
                if st_text:
                    sections.append(("subtask", st_text))

        # Comments from enriched inbox format (list of dicts with issue/author/body)
        inbox_comments = item.get("comments", [])
        if isinstance(inbox_comments, list):
            for c in inbox_comments:
                if not isinstance(c, dict):
                    continue
                body = safe_text(c.get("body", ""))
                if body:
                    issue_ref = c.get("issue", "")
                    author = c.get("author", "")
                    comment_text = f"[{issue_ref}] {author}: {body}".strip(" :")
                    sections.append(("comments_bulk", comment_text))

        # Comments embedded inside fields.comment (Jira native structure in subtask JSONs)
        fields_comment = fields.get("comment", {})
        if isinstance(fields_comment, dict):
            embedded_comments = fields_comment.get("comments", [])
            if embedded_comments:
                ct = "\n".join(
                    safe_text(c.get("body")) for c in embedded_comments
                    if isinstance(c, dict) and c.get("body")
                )
                if ct.strip():
                    sections.append(("comments_bulk", ct))

        if "acceptance_criteria" in item:
            ac = item.get("acceptance_criteria", {})
            if isinstance(ac, str):
                ac = {"main": ac}
            elif not isinstance(ac, dict):
                ac = {"main": str(ac)}
            ac_txt = "\n".join(filter(None, [
                "Acceptance Criteria:",
                f"Main: {safe_text(ac.get('main'))}",
                f"Expected: {safe_text(ac.get('expected_outcomes'))}",
                f"Out of Scope: {safe_text(ac.get('out_of_scope'))}",
            ]))
            if ac_txt:
                sections.append(("acceptance_criteria", ac_txt))

        if "consolidated_text" in item:
            ct = safe_text(item.get("consolidated_text"))
            if ct:
                sections.append(("consolidated", ct))

        for section_type, text in sections:
            if not text or len(text.strip()) < 5:
                continue
            chunks = smart_chunk_text(text)
            for chunk in chunks:
                if not chunk or len(chunk.strip()) < 10:
                    continue

                qa_type      = classify_qa_type(chunk)
                content_type = classify_content_type(chunk, section_type)

                enriched_text = (
                    f"Source: PRD\n"
                    f"Project: {PROJECT_KEY}\n"
                    f"Section: {section_type}\n"
                    f"Type: {qa_type}\n\n"
                    + chunk.strip()
                )

                referenced_urls = extract_urls_from_text(enriched_text)

                points.append({
                    "id":              generate_id(enriched_text, filename, idx),
                    "text":            enriched_text,
                    "business_intent": normalize_business_intent(chunk),
                    "source":          filename,
                    "section":         section_type,
                    "type":            qa_type,
                    "content_type":    content_type,
                    "requirement_id":  item.get("key") or fields.get("key", "UNKNOWN"),
                    "issuetype":       issue_type_name,   # "epic", "story", "sub-task", "task", etc.
                    "url":             referenced_urls[0] if referenced_urls else "",
                    "referenced_urls": referenced_urls,
                    "ancestry": {
                        "filename": filename,
                        "index":    idx,
                        "section":  section_type,
                    },
                })

    print(f"  Found {len(points)} text chunks")
    return points


def process_csv_file(filepath: str, source_name: str) -> List[Dict]:
    points   = []
    filename = os.path.basename(filepath)
    with open(filepath, 'r') as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows):
        text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
        content_type = classify_content_type(text, "csv_row")
        points.append({
            "id":              generate_id(text, filename, i + 1),
            "text":            text,
            "business_intent": normalize_business_intent(text),
            "source":          filename,
            "section":         "row",
            "type":            classify_qa_type(text),
            "content_type":    content_type,
            "requirement_id":  f"{source_name}_row_{i+1}",
            "ancestry": {
                "filename":    filename,
                "page_or_row": i + 1,
                "section":     "row",
            },
        })
    return points


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant upload (requirements)
# ══════════════════════════════════════════════════════════════════════════════

def upload_to_qdrant(points: List[Dict], collection_name: str) -> int:
    """
    Generate embeddings and upload requirements points to Qdrant.

    project_key stored as raw PROJECT_KEY (e.g. "SCRUM-70") — NOT sanitised.
    Collection name is already sanitised by the caller.
    """
    client      = QdrantClient(url=QDRANT_URL)
    collections = client.get_collections().collections

    if any(c.name == collection_name for c in collections):
        print(f"Collection '{collection_name}' exists — deleting...")
        try:
            client.delete_collection(collection_name=collection_name)
            print(f"  ✓ Deleted '{collection_name}'")
        except Exception as exc:
            print(f"  ⚠ Could not delete via API: {exc}")

    storage_path = f"./storage/collections/{collection_name}"
    if os.path.exists(storage_path):
        print(f"  ⚠ Removing leftover storage: {storage_path}")
        shutil.rmtree(storage_path, ignore_errors=True)

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"  ✓ Created collection '{collection_name}'")
    print(f"\nProcessing {len(points)} points…")

    uploaded = 0
    for i, pd_ in enumerate(points):
        try:
            text    = pd_.get("text", "") if isinstance(pd_, dict) else str(pd_)
            payload = pd_ if isinstance(pd_, dict) else {}

            if not text or not text.strip():
                print(f"    ⚠ Skipping empty text at index {i}")
                continue

            print(f"  [{i+1}/{len(points)}] {text[:60]}…")
            vec = generate_embedding(text)

            if not vec or len(vec) != VECTOR_SIZE:
                print(f"    ✗ Bad embedding ({len(vec) if vec else 0} dims) — skipping")
                continue

            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=payload.get("id", i),
                    vector=vec,
                    payload={
                        "source":          payload.get("source", "unknown"),
                        "text":            text,
                        "section":         payload.get("section", ""),
                        "type":            payload.get("type", "requirement"),
                        # ── NEW: content_type for Phase 3 DOM-query filtering ──
                        "content_type":    payload.get("content_type", "general"),
                        "requirement_id":  payload.get("requirement_id", ""),
                        # ── RAW project key — never sanitised ─────────────────
                        "project_key":     PROJECT_KEY,
                        "ancestry":        payload.get("ancestry", {}),
                        "business_intent": payload.get("business_intent", {}),
                        "url":             payload.get("url", ""),
                        "referenced_urls": payload.get("referenced_urls", []),
                        "metadata": {
                            "created_at": datetime.now().isoformat(),
                            "version":    "2.0",
                        },
                    },
                )],
            )
            uploaded += 1
            print(f"    ✓ Uploaded ID: {payload.get('id', i)}"
                  + (f"  content_type={payload.get('content_type')}" if payload.get("content_type") else ""))

        except Exception as exc:
            print(f"    ✗ Error: {exc}")

    return uploaded


# ══════════════════════════════════════════════════════════════════════════════
# DOM upload
# ══════════════════════════════════════════════════════════════════════════════

def _build_qa_summary_text(qa_summary: Dict[str, Any]) -> str:
    return (
        f"QA Summary:\n"
        f"Total Elements: {qa_summary.get('total_elements')}\n"
        f"Risky Elements: {qa_summary.get('risky_elements')}\n"
        f"Overlay Present: {qa_summary.get('overlay_present')}\n"
        f"Pass Rate: {qa_summary.get('pass_rate')}\n"
        f"Failed Elements: {qa_summary.get('failed_elements')}\n"
        f"Warning Elements: {qa_summary.get('warning_elements')}"
    ).strip()


def _build_embedding_text(
    kind: str,
    el: Dict[str, Any],
    fields: List[str],
    page_url: str = "",
) -> str:
    """
    Build an enriched, semantically rich embedding text for a DOM element.

    Structure
    ─────────
    Line 1  Element Type: <kind>
    Line 2  Identity: label: … | text: … | placeholder: … | name: … | ariaLabel: …
            (deduplicated — each unique value appears only once)
    Line 3  Structure: role: … | type: … | tagName: … | id: …
    Line 4  Page: <page_url>
    Line 5  QA: visible: … | obstructed: … | clickable_score: … | qa_status: … | selector: …
    """
    identity_fields = ["label", "text", "placeholder", "name", "ariaLabel", "title"]
    identity_parts: List[str] = []
    seen_vals: set = set()
    for field in identity_fields:
        val = (el.get(field) or "").strip()
        if val and val.lower() not in seen_vals:
            identity_parts.append(f"{field}: {val}")
            seen_vals.add(val.lower())

    structural_parts: List[str] = []
    for field in ["role", "type", "tagName", "id", "name"]:
        val = (el.get(field) or "").strip()
        if val:
            structural_parts.append(f"{field}: {val}")

    lines: List[str] = [f"Element Type: {kind}"]
    if identity_parts:
        lines.append("Identity: " + " | ".join(identity_parts))
    if structural_parts:
        lines.append("Structure: " + " | ".join(structural_parts))
    if page_url:
        lines.append(f"Page URL: {page_url}")

    distinguishers: List[str] = []
    for field in ["label", "text", "placeholder", "name", "role", "type"]:
        val = (el.get(field) or "").strip()
        if val:
            distinguishers.append(f"{field}={val}")
    if distinguishers:
        lines.append("Matching Signals: " + " | ".join(distinguishers))

    qa_parts: List[str] = []
    if el.get("visible") is not None:
        qa_parts.append(f"visible: {el['visible']}")
    if el.get("obstructed") is not None:
        qa_parts.append(f"obstructed: {el['obstructed']}")
    if el.get("clickable_score") is not None:
        qa_parts.append(f"clickable_score: {el['clickable_score']}")
    if el.get("qa_status"):
        qa_parts.append(f"qa_status: {el['qa_status']}")
    selector = el.get("selector") or el.get("id") or ""
    if selector:
        qa_parts.append(f"selector: {selector}")
    if qa_parts:
        lines.append("QA: " + " | ".join(qa_parts))

    return "\n".join(lines).strip()


def upload_dom_to_qdrant(dom_data: Dict, collection_name: str) -> int:
    """
    Upload DOM elements to the ui_memory Qdrant collection.

    project_key stored as raw PROJECT_KEY (e.g. "SCRUM-70").
    """
    client      = QdrantClient(url=QDRANT_URL)
    collections = client.get_collections().collections

    if any(c.name == collection_name for c in collections):
        print(f"Collection '{collection_name}' exists — deleting and recreating…")
        try:
            client.delete_collection(collection_name=collection_name)
            print(f"  ✓ Deleted '{collection_name}'")
        except Exception as exc:
            print(f"  ⚠ Could not delete: {exc}")

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"  ✓ Created DOM collection '{collection_name}'")

    page_url = (
        dom_data.get("url")
        or dom_data.get("page_url")
        or dom_data.get("base_url")
        or (BASE_URL + "/" if BASE_URL else "")
    )
    if not page_url:
        print("  ⚠ No URL found in DOM data and BASE_URL is unset — "
              "navigation URL resolution will fall back to BASE_URL at runtime")

    qa_analysis       = dom_data.get("qa_analysis", [])
    qa_summary        = dom_data.get("qa_summary", {})
    overlays_detected = dom_data.get("overlays_detected", [])

    qa_by_selector: Dict[str, Dict[str, Any]] = {}
    for qa_item in qa_analysis:
        sel = qa_item.get("selector") or qa_item.get("id") or ""
        if sel:
            qa_by_selector[sel] = qa_item

    if qa_analysis:
        print(f"  ✓ Loaded {len(qa_analysis)} QA annotations "
              f"({len(qa_by_selector)} unique selectors)")
    if qa_summary:
        print(f"  ✓ QA Summary: total={qa_summary.get('total_elements')} "
              f"risky={qa_summary.get('risky_elements')} "
              f"overlay={qa_summary.get('overlay_present')}")

    element_groups = [
        ("input",       dom_data.get("input_elements",    []),
         ["type", "placeholder", "label", "name", "id"]),
        ("button",      dom_data.get("button_elements",   []),
         ["text", "label", "id", "className"]),
        ("dropdown",    dom_data.get("dropdown_elements", []),
         ["name", "label", "options"]),
        ("link",        dom_data.get("link_elements",     []),
         ["text", "href"]),
        ("interactive", dom_data.get("all_interactive_elements", []),
         ["tagName", "text", "placeholder", "role", "ariaRole"]),
    ]
    points = []
    for kind, items, fields in element_groups:
        for i, el in enumerate(items):
            sel          = el.get("selector") or el.get("id") or ""
            qa_ann       = qa_by_selector.get(sel, {})
            merged_el    = {**el, **{k: v for k, v in qa_ann.items() if k not in el}}
            # Use the per-element page_url written by dom_capture during crawl,
            # falling back to the top-level page_url only if absent.
            el_page_url  = (
                el.get("page_url")
                or el.get("url")
                or page_url
            )
            text         = _build_embedding_text(kind, merged_el, fields, el_page_url)
            points.append({
                "id":       abs(hash(f"{kind}_{i}_{PROJECT_KEY}")) % (2**31),
                "text":     text,
                "details":  merged_el,
                "kind":     kind,
                "page_url": el_page_url,   # ← carry it forward
            })
    uploaded = 0
    for i, p in enumerate(points):
        try:
            vec = generate_embedding(p["text"])
            if not vec:
                continue
            el = p["details"]
            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=p["id"],
                    vector=vec,
                    payload={
                        "source":            "dom_capture",
                        "text":              p["text"],
                        # ── RAW project key — never sanitised ─────────────────
                        "project_key":       PROJECT_KEY,
                        "url":               p["page_url"],
                        "details":           el,
                        "type":              "dom_element",
                        "selector":          el.get("selector") or el.get("id") or "",
                        "qa_status":         el.get("qa_status"),
                        "visible":           el.get("visible"),
                        "obstructed":        el.get("obstructed"),
                        "clickable_score":   el.get("clickable_score"),
                        "overlays_detected": overlays_detected,
                        "metadata": {
                            "created_at": datetime.now().isoformat(),
                        },
                    },
                )],
            )
            uploaded += 1
            if (i + 1) % 10 == 0:
                print(f"    [{i+1}/{len(points)}] DOM elements uploaded…")
        except Exception as exc:
            print(f"    ⚠ {exc}")

    # QA summary document point
    qa_summary_uploaded = 0
    if qa_summary:
        try:
            summary_text = _build_qa_summary_text(qa_summary)
            vec          = generate_embedding(summary_text)
            if vec and len(vec) == VECTOR_SIZE:
                summary_id = abs(hash(f"qa_summary_{PROJECT_KEY}_{page_url}")) % (2**31)
                client.upsert(
                    collection_name=collection_name,
                    points=[PointStruct(
                        id=summary_id,
                        vector=vec,
                        payload={
                            "source":            "dom_capture",
                            "type":              "qa_summary",
                            "text":              summary_text,
                            "project_key":       PROJECT_KEY,
                            "url":               page_url,
                            "overlays_detected": overlays_detected,
                            "qa_summary":        qa_summary,
                            "metadata": {"created_at": datetime.now().isoformat()},
                        },
                    )],
                )
                qa_summary_uploaded = 1
                print(f"  ✓ QA Summary document uploaded (id={summary_id})")
        except Exception as exc:
            print(f"  ⚠ QA Summary upload error: {exc}")

    total = uploaded + qa_summary_uploaded
    print(f"  ✓ Uploaded {uploaded}/{len(points)} DOM elements → '{collection_name}'")
    if qa_summary_uploaded:
        print(f"  ✓ QA Summary document uploaded → '{collection_name}'")
    print(f"  ✓ Total: {total} points | project_key='{PROJECT_KEY}' | url='{page_url or '(empty)'}'")
    return total


# ══════════════════════════════════════════════════════════════════════════════
# Verification
# ══════════════════════════════════════════════════════════════════════════════

def verify_upload(collection_name: str) -> Optional[int]:
    client = QdrantClient(url=QDRANT_URL)
    try:
        info = client.get_collection(collection_name)
        print(f"\n{'='*50}")
        print(f"Collection  : {collection_name}")
        print(f"Points      : {info.points_count}")
        print(f"Status      : {info.status}")
        print(f"project_key : {PROJECT_KEY}  (raw, as stored in payloads)")
        print(f"{'='*50}")

        # Sample filter uses raw PROJECT_KEY — must match stored payloads
        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            filtered, _ = client.scroll(
                collection_name=collection_name,
                scroll_filter=Filter(
                    must=[FieldCondition(key="project_key", match=MatchValue(value=PROJECT_KEY))]
                ),
                limit=5,
                with_payload=True,
            )
        except Exception:
            # Fallback for older qdrant-client versions
            filtered, _ = client.scroll(
                collection_name=collection_name,
                scroll_filter={
                    "must": [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
                },
                limit=5,
            )

        print(f"Sample points for '{PROJECT_KEY}': {len(filtered)}")
        if filtered:
            sample = filtered[0].payload
            print(f"  url field present : {'url' in sample}")
            print(f"  url value         : {sample.get('url', '(none)')}")
            print(f"  content_type      : {sample.get('content_type', '(none)')}")
        return info.points_count
    except Exception as exc:
        print(f"Error verifying upload: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# File processors
# ══════════════════════════════════════════════════════════════════════════════

class FileProcessor(ABC):
    @abstractmethod
    def process(self, filepath: str) -> List[Dict[str, Any]]:
        pass


class JSONProcessor(FileProcessor):
    def process(self, filepath: str) -> List[Dict[str, Any]]:
        return process_raw_requirements(filepath)


class CSVProcessor(FileProcessor):
    def process(self, filepath: str) -> List[Dict[str, Any]]:
        return process_csv_file(filepath, os.path.basename(filepath))


class PDFProcessor(FileProcessor):
    def process(self, filepath: str) -> List[Dict[str, Any]]:
        try:
            import PyPDF2
        except ImportError:
            print("Warning: PyPDF2 not installed. Skipping PDF.")
            return []
        points   = []
        filename = os.path.basename(filepath)
        with open(filepath, 'rb') as fh:
            reader = PyPDF2.PdfReader(fh)
            for pg, page in enumerate(reader.pages):
                text = page.extract_text()
                if text and text.strip():
                    content_type = classify_content_type(text, "page")
                    points.append({
                        "id":              generate_id(text, filename, pg + 1),
                        "text":            text,
                        "business_intent": normalize_business_intent(text),
                        "source":          filename,
                        "section":         "page",
                        "type":            classify_qa_type(text),
                        "content_type":    content_type,
                        "requirement_id":  f"{filename}_page_{pg+1}",
                        "ancestry": {"filename": filename, "page_or_row": pg + 1, "section": "page"},
                    })
        return points


class ExcelProcessor(FileProcessor):
    def process(self, filepath: str) -> List[Dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError:
            print("Warning: pandas not installed. Skipping Excel.")
            return []
        points   = []
        filename = os.path.basename(filepath)
        xf       = pd.ExcelFile(filepath)
        for sheet in xf.sheet_names:
            df = pd.read_excel(filepath, sheet_name=sheet)
            for idx, row in df.iterrows():
                text = " | ".join(
                    f"{col}: {str(val)}"
                    for col, val in row.items()
                    if pd.notna(val)
                )
                if text.strip():
                    content_type = classify_content_type(text, f"sheet_{sheet}_row")
                    points.append({
                        "id":              generate_id(text, filename, idx + 1),
                        "text":            text,
                        "business_intent": normalize_business_intent(text),
                        "source":          filename,
                        "section":         f"sheet_{sheet}_row",
                        "type":            classify_qa_type(text),
                        "content_type":    content_type,
                        "requirement_id":  f"{filename}_{sheet}_row_{idx+1}",
                        "ancestry": {
                            "filename":    filename,
                            "page_or_row": idx + 1,
                            "section":     f"sheet_{sheet}_row",
                        },
                    })
        return points


class FileFactory:
    def __init__(self):
        self.processors = {
            '.json': JSONProcessor(),
            '.csv':  CSVProcessor(),
            '.pdf':  PDFProcessor(),
            '.xlsx': ExcelProcessor(),
            '.xls':  ExcelProcessor(),
        }

    def get_processor(self, filepath: str) -> Optional[FileProcessor]:
        _, ext = os.path.splitext(filepath.lower())
        return self.processors.get(ext)

    def process_file(self, filepath: str) -> List[Dict[str, Any]]:
        if not os.path.exists(filepath):
            print(f"Warning: File not found: {filepath}")
            return []
        proc = self.get_processor(filepath)
        if proc:
            print(f"Processing {filepath} with {proc.__class__.__name__}…")
            return proc.process(filepath)
        print(f"Warning: No processor for: {filepath}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Inbox scanner
# ══════════════════════════════════════════════════════════════════════════════

def scan_inbox() -> List[str]:
    supported = {'.pdf', '.xlsx', '.csv', '.json'}
    seen:  set  = set()
    files: list = []

    jira_sync_root = os.path.join(DOCS_DIR, "jira_sync")
    if os.path.isdir(jira_sync_root):
        for dirpath, _dirs, fnames in os.walk(jira_sync_root):
            for fn in fnames:
                if os.path.splitext(fn)[1].lower() in supported:
                    fp = os.path.realpath(os.path.join(dirpath, fn))
                    if fp not in seen:
                        seen.add(fp)
                        files.append(fp)
        if files:
            print(f"  ✓ jira_sync tree: found {len(files)} file(s) under {jira_sync_root}")

    inbox_count = 0
    if os.path.isdir(INBOX_DIR):
        for fn in os.listdir(INBOX_DIR):
            fp = os.path.realpath(os.path.join(INBOX_DIR, fn))
            if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in supported:
                if fp not in seen:
                    seen.add(fp)
                    files.append(fp)
                    inbox_count += 1
    if inbox_count:
        print(f"  ✓ inbox: found {inbox_count} additional file(s) under {INBOX_DIR}")

    return files


# ══════════════════════════════════════════════════════════════════════════════
# PRD generation
# ══════════════════════════════════════════════════════════════════════════════

def _build_prd_input_block(all_points: List[Dict]) -> str:
    """
    Build a hierarchy-aware input block for the PRD LLM prompt.
    Groups chunks by Jira issue (requirement_id) then by section within that issue,
    so the LLM knows exactly which comment/attachment belongs to which issue.
    """
    # Separate attachment/supplementary points — PDF uses "page", Excel uses "sheet_*_row"
    def _is_attachment_point(p: dict) -> bool:
        sec = p.get("section", "")
        return sec == "page" or sec.startswith("sheet_")

    attachment_points = [p for p in all_points if isinstance(p, dict) and _is_attachment_point(p)]
    jira_points = [p for p in all_points if isinstance(p, dict) and not _is_attachment_point(p)]

    # Section display order within each issue
    SECTION_LABELS = {
        "epic":                "### Epic Details",
        "jira_main":           "### Summary & Description",
        "story":               "### Story Details",
        "subtask":             "### Subtasks",
        "acceptance_criteria": "### Acceptance Criteria",
        "comments_bulk":       "### Comments",
        "comment":             "### Comments",
        "consolidated":        "### Consolidated Notes",
        "general":             "### Additional Info",
    }
    SECTION_ORDER = list(SECTION_LABELS.keys())

    # Group jira points by requirement_id (Jira issue key)
    from collections import defaultdict, OrderedDict
    by_issue: dict = defaultdict(lambda: defaultdict(list))
    issue_order: list = []
    seen_issues: set = set()

    for p in jira_points:
        if not isinstance(p, dict):
            continue
        text = p.get("text", "").strip()
        if not text or len(text) < 10:
            continue
        rid = p.get("requirement_id", "UNKNOWN")
        sec = p.get("section", "general")
        if rid not in seen_issues:
            seen_issues.add(rid)
            issue_order.append(rid)
        by_issue[rid][sec].append(text)

    blocks: List[str] = []

    # Classify each issue as EPIC / STORY / SUBTASK using stored issuetype metadata
    def issue_kind(rid: str, sec_map: dict) -> str:
        # Collect all issuetype values stored across the points for this issue
        issue_types = set()
        for p in jira_points:
            if p.get("requirement_id") == rid:
                it = p.get("issuetype", "").lower()
                if it:
                    issue_types.add(it)

        if "epic" in issue_types:
            return "EPIC"
        # Jira uses "sub-task" or "subtask" depending on the instance
        if issue_types & {"sub-task", "subtask"}:
            return "SUBTASK"
        # If no issuetype stored (e.g. attachment-derived points), fall back to section clues
        if not issue_types:
            if "epic" in sec_map and sec_map["epic"]:
                return "EPIC"
            if "subtask" not in sec_map and "acceptance_criteria" not in sec_map:
                return "SUBTASK"
        return "STORY"

    def issue_heading(rid: str, kind: str) -> str:
        return f"## [{kind}] {rid}"

    for rid in issue_order:
        sec_map = by_issue[rid]
        kind = issue_kind(rid, sec_map)
        blocks.append(issue_heading(rid, kind))

        # Emit sections in defined order
        seen_sec_labels: set = set()
        for sec_key in SECTION_ORDER:
            texts = list(dict.fromkeys(sec_map.get(sec_key, [])))
            if not texts:
                continue
            label = SECTION_LABELS[sec_key]
            if label not in seen_sec_labels:
                blocks.append(label)
                seen_sec_labels.add(label)
            blocks.extend(texts)

        # Any unlisted sections
        for sec_key, texts in sec_map.items():
            if sec_key in SECTION_LABELS:
                continue
            unique = list(dict.fromkeys(texts))
            if unique:
                blocks.append(f"### {sec_key.replace('_', ' ').title()}")
                blocks.extend(unique)

    # Attachments section at the end — grouped by source file
    if attachment_points:
        blocks.append("## ATTACHMENTS & SUPPLEMENTARY DATA")
        att_by_source: dict = defaultdict(list)
        for p in attachment_points:
            src = p.get("source", "unknown")
            text = p.get("text", "").strip()
            if text and len(text) > 10:
                att_by_source[src].append(text)
        for src, texts in att_by_source.items():
            blocks.append(f"### {src}")
            blocks.extend(list(dict.fromkeys(texts)))

    return "\n\n".join(blocks)


def generate_prd(all_points: List[Dict], project_key: str) -> str:
    print("\n" + "=" * 60)
    print("Generating PRD using LLM…")
    print("=" * 60)

    req_dir = os.path.join(DOCS_DIR, "requirements")
    os.makedirs(req_dir, exist_ok=True)

    gateway = get_llm_gateway()

    # Quick validity check — bail early if nothing to work with
    valid_points = [p for p in all_points if isinstance(p, dict) and len(p.get("text", "").strip()) > 20]
    if not valid_points:
        print("⚠ No valid requirement text found. Skipping PRD generation.")
        return ""

    app_url = BASE_URL.rstrip("/") if BASE_URL else "(BASE_URL not set)"

    requirements_block = _build_prd_input_block(all_points)
    print(f"  PRD input: {len(requirements_block)} chars across {len(valid_points)} chunks (no truncation)")

    system_prompt = """You are a senior QA architect and product analyst.
Your job is to produce a complete, faithful PRD that will be the single source of truth for Gherkin test generation.

The input is structured by Jira hierarchy:
  [EPIC]    — the parent epic: its description, goals, scope
  [STORY]   — the user story: summary, full description, acceptance criteria, comments, attachments
  [SUBTASK] — each subtask: what it implements, its own comments and attachments if any

STRICT RULES:
- DO NOT invent, infer, or assume anything not present in the input
- Preserve ALL acceptance criteria exactly — numbering, conditions, test data values, edge cases
- Strip only: metadata prefix lines (Source:/Section:/Type:/Project:), raw Jira noise, duplicate chunks
- Every comment that contains a decision, clarification, or constraint MUST appear under the relevant issue
- Every attachment (Excel/PDF) row or page that contains test data, validation rules, or field specs MUST be preserved verbatim under the relevant issue
- Subtask descriptions are implementation steps — list them faithfully under their subtask heading
- The output will be fed directly into a Gherkin generator — completeness and precision are critical

OUTPUT: Clean markdown only. No preamble, no commentary, no invented content."""

    prompt = f"""Project: {project_key}
Application URL: {app_url}

INPUT (structured by Jira issue):
{requirements_block}

---
Produce a complete PRD with the following structure.
Every section must reference ONLY what is in the input above.

# PRD — {project_key}

## 1. Epic Overview
*(Epic key, name, description, goals, scope, status — from [EPIC] input)*

## 2. User Story
*(Story key, summary, full description)*

## 3. Acceptance Criteria
*(All AC items verbatim — preserve numbering, conditions, test data)*

## 4. Functional Requirements
*(Derived from story description + subtask summaries — what the system must do)*

## 5. Subtask Breakdown
*(One sub-section per subtask: key, summary, description, status, comments if any)*

## 6. Validation Rules & Test Data
*(All field-level rules, allowed/disallowed values, formats, lengths — from AC, attachments, and description)*

## 7. Negative & Edge Case Scenarios
*(Invalid inputs, boundary conditions, error messages, out-of-scope items)*

## 8. UI Behaviour & Interaction Rules
*(Field behaviour, button states, navigation, display rules)*

## 9. Comments & Decisions
*(Any comment from Epic, Story, or Subtask that contains a clarification, constraint, or scope decision — attributed to issue key)*

## 10. Attachment Data
*(All rows/pages from Excel/PDF attachments that contain specs, test data, or validation matrices — attributed to source file)*

## 11. Non-Functional Constraints
*(Performance, security, browser support, accessibility — only if mentioned in input)*

IMPORTANT: Do not skip sections. Do not summarise away detail. Return ONLY the markdown PRD.
"""

    try:
        model_override = gateway.resolve_model_for_agent(
            "vectorize_and_upload_v1",
            purpose="chat",
            fallback_model=os.getenv("CHAT_MODEL", "llama3:8b"),
        )
        prd_content = gateway.chat(
            prompt=prompt,
            system_prompt=system_prompt,
            model_override=model_override,
            temperature=0.2,
        )
    except Exception as e:
        print(f"✗ LLM PRD generation failed: {e}")
        prd_content = ""

    if not prd_content or len(prd_content.strip()) < 50:
        print("⚠ Falling back to raw PRD")
        prd_content = (
            f"# Product Requirements Document (PRD)\n"
            f"## Project: {project_key}\n\n"
            f"### Application URL\n{app_url}\n\n"
            f"### Raw Requirements\n"
            + requirements_block
        )

    prd_path = os.path.join(req_dir, f"{project_key}_PRD.md")
    with open(prd_path, 'w') as f:
        f.write(prd_content)

    print(f"✓ PRD saved to: {prd_path}")
    return prd_path


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global PROJECT_KEY, REQUIREMENTS_COLLECTION, UI_MEMORY_COLLECTION

    parser = argparse.ArgumentParser(description="Vectorize and upload files to Qdrant")
    parser.add_argument("--project", default=PROJECT_KEY,
                        help="Project key in original form, e.g. SCRUM-70")
    parser.add_argument("--dom",
                        help="Optional path to a DOM JSON file to upload to ui_memory")
    args = parser.parse_args()

    # PROJECT_KEY stays in its original hyphenated form — used in payloads/filters.
    # Collection names are sanitised separately.
    PROJECT_KEY             = args.project
    REQUIREMENTS_COLLECTION = collection_name_for(PROJECT_KEY, "requirements")
    #UI_MEMORY_COLLECTION    = collection_name_for(PROJECT_KEY, "ui_memory")

    print("=" * 60)
    print("Step 1b: Vectorization and Upload to Qdrant  (v2)")
    print("=" * 60)
    print(f"Project Key            : {PROJECT_KEY}  (raw — stored in payloads as-is)")
    print(f"Requirements Collection: {REQUIREMENTS_COLLECTION}  (sanitised for Qdrant)")
    #print(f"UI Memory Collection   : {UI_MEMORY_COLLECTION}  (sanitised for Qdrant)")
    print(f"Input Directory        : {INBOX_DIR}")
    if BASE_URL:
        print(f"BASE_URL               : {BASE_URL}/")

    # ── Requirements upload ────────────────────────────────────────────────
    ff         = FileFactory()
    all_points = []
    sources    = scan_inbox()

    if not sources:
        print(f"\n⚠ No files found in {INBOX_DIR}")
    else:
        for i, fp in enumerate(sources, 1):
            print(f"\n[{i}/{len(sources)}] Processing {fp}…")
            pts = ff.process_file(fp)
            print(f"  Found {len(pts)} text chunks")
            all_points.extend(pts)

        # Print content_type breakdown for visibility
        from collections import Counter
        ct_counts = Counter(p.get("content_type", "general") for p in all_points)
        print(f"\nContent type breakdown: {dict(ct_counts)}")
        print(f"Total points to vectorize: {len(all_points)}")

        upload_to_qdrant(all_points, REQUIREMENTS_COLLECTION)
        verify_upload(REQUIREMENTS_COLLECTION)

        if all_points:
            generate_prd(all_points, PROJECT_KEY)

    # # ── DOM upload ─────────────────────────────────────────────────────────
    # dom_file = None
    # if args.dom:
    #     if not os.path.exists(args.dom):
    #         print(f"\n⚠ DOM file not found: {args.dom}")
    #     else:
    #         dom_file = args.dom
    # else:
    #     candidates: List[str] = []
    #     jira_sync_root = os.path.join(DOCS_DIR, "jira_sync")
    #     if os.path.isdir(jira_sync_root):
    #         for dirpath, _dirs, fnames in os.walk(jira_sync_root):
    #             for fn in fnames:
    #                 if fn.endswith(".json") and "dom" in fn.lower():
    #                     candidates.append(os.path.join(dirpath, fn))
    #     candidates += glob.glob(os.path.join(DOCS_DIR, "live_dom_elements*.json"))
    #     if candidates:
    #         dom_file = max(candidates, key=os.path.getmtime)

    # if dom_file:
    #     print(f"\n{'='*60}")
    #     print(f"Uploading DOM elements from {dom_file}…")
    #     print(f"{'='*60}")
    #     try:
    #         with open(dom_file) as f:
    #             dom_data = json.load(f)
    #         upload_dom_to_qdrant(dom_data, UI_MEMORY_COLLECTION)
    #         verify_upload(UI_MEMORY_COLLECTION)
    #     except Exception as exc:
    #         print(f"  ✗ DOM upload failed: {exc}")


if __name__ == "__main__":
    main()