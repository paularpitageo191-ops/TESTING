#!/usr/bin/env python3

from __future__ import annotations

import os
import re
import json
import glob
import sqlite3
import argparse
import datetime
import time
import uuid
import requests
from typing import Dict, List, Optional, Tuple
from dotenv import load_dotenv
from agent_config import embed, log_agent_config

load_dotenv()

AGENT_NAME = "selector_healer_v2"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
DEFAULT_DB = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
STEPS_DIR = os.path.join(PROJECT_ROOT, "tests", "steps")

# ══════════════════════════════════════════════════════════════════════════════
# SAFE EMBEDDING
# ══════════════════════════════════════════════════════════════════════════════

def safe_embed(text: str, retries: int = 3):
    for _ in range(retries):
        try:
            return embed(AGENT_NAME, text)
        except Exception:
            time.sleep(1)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SELECTOR VALIDATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def is_valid_selector(selector: str) -> bool:
    if not selector:
        return False
    if "TODO" in selector:
        return False
    if selector.strip().startswith("/*"):
        return False
    if len(selector) > 200:
        return False

    # basic CSS sanity
    invalid_patterns = [";;", "///", "\\\\"]
    if any(p in selector for p in invalid_patterns):
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════════
# LIGHT DOM-AWARE HEURISTIC (SAFE)
# ══════════════════════════════════════════════════════════════════════════════

def estimate_selector_quality(selector: str) -> float:
    score = 0.0

    if selector.startswith("#"):
        score += 0.5
    elif selector.startswith("."):
        score += 0.3

    if "[" in selector and "]" in selector:
        score += 0.2

    if "nth-child" in selector:
        score -= 0.2  # fragile

    return max(0.0, min(score, 1.0))


# ══════════════════════════════════════════════════════════════════════════════
# CONFIDENCE SCORING
# ══════════════════════════════════════════════════════════════════════════════

def compute_confidence(old_sel: str, new_sel: str) -> float:
    score = 0.0

    if old_sel and old_sel in new_sel:
        score += 0.2

    score += estimate_selector_quality(new_sel)

    return round(min(score, 1.0), 2)


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY SEARCH (validated-aware)
# ══════════════════════════════════════════════════════════════════════════════

def _search_healing_memory(project_key: str, old_selector: str) -> Optional[str]:
    try:
        collection = f"{project_key}_healing_memory"

        vector = safe_embed(f"{old_selector} ->")
        if not vector:
            return None

        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={"vector": vector, "limit": 3, "with_payload": True},
            timeout=5
        )

        if not r.ok:
            return None

        results = r.json().get("result", [])
        if not results:
            return None

        # prioritize validated + high score
        candidates = sorted(
            results,
            key=lambda x: (
                x["payload"].get("validated", 0),
                x.get("score", 0)
            ),
            reverse=True
        )

        best = candidates[0]["payload"]

        if best.get("validated") == 0:
            print("    ⚠ Skipping known bad fix")
            return None

        return best.get("new_selector")

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# DIFF LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_diff_report(project_key: str, report_path: Optional[str]) -> Dict:
    if report_path and os.path.exists(report_path):
        with open(report_path) as f:
            return json.load(f)

    pattern = os.path.join("docs", f"{project_key}_dom_diff_*.json")
    found = sorted(glob.glob(pattern), key=os.path.getmtime)

    if not found:
        raise FileNotFoundError("No diff report found")

    with open(found[-1]) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# SPEC FINDING
# ══════════════════════════════════════════════════════════════════════════════

def find_affected_specs(project_key: str, selectors: List[str]) -> List[str]:
    pattern = os.path.join(STEPS_DIR, f"{project_key}*.spec.ts")
    specs = glob.glob(pattern)

    affected = []
    for sf in specs:
        with open(sf) as f:
            content = f.read()
        if any(sel in content for sel in selectors if sel):
            affected.append(sf)

    return affected


# ══════════════════════════════════════════════════════════════════════════════
# DIRECT MAP
# ══════════════════════════════════════════════════════════════════════════════

def build_direct_replacement_map(diff: Dict) -> Dict[str, str]:
    mapping = {}
    for entry in diff.get("diff", {}).get("selector_drift", []):
        old = entry.get("old_selector", "")
        new = entry.get("new_selector", "")
        if old and new and old != new:
            mapping[old] = new
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# PATCH FILE (SAFE HEALING)
# ══════════════════════════════════════════════════════════════════════════════

def patch_spec_file(
    spec_file: str,
    replacements: Dict[str, str],
    project_key: str,
    db_path: str,
) -> int:

    with open(spec_file) as f:
        content = f.read()

    count = 0
    heals = []

    for old_sel, new_sel in replacements.items():

        # 🔁 MEMORY FIRST
        try:
            memory_fix = _search_healing_memory(project_key, old_sel)
            if memory_fix and memory_fix != old_sel:
                print(f"    🧠 Reusing learned fix: {old_sel} → {memory_fix}")
                new_sel = memory_fix
        except Exception:
            pass

        # 🚫 VALIDATION
        if not is_valid_selector(new_sel):
            print(f"    ⚠ Invalid selector skipped: {new_sel}")
            continue

        confidence = compute_confidence(old_sel, new_sel)

        if confidence < 0.5:
            print(f"    ⚠ Low confidence skipped: {new_sel} ({confidence})")
            continue

        if old_sel not in content:
            continue

        patterns = [
            (rf'locator\(["\']({re.escape(old_sel)})["\']', new_sel),
            (rf'\.locator\(["\']({re.escape(old_sel)})["\']', new_sel),
        ]

        for pattern, replacement in patterns:
            new_content = re.sub(
                pattern,
                lambda m: m.group(0).replace(m.group(1), replacement),
                content,
            )

            if new_content != content:
                print(f"    ✓ Healing {old_sel} → {new_sel} (conf={confidence})")
                count += 1
                content = new_content
                heals.append((old_sel, new_sel))

    if count > 0:
        with open(spec_file, "w") as f:
            f.write(content)

        _log_heals(heals, spec_file, project_key, db_path)

    return count


# ══════════════════════════════════════════════════════════════════════════════
# LOG + LEARNING
# ══════════════════════════════════════════════════════════════════════════════

def _log_heals(
    heals: List[Tuple[str, str]],
    spec_file: str,
    project_key: str,
    db_path: str,
) -> None:

    try:
        conn = sqlite3.connect(db_path)
        ts = datetime.datetime.utcnow().isoformat()

        conn.executemany(
            """
            INSERT INTO healing_log
            (project_key, spec_file, old_selector, new_selector, healed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(project_key, spec_file, old, new, ts) for old, new in heals],
        )

        conn.commit()
        conn.close()

    except Exception as exc:
        print(f"  ⚠ DB write failed: {exc}")

    # Qdrant learning
    for old, new in heals:
        try:
            vector = safe_embed(f"{old} -> {new}")
            if not vector:
                continue

            payload = {
                "old_selector": old,
                "new_selector": new,
                "mapping": f"{old}->{new}",
                "validated": None
            }

            requests.put(
                f"{QDRANT_URL}/collections/{project_key}_healing_memory/points",
                json={
                    "points": [{
                        "id": str(uuid.uuid4()),
                        "vector": vector,
                        "payload": payload
                    }]
                },
                timeout=5
            )

            print(f"    🧠 Learned fix stored")

        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════════

def heal_all(project_key: str, diff: Dict, db_path: str, dry_run: bool):

    entries = diff.get("diff", {}).get("selector_drift", [])
    if not entries:
        print("No drift detected")
        return []

    replacements = build_direct_replacement_map(diff)
    selectors = list(replacements.keys())

    affected = find_affected_specs(project_key, selectors)

    healed = []

    for sf in affected:
        count = patch_spec_file(sf, replacements, project_key, db_path) if not dry_run else 0
        if count > 0:
            healed.append(sf)

    return healed


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--diff-report")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log_agent_config(AGENT_NAME)

    diff = load_diff_report(args.project, args.diff_report)

    healed = heal_all(args.project, diff, args.db, args.dry_run)

    print(f"\nDone. Patched: {len(healed)} files\n")


if __name__ == "__main__":
    main()
