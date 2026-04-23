#!/usr/bin/env python3
"""
Quality Alignment — Phase 3
============================
Reads the outputs produced by the previous three pipeline steps and uses the
LLM Gateway to generate a detailed Gherkin feature file from real requirements.

Pipeline context
────────────────
  Phase 0  jira_sync_agent.py      →  docs/inbox/*.json
                                       Qdrant: {PROJECT_KEY}_requirements
  Phase 1  vectorize_and_upload.py →  Qdrant: {PROJECT_KEY}_requirements
                                       docs/{PROJECT_KEY}_prd.md
  Phase 2  dom_capture.py          →  docs/live_dom_elements_*.json
                                       Qdrant: {PROJECT_KEY}_ui_memory

  Phase 3  quality_alignment.py   →  tests/features/{PROJECT_KEY}.feature
                                       docs/quality_alignment_report_{PROJECT_KEY}.json

Fix log
-------
* _GHERKIN_SYSTEM prompt: added explicit rule 9 — every Scenario Outline
  MUST have UNIQUE scenario names across all Examples rows.  The LLM was
  previously generating multiple rows with identical Outline titles, which
  after expansion in parse_feature_file produced duplicate Playwright test
  titles and an immediate crash:

      Error: duplicate test title "... Multiple invalid login attempts ..."
      first declared at :33, again at :39

  The new rule requires the LLM to either (a) include the key column value
  in the Outline title as a <placeholder>, or (b) write separate Scenarios
  instead of a Scenario Outline when every row would have the same title.

* vectorize_and_upload_dom: now stores a top-level `url` field on every
  DOM point payload so that BasePage.resolveUrlFromQdrant() can return the
  exact page URL instead of always falling back to BASE_URL root.

* search_collection: falls back to scroll + manual filter when
  qdrant_client.search() is unavailable (removed in ≥ 1.7.x / 1.12.x).
"""

import csv
import glob
import json
import os
import re
import argparse
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
VECTOR_SIZE     = 1024

DOCS_DIR      = "docs"
INBOX_DIR     = os.path.join(DOCS_DIR, "inbox")
SELECTORS_CSV = os.path.join(DOCS_DIR, "selectors.csv")

PROJECT_KEY             = ""
REQUIREMENTS_COLLECTION = ""
DOM_COLLECTION          = ""


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
    return get_llm_gateway().generate_embedding(text)


def scroll_all_points(collection: str, limit: int = 200) -> List[Dict]:
    """Return up to `limit` points from `collection` that belong to PROJECT_KEY."""
    client = _qdrant()
    project_filter = {
        "must": [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
    }
    try:
        results, _ = client.scroll(
            collection_name=collection,
            scroll_filter=project_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [r.payload for r in results]
    except Exception as exc:
        print(f"  ⚠ scroll with filter failed ({exc}), trying unfiltered…")
        try:
            results, _ = client.scroll(
                collection_name=collection,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            return [r.payload for r in results
                    if r.payload.get("project_key") == PROJECT_KEY]
        except Exception as exc2:
            print(f"  ✗ Could not read from {collection}: {exc2}")
            return []


def search_collection(collection: str, query_text: str, limit: int = 10) -> List[Dict]:
    """
    Semantic search in a collection filtered to PROJECT_KEY.

    Uses the Qdrant REST API directly (HTTP POST) to avoid the
    'QdrantClient has no attribute search' error on qdrant-client ≥ 1.7.
    """
    vector = generate_embedding(query_text)
    if not vector:
        return []

    try:
        resp = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector":       vector,
                "limit":        limit,
                "with_payload": True,
                "filter": {
                    "must": [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
                },
            },
            timeout=15,
        )
        if not resp.ok:
            print(f"  ⚠ Qdrant search {resp.status_code}: {resp.text[:80]}")
            return []
        hits = resp.json().get("result", [])
        return [
            {"text": h["payload"].get("text", ""),
             "score": h.get("score", 0),
             "payload": h["payload"]}
            for h in hits
            if h.get("payload", {}).get("project_key") == PROJECT_KEY
        ]
    except Exception as exc:
        print(f"  ⚠ search failed: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Phase 0/1 output readers
# ══════════════════════════════════════════════════════════════════════════════

def load_requirements_from_qdrant() -> List[str]:
    if not _collection_exists(REQUIREMENTS_COLLECTION):
        print(f"  ⚠ Collection '{REQUIREMENTS_COLLECTION}' not found.")
        return []

    payloads = scroll_all_points(REQUIREMENTS_COLLECTION, limit=300)
    seen, texts = set(), []
    for p in payloads:
        t = (p.get("text") or p.get("content") or "").strip()
        if t and t not in seen:
            seen.add(t)
            texts.append(t)

    print(f"  ✓ Loaded {len(texts)} requirement text(s) from Qdrant")
    return texts


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

    prefix  = PROJECT_KEY.split("-")[0]
    project = [i for i in items if str(i.get("key", "")).startswith(prefix)]
    print(f"  ✓ Loaded {len(project)} issue(s) from inbox")
    return project


def load_prd_text() -> str:
    for path in [
        os.path.join(DOCS_DIR, f"{PROJECT_KEY}_prd.md"),
        os.path.join(DOCS_DIR, f"{PROJECT_KEY}_requirements.md"),
        os.path.join(DOCS_DIR, "prd.md"),
        os.path.join(DOCS_DIR, "requirements", f"{PROJECT_KEY}_PRD.md"),
    ]:
        if os.path.exists(path):
            text = open(path).read().strip()
            if text:
                print(f"  ✓ PRD loaded from {path}")
                return text
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 output readers
# ══════════════════════════════════════════════════════════════════════════════

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
        return data
    except Exception as exc:
        print(f"  ✗ Could not read DOM file: {exc}")
        return {}


def dom_summary_for_llm(dom_data: Dict, max_elements: int = 60) -> str:
    """Build a concise LLM-readable summary of the live DOM."""
    lines = []

    def add(kind: str, items: List[Dict], fields: List[str]):
        for el in items[:max_elements // 4]:
            parts = [f"[{kind}]"]
            for f in fields:
                v = (el.get(f) or "").strip()
                if v:
                    parts.append(f"{f}={v!r}")
            lines.append("  " + " ".join(parts))

    add("INPUT",    dom_data.get("input_elements",    []), ["type", "placeholder", "label", "name", "id"])
    add("BUTTON",   dom_data.get("button_elements",   []), ["text", "label", "id", "className"])
    add("DROPDOWN", dom_data.get("dropdown_elements", []), ["name", "label", "options"])
    add("LINK",     dom_data.get("link_elements",     []), ["text", "href"])

    seen = set(l[:60] for l in lines)
    for el in dom_data.get("all_interactive_elements", [])[:max_elements]:
        tag  = el.get("tagName", "").upper()
        text = (el.get("text") or el.get("placeholder") or "").strip()[:50]
        key  = f"{tag}:{text}"
        if key not in seen and text:
            seen.add(key)
            lines.append(f"  [{tag}] text={text!r}")

    return "\n".join(lines) if lines else "  (no DOM elements captured)"


# ══════════════════════════════════════════════════════════════════════════════
# DOM vectorisation — stores `url` field so navigation resolves correctly
# ══════════════════════════════════════════════════════════════════════════════

def vectorize_and_upload_dom(dom_data: Dict):
    """
    Upload / refresh DOM vectors in the project-specific Qdrant collection.

    CRITICAL: every point payload now includes a top-level `url` field so
    that BasePage.resolveUrlFromQdrant() can return the real page URL instead
    of always falling back to BASE_URL root.
    """
    client = _qdrant()

    if not _collection_exists(DOM_COLLECTION):
        client.create_collection(
            collection_name=DOM_COLLECTION,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"  ✓ Created collection '{DOM_COLLECTION}'")

    # Resolve page URL from DOM data or BASE_URL env var.
    page_url = (
        dom_data.get("url")
        or dom_data.get("page_url")
        or dom_data.get("base_url")
        or (BASE_URL + "/" if BASE_URL else "")
    )
    if page_url:
        print(f"  ✓ Page URL for DOM points: {page_url}")
    else:
        print("  ⚠ No page URL found in DOM data and BASE_URL is unset")

    element_groups = [
        ("input",       dom_data.get("input_elements",    []), ["type", "placeholder", "label", "name"]),
        ("button",      dom_data.get("button_elements",   []), ["text", "label", "id"]),
        ("dropdown",    dom_data.get("dropdown_elements", []), ["name", "label"]),
        ("interactive", dom_data.get("all_interactive_elements", []), ["tagName", "text", "placeholder", "role"]),
    ]

    points = []
    for kind, items, fields in element_groups:
        for i, el in enumerate(items):
            parts = [f"{k}={el.get(k,'')}" for k in fields if el.get(k)]
            text  = f"{kind} " + " ".join(parts)
            points.append({"id": f"{kind}_{i}", "text": text, "details": el})

    uploaded = 0
    for i, p in enumerate(points):
        try:
            vec = generate_embedding(p["text"])
            if not vec:
                continue
            client.upsert(
                collection_name=DOM_COLLECTION,
                points=[PointStruct(
                    id=abs(hash(p["id"] + PROJECT_KEY)) % (2**31),
                    vector=vec,
                    payload={
                        "source":      "dom_capture",
                        "text":        p["text"],
                        "project_key": PROJECT_KEY,
                        "url":         page_url,      # ← THE FIX
                        "details":     p["details"],
                        "metadata": {"created_at": datetime.now().isoformat()},
                    },
                )],
            )
            uploaded += 1
            if (i + 1) % 10 == 0:
                print(f"    [{i+1}/{len(points)}] uploaded…")
        except Exception as exc:
            print(f"    ⚠ {exc}")

    print(f"  ✓ Uploaded {uploaded}/{len(points)} DOM vectors → '{DOM_COLLECTION}'")
    print(f"  ✓ Each point includes url='{page_url}'")


# ══════════════════════════════════════════════════════════════════════════════
# LLM-driven Gherkin generation
# ══════════════════════════════════════════════════════════════════════════════

_GHERKIN_SYSTEM = """You are a senior QA engineer and BDD specialist.
Your task: write a complete, detailed Gherkin feature file from the
requirements and UI context provided.

Rules:
1. Use standard Gherkin keywords: Feature, Background, Scenario,
   Scenario Outline, Given, When, Then, And, But, Examples.
2. Cover EVERY acceptance criterion with at least one scenario.
3. Include both happy-path AND negative / edge-case scenarios.
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
     Example: "Login attempt <attempt_number> shows error" (not
     "Login attempt shows error" for every row).
   - If all rows would produce the same title, write separate Scenarios
     instead of a Scenario Outline.
   - Duplicate titles cause an immediate Playwright crash — avoid them.
"""


def build_gherkin_prompt(
    requirement_texts: List[str],
    inbox_issues: List[Dict],
    prd_text: str,
    dom_summary: str,
    app_url: str,
) -> str:
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
    elif requirement_texts:
        req_block = ("=== Requirements (from Qdrant) ===\n" +
                     "\n".join(f"- {t[:200]}" for t in requirement_texts[:30]))

    if prd_text:
        req_block += f"\n\n=== PRD (from vectorize_and_upload.py) ===\n{prd_text[:3000]}"

    if not req_block.strip():
        req_block = ("No structured requirements found in Qdrant or inbox.\n"
                     "Generate general smoke-test scenarios based on the DOM below.")

    dom_block = (f"=== Live DOM Elements (from dom_capture.py — {app_url}) ===\n"
                 f"{dom_summary}")

    return (
        f"Generate a complete Gherkin feature file for project {PROJECT_KEY}.\n\n"
        f"{req_block}\n\n"
        f"{dom_block}\n\n"
        f"Application URL: {app_url}\n"
        f"Project key for tags: {PROJECT_KEY}\n\n"
        f"REMINDER: Every scenario and every Scenario Outline expansion row "
        f"MUST have a unique title (Rule 9).\n\n"
        f"Write the feature file now:"
    )


def parse_gherkin_output(raw: str) -> str:
    """Strip markdown fences or preamble, returning clean Gherkin."""
    raw = re.sub(r'^```[a-z]*\n?', '', raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r'\n?```$',       '', raw.strip(), flags=re.MULTILINE)
    idx = raw.find("Feature:")
    if idx > 0:
        raw = raw[idx:]
    return raw.strip()


def generate_gherkin_via_llm(
    requirement_texts: List[str],
    inbox_issues: List[Dict],
    prd_text: str,
    dom_data: Dict,
) -> str:
    app_url     = (BASE_URL + "/") if BASE_URL else os.getenv("BASE_URL", "https://your-app.example.com/")
    dom_summary = dom_summary_for_llm(dom_data)
    prompt      = build_gherkin_prompt(
        requirement_texts, inbox_issues, prd_text, dom_summary, app_url)

    provider = os.getenv("LLM_PROVIDER", "ollama")
    print(f"  Calling LLM provider '{provider}' to write Gherkin…")

    gateway = get_llm_gateway()
    raw     = gateway.chat(prompt, system_prompt=_GHERKIN_SYSTEM, temperature=0.3)

    if not raw or not raw.strip():
        print("  ⚠ LLM returned empty response — using structural fallback")
        return _gherkin_fallback(requirement_texts, inbox_issues, app_url)

    gherkin = parse_gherkin_output(raw)

    if "Scenario" not in gherkin:
        print("  ⚠ LLM output does not look like Gherkin — using fallback")
        return _gherkin_fallback(requirement_texts, inbox_issues, app_url)

    count = len(re.findall(r'^\s*Scenario', gherkin, re.MULTILINE))
    print(f"  ✓ LLM generated {count} scenario(s)")
    return gherkin


def _gherkin_fallback(
    requirement_texts: List[str],
    inbox_issues: List[Dict],
    app_url: str,
) -> str:
    """Minimal structural Gherkin when the LLM is unavailable."""
    title = (inbox_issues[0].get("summary", f"{PROJECT_KEY} Acceptance Tests")
             if inbox_issues else f"{PROJECT_KEY} Acceptance Tests")
    lines = [
        f"Feature: {PROJECT_KEY}: {title}",
        f"  As a user of {app_url}",
        f"  I want to fulfil the acceptance criteria for {PROJECT_KEY}",
        "",
        "  Background:",
        f'    Given I am on the application at "{app_url}"',
        "",
    ]
    sources = inbox_issues or [
        {"key": PROJECT_KEY, "summary": t[:80]} for t in requirement_texts[:10]
    ]
    for i, src in enumerate(sources[:15], 1):
        key  = src.get("key", f"{PROJECT_KEY}-{i}")
        summ = src.get("summary", src.get("text", f"Requirement {i}"))[:80]
        lines += [
            f"  @{PROJECT_KEY} @generated",
            f"  Scenario: {summ} - test {i}",   # include index to guarantee uniqueness
            f"    Given the system is in the correct initial state",
            f"    When I perform the action described in {key}",
            f"    Then the expected outcome should occur",
            "",
        ]
    return "\n".join(lines)


def save_feature_file(gherkin: str) -> str:
    features_dir = os.path.join("tests", "features")
    os.makedirs(features_dir, exist_ok=True)
    path = os.path.join(features_dir, f"{PROJECT_KEY}.feature")
    with open(path, "w") as f:
        f.write(gherkin)
    print(f"  ✓ Feature file saved → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Cross-reference & drift
# ══════════════════════════════════════════════════════════════════════════════

def build_search_queries(requirement_texts: List[str], inbox_issues: List[Dict]) -> List[str]:
    queries = []
    for issue in inbox_issues[:15]:
        summ = issue.get("summary", "")
        if summ:
            queries.append(summ[:120])
        desc  = (issue.get("description") or "")[:200]
        first = re.split(r'[.!\n]', desc)[0].strip()
        if first and first not in queries:
            queries.append(first)
    for text in requirement_texts[:20]:
        first = re.split(r'[.!\n]', text)[0].strip()
        if first and first not in queries:
            queries.append(first)
    if not queries:
        queries = ["login", "form submission", "navigation", "error message", "button click"]
    return queries[:20]


def cross_reference_with_requirements(
    requirement_texts: List[str], inbox_issues: List[Dict]
) -> List[Dict]:
    print("\n  SEMANTIC CROSS-REFERENCE")

    if not _collection_exists(DOM_COLLECTION):
        print(f"  ⚠ DOM collection '{DOM_COLLECTION}' not found")
        return []

    queries = build_search_queries(requirement_texts, inbox_issues)
    results = []

    for query in queries:
        dom_hits = search_collection(DOM_COLLECTION, query, limit=5)
        req_hits = (search_collection(REQUIREMENTS_COLLECTION, query, limit=3)
                    if _collection_exists(REQUIREMENTS_COLLECTION) else [])
        results.append({
            "query":              query,
            "dom_elements_found": [{"text": h["text"][:100], "score": round(h["score"], 3)} for h in dom_hits],
            "requirements_found": [{"text": h["text"][:100], "score": round(h["score"], 3)} for h in req_hits],
            "match_count":        len(dom_hits),
        })
        print(f"    {'✓' if dom_hits else '✗'} '{query[:55]}'  →  {len(dom_hits)} DOM match(es)")

    return results


def identify_drift_dynamic(
    dom_data: Dict, requirement_texts: List[str], inbox_issues: List[Dict]
) -> List[Dict]:
    print("\n  DRIFT ANALYSIS")

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
        summ = issue.get("summary", "")
        if summ:
            words = [w.lower() for w in re.findall(r'\w+', summ)
                     if w.lower() not in stop and len(w) > 3]
            if words:
                concepts.append({"requirement": summ[:80], "keywords": words[:5], "source": issue.get("key", "inbox")})
    for text in requirement_texts[:15]:
        first = re.split(r'[.!\n]', text)[0].strip()[:80]
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


def validate_ui_alignment_dynamic(
    dom_data: Dict, requirement_texts: List[str], inbox_issues: List[Dict]
) -> Tuple[List[Dict], List[Dict]]:
    print("\n  VALIDATION WITH CONFIDENCE SCORING")

    requirements = []
    for issue in inbox_issues[:20]:
        summ = issue.get("summary", "")
        if summ:
            requirements.append(summ)
    requirements.extend(t[:80] for t in requirement_texts[:20] if t[:80] not in requirements)
    if not requirements:
        requirements = ["user interaction", "form input", "button action", "navigation", "verification"]

    elements = (
        dom_data.get("input_elements",    []) +
        dom_data.get("button_elements",   []) +
        dom_data.get("dropdown_elements", []) +
        dom_data.get("textarea_elements", [])
    )

    validation_results = []
    self_healing_items = []

    for el in elements:
        label = (el.get("label") or el.get("text") or el.get("placeholder") or "").strip()
        if not label:
            continue

        best_score, best_req = 0.0, ""
        for req in requirements:
            rw    = set(re.findall(r'\w+', req.lower()))
            lw    = set(re.findall(r'\w+', label.lower()))
            score = len(rw & lw) / max(len(rw), len(lw), 1)
            if score > best_score:
                best_score, best_req = score, req

        level = "HIGH" if best_score >= 0.6 else "MEDIUM" if best_score >= 0.3 else "LOW"
        print(f"    {'✓ HIGH' if level=='HIGH' else '⚠ MEDIUM' if level=='MEDIUM' else '✗ LOW'} "
              f"({best_score:.2f}): '{label[:40]}' → '{best_req[:45]}'")

        result = {
            "element_label":    label,
            "best_requirement": best_req,
            "confidence_data": {
                "composite_score":    round(best_score, 3),
                "confidence_level":   level,
                "needs_self_healing": best_score < 0.3,
            },
            "element": el,
        }
        validation_results.append(result)

        if best_score < 0.3:
            self_healing_items.append({
                "label":            label,
                "requirement":      best_req,
                "confidence_score": best_score,
                "recommendations": [
                    f"Label '{label}' has low alignment with any known requirement.",
                    "Re-run dom_capture.py to refresh element metadata.",
                    "Check if element purpose is described in the Jira story.",
                ],
            })

    return validation_results, self_healing_items


def check_selectors_coverage(dom_data: Dict) -> List[Dict]:
    if not os.path.exists(SELECTORS_CSV):
        return []

    all_dom_text = " ".join(
        str(el) for el in dom_data.get("all_interactive_elements", [])
    ).lower()

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
) -> str:
    report = {
        "project_key":  PROJECT_KEY,
        "generated_at": datetime.now().isoformat(),
        "feature_file": feature_path,
        "summary": {
            "cross_reference_queries": len(cross_ref),
            "requirements_checked":    len(drift),
            "requirements_met":        sum(1 for d in drift if d["status"] == "PRESENT"),
            "requirements_missing":    sum(1 for d in drift if d["status"] == "MISSING"),
            "validations_total":       len(validation),
            "high_confidence":         sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "HIGH"),
            "medium_confidence":       sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "MEDIUM"),
            "low_confidence":          sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "LOW"),
            "self_healing_needed":     len(self_healing),
            "selectors_checked":       len(coverage),
            "selectors_found":         sum(1 for c in coverage if c["found"]),
        },
        "cross_reference":   cross_ref,
        "drift_analysis":    drift,
        "validation":        validation,
        "self_healing":      self_healing,
        "selector_coverage": coverage,
    }

    path = os.path.join(DOCS_DIR, f"quality_alignment_report_{PROJECT_KEY}.json")
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n  ✓ Report saved → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global PROJECT_KEY, REQUIREMENTS_COLLECTION, DOM_COLLECTION

    parser = argparse.ArgumentParser(
        description="Quality Alignment — generate LLM Gherkin from pipeline outputs"
    )
    parser.add_argument("--project", required=True,
                        help="Project key (e.g. SCRUM-86)")
    args = parser.parse_args()

    PROJECT_KEY             = args.project
    REQUIREMENTS_COLLECTION = f"{PROJECT_KEY}_requirements"
    DOM_COLLECTION          = f"{PROJECT_KEY}_ui_memory"

    print("=" * 60)
    print("Phase 3: Quality Alignment")
    print("=" * 60)
    print(f"  Project:           {PROJECT_KEY}")
    print(f"  Requirements coll: {REQUIREMENTS_COLLECTION}")
    print(f"  DOM coll:          {DOM_COLLECTION}")
    print(f"  LLM provider:      {os.getenv('LLM_PROVIDER', 'ollama')}")

    print("\n[1/7] Loading Phase 0/1 outputs (requirements)…")
    requirement_texts = load_requirements_from_qdrant()
    inbox_issues      = load_requirements_from_inbox()
    prd_text          = load_prd_text()

    if not requirement_texts and not inbox_issues:
        print("\n  ⚠ No requirements found. Gherkin will be generated from DOM only.")

    print("\n[2/7] Loading Phase 2 outputs (DOM)…")
    dom_data = load_dom_data()

    if dom_data:
        print("\n[3/7] Uploading DOM vectors to Qdrant…")
        vectorize_and_upload_dom(dom_data)
    else:
        print("\n[3/7] Skipping DOM vectorisation (no DOM data)")

    print("\n[4/7] Generating Gherkin feature file via LLM…")
    gherkin      = generate_gherkin_via_llm(requirement_texts, inbox_issues, prd_text, dom_data)
    feature_path = save_feature_file(gherkin)

    print("\n[5/7] Cross-referencing requirements with live DOM…")
    cross_ref = cross_reference_with_requirements(requirement_texts, inbox_issues)

    print("\n[6/7] Running drift analysis and confidence validation…")
    drift             = identify_drift_dynamic(dom_data, requirement_texts, inbox_issues)
    validation, heals = validate_ui_alignment_dynamic(dom_data, requirement_texts, inbox_issues)
    coverage          = check_selectors_coverage(dom_data)

    print("\n[7/7] Saving comprehensive report…")
    report_path = save_report(cross_ref, drift, validation, heals, coverage, feature_path)

    met     = sum(1 for d in drift if d["status"] == "PRESENT")
    missing = sum(1 for d in drift if d["status"] == "MISSING")
    high    = sum(1 for v in validation if v["confidence_data"]["confidence_level"] == "HIGH")

    print("\n" + "=" * 60)
    print("✓ Phase 3: Quality Alignment completed!")
    print(f"  Feature file:         {feature_path}")
    print(f"  Report:               {report_path}")
    print(f"  Requirements met:     {met}/{met + missing}")
    print(f"  High-conf matches:    {high}/{len(validation)}")
    print(f"  Self-healing flagged: {len(heals)}")
    if heals:
        print("\n  Self-healing items:")
        for h in heals[:5]:
            print(f"    • '{h['label'][:40]}' — {h['recommendations'][0][:60]}")
    print("=" * 60)


if __name__ == "__main__":
    main()