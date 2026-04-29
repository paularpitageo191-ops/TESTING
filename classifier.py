#!/usr/bin/env python3
"""
classifier.py — UC-3: True vs False Failure Classification
==========================================================
For every failed test in a run:

  Layer 1  Rule-based (fast, deterministic)
           Patterns that unambiguously indicate infrastructure/env issues.

  Layer 2  Embedding similarity (Qdrant)
           Embed the error message + stack trace and match against known
           failure clusters to surface recurring patterns.

  Layer 3  LLM (Ollama) — final arbitration
           When layers 1 & 2 disagree or are uncertain, the LLM reads
           the full error context and returns a structured verdict.

Output
──────
  Updates test_results.failure_class + rca_summary in DB.
  Spawns true_failure_rca.py or false_failure_rca.py per verdict.

Usage
─────
  python3 classifier.py --project SCRUM-70 --run-id SCRUM-70-20260427120000
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import subprocess
import datetime
from typing import Dict, List, Optional, Tuple
import sys
import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config
# --- ADD THIS IMPORT (top with others) ---
import uuid
load_dotenv()

AGENT_NAME = "classifier_v1"

QDRANT_URL  = os.getenv("QDRANT_URL",  "http://localhost:6333")
DEFAULT_DB  = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

# ── Classification constants ──────────────────────────────────────────────────

# Patterns that are ALWAYS false failures (infrastructure / env / selector)
FALSE_PATTERNS: List[str] = [
    r"timeouterror",
    r"strict mode violation",
    r"locator resolved to \d+ elements",
    r"net::err_",
    r"econnrefused",
    r"enotfound",
    r"ssl_error",
    r"navigation timeout",
    r"target closed",
    r"browser has been closed",
    r"context was destroyed",
    r"page\.goto.*timeout",
    r"waiting for locator",
    r"element is not enabled",
    r"element is not stable",
    r"no element found for intent",
    r"smartaction fill failed",
    r"smartaction failed",
    r"healed selector",
]

# Patterns that are ALWAYS true failures (business logic assertion)
TRUE_PATTERNS: List[str] = [
    r"assertionerror",
    r"tocontaintext.*expected",
    r"tobevisible.*expected.*visible.*received.*hidden",
    r"expect.*received.*expected",
    r"tobeenabled.*expected.*enabled.*received.*disabled",
    r"tohaveurl.*expected",
    r"tohavevalue.*expected",
    r"error: expect\(locator\)\.",
]

CONFIDENCE_THRESHOLD = 0.70   # minimum to trust rule/embedding verdict without LLM


# def _sanitize(name: str) -> str:
#     return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_')
def _sanitize(name: str) -> str:
    return name.replace("-", "_")


# ══════════════════════════════════════════════════════════════════════════════
# §1  LAYER 1 — RULE-BASED
# ══════════════════════════════════════════════════════════════════════════════

def rule_classify(error_msg: str, stack: str) -> Tuple[Optional[str], float]:
    """
    Returns ('true'|'false'|None, confidence).
    None means "undetermined — escalate to next layer".
    """
    haystack = (error_msg + " " + stack).lower()

    for pattern in FALSE_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "false", 0.92

    for pattern in TRUE_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            return "true", 0.90

    return None, 0.0


# ══════════════════════════════════════════════════════════════════════════════
# §2  LAYER 2 — EMBEDDING CLUSTER MATCH
# ══════════════════════════════════════════════════════════════════════════════

def _embed(text: str) -> List[float]:
    return embed(AGENT_NAME, text)



def _search_failure_clusters(project_key: str, vector: List[float]) -> List[Dict]:
    collection = _sanitize(f"{project_key}_failure_clusters")
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 3, "with_payload": True},
            timeout=10,
        )
        if r.ok:
            return r.json().get("result", [])
    except Exception:
        pass
    return []


def embedding_classify(
    project_key: str, error_msg: str, stack: str
) -> Tuple[Optional[str], float, str]:
    """
    Returns ('true'|'false'|None, confidence, matched_pattern_label).
    """
    text   = f"{error_msg}\n{stack}"[:2000]
    vector = _embed(text)
    if not vector:
        return None, 0.0, ""

    hits = _search_failure_clusters(project_key, vector)
    if not hits:
        return None, 0.0, ""

    top     = hits[0]
    score   = top.get("score", 0.0)
    payload = top.get("payload", {})
    label   = payload.get("failure_class", "")
    pattern = payload.get("pattern_label", "")

    if score >= CONFIDENCE_THRESHOLD and label in ("true", "false"):
        return label, round(score, 4), pattern

    return None, round(score, 4), pattern
  
# ── NEW: QDRANT UPSERT LOGIC ────────────────────────────────────────────────
def upsert_failure_vector(project_key: str, text: str, verdict: str, pattern: str):
    collection = _sanitize(f"{project_key}_failure_clusters")

    vector = _embed(text)
    print("    🔍 Vector size:", len(vector))

    if not vector:
        print("    ⚠ No embedding generated — skipping Qdrant upsert")
        return

    payload = {
        "failure_class": verdict,
        "pattern_label": pattern or "unknown",
        "text": text[:500]
    }

    try:
        r = requests.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            json={
                "points": [
                    {
                        "id": str(uuid.uuid4()),
                        "vector": vector,
                        "payload": payload
                    }
                ]
            },
            timeout=10
        )

        if r.ok:
            print("    ✓ Stored in Qdrant")
        else:
            print("    ❌ Qdrant error:", r.text)

    except Exception as e:
        print(f"    ⚠ Qdrant upsert failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# §3  LAYER 3 — LLM ARBITRATION
# ══════════════════════════════════════════════════════════════════════════════

def llm_classify(
    test_title:  str,
    error_msg:   str,
    stack:       str,
    l1_verdict:  Optional[str],
    l2_verdict:  Optional[str],
    l2_conf:     float,
    l2_pattern:  str,
) -> Tuple[str, str]:
    raw = call_llm(AGENT_NAME, prompt, system=system)
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        obj = json.loads(raw)
        verdict     = obj.get("verdict", "false")
        rca_summary = obj.get("rca_summary", "LLM classification.")
        return verdict, rca_summary
    except Exception:
        return "false", "Classification via fallback rules."


def load_failures(project_key: str, run_id: str, db_path: str) -> List[Dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT * FROM test_results
        WHERE project_key = ? AND run_id = ? AND status = 'failed'
        """,
        (project_key, run_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_classification(
    test_id:       int,
    failure_class: str,
    rca_summary:   str,
    db_path:       str,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE test_results SET failure_class=?, rca_summary=? WHERE id=?",
        (failure_class, rca_summary, test_id),
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# §5  DISPATCH RCA AGENTS
# ══════════════════════════════════════════════════════════════════════════════

def dispatch_rca(
    verdict:     str,
    project_key: str,
    test_record: Dict,
    db_path:     str,
) -> None:
    script = "true_failure_rca.py" if verdict == "true" else "false_failure_rca.py"
    cmd = [
        sys.executable, script,
        "--project",    project_key,
        "--test-id",    str(test_record["id"]),
        "--db",         db_path,
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),   # ← inherits QDRANT_URL, OLLAMA_HOST from Jenkins
        )
        print(f"    → Dispatched {script} for: {test_record['test_title'][:60]}")
    except FileNotFoundError:
        print(f"    ⚠ {script} not found")

# ══════════════════════════════════════════════════════════════════════════════
# §6  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-3 Classifier")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--run-id",   required=True)
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--no-dispatch", action="store_true")
    args = parser.parse_args()

    project_key = args.project
    log_agent_config(AGENT_NAME)

    print(f"\n{'='*60}")
    print(f"Classifier — {project_key} / run {args.run_id}")
    print(f"{'='*60}")

    failures = load_failures(project_key, args.run_id, args.db)
    print(f"  Failures to classify: {len(failures)}")

    true_count  = 0
    false_count = 0

    for rec in failures:
        title   = rec["test_title"]
        err_msg = rec.get("error_message", "")
        stack   = rec.get("stack_trace",   "")

        # Layer 1
        l1_verdict, l1_conf = rule_classify(err_msg, stack)

        # Layer 2
        l2_verdict, l2_conf, l2_pattern = embedding_classify(project_key, err_msg, stack)

        # Decide: escalate to LLM only when layers disagree or confidence is low
        needs_llm = (
            l1_verdict is None or
            (l2_verdict and l2_verdict != l1_verdict) or
            (l1_conf < CONFIDENCE_THRESHOLD and l2_conf < CONFIDENCE_THRESHOLD)
        )

        if needs_llm:
            verdict, rca_summary = llm_classify(
                title, err_msg, stack, l1_verdict, l2_verdict, l2_conf, l2_pattern
            )
            method = "LLM"
        else:
            verdict     = l1_verdict or l2_verdict or "false"
            rca_summary = f"Rule-based: matched pattern. Confidence={max(l1_conf, l2_conf):.2f}."
            method      = "rules" if l1_verdict else "embedding"

        print(f"  [{method:9s}] {verdict.upper()} — {title[:55]}")

        update_classification(rec["id"], verdict, rca_summary, args.db)
        # ── NEW: STORE FAILURE IN QDRANT ────────────────────────────────────────────
        text = f"{err_msg}\n{stack}"
        upsert_failure_vector(
            project_key,
            text,
            verdict,
            l2_pattern
        )
        if verdict == "true":
            true_count  += 1
        else:
            false_count += 1

        if not args.no_dispatch:
            dispatch_rca(verdict, project_key, rec, args.db)

    print(f"\n  Summary: {true_count} true failures  |  {false_count} false failures")
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
