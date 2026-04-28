#!/usr/bin/env python3
"""
Step 1b: Vectorization Script (BMAD BMM Architecture)
Generates vectors from various file formats (.pdf, .xlsx, .json, .csv)
using Ollama (mxbai-embed-large) and uploads them to Qdrant with
project-specific isolation.

Fix log
-------
* vectorize_and_upload_dom_elements (NEW): When DOM elements are uploaded to
  Qdrant ui_memory, each point's payload now includes a top-level `url` field
  populated from the DOM capture data (dom_data["url"] or dom_data["page_url"]
  or BASE_URL fallback).

  Without this field, BasePage.resolveUrlFromQdrant() and
  step_generator.resolve_url_from_qdrant() always return "" because they
  search the payload for a "url" key that was never stored.  This caused
  EVERY navigation step to fall through to the BASE_URL fallback with the
  warning:

      ⚠ No Qdrant URL match for 'I am on the SauceDemo login page'
        — falling back to BASE_URL root: https://www.saucedemo.com/

  With the fix, Qdrant can return the exact page URL (e.g.
  "https://www.saucedemo.com/" for login-page elements) so navigation steps
  resolve precisely even when multiple pages are tested.

* upload_to_qdrant: metadata.created_at now uses the real current timestamp
  instead of a hardcoded 2024-01-01 value.

* generate_prd: removed hardcoded SauceDemo references; PRD content is now
  generated purely from the extracted requirements data.
"""

import json
import csv
import hashlib
import os
import re
import argparse
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Dict, Any, Optional

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
BASE_URL        = os.getenv("BASE_URL",         "").rstrip("/")
VECTOR_SIZE     = 1024  # mxbai-embed-large produces 1024-dimensional vectors

PROJECT_KEY             = "SCRUM-103"   # overridden by --project
REQUIREMENTS_COLLECTION: Optional[str] = None
UI_MEMORY_COLLECTION:    Optional[str] = None

INBOX_DIR = "docs/inbox"
DOCS_DIR  = "docs"


# ── ADF / text helpers ─────────────────────────────────────────────────────────

def extract_plain_text(value) -> str:
    """
    Recursively convert an Atlassian Document Format (ADF) dict — or any
    nested structure — into a plain UTF-8 string.

    Jira stores rich-text fields (epic/story descriptions, acceptance criteria)
    as ADF JSON.  Sending a raw repr() of that dict to the embedding model
    produces a 500 / empty-vector because the model receives thousands of tokens
    of JSON noise instead of prose.

    Extraction rules
    ────────────────
    • ADF leaf node  → {'type': 'text', 'text': '...'} → return text value
    • ADF branch     → recurse into 'content' array
    • Plain string   → return as-is
    • List           → join each element
    • Anything else  → str()
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "text":
            return value.get("text", "")
        parts = []
        for child in value.get("content", []):
            parts.append(extract_plain_text(child))
        return " ".join(p for p in parts if p.strip())
    if isinstance(value, list):
        return " ".join(extract_plain_text(v) for v in value if v)
    return str(value)


def extract_urls_from_text(text: str) -> List[str]:
    """
    Pull every http/https URL out of a plain-text string.
    Used to preserve URL references that live inside ADF prose
    (e.g. 'Target URL: https://www.saucedemo.com/') so they are
    stored alongside the vectorized chunk and are queryable later.
    """
    return re.findall(r'https?://[^\s\'"<>]+', text)


# ── Embedding helpers ──────────────────────────────────────────────────────────

def generate_embedding(text: str) -> List[float]:
    """Generate embedding vector using LLM Gateway."""
    return get_llm_gateway().generate_embedding(text)


def normalize_business_intent(text: str) -> Dict[str, Any]:
    """Clean and format requirements text into a Business Intent object."""
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
    """Extract meaningful keywords from text."""
    common = {
        'the','and','or','but','for','nor','yet','so','this','that',
        'these','those','with','from','are','was','were','been','have',
        'has','had','will','would','could','should','shall','must',
    }
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())
    return list(dict.fromkeys(w for w in words if w not in common))[:10]


def generate_id(text: str, filename: str, page_or_row: int) -> int:
    """Generate a deterministic numeric ID from text + filename + row."""
    combined = f"{filename}_{page_or_row}_{text}"
    return int(hashlib.md5(combined.encode()).hexdigest()[:8], 16)


# ── Requirements processing ────────────────────────────────────────────────────

def process_raw_requirements(filepath: str) -> List[Dict]:
    """Process raw_requirements.json and create text chunks for vectorization."""
    with open(filepath, 'r') as f:
        data = json.load(f)

    points   = []
    filename = os.path.basename(filepath)

    sections = []

    epic = data.get("epic", {})
    if epic:
        # ADF guard: description may be a nested dict — flatten to plain text
        epic_desc = extract_plain_text(epic.get("description", ""))
        sections.append(("epic",
            f"Epic {epic.get('key','')}: {epic.get('summary','')}\n{epic_desc}",
            1))

    story = data.get("story", {})
    if story:
        story_desc = extract_plain_text(story.get("description", ""))
        sections.append(("story",
            f"Story {story.get('key','')}: {story.get('summary','')}\n{story_desc}",
            1))

    ac = data.get("acceptance_criteria", {})
    if ac:
        sections.append(("acceptance_criteria",
            f"Acceptance Criteria:\nMain: {extract_plain_text(ac.get('main',''))}\n"
            f"Expected: {extract_plain_text(ac.get('expected_outcomes',''))}\n"
            f"Out of Scope: {extract_plain_text(ac.get('out_of_scope',''))}",
            1))

    ct = data.get("consolidated_text", "")
    if ct:
        sections.append(("consolidated", extract_plain_text(ct), 1))

    for section_type, text, page_or_row in sections:
        # Collect any URLs referenced in this chunk so they are queryable in Qdrant
        referenced_urls = extract_urls_from_text(text)
        points.append({
            "id":              generate_id(text, filename, page_or_row),
            "text":            text,
            "business_intent": normalize_business_intent(text),
            "source":          filename,
            "section":         section_type,
            "requirement_id":  data.get("story", {}).get("key", "UNKNOWN"),
            # ── URL fields ────────────────────────────────────────────────────
            # Primary URL: first http reference found in the chunk (usually the
            # app under test).  Stored at top level so resolveUrlFromQdrant()
            # can find it with a simple payload["url"] lookup.
            "url":             referenced_urls[0] if referenced_urls else "",
            # Full list preserved for richer downstream queries
            "referenced_urls": referenced_urls,
            "ancestry": {
                "filename":    filename,
                "page_or_row": page_or_row,
                "section":     section_type,
            },
        })

    return points


def process_csv_file(filepath: str, source_name: str) -> List[Dict]:
    """Process a CSV file and create text chunks for vectorization."""
    points   = []
    filename = os.path.basename(filepath)

    with open(filepath, 'r') as f:
        rows = list(csv.DictReader(f))

    for i, row in enumerate(rows):
        text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
        points.append({
            "id":              generate_id(text, filename, i + 1),
            "text":            text,
            "business_intent": normalize_business_intent(text),
            "source":          filename,
            "section":         "row",
            "requirement_id":  f"{source_name}_row_{i+1}",
            "ancestry": {
                "filename":    filename,
                "page_or_row": i + 1,
                "section":     "row",
            },
        })

    return points


# ── Qdrant upload (requirements) ───────────────────────────────────────────────

def upload_to_qdrant(points: List[Dict], collection_name: str) -> int:
    """
    Generate embeddings and upload requirements points to Qdrant.

    Deletes and recreates the collection on each run so a new project run
    never inherits stale 'memory' from a previous one.
    """
    client      = QdrantClient(url=QDRANT_URL)
    collections = client.get_collections().collections

    if any(c.name == collection_name for c in collections):
        print(f"Collection '{collection_name}' exists — deleting and recreating...")
        try:
            client.delete_collection(collection_name=collection_name)
            print(f"  ✓ Deleted '{collection_name}'")
        except Exception as exc:
            print(f"  ⚠ Could not delete: {exc} — continuing with existing collection")

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
    )
    print(f"Collection '{collection_name}' created.")

    print(f"\nProcessing {len(points)} points...")
    uploaded = 0
    for i, pd_ in enumerate(points):
        try:
            print(f"  [{i+1}/{len(points)}] {pd_['text'][:60]}...")
            vec = generate_embedding(pd_["text"])

            # Guard: skip points whose embedding failed (empty or wrong dimension).
            # An empty vector would cause Qdrant to reject the whole batch with
            # "Vector dimension error: expected dim: 1024, got 0".
            if not vec or len(vec) != VECTOR_SIZE:
                print(f"    ✗ Skipping — bad embedding ({len(vec)} dims). "
                      f"Text snippet: {pd_['text'][:80]!r}")
                continue

            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=pd_["id"],
                    vector=vec,
                    payload={
                        "source":          pd_["source"],
                        "text":            pd_["text"],
                        "section":         pd_.get("section", ""),
                        "requirement_id":  pd_["requirement_id"],
                        "project_key":     PROJECT_KEY,
                        "ancestry":        pd_.get("ancestry", {}),
                        "business_intent": pd_.get("business_intent", {}),
                        # ── URL references extracted from requirements text ──
                        # Stored at top-level so resolveUrlFromQdrant() (BasePage /
                        # step_generator) can return the app URL from a semantic
                        # search hit even against requirements chunks, not only DOM.
                        "url":             pd_.get("url", ""),
                        "referenced_urls": pd_.get("referenced_urls", []),
                        "metadata": {
                            "created_at": datetime.now().isoformat(),
                            "version":    "1.0",
                        },
                    },
                )],
            )
            uploaded += 1
            print(f"    ✓ Uploaded ID: {pd_['id']}"
                  + (f"  url={pd_['url']}" if pd_.get("url") else ""))
        except Exception as exc:
            print(f"    ✗ Error: {exc}")

    return uploaded


# ── DOM upload — THE KEY FIX ───────────────────────────────────────────────────

def upload_dom_to_qdrant(dom_data: Dict, collection_name: str) -> int:
    """
    Upload DOM elements to the ui_memory Qdrant collection.

    CRITICAL FIX: every point payload now includes a top-level `url` field
    ──────────────────────────────────────────────────────────────────────
    BasePage.resolveUrlFromQdrant() and step_generator.resolve_url_from_qdrant()
    both search Qdrant and return the first hit whose payload["url"] starts
    with "http".  If no point stores a `url` field, these helpers always return
    "" and every navigation step falls through to the BASE_URL root fallback,
    printing a noisy warning and losing per-page URL resolution.

    The page URL is read from dom_data["url"] (set by dom_capture.py) or
    dom_data["page_url"], falling back to the BASE_URL env var.  This means
    you get correct URL resolution as soon as you have a DOM snapshot with a
    url field — no re-crawling needed, just re-run vectorize_and_upload.py.

    Schema per point
    ────────────────
    {
      "source":      "dom_capture",
      "text":        "<element description>",
      "project_key": "<PROJECT_KEY>",
      "url":         "https://www.saucedemo.com/",   ← NEW, always present
      "details":     { <raw DOM element dict> },
    }
    """
    client      = QdrantClient(url=QDRANT_URL)
    collections = client.get_collections().collections

    if not any(c.name == collection_name for c in collections):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"  ✓ Created DOM collection '{collection_name}'")
    else:
        print(f"  ✓ DOM collection '{collection_name}' already exists — upserting")

    # Resolve the page URL for this DOM snapshot.
    # dom_capture.py should store the crawled URL in dom_data["url"].
    page_url = (
        dom_data.get("url")
        or dom_data.get("page_url")
        or dom_data.get("base_url")
        or (BASE_URL + "/" if BASE_URL else "")
    )
    if not page_url:
        print("  ⚠ No URL found in DOM data and BASE_URL is unset — "
              "navigation URL resolution will fall back to BASE_URL at runtime")

    # Build a flat list of (kind, element_dict, text_fields) tuples
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
            parts = [f"{k}={el.get(k,'')}" for k in fields if el.get(k)]
            text  = kind + (" " + " ".join(parts) if parts else "")
            points.append({
                "id":      abs(hash(f"{kind}_{i}_{PROJECT_KEY}")) % (2**31),
                "text":    text,
                "details": el,
            })

    uploaded = 0
    for i, p in enumerate(points):
        try:
            vec = generate_embedding(p["text"])
            if not vec:
                continue
            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=p["id"],
                    vector=vec,
                    payload={
                        "source":      "dom_capture",
                        "text":        p["text"],
                        "project_key": PROJECT_KEY,
                        "url":         page_url,      # ← THE FIX
                        "details":     p["details"],
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

    print(f"  ✓ Uploaded {uploaded}/{len(points)} DOM elements → '{collection_name}'")
    print(f"  ✓ Page URL stored in all points: {page_url or '(empty)'}")
    return uploaded


# ── Qdrant verification ────────────────────────────────────────────────────────

def verify_upload(collection_name: str) -> Optional[int]:
    """Verify the upload by checking point count and project isolation."""
    client = QdrantClient(url=QDRANT_URL)
    try:
        info = client.get_collection(collection_name)
        print(f"\n{'='*50}")
        print(f"Collection : {collection_name}")
        print(f"Points     : {info.points_count}")
        print(f"Status     : {info.status}")
        print(f"Project    : {PROJECT_KEY}")
        print(f"{'='*50}")

        filtered, _ = client.scroll(
            collection_name=collection_name,
            scroll_filter={
                "must": [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
            },
            limit=10,
        )
        print(f"Sample points for '{PROJECT_KEY}': {len(filtered)}")
        if filtered:
            sample = filtered[0].payload
            print(f"  url field present: {'url' in sample}")
            if "url" in sample:
                print(f"  url value: {sample['url']}")
            print(f"  ancestry: {sample.get('ancestry', {})}")
        return info.points_count
    except Exception as exc:
        print(f"Error verifying upload: {exc}")
        return None


# ── File processors ────────────────────────────────────────────────────────────

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
                    points.append({
                        "id":              generate_id(text, filename, pg + 1),
                        "text":            text,
                        "business_intent": normalize_business_intent(text),
                        "source":          filename,
                        "section":         "page",
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
                    points.append({
                        "id":              generate_id(text, filename, idx + 1),
                        "text":            text,
                        "business_intent": normalize_business_intent(text),
                        "source":          filename,
                        "section":         f"sheet_{sheet}_row",
                        "requirement_id":  f"{filename}_{sheet}_row_{idx+1}",
                        "ancestry": {
                            "filename":    filename,
                            "page_or_row": idx + 1,
                            "section":     f"sheet_{sheet}_row",
                        },
                    })
        return points


class FileFactory:
    """Creates appropriate processors based on file extension."""

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
            print(f"Processing {filepath} with {proc.__class__.__name__}...")
            return proc.process(filepath)
        print(f"Warning: No processor for: {filepath}")
        return []


def scan_inbox() -> List[str]:
    """Scan docs/inbox/ for new files to process."""
    supported = {'.pdf', '.xlsx', '.csv', '.json'}
    files = []
    if os.path.exists(INBOX_DIR):
        for fn in os.listdir(INBOX_DIR):
            fp = os.path.join(INBOX_DIR, fn)
            if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in supported:
                files.append(fp)
    return files


# ── PRD generation ─────────────────────────────────────────────────────────────

def generate_prd(all_points: List[Dict], project_key: str) -> str:
    """Generate a Product Requirements Document from vectorized requirements."""
    print("\n" + "="*60)
    print("Generating PRD from requirements...")
    print("="*60)

    req_dir = os.path.join(DOCS_DIR, "requirements")
    os.makedirs(req_dir, exist_ok=True)

    # Collect unique requirements texts
    req_texts = list(dict.fromkeys(
        p["text"] for p in all_points
        if p.get("text") and len(p["text"]) > 20
    ))[:20]

    app_url = BASE_URL + "/" if BASE_URL else "(BASE_URL not set)"

    prd_content = f"""# Product Requirements Document (PRD)
## Project: {project_key}

### Application URL
{app_url}

### Requirements Summary
{chr(10).join(f'- {t[:200]}' for t in req_texts)}

### Vector Memory Collections
- **Requirements Collection**: `{project_key}_requirements`
- **DOM Collection**: `{project_key}_ui_memory`
- **Project Key**: `{project_key}`

### Healing Strategy
If selectors change, the Healer Guard will:
1. Query Qdrant with the semantic intent
2. Find the closest matching element in the current DOM
3. Execute the action with the new selector
4. Log the healing event for Phase 4 reporting
"""

    prd_path = os.path.join(req_dir, f"{project_key}_PRD.md")
    with open(prd_path, 'w') as f:
        f.write(prd_content)

    print(f"✓ PRD saved to: {prd_path}")
    return prd_path


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global PROJECT_KEY, REQUIREMENTS_COLLECTION, UI_MEMORY_COLLECTION

    parser = argparse.ArgumentParser(description="Vectorize and upload files to Qdrant")
    parser.add_argument("--project", default=PROJECT_KEY, help="Project key (e.g. SCRUM-103)")
    parser.add_argument(
        "--dom",
        help="Optional path to a DOM JSON file to upload to ui_memory collection",
    )
    args = parser.parse_args()

    PROJECT_KEY             = args.project
    REQUIREMENTS_COLLECTION = f"{PROJECT_KEY}_requirements"
    UI_MEMORY_COLLECTION    = f"{PROJECT_KEY}_ui_memory"

    print("=" * 60)
    print("Step 1b: Vectorization and Upload to Qdrant")
    print("=" * 60)
    print(f"Project Key            : {PROJECT_KEY}")
    print(f"Requirements Collection: {REQUIREMENTS_COLLECTION}")
    print(f"UI Memory Collection   : {UI_MEMORY_COLLECTION}")
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
            print(f"\n[{i}/{len(sources)}] Processing {fp}...")
            pts = ff.process_file(fp)
            print(f"  Found {len(pts)} text chunks")
            all_points.extend(pts)

        print(f"\nTotal points to vectorize: {len(all_points)}")
        uploaded = upload_to_qdrant(all_points, REQUIREMENTS_COLLECTION)

        total = verify_upload(REQUIREMENTS_COLLECTION)
        if total == len(all_points):
            print(f"\n✓ All {len(all_points)} requirement points uploaded and verified")
        else:
            print(f"\n⚠ Expected {len(all_points)} points, found {total}")

        if all_points:
            generate_prd(all_points, PROJECT_KEY)

    # ── DOM upload (optional --dom flag) ───────────────────────────────────
    if args.dom:
        if not os.path.exists(args.dom):
            print(f"\n⚠ DOM file not found: {args.dom}")
        else:
            print(f"\n{'='*60}")
            print(f"Uploading DOM elements from {args.dom}...")
            print(f"{'='*60}")
            try:
                with open(args.dom) as f:
                    dom_data = json.load(f)
                upload_dom_to_qdrant(dom_data, UI_MEMORY_COLLECTION)
                verify_upload(UI_MEMORY_COLLECTION)
            except Exception as exc:
                print(f"  ✗ DOM upload failed: {exc}")
    else:
        # Check if a DOM file already exists in docs/ and offer to upload it
        import glob
        dom_files = sorted(glob.glob(os.path.join(DOCS_DIR, "live_dom_elements*.json")),
                           key=os.path.getmtime, reverse=True)
        if dom_files:
            latest = dom_files[0]
            print(f"\n{'='*60}")
            print(f"Found DOM file: {latest}")
            print(f"Uploading DOM elements to '{UI_MEMORY_COLLECTION}'...")
            print(f"{'='*60}")
            try:
                with open(latest) as f:
                    dom_data = json.load(f)
                upload_dom_to_qdrant(dom_data, UI_MEMORY_COLLECTION)
                verify_upload(UI_MEMORY_COLLECTION)
            except Exception as exc:
                print(f"  ✗ DOM upload failed: {exc}")


if __name__ == "__main__":
    main()