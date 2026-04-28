#!/usr/bin/env python3
"""
risk_scorer.py — UC-2: Risk-Based Test Prioritization
======================================================
Scores every spec file with a composite risk value:

  composite = 0.40 × change_impact
            + 0.40 × historical_fail_rate
            + 0.20 × business_criticality

Inputs
──────
  git diff --name-only HEAD~1          → which source files changed
  failure_history.sqlite               → historical pass/fail per spec
  Qdrant {PROJECT}_requirements        → AC text → criticality via LLM
  tests/steps/{PROJECT}.spec.ts        → spec → AC tag mapping

Outputs
───────
  risk_scores table in failure_history.sqlite  (upserted)
  tests/steps/{PROJECT}_risk_scores.json       (human-readable)

Usage
─────
  python3 risk_scorer.py --project SCRUM-70
  python3 risk_scorer.py --project SCRUM-70 --since HEAD~3
  python3 risk_scorer.py --project SCRUM-70 --dry-run
"""

from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import subprocess
import datetime
from typing import Dict, List, Tuple, Optional

import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
AGENT_NAME = "risk_scorer_v1"
DEFAULT_DB  = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")


# ══════════════════════════════════════════════════════════════════════════════
# §1  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_collection_name(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_') or 'collection'


def collection_name_for(project_key: str, suffix: str) -> str:
    return sanitize_collection_name(f"{project_key}_{suffix}")


# ══════════════════════════════════════════════════════════════════════════════
# §2  GIT CHANGE IMPACT
# ══════════════════════════════════════════════════════════════════════════════

def get_changed_files(since: str = "HEAD~1") -> List[str]:
    """Return list of source files changed since the given ref."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", since],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except Exception as exc:
        print(f"  ⚠ git diff failed: {exc}")
        return []


def extract_ac_tags(spec_path: str) -> Dict[str, List[str]]:
    """
    Parse a spec file and return {test_title: [AC tags]}.
    Looks for @AC\\d+ and @SCRUM_\\d+ tags in test() title strings.
    """
    mapping: Dict[str, List[str]] = {}
    if not os.path.exists(spec_path):
        return mapping
    with open(spec_path) as f:
        content = f.read()
    for m in re.finditer(r'test\(["\'](.+?)["\']', content):
        title = m.group(1)
        tags  = re.findall(r'@(AC\d+|SCRUM[-_]\d+)', title)
        mapping[title] = tags
    return mapping


def compute_change_impact(
    changed_files: List[str],
    spec_file: str,
    ac_tags: List[str],
) -> float:
    """
    0.0 – 1.0.  Heuristics (no LLM needed):
      - If the spec file itself changed → 1.0
      - If a source file in the same feature area changed → scale by overlap
      - Otherwise → baseline 0.1 (all tests carry some risk on any change)
    """
    spec_name = os.path.basename(spec_file).lower()

    # Direct spec change
    if any(spec_name in cf or cf.endswith(".spec.ts") for cf in changed_files):
        return 1.0

    if not changed_files:
        return 0.1

    # Keyword overlap between changed file names and spec name / AC tags
    spec_tokens = set(re.split(r'[-_./]', spec_name))
    tag_tokens  = set(t.lower() for t in ac_tags)
    all_tokens  = spec_tokens | tag_tokens

    hits = sum(
        1 for cf in changed_files
        if any(tok in cf.lower() for tok in all_tokens if len(tok) > 2)
    )
    raw = min(hits / max(len(changed_files), 1), 1.0)
    return round(max(raw, 0.1), 4)


# ══════════════════════════════════════════════════════════════════════════════
# §3  HISTORICAL FAIL RATE
# ══════════════════════════════════════════════════════════════════════════════

def compute_fail_rate(project_key: str, spec_file: str,
                      db_path: str, window: int = 20) -> float:
    """
    Fraction of the last `window` runs for this spec that failed.
    Returns 0.5 as a neutral prior when there is no history yet.
    """
    try:
        conn = sqlite3.connect(db_path)
        cur  = conn.execute(
            """
            SELECT status FROM test_results
            WHERE project_key = ? AND spec_file = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (project_key, spec_file, window),
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        if not rows:
            return 0.5   # no history → neutral prior
        fail_count = sum(1 for s in rows if s == "failed")
        return round(fail_count / len(rows), 4)
    except Exception as exc:
        print(f"  ⚠ DB fail rate query failed: {exc}")
        return 0.5


# ══════════════════════════════════════════════════════════════════════════════
# §4  BUSINESS CRITICALITY  — LLM reads Jira AC text from Qdrant
# ══════════════════════════════════════════════════════════════════════════════

def _generate_embedding(text: str) -> List[float]:
    return embed(AGENT_NAME, text)


def _fetch_ac_text_from_qdrant(project_key: str, ac_tags: List[str]) -> str:
    """
    Pull acceptance criteria text for the given AC tags from Qdrant requirements
    collection.  Returns concatenated text (may be empty).
    """
    if not ac_tags:
        return ""
    collection = collection_name_for(project_key, "requirements")
    query      = " ".join(ac_tags)
    vector     = _generate_embedding(query)
    if not vector:
        return ""
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector":       vector,
                "limit":        5,
                "with_payload": True,
                "filter": {"must": [{"key": "project_key", "match": {"value": project_key}}]},
            },
            timeout=15,
        )
        if not r.ok:
            return ""
        texts = []
        for hit in r.json().get("result", []):
            t = hit.get("payload", {}).get("text", "")
            if t:
                texts.append(t)
        return "\n".join(texts)
    except Exception as exc:
        print(f"  ⚠ Qdrant AC fetch failed: {exc}")
        return ""


def _call_ollama(prompt: str, system: str = "") -> str:
    return call_llm(AGENT_NAME, prompt, system=system)


def compute_criticality(project_key: str, ac_tags: List[str],
                        test_title: str) -> float:
    """
    LLM-scored business criticality ∈ [0.0, 1.0].

    Prompt strategy: give the model the AC text + test title and ask for a
    criticality score with a short rationale.  Parse the first float found.
    Falls back to 0.5 if LLM is unreachable or response is unparseable.
    """
    ac_text = _fetch_ac_text_from_qdrant(project_key, ac_tags)

    system = (
        "You are a QA risk analyst. Your job is to score the business criticality "
        "of a test case on a scale from 0.0 (trivial cosmetic test) to 1.0 "
        "(critical payment / security / data-loss path). "
        "Reply with ONLY a JSON object: {\"score\": <float>, \"reason\": \"<one sentence>\"}."
    )
    prompt = (
        f"Test title: {test_title}\n"
        f"AC tags: {', '.join(ac_tags) if ac_tags else 'none'}\n"
        f"Acceptance criteria text:\n{ac_text or '(not available)'}\n\n"
        "Score the business criticality of this test."
    )

    raw = _call_ollama(prompt, system=system)
    if not raw:
        return 0.5

    # Try JSON parse first
    try:
        obj   = json.loads(raw)
        score = float(obj.get("score", 0.5))
        return round(min(max(score, 0.0), 1.0), 4)
    except Exception:
        pass

    # Fallback: find first float in response
    m = re.search(r'\b(0\.\d+|1\.0)\b', raw)
    if m:
        return round(float(m.group(1)), 4)
    return 0.5


# ══════════════════════════════════════════════════════════════════════════════
# §5  COMPOSITE SCORING
# ══════════════════════════════════════════════════════════════════════════════

def score_spec(
    project_key:   str,
    spec_file:     str,
    changed_files: List[str],
    db_path:       str,
    since:         str,
) -> Dict:
    """Score a single spec file and return the full score record."""
    ac_map    = extract_ac_tags(spec_file)
    all_tags  = list({tag for tags in ac_map.values() for tag in tags})
    test_title = os.path.basename(spec_file)

    change_impact = compute_change_impact(changed_files, spec_file, all_tags)
    fail_rate     = compute_fail_rate(project_key, spec_file, db_path)
    criticality   = compute_criticality(project_key, all_tags, test_title)

    composite = round(
        0.40 * change_impact +
        0.40 * fail_rate     +
        0.20 * criticality,
        4,
    )

    return {
        "project_key":   project_key,
        "spec_file":     spec_file,
        "ac_tags":       ",".join(all_tags),
        "change_impact": change_impact,
        "fail_rate":     fail_rate,
        "criticality":   criticality,
        "composite":     composite,
        "scored_at":     datetime.datetime.utcnow().isoformat(),
    }


def upsert_risk_scores(scores: List[Dict], db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executemany(
        """
        INSERT INTO risk_scores
            (project_key, spec_file, ac_tags,
             change_impact, fail_rate, criticality, composite, scored_at)
        VALUES
            (:project_key, :spec_file, :ac_tags,
             :change_impact, :fail_rate, :criticality, :composite, :scored_at)
        ON CONFLICT(project_key, spec_file) DO UPDATE SET
            ac_tags       = excluded.ac_tags,
            change_impact = excluded.change_impact,
            fail_rate     = excluded.fail_rate,
            criticality   = excluded.criticality,
            composite     = excluded.composite,
            scored_at     = excluded.scored_at
        """,
        scores,
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# §6  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-2 Risk Scorer")
    parser.add_argument("--project",  required=True, help="Project key e.g. SCRUM-70")
    parser.add_argument("--since",    default="HEAD~1", help="Git ref for diff base")
    parser.add_argument("--db",       default=DEFAULT_DB)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    project_key = args.project
    log_agent_config(AGENT_NAME)
    spec_glob   = os.path.join(STEPS_DIR, f"{project_key}.spec.ts")

    print(f"\n{'='*60}")
    print(f"Risk Scorer — {project_key}")
    print(f"{'='*60}")

    changed_files = get_changed_files(args.since)
    print(f"  Changed files ({args.since}): {len(changed_files)}")

    spec_files = [spec_glob] if os.path.exists(spec_glob) else []
    if not spec_files:
        import glob as _glob
        spec_files = _glob.glob(os.path.join(STEPS_DIR, "*.spec.ts"))

    print(f"  Specs to score: {len(spec_files)}")

    scores = []
    for sf in sorted(spec_files):
        print(f"  Scoring {os.path.basename(sf)} …", end=" ", flush=True)
        rec = score_spec(project_key, sf, changed_files, args.db, args.since)
        scores.append(rec)
        print(f"composite={rec['composite']:.3f} "
              f"(impact={rec['change_impact']:.2f} "
              f"fail={rec['fail_rate']:.2f} "
              f"crit={rec['criticality']:.2f})")

    scores.sort(key=lambda x: x["composite"], reverse=True)

    out_json = os.path.join(STEPS_DIR, f"{project_key}_risk_scores.json")
    if not args.dry_run:
        upsert_risk_scores(scores, args.db)
        with open(out_json, "w") as f:
            json.dump(scores, f, indent=2)
        print(f"\n  ✓ Risk scores written → {out_json}")
    else:
        print("\n  [DRY RUN] No files written.")
        print(json.dumps(scores, indent=2))

    print(f"\n  Top 3 highest-risk specs:")
    for rec in scores[:3]:
        print(f"    {os.path.basename(rec['spec_file'])}  → {rec['composite']:.3f}")

    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
