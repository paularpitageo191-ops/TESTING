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
import time
import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config
# --- ADD THIS IMPORT (top with others) ---
import uuid
load_dotenv()
JIRA_BASE_URL   = os.getenv("JIRA_BASE_URL")
JIRA_API_TOKEN  = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
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

FAILURE_TYPES = [
    "selector",
    "timeout",
    "network",
    "assertion",
    "data",
    "auth",
    "backend",
]

# ── Thresholds ─────────────────────────────────────────────
MEMORY_THRESHOLD = 0.90          # strict → only very strong matches reuse memory
CLASSIFICATION_THRESHOLD = 0.75  # softer → allow embedding-based decisions
# ══════════════════════════════════════════════════════════════════════════════
# §0  LAYER 0 — HELPER-FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

# def _sanitize(name: str) -> str:
#     return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_')
def _sanitize(name: str) -> str:
    return name.replace("-", "_")


def detect_failure_type(error_msg: str, stack: str, rca_summary: str) -> str:
    text = f"{error_msg} {stack} {rca_summary}".lower()

    if "timeout" in text:
        return "timeout"
    if "net::err" in text or "econn" in text or "network" in text:
        return "network"
    if "assert" in text or "expected" in text:
        return "assertion"
    if "login" in text or "auth" in text:
        return "auth"
    if "data" in text:
        return "data"
    if "500" in text or "backend" in text:
        return "backend"

    return "selector"

def should_retry(failure_type: str) -> bool:
    return failure_type in ["timeout", "network"]

def retry_test(test_title: str):
    print(f"    🔁 Retrying test: {test_title}")

    subprocess.run(
        ["npx", "playwright", "test", "--grep", test_title],
        cwd=PROJECT_ROOT,
        timeout=120
    )

def is_flaky(test_title: str, db_path: str) -> bool:
    conn = sqlite3.connect(db_path)

    rows = conn.execute(
        """
        SELECT status FROM test_results
        WHERE test_title = ?
        ORDER BY id DESC LIMIT 5
        """,
        (test_title,)
    ).fetchall()

    conn.close()

    statuses = [r[0] for r in rows]
    return "failed" in statuses and "passed" in statuses

def create_jira_ticket(title: str, rca_summary: str):
    try:
        requests.post(
            f"{JIRA_BASE_URL}/rest/api/2/issue",
            headers={
                "Authorization": f"Bearer {JIRA_API_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "fields": {
                    "project": {"key": JIRA_PROJECT_KEY},
                    "summary": title,
                    "description": rca_summary,
                    "issuetype": {"name": "Bug"}
                }
            }
        )
        print("    🎫 Jira ticket created")

    except Exception as e:
        print(f"    ⚠ Jira creation failed: {e}")
      

def retrieve_similar_failures(
    project_key: str,
    vector: List[float],
    limit: int = 3,
) -> Dict:
    collection = _sanitize(f"{project_key}_failure_clusters")

    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector": vector,
                "limit": limit,
                "with_payload": True
            },
            timeout=5
        )

        if not r.ok:
            return _empty_retrieval()

        results = r.json().get("result", [])
        if not results:
            return _empty_retrieval()

        contexts = []

        # ── Build contexts (top-k) ───────────────────────────────────────────
        for hit in results:
            score = hit.get("score", 0.0)
            payload = hit.get("payload", {})

            label_full = payload.get("failure_class", "")
            label = label_full.split(":")[0] if ":" in label_full else label_full

            pattern = (payload.get("pattern_label") or "").strip()
            rca = payload.get("rca_summary", "")

            contexts.append({
                "score": round(score, 4),
                "label": label,
                "pattern": pattern,
                "rca": rca,
            })

        # ── Top hit (for decisions) ──────────────────────────────────────────
        top = results[0]
        top_score = top.get("score", 0.0)
        top_payload = top.get("payload", {})

        top_label_full = top_payload.get("failure_class", "")
        top_label = top_label_full.split(":")[0] if ":" in top_label_full else top_label_full
        top_pattern = (top_payload.get("pattern_label") or "").strip()

        # ── Memory (STRICT) ──────────────────────────────────────────────────
        memory_hit = top_payload if top_score >= MEMORY_THRESHOLD else None

        # ── Classification (CONTROLLED) ──────────────────────────────────────
        if top_score >= CLASSIFICATION_THRESHOLD and top_label in ("true", "false"):
            best_label = top_label
        else:
            best_label = None

        return {
            "memory_hit": memory_hit,
            "contexts": contexts,
            "best_label": best_label,
            "confidence": round(top_score, 4),
            "pattern": top_pattern,
        }

    except Exception as e:
        print(f"    ⚠ Qdrant retrieval error: {e}")
        return _empty_retrieval()


def _empty_retrieval():
    return {
        "memory_hit": None,
        "contexts": [],
        "best_label": None,
        "confidence": 0.0,
        "pattern": ""
    }
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
  
def safe_embed(text, retries=3):
    for _ in range(retries):
        try:
            return _embed(text)
        except Exception:
            time.sleep(1)
    return None


# ── NEW: QDRANT UPSERT LOGIC ────────────────────────────────────────────────
def upsert_failure_vector(
    project_key: str,
    text: str,
    verdict: str,
    pattern: str,
    rca_summary: str,
    vector=None
):
    collection = _sanitize(f"{project_key}_failure_clusters")

    # 🔥 reuse embedding if provided
    if vector is None:
        vector = safe_embed(text)

    print("    🔍 Vector size:", len(vector) if vector else "None")

    if not vector:
        print("    ⚠ No embedding generated — skipping Qdrant upsert")
        return

    failure_type = verdict.split(":")[1] if ":" in verdict else "selector"

    payload = {
        "failure_class": verdict,
        "failure_type": failure_type,
        "pattern_label": (pattern or "unknown").strip(),
        "text": text[:500],
        "rca_summary": rca_summary,
        "created_at": datetime.datetime.utcnow().isoformat()
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
    contexts:    Optional[List[Dict]] = None,
) -> Tuple[str, str]:

    # ── BUILD CONTEXT BLOCK (RAG) ────────────────────────────────────────────
    context_text = ""

    if contexts:
        for i, ctx in enumerate(contexts[:3]):  # top-3 only
            context_text += f"""
Example {i+1}:
- label: {ctx.get('label')}
- pattern: {ctx.get('pattern')}
- rca: {ctx.get('rca')}
"""

    # ── BUILD PROMPT ─────────────────────────────────────────────────────────
    prompt = f"""
You are a QA failure classifier.

Past similar failures:
{context_text}

Current failure:
Test: {test_title}

Error:
{error_msg}

Stack:
{stack}

Signals:
- Layer1 verdict: {l1_verdict}
- Layer2 verdict: {l2_verdict} (confidence={l2_conf}, pattern={l2_pattern})

Decide:
- "true"  → real product bug
- "false" → infra / flaky / locator issue

Return STRICT JSON:
{{"verdict": "true|false", "rca_summary": "short reason"}}
"""

    system = "You are a precise and consistent QA failure classifier. Always return valid JSON."

    # ── CALL LLM ─────────────────────────────────────────────────────────────
    raw = call_llm(AGENT_NAME, prompt, system=system)

    # Clean markdown/code fences if present
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())

    # ── PARSE RESPONSE ───────────────────────────────────────────────────────
    try:
        obj = json.loads(raw)

        verdict = obj.get("verdict", "false")
        if verdict not in ("true", "false"):
            verdict = "false"

        rca_summary = obj.get("rca_summary", "LLM classification.")

        return verdict, rca_summary

    except Exception:
        # fallback safety
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
# §6  MAIN (RAG-UNIFIED + MULTI-CONTEXT)
# ══════════════════════════════════════════════════════════════════════════════
for rec in failures:
    title   = rec["test_title"]
    err_msg = rec.get("error_message", "")
    stack   = rec.get("stack_trace", "")

    text = f"{err_msg}\n{stack}"

    # ─────────────────────────────────────────────
    # 🚨 EARLY DETECTION: UI BLOCKING
    # ─────────────────────────────────────────────
    if any(x in err_msg.lower() for x in [
        "intercepts pointer events",
        "element is not clickable",
        "another element would receive the click"
    ]):
        failure_type = "ui_blocking"
        verdict = "false"
        rca_summary = "UI blocking issue (modal/overlay intercepting clicks)"
        method = "rule"

        verdict_with_type = f"{verdict}:{failure_type}"

        print(f"  [{method:9s}] {verdict_with_type.upper()} — {title[:55]}")
        print("    ⚠ Modal blocking detected — skipping healer")

        update_classification(rec["id"], verdict_with_type, rca_summary, args.db)

        # Optional retry
        if not args.no_dispatch:
            print("    🔁 Retrying test after wait")
            retry_test(title)

        false_count += 1
        continue  # 🚨 CRITICAL: skip rest of pipeline

    # ─────────────────────────────────────────────
    # Layer 1 (rules)
    # ─────────────────────────────────────────────
    l1_verdict, l1_conf = rule_classify(err_msg, stack)

    # ─────────────────────────────────────────────
    # RAG Retrieval
    # ─────────────────────────────────────────────
    vector = safe_embed(text)

    if vector:
        print(f"    🔍 Vector size: {len(vector)}")
        retrieval = retrieve_similar_failures(project_key, vector)
    else:
        print("    ⚠ Embedding failed")
        retrieval = _empty_retrieval()

    memory     = retrieval.get("memory_hit")
    contexts   = retrieval.get("contexts", [])
    l2_verdict = retrieval.get("best_label")
    l2_conf    = retrieval.get("confidence", 0.0)
    l2_pattern = retrieval.get("pattern", "")

    # ─────────────────────────────────────────────
    # Decide LLM usage
    # ─────────────────────────────────────────────
    needs_llm = (
        l1_verdict is None or
        (l2_verdict and l2_verdict != l1_verdict) or
        (l1_conf < CLASSIFICATION_THRESHOLD and l2_conf < CLASSIFICATION_THRESHOLD)
    )

    # ─────────────────────────────────────────────
    # MEMORY FIRST
    # ─────────────────────────────────────────────
    if memory:
        verdict_full = memory.get("failure_class", "false:selector")
        rca_summary  = memory.get("rca_summary", "Reused from memory")

        if ":" in verdict_full:
            verdict, failure_type = verdict_full.split(":", 1)
        else:
            verdict = verdict_full
            failure_type = memory.get("failure_type", "selector")

        method = "memory"
        print(f"    🧠 Memory hit → {verdict}:{failure_type}")

    # ─────────────────────────────────────────────
    # LLM fallback
    # ─────────────────────────────────────────────
    elif needs_llm:
        verdict, rca_summary = llm_classify(
            title, err_msg, stack,
            l1_verdict, l2_verdict, l2_conf, l2_pattern,
            contexts=contexts
        )
        failure_type = detect_failure_type(err_msg, stack, rca_summary)
        method = "LLM"

    # ─────────────────────────────────────────────
    # Embedding fallback
    # ─────────────────────────────────────────────
    else:
        verdict = l1_verdict or l2_verdict or "false"

        if l1_verdict is None:
            labels = [c["label"] for c in contexts if c["label"] in ("true", "false")]
            if labels:
                verdict = max(set(labels), key=labels.count)

        rca_summary = f"Embedding-assisted. Confidence={l2_conf:.2f}"
        failure_type = detect_failure_type(err_msg, stack, rca_summary)
        method = "embedding"

    verdict_with_type = f"{verdict}:{failure_type}"
    print(f"  [{method:9s}] {verdict_with_type.upper()} — {title[:55]}")

    # ─────────────────────────────────────────────
    # Store results
    # ─────────────────────────────────────────────
    update_classification(rec["id"], verdict_with_type, rca_summary, args.db)

    upsert_failure_vector(
        project_key,
        text,
        verdict_with_type,
        l2_pattern,
        rca_summary,
        vector
    )

    # ─────────────────────────────────────────────
    # Flaky detection
    # ─────────────────────────────────────────────
    if is_flaky(title, args.db):
        print("    ⚠ Flaky test detected → skipping healing & routing")
        continue

    # ─────────────────────────────────────────────
    # Counters
    # ─────────────────────────────────────────────
    if verdict == "true":
        true_count += 1
    else:
        false_count += 1

    # ─────────────────────────────────────────────
    # Auto Jira
    # ─────────────────────────────────────────────
    if verdict == "true" and failure_type in ["backend", "assertion"]:
        if not rec.get("jira_created"):
            create_jira_ticket(title, rca_summary)

    # ─────────────────────────────────────────────
    # Routing (FIXED INDENTATION)
    # ─────────────────────────────────────────────
    if not args.no_dispatch:

        if failure_type == "selector":
            dispatch_rca(verdict, project_key, rec, args.db)

        elif should_retry(failure_type):
            print("    🔁 Transient issue → retrying")
            retry_test(title)

        elif failure_type == "assertion":
            print("    🧪 Assertion issue → needs review")

        elif failure_type == "backend":
            print("    🚨 Backend bug → already escalated")

        elif failure_type == "auth":
            print("    🔐 Auth issue → session/login")

        elif failure_type == "data":
            print("    📊 Data issue → input mismatch")

        else:
            print(f"    ⚠ Unknown failure type '{failure_type}' → skipping healer")
