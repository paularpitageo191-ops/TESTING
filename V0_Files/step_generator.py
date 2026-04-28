#!/usr/bin/env python3
"""
Step 2.5: Step Definition Generator (TEA Architecture)
Reads .feature files and generates Playwright TypeScript step definitions.
Uses Qdrant memory to map Gherkin steps to basePage.smartAction() calls.

Fix log
-------
* parse_feature_file / _flush_outline: when expanding a Scenario Outline,
  the expanded scenario name now appends the Example row values so every
  concrete scenario gets a UNIQUE title.  Without this, two rows that share
  the same Outline title produce duplicate test titles and Playwright refuses
  to run:

      Error: duplicate test title "... Multiple invalid login attempts ..."
      first declared at :33, again at :39

* resolve_url_from_qdrant: uses _qdrant_rest_search() (HTTP POST) instead of
  qdrant_client.search() which was removed in qdrant-client ≥ 1.7.

* generate_typescript_step: no static toHaveURL() emission — all verify/click
  steps become smartAction() calls handled at runtime by BasePage.
"""

import os
import re
import json
import argparse
import requests
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
QDRANT_URL:  str = os.getenv("QDRANT_URL",  "http://localhost:6333")
BASE_URL:    str = os.getenv("BASE_URL",    "").rstrip("/")
VECTOR_SIZE: int = 1024

from llm_gateway import get_llm_gateway

PROJECT_KEY:             str = ""
REQUIREMENTS_COLLECTION: str = ""
DOM_COLLECTION:          str = ""

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
FEATURES_DIR = os.path.join(PROJECT_ROOT, "tests", "features")
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")
BOMAD_DIR    = os.path.join(PROJECT_ROOT, "tests", "_bmad")


# ── Embedding + Qdrant helpers ─────────────────────────────────────────────────

def generate_embedding(text: str) -> List[float]:
    """Generate embedding vector via LLM Gateway."""
    return get_llm_gateway().generate_embedding(text)


def _qdrant_rest_search(
    collection: str,
    vector: List[float],
    limit: int = 5,
    payload_filter: Optional[Dict] = None,
) -> List[Dict]:
    """
    Vector-similarity search via Qdrant REST API (HTTP POST).

    Bypasses qdrant_client.search() which was removed in ≥ 1.7.x and is
    absent in the currently installed 1.12.x, avoiding:
        'QdrantClient' object has no attribute 'search'
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


def resolve_url_from_qdrant(step_text: str, dom_collection: str) -> str:
    """
    Semantic-search Qdrant ui_memory for a page URL matching the step intent.

    The DOM crawler stores a `url` field in every point payload (added by the
    fixed vectorize_and_upload.py).  We embed the step text, search for the
    closest match, and return the first hit whose payload contains a real
    http(s) URL.
    """
    if not dom_collection:
        return ""

    vector = generate_embedding(step_text)
    if not vector:
        return ""

    hits = _qdrant_rest_search(
        collection=dom_collection,
        vector=vector,
        limit=5,
        payload_filter={
            "must": [{"key": "project_key", "match": {"value": PROJECT_KEY}}]
        },
    )

    for hit in hits:
        payload = hit.get("payload") or {}
        url = (
            payload.get("url")
            or payload.get("details", {}).get("url")
            or ""
        )
        if url.startswith(("http://", "https://")):
            print(f"  ↳ Qdrant URL match (score={hit.get('score', 0):.2f}): {url}")
            return url

    return ""


def resolve_url_from_intent(step_text: str, dom_collection: str = "") -> str:
    """
    Resolve a navigation URL from a Gherkin step with no literal URL.

    Resolution order (site-agnostic, no hardcoded paths):
      1. Qdrant ui_memory semantic search → real URL from the crawled DOM.
      2. BASE_URL root from .env → safe fallback.
      3. "" → caller emits a WARNING comment.
    """
    url = resolve_url_from_qdrant(step_text, dom_collection)
    if url:
        return url

    if BASE_URL:
        print(
            f"  ⚠ No Qdrant URL match for '{step_text}' "
            f"— falling back to BASE_URL root: {BASE_URL}/"
        )
        return BASE_URL + "/"

    return ""


def search_qdrant(collection_name: str, query_text: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Search a Qdrant collection and return scored payload dicts.
    Uses client.scroll() + manual cosine similarity (qdrant-client 1.12.x compatible).
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    client       = QdrantClient(url=QDRANT_URL)
    query_vector = generate_embedding(query_text)
    if not query_vector:
        return []

    project_filter = Filter(
        must=[FieldCondition(key="project_key", match=MatchValue(value=PROJECT_KEY))]
    )

    try:
        scroll_results, _ = client.scroll(
            collection_name=collection_name,
            scroll_filter=project_filter,
            limit=limit * 3,
            with_vectors=True,
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
        print(f"  Warning: Qdrant scroll error: {exc}")
        return []


# ── Feature-file parser ────────────────────────────────────────────────────────

def parse_feature_file(feature_path: str) -> Dict[str, Any]:
    """
    Parse a Gherkin .feature file.

    Handles Scenario Outlines by expanding Examples rows into individual
    concrete scenarios.

    KEY FIX: each expanded scenario gets a UNIQUE name by appending the
    row values to the Outline title.  Without this, two rows sharing the
    same Outline title produce duplicate test titles and Playwright refuses
    to run with:

        Error: duplicate test title "...", first declared at :33, again at :39

    Example:
      Outline title: "Multiple invalid login attempts - error message displayed"
      Row 1: attempt=1  →  "... (attempt=1)"
      Row 2: attempt=2  →  "... (attempt=2)"
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

    def _flush_outline(
        scenario: Dict, headers: List[str], rows: List[Dict[str, str]]
    ) -> List[Dict]:
        """
        Expand a Scenario Outline into one concrete scenario per Examples row.

        Each expanded scenario name = "<Outline title> (<col>=<val>, ...)"
        so that Playwright sees distinct test titles and doesn't crash with
        'duplicate test title'.
        """
        expanded = []
        for row in rows:
            # Build a short suffix from the row values so the title is unique.
            # E.g. row {"attempt": "1", "user": "locked_out_user"}
            #   → suffix "(attempt=1, user=locked_out_user)"
            suffix_parts = [f"{k}={v}" for k, v in row.items() if v]
            suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""

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
            if current_section == "feature":
                feature.setdefault("tags", []).extend(stripped.split())
            elif current_section == "scenario" and current_scenario:
                current_scenario["tags"].extend(stripped.split())
            else:
                pending_tags.extend(stripped.split())

        elif stripped.startswith("Scenario Outline:") or stripped.startswith("Scenario:"):
            if current_scenario and current_scenario.get("is_outline") and examples_rows:
                expanded = _flush_outline(current_scenario, examples_headers, examples_rows)
                feature["scenarios"].pop()
                feature["scenarios"].extend(expanded)

            is_outline = stripped.startswith("Scenario Outline:")
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

    # Flush trailing Scenario Outline at EOF
    if current_scenario and current_scenario.get("is_outline") and examples_rows:
        expanded = _flush_outline(current_scenario, examples_headers, examples_rows)
        feature["scenarios"].pop()
        feature["scenarios"].extend(expanded)

    return feature


# ── Step mapping ───────────────────────────────────────────────────────────────

def map_step_to_action(step_text: str, step_keyword: str) -> Dict[str, Any]:
    """Map a Gherkin step to a semantic action dict via LLM + Qdrant."""
    gateway    = get_llm_gateway()
    llm_result = gateway.analyze_gherkin_step(step_text, step_keyword)

    semantic_matches = search_qdrant(DOM_COLLECTION, step_text, limit=3)
    best_match = semantic_matches[0] if semantic_matches else {}
    payload    = best_match.get("payload", {}) if best_match else {}
    details    = payload.get("details", {})     if payload    else {}

    return {
        "action_type":    llm_result.get("action_type", "unknown"),
        "intent":         step_text,
        "value":          llm_result.get("value", ""),
        "confidence":     max(
            llm_result.get("confidence", 0.0),
            best_match.get("score", 0.0),
        ),
        "reasoning":      llm_result.get("reasoning", ""),
        "semantic_match": {
            "text":         payload.get("text", ""),
            "element_type": payload.get("element_type", ""),
            "details":      details,
        },
    }


def escape_typescript_string(s: str) -> str:
    """Escape a Python string for embedding inside a TypeScript string literal."""
    if not s:
        return s
    s = s.replace("\\", "\\\\")
    s = s.replace('"',  '\\"')
    return s


# ── TypeScript code generation ─────────────────────────────────────────────────

def generate_smart_action_step(
    step_text: str, step_keyword: str, action_map: Dict[str, Any]
) -> str:
    """Return a smartAction(intent[, value]) TypeScript call."""
    value  = action_map.get("value", "") if action_map else ""
    intent = action_map.get("intent", step_text) if action_map else step_text

    escaped_intent = escape_typescript_string(intent or step_text)
    escaped_value  = escape_typescript_string(value) if value else ""

    if escaped_value:
        return f'    await basePage.smartAction("{escaped_intent}", "{escaped_value}");'
    return f'    await basePage.smartAction("{escaped_intent}");'


def generate_typescript_step(
    action_map: Dict[str, Any], step_keyword: str, step_text: str
) -> str:
    """
    Map one Gherkin step to one line of Playwright TypeScript.

    Navigation steps emit page.goto() using a URL resolved from:
      a) LLM extraction  b) literal URL in step text
      c) Qdrant ui_memory semantic search  d) BASE_URL fallback

    All other steps become smartAction() calls — BasePage handles
    self-healing and URL assertions at runtime.
    """
    action_type   = action_map.get("action_type", "unknown")
    value         = action_map.get("value", "")
    escaped_step  = escape_typescript_string(step_text)
    escaped_value = escape_typescript_string(value) if value else ""

    # ── 1. NAVIGATION ─────────────────────────────────────────────────────────
    if action_type == "navigate":
        url = action_map.get("target_selector") or value

        if not url or not url.startswith(("http://", "https://")):
            m = re.search(r'https?://[^\s"\']+', step_text)
            url = m.group(0).rstrip(',;:!?)"\'') if m else ""

        if not url or not url.startswith(("http://", "https://")):
            url = resolve_url_from_intent(step_text, DOM_COLLECTION)

        if url and url.startswith(("http://", "https://")):
            return f'        await basePage.page.goto("{escape_typescript_string(url)}");'

        return (
            f'        await basePage.smartAction("{escaped_step}"); '
            f'// WARNING: no URL resolved — set BASE_URL in .env or crawl DOM into Qdrant'
        )

    # ── 2. FILL ────────────────────────────────────────────────────────────────
    if action_type == "smartFill":
        return f'        await basePage.smartAction("{escaped_step}", "{escaped_value}");'

    # ── 3. CLICK / VERIFY → smartAction ───────────────────────────────────────
    if action_type in ("smartClick", "verifyText"):
        return f'        await basePage.smartAction("{escaped_step}");'

    # ── 4. FALLBACK ───────────────────────────────────────────────────────────
    return f'        await basePage.smartAction("{escaped_step}"); // TEA Fallback for {action_type}'


# ── Test-file builder ──────────────────────────────────────────────────────────

def generate_test_file(feature: Dict[str, Any], output_path: str) -> None:
    """Generate a site-agnostic Playwright TypeScript spec file."""

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
        f'        basePage = new BasePage(page, "{PROJECT_KEY}");',
        "        await basePage.initialize();",
    ]

    if feature["background"]:
        for step in feature["background"]["steps"]:
            action_map = map_step_to_action(step["text"], step["keyword"])
            ts_line    = generate_typescript_step(action_map, step["keyword"], step["text"])
            lines.append("    " + ts_line)

    lines += ["    });", ""]

    for scenario in feature["scenarios"]:
        tags_list   = scenario.get("tags", [])
        tags_suffix = f" {' '.join(tags_list)}" if tags_list else ""
        title       = escape_typescript_string(f"{scenario['name']}{tags_suffix}")

        lines.append(f'    test("{title}", async ({{ page }}) => {{')

        for step in scenario["steps"]:
            action_map = map_step_to_action(step["text"], step["keyword"])
            ts_line    = generate_typescript_step(action_map, step["keyword"], step["text"])
            lines.append("    " + ts_line)

        lines += ["    });", ""]

    lines.append("});")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"✓ Generated test file: {output_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    global PROJECT_KEY, REQUIREMENTS_COLLECTION, DOM_COLLECTION

    parser = argparse.ArgumentParser(
        description="Generate Playwright step definitions from Gherkin feature files"
    )
    parser.add_argument("--project", required=True, help="Project key (e.g. SCRUM-86)")
    parser.add_argument("--feature", help="Path to .feature file (optional)")
    args = parser.parse_args()

    PROJECT_KEY             = args.project
    REQUIREMENTS_COLLECTION = f"{PROJECT_KEY}_requirements"
    DOM_COLLECTION          = f"{PROJECT_KEY}_ui_memory"

    print("=" * 60)
    print("Step 2.5: Step Definition Generator (TEA)")
    print("=" * 60)
    print(f"Project Key       : {PROJECT_KEY}")
    print(f"Features Directory: {FEATURES_DIR}")
    print(f"Steps Directory   : {STEPS_DIR}")
    if BASE_URL:
        print(f"BASE_URL          : {BASE_URL}/")
    else:
        print("BASE_URL          : (not set — crawl DOM into Qdrant for nav fallback)")

    feature_path = args.feature
    if not feature_path:
        candidate = os.path.join(FEATURES_DIR, f"{PROJECT_KEY}.feature")
        if os.path.exists(candidate):
            feature_path = candidate
        else:
            print(f"Error: No feature file found for project {PROJECT_KEY}")
            print("Please run quality_alignment.py first or specify --feature path")
            return

    print(f"\n[1/4] Reading feature file: {feature_path}")
    feature = parse_feature_file(feature_path)
    print(f"  ✓ Found {len(feature['scenarios'])} scenarios")

    print("\n[2/4] Connecting to Qdrant memory...")
    from qdrant_client import QdrantClient
    client = QdrantClient(url=QDRANT_URL)
    try:
        collections = client.get_collections().collections
        dom_exists  = any(c.name == DOM_COLLECTION for c in collections)
        print(
            f"  ✓ DOM collection '{DOM_COLLECTION}' found"
            if dom_exists
            else f"  ⚠ DOM collection '{DOM_COLLECTION}' not found"
        )
    except Exception as exc:
        print(f"  ⚠ Could not connect to Qdrant: {exc}")

    print("\n[3/4] Mapping Gherkin steps to Playwright actions (preview)...")
    bg_steps           = feature["background"]["steps"] if feature["background"] else []
    all_scenario_steps = [s for sc in feature["scenarios"] for s in sc["steps"]]
    total_steps        = len(bg_steps) + len(all_scenario_steps)
    print(f"  Background steps  : {len(bg_steps)}")
    print(f"  Scenario steps    : {len(all_scenario_steps)}")
    print(f"  Total steps       : {total_steps}")
    print(f"  Scenarios (after Outline expansion): {len(feature['scenarios'])}")

    print("\n[4/4] Generating TypeScript test file...")
    output_path = os.path.join(STEPS_DIR, f"{PROJECT_KEY}.spec.ts")
    generate_test_file(feature, output_path)

    print("\n" + "=" * 60)
    print("✓ Step 2.5: Step Definition Generator completed!")
    print(f"  Generated test file: {output_path}")
    print(f"  Total scenarios    : {len(feature['scenarios'])}")
    print(f"  Total steps        : {total_steps}")
    print("=" * 60)


if __name__ == "__main__":
    main()