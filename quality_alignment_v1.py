#!/usr/bin/env python3
"""
Quality Alignment — Phase 3  (v2)
===================================
Reads the outputs produced by the previous pipeline steps and uses the
LLM Gateway to generate a detailed Gherkin feature file from real requirements.

Pipeline context
────────────────
  Phase 0  jira_sync_agent.py      →  docs/inbox/*.json
                                       Qdrant: {COLLECTION}_requirements
  Phase 1  dom_capture.py          →  docs/live_dom_elements_*.json
                                       Qdrant: {COLLECTION}_ui_memory
  Phase 2  vectorize_and_upload.py →  Qdrant populated
                                       docs/requirements/{PROJECT_KEY}_PRD.md
  Phase 3  quality_alignment.py   →  tests/features/{PROJECT_KEY}.feature
                                       docs/quality_alignment_report_{PROJECT_KEY}.json

Key conventions (v2)
─────────────────────
  PROJECT_KEY      "SCRUM-70"  — always the original hyphenated form.
                                 Used as project_key filter value in all
                                 Qdrant queries — matches exactly what
                                 vectorize_and_upload_v2.py stores.

  RAW_PROJECT_KEY  Same as PROJECT_KEY.  Kept as a separate name to make
                   intent obvious at call sites.

  Collection names are the ONLY place where the key is sanitised
  (hyphens → underscores):
      SCRUM-70  →  SCRUM_70_requirements
                   SCRUM_70_ui_memory

Fix log
-------
v2 vs v1:

  FIX 1 — project_key filter now uses RAW_PROJECT_KEY ("SCRUM-70"), not the
           sanitised form ("SCRUM_70").  This was the primary cause of all
           cross-reference queries returning 0 results: every point in Qdrant
           stores project_key as "SCRUM-70" but the filter was sending "SCRUM_70".

  FIX 2 — DOM cross-reference queries exclusively use extracted UI keywords
           (content_type == "ui_spec" records preferred).  Process-level subtask
           text ("Activities: Execute full test suite…") is excluded from DOM
           queries because it generates tokens like "activities", "execute",
           "criteria" that will never match DOM element labels.

  FIX 3 — validate_ui_alignment_dynamic also filters by content_type so
           process records do not pollute confidence scoring.

  FIX 4 — sanitize_collection_name() is the single source of truth for
           collection name derivation; called in both scripts consistently.

  Preserved from v1:
  - _strip_preamble, _extract_ui_keywords, normalize_text, _token_overlap_score,
    compute_similarity, build_element_text (all unchanged)
  - Gherkin generation logic (unchanged)
  - Drift analysis, selector coverage, report structure (unchanged)
  - All QA signal / self-healing logic (unchanged)
"""

import csv
import glob
import json
import os
import re
import argparse
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import requests

from llm_gateway import get_llm_gateway

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
OLLAMA_HOST     = os.getenv("OLLAMA_HOST",     "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "mxbai-embed-large:latest")
QDRANT_URL      = os.getenv("QDRANT_URL",      "http://localhost:6333")
BASE_URL        = os.getenv("BASE_URL",        "").rstrip("/")
#VECTOR_SIZE     = 1024

DOCS_DIR      = "docs"
INBOX_DIR     = os.path.join(DOCS_DIR, "inbox")
SELECTORS_CSV = os.path.join(DOCS_DIR, "selectors.csv")

# Set at startup by main() — never mutated elsewhere.
# RAW_PROJECT_KEY / PROJECT_KEY are identical and always in hyphenated form,
# e.g. "SCRUM-70".  The distinction exists only for readability at call sites.
RAW_PROJECT_KEY         = ""
PROJECT_KEY             = ""
REQUIREMENTS_COLLECTION = ""
DOM_COLLECTION          = ""

_vector_size_cache: Optional[int] = None

def get_vector_size() -> int:
    global _vector_size_cache
    if _vector_size_cache is not None:
        return _vector_size_cache
    probe = generate_embedding("probe")
    _vector_size_cache = len(probe) if probe else 1024
    print(f"  [Embedding] Vector size probed: {_vector_size_cache}")
    return _vector_size_cache
# ══════════════════════════════════════════════════════════════════════════════
# Naming helpers  (must mirror vectorize_and_upload_v2.py exactly)
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_collection_name(name: str) -> str:
    """
    Convert an arbitrary string into a valid Qdrant collection name.
    Called ONLY when building collection names, never for filter values.

    "SCRUM-70_requirements"  →  "SCRUM_70_requirements"
    """
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return sanitized.strip('_') or 'collection'


def collection_name_for(project_key: str, suffix: str) -> str:
    return sanitize_collection_name(f"{project_key}_{suffix}")


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant helpers
# ══════════════════════════════════════════════════════════════════════════════

def _qdrant() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def _collection_exists(name: str) -> bool:
    try:
        return any(c.name == name for c in _qdrant().get_collections().collections)
    except Exception:
        return False


def generate_embedding(text: str) -> List[float]:
    gateway = get_llm_gateway()
    model_override = gateway.resolve_model_for_agent(
        "quality_alignment_v1",
        purpose="embedding",
        fallback_model=None,
    )
    return gateway.generate_embedding(text, model_override=model_override)


def scroll_all_points(collection_name: str, limit: int = 100) -> List[Dict]:
    client      = QdrantClient(url=QDRANT_URL)
    all_payloads = []
    offset       = None

    while True:
        records, offset = client.scroll(
            collection_name=collection_name,
            limit=limit,
            offset=offset,
            with_payload=True,
        )
        if not records:
            break
        for r in records:
            if r.payload:
                all_payloads.append(r.payload)
        if offset is None:
            break

    return all_payloads


def search_collection(collection: str, query_text: str, limit: int = 10,
                      content_type_filter: Optional[str] = None) -> List[Dict]:
    """
    Semantic vector search in a collection, filtered to PROJECT_KEY.

    PROJECT_KEY is used RAW (e.g. "SCRUM-70") — this matches exactly what
    vectorize_and_upload_v2.py stores in every point's project_key payload.

    Optionally filters by content_type (e.g. "ui_spec") to exclude process
    records from DOM-matching queries.
    """
    vector = generate_embedding(query_text)
    if not vector:
        return []

    # Build the must-filter list
    must_filters = [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
    if content_type_filter:
        must_filters.append({"key": "content_type", "match": {"value": content_type_filter}})

    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector":       vector,
                "limit":        limit,
                "with_payload": True,
                "filter":       {"must": must_filters},
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  ⚠ Qdrant search {resp.status_code}: {resp.text[:80]}")
            return []
        hits = resp.json().get("result", [])
        return [
            {
                "text":    h["payload"].get("text", ""),
                "score":   h.get("score", 0),
                "payload": h["payload"],
            }
            for h in hits
            if h.get("payload", {}).get("project_key") == PROJECT_KEY
        ]
    except Exception as exc:
        print(f"  ⚠ search failed: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Phase 0/1/2 output readers
# ══════════════════════════════════════════════════════════════════════════════

def load_requirements_from_qdrant() -> List[Dict]:
    """
    Load all requirement payloads from Qdrant for PROJECT_KEY.

    Returns a list of payload dicts (not just text strings) so callers
    can filter by content_type without re-fetching.
    """
    if not _collection_exists(REQUIREMENTS_COLLECTION):
        print(f"  ⚠ Collection '{REQUIREMENTS_COLLECTION}' not found.")
        return []

    payloads = scroll_all_points(REQUIREMENTS_COLLECTION, limit=300)
    print(f"  DEBUG: fetched {len(payloads)} total records from '{REQUIREMENTS_COLLECTION}'")

    matched = []
    for p in payloads:
        if p.get("project_key") == PROJECT_KEY:
            matched.append(p)

    print(f"  DEBUG: {len(matched)} records match project_key='{PROJECT_KEY}'")

    # Deduplicate by text
    seen, result = set(), []
    for p in matched:
        t = (p.get("text") or p.get("content") or p.get("summary") or "").strip()
        if t and t not in seen:
            seen.add(t)
            result.append(p)

    print(f"  ✓ Loaded {len(result)} unique requirement payload(s) from Qdrant")
    return result


def load_requirements_from_inbox() -> List[Dict]:
    items = []
    if not os.path.exists(INBOX_DIR):
        return items
    for path in sorted(glob.glob(os.path.join(INBOX_DIR, "*.json"))):
        try:
            data = json.load(open(path))
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                items.extend(data["issues"]) if "issues" in data else items.append(data)
        except Exception as exc:
            print(f"  ⚠ Could not read {path}: {exc}")

    prefix  = RAW_PROJECT_KEY.split("-")[0]
    project = [i for i in items if str(i.get("key", "")).startswith(prefix)]
    print(f"  ✓ Loaded {len(project)} issue(s) from inbox")
    return project

def load_jira_story_structured() -> Optional[Dict]:
    """
    Load the Jira story JSON saved by jira_sync_agent and extract
    structured fields: summary, description, acceptance criteria.

    Works for any project — reads the most recent jira_sync folder
    for PROJECT_KEY, finds the story JSON, and parses Atlassian
    document format or plain text description.

    Returns a dict with keys:
      key, summary, description, acceptance_criteria (as plain text)
    or None if not found.
    """
    import glob as _glob

    jira_sync_root = os.path.join(DOCS_DIR, "jira_sync")
    if not os.path.isdir(jira_sync_root):
        return None

    # Find the most recent run folder for this project key
    pattern = os.path.join(jira_sync_root, f"{RAW_PROJECT_KEY}_*", "story", "*.json")
    candidates = _glob.glob(pattern)
    if not candidates:
        return None

    story_path = max(candidates, key=os.path.getmtime)

    try:
        with open(story_path) as f:
            raw = json.load(f)
    except Exception as exc:
        print(f"  ⚠ Could not read Jira story: {exc}")
        return None

    fields = raw.get("fields", {})
    key    = raw.get("key", RAW_PROJECT_KEY)

    # ── Summary ───────────────────────────────────────────────────────────
    summary = (fields.get("summary") or "").strip()

    # ── Description — handle Atlassian Document Format (ADF) ──────────────
    def _extract_text_from_adf(node) -> str:
        """Recursively extract plain text from Atlassian Document Format."""
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return " ".join(_extract_text_from_adf(n) for n in node)
        if isinstance(node, dict):
            node_type = node.get("type", "")
            text      = node.get("text", "")
            content   = node.get("content", [])
            if text:
                return text
            if node_type in ("bulletList", "orderedList", "listItem", "paragraph",
                             "heading", "blockquote", "panel"):
                parts = [_extract_text_from_adf(c) for c in content]
                return "\n".join(p for p in parts if p.strip())
            return _extract_text_from_adf(content)
        return ""

    desc_raw = fields.get("description", "")
    if isinstance(desc_raw, dict):
        description = _extract_text_from_adf(desc_raw).strip()
    elif isinstance(desc_raw, str):
        description = desc_raw.strip()
    else:
        description = ""

    # ── Acceptance Criteria — look for custom field or parse from description
    ac_text = ""

    # Try common Jira AC custom fields first
    for ac_field in ("customfield_10016", "customfield_10014",
                     "acceptance_criteria", "acceptanceCriteria"):
        val = fields.get(ac_field)
        if val:
            if isinstance(val, dict):
                ac_text = _extract_text_from_adf(val).strip()
            elif isinstance(val, str):
                ac_text = val.strip()
            if ac_text:
                break

    # If no custom field, extract AC section from description
    if not ac_text and description:
        import re as _re
        # Look for AC section headers like "Acceptance Criteria", "✅ AC", etc.
        ac_pattern = _re.compile(
            r'(?:acceptance criteria|✅|ac\d|criteria).*',
            _re.IGNORECASE | _re.DOTALL
        )
        ac_match = ac_pattern.search(description)
        if ac_match:
            ac_text = ac_match.group(0).strip()

    print(f"  ✓ Jira story loaded: {key} — '{summary[:60]}'")
    if ac_text:
        print(f"  ✓ Acceptance criteria extracted ({len(ac_text)} chars)")
    else:
        print(f"  ⚠ No acceptance criteria found in Jira story")

    return {
        "key":                key,
        "summary":            summary,
        "description":        description,
        "acceptance_criteria": ac_text,
    }

def load_prd_text() -> str:
    for path in [
        os.path.join(DOCS_DIR, f"{RAW_PROJECT_KEY}_prd.md"),
        os.path.join(DOCS_DIR, f"{RAW_PROJECT_KEY}_requirements.md"),
        os.path.join(DOCS_DIR, "prd.md"),
        os.path.join(DOCS_DIR, "requirements", f"{RAW_PROJECT_KEY}_PRD.md"),
    ]:
        if os.path.exists(path):
            text = open(path).read().strip()
            if text:
                print(f"  ✓ PRD loaded from {path}")
                return text
    return ""


def find_latest_dom_file() -> Optional[str]:
    files = glob.glob(os.path.join(DOCS_DIR, "live_dom_elements_*.json"))
    if files:
        return max(files, key=os.path.getmtime)
    fb = os.path.join(DOCS_DIR, "live_dom_elements.json")
    return fb if os.path.exists(fb) else None


def load_dom_data() -> Dict:
    path = find_latest_dom_file()
    if not path:
        print("  ⚠ No DOM file found. Run dom_capture.py --project first.")
        return {}
    try:
        data  = json.load(open(path))
        total = len(data.get("all_interactive_elements", []))
        print(f"  ✓ DOM loaded from {path} ({total} interactive elements)")
        qa_summary = data.get("qa_summary", {})
        if qa_summary:
            print(f"  ✓ QA signals loaded — "
                  f"total_elements={qa_summary.get('total_elements', 'N/A')}, "
                  f"risky_elements={qa_summary.get('risky_elements', 'N/A')}, "
                  f"overlay_present={qa_summary.get('overlay_present', False)}")
        qa_analysis = data.get("qa_analysis", [])
        if qa_analysis:
            print(f"  ✓ {len(qa_analysis)} QA-analysed element(s) available")
        return data
    except Exception as exc:
        print(f"  ✗ Could not read DOM file: {exc}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers for separating requirement payloads by content_type
# ══════════════════════════════════════════════════════════════════════════════

def _texts_for_dom_matching(req_payloads: List[Dict]) -> List[str]:
    """
    Return requirement texts that are useful for DOM label matching.

    v3 behavior:
      - combine all DOM-relevant buckets instead of returning only the first
        non-empty bucket
      - prefer the richer buckets first so query construction still sees the
        most specific material early

    Priority order:
      1. content_type == "ui_spec"              (pipe-delimited Excel rows)
      2. content_type == "acceptance_criteria"  (AC blocks with field names)
      3. content_type == "test_data"            (input examples)
      4. content_type == "general"              (fallback)

    Explicitly EXCLUDED:
      - content_type == "process"  (execution instructions, no field names)

    Falls back to ALL non-process texts if no DOM-relevant bucket exists.
    """
    by_type: Dict[str, List[str]] = {
        "ui_spec": [], "acceptance_criteria": [], "test_data": [], "general": []
    }
    for p in req_payloads:
        ct   = p.get("content_type", "general")
        text = (p.get("text") or "").strip()
        if ct == "process" or not text or _is_generic_requirement_text(text):
            continue
        bucket = by_type.get(ct, by_type["general"])
        bucket.append(text)

    ordered: List[str] = []
    for ct in ("ui_spec", "acceptance_criteria", "test_data", "general"):
        if by_type[ct]:
            ordered.extend(by_type[ct])

    # BUG FIX: "general" records that contain explicit CSS selectors or
    # pipe-delimited field specs are treated as ui_spec equivalents for DOM
    # query purposes.  Previously they were only included if the preferred
    # buckets were ALL empty — meaning the "Selectors • Email Field: #userEmail"
    # record was silently omitted whenever any acceptance_criteria existed.
    if ordered:
        # Always lift high-signal general records to the front so query
        # construction sees them early (they produce the best DOM keyword tokens)
        high_signal = [
            t for t in by_type["general"]
            if re.search(r'#[a-zA-Z][\w-]{1,}', t) or ("|" in t and ":" in t)
        ]
        others = [t for t in ordered if t not in high_signal]
        return high_signal + others

    # Absolute fallback: everything (process records still excluded)
    return [p.get("text", "") for p in req_payloads
            if p.get("content_type") != "process" and p.get("text")]


def _texts_for_validation(req_payloads: List[Dict]) -> List[str]:
    preferred_sections = {"ui_spec", "acceptance_criteria", "test_data"}
    selected: List[str] = []
    fallback: List[str] = []
    for payload in req_payloads:
        text = (payload.get("text") or "").strip()
        if not text or payload.get("content_type") == "process" or _is_generic_requirement_text(text):
            continue
        if payload.get("content_type") in preferred_sections:
            selected.append(text)
        else:
            fallback.append(text)

    # BUG FIX: the original returned `selected or fallback`, so whenever any
    # preferred records existed, ALL "general" records were dropped — including
    # the "Selectors • Email Field: #userEmail • Submit Button: #submit"
    # requirement which is the ONLY record explicitly naming DOM elements.
    # That record was classified content_type="general" by the vectorizer.
    #
    # Fix: always include "general" records that contain an explicit CSS
    # selector (#id) or pipe-delimited field list regardless of whether
    # preferred-type records are present.  These are the highest-signal
    # records for DOM label matching.
    def _has_explicit_selector(text: str) -> bool:
        return bool(re.search(r'#[a-zA-Z][\w-]{1,}', text))

    def _has_pipe_fields(text: str) -> bool:
        return "|" in text and ":" in text

    high_signal_general = [
        t for t in fallback
        if _has_explicit_selector(t) or _has_pipe_fields(t)
    ]
    low_signal_general = [
        t for t in fallback
        if t not in high_signal_general
    ]

    # Return: preferred + high-signal general always; low-signal general only
    # as absolute fallback when nothing else exists.
    combined = selected + high_signal_general
    return combined if combined else low_signal_general


def _all_texts(req_payloads: List[Dict]) -> List[str]:
    """All requirement texts including process records — used for Gherkin/drift."""
    return [p.get("text", "") for p in req_payloads if p.get("text")]


# ══════════════════════════════════════════════════════════════════════════════
# Shared semantic-matching helpers  (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

STOPWORDS = {
    "the", "is", "a", "an", "to", "of", "and", "or", "in", "on",
    "at", "by", "for", "with", "that", "this", "it", "be", "are",
    "was", "were", "has", "have", "had", "will", "would", "could",
    "should", "shall", "must", "from", "not", "no", "its", "if",
    "as", "but", "so", "do", "does",
}

UI_SYNONYMS: Dict[str, set] = {
    "email":           {"email", "mail", "useremail", "emailaddress", "e-mail", "emailid"},
    "password":        {"password", "pass", "pwd", "passcode", "secret", "pin"},
    "username":        {"username", "user", "userid", "loginid", "accountname"},
    "submit":          {"submit", "send", "confirm", "save", "continue", "proceed", "go", "ok"},
    "button":          {"button", "btn", "cta"},
    "name":            {"name", "fullname", "firstname", "lastname", "displayname",
                        "givenname", "full name", "full_name"},
    "phone":           {"phone", "mobile", "tel", "telephone", "contactnumber", "cell"},
    # BUG FIX: "address" canonical now includes "currentaddress" and "permanentaddress"
    # so DemoQA's "Current Address" textarea maps to the same token as any
    # requirement that says "address field".
    "address":         {"address", "street", "city", "zipcode", "zip", "postcode",
                        "location", "currentaddress", "permanentaddress",
                        "current address", "permanent address"},
    "search":          {"search", "find", "lookup", "query", "filter", "seek"},
    "cancel":          {"cancel", "close", "dismiss", "abort", "back", "exit", "escape"},
    "login":           {"login", "signin", "logon", "authenticate"},
    "logout":          {"logout", "signout", "logoff"},
    "register":        {"register", "signup", "createaccount", "enroll"},
    "select":          {"select", "choose", "pick", "dropdown", "combobox"},
    "upload":          {"upload", "attach", "browse", "filepicker"},
    "date":            {"date", "dob", "birthdate", "dateofbirth", "calendar"},
    # BUG FIX: "yes" / "no" are radio/checkbox labels on DemoQA; map them to
    # "option" so a requirement mentioning "option" or "choice" can match.
    "option":          {"option", "yes", "no", "true", "false", "maybe",
                        "radio", "choice", "value"},
    # BUG FIX: "impressive" is a DemoQA check-box tree node label.  Map it to
    # "item" so requirements about "list items" or "tree items" can match it.
    "item":            {"item", "impressive", "entry", "record", "node", "leaf",
                        "branch", "tree", "checktree"},
    "salary":          {"salary", "pay", "wage", "income", "compensation"},
    "age":             {"age", "years", "dob"},
    "department":      {"department", "dept", "team", "group", "division"},
    "table":           {"table", "grid", "webtable", "datatables", "datagrid"},
}

_SYNONYM_LOOKUP: Dict[str, str] = {}
for _canonical, _variants in UI_SYNONYMS.items():
    for _variant in _variants:
        _SYNONYM_LOOKUP[_variant] = _canonical


def _canonicalize(token: str) -> str:
    return _SYNONYM_LOOKUP.get(token, token)


def normalize_text(text: str) -> List[str]:
    tokens = re.findall(r'\w+', (text or "").lower())
    result = []
    for t in tokens:
        if t not in STOPWORDS and len(t) > 1:
            result.append(_canonicalize(t))
    return result


def _strip_preamble(text: str) -> str:
    """Strip 'Source/Project/Section/Type:' prefix lines from vectorized text."""
    preamble_keys = {"source", "project", "section", "type"}
    content: List[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in stripped:
            key = stripped.split(":", 1)[0].strip().lower()
            if key in preamble_keys:
                continue
        content.append(stripped)
    return " ".join(content).strip()


def _is_empty_acceptance_shell(text: str) -> bool:
    normalized = " ".join(_strip_preamble(text).lower().split())
    return normalized in {
        "acceptance criteria: main: expected: out of scope:",
        "acceptance criteria main expected out of scope",
    }


def _is_generic_requirement_text(text: str) -> bool:
    clean = _strip_preamble(text)
    lower = clean.lower()
    if not clean or _is_empty_acceptance_shell(clean):
        return True

    if any(marker in lower for marker in (
        "document overview",
        "technical contract",
        "non -functional constraints",
        "non-functional constraints",
    )):
        return True

    # BUG FIX: the original check used "epic " and "story " (with a trailing space)
    # which silently passed "Epic: ..." and "Story: ..." texts because the colon
    # immediately follows the word with no space.  Use a regex that matches the
    # word boundary regardless of whether a colon or space follows.
    if re.match(r'^(epic|story)[\s:]', lower):
        return True

    ui_signals = (
        "#", "field", "button", "input", "selector", "placeholder", "text box",
        "email", "password", "username", "submit", "radio", "checkbox", "dropdown",
        "textarea", "address", "salary", "age", "name", "validation", "error",
        "tooltip", "modal", "row", "table", "current address", "full name",
    )
    has_ui_signal = any(signal in lower for signal in ui_signals)
    token_count = len(normalize_text(clean))
    return (not has_ui_signal) and token_count < 6


def _extract_ui_keywords(text: str) -> List[str]:
    """
    Extract short, UI-relevant keyword tokens from verbose requirement text.

    Priority:
      1. Pipe-delimited:  "Module: Text Box | Field: Email | …"  → ["textbox","email",…]
      2. Labelled:        "Field: Email Address"                  → ["email","address"]
      3. Fallback:        capitalised words + known UI action terms
    """
    text = _strip_preamble(text)
    keywords: List[str] = []
    seen: set = set()

    def add(token: str) -> None:
        t = _canonicalize(token.lower().strip())
        if t and t not in seen and len(t) > 1 and t not in STOPWORDS:
            seen.add(t)
            keywords.append(t)

    if "|" in text:
        for segment in text.split("|"):
            segment = segment.strip()
            if ":" in segment:
                _, _, value = segment.partition(":")
                value = value.strip()
                if value and len(value) < 60:
                    for tok in re.findall(r'\w+', value):
                        add(tok)
        if keywords:
            return keywords[:10]

    labelled = re.findall(
        r'(?:Field|Label|Name|ID|Button|Input|Element)\s*:\s*([^|.\n]{1,50})',
        text, re.IGNORECASE
    )
    for match in labelled:
        for tok in re.findall(r'\w+', match):
            add(tok)
    if keywords:
        return keywords[:10]

    # BUG FIX: the original fallback used re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    # which grabbed ANY capitalized word — "Error", "Handling", "State",
    # "Transitions", "Screenshots", "Target" — none of which are DOM element labels.
    # The tier-3 fallback now ONLY extracts tokens that appear in the explicit
    # UI action / field term regex.  Arbitrary capitalized words are discarded.
    for tok in re.findall(
        r'\b(email|password|username|submit|login|button|input|field|form|'
        r'dropdown|checkbox|radio|select|search|cancel|reset|save|delete|'
        r'edit|upload|date|phone|address|name|fullname|currentaddress|'
        r'permanentaddress|salary|age|department|firstname|lastname|'
        r'table|row|column|modal|tooltip|overlay|link|menu|nav|header|'
        r'footer|sidebar|tab|panel|card|list|item|icon|label|text|'
        r'textarea|file|image|video|audio|iframe|frame|window|dialog|'
        r'alert|confirm|prompt|toast|notification|badge|chip|tag|'
        r'checkbox|toggle|switch|slider|spinner|loader|progress)\b',
        text.lower()
    ):
        add(tok)

    return keywords[:10]


def extract_requirement_keywords(req: str) -> List[str]:
    text = _strip_preamble(req)
    kw   = _extract_ui_keywords(text)
    if kw:
        return kw
    return list(dict.fromkeys(normalize_text(text)))[:8]


def build_element_text(el: Dict, page_url: str = "") -> str:
    fields = ["label", "text", "placeholder", "name", "role", "type", "ariaLabel"]
    parts  = [str(el.get(f, "")).strip() for f in fields]
    text   = " ".join(p for p in parts if p)
    if page_url:
        text += f" {page_url}"
    return text


def _token_overlap_score(tokens_a: List[str], tokens_b: List[str]) -> float:
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    if not intersection:
        return 0.0
    jaccard   = len(intersection) / len(set_a | set_b)
    precision = len(intersection) / len(set_a)
    return 0.5 * jaccard + 0.5 * precision


def compute_similarity(req_tokens: List[str], el_tokens: List[str]) -> float:
    if not req_tokens or not el_tokens:
        return 0.0
    base        = _token_overlap_score(req_tokens, el_tokens)
    exact_bonus = 0.20 if base > 0 else 0.0
    return min(base + exact_bonus, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# DOM summary for Gherkin LLM
# ══════════════════════════════════════════════════════════════════════════════

def _dominant_form_page_url(dom_data: Dict) -> str:
    counts: Counter = Counter()
    for key, weight in (
        ("input_elements", 3),
        ("textarea_elements", 3),
        ("button_elements", 2),
        ("dropdown_elements", 2),
    ):
        for el in dom_data.get(key, []):
            page_url = (el.get("page_url") or "").strip()
            if page_url:
                counts[page_url] += weight
    if counts:
        return counts.most_common(1)[0][0]
    return (dom_data.get("page_url") or dom_data.get("url") or "").strip()


def dom_summary_for_llm(dom_data: Dict, max_elements: int = 60) -> str:
    lines = []

    # ── Group elements by page_url ─────────────────────────────────────────
    from collections import defaultdict
    pages: Dict[str, Dict[str, list]] = defaultdict(lambda: {
        "input": [], "textarea": [], "button": [], "dropdown": [], "interactive": []
    })

    for el in dom_data.get("input_elements", []):
        pages[el.get("page_url", "")]["input"].append(el)
    for el in dom_data.get("textarea_elements", []):
        pages[el.get("page_url", "")]["textarea"].append(el)
    for el in dom_data.get("button_elements", []):
        pages[el.get("page_url", "")]["button"].append(el)
    for el in dom_data.get("dropdown_elements", []):
        pages[el.get("page_url", "")]["dropdown"].append(el)
    for el in dom_data.get("all_interactive_elements", []):
        pages[el.get("page_url", "")]["interactive"].append(el)

    def fmt_el(kind: str, el: Dict, fields: List[str]) -> str:
        parts = [f"[{kind}]"]
        for f in fields:
            raw = el.get(f) or ""
            if isinstance(raw, list):
                v = ", ".join(str(x) for x in raw if x)
            elif isinstance(raw, dict):
                v = str(raw)
            else:
                v = str(raw).strip()
            if v:
                parts.append(f"{f}={v!r}")
        return "    " + " ".join(parts)

    # ── Emit per-page sections ─────────────────────────────────────────────
    per_page = max_elements // max(len(pages), 1)
    for page_url, groups in sorted(pages.items()):
        if not page_url:
            continue
        lines.append(f"\n  [PAGE] url={page_url!r}")
        count = 0
        for el in groups["input"][:per_page]:
            lines.append(fmt_el("INPUT", el, ["type", "placeholder", "label", "name", "id"]))
            count += 1
        for el in groups["textarea"][:per_page]:
            lines.append(fmt_el("TEXTAREA", el, ["placeholder", "label", "name", "id"]))
            count += 1
        for el in groups["button"][:per_page]:
            lines.append(fmt_el("BUTTON", el, ["text", "label", "id"]))
            count += 1
        for el in groups["dropdown"][:per_page]:
            lines.append(fmt_el("DROPDOWN", el, ["name", "label", "options"]))
            count += 1
        # fill remaining slots with interactive
        seen_text = set()
        for el in groups["interactive"]:
            if count >= per_page * 2:
                break
            text = (el.get("text") or el.get("placeholder") or "").strip()[:50]
            tag  = el.get("tagName", "").upper()
            if text and f"{tag}:{text}" not in seen_text:
                seen_text.add(f"{tag}:{text}")
                lines.append(f"    [{tag}] text={text!r}")
                count += 1

    return "\n".join(lines) if lines else "  (no DOM elements captured)"


# ══════════════════════════════════════════════════════════════════════════════
# Cross-reference (v2)
# ══════════════════════════════════════════════════════════════════════════════

def build_search_queries(
    dom_texts: List[str],
    inbox_issues: List[Dict],
) -> List[str]:
    """
    Build focused, UI-relevant search queries from requirement data.

    IMPORTANT: dom_texts should be pre-filtered to exclude "process" records
    (see _texts_for_dom_matching).  This function further distils them to
    short keyword tokens so vector queries match DOM label embeddings.

    v2 changes vs v1:
    1. Caller is responsible for passing only DOM-relevant texts.
    2. Inbox issues still contribute via summary/description keywords.
    3. Global deduplication; limit 25.
    """
    queries: List[str] = []
    seen: set = set()

    # BUG FIX: the original blocklist was far too narrow.  Words like "error",
    # "handling", "state", "transitions", "screenshots", "notes", "during",
    # "acceptance", "criteria", "target", "output", "number", "test", "data",
    # "invalid", "non", "empty" were all reaching Qdrant as vector queries and
    # matching random DOM points.  None of these are UI element labels.
    generic_queries = {
        # Preamble / metadata tokens
        "source", "project", "section", "type", "general", "prd", "document",
        "overview", "feature", "module", "requirement", "requirements",
        "story", "epic", "req",
        # Structural / process words from requirement prose
        "validation", "acceptance", "criteria", "target", "application",
        "output", "during", "notes", "screenshots", "number", "non",
        "empty", "test", "data", "invalid", "error", "handling", "state",
        "transitions", "classification", "model", "flow", "recovery",
        "activities", "execute", "suite", "implement", "automate",
        "verify", "ensure", "review", "check", "expected", "actual",
        "result", "results", "outcome", "outcomes", "scope", "phase",
        "step", "steps", "action", "actions", "task", "tasks",
    }

    def add(q: str) -> None:
        q = q.strip()
        q_lower = q.lower()
        if q and q_lower not in seen and len(q) > 2 and q_lower not in generic_queries:
            seen.add(q_lower)
            queries.append(q)

    # Inbox issues — summary + description keywords
    for issue in inbox_issues[:15]:
        summary = _strip_preamble(issue.get("summary") or "")
        if summary and not _is_generic_requirement_text(summary):
            add(summary[:80])
            for kw in _extract_ui_keywords(summary):
                add(kw)
        desc = _strip_preamble(issue.get("description") or "")
        if desc and not _is_generic_requirement_text(desc):
            first_line = re.split(r'[.!\n]', desc)[0].strip()
            if first_line:
                add(first_line[:80])
                for kw in _extract_ui_keywords(first_line):
                    add(kw)

    # DOM-relevant requirement texts
    for text in dom_texts[:20]:
        clean = _strip_preamble(text)
        if not clean:
            continue
        first_sentence = re.split(r'[.!\n]', clean)[0].strip()
        first_tokens = normalize_text(first_sentence)
        if len(first_tokens) >= 2:
            add(first_sentence[:80])
        for kw in _extract_ui_keywords(clean):
            add(kw)

    if not queries:
        queries = ["form", "input", "button", "submit", "field"]

    return queries[:25]


def cross_reference_with_requirements(
    req_payloads: List[Dict],
    inbox_issues: List[Dict],
) -> List[Dict]:
    """
    Cross-reference requirements against live DOM using keyword queries
    and vector similarity + token overlap reranking.

    v2 changes:
    1. DOM queries use only DOM-relevant texts (process records excluded).
    2. Qdrant search for requirements uses PROJECT_KEY raw ("SCRUM-70").
    3. Richer per-hit metadata (vector_score, token_score, combined_score).
    """
    print("\n  SEMANTIC CROSS-REFERENCE  (v2: keyword queries + token rerank)")

    if not _collection_exists(DOM_COLLECTION):
        print(f"  ⚠ DOM collection '{DOM_COLLECTION}' not found")
        return []

    dom_texts = _texts_for_dom_matching(req_payloads)
    queries   = build_search_queries(dom_texts, inbox_issues)
    results: List[Dict] = []

    for query in queries:
        dom_hits = search_collection(DOM_COLLECTION, query, limit=10)
        req_hits = (
            search_collection(REQUIREMENTS_COLLECTION, query, limit=5)
            if _collection_exists(REQUIREMENTS_COLLECTION) else []
        )

        query_tokens = normalize_text(query)
        scored: List[Dict] = []

        for hit in dom_hits:
            vector_score = hit.get("score", 0.0)
            dom_tokens   = normalize_text(hit.get("text", ""))
            tok_score    = _token_overlap_score(query_tokens, dom_tokens)
            combined     = 0.6 * vector_score + 0.4 * tok_score

            if combined < 0.15:
                continue

            scored.append({
                "text":           hit["text"][:120],
                "vector_score":   round(vector_score, 3),
                "token_score":    round(tok_score, 3),
                "combined_score": round(combined, 3),
            })

        scored.sort(key=lambda x: x["combined_score"], reverse=True)
        top_hits = scored[:5]

        best   = top_hits[0]["combined_score"] if top_hits else 0.0
        symbol = "✓" if top_hits else "✗"
        print(f"    {symbol} '{query[:55]}'  →  {len(top_hits)} match(es)  best={best:.2f}")

        results.append({
            "query":              query,
            "dom_elements_found": top_hits,
            "requirements_found": [
                {"text": h["text"][:100], "score": round(h["score"], 3)}
                for h in req_hits
            ],
            "match_count": len(top_hits),
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# Drift analysis  (unchanged from v1 except receives all_texts not raw strings)
# ══════════════════════════════════════════════════════════════════════════════

def identify_drift_dynamic(
    dom_data: Dict,
    req_payloads: List[Dict],
    inbox_issues: List[Dict],
) -> List[Dict]:
    print("\n  DRIFT ANALYSIS")

    if dom_data.get("qa_summary", {}).get("overlay_present"):
        print("  ⚠ Overlay detected — may affect interaction reliability")

    all_dom_text = " ".join(
        (el.get("text") or "") + " " +
        (el.get("placeholder") or "") + " " +
        (el.get("label") or "") + " " +
        (el.get("className") or "")
        for el in dom_data.get("all_interactive_elements", [])
    ).lower()

    stop = {"a","an","the","of","in","on","to","for","and","or",
            "is","as","be","with","that","this","it","at","by"}

    concepts = []
    for issue in inbox_issues[:15]:
        summ  = issue.get("summary", "")
        if summ:
            words = [w.lower() for w in re.findall(r'\w+', summ)
                     if w.lower() not in stop and len(w) > 3]
            if words:
                concepts.append({
                    "requirement": summ[:80],
                    "keywords":    words[:5],
                    "source":      issue.get("key", "inbox"),
                })

    # Use all texts for drift (including process — it's still a valid requirement)
    all_texts = [t for t in _all_texts(req_payloads) if not _is_generic_requirement_text(t)]
    for text in all_texts[:15]:
        clean = _strip_preamble(text)
        first = re.split(r'[.!\n]', clean)[0].strip()[:80]
        if first and not any(c["requirement"] == first for c in concepts):
            words = [w.lower() for w in re.findall(r'\w+', first)
                     if w.lower() not in stop and len(w) > 3]
            if words:
                concepts.append({"requirement": first, "keywords": words[:5], "source": "qdrant"})

    drift_items = []
    for concept in concepts:
        found  = any(kw in all_dom_text for kw in concept["keywords"])
        status = "PRESENT" if found else "MISSING"
        print(f"    {'✓' if found else '✗ DRIFT'}: {concept['requirement'][:65]}")
        drift_items.append({
            "requirement": concept["requirement"],
            "keywords":    concept["keywords"],
            "source":      concept["source"],
            "status":      status,
        })
    return drift_items


# ══════════════════════════════════════════════════════════════════════════════
# Validation with confidence scoring  (v2)
# ══════════════════════════════════════════════════════════════════════════════

def validate_ui_alignment_dynamic(
    dom_data: Dict,
    req_payloads: List[Dict],
    inbox_issues: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """
    Score each DOM element's alignment against known requirements.

    v2 change: only DOM-relevant texts are used for scoring; process records
    are excluded so "Activities Execute full test suite…" never becomes the
    best match for a DOM element like "Submit" or "Full Name".
    """
    print("\n  VALIDATION WITH CONFIDENCE SCORING  (v2)")

    requirements: List[str] = []
    seen_reqs: set = set()

    for issue in inbox_issues[:20]:
        summ = _strip_preamble(issue.get("summary") or "")
        if summ and summ not in seen_reqs:
            seen_reqs.add(summ)
            requirements.append(summ)

    # Use DOM-relevant texts only for confidence scoring
    dom_texts = _texts_for_validation(req_payloads)
    for t in dom_texts[:20]:
        clean = _strip_preamble(t)
        short = clean[:120]
        if short and short not in seen_reqs:
            seen_reqs.add(short)
            requirements.append(short)

    if not requirements:
        requirements = ["form", "input", "button", "submit", "navigation"]

    elements: List[Dict] = (
        dom_data.get("input_elements",    []) +
        dom_data.get("button_elements",   []) +
        dom_data.get("dropdown_elements", []) +
        dom_data.get("textarea_elements", [])
    )

    page_url = (
        dom_data.get("url") or
        dom_data.get("page_url") or
        dom_data.get("base_url") or ""
    )

    validation_results: List[Dict] = []
    self_healing_items: List[Dict] = []

    for el in elements:
        label = (
            el.get("label")       or
            el.get("text")        or
            el.get("placeholder") or
            el.get("ariaLabel")   or
            el.get("name")        or
            ""
        ).strip()

        if not label:
            continue

        el_tokens  = normalize_text(build_element_text(el, page_url))
        best_score = 0.0
        best_req   = ""

        for req in requirements:
            req_tokens = extract_requirement_keywords(req)
            score      = compute_similarity(req_tokens, el_tokens)
            if score > best_score:
                best_score = score
                best_req   = req

        if best_score >= 0.65:
            level = "HIGH"
        elif best_score >= 0.35:
            level = "MEDIUM"
        else:
            level = "LOW"

        if el.get("qa_status") == "RISKY":
            best_score *= 0.7
            level = ("HIGH" if best_score >= 0.65
                     else "MEDIUM" if best_score >= 0.35
                     else "LOW")

        symbol    = "✓ HIGH" if level == "HIGH" else ("⚠ MEDIUM" if level == "MEDIUM" else "✗ LOW")
        risky_tag = " [RISKY]" if el.get("qa_status") == "RISKY" else ""
        print(f"    {symbol} ({best_score:.2f}): '{label[:40]}' → '{best_req[:45]}'{risky_tag}")

        result = {
            "element_label":    label,
            "best_requirement": best_req,
            "confidence_data":  {
                "composite_score":    round(best_score, 3),
                "confidence_level":   level,
                "needs_self_healing": best_score < 0.35,
            },
            "element": el,
        }
        validation_results.append(result)

        if best_score < 0.35:
            self_healing_items.append({
                "label":            label,
                "requirement":      best_req,
                "confidence_score": best_score,
                "recommendations": [
                    f"Label '{label}' has low alignment with any known requirement.",
                    "Verify requirement wording vs UI label — add a synonym mapping if needed.",
                    "Enrich DOM metadata: ensure name/placeholder/ariaLabel are populated.",
                ],
            })

    return validation_results, self_healing_items


# ══════════════════════════════════════════════════════════════════════════════
# Gherkin generation  (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

_GHERKIN_SYSTEM = """You are a senior QA engineer and BDD specialist.
Your task: write a complete, detailed Gherkin feature file from the
requirements and UI context provided.

Rules:
1. Use standard Gherkin keywords: Feature, Background, Scenario,
   Scenario Outline, Given, When, Then, And, But, Examples.
2. Cover EVERY acceptance criterion with at least one scenario.
3. PRIORITISE negative and edge-case scenarios — these are the primary test
   concern. Happy-path scenarios are secondary. For each acceptance criterion,
   write at least one negative scenario with a concrete invalid input value
   and the expected rejection behaviour.
4. Use concrete, realistic test data values (never placeholders like
   <value> unless inside a Scenario Outline Examples table).
5. Tag every scenario with the project key and relevant labels,
   e.g. @SCRUM-86 @positive @smoke  or  @SCRUM-86 @negative @boundary.
6. Steps must use natural language that maps cleanly to
   basePage.smartAction() calls — no site-specific CSS selectors.
7. Background steps must only contain navigation/setup that is identical
   for EVERY scenario in the feature.
8. Output ONLY the raw Gherkin text — no markdown fences, no commentary.
9. UNIQUE SCENARIO TITLES — this is critical:
   - Every Scenario and every expanded Scenario Outline row MUST have a
     UNIQUE title within the feature.
   - For Scenario Outlines, include the key column value as a <placeholder>
     IN the Outline title so each expanded row gets a distinct title.
   - If all rows would produce the same title, write separate Scenarios.
   - Duplicate titles cause an immediate Playwright crash — avoid them.
10. QA SIGNAL RULES — apply these when the QA Analysis section is present:
   - If an element is not visible, include a scroll step before interacting.
   - If an element is obstructed or an overlay is detected, include an
     overlay-dismissal step before the interaction.
   - If qa_status=RISKY, include a retry or fallback step / @flaky tag.
   - Prefer resilient actions over naive clicks.
11. ATOMIC STEP RULES — mandatory:
   - Each step must represent exactly one UI action.
   - Each step must map to exactly one DOM element interaction.
   - Use explicit element names such as Username field, Password field,
     Login button, error message.
   - Never combine field entry and button click in one step.
   - Never write vague steps like "I perform login" or "I verify system behavior".
12. DOM GROUNDING RULES — mandatory:
   - Use only fields, buttons, and page names that appear in the DOM section.
   - Do not invent unsupported fields such as Username, Salary, Password, or overlay-dismiss steps unless they appear in the DOM section.
   - Prefer the exact names shown in the DOM section, for example Full Name, Email, Current Address, Permanent Address, Submit.
13. FILE STRUCTURE RULES — mandatory:
   - Output exactly ONE Feature block in the entire response.
   - Do not output a second Feature section for another module/page.
   - Do not include explanations, notes, apologies, or any prose after the Gherkin.
14. GOOD examples:
   - "When I enter 'Jane Doe' in the Full Name field"
   - "And I enter 'jane@example.com' in the Email field"
   - "And I click the Submit button"
   - "Then I should see the submitted output section"
15. BAD examples:
   - "When I submit login with username and password"
   - "When I perform login"
   - "Then I verify system behavior"."""


def build_qa_signals_block(dom_data: Dict) -> str:
    qa_analysis = dom_data.get("qa_analysis", [])
    qa_summary  = dom_data.get("qa_summary", {})
    overlays    = dom_data.get("overlays_detected", [])
    if not qa_analysis and not qa_summary and not overlays:
        return ""
    lines = ["=== QA ANALYSIS (from dom_capture.py) ===", "Summary:"]
    lines.append(f"  total_elements : {qa_summary.get('total_elements', 'N/A')}")
    lines.append(f"  risky_elements : {qa_summary.get('risky_elements', 'N/A')}")
    lines.append(f"  overlay_present: {qa_summary.get('overlay_present', False)}")
    if overlays:
        lines.append(f"\nOverlays detected ({len(overlays)}):")
        for ov in overlays[:5]:
            lines.append(f"  - {str(ov)[:120]}")
    if qa_analysis:
        lines.append(f"\nSample elements (top {min(10, len(qa_analysis))}):")
        for el in qa_analysis[:10]:
            lines.append(f"  {str(el)[:160]}")
    return "\n".join(lines)


def build_gherkin_prompt(
    all_req_texts: List[str],
    inbox_issues: List[Dict],
    prd_text: str,
    dom_summary: str,
    app_url: str,
    dom_data: Optional[Dict] = None,
    jira_story: Optional[Dict] = None,   # ← ADD
) -> str:

    # ── Requirements block ─────────────────────────────────────────────────
    req_block = ""
    if inbox_issues:
        lines = []
        for issue in inbox_issues[:20]:
            key  = issue.get("key", "")
            summ = issue.get("summary", issue.get("title", ""))
            desc = (issue.get("description") or "")[:500]
            ac   = (issue.get("acceptance_criteria") or
                    issue.get("acceptanceCriteria") or "")[:500]
            lines.append(f"Issue {key}: {summ}")
            if desc: lines.append(f"  Description: {desc}")
            if ac:   lines.append(f"  Acceptance Criteria:\n{ac}")
        req_block = "=== Jira Issues (from jira_sync_agent.py) ===\n" + "\n".join(lines)
    elif all_req_texts:
        req_block = ("=== Requirements (from Qdrant) ===\n" +
                     "\n".join(f"- {t[:200]}" for t in all_req_texts[:30]))

    if prd_text:
        #req_block += f"\n\n=== PRD (from vectorize_and_upload.py) ===\n{prd_text[:3000]}"
        # AFTER — increase limit and prioritise acceptance criteria sections
        prd_relevant = prd_text
        # Try to find and prioritise acceptance criteria section
        ac_idx = prd_text.lower().find("acceptance criteria")
        if ac_idx > 0:
            prd_relevant = prd_text[max(0, ac_idx - 200):]  # start from AC section
        req_block += f"\n\n=== PRD (from vectorize_and_upload.py) ===\n{prd_relevant[:5000]}"

    if not req_block.strip():
        req_block = ("No structured requirements found. "
                     "Generate general smoke-test scenarios based on the DOM below.")
        
    # ── Jira story block — highest priority, added first ──────────────────
    # This preserves AC1/AC2/AC3 structure that gets lost in Qdrant chunking
    jira_block = ""
    if jira_story:
        jira_block = f"=== Jira Story (source of truth) ===\n"
        jira_block += f"Key: {jira_story.get('key', '')}\n"
        jira_block += f"Summary: {jira_story.get('summary', '')}\n"
        if jira_story.get("description"):
            jira_block += f"\nDescription:\n{jira_story['description'][:1000]}\n"
        if jira_story.get("acceptance_criteria"):
            jira_block += f"\nAcceptance Criteria (MUST be covered by scenarios):\n"
            jira_block += jira_story["acceptance_criteria"][:2000]
        jira_block += "\n"

    # ── DOM block ──────────────────────────────────────────────────────────
    dom_block = (f"=== Live DOM Elements (from dom_capture.py) ===\n"
                 f"{dom_summary}")

    # ── QA signals block ───────────────────────────────────────────────────
    qa_block = build_qa_signals_block(dom_data) if dom_data else ""

    # ── Collect all captured page URLs dynamically from dom_data ───────────
    # Works for any site — reads whatever pages dom_capture stored,
    # whether that is 1 page or 20, without any hardcoding.
    all_page_urls: List[str] = []
    if dom_data:
        seen_pages: set = set()
        for group_key in ["input_elements", "button_elements",
                          "textarea_elements", "dropdown_elements",
                          "all_interactive_elements"]:
            for el in dom_data.get(group_key, []):
                pu = (el.get("page_url") or "").strip()
                if pu and pu not in seen_pages:
                    seen_pages.add(pu)
                    all_page_urls.append(pu)
    if not all_page_urls:
        all_page_urls = [app_url]

    pages_block = "\n".join(f"  - {u}" for u in sorted(all_page_urls))

    # ── Assemble final prompt ──────────────────────────────────────────────
    return (
        f"Generate a complete Gherkin feature file for project {PROJECT_KEY}.\n\n"
        +(f"{jira_block}\n" if jira_block else "")   # ← ADD — Jira ACs first
        +f"{req_block}\n\n"

        +f"{dom_block}\n\n"

        + (f"{qa_block}\n\n" if qa_block else "")

        + "=== Pages available in this application ===\n"
        f"Each scenario MUST start with 'Given I am on the \"<url>\" page'\n"
        f"using the exact URL from the list below where the elements for that\n"
        f"scenario live. Do NOT put all scenarios on the same page.\n"
        f"{pages_block}\n\n"

        f"Project key for tags: {PROJECT_KEY}\n\n"

        "CRITICAL: Every scenario MUST open with a concrete page navigation step:\n"
        "  Given I am on the \"<exact url from list above>\" page\n\n"

        "CRITICAL: Every step must be atomic — one UI action per step.\n"
        "CRITICAL: Split field entry, button clicks, and assertions into separate steps.\n"
        "CRITICAL: Use human-readable element names from the DOM section:\n"
        "  e.g. 'Full Name field', 'Email field', 'Submit button', 'Age field'.\n"
        "CRITICAL: Do NOT use CSS selectors like '#userEmail' in step text.\n"
        "CRITICAL: Only reference fields and buttons that appear in the DOM section.\n"
        "  Do not invent elements absent from the DOM.\n\n"

        "CRITICAL: Output exactly ONE Feature block — no second Feature section,\n"
        "  no markdown fences, no commentary after the Gherkin.\n\n"

        "CRITICAL: Requirements describe NEGATIVE PATH validation — prioritise these scenario types:\n"
        "  1. Invalid input → validation error shown, output NOT rendered\n"
        "  2. Valid input → output IS rendered, no error\n"
        "  3. Disabled element → no state change occurs\n"
        "  4. Use Scenario Outline with Examples table for multiple invalid values.\n\n"

        f"REMINDER: Every Scenario and every Scenario Outline expansion row\n"
        f"MUST have a unique title (Rule 9 of the system prompt).\n\n"

        "Write the feature file now:"
    )


def _is_step_line(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r'^(Given|When|Then|And|But)\b', stripped))


def _dedupe_scenario_titles(lines: List[str]) -> List[str]:
    title_counts: Dict[str, int] = {}
    deduped: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Scenario Outline:") or stripped.startswith("Scenario:"):
            prefix, _, name = stripped.partition(":")
            scenario_name = name.strip()
            seen = title_counts.get(scenario_name, 0)
            title_counts[scenario_name] = seen + 1
            if seen:
                scenario_name = f"{scenario_name} [{seen + 1}]"
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}{prefix}: {scenario_name}"
        deduped.append(line)
    return deduped


def _split_atomic_step_lines(line: str) -> List[str]:
    stripped = line.strip()
    indent = line[: len(line) - len(line.lstrip())]
    match = re.match(
        r'^(Given|When|Then|And|But)\s+I enter\s+"([^"]+)"\s+in the\s+(.+?)\s+and\s+"([^"]+)"\s+in the\s+(.+)$',
        stripped,
        re.IGNORECASE,
    )
    if match:
        keyword, first_value, first_target, second_value, second_target = match.groups()
        return [
            f'{indent}{keyword} I enter "{first_value}" in the {first_target.strip()}',
            f'{indent}And I enter "{second_value}" in the {second_target.strip()}',
        ]
    return [line]


def _normalize_gherkin_blocks(lines: List[str]) -> List[str]:
    normalized: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("Scenario Outline:") or stripped.startswith("Scenario:"):
            scenario_lines = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(("Scenario:", "Scenario Outline:", "Feature:")):
                scenario_lines.append(lines[i])
                i += 1

            has_placeholders = any("<" in candidate and ">" in candidate for candidate in scenario_lines if _is_step_line(candidate))
            has_steps = any(_is_step_line(candidate) for candidate in scenario_lines)

            if scenario_lines[0].strip().startswith("Scenario Outline:") and not has_placeholders:
                scenario_lines[0] = scenario_lines[0].replace("Scenario Outline:", "Scenario:", 1)
                filtered: List[str] = []
                in_examples = False
                for candidate in scenario_lines:
                    cand_stripped = candidate.strip()
                    if cand_stripped.startswith("Examples:"):
                        in_examples = True
                        continue
                    if in_examples:
                        if cand_stripped.startswith("|") or cand_stripped == "":
                            continue
                        in_examples = False
                    filtered.append(candidate)
                scenario_lines = filtered
                has_steps = any(_is_step_line(candidate) for candidate in scenario_lines)

            if has_steps:
                for candidate in scenario_lines:
                    normalized.extend(_split_atomic_step_lines(candidate))
            continue

        normalized.extend(_split_atomic_step_lines(line))
        i += 1

    return normalized


def _scenario_mentions_other_page(scenario_lines: List[str], feature_name: str) -> bool:
    feature_lower = feature_name.lower()
    scenario_text = " ".join(line.strip().lower() for line in scenario_lines)

    page_markers = {
        "text box": ["web tables", "radio button", "buttons", "upload and download"],
        "web tables": ["text box", "radio button", "buttons", "upload and download"],
        "radio button": ["text box", "web tables", "buttons", "upload and download"],
    }
    for page_name, forbidden in page_markers.items():
        if page_name in feature_lower:
            return any(marker in scenario_text for marker in forbidden)
    return False


def _filter_page_incoherent_scenarios(lines: List[str]) -> List[str]:
    feature_name = ""
    filtered: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("Feature:"):
            feature_name = stripped.split(":", 1)[1].strip()
            filtered.append(line)
            i += 1
            continue

        if stripped.startswith("Scenario:") or stripped.startswith("Scenario Outline:"):
            block = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(("Scenario:", "Scenario Outline:", "Feature:")):
                block.append(lines[i])
                i += 1
            if not _scenario_mentions_other_page(block, feature_name):
                filtered.extend(block)
            continue

        filtered.append(line)
        i += 1

    return filtered


def _dom_supported_terms(dom_data: Optional[Dict]) -> Dict[str, Any]:
    dom_data = dom_data or {}
    primary_page = _dominant_form_page_url(dom_data)

    def on_primary_page(el: Dict) -> bool:
        return True   # accept elements from any captured page

    fields = set()
    buttons = set()
    free_text = set()

    for key in ("input_elements", "textarea_elements", "dropdown_elements"):
        for el in dom_data.get(key, []):
            if not on_primary_page(el):
                continue
            for value in (
                el.get("label"),
                el.get("placeholder"),
                el.get("name"),
                el.get("text"),
                el.get("id"),
            ):
                if isinstance(value, str) and value.strip():
                    fields.add(value.strip().lower())

    for el in dom_data.get("button_elements", []):
        if not on_primary_page(el):
            continue
        for value in (el.get("text"), el.get("label"), el.get("name"), el.get("id")):
            if isinstance(value, str) and value.strip():
                buttons.add(value.strip().lower())

    for el in dom_data.get("all_interactive_elements", []):
        if not on_primary_page(el):
            continue
        for value in (el.get("text"), el.get("label"), el.get("placeholder"), el.get("name"), el.get("id")):
            if isinstance(value, str) and value.strip():
                free_text.add(value.strip().lower())

    overlay_present = bool(dom_data.get("qa_summary", {}).get("overlay_present", False))
    return {
        "primary_page": primary_page,
        "fields": fields,
        "buttons": buttons,
        "free_text": free_text,
        "overlay_present": overlay_present,
    }


def _scenario_supported_by_dom(scenario_lines: List[str], dom_data: Optional[Dict]) -> bool:
    support = _dom_supported_terms(dom_data)
    fields = support["fields"]
    buttons = support["buttons"]
    free_text = support["free_text"]
    overlay_present = support["overlay_present"]

    text_blob = " ".join(line.strip().lower() for line in scenario_lines)
    if "overlay" in text_blob and not overlay_present and "overlay" not in free_text:
        return False
    if "lazy loading" in text_blob or "lazy-loading" in text_blob:
        return False

    for line in scenario_lines:
        stripped = line.strip()
        if not _is_step_line(line):
            continue

        field_match = re.search(r"\bin the ([a-z0-9 _-]+?) field\b", stripped, re.IGNORECASE)
        if field_match:
            field_name = field_match.group(1).strip().lower()
            if field_name and field_name not in fields:
                return False

        button_match = re.search(r"\bclick the ([a-z0-9 _-]+?) button\b", stripped, re.IGNORECASE)
        if button_match:
            button_name = button_match.group(1).strip().lower()
            if button_name and button_name not in buttons:
                return False

        page_match = re.search(r"\bnavigate to the ([a-z0-9 _-]+?) page\b", stripped, re.IGNORECASE)
        if page_match:
            page_name = page_match.group(1).strip().lower()
            primary_page = (support["primary_page"] or "").lower()
            if page_name and primary_page and page_name.replace(" ", "-") not in primary_page and page_name not in primary_page:
                return False

    return True


def _filter_dom_unsupported_scenarios(lines: List[str], dom_data: Optional[Dict]) -> List[str]:
    if not dom_data:
        return lines

    filtered: List[str] = []
    pending_tags: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("@"):
            pending_tags.append(line)
            i += 1
            continue

        if stripped.startswith("Scenario:") or stripped.startswith("Scenario Outline:"):
            block = pending_tags + [line]
            pending_tags = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(("Scenario:", "Scenario Outline:", "Feature:", "@")):
                block.append(lines[i])
                i += 1
            if _scenario_supported_by_dom(block, dom_data):
                filtered.extend(block)
            continue

        if pending_tags:
            filtered.extend(pending_tags)
            pending_tags = []
        filtered.append(line)
        i += 1

    if pending_tags:
        filtered.extend(pending_tags)
    return filtered


def _sanitize_gherkin_structure(raw: str, dom_data: Optional[Dict] = None) -> str:
    allowed_prefixes = (
        "@", "Feature:", "Background:", "Scenario:", "Scenario Outline:",
        "Examples:", "|",
    )
    feature_seen = False
    background_seen = False
    in_examples = False
    collecting_description = False
    lines_out: List[str] = []

    for original_line in raw.splitlines():
        line = original_line.rstrip()
        stripped = line.strip()

        if not stripped:
            if lines_out and lines_out[-1] != "":
                lines_out.append("")
            continue

        if stripped.startswith("Feature:"):
            if feature_seen:
                collecting_description = False
                in_examples = False
                continue
            feature_seen = True
            background_seen = False
            collecting_description = True
            in_examples = False
            lines_out.append(stripped)
            continue

        if not feature_seen:
            continue

        if stripped.startswith("Background:"):
            if background_seen:
                collecting_description = False
                in_examples = False
                continue
            background_seen = True
            collecting_description = False
            in_examples = False
            lines_out.append(line)
            continue

        if stripped.startswith("Scenario Outline:") or stripped.startswith("Scenario:"):
            collecting_description = False
            in_examples = False
            lines_out.append(line)
            continue

        if stripped.startswith("Examples:"):
            collecting_description = False
            in_examples = True
            lines_out.append(line)
            continue

        if stripped.startswith("@"):
            collecting_description = False
            lines_out.append(line)
            continue

        if stripped.startswith("|"):
            if in_examples:
                lines_out.append(line)
            continue

        if _is_step_line(line):
            collecting_description = False
            lines_out.append(line)
            continue

        if collecting_description and line.startswith("  "):
            lines_out.append(line)
            continue

        if stripped.startswith("Please note") or stripped.startswith("Note:"):
            break

        if any(stripped.startswith(prefix) for prefix in allowed_prefixes):
            lines_out.append(line)
            continue

    # Trim repeated blank lines
    compact: List[str] = []
    for line in lines_out:
        if line == "" and compact and compact[-1] == "":
            continue
        compact.append(line)

    normalized = _normalize_gherkin_blocks(compact)
    normalized = _filter_page_incoherent_scenarios(normalized)
    normalized = _filter_dom_unsupported_scenarios(normalized, dom_data)
    return "\n".join(_dedupe_scenario_titles(normalized)).strip()


def parse_gherkin_output(raw: str, dom_data: Optional[Dict] = None) -> str:
    raw = re.sub(r'^```[a-z]*\n?', '', raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r'\n?```$',       '', raw.strip(), flags=re.MULTILINE)
    idx = raw.find("Feature:")
    if idx > 0:
        raw = raw[idx:]
    return _sanitize_gherkin_structure(raw, dom_data=dom_data)


def generate_gherkin_via_llm(
    req_payloads: List[Dict],
    inbox_issues: List[Dict],
    prd_text: str,
    dom_data: Dict,
    jira_story: Optional[Dict] = None,   # ← ADD
) -> str:
    app_url = _dominant_form_page_url(dom_data)
    if not app_url:
        app_url = ((BASE_URL + "/") if BASE_URL else
                   os.getenv("BASE_URL", "https://your-app.example.com/"))
    dom_summary  = dom_summary_for_llm(dom_data)
    all_req_texts = _all_texts(req_payloads)

    prompt = build_gherkin_prompt(
        all_req_texts, inbox_issues, prd_text, dom_summary, app_url,
        dom_data=dom_data,
        jira_story=jira_story)

    default_gateway = get_llm_gateway()
    provider = default_gateway.resolve_provider_for_agent(
        "quality_alignment_v1",
        purpose="chat",
        fallback_provider=os.getenv("LLM_PROVIDER", "ollama"),
    )
    print(f"  Calling LLM provider '{provider}' to write Gherkin…")

    gateway = get_llm_gateway(provider=provider)
    model_override = gateway.resolve_model_for_agent(
        "quality_alignment_v1",
        purpose="chat",
        fallback_model=os.getenv("CHAT_MODEL", "llama3:8b"),
    )

    # BUG FIX: the original call had no explicit timeout argument, so it
    # inherited the gateway default of 120s.  Generating a 10-scenario Gherkin
    # file locally takes 2–4 minutes on modest hardware.  We now try up to 2
    # times with a 300s timeout before falling back.
    raw = ""
    for attempt in range(1, 3):
        try:
            raw = gateway.chat(
                prompt,
                system_prompt=_GHERKIN_SYSTEM,
                model_override=model_override,
                temperature=0.3,
                timeout=300,          # 5-minute ceiling per attempt
            )
            if raw and raw.strip():
                break
            print(f"  ⚠ LLM returned empty response (attempt {attempt}/2)")
        except Exception as exc:
            print(f"  ⚠ LLM call failed (attempt {attempt}/2): {exc}")
            if attempt == 2:
                raw = ""

    if not raw or not raw.strip():
        print("  ⚠ LLM returned empty response — using structural fallback")
        return _gherkin_fallback(all_req_texts, inbox_issues, app_url, dom_data=dom_data)

    gherkin = parse_gherkin_output(raw, dom_data=dom_data)

    if "Scenario" not in gherkin:
        print("  ⚠ LLM output does not look like Gherkin — using fallback")
        return _gherkin_fallback(all_req_texts, inbox_issues, app_url, dom_data=dom_data)

    count = len(re.findall(r'^\s*Scenario', gherkin, re.MULTILINE))
    print(f"  ✓ LLM generated {count} scenario(s)")
    return gherkin


def _gherkin_fallback(
    all_req_texts: List[str],
    inbox_issues: List[Dict],
    app_url: str,
    dom_data: Optional[Dict] = None,
) -> str:
    """
    Fallback Gherkin generator used when the LLM times out or returns empty.

    BUG FIX: the original fallback produced unexecutable placeholder steps:
        Given the system is in the correct initial state
        When  I perform the action described in SCRUM-70
        Then  the expected outcome should occur

    smartAction has no element to click for "correct initial state" or
    "perform the action described in SCRUM-70", so every test immediately
    fails with "smartAction failed: no element found for intent...".

    The replacement generates real, executable steps derived from:
      1. The app URL          → a concrete page.goto() navigation step
      2. DOM keyword signals  → text-level interaction steps the page actually has
      3. Requirement summaries → requirement-scoped assertion steps

    These steps are conservative (navigate → assert visibility) so they
    succeed on a real page without needing a fully working smartAction.
    """
    title = (inbox_issues[0].get("summary", f"{PROJECT_KEY} Acceptance Tests")
             if inbox_issues else f"{PROJECT_KEY} Acceptance Tests")

    lines = [
        f"Feature: {PROJECT_KEY}: {title}",
        f"  As a user of {app_url}",
        f"  I want to fulfil the acceptance criteria for {PROJECT_KEY}",
        "",
        "  Background:",
        f'    Given I navigate to "{app_url}"',
        "",
    ]

    dom_data = dom_data or {}
    primary_page = _dominant_form_page_url(dom_data)

    def on_primary_page(el: Dict) -> bool:
        page_url = (el.get("page_url") or "").strip()
        return not primary_page or page_url == primary_page

    input_elements = [el for el in dom_data.get("input_elements", []) if on_primary_page(el)]
    textarea_elements = [el for el in dom_data.get("textarea_elements", []) if on_primary_page(el)]
    button_elements = [el for el in dom_data.get("button_elements", []) if on_primary_page(el)]

    field_names: List[str] = []
    for el in input_elements + textarea_elements:
        for candidate in (el.get("label"), el.get("placeholder"), el.get("name"), el.get("id")):
            if isinstance(candidate, str) and candidate.strip():
                value = candidate.strip()
                if value not in field_names:
                    field_names.append(value)
                break

    button_name = ""
    for el in button_elements:
        for candidate in (el.get("text"), el.get("label"), el.get("name"), el.get("id")):
            if isinstance(candidate, str) and candidate.strip():
                button_name = candidate.strip()
                break
        if button_name:
            break

    if field_names or button_name:
        lines += [
            f"  @{PROJECT_KEY} @fallback @smoke",
            "  Scenario: Primary page controls are visible",
            f'    Given I navigate to "{app_url}"',
        ]
        for field in field_names[:4]:
            lines.append(f'    Then I should see the {field} field')
        if button_name:
            lines.append(f'    And I should see the {button_name} button')
        lines.append("")

        if len(field_names) >= 2 and button_name:
            first_field = field_names[0]
            second_field = field_names[1]
            sample_first = "Jane Doe" if "name" in first_field.lower() else "sample value"
            sample_second = "jane@example.com" if "email" in second_field.lower() else "sample value"
            lines += [
                f"  @{PROJECT_KEY} @fallback @smoke",
                "  Scenario: Primary form accepts sample input",
                f'    Given I navigate to "{app_url}"',
                f"    And I enter '{sample_first}' in the {first_field} field",
                f"    And I enter '{sample_second}' in the {second_field} field",
            ]
            for field in field_names[2:4]:
                sample = "sample text"
                lines.append(f"    And I enter '{sample}' in the {field} field")
            lines.append(f"    When I click the {button_name} button")
            lines.append("")
    else:
        lines += [
            f"  @{PROJECT_KEY} @fallback",
            "  Scenario: Primary page is reachable",
            f'    Given I navigate to "{app_url}"',
            "    Then I should see the page content",
            "",
        ]

    print("  ⚠ LLM fallback used — re-run after fixing Ollama timeout to get "
          "full BDD scenarios.  Set OLLAMA_READ_TIMEOUT=300 in your .env.")
    return "\n".join(lines)


def save_feature_file(gherkin: str) -> str:
    features_dir = os.path.join("tests", "features")
    os.makedirs(features_dir, exist_ok=True)
    path = os.path.join(features_dir, f"{RAW_PROJECT_KEY}.feature")
    with open(path, "w") as f:
        f.write(gherkin)
    print(f"  ✓ Feature file saved → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Selector coverage  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def check_selectors_coverage(dom_data: Dict) -> List[Dict]:
    if not os.path.exists(SELECTORS_CSV):
        return []
    all_dom_text = " ".join(str(el) for el in dom_data.get("all_interactive_elements", [])).lower()
    results = []
    with open(SELECTORS_CSV) as f:
        for row in csv.DictReader(f):
            sel   = row.get("selector", "")
            found = sel.lower() in all_dom_text if sel else False
            results.append({
                "field":    row.get("field", ""),
                "selector": sel,
                "notes":    row.get("notes", ""),
                "found":    found,
            })
            print(f"    {'✓' if found else '✗'}: {row.get('field','')} = '{sel}'")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def save_report(
    cross_ref: List[Dict], drift: List[Dict], validation: List[Dict],
    self_healing: List[Dict], coverage: List[Dict], feature_path: str,
    dom_data: Optional[Dict] = None,
    gherkin_coverage: Optional[Dict] = None,
) -> str:
    dom_data         = dom_data or {}
    gherkin_coverage = gherkin_coverage or {}
    gc_summary       = gherkin_coverage.get("summary", {})

    report = {
        "project_key":  PROJECT_KEY,
        "generated_at": datetime.now().isoformat(),
        "feature_file": feature_path,
        "summary": {
            # Gherkin coverage (from gherkin_agent)
            "gherkin_ac_coverage_pct":    gc_summary.get("ac_coverage_pct", 0),
            "gherkin_signal_coverage_pct": gc_summary.get("signal_coverage_pct", 0),
            "gherkin_scenarios_written":  gc_summary.get("scenarios_written", 0),
            "gherkin_acs_covered":        gc_summary.get("acs_covered", 0),
            "gherkin_acs_missing":        gc_summary.get("acs_missing", 0),
            "gherkin_todo_placeholders":  gc_summary.get("todo_placeholders", 0),
            # DOM alignment (from quality_alignment)
            "cross_reference_queries":    len(cross_ref),
            "requirements_checked":       len(drift),
            "requirements_met":           sum(1 for d in drift if d["status"] == "PRESENT"),
            "requirements_missing":       sum(1 for d in drift if d["status"] == "MISSING"),
            "validations_total":          len(validation),
            "high_confidence":            sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "HIGH"),
            "medium_confidence":          sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "MEDIUM"),
            "low_confidence":             sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "LOW"),
            "self_healing_needed":        len(self_healing),
            "selectors_checked":          len(coverage),
            "selectors_found":            sum(1 for c in coverage if c["found"]),
            "risky_ui_elements":          len(dom_data.get("qa_analysis", [])),
            "overlay_present":            dom_data.get("qa_summary", {}).get("overlay_present", False),
        },
        "gherkin_coverage":  gherkin_coverage,
        "cross_reference":   cross_ref,
        "drift_analysis":    drift,
        "validation":        validation,
        "self_healing":      self_healing,
        "selector_coverage": coverage,
    }

    path = os.path.join(DOCS_DIR, f"quality_alignment_report_{RAW_PROJECT_KEY}.json")
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  ✓ Report saved → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global PROJECT_KEY, RAW_PROJECT_KEY, REQUIREMENTS_COLLECTION, DOM_COLLECTION

    parser = argparse.ArgumentParser(
        description="Quality Alignment — generate LLM Gherkin from pipeline outputs"
    )
    parser.add_argument("--project", required=True,
                        help="Project key in original form, e.g. SCRUM-70")
    args = parser.parse_args()

    # PROJECT_KEY stays in its original hyphenated form — used in payloads/filters.
    RAW_PROJECT_KEY         = args.project
    PROJECT_KEY             = args.project          # same — no normalisation
    REQUIREMENTS_COLLECTION = collection_name_for(PROJECT_KEY, "requirements")
    DOM_COLLECTION          = collection_name_for(PROJECT_KEY, "ui_memory")

    print("=" * 60)
    print("Phase 3: Quality Alignment  (v2)")
    print("=" * 60)
    print(f"  Project key (raw)  : {PROJECT_KEY}  ← used as filter value")
    print(f"  Requirements coll  : {REQUIREMENTS_COLLECTION}  ← sanitised for Qdrant")
    print(f"  DOM coll           : {DOM_COLLECTION}  ← sanitised for Qdrant")
    resolved_provider = get_llm_gateway().resolve_provider_for_agent(
        "quality_alignment_v1",
        purpose="chat",
        fallback_provider=os.getenv("LLM_PROVIDER", "ollama"),
    )
    print(f"  LLM provider       : {resolved_provider}")

    # [1/7] Load requirements
    print("\n[1/7] Loading Phase 0/1 outputs (requirements)…")
    req_payloads = load_requirements_from_qdrant()   # list of payload dicts
    inbox_issues = load_requirements_from_inbox()
    prd_text     = load_prd_text()
    jira_story    = load_jira_story_structured()

    # Show content_type breakdown for transparency
    from collections import Counter
    ct_counts = Counter(p.get("content_type", "general") for p in req_payloads)
    print(f"  Content type breakdown: {dict(ct_counts)}")
    print(f"  → DOM-matching will use: "
          f"{len(_texts_for_dom_matching(req_payloads))} records  "
          f"(process records excluded from DOM queries)")

    if not req_payloads and not inbox_issues:
        print("\n  ⚠ No requirements found. Gherkin will be generated from DOM only.")

    # [2/7] Load DOM
    print("\n[2/7] Loading Phase 2 outputs (DOM)…")
    dom_data = load_dom_data()

    # [3/7] DOM vectorisation already done by vectorize_and_upload
    if dom_data:
        print("\n[3/7] DOM vectorisation already handled in previous step — skipping")
    else:
        print("\n[3/7] Skipping DOM vectorisation (no DOM data)")

    # [4/7] Gherkin
    print("\n[4/7] Generating Gherkin feature file via LLM…")
    # gherkin = generate_gherkin_via_llm(
    #     req_payloads, inbox_issues, prd_text, dom_data,
    #     jira_story=jira_story   # ← ADD THIS
    # )
    from gherkin_agent import run_gherkin_agent
    gherkin, gherkin_coverage = run_gherkin_agent(
        jira_story              = jira_story,
        dom_data                = dom_data,
        req_payloads            = req_payloads,
        project_key             = RAW_PROJECT_KEY,
        gateway                 = get_llm_gateway(),
        prd_text                = prd_text,
        inbox_issues            = inbox_issues,
        qdrant_url              = QDRANT_URL,
        dom_collection          = DOM_COLLECTION,
        requirements_collection = REQUIREMENTS_COLLECTION,
    )
    feature_path = save_feature_file(gherkin)
    print(f"  Gherkin coverage: "
          f"{gherkin_coverage['summary']['ac_coverage_pct']}% "
          f"({gherkin_coverage['summary']['acs_covered']}/"
          f"{gherkin_coverage['summary']['total_acs_extracted']} ACs)")

    # [5/7] Cross-reference
    print("\n[5/7] Cross-referencing requirements with live DOM…")
    cross_ref = cross_reference_with_requirements(req_payloads, inbox_issues)

    # [6/7] Drift + validation
    print("\n[6/7] Running drift analysis and confidence validation…")
    drift             = identify_drift_dynamic(dom_data, req_payloads, inbox_issues)
    validation, heals = validate_ui_alignment_dynamic(dom_data, req_payloads, inbox_issues)
    coverage          = check_selectors_coverage(dom_data)

    # [7/7] Report
    print("\n[7/7] Saving comprehensive report…")
    report_path = save_report(cross_ref, drift, validation, heals, coverage, feature_path,
                              dom_data=dom_data, gherkin_coverage=gherkin_coverage)

    met     = sum(1 for d in drift if d["status"] == "PRESENT")
    missing = sum(1 for d in drift if d["status"] == "MISSING")
    high    = sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "HIGH")
    gc      = gherkin_coverage.get("summary", {})

    print("\n" + "=" * 60)
    print("✓ Phase 3: Quality Alignment completed!")
    print(f"  Feature file:         {feature_path}")
    print(f"  Report:               {report_path}")
    print(f"  ── Gherkin Coverage ──────────────────")
    print(f"  AC coverage:          {gc.get('acs_covered',0)}/{gc.get('total_acs_extracted',0)} = {gc.get('ac_coverage_pct',0)}%")
    print(f"  Signal coverage:      {gc.get('scenarios_written',0)}/{gc.get('total_kb_signals',0)} = {gc.get('signal_coverage_pct',0)}%")
    print(f"  Scenarios written:    {gc.get('scenarios_written',0)} ({gc.get('scenario_outlines',0)} outlines, {gc.get('todo_placeholders',0)} TODOs)")
    print(f"  ── DOM Alignment ─────────────────────")
    print(f"  Requirements met:     {met}/{met + missing}")
    print(f"  High-conf matches:    {high}/{len(validation)}")
    print(f"  Self-healing flagged: {len(heals)}")
    if heals:
        print("\n  Self-healing items:")
        for h in heals[:5]:
            print(f"    • '{h['label'][:40]}' — {h['recommendations'][0][:60]}")
    if gc.get("missing_coverage"):
        print(f"\n  Missing AC coverage ({len(gc['missing_coverage'])}):")
        for m in gc["missing_coverage"][:5]:
            print(f"    ✗ {m['id']}: {m['title'][:50]} [{m['test_type']}]")
    print("=" * 60)


if __name__ == "__main__":
    main()