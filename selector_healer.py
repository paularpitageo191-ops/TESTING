#!/usr/bin/env python3
"""
selector_healer.py — UC-4: Automatic Selector Healing
======================================================
Reads a dom_diff report and patches every drifted selector in the
affected spec files.  Three strategies in priority order:

  1. Direct new_selector from diff   — element found in new snapshot
  2. Qdrant nearest match            — embed old label → find best new element
  3. LLM reasoning                   — LLM reads old step + new DOM context

After patching, records each heal in healing_log table and
dispatches test_validator.py to gate before committing.

Usage
─────
  python3 selector_healer.py --project SCRUM-70 --diff-report docs/SCRUM-70_dom_diff_*.json
"""

from __future__ import annotations

import os
import re
import json
import glob
import sqlite3
import argparse
import subprocess
import datetime
from typing import Dict, List, Optional, Tuple
import time
import uuid
import requests
from dotenv import load_dotenv
from agent_config import call_llm, embed, log_agent_config

load_dotenv()

AGENT_NAME = "selector_healer_v1"

QDRANT_URL  = os.getenv("QDRANT_URL",  "http://localhost:6333")
DEFAULT_DB  = os.path.join(os.path.abspath(os.path.dirname(__file__)), "failure_history.sqlite")
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
STEPS_DIR    = os.path.join(PROJECT_ROOT, "tests", "steps")

def _search_healing_memory(project_key: str, old_selector: str) -> Optional[str]:
    try:
        collection = f"{project_key}_healing_memory"

        vector = safe_embed(old_selector)
        if not vector:
            return None

        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector": vector,
                "limit": 3,
                "with_payload": True
            },
            timeout=5
        )

        if not r.ok:
            return None

        results = r.json().get("result", [])
        if not results:
            return None

        # 🔥 SORT: prefer successful fixes first
        candidates = sorted(
            results,
            key=lambda x: (
                x["payload"].get("validated", 0),  # success first
                x.get("score", 0)
            ),
            reverse=True
        )

        best = candidates[0]["payload"]

        # ❌ skip bad fixes
        if best.get("validated") == 0:
            print("  ⚠ Known bad fix — skipping")
            return None

        return best.get("new_selector")

    except Exception:
        return None

def safe_embed(text, retries=3):
    for _ in range(retries):
        try:
            return _embed(text)
        except Exception:
            time.sleep(1)
    return None
# ══════════════════════════════════════════════════════════════════════════════
# §1  LOAD DIFF REPORT
# ══════════════════════════════════════════════════════════════════════════════

def load_diff_report(project_key: str, report_path: Optional[str]) -> Dict:
    if report_path and os.path.exists(report_path):
        with open(report_path) as f:
            return json.load(f)
    # Find latest diff report
    pattern = os.path.join("docs", f"{project_key}_dom_diff_*.json")
    found   = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not found:
        raise FileNotFoundError(
            f"No diff report found. Run: python3 dom_diff.py --project {project_key}"
        )
    with open(found[-1]) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# §2  FIND SPEC FILES AFFECTED BY DRIFT
# ══════════════════════════════════════════════════════════════════════════════

def find_affected_specs(project_key: str, drifted_selectors: List[str]) -> List[str]:
    """Return spec files that reference any of the drifted selectors."""
    pattern   = os.path.join(STEPS_DIR, f"{project_key}*.spec.ts")
    all_specs = glob.glob(pattern)
    affected  = []
    for sf in all_specs:
        with open(sf) as f:
            content = f.read()
        if any(sel in content for sel in drifted_selectors if sel):
            affected.append(sf)
    return affected


# ══════════════════════════════════════════════════════════════════════════════
# §3  STRATEGY 1 — DIRECT REPLACEMENT FROM DIFF
# ══════════════════════════════════════════════════════════════════════════════

def build_direct_replacement_map(diff: Dict) -> Dict[str, str]:
    """
    Build {old_selector: new_selector} from selector_drift entries
    where the new selector is available directly from the diff.
    """
    mapping: Dict[str, str] = {}
    for entry in diff.get("diff", {}).get("selector_drift", []):
        old_sel = entry.get("old_selector", "")
        new_sel = entry.get("new_selector", "")
        if old_sel and new_sel and old_sel != new_sel:
            mapping[old_sel] = new_sel
    return mapping


# ══════════════════════════════════════════════════════════════════════════════
# §4  STRATEGY 2 — QDRANT NEAREST MATCH
# ══════════════════════════════════════════════════════════════════════════════

def _embed(text: str) -> List[float]:
    return embed(AGENT_NAME, text)



def _sanitize(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_')


def qdrant_heal(project_key: str, fingerprint: str) -> Optional[str]:
    """
    Embed the element fingerprint and find nearest element in the
    (updated) Qdrant UI memory collection.
    Returns the best selector or None.
    """
    collection = _sanitize(f"{project_key}_ui_memory")
    vector     = _embed(fingerprint)
    if not vector:
        return None
    try:
        r = requests.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector":       vector,
                "limit":        3,
                "with_payload": True,
                "filter": {"must": [{"key": "project_key",
                                     "match": {"value": project_key}}]},
            },
            timeout=10,
        )
        if not r.ok:
            return None
        for hit in r.json().get("result", []):
            score   = hit.get("score", 0)
            payload = hit.get("payload", {}).get("details", {})
            el_id   = payload.get("id", "")
            if score >= 0.80 and el_id:
                return f"#{el_id}"
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# §5  STRATEGY 3 — LLM REASONING
# ══════════════════════════════════════════════════════════════════════════════

def llm_heal(
    old_selector: str,
    fingerprint:  str,
    new_dom_ctx:  str,
    test_step:    str,
) -> Optional[str]:
    raw  = call_llm(AGENT_NAME, prompt, system=system)
    raw  = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    try:
        obj  = json.loads(raw)
        sel  = obj.get("selector", "")
        conf = float(obj.get("confidence", 0))
        return sel if sel and conf >= 0.65 else None
    except Exception:
        return None


def patch_spec_file(
    spec_file: str,
    replacements: Dict[str, str],
    project_key: str,
    db_path: str,
) -> int:
    """
    Apply selector replacements to spec file.
    Returns number of replacements made.
    """
    with open(spec_file) as f:
        content = f.read()

    count = 0
    heals = []

    for old_sel, new_sel in replacements.items():

        # 🔥 PHASE 4 ADD — try memory reuse first
        try:
            memory_fix = _search_healing_memory(project_key, old_sel)
            if memory_fix:
                print(f"  🧠 Reusing learned fix: {old_sel} → {memory_fix}")
                new_sel = memory_fix
        except Exception:
            pass

        if old_sel not in content:
            continue

        # Replace in locator() calls
        patterns = [
            (rf'locator\(["\']({re.escape(old_sel)})["\']', new_sel),
            (rf'\.locator\(["\']({re.escape(old_sel)})["\']', new_sel),
        ]

        for pattern, replacement in patterns:
            new_content = re.sub(
                pattern,
                lambda m, r=replacement: m.group(0).replace(m.group(1), r),
                content,
            )

            if new_content != content:
                # 🔧 safer counting (avoids double counting bugs)
                delta = new_content.count(new_sel) - content.count(new_sel)
                count += max(delta, 1)

                content = new_content
                heals.append((old_sel, new_sel))

    if count > 0:
        # 🔥 OPTIONAL SAFETY — backup before write
        try:
            import shutil
            shutil.copy(spec_file, spec_file + ".bak")
        except Exception:
            pass

        with open(spec_file, "w") as f:
            f.write(content)

        # Log to DB + Qdrant learning
        _log_heals(heals, spec_file, project_key, db_path)

    return count


def _log_heals(
    heals: List[Tuple[str, str]],
    spec_file: str,
    project_key: str,
    db_path: str,
) -> None:
    """
    Logs healing actions to SQLite and stores them in Qdrant for reuse.
    """
    try:
        conn = sqlite3.connect(db_path)
        ts   = datetime.datetime.utcnow().isoformat()

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
        print(f"  ⚠ Heal log DB write failed: {exc}")

    # 🔥 PHASE 4 ADD — Store fixes in Qdrant (learning loop)
    for old, new in heals:
        try:
            collection = f"{project_key}_healing_memory"

            text = f"{old} -> {new}"
            vector = safe_embed(text)

            if not vector:
                continue

            payload = {
                "old_selector": old,
                "new_selector": new,
                "mapping": text,
                "validated": None
            }

            requests.put(
                f"{QDRANT_URL}/collections/{collection}/points",
                json={
                    "points": [{
                        "id": str(uuid.uuid4()),
                        "vector": vector,
                        "payload": payload
                    }]
                },
                timeout=5
            )

            print(f"    🧠 Learned fix: {old} → {new}")

        except Exception as e:
            print(f"    ⚠ Qdrant heal store failed: {e}")


def _store_healing_memory(project_key: str, old: str, new: str):
    """
    Store selector fix in Qdrant for future reuse
    """
    try:
        collection = f"{project_key}_healing_memory"

        text = f"{old} -> {new}"
        vector = safe_embed(text)

        if not vector:
            return

        payload = {
            "old_selector": old,
            "new_selector": new,
            "mapping": text,
        }

        requests.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            json={
                "points": [{
                    "id": str(uuid.uuid4()),
                    "vector": vector,
                    "payload": payload
                }]
            },
            timeout=5
        )

        print(f"    🧠 Learned fix: {old} → {new}")

    except Exception as e:
        print(f"    ⚠ Qdrant heal store failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# §7  ORCHESTRATE HEALING
# ══════════════════════════════════════════════════════════════════════════════

def heal_all(project_key: str, diff: Dict, db_path: str, dry_run: bool) -> List[str]:
    """
    For each drifted selector:
      1. Try direct replacement (from diff)
      2. If unavailable → Qdrant nearest
      3. If still None → LLM
    Returns list of healed spec files.
    """
    drift_entries  = diff.get("diff", {}).get("selector_drift", [])
    if not drift_entries:
        print("  No selector drift found.")
        return []

    # Build maps
    direct_map = build_direct_replacement_map(diff)

    # Collect all old selectors to find affected specs
    all_old = list(direct_map.keys())
    for entry in drift_entries:
        if entry.get("old_selector"):
            all_old.append(entry["old_selector"])

    affected_specs = find_affected_specs(project_key, all_old)
    print(f"  Affected specs : {len(affected_specs)}")

    final_replacements: Dict[str, str] = {}

    for entry in drift_entries:
        old_sel     = entry.get("old_selector", "")
        new_sel     = entry.get("new_selector", "")
        fingerprint = entry.get("fingerprint", "")

        if not old_sel:
            continue

        # Strategy 1
        if new_sel and new_sel != old_sel:
            final_replacements[old_sel] = new_sel
            print(f"  ✓ Direct  : {old_sel} → {new_sel}")
            continue

        # Strategy 2
        qdrant_sel = qdrant_heal(project_key, fingerprint)
        if qdrant_sel:
            final_replacements[old_sel] = qdrant_sel
            print(f"  ✓ Qdrant  : {old_sel} → {qdrant_sel}")
            continue

        # Strategy 3
        new_dom_ctx = json.dumps(entry.get("new_el", {}), indent=2)
        llm_sel     = llm_heal(old_sel, fingerprint, new_dom_ctx, fingerprint)
        if llm_sel:
            final_replacements[old_sel] = llm_sel
            print(f"  ✓ LLM     : {old_sel} → {llm_sel}")
        else:
            print(f"  ✗ Unhealed: {old_sel}  (all strategies failed)")

    healed_files = []
    for sf in affected_specs:
        count = patch_spec_file(sf, final_replacements, project_key, db_path) \
                if not dry_run else 0
        if count > 0 or dry_run:
            healed_files.append(sf)
            print(f"  Patched {count} occurrence(s) in {os.path.basename(sf)}")

    return healed_files


# ══════════════════════════════════════════════════════════════════════════════
# §8  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="UC-4 Selector Healer")
    parser.add_argument("--project",     required=True)
    parser.add_argument("--diff-report", default=None)
    parser.add_argument("--db",          default=DEFAULT_DB)
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    project_key = args.project
    log_agent_config(AGENT_NAME)

    print(f"\n{'='*60}")
    print(f"Selector Healer — {project_key}")
    print(f"{'='*60}")

    diff        = load_diff_report(project_key, args.diff_report)
    drift_count = len(diff.get("diff", {}).get("selector_drift", []))
    print(f"  Drifted selectors : {drift_count}")

    healed_files = heal_all(project_key, diff, args.db, args.dry_run)

    if healed_files and not args.no_validate and not args.dry_run:
        print(f"\n  → Dispatching test_validator.py for {len(healed_files)} file(s)")
        for sf in healed_files:
            subprocess.Popen([
                "python3", "test_validator.py",
                "--project", project_key,
                "--spec",    sf,
                "--db",      args.db,
            ], cwd=PROJECT_ROOT)

    print(f"\n{'='*60}")
    print(f"✓ Healer complete — {len(healed_files)} spec(s) patched")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
