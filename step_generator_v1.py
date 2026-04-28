#!/usr/bin/env python3
"""
Step Definition Generator (TEA Architecture) — v2
==================================================
Generates Playwright TypeScript spec files from ANY Gherkin feature file.
Fully generalised — no project, URL, or selector assumptions hardcoded.

Pipeline context
────────────────
  Phase 0  jira_sync_agent.py         →  Jira stories / subtasks / epics
  Phase 1  dom_capture.py             →  docs/live_dom_elements_*.json
  Phase 2  vectorize_and_upload.py    →  Qdrant: {PROJECT}_requirements
                                          Qdrant: {PROJECT}_ui_memory
  Phase 3  quality_alignment.py       →  tests/features/{PROJECT}.feature
  Phase 4  step_generator.py (this)   →  tests/steps/{PROJECT}.spec.ts
                                          tests/steps/{PROJECT}_coverage.json

Action types resolved per step
───────────────────────────────
  navigate          → page.goto(url)
  smartFill         → locator.fill(value)  or smartAction(intent, value)
  smartClick        → locator.click()      or smartAction(intent)
  verifyText        → expect(locator).toContainText(value) / toBeVisible()
  verifyAbsent      → expect(locator).toBeHidden() / not.toBeVisible()
  verifyDisabled    → expect(locator).toBeDisabled()
  verifySelectorExists → expect(locator).toBeVisible() + toBeEnabled()
  unknown           → smartAction(intent)  [TEA semantic fallback]

BasePage interface expected by the generated TypeScript
────────────────────────────────────────────────────────
  class BasePage {
    constructor(page: Page, projectKey: string)
    async initialize(): Promise<void>
    async smartAction(intent: string, value?: string): Promise<void>
    page: Page   // direct Playwright page for locator() / goto()
  }

Key conventions
───────────────
  PROJECT_KEY stays in original hyphenated form (e.g. "PROJ-70") everywhere:
    - Qdrant filter values
    - report filenames
  Collection names are the ONLY place key is sanitised (hyphens → underscores).

Fix log v1 → v2
────────────────
  FIX 1  project_key Qdrant filter uses RAW key (not sanitised)
  FIX 2  search_qdrant / _qdrant_rest_search both use raw key filter
  FIX 3  resolve_url_from_qdrant uses raw key filter
  FIX 4  main() keeps PROJECT_KEY raw; collection names built via helper
  FIX 5  QA signals use real stored field names: visible/obstructed not is_*
  FIX 6  page_url resolution checks details.page_url (dom_capture field)
  FIX 7  Duplicate tag lines in feature file deduplicated during parse
  FIX 8  New action types: verifyAbsent, verifyDisabled, verifySelectorExists
  FIX 9  _rule_based_step_analysis extended to classify all new action types
  FIX 10 generate_typescript_step emits correct Playwright code per action type
  FIX 11 Empty-field steps ("leave X empty") handled explicitly
  FIX 12 Inline selector assertions ("selector #x exists and is enabled") resolved
  FIX 13 PROJECT_KEY passed as parameter to generate_test_file (no global dependency)
"""

from __future__ import annotations

import os
import re
import json
import argparse
import requests
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
QDRANT_URL:  str = os.getenv("QDRANT_URL",  "http://localhost:6333")
BASE_URL:    str = os.getenv("BASE_URL",    "").rstrip("/")
VECTOR_SIZE: int = 1024

from llm_gateway import get_llm_gateway  # noqa: E402

# Set by main() — always kept in original hyphenated form, e.g. "SCRUM-70".
PROJECT_KEY:             str = ""
REQUIREMENTS_COLLECTION: str = ""
DOM_COLLECTION:          str = ""

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
FEATURES_DIR = os.path.join(PROJECT_ROOT, "tests", "features")
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")
BMAD_DIR     = os.path.join(PROJECT_ROOT, "tests", "_bmad")


# ══════════════════════════════════════════════════════════════════════════════
# Naming helpers  (must mirror vectorize_and_upload_v2 / quality_alignment_v2)
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_collection_name(name: str) -> str:
    """
    Convert a project key + suffix into a valid Qdrant collection name.
    Called ONLY for collection names — never for filter values.

    "SCRUM-70_ui_memory"  →  "SCRUM_70_ui_memory"
    """
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return sanitized.strip('_') or 'collection'


def collection_name_for(project_key: str, suffix: str) -> str:
    return sanitize_collection_name(f"{project_key}_{suffix}")


# ══════════════════════════════════════════════════════════════════════════════
# §1  DATA MODEL  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class QAAdjustments:
    """
    Derived from QA signals stored in the Qdrant element payload.

    Field name alignment (FIX 5):
      dom_capture / vectorize_and_upload store:
        "visible"    (not "is_visible")
        "obstructed" (not "is_obstructed")
        "qa_status"  (ok / GOOD / RISKY / warn / fail)
    """
    needs_scroll:           bool = False
    needs_overlay_dismiss:  bool = False
    needs_retry:            bool = False
    raw_is_visible:         Optional[bool] = None
    raw_is_obstructed:      Optional[bool] = None
    raw_qa_status:          Optional[str]  = None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "QAAdjustments":
        details = payload.get("details", {}) or {}

        # FIX 5: check both the old "is_visible" names AND the real stored names
        is_visible = (
            payload.get("visible")
            or payload.get("is_visible")
            or details.get("visible")
            or details.get("is_visible")
        )
        is_obstructed = (
            payload.get("obstructed")
            or payload.get("is_obstructed")
            or details.get("obstructed")
            or details.get("is_obstructed")
        )
        qa_status = (
            payload.get("qa_status")
            or details.get("qa_status")
            or "ok"
        )

        return cls(
            needs_scroll          = is_visible is False,
            needs_overlay_dismiss = bool(is_obstructed),
            needs_retry           = str(qa_status).upper() in ("RISKY", "WARN", "FAIL", "WARNING"),
            raw_is_visible        = is_visible,
            raw_is_obstructed     = is_obstructed,
            raw_qa_status         = qa_status,
        )

    def has_any(self) -> bool:
        return self.needs_scroll or self.needs_overlay_dismiss or self.needs_retry

    def as_comment_parts(self) -> List[str]:
        parts: List[str] = []
        if self.needs_scroll:
            parts.append("QA:scroll-required")
        if self.needs_overlay_dismiss:
            parts.append("QA:overlay-may-block")
        if self.needs_retry:
            parts.append(f"QA:retry-advised(qa_status={self.raw_qa_status})")
        return parts


@dataclass
class StepMapping:
    """
    Single resolved mapping between one Gherkin step and its Playwright action.
    """
    step_text:      str
    step_keyword:   str
    action_type:    str              = "unknown"
    page_url:       str              = ""
    selector:       str              = ""
    value:          str              = ""
    confidence:     float            = 0.0
    qa_adjustments: QAAdjustments    = field(default_factory=QAAdjustments)
    reasoning:      str              = ""
    semantic_match: Dict[str, Any]   = field(default_factory=dict)
    ambiguous:      bool             = False
    warnings:       List[str]        = field(default_factory=list)

    def confidence_tier(self) -> str:
        if self.confidence >= 0.75:
            return "high"
        if self.confidence >= 0.45:
            return "medium"
        return "low"

    def to_legacy_dict(self) -> Dict[str, Any]:
        return {
            "action_type":    self.action_type,
            "intent":         self.step_text,
            "value":          self.value,
            "confidence":     self.confidence,
            "reasoning":      self.reasoning,
            "semantic_match": self.semantic_match,
            "page_url":       self.page_url,
            "selector":       self.selector,
            "qa_adjustments": asdict(self.qa_adjustments),
        }


@dataclass
class StepContext:
    """
    Carries all per-run context through the step mapping pipeline.
    Nothing is read from module globals during step processing —
    everything needed is in this context object.
    """
    current_page_url: str = ""
    project_key:      str = ""   # raw form e.g. "PROJ-70"
    dom_collection:   str = ""   # sanitised e.g. "PROJ_70_ui_memory"


def _single_quote(value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        return value
    return f"'{value}'"


def _extract_field_value(step_text: str, aliases: List[str]) -> str:
    text = step_text.strip()
    alias_pattern = "|".join(re.escape(a) for a in aliases)
    patterns = [
        rf"(?:{alias_pattern})\s*(?:field)?\s*(?:with|=|as|of|is|to)?\s*['\"]([^'\"]+)['\"]",
        rf"(?:{alias_pattern})\s*(?:field)?\s*(?:with|=|as|of|is|to)?\s*(<[^>]+>)",
        rf"['\"]([^'\"]+)['\"]\s*(?:in|into|for|as)\s*(?:the\s+)?(?:{alias_pattern})\s*(?:field)?",
        rf"(<[^>]+>)\s*(?:in|into|for|as)\s*(?:the\s+)?(?:{alias_pattern})\s*(?:field)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def decompose_step(step_text: str, step_keyword: str) -> List[Dict[str, str]]:
    """Split multi-intent Gherkin steps into atomic UI interactions."""
    text = step_text.strip()
    lower = text.lower()
    atomic: List[Dict[str, str]] = []

    has_username = "username" in lower or "user name" in lower
    has_password = "password" in lower
    has_submit = any(token in lower for token in ("submit", "login", "log in", "sign in", "click"))

    if has_username and has_password and has_submit:
        username = _extract_field_value(text, ["username", "user name"])
        password = _extract_field_value(text, ["password", "passcode", "pwd"])
        atomic.append({
            "keyword": step_keyword,
            "text": f"I enter {_single_quote(username)} in the Username field" if username else "I enter the username in the Username field",
        })
        atomic.append({
            "keyword": "And",
            "text": f"I enter {_single_quote(password)} in the Password field" if password else "I enter the password in the Password field",
        })
        atomic.append({"keyword": "And", "text": "I click the Login button"})
        return atomic

    clauses = re.split(r"\s+\band\b\s+", text, flags=re.IGNORECASE)
    if len(clauses) > 1:
        verb_count = sum(
            1
            for clause in clauses
            if re.search(r"\b(enter|type|fill|input|click|submit|press|verify|see|check)\b", clause, re.IGNORECASE)
        )
        if verb_count > 1:
            return [
                {"keyword": step_keyword if index == 0 else "And", "text": clause.strip(" ,.")}
                for index, clause in enumerate(clauses)
                if clause.strip(" ,.")
            ]

    return [{"keyword": step_keyword, "text": text}]


# ══════════════════════════════════════════════════════════════════════════════
# §2  EMBEDDING + QDRANT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def generate_embedding(text: str) -> List[float]:
    gateway = get_llm_gateway()
    model_override = gateway.resolve_model_for_agent(
        "step_generator_v1",
        purpose="embedding",
        fallback_model=None,
    )
    return gateway.generate_embedding(text, model_override=model_override)


def _qdrant_rest_search(
    collection: str,
    vector: List[float],
    limit: int = 5,
    payload_filter: Optional[Dict] = None,
) -> List[Dict]:
    """
    Vector-similarity search via Qdrant REST API.

    FIX 1/2: payload_filter uses PROJECT_KEY raw ("SCRUM-70") so it matches
    what vectorize_and_upload_v2 stores in every payload.
    """
    body: Dict[str, Any] = {
        "vector":       vector,
        "limit":        limit,
        "with_payload": True,
    }
    if payload_filter:
        body["filter"] = payload_filter

    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json=body,
            timeout=15,
        )
        if not resp.ok:
            print(f"  ⚠ Qdrant REST search {resp.status_code}: {resp.text[:120]}")
            return []
        return resp.json().get("result", [])
    except Exception as exc:
        print(f"  ⚠ Qdrant REST search exception: {exc}")
        return []


def resolve_url_from_qdrant(step_text: str, dom_collection: str,
                            project_key: str = "") -> str:
    """
    Search DOM collection for a URL matching the step's navigation intent.
    project_key is passed explicitly — never read from the global.
    """
    if not dom_collection:
        return ""
    vector = generate_embedding(step_text)
    if not vector:
        return ""

    pk = project_key or PROJECT_KEY   # explicit param wins; global only as last resort
    payload_filter = {"must": [{"key": "project_key", "match": {"value": pk}}]} if pk else None

    hits = _qdrant_rest_search(
        collection=dom_collection,
        vector=vector,
        limit=5,
        payload_filter=payload_filter,
    )
    for hit in hits:
        payload = hit.get("payload") or {}
        details = payload.get("details", {}) or {}
        url = (
            payload.get("url")
            or payload.get("page_url")
            or details.get("url")
            or details.get("page_url")
            or ""
        )
        if url.startswith(("http://", "https://")):
            print(f"  ↳ Qdrant URL match (score={hit.get('score', 0):.2f}): {url}")
            return url
    return ""


def resolve_url_from_intent(step_text: str, dom_collection: str = "",
                            project_key: str = "") -> str:
    """
    Resolve a navigation URL from the step text.
    Priority:
      1. Literal URL in the step text itself  (no Qdrant needed)
      2. Qdrant ui_memory semantic search     (enrichment)
      3. BASE_URL env var root                (last resort)
    """
    # Priority 1: literal URL in the step text — works for any feature file
    m = re.search(r'https?://[^\s"\']+', step_text)
    if m:
        return m.group(0).rstrip(',;:!?)"\'')

    # Priority 2: Qdrant semantic search
    url = resolve_url_from_qdrant(step_text, dom_collection, project_key=project_key)
    if url:
        return url

    # Priority 3: BASE_URL fallback
    if BASE_URL:
        print(f"  ⚠ No URL found for '{step_text[:60]}' — falling back to BASE_URL: {BASE_URL}/")
        return BASE_URL + "/"
    return ""


def search_qdrant(collection_name: str, query_text: str, limit: int = 5,
                  project_key: str = "") -> List[Dict[str, Any]]:
    """
    Cosine-similarity search in Qdrant.
    project_key is passed explicitly so this works for any feature file,
    not just the one loaded at startup.
    Returns [] gracefully if Qdrant is unreachable or collection is empty.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    try:
        client       = QdrantClient(url=QDRANT_URL)
        query_vector = generate_embedding(query_text)
        if not query_vector:
            return []

        pk = project_key or PROJECT_KEY
        project_filter = Filter(
            must=[FieldCondition(key="project_key", match=MatchValue(value=pk))]
        ) if pk else None

        scroll_results, _ = client.scroll(
            collection_name = collection_name,
            scroll_filter   = project_filter,
            limit           = limit * 3,
            with_vectors    = True,
        )

        results = []
        for point in scroll_results:
            pv = point.vector
            if pv and len(pv) == len(query_vector):
                dot   = sum(a * b for a, b in zip(query_vector, pv))
                ma    = sum(a * a for a in query_vector) ** 0.5
                mb    = sum(b * b for b in pv) ** 0.5
                score = dot / (ma * mb) if ma > 0 and mb > 0 else 0.0
            else:
                score = 0.0
            results.append({
                "text":    point.payload.get("text", ""),
                "score":   score,
                "payload": point.payload,
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    except Exception as exc:
        print(f"  ⚠ Qdrant search skipped ({collection_name}): {exc}")
        return []


def _escape_selector_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _clean_user_facing_text(value: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    if any(marker in text for marker in ("Element Type:", "Identity:", "Structure:", "Page URL:", "Matching Signals:")):
        return ""
    return text[:160]


def _is_safe_locator_selector(selector: str) -> bool:
    if not selector:
        return False
    if "\n" in selector or "\r" in selector:
        return False
    if len(selector) > 220:
        return False
    # Reject positional XPaths — they break across different page contexts
    # e.g. /html/body/.../ul/li[4]/a matches different links on different pages
    if re.match(r'^(xpath=)?/html/body/', selector):
        return False
    # Reject XPaths with positional predicates — fragile across DOM changes
    if re.search(r'\[\d+\]', selector) and ("/" in selector):
        return False
    return True


def _is_xpath_selector(selector: str) -> bool:
    sel = (selector or "").strip()
    return sel.startswith("/") or sel.startswith("(")


def _playwright_selector(selector: str) -> str:
    sel = (selector or "").strip()
    if _is_xpath_selector(sel):
        return f"xpath={sel}"
    return sel


def _is_fillable_payload(mapping: StepMapping) -> bool:
    details = mapping.semantic_match.get("details", {}) or {}
    tag = str(details.get("tagName") or "").lower()
    role = str(details.get("role") or details.get("ariaRole") or "").lower()
    return tag in {"input", "textarea", "select"} or role in {"textbox", "searchbox", "combobox"}


def _is_clickable_payload(mapping: StepMapping) -> bool:
    details = mapping.semantic_match.get("details", {}) or {}
    element_type = str(mapping.semantic_match.get("element_type") or "").lower()
    tag = str(details.get("tagName") or "").lower()
    role = str(details.get("role") or details.get("ariaRole") or "").lower()
    text_blob = " ".join(
        str(details.get(k) or "") for k in ("text", "label", "placeholder", "name")
    ).lower()
    return (
        tag in {"button", "a"}
        or role in {"button", "link"}
        or element_type in {"button", "interactive"}
        or "submit" in text_blob
    )


def _should_emit_direct_locator(mapping: StepMapping) -> bool:
    """
    A tester would only hard-code a locator when the match is both plausible
    and action-compatible. Otherwise we keep the step semantic and let
    BasePage.smartAction() heal/search more safely at runtime.
    """
    selector = mapping.selector
    if not _is_safe_locator_selector(selector):
        return False
    if mapping.confidence_tier() == "low":
        return False
    if mapping.ambiguous:
        return False
    if mapping.action_type == "smartFill":
        return _is_fillable_payload(mapping)
    if mapping.action_type == "smartClick":
        return _is_clickable_payload(mapping)
    if mapping.action_type == "verifyText":
        return not _is_xpath_selector(selector)
    return False


def _extract_target_candidates(step_text: str) -> List[str]:
    text = step_text.strip()
    candidates: List[str] = []
    patterns = [
        r"in the ([A-Za-z0-9 _-]+? field)\b",
        r"click the ([A-Za-z0-9 _-]+? button)\b",
        r"see the ([A-Za-z0-9 _-]+? message)\b",
        r"see ([A-Za-z0-9 _-]+? error message)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidates.append(match.group(1).strip())

    for token in ("username", "password", "login button", "error message"):
        if token in text.lower():
            candidates.append(token)

    return list(dict.fromkeys(candidates))


def _role_matches(action_type: str, payload: Dict[str, Any]) -> bool:
    details = payload.get("details", {}) or {}
    role = str(payload.get("role") or details.get("role") or details.get("ariaRole") or "").lower()
    tag = str(payload.get("tagName") or details.get("tagName") or "").lower()
    if action_type == "smartFill":
        return role in {"textbox", "searchbox", "combobox"} or tag in {"input", "textarea", "select"}
    if action_type == "smartClick":
        return role in {"button", "link", "checkbox", "radio"} or tag in {"button", "a"}
    if action_type == "verifyText":
        return True
    return False


def _payload_details(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("details", {}) or {}


def _payload_page_url(payload: Dict[str, Any]) -> str:
    details = _payload_details(payload)
    return str(
        payload.get("page_url")
        or payload.get("url")
        or details.get("page_url")
        or details.get("url")
        or ""
    ).strip()


def _payload_tag(payload: Dict[str, Any]) -> str:
    details = _payload_details(payload)
    return str(payload.get("tagName") or details.get("tagName") or "").lower()


def _payload_role(payload: Dict[str, Any]) -> str:
    details = _payload_details(payload)
    return str(
        payload.get("role")
        or details.get("role")
        or details.get("ariaRole")
        or payload.get("ariaRole")
        or ""
    ).lower()


def _payload_identity_text(payload: Dict[str, Any]) -> str:
    return " ".join(_payload_field_values(payload)).lower()


def _normalized_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _tester_match_adjustment(
    payload: Dict[str, Any],
    action_type: str,
    target_candidates: List[str],
    current_page_url: str = "",
) -> float:
    """
    Heuristics that mimic how a tester thinks:
    stay on the current page, prefer executable controls for the action type,
    and avoid using navigation chrome when the step is about a form control.
    """
    score = 0.0
    tag = _payload_tag(payload)
    role = _payload_role(payload)
    page_url = _normalized_url(_payload_page_url(payload))
    current_page = _normalized_url(current_page_url)
    identity = _payload_identity_text(payload)
    wants_button = any("button" in c.lower() for c in target_candidates)
    wants_field = any("field" in c.lower() for c in target_candidates)

    if current_page and page_url:
        score += 0.35 if page_url == current_page else -0.55

    if action_type == "smartFill":
        if tag in {"input", "textarea", "select"} or role in {"textbox", "searchbox", "combobox"}:
            score += 0.30
        else:
            score -= 0.80

    elif action_type == "smartClick":
        if wants_button or "submit" in identity:
            if tag == "button" or role == "button":
                score += 0.35
            if tag == "a" or role == "link":
                score -= 0.60

    elif action_type == "verifyText":
        if "error" in " ".join(target_candidates).lower():
            if "error" in identity or "invalid" in identity:
                score += 0.20
            elif tag in {"a", "button"}:
                score -= 0.25
        if "output section" in " ".join(target_candidates).lower():
            if "output" in identity:
                score += 0.25
            elif tag in {"a", "button"}:
                score -= 0.25

    if wants_field and tag in {"a", "button"} and action_type == "smartFill":
        score -= 0.40

    return score


def _generic_penalty(payload: Dict[str, Any]) -> float:
    details = payload.get("details", {}) or {}
    tag = str(payload.get("tagName") or details.get("tagName") or "").lower()
    return 0.10 if tag in {"div", "span"} else 0.0


def _payload_field_values(payload: Dict[str, Any]) -> List[str]:
    details = payload.get("details", {}) or {}
    values: List[str] = []
    for key in ("label", "text", "placeholder", "name", "ariaLabel", "aria_label"):
        for source in (payload, details):
            val = source.get(key)
            if isinstance(val, str) and val.strip():
                values.append(val.strip())
    return list(dict.fromkeys(values))


def _match_bonus(payload: Dict[str, Any], target_candidates: List[str], action_type: str) -> float:
    fields = [value.lower() for value in _payload_field_values(payload)]
    bonus = 0.0
    for candidate in target_candidates:
        c = candidate.lower()
        if any(field == c for field in fields):
            bonus += 0.25
        elif any(c in field or field in c for field in fields):
            bonus += 0.15
    if _role_matches(action_type, payload):
        bonus += 0.10
    bonus -= _generic_penalty(payload)
    return bonus


def _fallback_selector_from_payload(payload: Dict[str, Any], action_type: str) -> str:
    details = payload.get("details", {}) or {}
    combined = {**payload, **details}

    if action_type == "smartFill":
        if combined.get("name"):
            return f'[name="{_escape_selector_value(str(combined["name"]))}"]'
        if combined.get("placeholder"):
            return f'[placeholder="{_escape_selector_value(str(combined["placeholder"]))}"]'
        aria = str(combined.get("ariaLabel") or combined.get("aria_label") or "").strip()
        if aria:
            return f'[aria-label="{_escape_selector_value(aria)}"]'
        label = _clean_user_facing_text(str(combined.get("label") or ""))
        if label:
            return f'[aria-label="{_escape_selector_value(label)}"]'

    if action_type == "smartClick":
        label = _clean_user_facing_text(str(combined.get("label") or combined.get("text") or ""))
        if label:
            return f'role=button[name="{_escape_selector_value(label)}"]'
        text = _clean_user_facing_text(str(combined.get("text") or ""))
        if text:
            return f'text={_escape_selector_value(text)}'

    if action_type == "verifyText":
        text = _clean_user_facing_text(str(combined.get("text") or combined.get("label") or ""))
        if text:
            return f'text={_escape_selector_value(text)}'

    return ""


def _rule_based_step_analysis(step_text: str, step_keyword: str) -> Dict[str, Any]:
    lower = step_text.lower()
    quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", step_text)
    values = [a or b for a, b in quoted if (a or b)]
    value = values[0] if values else ""
    target_query = " ".join(_extract_target_candidates(step_text)) or step_text

    # ── Navigation ────────────────────────────────────────────────────────
    if re.search(r'https?://[^\s"\']+', step_text):
        return {"action_type": "navigate", "value": "", "confidence": 0.98,
                "reasoning": "Explicit URL in step", "target_query": step_text}
    if step_keyword == "Given" and any(t in lower for t in
            ("am on", "go to", "visit", "open", "navigate to", "i am on the")):
        return {"action_type": "navigate", "value": "", "confidence": 0.92,
                "reasoning": "Navigation keyword", "target_query": target_query}

    # ── verifySelectorExists ──────────────────────────────────────────────
    # Steps like: Then the "Full Name" field selector "#userName" exists and is enabled
    if re.search(r'selector\s+["\']?#[\w-]+["\']?\s+(exists|is present|is enabled)', lower):
        return {"action_type": "verifySelectorExists", "value": value,
                "confidence": 0.95, "reasoning": "Explicit selector existence check",
                "target_query": target_query}

    # ── verifyDisabled ────────────────────────────────────────────────────
    # Steps like: Then the X button should be disabled / X is disabled
    if re.search(r'\b(should be disabled|is disabled|remains disabled|is not interactable|'
                 r'should remain disabled|assert.disabled)\b', lower):
        return {"action_type": "verifyDisabled", "value": value,
                "confidence": 0.94, "reasoning": "Disabled state assertion",
                "target_query": target_query}

    # ── verifyAbsent ──────────────────────────────────────────────────────
    # Steps like: And the output section should not be displayed / should not render
    if re.search(r'\b(should not be (displayed|rendered|visible|shown|present)|'
                 r'not (rendered|displayed|visible|shown)|'
                 r'should not see|must not appear|is not rendered|'
                 r'is not displayed|not be present|should be hidden)\b', lower):
        return {"action_type": "verifyAbsent", "value": value,
                "confidence": 0.93, "reasoning": "Absence / hidden assertion",
                "target_query": target_query}

    # ── verifyText ────────────────────────────────────────────────────────
    if re.search(r'\b(should see|should display|should show|should contain|'
                 r'should be visible|verify|check|is visible|'
                 r'error message should appear|validation error|'
                 r'display.*submitted|result.*display|label.*present|'
                 r'field.*invalid|border.*red|in an invalid state)\b', lower):
        return {"action_type": "verifyText", "value": value,
                "confidence": 0.88, "reasoning": "Visible text / state assertion",
                "target_query": target_query}

    # ── smartFill — empty field ───────────────────────────────────────────
    # Steps like: When I leave the Email field empty
    if re.search(r'\b(leave|leave the|keep)\b.*\b(empty|blank|clear)\b', lower):
        return {"action_type": "smartFill", "value": "",
                "confidence": 0.91, "reasoning": "Explicit empty field fill",
                "target_query": target_query}

    # ── smartFill ─────────────────────────────────────────────────────────
    if any(t in lower for t in ("enter", "type", "fill", "input")):
        return {"action_type": "smartFill", "value": value,
                "confidence": 0.90, "reasoning": "Fill keyword detected",
                "target_query": target_query}

    # ── smartClick ────────────────────────────────────────────────────────
    if re.search(r'\b(click|press|select|tap|interact with)\b', lower) or \
       re.search(r'\bsubmit\b', lower):
        return {"action_type": "smartClick", "value": "",
                "confidence": 0.88, "reasoning": "Click keyword detected",
                "target_query": target_query}

    # ── verifyText — remaining assertion keywords ─────────────────────────
    if any(t in lower for t in ("accessible", "stable", "interactable",
                                "remain open", "no submission", "should be")):
        return {"action_type": "verifyText", "value": value,
                "confidence": 0.80, "reasoning": "State assertion fallback",
                "target_query": target_query}

    return {"action_type": "unknown", "value": value, "confidence": 0.0,
            "reasoning": "", "target_query": target_query}


# ══════════════════════════════════════════════════════════════════════════════
# §3  SELECTOR STRATEGY  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

_SELECTOR_PRIORITY = [
    "data_testid", "data-testid", "testId",
    "id",
    "aria_label", "aria-label",
    "name",
    "selector",
    "xpath",
    "className",
]


def _extract_best_selector(payload: Dict[str, Any]) -> str:
    """
    Extract the most stable selector from an element payload.
    Priority: data-testid > id > aria-label > name > crawler selector > xpath.
    """
    details  = payload.get("details", {}) or {}
    combined = {**payload, **details}

    for key in _SELECTOR_PRIORITY:
        val = combined.get(key, "")
        if not val or not isinstance(val, str):
            continue
        val = val.strip()
        if not val:
            continue
        if re.search(r"\d{5,}", val):
            continue
        if re.fullmatch(r"[.\#][a-z0-9]{1,3}", val):
            continue
        if key == "id" and not val.startswith("#"):
            return f"#{val}"
        if key in ("data_testid", "data-testid", "testId"):
            return f'[data-testid="{val}"]'
        if key in ("aria_label", "aria-label"):
            return f'[aria-label="{val}"]'
        if key == "name":
            return f'[name="{val}"]'
        return val

    return ""


# ══════════════════════════════════════════════════════════════════════════════
# §4  CONFIDENCE SCORING  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def _compute_confidence(
    llm_confidence: float,
    semantic_score: float,
    selector:       str,
    page_url:       str,
    step_text:      str,
) -> float:
    """
    Composite confidence ∈ [0.0, 1.0].

    semantic_score  0.40  (vector cosine from Qdrant — primary signal)
    llm_confidence  0.25  (LLM reasoning quality)
    selector_bonus  0.20  (stable selector found?)
    page_url_bonus  0.15  (page_url resolved from payload?)
    """
    selector_score = 1.0 if selector else 0.0
    if selector and selector.startswith(("/", "(/")):
        selector_score = 0.5

    page_url_score = 1.0 if page_url.startswith(("http://", "https://")) else 0.0

    raw = (
        semantic_score * 0.40
        + llm_confidence * 0.25
        + selector_score * 0.20
        + page_url_score * 0.15
    )
    return round(min(max(raw, 0.0), 1.0), 4)


# ══════════════════════════════════════════════════════════════════════════════
# §5  ANALYZE GHERKIN STEP
# ══════════════════════════════════════════════════════════════════════════════

def analyze_gherkin_step(step_text: str, step_keyword: str,
                         current_page_url: str = "",
                         project_key: str = "",
                         dom_collection: str = "") -> Dict[str, Any]:
    """
    Classify a Gherkin step and enrich with Qdrant DOM data.

    Fully feature-file-agnostic:
      1. Rule-based classifier  — works with ANY .feature file, no Qdrant/LLM needed
      2. LLM fallback           — only for genuinely ambiguous steps; graceful if unavailable
      3. Qdrant enrichment      — selector + page_url from ui_memory (optional enrichment)

    project_key / dom_collection passed explicitly — never read from module globals.
    """
    pk  = project_key  or PROJECT_KEY
    col = dom_collection or DOM_COLLECTION

    # Stage 1 — rule-based (handles 95%+ of standard Gherkin patterns)
    rule_result = _rule_based_step_analysis(step_text, step_keyword)
    if rule_result.get("action_type") != "unknown":
        llm_result = rule_result
    else:
        # Stage 2 — LLM (only for truly ambiguous steps, optional)
        try:
            default_gateway = get_llm_gateway()
            provider = default_gateway.resolve_provider_for_agent(
                "step_generator_v2",
                purpose="chat",
                fallback_provider=os.getenv("LLM_PROVIDER", "ollama"),
            )
            llm_result = get_llm_gateway(provider=provider).analyze_gherkin_step(
                step_text, step_keyword
            )
        except Exception as exc:
            print(f"  ⚠ LLM skipped ({exc}) — using rule-based result")
            llm_result = rule_result

    action_type       = llm_result.get("action_type", "unknown")
    search_query      = rule_result.get("target_query") or step_text
    # Stage 3 — Qdrant DOM enrichment (graceful if empty/unavailable)
    semantic_matches  = search_qdrant(col, search_query, limit=8, project_key=pk)
    target_candidates = _extract_target_candidates(step_text)

    reranked_matches: List[Dict[str, Any]] = []
    for match in semantic_matches:
        payload        = match.get("payload", {}) or {}
        adjusted_score = (
            float(match.get("score", 0.0))
            + _match_bonus(payload, target_candidates, action_type)
            + _tester_match_adjustment(
                payload, action_type, target_candidates,
                current_page_url=current_page_url,
            )
        )
        reranked_matches.append({**match, "adjusted_score": round(adjusted_score, 4)})
    reranked_matches.sort(
        key=lambda x: x.get("adjusted_score", x.get("score", 0.0)), reverse=True
    )

    best_match = reranked_matches[0] if reranked_matches else {}
    payload    = best_match.get("payload", {}) if best_match else {}
    details    = payload.get("details", {}) or {}
    selector   = _extract_best_selector(payload)
    page_url   = (
        payload.get("url") or payload.get("page_url")
        or details.get("url") or details.get("page_url") or ""
    )
    qa_signals = {
        "visible":    payload.get("visible",    details.get("visible")),
        "obstructed": payload.get("obstructed", details.get("obstructed")),
        "qa_status":  payload.get("qa_status",  details.get("qa_status", "ok")),
    }

    return {
        "action_type": action_type,
        "value":       llm_result.get("value", ""),
        "confidence":  llm_result.get("confidence", 0.0),
        "reasoning":   llm_result.get("reasoning", ""),
        "selector":    selector,
        "page_url":    page_url,
        "qa_signals":  qa_signals,
        "_semantic": {
            "score":        best_match.get("adjusted_score", best_match.get("score", 0.0)),
            "all_matches":  reranked_matches,
            "text":         payload.get("text", ""),
            "element_type": payload.get("element_type", ""),
            "details":      details,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# §6  MAP STEP TO ACTION  (returns StepMapping)
# ══════════════════════════════════════════════════════════════════════════════

def map_step_to_action(step_text: str, step_keyword: str,
                       context: Optional[StepContext] = None) -> StepMapping:
    """
    Core mapping function — returns a StepMapping.

    Resolution priority (all sources optional, graceful degradation):
      1. Rule-based step classification (always runs, no dependencies)
      2. Qdrant DOM collection — selector + page_url enrichment
      3. LLM gateway — classification fallback for ambiguous steps
      4. Literal URL in step text — navigation without Qdrant
      5. BASE_URL env var — last-resort navigation fallback

    All context (project_key, dom_collection) comes from StepContext,
    never from module globals — so any .feature file works correctly.
    """
    ctx              = context or StepContext()
    current_page_url = ctx.current_page_url
    pk               = ctx.project_key  or PROJECT_KEY
    col              = ctx.dom_collection or DOM_COLLECTION
    warnings: List[str] = []

    try:
        analysis = analyze_gherkin_step(
            step_text, step_keyword,
            current_page_url = current_page_url,
            project_key      = pk,
            dom_collection   = col,
        )
    except Exception as exc:
        warnings.append(f"analyze_gherkin_step failed: {exc}")
        analysis = {
            "action_type": "unknown", "value": "", "confidence": 0.0,
            "reasoning": "", "selector": "", "page_url": "",
            "qa_signals": {}, "_semantic": {"score": 0.0, "all_matches": []},
        }

    llm_confidence = float(analysis.get("confidence", 0.0))
    semantic       = analysis.get("_semantic", {})
    semantic_score = float(semantic.get("score", 0.0))
    selector       = analysis.get("selector", "")
    page_url       = analysis.get("page_url", "")
    qa_signals     = analysis.get("qa_signals", {})
    all_matches    = semantic.get("all_matches", [])
    payload        = (all_matches[0].get("payload", {}) or {}) if all_matches else {}
    if not payload and semantic.get("details"):
        payload = {"details": semantic.get("details", {})}

    # Ambiguity detection
    ambiguous = False
    if len(all_matches) >= 2:
        s0 = all_matches[0].get("adjusted_score", all_matches[0].get("score", 0.0))
        s1 = all_matches[1].get("adjusted_score", all_matches[1].get("score", 0.0))
        if abs(s0 - s1) < 0.05 and s0 > 0.3:
            ambiguous = True
            selector  = _fallback_selector_from_payload(payload, analysis.get("action_type", "unknown"))
            warnings.append(
                f"Ambiguous: top-2 scores {s0:.3f} vs {s1:.3f} (Δ={abs(s0-s1):.3f}<0.05)"
            )
    if not selector:
        selector = _fallback_selector_from_payload(payload, analysis.get("action_type", "unknown"))

    action_type = analysis.get("action_type", "unknown")
    value       = analysis.get("value", "")

    # Navigation URL — priority: Qdrant result → literal in step → BASE_URL
    if action_type == "navigate" and not page_url:
        resolved = resolve_url_from_intent(step_text, col, project_key=pk)
        if resolved:
            page_url = resolved
        else:
            warnings.append("Navigation: no URL resolved from step text, Qdrant, or BASE_URL.")

    if action_type != "navigate" and not page_url and current_page_url:
        page_url = current_page_url

    confidence     = _compute_confidence(llm_confidence, semantic_score, selector, page_url, step_text)
    qa_adjustments = QAAdjustments.from_payload({**qa_signals, "details": semantic.get("details", {})})

    if not selector:
        warnings.append("No stable selector found — smartAction() will use semantic intent.")
    if not page_url:
        warnings.append("page_url not resolved — step is not page-pinned.")
    if confidence < 0.45:
        warnings.append(f"Low confidence ({confidence:.2f}) — manual review recommended.")

    return StepMapping(
        step_text      = step_text,
        step_keyword   = step_keyword,
        action_type    = action_type,
        page_url       = page_url,
        selector       = selector,
        value          = value,
        confidence     = confidence,
        qa_adjustments = qa_adjustments,
        reasoning      = analysis.get("reasoning", ""),
        semantic_match = {
            "text":         semantic.get("text", ""),
            "element_type": semantic.get("element_type", ""),
            "details":      semantic.get("details", {}),
        },
        ambiguous      = ambiguous,
        warnings       = warnings,
    )


# ══════════════════════════════════════════════════════════════════════════════
# §7  GENERATE TYPESCRIPT STEP  (v2 — all action types)
# ══════════════════════════════════════════════════════════════════════════════

def escape_typescript_string(s: str) -> str:
    if not s:
        return s
    s = s.replace("\\", "\\\\")
    s = s.replace('"',  '\\"')
    return s


def _extract_inline_selector(step_text: str) -> str:
    """
    FIX 12: Extract an explicit CSS selector written directly in the step text.
    Handles:
      - selector "#userName"  /  selector '#submit'
      - (#noRadio)  anywhere in the step
      - "field selector #id"
    Returns the selector string (e.g. "#userName") or "".
    """
    # Pattern: the word 'selector' followed by optional quote and #id
    m = re.search(r'\bselector\s+["\']?(#[\w-]+)["\']?', step_text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Pattern: (#id) — selector in parentheses
    m = re.search(r'\((#[\w-]+)\)', step_text)
    if m:
        return m.group(1)
    return ""


def _has_explicit_empty_fill(step_text: str) -> bool:
    """FIX 11: Detect 'leave X empty' / 'enter "" in' patterns."""
    lower = step_text.lower()
    return bool(
        re.search(r"\benter\s+''\b", lower)
        or re.search(r'\benter\s+""\b', step_text)
        or "enter '' in the" in lower
        or 'enter "" in the' in step_text
        or re.search(r'\b(leave|keep)\b.{0,40}\b(empty|blank|clear)\b', lower)
    )


def generate_typescript_step(
    action_map:   Any,    # StepMapping | legacy Dict
    step_keyword: str,
    step_text:    str,
) -> str:
    """
    Map one Gherkin step to Playwright TypeScript.

    Action routing:
      navigate           → page.goto(url)
      smartFill          → locator.fill(value)  OR smartAction(intent, value)
      smartClick         → locator.click()       OR smartAction(intent)
      verifyText         → expect(locator).toContainText/toBeVisible
      verifyAbsent       → expect(locator).toBeHidden()          [FIX 8]
      verifyDisabled     → expect(locator).toBeDisabled()        [FIX 8]
      verifySelectorExists → expect(locator).toBeVisible/Enabled [FIX 8]
      unknown            → smartAction(intent)  [TEA semantic fallback]

    BasePage.smartAction() handles all fallbacks when no direct locator is used:
      - Qdrant DOM semantic search
      - Role/aria-label resolution
      - URL assertion fallback for verify intents
      - Error guard for negative/validation assertions
    """
    if isinstance(action_map, StepMapping):
        mapping: StepMapping = action_map
    else:
        mapping = StepMapping(
            step_text    = step_text,
            step_keyword = step_keyword,
            action_type  = action_map.get("action_type", "unknown"),
            page_url     = action_map.get("page_url", ""),
            selector     = action_map.get("selector", ""),
            value        = action_map.get("value", ""),
            confidence   = float(action_map.get("confidence", 0.0)),
        )

    action_type     = mapping.action_type
    value           = mapping.value
    selector        = mapping.selector
    safe_selector   = selector if _is_safe_locator_selector(selector) else ""
    direct_selector = _playwright_selector(safe_selector) if safe_selector else ""
    use_direct      = _should_emit_direct_locator(mapping)
    page_url        = mapping.page_url
    escaped_step    = escape_typescript_string(step_text)
    escaped_value   = escape_typescript_string(value) if value else ""

    # FIX 12: for assertion steps, prefer inline selector from step text
    inline_sel = _extract_inline_selector(step_text)
    if inline_sel and action_type in (
        "verifyAbsent", "verifyDisabled", "verifySelectorExists", "verifyText"
    ):
        direct_selector = inline_sel
        use_direct      = True

    # Build QA comment suffix
    qa_parts = mapping.qa_adjustments.as_comment_parts()
    if mapping.confidence_tier() == "low":
        qa_parts.insert(0, "LOW-CONFIDENCE")
    if mapping.ambiguous:
        qa_parts.insert(0, "AMBIGUOUS-MATCH")
    qa_comment    = f"  // {', '.join(qa_parts)}" if qa_parts else ""
    selector_hint = f" /* selector: {selector} */" if safe_selector and not use_direct else ""

    I = "        "  # 8-space indent — inside test() body

    # ── 1. NAVIGATE ─────────────────────────────────────────────────────────
    if action_type == "navigate":
        url = page_url
        if not url or not url.startswith(("http://", "https://")):
            m   = re.search(r'https?://[^\s"\']+', step_text)
            url = m.group(0).rstrip(',;:!?)"\'') if m else ""
        if not url or not url.startswith(("http://", "https://")):
            url = resolve_url_from_intent(step_text, DOM_COLLECTION)
        if url and url.startswith(("http://", "https://")):
            return f'{I}await basePage.page.goto("{escape_typescript_string(url)}");{qa_comment}'
        return (
            f'{I}await basePage.smartAction("{escaped_step}");'
            f'  // WARNING: no URL resolved — set BASE_URL in .env or re-run dom_capture'
            f'{qa_comment}'
        )

    # ── 2. FILL ─────────────────────────────────────────────────────────────
    if action_type == "smartFill":
        # FIX 11: explicit empty fill ("leave X empty" / enter "" in)
        if _has_explicit_empty_fill(step_text):
            if use_direct and direct_selector:
                return f'{I}await basePage.page.locator("{escape_typescript_string(direct_selector)}").fill("");{qa_comment}'
            return f'{I}await basePage.smartAction("{escaped_step}", "");{selector_hint}{qa_comment}'
        # Normal fill with value
        if use_direct and direct_selector and escaped_value:
            return f'{I}await basePage.page.locator("{escape_typescript_string(direct_selector)}").fill("{escaped_value}");{qa_comment}'
        # FIX: never emit smartAction(intent, "") — BasePage throws "value required"
        # If no value was extracted, use the selector hint in a comment and
        # let smartAction parse the full intent (BasePage will deduce the value)
        if escaped_value:
            return f'{I}await basePage.smartAction("{escaped_step}", "{escaped_value}");{selector_hint}{qa_comment}'
        # No value at all — emit as smartAction without value arg so BasePage
        # can attempt semantic resolution rather than throwing
        return f'{I}await basePage.smartAction("{escaped_step}");{selector_hint}  // TODO: add explicit value to Gherkin step{qa_comment}'

    # ── 3. CLICK ────────────────────────────────────────────────────────────
    if action_type == "smartClick":
        if use_direct and direct_selector:
            return f'{I}await basePage.page.locator("{escape_typescript_string(direct_selector)}").click();{qa_comment}'
        return f'{I}await basePage.smartAction("{escaped_step}");{selector_hint}{qa_comment}'

    # ── 4. VERIFY TEXT / VISIBLE ────────────────────────────────────────────
    if action_type == "verifyText":
        if use_direct and direct_selector:
            if escaped_value:
                return f'{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toContainText("{escaped_value}");{qa_comment}'
            return f'{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toBeVisible();{qa_comment}'
        return f'{I}await basePage.smartAction("{escaped_step}");{selector_hint}{qa_comment}'

    # ── 5. VERIFY ABSENT — "should not be displayed/rendered/visible" ────────
    # FIX 8: emit toBeHidden() directly when selector known; else smartAction
    if action_type == "verifyAbsent":
        if use_direct and direct_selector:
            return f'{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toBeHidden();{qa_comment}'
        return (
            f'{I}await basePage.smartAction("{escaped_step}");'
            f'  // verifyAbsent — BasePage will assert element is not visible'
            f'{qa_comment}'
        )

    # ── 6. VERIFY DISABLED — "should be disabled / remains disabled" ─────────
    # FIX 8: emit toBeDisabled() directly when selector known
    if action_type == "verifyDisabled":
        if use_direct and direct_selector:
            return f'{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toBeDisabled();{qa_comment}'
        return (
            f'{I}await basePage.smartAction("{escaped_step}");'
            f'  // verifyDisabled — BasePage will assert element is disabled'
            f'{qa_comment}'
        )

    # ── 7. VERIFY SELECTOR EXISTS ─────────────────────────────────────────────
    # FIX 8/12: steps like 'the "Full Name" field selector "#userName" exists and is enabled'
    # Expand into one assertion per selector pair found in the step text.
    if action_type == "verifySelectorExists":
        lines: List[str] = []
        # Extract all "label" + selector pairs from the step
        pairs = re.findall(
            r'"([^"]+)"\s+(?:field\s+)?selector\s+"?(#[\w-]+)"?',
            step_text, re.IGNORECASE
        )
        check_enabled = "enabled" in step_text.lower()
        check_empty   = "empty"   in step_text.lower()
        if pairs:
            for label, sel in pairs:
                esc_sel = escape_typescript_string(sel)
                lines.append(f'{I}await expect(basePage.page.locator("{esc_sel}")).toBeVisible();  // {label}')
                if check_enabled:
                    lines.append(f'{I}await expect(basePage.page.locator("{esc_sel}")).toBeEnabled();  // {label}')
                if check_empty:
                    lines.append(f'{I}await expect(basePage.page.locator("{esc_sel}")).toBeEmpty();  // {label}')
            return "\n".join(lines) + qa_comment
        # No pairs found — single selector from inline extraction
        if direct_selector:
            result = f'{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toBeVisible();{qa_comment}'
            if check_enabled:
                result += f'\n{I}await expect(basePage.page.locator("{escape_typescript_string(direct_selector)}")).toBeEnabled();'
            return result
        return f'{I}await basePage.smartAction("{escaped_step}");  // verifySelectorExists{qa_comment}'

    # ── 8. UNKNOWN — TEA semantic fallback via smartAction ───────────────────
    return (
        f'{I}await basePage.smartAction("{escaped_step}");'
        f'  // TEA fallback: action_type={action_type}'
        f'{selector_hint}{qa_comment}'
    )


# ══════════════════════════════════════════════════════════════════════════════
# §8  COVERAGE REPORT  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def generate_coverage_report(
    all_mappings: List[StepMapping],
    output_path:  Optional[str] = None,
) -> Dict[str, Any]:
    total  = len(all_mappings)
    mapped = sum(1 for m in all_mappings if m.action_type not in ("unknown",))

    step_coverage = {
        "total_steps":  total,
        "mapped_steps": mapped,
        "coverage_pct": round(mapped / total * 100, 1) if total else 0.0,
    }

    tiers: Dict[str, int] = defaultdict(int)
    for m in all_mappings:
        tiers[m.confidence_tier()] += 1

    confidence_breakdown = {
        tier: {
            "count": tiers[tier],
            "pct":   round(tiers[tier] / total * 100, 1) if total else 0.0,
        }
        for tier in ("high", "medium", "low")
    }

    page_counts: Dict[str, int] = defaultdict(int)
    for m in all_mappings:
        key = m.page_url if m.page_url else "__unresolved__"
        page_counts[key] += 1

    page_coverage = {
        url: {"steps": count}
        for url, count in sorted(page_counts.items(), key=lambda kv: -kv[1])
    }

    selector_counts: Dict[str, int] = defaultdict(int)
    for m in all_mappings:
        if m.selector:
            selector_counts[m.selector] += 1

    element_usage = [
        {"selector": sel, "used_by_steps": cnt}
        for sel, cnt in sorted(selector_counts.items(), key=lambda kv: -kv[1])[:10]
    ]

    needs_retry   = [m.step_text for m in all_mappings if m.qa_adjustments.needs_retry]
    needs_overlay = [m.step_text for m in all_mappings if m.qa_adjustments.needs_overlay_dismiss]
    needs_scroll  = [m.step_text for m in all_mappings if m.qa_adjustments.needs_scroll]
    low_conf      = [m.step_text for m in all_mappings if m.confidence_tier() == "low"]
    ambiguous     = [m.step_text for m in all_mappings if m.ambiguous]

    risk_summary = {
        "needs_retry":           {"count": len(needs_retry),   "steps": needs_retry},
        "needs_overlay_dismiss": {"count": len(needs_overlay), "steps": needs_overlay},
        "needs_scroll":          {"count": len(needs_scroll),  "steps": needs_scroll},
        "low_confidence":        {"count": len(low_conf),      "steps": low_conf},
        "ambiguous_match":       {"count": len(ambiguous),     "steps": ambiguous},
    }

    warnings_log = [
        {"step": m.step_text, "keyword": m.step_keyword, "warnings": m.warnings}
        for m in all_mappings if m.warnings
    ]

    report = {
        "step_coverage":        step_coverage,
        "confidence_breakdown": confidence_breakdown,
        "page_coverage":        page_coverage,
        "element_usage":        element_usage,
        "risk_summary":         risk_summary,
        "warnings_log":         warnings_log,
    }

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"  ✓ Coverage report written: {output_path}")

    return report


def _print_coverage_summary(report: Dict[str, Any]) -> None:
    sc = report["step_coverage"]
    cb = report["confidence_breakdown"]
    rs = report["risk_summary"]

    print("\n" + "═" * 60)
    print("  COVERAGE REPORT")
    print("═" * 60)
    print(f"  Step Coverage  : {sc['mapped_steps']}/{sc['total_steps']} "
          f"({sc['coverage_pct']}%)")
    print(f"  Confidence     : "
          f"HIGH={cb['high']['count']}({cb['high']['pct']}%)  "
          f"MED={cb['medium']['count']}({cb['medium']['pct']}%)  "
          f"LOW={cb['low']['count']}({cb['low']['pct']}%)")

    print("\n  Pages touched:")
    for url, info in list(report["page_coverage"].items())[:8]:
        display = url if len(url) <= 60 else "…" + url[-57:]
        print(f"    {info['steps']:>3} step(s)  {display}")

    print("\n  Risk flags:")
    for risk_key, risk_val in rs.items():
        if risk_val["count"] > 0:
            label = risk_key.replace("_", " ").title()
            print(f"    ⚠  {label}: {risk_val['count']} step(s)")

    print("═" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# §9  TEST-FILE BUILDER  (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def _map_steps(steps: List[Dict],
               initial_context: Optional[StepContext] = None) -> Tuple[List[str], List[StepMapping], StepContext]:
    """
    Map a list of Gherkin steps to TypeScript lines.
    Threads StepContext (current_page_url, project_key, dom_collection)
    through every step so each call is fully self-contained.
    """
    ts_lines: List[str]         = []
    mappings: List[StepMapping] = []
    context = StepContext(
        current_page_url = initial_context.current_page_url if initial_context else "",
        project_key      = initial_context.project_key      if initial_context else "",
        dom_collection   = initial_context.dom_collection   if initial_context else "",
    )

    for step in steps:
        for atomic_step in decompose_step(step["text"], step["keyword"]):
            mapping  = map_step_to_action(atomic_step["text"], atomic_step["keyword"],
                                          context=context)
            ts_line  = generate_typescript_step(mapping, atomic_step["keyword"],
                                                atomic_step["text"])
            ts_lines.append("    " + ts_line)
            mappings.append(mapping)

            if mapping.action_type == "navigate" and mapping.page_url:
                context.current_page_url = mapping.page_url

            if mapping.warnings:
                for w in mapping.warnings:
                    print(f"    ⚠ [{mapping.confidence_tier().upper()}] {w}")

    return ts_lines, mappings, context


def generate_test_file(
    feature:     Dict[str, Any],
    output_path: str,
    project_key: str = "",
) -> List[StepMapping]:
    """
    Build a Playwright TypeScript spec file from a parsed Gherkin feature.
    Fully self-contained — works with any .feature file regardless of which
    pipeline run, Jira project, URL, or LLM produced it.

    project_key is passed explicitly — not read from the module global.
    Qdrant/LLM enrichment is used if available; rule-based mapping works standalone.
    """
    all_mappings: List[StepMapping] = []
    title_counts: Dict[str, int]   = {}

    pk           = project_key or PROJECT_KEY
    dom_coll     = collection_name_for(pk, "ui_memory") if pk else DOM_COLLECTION
    feature_name = escape_typescript_string(feature["name"])

    lines: List[str] = [
        "import { test, expect } from '@playwright/test';",
        "import { BasePage } from '../_bmad/BasePage';",
        "",
        f'test.describe("{feature_name}", () => {{',
        "",
        "    let basePage: BasePage;",
        "",
        "    test.beforeEach(async ({ page }) => {",
        f'        basePage = new BasePage(page, "{pk}");',
        "        await basePage.initialize();",
    ]

    # Seed context — carries project_key and dom_collection through all steps
    seed_context = StepContext(
        current_page_url = "",
        project_key      = pk,
        dom_collection   = dom_coll,
    )

    if feature["background"]:
        bg_lines, bg_mappings, bg_context = _map_steps(
            feature["background"]["steps"], initial_context=seed_context
        )
        lines.extend(bg_lines)
        all_mappings.extend(bg_mappings)
        seed_context = bg_context   # carry URL state forward

    lines += ["    });", ""]

    for scenario in feature["scenarios"]:
        tags_list   = scenario.get("tags", [])
        tags_suffix = f" {' '.join(tags_list)}" if tags_list else ""
        raw_title   = f"{scenario['name']}{tags_suffix}"
        seen_count  = title_counts.get(raw_title, 0)
        title_counts[raw_title] = seen_count + 1
        if seen_count:
            raw_title = f"{raw_title} [{seen_count + 1}]"
        title = escape_typescript_string(raw_title)

        lines.append(f'    test("{title}", async ({{ page }}) => {{')
        sc_lines, sc_mappings, _ = _map_steps(
            scenario["steps"], initial_context=seed_context
        )
        lines.extend(sc_lines)
        all_mappings.extend(sc_mappings)
        lines += ["    });", ""]

    lines.append("});")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"✓ Generated test file: {output_path}")
    return all_mappings


# ══════════════════════════════════════════════════════════════════════════════
# §10  FEATURE-FILE PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_feature_file(feature_path: str) -> Dict[str, Any]:
    """
    Parse a Gherkin .feature file.
    Handles Scenario Outlines by expanding Examples rows into concrete
    scenarios with unique names to prevent Playwright duplicate-title errors.
    """
    with open(feature_path, "r") as fh:
        content = fh.read()

    feature: Dict[str, Any] = {
        "name": "", "description": "", "scenarios": [], "background": None
    }

    lines:            List[str]            = content.split("\n")
    current_section:  Optional[str]        = None
    current_scenario: Optional[Dict]       = None
    pending_tags:     List[str]            = []
    in_examples:      bool                 = False
    examples_headers: List[str]            = []
    examples_rows:    List[Dict[str, str]] = []

    def _flush_outline(scenario, headers, rows):
        expanded = []
        for row in rows:
            suffix_parts = [f"{k}={v}" for k, v in row.items() if v]
            suffix       = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
            concrete: Dict[str, Any] = {
                "name":  scenario["name"] + suffix,
                "tags":  list(scenario["tags"]),
                "steps": [],
            }
            for step in scenario["steps"]:
                subst = step["text"]
                for col, val in row.items():
                    subst = subst.replace(f"<{col}>", val)
                concrete["steps"].append({"keyword": step["keyword"], "text": subst})
            expanded.append(concrete)
        return expanded

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("Feature:"):
            feature["name"] = stripped.replace("Feature:", "").strip()
            current_section = "feature"
            in_examples     = False

        elif stripped.startswith("Background:"):
            feature["background"] = {"steps": []}
            current_section = "background"
            in_examples     = False

        elif stripped.startswith("@"):
            # FIX 7: deduplicate tag tokens — the feature file may emit the
            # same tag line twice (e.g. @SCRUM-70 @negative @AC1 appears on
            # two consecutive lines).  Normalise to a set per scenario.
            new_tags = stripped.split()
            if current_section == "feature":
                existing = set(feature.get("tags", []))
                feature.setdefault("tags", []).extend(
                    t for t in new_tags if t not in existing
                )
            elif current_section == "scenario" and current_scenario:
                existing = set(current_scenario["tags"])
                current_scenario["tags"].extend(
                    t for t in new_tags if t not in existing
                )
            else:
                existing = set(pending_tags)
                pending_tags.extend(t for t in new_tags if t not in existing)

        elif stripped.startswith("Scenario Outline:") or stripped.startswith("Scenario:"):
            if current_scenario and current_scenario.get("is_outline") and examples_rows:
                expanded = _flush_outline(current_scenario, examples_headers, examples_rows)
                feature["scenarios"].pop()
                feature["scenarios"].extend(expanded)

            is_outline       = stripped.startswith("Scenario Outline:")
            current_scenario = {
                "name":       stripped.split(":", 1)[1].strip(),
                "tags":       list(pending_tags),
                "steps":      [],
                "is_outline": is_outline,
            }
            pending_tags     = []
            examples_headers = []
            examples_rows    = []
            in_examples      = False
            feature["scenarios"].append(current_scenario)
            current_section = "scenario"

        elif stripped.startswith("Examples:"):
            in_examples = True

        elif in_examples and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not examples_headers:
                examples_headers = cells
            else:
                examples_rows.append(dict(zip(examples_headers, cells)))

        elif stripped.startswith(("Given ", "When ", "Then ", "And ", "But ")):
            in_examples = False
            step = {
                "keyword": stripped.split()[0],
                "text":    stripped.split(" ", 1)[1] if " " in stripped else stripped,
            }
            if current_section == "background" and feature["background"]:
                feature["background"]["steps"].append(step)
            elif current_section == "scenario" and current_scenario:
                current_scenario["steps"].append(step)

    if current_scenario and current_scenario.get("is_outline") and examples_rows:
        expanded = _flush_outline(current_scenario, examples_headers, examples_rows)
        feature["scenarios"].pop()
        feature["scenarios"].extend(expanded)

    return feature


# ══════════════════════════════════════════════════════════════════════════════
# §11  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global PROJECT_KEY, REQUIREMENTS_COLLECTION, DOM_COLLECTION

    parser = argparse.ArgumentParser(
        description="Generate Playwright step definitions from Gherkin feature files"
    )
    parser.add_argument("--project",  required=True,
                        help="Project key in original form, e.g. SCRUM-70")
    parser.add_argument("--feature",  help="Path to .feature file (optional)")
    parser.add_argument("--coverage-report", dest="coverage_report", default="",
                        help="Path to write JSON coverage report (optional)")
    args = parser.parse_args()

    # PROJECT_KEY stays in its original hyphenated form — used in Qdrant filters.
    # Collection names are sanitised separately.
    PROJECT_KEY             = args.project
    REQUIREMENTS_COLLECTION = collection_name_for(PROJECT_KEY, "requirements")
    DOM_COLLECTION          = collection_name_for(PROJECT_KEY, "ui_memory")

    print("=" * 60)
    print("Step 2.5: Step Definition Generator (TEA) v2")
    print("=" * 60)
    print(f"Project Key (raw)  : {PROJECT_KEY}  ← used as Qdrant filter value")
    print(f"DOM Collection     : {DOM_COLLECTION}  ← sanitised for Qdrant")
    print(f"Features Directory : {FEATURES_DIR}")
    print(f"Steps Directory    : {STEPS_DIR}")
    if BASE_URL:
        print(f"BASE_URL           : {BASE_URL}/")
    else:
        print("BASE_URL           : (not set — crawl DOM into Qdrant for nav fallback)")

    # [1/5] Parse feature file
    feature_path = args.feature
    if not feature_path:
        candidate = os.path.join(FEATURES_DIR, f"{PROJECT_KEY}.feature")
        if os.path.exists(candidate):
            feature_path = candidate
        else:
            print(f"Error: No feature file found for project {PROJECT_KEY}")
            print("Please run quality_alignment.py first or specify --feature path")
            return

    print(f"\n[1/5] Reading feature file: {feature_path}")
    feature = parse_feature_file(feature_path)
    print(f"  ✓ Found {len(feature['scenarios'])} scenarios")

    # [2/5] Connect to Qdrant
    print("\n[2/5] Connecting to Qdrant memory…")
    from qdrant_client import QdrantClient
    client = QdrantClient(url=QDRANT_URL)
    try:
        collections = client.get_collections().collections
        dom_exists  = any(c.name == DOM_COLLECTION for c in collections)
        if dom_exists:
            print(f"  ✓ DOM collection '{DOM_COLLECTION}' found")
        else:
            print(f"  ⚠ DOM collection '{DOM_COLLECTION}' not found — confidence will be lower")

        # Verify project_key filter works — warn if 0 results
        if dom_exists:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            try:
                sample, _ = client.scroll(
                    collection_name=DOM_COLLECTION,
                    scroll_filter=Filter(
                        must=[FieldCondition(key="project_key",
                                             match=MatchValue(value=PROJECT_KEY))]
                    ),
                    limit=3,
                    with_payload=True,
                )
                if sample:
                    print(f"  ✓ project_key filter '{PROJECT_KEY}' matches "
                          f"{len(sample)}+ DOM points — URL resolution will work")
                else:
                    print(f"  ⚠ project_key filter '{PROJECT_KEY}' matched 0 DOM points")
                    print(f"    → Re-run vectorize_and_upload_v2.py --project {PROJECT_KEY}")
                    print(f"      to repopulate with the corrected project_key")
            except Exception:
                pass
    except Exception as exc:
        print(f"  ⚠ Could not connect to Qdrant: {exc}")

    # [3/5] Step count summary
    print("\n[3/5] Mapping Gherkin steps to Playwright actions…")
    bg_steps           = feature["background"]["steps"] if feature["background"] else []
    all_scenario_steps = [s for sc in feature["scenarios"] for s in sc["steps"]]
    total_steps        = len(bg_steps) + len(all_scenario_steps)
    print(f"  Background steps  : {len(bg_steps)}")
    print(f"  Scenario steps    : {len(all_scenario_steps)}")
    print(f"  Total steps       : {total_steps}")
    print(f"  Scenarios (after Outline expansion): {len(feature['scenarios'])}")

    # [4/5] Generate TypeScript test file
    # Output filename uses RAW_PROJECT_KEY so it matches the feature file name
    print("\n[4/5] Generating TypeScript test file…")
    output_path  = os.path.join(STEPS_DIR, f"{PROJECT_KEY}.spec.ts")
    all_mappings = generate_test_file(feature, output_path, project_key=PROJECT_KEY)

    # [5/5] Coverage report
    print("\n[5/5] Computing coverage report…")
    coverage_path = (
        args.coverage_report
        or os.path.join(STEPS_DIR, f"{PROJECT_KEY}_coverage.json")
    )
    report = generate_coverage_report(all_mappings, output_path=coverage_path)
    _print_coverage_summary(report)

    print("\n" + "=" * 60)
    print("✓ Step 2.5: Step Definition Generator v2 completed!")
    print(f"  Generated test file   : {output_path}")
    print(f"  Coverage report       : {coverage_path}")
    print(f"  Total scenarios       : {len(feature['scenarios'])}")
    print(f"  Total steps           : {total_steps}")
    sc = report["step_coverage"]
    print(f"  Step coverage         : {sc['coverage_pct']}%")
    print("=" * 60)


if __name__ == "__main__":
    main()