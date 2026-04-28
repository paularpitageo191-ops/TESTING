#!/usr/bin/env python3
"""
Gherkin Agent v2 — Full knowledgebase-driven Gherkin generator
================================================================

Knowledge sources consumed before any agent runs:
  • Qdrant requirements collection  — all vectorised Jira/PRD/attachment chunks
  • Qdrant ui_memory collection     — live DOM element vectors
  • docs/jira_sync/.../story/       — full Jira story JSON
  • docs/jira_sync/.../epic/        — epic JSON
  • docs/jira_sync/.../subtasks/    — one JSON per subtask
  • docs/jira_sync/.../comments/    — per-issue comment JSONs
  • docs/inbox/{PROJECT}.json       — enriched inbox (AC + subtasks + comments)
  • docs/requirements/{P}_PRD.md   — generated PRD

5-step pipeline:
  Step 0 — Knowledge Builder : assemble unified KB from ALL sources
  Agent 1 — AC Analyst       : KB → exhaustive structured AC list
  Agent 2 — DOM Mapper       : AC → real DOM elements (raw DOM + Qdrant ui_memory)
  Agent 3 — Scenario Writer  : Gherkin per AC with steps, test data, expected results
  Agent 4 — QA Reviewer      : validate, dedup, enforce BDD, verify completeness
  Step 5 — Coverage Report   : % coverage against KB AC count

Output:
  tests/features/{PROJECT}.feature   — Gherkin file
  docs/gherkin_coverage_{PROJECT}.json — coverage report
"""

import os
import re
import json
import glob
import argparse
from typing import Dict, List, Optional, Any, Tuple
from dotenv import load_dotenv

load_dotenv()

TEMPERATURE = 0.1
QDRANT_URL  = os.getenv("QDRANT_URL", "http://localhost:6333")


# ══════════════════════════════════════════════════════════════════════════════
# LLM helper
# ══════════════════════════════════════════════════════════════════════════════

def _call_llm(gateway, prompt: str, system: str, label: str) -> str:
    print(f"  [Agent:{label}] calling LLM…")
    for attempt in range(1, 4):
        try:
            raw = gateway.chat(
                prompt,
                system_prompt=system,
                temperature=TEMPERATURE,
                timeout=180,
            )
            if raw and raw.strip():
                print(f"  [Agent:{label}] ✓ {len(raw)} chars")
                return raw.strip()
            print(f"  [Agent:{label}] empty response (attempt {attempt}/3)")
        except Exception as exc:
            print(f"  [Agent:{label}] error attempt {attempt}/3: {exc}")
    raise RuntimeError(f"[Agent:{label}] LLM failed after 3 attempts")


def _extract_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    for pattern in [r'\[.*\]', r'\{.*\}']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant helpers
# ══════════════════════════════════════════════════════════════════════════════

def _qdrant_search(collection: str, query_text: str, gateway,
                   limit: int = 15, qdrant_url: str = QDRANT_URL,
                   project_key: str = "") -> List[str]:
    import requests as _req
    try:
        model_override = None
        try:
            model_override = gateway.resolve_model_for_agent(
                "gherkin_agent", purpose="embedding", fallback_model=None)
        except Exception:
            pass
        vector = gateway.generate_embedding(query_text, model_override=model_override)
        if not vector:
            return []
        body: dict = {"vector": vector, "limit": limit, "with_payload": True}
        if project_key:
            body["filter"] = {"must": [{"key": "project_key",
                                        "match": {"value": project_key}}]}
        resp = _req.post(f"{qdrant_url}/collections/{collection}/points/search",
                         json=body, timeout=15)
        if not resp.ok:
            return []
        return [
            h["payload"].get("text", "") or h["payload"].get("label", "")
            for h in resp.json().get("result", [])
            if h.get("payload")
        ]
    except Exception as exc:
        print(f"    ⚠ Qdrant search ({collection}): {exc}")
        return []


def _qdrant_scroll_all(collection: str, qdrant_url: str = QDRANT_URL,
                       project_key: str = "") -> List[Dict]:
    import requests as _req
    try:
        body: dict = {"limit": 500, "with_payload": True}
        if project_key:
            body["filter"] = {"must": [{"key": "project_key",
                                        "match": {"value": project_key}}]}
        resp = _req.post(f"{qdrant_url}/collections/{collection}/points/scroll",
                         json=body, timeout=20)
        if not resp.ok:
            return []
        return [p["payload"] for p in resp.json().get("result", {}).get("points", [])
                if p.get("payload")]
    except Exception as exc:
        print(f"    ⚠ Qdrant scroll ({collection}): {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# DOM helpers
# ══════════════════════════════════════════════════════════════════════════════

def _dom_index(dom_data: Dict) -> Dict[str, List[Dict]]:
    index: Dict[str, List[Dict]] = {}
    for group in ["input_elements", "button_elements", "textarea_elements",
                  "dropdown_elements", "custom_dropdown_elements"]:
        for el in dom_data.get(group, []):
            page = (el.get("page_url") or "").strip().rstrip("/")
            if not page:
                continue
            index.setdefault(page, [])
            label = (
                el.get("label") or el.get("placeholder") or
                el.get("text")  or el.get("name") or el.get("id") or ""
            ).strip()
            el_id    = el.get("id") or ""
            selector = f"#{el_id}" if el_id else (el.get("selector") or "")
            if label or selector:
                index[page].append({
                    "label":    label,
                    "selector": selector,
                    "type":     el.get("type") or el.get("tagName") or group.replace("_elements", ""),
                    "disabled": el.get("isDisabled") or el.get("disabled") or False,
                })
    return index


def _dom_summary(dom_index: Dict[str, List[Dict]]) -> str:
    lines = []
    for page_url, elements in sorted(dom_index.items()):
        lines.append(f"\nPage: {page_url}")
        for el in elements:
            dis = " [DISABLED]" if el.get("disabled") else ""
            lines.append(f"  [{el['type'].upper()}] "
                         f"label={el['label']!r:35} "
                         f"selector={el['selector']!r}{dis}")
    return "\n".join(lines) if lines else "(no form elements captured)"


# ══════════════════════════════════════════════════════════════════════════════
# Jira file loaders
# ══════════════════════════════════════════════════════════════════════════════

def _adf_to_text(node) -> str:
    if isinstance(node, str):  return node
    if isinstance(node, list): return "\n".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("text"): return node["text"]
        return _adf_to_text(node.get("content", []))
    return ""


def _load_jira_files(project_key: str) -> Dict[str, Any]:
    result = {"story": {}, "epic": {}, "subtasks": [], "comments": {}, "inbox": {}}
    jira_sync_root = os.path.join("docs", "jira_sync")
    if not os.path.isdir(jira_sync_root):
        return result

    run_dirs = sorted(
        d for d in glob.glob(os.path.join(jira_sync_root, f"{project_key}_*"))
        if os.path.isdir(d)
    )
    if not run_dirs:
        return result
    run_dir = run_dirs[-1]

    for p in glob.glob(os.path.join(run_dir, "story", "*.json")):
        try:
            result["story"] = json.load(open(p)); break
        except Exception:
            pass

    for p in glob.glob(os.path.join(run_dir, "epic", "*.json")):
        try:
            result["epic"] = json.load(open(p)); break
        except Exception:
            pass

    for p in glob.glob(os.path.join(run_dir, "subtasks", "*.json")):
        try:
            result["subtasks"].append(json.load(open(p)))
        except Exception:
            pass

    for p in glob.glob(os.path.join(run_dir, "comments", "*.json")):
        issue_key = os.path.basename(p).replace("_comments.json", "")
        try:
            c = json.load(open(p))
            if c:
                result["comments"][issue_key] = c
        except Exception:
            pass

    inbox_path = os.path.join("docs", "inbox", f"{project_key}.json")
    if os.path.exists(inbox_path):
        try:
            data = json.load(open(inbox_path))
            result["inbox"] = data[0] if isinstance(data, list) and data else data
        except Exception:
            pass

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Step 0 — Knowledge Builder
# ══════════════════════════════════════════════════════════════════════════════

def build_knowledgebase(
    project_key:             str,
    jira_story:              Optional[Dict],
    req_payloads:            List[Dict],
    prd_text:                str,
    inbox_issues:            List[Dict],
    qdrant_url:              str,
    requirements_collection: str,
    dom_collection:          str,
    gateway,
) -> Dict[str, Any]:
    """
    Assemble a unified, structured knowledgebase from all pipeline sources.
    This is the single source of truth fed to all agents.
    """
    print("\n[Step 0 — Knowledge Builder] Assembling knowledgebase…")

    kb: Dict[str, Any] = {
        "project_key":         project_key,
        "epic":                {},
        "story":               {},
        "subtasks":            [],
        "acceptance_criteria": [],  # raw AC text blocks
        "validation_rules":    [],  # field-level rules (generates negative tests)
        "test_data":           [],  # concrete input values
        "ui_specs":            [],  # field/selector specs
        "comments":            [],  # decisions, clarifications
        "attachment_data":     [],  # Excel rows / PDF pages
        "prd_sections":        {},  # keyed by section name
        "dom_element_texts":   [],  # from ui_memory Qdrant
        "negative_scenarios":  [],  # explicit negative test conditions
        "non_functional":      [],  # perf, security, accessibility
        "tester_steps":        [],  # manual test steps with expected results
    }

    jira_files = _load_jira_files(project_key)
    inbox      = jira_files.get("inbox", {})

    # ── Epic ─────────────────────────────────────────────────────────────
    if jira_files["epic"]:
        ef = jira_files["epic"].get("fields", {})
        kb["epic"] = {
            "key":         jira_files["epic"].get("key", ""),
            "summary":     ef.get("summary", ""),
            "description": _adf_to_text(ef.get("description", "")),
            "status":      ef.get("status", {}).get("name", "")
                           if isinstance(ef.get("status"), dict) else "",
        }
        print(f"  ✓ Epic: {kb['epic']['key']} — {kb['epic']['summary'][:60]}")

    # ── Story ─────────────────────────────────────────────────────────────
    if jira_story:
        kb["story"] = jira_story
    elif jira_files["story"]:
        sf = jira_files["story"].get("fields", {})
        kb["story"] = {
            "key":                 jira_files["story"].get("key", project_key),
            "summary":             sf.get("summary", ""),
            "description":         _adf_to_text(sf.get("description", "")),
            "acceptance_criteria": "",
        }
    print(f"  ✓ Story: {kb['story'].get('key','?')} — {kb['story'].get('summary','')[:60]}")

    # ── Subtasks ──────────────────────────────────────────────────────────
    for sub in jira_files["subtasks"]:
        sf     = sub.get("fields", {})
        status = sf.get("status", {})
        kb["subtasks"].append({
            "key":         sub.get("key", ""),
            "summary":     sf.get("summary", ""),
            "description": _adf_to_text(sf.get("description", "")),
            "status":      status.get("name", "") if isinstance(status, dict) else "",
            "issuetype":   sf.get("issuetype", {}).get("name", "")
                           if isinstance(sf.get("issuetype"), dict) else "",
        })
    # Fallback to inbox subtasks if raw files empty
    if not kb["subtasks"] and isinstance(inbox.get("subtasks"), list):
        kb["subtasks"] = inbox["subtasks"]
    print(f"  ✓ Subtasks: {len(kb['subtasks'])}")

    # ── Comments ──────────────────────────────────────────────────────────
    for issue_key, comments in jira_files["comments"].items():
        for c in (comments if isinstance(comments, list) else []):
            body = _adf_to_text(c.get("body", "")).strip() if isinstance(c, dict) else ""
            if body and len(body) > 10:
                kb["comments"].append({
                    "issue":   issue_key,
                    "author":  c.get("author", ""),
                    "body":    body,
                    "created": c.get("created", ""),
                })
    for c in (inbox.get("comments") or []):
        if isinstance(c, dict) and c.get("body", "").strip():
            if len(c["body"].strip()) > 10:
                kb["comments"].append({
                    "issue":  c.get("issue", ""),
                    "author": c.get("author", ""),
                    "body":   c["body"].strip(),
                })
    print(f"  ✓ Comments: {len(kb['comments'])}")

    # ── Qdrant requirements collection ───────────────────────────────────
    all_req_points = req_payloads or _qdrant_scroll_all(
        requirements_collection, qdrant_url, project_key)

    for p in all_req_points:
        text    = (p.get("text") or "").strip()
        if not text or len(text) < 10:
            continue
        ct      = p.get("content_type", "general")
        section = p.get("section", "")

        if ct == "acceptance_criteria" or section == "acceptance_criteria":
            kb["acceptance_criteria"].append(text)
        elif ct == "test_data":
            kb["test_data"].append(text)
        elif ct == "ui_spec" or section in ("jira_main", "story", "epic"):
            kb["ui_specs"].append(text)
        elif section in ("comments_bulk", "comment"):
            kb["comments"].append({"issue": p.get("requirement_id", ""), "body": text})
        elif section == "page":
            kb["attachment_data"].append({"source": p.get("source", ""), "text": text})
        elif "sheet_" in section:
            kb["attachment_data"].append({"source": p.get("source", ""), "text": text})
        else:
            kb["validation_rules"].append(text)

    print(f"  ✓ Qdrant requirements: {len(all_req_points)} points → "
          f"AC:{len(kb['acceptance_criteria'])}  "
          f"test_data:{len(kb['test_data'])}  "
          f"ui_specs:{len(kb['ui_specs'])}  "
          f"validation:{len(kb['validation_rules'])}  "
          f"attachments:{len(kb['attachment_data'])}")

    # ── Qdrant ui_memory ──────────────────────────────────────────────────
    if dom_collection:
        dom_points = _qdrant_scroll_all(dom_collection, qdrant_url, project_key)
        kb["dom_element_texts"] = [
            p.get("text", "") or p.get("label", "")
            for p in dom_points
            if p.get("text") or p.get("label")
        ]
        print(f"  ✓ Qdrant ui_memory: {len(kb['dom_element_texts'])} DOM vectors")

    # ── PRD — parse into named sections ───────────────────────────────────
    if prd_text:
        current_section = "overview"
        current_lines: List[str] = []
        for line in prd_text.splitlines():
            if line.startswith("## "):
                if current_lines:
                    kb["prd_sections"][current_section] = "\n".join(current_lines).strip()
                current_section = line.lstrip("# ").strip().lower()
                current_lines   = []
            else:
                current_lines.append(line)
        if current_lines:
            kb["prd_sections"][current_section] = "\n".join(current_lines).strip()

        for sec_name, sec_text in kb["prd_sections"].items():
            if not sec_text:
                continue
            if "acceptance" in sec_name:
                kb["acceptance_criteria"].insert(0, sec_text)
            if "negative" in sec_name or "edge" in sec_name:
                kb["negative_scenarios"].append(sec_text)
            if "validation" in sec_name or "test data" in sec_name:
                kb["validation_rules"].append(sec_text)
            if "non-functional" in sec_name or "non functional" in sec_name:
                kb["non_functional"].append(sec_text)
            if "attachment" in sec_name:
                kb["attachment_data"].append({"source": "PRD", "text": sec_text})
            if "comment" in sec_name or "decision" in sec_name:
                kb["comments"].append({"issue": "PRD", "body": sec_text})
            # Tester steps / manual test steps from PRD
            if "test step" in sec_name or "manual" in sec_name or "step" in sec_name:
                kb["tester_steps"].append({"source": "PRD", "text": sec_text})
        print(f"  ✓ PRD parsed: {len(kb['prd_sections'])} sections")

    # ── Inbox AC fallback ─────────────────────────────────────────────────
    inbox_ac = inbox.get("acceptance_criteria", "") or \
               (jira_story or {}).get("acceptance_criteria", "")
    if inbox_ac and len(inbox_ac.strip()) > 20:
        if not any(inbox_ac[:50] in a for a in kb["acceptance_criteria"]):
            kb["acceptance_criteria"].append(inbox_ac)

    # ── Deduplicate all text lists ────────────────────────────────────────
    for key in ("acceptance_criteria", "validation_rules", "test_data", "ui_specs",
                "negative_scenarios", "non_functional", "dom_element_texts"):
        kb[key] = list(dict.fromkeys(t for t in kb[key] if t and len(t.strip()) > 5))

    total_ac_signals = (
        len(kb["acceptance_criteria"]) +
        len(kb["subtasks"]) +
        len(kb["validation_rules"]) +
        len(kb["attachment_data"]) +
        len(kb["negative_scenarios"])
    )
    kb["_total_ac_signals"] = total_ac_signals
    print(f"\n  ✓ KB complete — estimated testable signals: {total_ac_signals}")
    return kb


def _kb_to_text(kb: Dict, max_chars: int = 3000) -> str:
    """
    Render KB as labelled text for LLM prompts.
    Caps total size so small local models (Ollama) don't truncate.
    Priority order: AC > story > subtasks > validation > test_data > rest.
    """
    sections = []
    budget   = max_chars

    def _add(text: str) -> bool:
        nonlocal budget
        if budget <= 0:
            return False
        trimmed = text[:budget]
        sections.append(trimmed)
        budget -= len(trimmed)
        return True

    # Story summary (compact)
    if kb.get("story"):
        s = kb["story"]
        _add(f"=== USER STORY ===\n"
             f"Key: {s.get('key')}  Summary: {s.get('summary')}\n"
             f"Description: {s.get('description','')[:400]}")

    # Epic (compact)
    if kb.get("epic") and budget > 0:
        e = kb["epic"]
        _add(f"=== EPIC ===\n"
             f"Key: {e.get('key')}  Summary: {e.get('summary')}\n"
             f"{e.get('description','')[:200]}")

    # Acceptance criteria — highest priority, each capped
    if kb.get("acceptance_criteria") and budget > 0:
        ac_block = "=== ACCEPTANCE CRITERIA ===\n"
        for i, ac in enumerate(kb["acceptance_criteria"]):
            ac_block += f"[AC{i+1}] {ac[:400]}\n"
        _add(ac_block)

    # Subtasks
    if kb.get("subtasks") and budget > 0:
        sub_lines = ["=== SUBTASKS ==="]
        for st in kb["subtasks"]:
            sub_lines.append(
                f"[{st.get('key')}] {st.get('summary')} "
                f"(Status:{st.get('status','')})\n"
                f"  {st.get('description','')[:200]}"
            )
        _add("\n".join(sub_lines))

    # Validation rules
    if kb.get("validation_rules") and budget > 0:
        _add("=== VALIDATION RULES ===\n" +
             "\n".join(kb["validation_rules"][:10]))

    # Test data
    if kb.get("test_data") and budget > 0:
        _add("=== TEST DATA ===\n" +
             "\n".join(kb["test_data"][:10]))

    # Negative scenarios
    if kb.get("negative_scenarios") and budget > 0:
        _add("=== NEGATIVE SCENARIOS ===\n" +
             "\n".join(kb["negative_scenarios"][:5]))

    # Attachment data (trimmed)
    if kb.get("attachment_data") and budget > 0:
        att_lines = ["=== ATTACHMENT DATA ==="]
        for a in kb["attachment_data"][:10]:
            att_lines.append(f"[{a.get('source','')}] {a.get('text','')[:150]}")
        _add("\n".join(att_lines))

    # Comments (trimmed)
    if kb.get("comments") and budget > 0:
        com_lines = ["=== COMMENTS ==="]
        for c in kb["comments"][:5]:
            body  = c.get("body","")[:150] if isinstance(c, dict) else str(c)[:150]
            issue = c.get("issue","")      if isinstance(c, dict) else ""
            com_lines.append(f"[{issue}] {body}")
        _add("\n".join(com_lines))

    return "\n\n".join(sections)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 1 — AC Analyst
# ══════════════════════════════════════════════════════════════════════════════

_AC_ANALYST_SYSTEM = """\
You are a senior QA analyst. Extract testable acceptance criteria as JSON.

Output ONLY a JSON array. Each element MUST have exactly these fields:
[
  {
    "id": "AC1",
    "title": "Short title",
    "description": "What is being tested",
    "test_type": "positive",
    "page_hint": "name of page or url path segment e.g. login or checkout",
    "test_data": ["example_value"],
    "steps": [{"step": "Enter value in the field", "expected": "Field accepts it"}],
    "expected_result": "Overall pass condition",
    "source": "acceptance_criteria",
    "source_ref": "issue key or filename from the input",
    "priority": "high"
  }
]

test_type values: positive | negative | boundary | disabled | smoke
source values: acceptance_criteria | subtask | validation | attachment | comment
priority values: high | medium | low

Rules:
- Output ONLY valid JSON array, no prose, no markdown, no explanation.
- negative test_type for invalid inputs, errors, empty fields.
- boundary test_type for min/max/edge values.
- Extract ALL items — do not stop early."""


def _normalise_ac(ac: Dict, idx: int) -> Dict:
    """
    Normalise a single AC dict — handles LLM returning wrong field names.
    Maps common variants back to the expected schema.
    """
    # ID normalisation
    ac_id = (
        ac.get("id") or ac.get("ac_id") or ac.get("ID") or
        ac.get("requirement_id") or f"AC{idx+1}"
    )
    # title
    title = (
        ac.get("title") or ac.get("name") or ac.get("summary") or
        ac.get("description", "")[:60]
    )
    # description
    desc = (
        ac.get("description") or ac.get("criteria") or
        ac.get("condition") or ac.get("text") or title
    )
    # test_type — normalise any variant
    raw_type = str(
        ac.get("test_type") or ac.get("type") or ac.get("scenario_type") or "positive"
    ).lower()
    VALID_TYPES = {"positive", "negative", "boundary", "disabled", "smoke"}
    test_type = raw_type if raw_type in VALID_TYPES else (
        "negative" if any(w in raw_type for w in ("neg", "invalid", "error", "fail")) else
        "boundary" if any(w in raw_type for w in ("bound", "edge", "limit", "min", "max")) else
        "positive"
    )
    # page_hint
    page_hint = str(
        ac.get("page_hint") or ac.get("page") or ac.get("url_hint") or
        ac.get("module") or ""
    ).lower()
    # test_data
    td = ac.get("test_data") or ac.get("test_values") or ac.get("data") or []
    test_data = td if isinstance(td, list) else [str(td)] if td else []
    # steps
    steps = ac.get("steps") or ac.get("test_steps") or ac.get("scenario_steps") or []
    if not isinstance(steps, list):
        steps = []
    # expected_result
    expected = (
        ac.get("expected_result") or ac.get("expected") or
        ac.get("expected_outcome") or ac.get("pass_condition") or ""
    )
    # source / source_ref / priority
    source     = ac.get("source") or ac.get("origin") or "acceptance_criteria"
    source_ref = ac.get("source_ref") or ac.get("issue") or ac.get("ref") or ""
    priority   = ac.get("priority") or ac.get("importance") or "medium"

    return {
        "id":              str(ac_id),
        "title":           str(title),
        "description":     str(desc),
        "test_type":       test_type,
        "page_hint":       page_hint,
        "test_data":       test_data,
        "steps":           steps,
        "expected_result": str(expected),
        "source":          str(source),
        "source_ref":      str(source_ref),
        "priority":        str(priority),
    }


def agent_ac_analyst(kb: Dict, gateway) -> List[Dict]:
    """
    Agent 1: Extract ACs in chunks by source type to stay within
    Ollama context window. Normalises field names after parse.
    """
    print("\n[Agent 1 — AC Analyst] Extracting ACs from knowledgebase…")

    all_acs: List[Dict] = []

    # ── Chunk 1: Acceptance Criteria + story/epic context ────────────────
    ac_texts = kb.get("acceptance_criteria", [])
    story    = kb.get("story", {})
    subtasks = kb.get("subtasks", [])

    chunk1 = (
        f"USER STORY: {story.get('key','')} — {story.get('summary','')}\n"
        f"Description: {story.get('description','')[:500]}\n\n"
        f"ACCEPTANCE CRITERIA:\n" +
        "\n---\n".join(f"[AC{i+1}] {t[:600]}" for i, t in enumerate(ac_texts[:10]))
    )

    prompt1 = (
        f"Extract all acceptance criteria as a JSON array from this story.\n\n"
        f"{chunk1}\n\n"
        f"For each AC include concrete test steps and expected results.\n"
        f"Output ONLY the JSON array."
    )
    raw1 = _call_llm(gateway, prompt1, _AC_ANALYST_SYSTEM, "AC-Analyst-1")
    parsed1 = _extract_json(raw1)
    if isinstance(parsed1, list):
        for i, ac in enumerate(parsed1):
            if isinstance(ac, dict):
                all_acs.append(_normalise_ac(ac, len(all_acs)))
        print(f"  [Agent 1] Chunk 1 (AC+story): {len(parsed1)} items")
    else:
        print(f"  [Agent 1] Chunk 1 parse failed: {raw1[:150]}")

    # ── Chunk 2: Subtasks ────────────────────────────────────────────────
    if subtasks:
        sub_text = "SUBTASKS:\n" + "\n".join(
            f"[{s.get('key','')}] {s.get('summary','')} — {s.get('description','')[:300]}"
            for s in subtasks
        )
        prompt2 = (
            f"Extract testable ACs from these subtasks as a JSON array.\n\n"
            f"{sub_text}\n\n"
            f"Each subtask → at least 1 AC. Output ONLY the JSON array."
        )
        raw2    = _call_llm(gateway, prompt2, _AC_ANALYST_SYSTEM, "AC-Analyst-2")
        parsed2 = _extract_json(raw2)
        if isinstance(parsed2, list):
            for ac in parsed2:
                if isinstance(ac, dict):
                    n = _normalise_ac(ac, len(all_acs))
                    n["source"] = "subtask"
                    all_acs.append(n)
            print(f"  [Agent 1] Chunk 2 (subtasks): {len(parsed2)} items")

    # ── Chunk 3: Validation rules + test data + attachments ───────────────
    val_texts = kb.get("validation_rules", [])
    td_texts  = kb.get("test_data", [])
    att_data  = kb.get("attachment_data", [])

    if val_texts or td_texts or att_data:
        chunk3 = ""
        if val_texts:
            chunk3 += "VALIDATION RULES:\n" + "\n".join(val_texts[:10]) + "\n\n"
        if td_texts:
            chunk3 += "TEST DATA:\n" + "\n".join(td_texts[:10]) + "\n\n"
        if att_data:
            chunk3 += "ATTACHMENT DATA:\n" + "\n".join(
                f"[{a.get('source','')}] {a.get('text','')[:200]}"
                for a in att_data[:10]
            )
        prompt3 = (
            f"Extract testable ACs from these validation rules and test data. "
            f"Each rule → 1 positive AC + 1 negative AC.\n\n"
            f"{chunk3}\n\n"
            f"Output ONLY the JSON array."
        )
        raw3    = _call_llm(gateway, prompt3, _AC_ANALYST_SYSTEM, "AC-Analyst-3")
        parsed3 = _extract_json(raw3)
        if isinstance(parsed3, list):
            for ac in parsed3:
                if isinstance(ac, dict):
                    n = _normalise_ac(ac, len(all_acs))
                    if not n["source"] or n["source"] == "acceptance_criteria":
                        n["source"] = "validation"
                    all_acs.append(n)
            print(f"  [Agent 1] Chunk 3 (validation/data/attachments): {len(parsed3)} items")

    # ── Deduplicate by title ──────────────────────────────────────────────
    seen_titles: set = set()
    deduped: List[Dict] = []
    for ac in all_acs:
        key = ac["title"].lower().strip()[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            deduped.append(ac)

    # Re-number IDs sequentially
    for i, ac in enumerate(deduped):
        ac["id"] = f"AC{i+1}"

    if not deduped:
        print("  [Agent 1] No ACs extracted — check LLM response above")
        return []

    print(f"  [Agent 1] Total extracted: {len(deduped)} ACs after dedup:")
    for ac in deduped:
        print(f"    {ac['id']}: {ac['title'][:55]} [{ac['test_type']}] src={ac['source']}")
    return deduped


# ══════════════════════════════════════════════════════════════════════════════
# Agent 2 — DOM Mapper
# ══════════════════════════════════════════════════════════════════════════════

_DOM_MAPPER_SYSTEM = """\
You are a QA automation engineer. Map ONE acceptance criterion to real DOM elements.

Output ONLY a single JSON object (not an array):
{
  "ac_id":          "AC1",
  "page_url":       "exact URL from the DOM list below",
  "elements": [
    {"label": "Field label from DOM", "selector": "#field-selector", "action": "fill",  "value": "test value"},
    {"label": "Button label from DOM","selector": "#button-selector","action": "click", "value": ""}
  ],
  "assert_element": {"label": "result label from DOM", "selector": "#result-selector"},
  "assert_text":    "expected visible text after action",
  "assert_absent":  false
}

Rules:
- page_url MUST be one of the exact URLs listed in DOM.
- labels and selectors MUST come from the DOM list exactly.
- fill → use value from test_data. click → value = "".
- disabled element → action = "assert_disabled".
- If no elements match → page_url = first URL in list, elements = [].
- Output ONLY the JSON object, no prose, no array wrapper."""


def _keyword_dom_match(ac: Dict, dom_index: Dict[str, List[Dict]]) -> Dict:
    """
    Deterministic fallback: match AC to DOM elements using keyword overlap.
    No LLM required. Returns a mapping dict.
    """
    # Build keyword set from AC
    ac_text = " ".join([
        ac.get("title", ""),
        ac.get("description", ""),
        ac.get("page_hint", ""),
        " ".join(str(v) for v in ac.get("test_data", [])),
    ]).lower()
    ac_words = set(re.findall(r'\b\w{3,}\b', ac_text))

    # Score each page
    best_page   = ""
    best_score  = -1
    best_els: List[Dict] = []

    for page_url, elements in dom_index.items():
        # Score page URL against AC keywords
        page_words = set(re.findall(r'\b\w{3,}\b', page_url.lower()))
        page_score = len(ac_words & page_words)

        # Score elements
        matched_els = []
        for el in elements:
            el_text  = " ".join([
                el.get("label", ""), el.get("selector", ""), el.get("type", "")
            ]).lower()
            el_words = set(re.findall(r'\b\w{3,}\b', el_text))
            score    = len(ac_words & el_words)
            if score > 0 or el.get("type", "").lower() in ("submit", "button"):
                matched_els.append((score, el))

        matched_els.sort(key=lambda x: x[0], reverse=True)
        total_score = page_score + sum(s for s, _ in matched_els[:5])

        if total_score > best_score:
            best_score = total_score
            best_page  = page_url
            best_els   = [e for _, e in matched_els[:5]]

    # If no page matched at all, use first page
    if not best_page and dom_index:
        best_page = next(iter(dom_index))
        best_els  = dom_index[best_page][:3]

    # Build element list with actions
    test_data = ac.get("test_data", [])
    test_type = ac.get("test_type", "positive")
    elements  = []
    td_idx    = 0

    for el in best_els:
        el_type = el.get("type", "").lower()
        label   = el.get("label", "")
        sel     = el.get("selector", "")
        disabled = el.get("disabled", False)

        if disabled:
            action = "assert_disabled"
            value  = ""
        elif el_type in ("submit", "button") or "submit" in label.lower():
            action = "click"
            value  = ""
        elif el_type in ("input", "text", "email", "textarea"):
            action = "fill"
            value  = test_data[td_idx] if td_idx < len(test_data) else ""
            td_idx += 1
        else:
            action = "interact"
            value  = ""

        elements.append({
            "label":    label,
            "selector": sel,
            "action":   action,
            "value":    value,
        })

    # Try to find an assert element (output/result section)
    page_els  = dom_index.get(best_page, [])
    assert_el = {}
    for el in page_els:
        lbl = el.get("label", "").lower()
        sel = el.get("selector", "").lower()
        if any(w in lbl or w in sel for w in ("output", "result", "error", "message", "display")):
            assert_el = {"label": el.get("label",""), "selector": el.get("selector","")}
            break

    return {
        "ac_id":          ac.get("id", ""),
        "page_url":       best_page,
        "elements":       elements,
        "assert_element": assert_el,
        "assert_text":    ac.get("expected_result", ""),
        "assert_absent":  test_type == "negative",
        "_matched_by":    "keyword_fallback" if best_score == 0 else "keyword",
    }


def agent_dom_mapper(
    acs:            List[Dict],
    dom_index:      Dict[str, List[Dict]],
    kb:             Dict,
    gateway,
    qdrant_url:     str,
    dom_collection: str,
    project_key:    str,
) -> List[Dict]:
    """
    Agent 2: Map each AC to DOM elements.
    Strategy:
      1. Try LLM per AC (small focused prompt)
      2. Validate result has page_url
      3. Fall back to deterministic keyword matching if LLM fails
    """
    print("\n[Agent 2 — DOM Mapper] Mapping ACs to DOM elements…")
    if not acs:
        return []

    dom_sum    = _dom_summary(dom_index)
    mappings: List[Dict] = []

    for ac in acs:
        ac_id = ac.get("id", "AC?")

        # Semantic Qdrant hint for this AC
        qdrant_hint = ""
        if dom_collection and qdrant_url:
            query = f"{ac.get('title','')} {ac.get('page_hint','')}"
            hits  = _qdrant_search(dom_collection, query, gateway,
                                   limit=5, qdrant_url=qdrant_url,
                                   project_key=project_key)
            if hits:
                qdrant_hint = "Relevant DOM elements from semantic search:\n" + \
                              "\n".join(hits[:5])

        prompt = (
            f"Map this acceptance criterion to real DOM elements.\n\n"
            f"AC:\n"
            f"  id: {ac_id}\n"
            f"  title: {ac.get('title','')}\n"
            f"  description: {ac.get('description','')[:300]}\n"
            f"  page_hint: {ac.get('page_hint','')}\n"
            f"  test_data: {ac.get('test_data',[])}\n"
            f"  test_type: {ac.get('test_type','positive')}\n\n"
            f"DOM ELEMENTS (use ONLY these URLs and selectors):\n{dom_sum}\n"
            f"{qdrant_hint}\n\n"
            f"Output ONLY a single JSON object for this one AC."
        )

        mapping = None
        try:
            raw     = _call_llm(gateway, prompt, _DOM_MAPPER_SYSTEM, f"DOM-{ac_id}")
            parsed  = _extract_json(raw)

            # Accept object or single-element array
            if isinstance(parsed, list) and len(parsed) == 1:
                parsed = parsed[0]

            if isinstance(parsed, dict) and parsed.get("page_url"):
                # Validate page_url is real
                if parsed["page_url"] in dom_index or not dom_index:
                    mapping = parsed
                    mapping["ac_id"] = ac_id  # ensure correct id
                    mapping.setdefault("elements", [])
                    mapping.setdefault("assert_element", {})
                    mapping.setdefault("assert_text", "")
                    mapping.setdefault("assert_absent", False)
        except Exception as exc:
            print(f"  [Agent 2] LLM error for {ac_id}: {exc}")

        # Deterministic fallback if LLM failed or gave bad URL
        if not mapping or not mapping.get("page_url"):
            mapping = _keyword_dom_match(ac, dom_index)
            print(f"  [Agent 2] {ac_id} → keyword fallback → {mapping.get('page_url','?')}")
        else:
            print(f"  [Agent 2] {ac_id} → LLM → {mapping.get('page_url','?')} "
                  f"({len(mapping.get('elements',[]))} elements)")

        mappings.append(mapping)

    return mappings


# ══════════════════════════════════════════════════════════════════════════════
# Agent 3 — Scenario Writer
# ══════════════════════════════════════════════════════════════════════════════

_SCENARIO_WRITER_SYSTEM = """\
You are a BDD expert writing Gherkin scenarios for Playwright automation.
The audience is BOTH automation engineers and manual QA testers.

MANDATORY rules:
1. Write EXACTLY ONE Scenario or Scenario Outline per AC.
2. First step MUST be: Given I am on the "<exact page URL>" page
3. Steps are atomic — one UI action or assertion per step.
4. ONLY use element labels/selectors from the DOM mapping. Never invent.
5. Include ALL steps from the AC "steps" list with their expected sub-results
   as And/Then steps where meaningful.
6. Include the overall expected_result as the final Then assertion.
7. Negative: assert error message appears AND success element does NOT appear.
8. Positive: assert success element or expected text appears.
9. Disabled: assert element is present AND disabled.
10. Boundary: use the exact boundary values from test_data in an Examples table.
11. Step wording for human readability:
    GOOD: When I enter "Alice" in the Name field
    GOOD: And I click the Submit button
    GOOD: Then the result section should display "Name: Alice"
    GOOD: And I should not see the success message
    BAD:  When I #inputField.fill("Alice")
12. Sub-step expected results go in comments above the step:
    # Expected: field accepts the input without error
    When I enter "Alice" in the Name field
13. Tags: @PROJECT @test_type @source_ref (e.g. @PROJ-115 for subtask-derived).
14. Scenario Outline + Examples when test_data has 2+ values of the same type.
15. Above each scenario, add a comment block:
    # Test Case: <title>
    # Source: <source> (<source_ref>)
    # Priority: <priority>
    # Expected Result: <expected_result>
16. Output ONLY raw Gherkin — no Feature header, no markdown fences, no prose."""


def _clean_scenario(raw: str, ac_id: str, page_url: str,
                    test_type: str, project_key: str, source_ref: str) -> str:
    """
    Deterministically clean a single LLM-generated scenario:
    1. Strip any Feature: header the LLM added
    2. Fix tags written as prose (Tags: @x) → proper tag line
    3. Ensure Given I am on "URL" is the first step
    4. Ensure @ac_id tag is present
    """
    # Strip Feature: header lines
    raw = re.sub(r'^Feature:.*\n?', '', raw, flags=re.MULTILINE)
    # Strip prose description lines (non-step lines before Scenario:)
    raw = re.sub(r'^As a .*\n?', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'^Scope.*\n?', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'^Sources.*\n?', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'^Total Scenario.*\n?', '', raw, flags=re.MULTILINE)

    # Fix "Tags: @x @y" written as a step/prose line → convert to real tag line
    # Must appear before Scenario: line
    def fix_tags(text: str) -> str:
        lines = text.splitlines()
        out   = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            # If line looks like "Tags: @foo @bar" or "Tags: @foo", convert it
            if re.match(r'^Tags?:\s*@', stripped, re.IGNORECASE):
                tag_part = re.sub(r'^Tags?:\s*', '', stripped, flags=re.IGNORECASE)
                out.append(tag_part)
            else:
                out.append(line)
        return "\n".join(out)

    raw = fix_tags(raw)

    # Ensure @ac_id tag is on the tag line before Scenario:
    lines = raw.splitlines()
    out   = []
    scenario_found = False
    tags_injected  = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if not scenario_found and re.match(r'^@', stripped):
            # Inject @ac_id if not already there
            if not tags_injected and ac_id not in stripped:
                line = f"@{project_key} @{test_type} @{ac_id}{(' @' + source_ref.replace('-','_')) if source_ref else ''}"
                tags_injected = True
            out.append(line)

        elif not scenario_found and re.match(r'^Scenario', stripped):
            # If we reach Scenario without having seen tags, inject them now
            if not tags_injected:
                out.append(
                    f"@{project_key} @{test_type} @{ac_id}"
                    f"{(' @' + source_ref.replace('-','_')) if source_ref else ''}"
                )
                tags_injected = True
            out.append(line)
            scenario_found = True

        elif scenario_found and not re.match(r'^\s*(Given|When|And|But|Then|\*|#)', stripped) \
                and stripped and not re.match(r'^(Examples|Scenario|@|\|)', stripped):
            # Skip non-step prose lines inside the scenario body
            # (but keep Examples tables and comments)
            pass

        elif scenario_found and re.match(r'^\s*Given', stripped):
            out.append(line)
            # After Given, check if next non-empty line adds "I am on" — if this Given
            # doesn't have "am on" and page_url is known, insert it
            if page_url and "am on" not in stripped.lower() and "navigate" not in stripped.lower():
                # This Given is wrong — replace it
                out[-1] = f'  Given I am on the "{page_url}" page'
                # Keep original as When
                out.append(f'  {stripped.replace("Given","When",1)}')
        else:
            out.append(line)

    result = "\n".join(out).strip()

    # Final safety: if "Given I am on" is completely absent, prepend it
    if page_url and "I am on" not in result and "Scenario" in result:
        result = re.sub(
            r'(Scenario[^\n]*\n)',
            rf'\1  Given I am on the "{page_url}" page\n',
            result, count=1
        )

    return result


def agent_scenario_writer(
    acs:         List[Dict],
    mappings:    List[Dict],
    kb:          Dict,
    project_key: str,
    gateway,
) -> str:
    print("\n[Agent 3 — Scenario Writer] Writing scenarios…")

    mapping_by_id = {m.get("ac_id", ""): m for m in mappings}
    scenarios: List[str] = []

    # Compact validation context
    val_ctx = ""
    if kb.get("validation_rules"):
        val_ctx  = "\nValidation rules:\n" + "\n".join(kb["validation_rules"][:8])
    if kb.get("test_data"):
        val_ctx += "\nTest data:\n" + "\n".join(kb["test_data"][:8])

    for ac in acs:
        ac_id      = ac.get("id", "AC?")
        mapping    = mapping_by_id.get(ac_id, {})
        page_url   = mapping.get("page_url", "")
        elements   = mapping.get("elements", [])
        test_data  = ac.get("test_data", [])
        test_type  = ac.get("test_type", "positive")
        source_ref = ac.get("source_ref", "")
        steps      = ac.get("steps", [])
        expected   = ac.get("expected_result", "")

        if not page_url:
            print(f"  [Agent 3] Skipping {ac_id} — no page URL mapped")
            continue

        elements_text = "\n".join(
            f"  [{e.get('action','interact')}] "
            f"label={e.get('label','')!r:30} "
            f"selector={e.get('selector','')!r} "
            f"value={e.get('value','')!r}"
            for e in elements
        ) or "  (page-level interaction only)"

        assert_el    = mapping.get("assert_element", {})
        assert_block = ""
        if assert_el:
            assert_block = (
                f"\nAssertion: label={assert_el.get('label','')!r} "
                f"selector={assert_el.get('selector','')!r} "
                f"expected_text={mapping.get('assert_text','')!r} "
                f"absent={mapping.get('assert_absent', False)}"
            )

        subtask_ctx = ""
        if ac.get("source") == "subtask" and source_ref:
            for st in kb.get("subtasks", []):
                if st.get("key") == source_ref:
                    subtask_ctx = f"\nSubtask [{source_ref}]: {st.get('description','')[:300]}"
                    break

        steps_text = ""
        if steps:
            steps_text = "\nTester steps:\n"
            for i, s in enumerate(steps, 1):
                steps_text += f"  {i}. {s.get('step','')}  → Expected: {s.get('expected','')}\n"

        prompt = (
            f"Write ONE Gherkin scenario for this AC. Output ONLY raw Gherkin steps.\n\n"
            f"AC ID: {ac_id}\n"
            f"Title: {ac.get('title','')}\n"
            f"Description: {ac.get('description','')[:250]}\n"
            f"Test type: {test_type}\n"
            f"Priority: {ac.get('priority','medium')}\n"
            f"Test data: {json.dumps(test_data)}\n"
            f"Expected result: {expected}\n"
            f"{steps_text}"
            f"\nPage URL: {page_url}\n"
            f"DOM elements:\n{elements_text}"
            f"{assert_block}"
            f"{subtask_ctx}"
            f"{val_ctx}\n\n"
            f"RULES:\n"
            f"- First tag line: @{project_key} @{test_type} @{ac_id}\n"
            f"- Next line: Scenario: {ac.get('title','')}\n"
            f"- First step: Given I am on the \"{page_url}\" page\n"
            f"- Use natural language steps (no selectors in step text)\n"
            f"- Tags go on their OWN line before Scenario, not as 'Tags: ...' prose\n"
            f"- NO Feature: header, NO markdown, NO prose\n"
            f"# Test Case: {ac.get('title','')}\n"
            f"# Source: {ac.get('source','')} ({source_ref})\n"
            f"# Priority: {ac.get('priority','medium')}\n"
            f"# Expected Result: {expected}"
        )

        try:
            raw = _call_llm(gateway, prompt, _SCENARIO_WRITER_SYSTEM, f"Writer-{ac_id}")
            cleaned = _clean_scenario(raw, ac_id, page_url, test_type, project_key, source_ref)
            if cleaned and "Scenario" in cleaned:
                scenarios.append(cleaned)
                print(f"  [Agent 3] ✓ {ac_id} — {ac.get('title','')[:50]}")
            else:
                print(f"  [Agent 3] ✗ {ac_id} — empty after clean")
        except Exception as exc:
            print(f"  [Agent 3] ✗ {ac_id}: {exc}")

    return "\n\n".join(scenarios)


# ══════════════════════════════════════════════════════════════════════════════
# Agent 4 — QA Reviewer
# ══════════════════════════════════════════════════════════════════════════════

_QA_REVIEWER_SYSTEM = """\
You are a senior QA reviewer finalising a Gherkin feature file.

Tasks:
1. Wrap all scenarios in a single Feature block.
   Feature title: story summary. Add a feature-level description block
   (indented under Feature:) with: scope summary, sources used, total scenario count.
2. Remove ONLY exact duplicate scenarios (identical title AND identical steps).
   Do NOT remove scenarios covering different types or edge cases.
3. Ensure every Scenario Outline has a proper Examples table.
4. Ensure every scenario title is unique — append AC id if needed.
5. Ensure every scenario has Given I am on "..." as first step.
6. Add @smoke to the most critical happy-path scenario.
7. Preserve ALL comment blocks above scenarios (# Test Case / Source / Priority / Expected Result).
8. DO NOT change step wording, DO NOT add new steps.
9. If any of these coverage categories has ZERO scenarios, add a placeholder:
     # TODO: No scenario found for <category>
     @todo
     Scenario: [MISSING] <category> coverage
       Given this scenario needs to be written
10. Output ONLY the final Gherkin feature file — no markdown fences, no prose."""


def _deterministic_feature_wrap(
    raw_scenarios: str,
    story_summary: str,
    project_key:   str,
    kb:            Dict,
) -> str:
    """
    Deterministically assemble a valid Gherkin feature file.
    Does NOT rely on LLM for structure — only content is LLM-generated.
    """
    # Strip any Feature: blocks the LLM leaked into raw_scenarios
    raw_scenarios = re.sub(r'^Feature:.*\n?', '', raw_scenarios, flags=re.MULTILINE)
    raw_scenarios = re.sub(r'^As a .*\n?',    '', raw_scenarios, flags=re.MULTILINE)
    raw_scenarios = re.sub(r'^Scope.*\n?',    '', raw_scenarios, flags=re.MULTILINE)
    raw_scenarios = re.sub(r'^Sources.*\n?',  '', raw_scenarios, flags=re.MULTILINE)
    raw_scenarios = re.sub(r'^Total Scenario.*\n?', '', raw_scenarios, flags=re.MULTILINE)

    # Count real scenarios
    scenario_count = len(re.findall(r'^\s*Scenario', raw_scenarios, re.MULTILINE))

    epic_key = kb.get("epic", {}).get("key", "")
    n_subtasks = len(kb.get("subtasks", []))
    n_attach   = len(kb.get("attachment_data", []))

    header = (
        f"Feature: {story_summary}\n"
        f"  # Project: {project_key}\n"
        f"  # Epic: {epic_key}\n"
        f"  # Sources: Jira Story, Epic, {n_subtasks} Subtasks, "
        f"{n_attach} Attachment rows, Validation Rules, DOM\n"
        f"  # Total scenarios: {scenario_count}\n"
    )

    return header + "\n" + raw_scenarios.strip()


def agent_qa_reviewer(
    raw_scenarios: str,
    kb:            Dict,
    project_key:   str,
    gateway,
) -> str:
    print("\n[Agent 4 — QA Reviewer] Reviewing and finalising…")

    story_summary = kb.get("story", {}).get("summary", f"{project_key} validation")
    epic_summary  = kb.get("epic",  {}).get("summary", "")

    # Step A: deterministic structural wrap (no LLM needed for this)
    structured = _deterministic_feature_wrap(raw_scenarios, story_summary, project_key, kb)

    # Step B: ask LLM only to fix step wording and add missing scenarios
    # Keep prompt small — just the scenarios, not the whole KB
    coverage_check = (
        f"Ensure these coverage categories all have at least one scenario:\n"
        f"  - positive (happy path)\n"
        f"  - negative (invalid input, error message shown)\n"
        f"  - boundary (min/max values)\n"
        f"  - subtask-derived ({len(kb.get('subtasks', []))} subtasks)\n"
        f"Add @todo placeholder for any missing category."
    )

    prompt = (
        f"Fix this Gherkin feature file — clean up step wording only.\n"
        f"Do NOT change the Feature header. Do NOT add a second Feature block.\n"
        f"Do NOT rewrite steps that already work.\n\n"
        f"{coverage_check}\n\n"
        f"Rules:\n"
        f"- Every scenario MUST start with: Given I am on the \"<url>\" page\n"
        f"- Tags go on their own line before Scenario:, never as prose\n"
        f"- No markdown fences, no prose outside Gherkin syntax\n\n"
        f"Draft:\n{structured}\n\n"
        f"Output ONLY the corrected Gherkin file."
    )

    try:
        raw = _call_llm(gateway, prompt, _QA_REVIEWER_SYSTEM, "QA-Reviewer")

        # If LLM doubled the Feature header, strip the duplicate
        feature_matches = list(re.finditer(r'^Feature:', raw, re.MULTILINE))
        if len(feature_matches) > 1:
            # Keep only the first Feature block
            second_start = feature_matches[1].start()
            raw = raw[:second_start].strip()

        # If LLM dropped the Feature header entirely, restore it
        if not raw.strip().startswith("Feature:"):
            raw = structured

        print(f"  [Agent 4] ✓ Done")
        return raw.strip()

    except Exception as exc:
        print(f"  [Agent 4] ✗ {exc} — using deterministic output")
        return structured.strip()


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — Coverage Report
# ══════════════════════════════════════════════════════════════════════════════

def compute_coverage(
    acs:          List[Dict],
    final_gherkin: str,
    kb:           Dict,
    project_key:  str,
) -> Dict:
    """
    Compute coverage % and produce a structured coverage report.

    Coverage = scenarios_written / total_ac_signals_in_KB * 100

    Also breaks down by:
      - test type  (positive / negative / boundary / disabled / smoke)
      - source     (acceptance_criteria / subtask / validation / attachment / comment)
      - priority   (high / medium / low)
    """
    print("\n[Step 5 — Coverage Report] Computing coverage…")

    total_signals = kb.get("_total_ac_signals", 0) or len(acs) or 1

    # Count scenarios in the final Gherkin
    scenario_count = len(re.findall(r'^\s*Scenario', final_gherkin, re.MULTILINE))
    outline_count  = len(re.findall(r'^\s*Scenario Outline', final_gherkin, re.MULTILINE))
    todo_count     = len(re.findall(r'@todo', final_gherkin, re.IGNORECASE))

    # AC breakdown from the extracted list
    by_type: Dict[str, int]   = {}
    by_source: Dict[str, int] = {}
    by_priority: Dict[str, int] = {}
    covered_ids: List[str]    = []
    missing: List[Dict]       = []

    for ac in acs:
        ac_id    = ac.get("id", "?")
        tt       = ac.get("test_type", "positive")
        src      = ac.get("source", "general")
        priority = ac.get("priority", "medium")

        by_type[tt]           = by_type.get(tt, 0) + 1
        by_source[src]        = by_source.get(src, 0) + 1
        by_priority[priority] = by_priority.get(priority, 0) + 1

        title    = ac.get("title", "")
        gherkin_lower = final_gherkin.lower()

        # Strategy 1: exact ac_id tag present (@AC1, @AC2 etc.)
        covered = False
        if ac_id and (f"@{ac_id}" in final_gherkin or f"@{ac_id.lower()}" in gherkin_lower):
            covered = True

        # Strategy 2: full title substring
        if not covered and title and len(title) > 10 and title.lower() in gherkin_lower:
            covered = True

        # Strategy 3: keyword overlap — at least 3 significant words match
        if not covered and title:
            keywords = [w for w in re.findall(r'\b\w{4,}\b', title.lower())
                        if w not in {'with','that','this','from','into','when',
                                     'then','given','should','must','have','will',
                                     'test','scenario','valid','value','field'}]
            if keywords:
                matches = sum(1 for kw in keywords if kw in gherkin_lower)
                if matches >= min(3, len(keywords)):
                    covered = True

        if covered:
            covered_ids.append(ac_id)
        else:
            missing.append({
                "id":        ac_id,
                "title":     title,
                "test_type": tt,
                "source":    src,
                "priority":  priority,
            })

    covered   = len(covered_ids)
    extracted = len(acs)
    coverage_pct = round(covered / extracted * 100, 1) if extracted else 0.0

    # Also compute signal-level coverage (broader)
    signal_coverage_pct = round(scenario_count / total_signals * 100, 1) \
                          if total_signals else 0.0

    report = {
        "project_key":           project_key,
        "generated_at":          __import__("datetime").datetime.now().isoformat(),
        "summary": {
            "total_kb_signals":          total_signals,
            "total_acs_extracted":       extracted,
            "scenarios_written":         scenario_count,
            "scenario_outlines":         outline_count,
            "todo_placeholders":         todo_count,
            "acs_covered":               covered,
            "acs_missing":               len(missing),
            "ac_coverage_pct":           coverage_pct,
            "signal_coverage_pct":       signal_coverage_pct,
        },
        "breakdown_by_test_type":        by_type,
        "breakdown_by_source":           by_source,
        "breakdown_by_priority":         by_priority,
        "covered_ac_ids":                covered_ids,
        "missing_coverage":              missing,
        "kb_signals": {
            "acceptance_criteria_blocks": len(kb.get("acceptance_criteria", [])),
            "subtasks":                  len(kb.get("subtasks", [])),
            "validation_rules":          len(kb.get("validation_rules", [])),
            "attachment_rows":           len(kb.get("attachment_data", [])),
            "negative_scenario_blocks":  len(kb.get("negative_scenarios", [])),
        },
    }

    # Print summary
    print(f"\n  {'='*50}")
    print(f"  Coverage Report — {project_key}")
    print(f"  {'='*50}")
    print(f"  KB signals total     : {total_signals}")
    print(f"  ACs extracted        : {extracted}")
    print(f"  Scenarios written    : {scenario_count} "
          f"({outline_count} outlines, {todo_count} TODOs)")
    print(f"  AC coverage          : {covered}/{extracted} = {coverage_pct}%")
    print(f"  Signal coverage      : {scenario_count}/{total_signals} = {signal_coverage_pct}%")
    print(f"  By type  : {by_type}")
    print(f"  By source: {by_source}")
    if missing:
        print(f"  Missing coverage ({len(missing)}):")
        for m in missing[:10]:
            print(f"    ✗ {m['id']}: {m['title'][:50]} [{m['test_type']}] ({m['priority']})")
    print(f"  {'='*50}")

    return report


def save_coverage_report(report: Dict, project_key: str) -> str:
    path = os.path.join("docs", f"gherkin_coverage_{project_key}.json")
    os.makedirs("docs", exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  ✓ Coverage report saved → {path}")
    return path


def save_agent_debug(
    project_key:   str,
    kb:            Dict,
    acs:           List[Dict],
    mappings:      List[Dict],
    raw_scenarios: str,
    final_gherkin: str,
    coverage:      Dict,
) -> str:
    """
    Save every agent's individual output to a single markdown debug file.
    Path: docs/agent_debug_{PROJECT}.md
    """
    import datetime
    out_dir  = "docs"
    os.makedirs(out_dir, exist_ok=True)
    path     = os.path.join(out_dir, f"agent_debug_{project_key}.md")
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    story    = kb.get("story", {})
    epic     = kb.get("epic",  {})

    lines: List[str] = []

    # ── Header ────────────────────────────────────────────────────────────
    lines += [
        f"# Gherkin Agent Debug Report",
        f"**Project:** {project_key}  ",
        f"**Generated:** {now}  ",
        f"**Story:** {story.get('key','')} — {story.get('summary','')}  ",
        f"**Epic:** {epic.get('key','')} — {epic.get('summary','')}  ",
        "",
        "---",
        "",
    ]

    # ── Step 0: Knowledge Builder ─────────────────────────────────────────
    lines += [
        "## Step 0 — Knowledge Builder",
        "",
        "Assembles a unified knowledgebase from every source before any agent runs.",
        "",
        f"| Source | Count |",
        f"|--------|-------|",
        f"| Acceptance Criteria blocks | {len(kb.get('acceptance_criteria', []))} |",
        f"| Subtasks | {len(kb.get('subtasks', []))} |",
        f"| Comments | {len(kb.get('comments', []))} |",
        f"| Validation Rules | {len(kb.get('validation_rules', []))} |",
        f"| Test Data items | {len(kb.get('test_data', []))} |",
        f"| Attachment rows | {len(kb.get('attachment_data', []))} |",
        f"| DOM element vectors | {len(kb.get('dom_element_texts', []))} |",
        f"| PRD sections | {len(kb.get('prd_sections', {}))} |",
        f"| Negative scenario blocks | {len(kb.get('negative_scenarios', []))} |",
        f"| Total KB signals (estimated) | {kb.get('_total_ac_signals', 0)} |",
        "",
        "### Epic",
        f"**Key:** {epic.get('key','')}  ",
        f"**Summary:** {epic.get('summary','')}  ",
        f"**Status:** {epic.get('status','')}  ",
        f"**Description:** {epic.get('description','')[:500]}",
        "",
        "### Story",
        f"**Key:** {story.get('key','')}  ",
        f"**Summary:** {story.get('summary','')}  ",
        f"**Description:** {story.get('description','')[:500]}",
        "",
    ]

    if kb.get("subtasks"):
        lines.append("### Subtasks")
        for st in kb["subtasks"]:
            lines += [
                f"- **{st.get('key','')}** — {st.get('summary','')} "
                f"*(Status: {st.get('status','')})*",
                f"  {st.get('description','')[:200]}",
            ]
        lines.append("")

    if kb.get("acceptance_criteria"):
        lines.append("### Acceptance Criteria (raw blocks)")
        for i, ac in enumerate(kb["acceptance_criteria"][:10], 1):
            lines += [f"**AC Block {i}:**", f"```", ac[:600], "```", ""]

    if kb.get("validation_rules"):
        lines.append("### Validation Rules")
        for r in kb["validation_rules"][:10]:
            lines.append(f"- {r[:200]}")
        lines.append("")

    if kb.get("test_data"):
        lines.append("### Test Data")
        for t in kb["test_data"][:10]:
            lines.append(f"- {t[:200]}")
        lines.append("")

    if kb.get("attachment_data"):
        lines.append("### Attachment Data (Excel / PDF rows)")
        for a in kb["attachment_data"][:10]:
            lines.append(f"- [{a.get('source','')}] {a.get('text','')[:200]}")
        lines.append("")

    if kb.get("comments"):
        lines.append("### Comments & Decisions")
        for c in kb["comments"][:10]:
            issue = c.get("issue","") if isinstance(c, dict) else ""
            body  = c.get("body","")[:200]  if isinstance(c, dict) else str(c)[:200]
            lines.append(f"- **[{issue}]** {body}")
        lines.append("")

    lines += ["---", ""]

    # ── Agent 1: AC Analyst ───────────────────────────────────────────────
    lines += [
        "## Agent 1 — AC Analyst",
        "",
        "Reads the KB in focused chunks (AC+story, subtasks, validation/attachments) "
        "and extracts every testable condition as a structured AC list.",
        "",
        f"**Total ACs extracted:** {len(acs)}",
        "",
        "| ID | Title | Type | Source | Source Ref | Priority |",
        "|----|-------|------|--------|------------|----------|",
    ]
    for ac in acs:
        lines.append(
            f"| {ac.get('id','')} | {ac.get('title','')[:50]} | "
            f"{ac.get('test_type','')} | {ac.get('source','')} | "
            f"{ac.get('source_ref','')} | {ac.get('priority','')} |"
        )
    lines.append("")

    lines.append("### AC Details (with steps and expected results)")
    lines.append("")
    for ac in acs:
        lines += [
            f"#### {ac.get('id','')} — {ac.get('title','')}",
            f"**Type:** {ac.get('test_type','')}  "
            f"**Source:** {ac.get('source','')} ({ac.get('source_ref','')})  "
            f"**Priority:** {ac.get('priority','')}",
            f"**Description:** {ac.get('description','')}",
            f"**Test Data:** {', '.join(str(v) for v in ac.get('test_data', []))}",
            f"**Expected Result:** {ac.get('expected_result','')}",
        ]
        steps = ac.get("steps", [])
        if steps:
            lines.append("**Steps:**")
            for i, s in enumerate(steps, 1):
                lines.append(f"{i}. {s.get('step','')}  → *{s.get('expected','')}*")
        lines.append("")

    lines += ["---", ""]

    # ── Agent 2: DOM Mapper ───────────────────────────────────────────────
    lines += [
        "## Agent 2 — DOM Mapper",
        "",
        "Maps each AC to real DOM elements. "
        "Uses Qdrant ui_memory semantic search first, falls back to keyword matching.",
        "",
        "| AC ID | Page URL | Elements | Method |",
        "|-------|----------|----------|--------|",
    ]
    for m in mappings:
        method = m.get("_matched_by", "llm")
        lines.append(
            f"| {m.get('ac_id','')} | {m.get('page_url','')[:60]} | "
            f"{len(m.get('elements',[]))} | {method} |"
        )
    lines.append("")

    lines.append("### Mapping Details")
    lines.append("")
    for m in mappings:
        lines += [
            f"#### {m.get('ac_id','')} → `{m.get('page_url','')}`",
        ]
        for el in m.get("elements", []):
            lines.append(
                f"- **[{el.get('action','').upper()}]** "
                f"`{el.get('selector','')}` — {el.get('label','')} "
                f"value=`{el.get('value','')}`"
            )
        ae = m.get("assert_element", {})
        if ae:
            lines.append(
                f"- **[ASSERT]** `{ae.get('selector','')}` — {ae.get('label','')}  "
                f"expected=`{m.get('assert_text','')}` absent={m.get('assert_absent',False)}"
            )
        lines.append("")

    lines += ["---", ""]

    # ── Agent 3: Scenario Writer ──────────────────────────────────────────
    lines += [
        "## Agent 3 — Scenario Writer (raw output before QA review)",
        "",
        "Writes one Gherkin scenario per AC. "
        "Each LLM call is focused on a single AC to stay within Ollama context limits.",
        "",
        "```gherkin",
        raw_scenarios.strip(),
        "```",
        "",
        "---",
        "",
    ]

    # ── Agent 4: QA Reviewer ──────────────────────────────────────────────
    lines += [
        "## Agent 4 — QA Reviewer (final output)",
        "",
        "Wraps scenarios in a Feature block, fixes structure, enforces BDD rules, "
        "adds TODO placeholders for missing coverage categories.",
        "",
        "```gherkin",
        final_gherkin.strip(),
        "```",
        "",
        "---",
        "",
    ]

    # ── Step 5: Coverage Report ───────────────────────────────────────────
    summary = coverage.get("summary", {})
    lines += [
        "## Step 5 — Coverage Report",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total KB signals | {summary.get('total_kb_signals', 0)} |",
        f"| ACs extracted | {summary.get('total_acs_extracted', 0)} |",
        f"| Scenarios written | {summary.get('scenarios_written', 0)} |",
        f"| Scenario Outlines | {summary.get('scenario_outlines', 0)} |",
        f"| TODO placeholders | {summary.get('todo_placeholders', 0)} |",
        f"| ACs covered | {summary.get('acs_covered', 0)} |",
        f"| ACs missing | {summary.get('acs_missing', 0)} |",
        f"| **AC coverage %** | **{summary.get('ac_coverage_pct', 0)}%** |",
        f"| Signal coverage % | {summary.get('signal_coverage_pct', 0)}% |",
        "",
        "### Breakdown by Test Type",
        "| Type | Count |",
        "|------|-------|",
    ]
    for k, v in coverage.get("breakdown_by_test_type", {}).items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "### Breakdown by Source",
        "| Source | Count |",
        "|--------|-------|",
    ]
    for k, v in coverage.get("breakdown_by_source", {}).items():
        lines.append(f"| {k} | {v} |")

    lines += [
        "",
        "### Breakdown by Priority",
        "| Priority | Count |",
        "|----------|-------|",
    ]
    for k, v in coverage.get("breakdown_by_priority", {}).items():
        lines.append(f"| {k} | {v} |")

    missing = coverage.get("missing_coverage", [])
    if missing:
        lines += [
            "",
            "### Missing Coverage",
            "| ID | Title | Type | Source | Priority |",
            "|----|-------|------|--------|----------|",
        ]
        for m in missing:
            lines.append(
                f"| {m.get('id','')} | {m.get('title','')[:50]} | "
                f"{m.get('test_type','')} | {m.get('source','')} | "
                f"{m.get('priority','')} |"
            )

    with open(path, "w") as f:
        f.write("\n".join(lines))

    print(f"  ✓ Agent debug report saved → {path}")
    return path
    path = os.path.join("docs", f"gherkin_coverage_{project_key}.json")
    os.makedirs("docs", exist_ok=True)
    with open(path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  ✓ Coverage report saved → {path}")
    return path


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

def run_gherkin_agent(
    jira_story:              Optional[Dict],
    dom_data:                Dict,
    req_payloads:            List[Dict],
    project_key:             str,
    gateway,
    prd_text:                str = "",
    inbox_issues:            Optional[List[Dict]] = None,
    qdrant_url:              str = "",
    dom_collection:          str = "",
    requirements_collection: str = "",
) -> Tuple[str, Dict]:
    """
    Main entry point. Returns (gherkin_str, coverage_report_dict).

    Steps:
      0. Knowledge Builder
      1. AC Analyst
      2. DOM Mapper
      3. Scenario Writer  (with steps + expected results)
      4. QA Reviewer
      5. Coverage Report
    """
    qdrant_url = qdrant_url or QDRANT_URL

    print(f"\n{'='*60}")
    print(f"Gherkin Agent v2 — {project_key}")
    print(f"{'='*60}")

    # Step 0
    kb = build_knowledgebase(
        project_key             = project_key,
        jira_story              = jira_story,
        req_payloads            = req_payloads,
        prd_text                = prd_text,
        inbox_issues            = inbox_issues or [],
        qdrant_url              = qdrant_url,
        requirements_collection = requirements_collection,
        dom_collection          = dom_collection,
        gateway                 = gateway,
    )

    dom_idx = _dom_index(dom_data)
    print(f"\n  DOM index: {len(dom_idx)} page(s), "
          f"{sum(len(v) for v in dom_idx.values())} element(s)")

    story_summary = kb.get("story", {}).get("summary", f"{project_key} validation")

    # Agent 1
    acs = agent_ac_analyst(kb, gateway)
    if not acs:
        empty = (
            f"Feature: {project_key} — {story_summary}\n\n"
            f"  # No acceptance criteria extracted.\n"
            f"  # Check docs/inbox/{project_key}.json and PRD then re-run.\n"
        )
        empty_report = compute_coverage([], empty, kb, project_key)
        return empty, empty_report

    # Agent 2
    mappings = agent_dom_mapper(
        acs, dom_idx, kb, gateway,
        qdrant_url, dom_collection, project_key
    )

    # Agent 3
    raw_scenarios = agent_scenario_writer(acs, mappings, kb, project_key, gateway)
    if not raw_scenarios.strip():
        no_scenarios = f"Feature: {project_key} — {story_summary}\n\n  # No scenarios generated.\n"
        no_report    = compute_coverage(acs, no_scenarios, kb, project_key)
        return no_scenarios, no_report

    # Agent 4
    final_gherkin = agent_qa_reviewer(raw_scenarios, kb, project_key, gateway)

    # Step 5
    coverage_report = compute_coverage(acs, final_gherkin, kb, project_key)
    save_coverage_report(coverage_report, project_key)
    save_agent_debug(
        project_key   = project_key,
        kb            = kb,
        acs           = acs,
        mappings      = mappings,
        raw_scenarios = raw_scenarios,
        final_gherkin = final_gherkin,
        coverage      = coverage_report,
    )

    count = len(re.findall(r'^\s*Scenario', final_gherkin, re.MULTILINE))
    pct   = coverage_report["summary"]["ac_coverage_pct"]
    print(f"\n{'='*60}")
    print(f"Gherkin Agent v2 — {count} scenario(s) | {pct}% AC coverage")
    print(f"{'='*60}\n")

    return final_gherkin, coverage_report


# ══════════════════════════════════════════════════════════════════════════════
# Standalone CLI
# ══════════════════════════════════════════════════════════════════════════════

def _load_dom(project_key: str) -> Dict:
    for pattern in [
        os.path.join("docs", f"live_dom_elements_{project_key}_*.json"),
        os.path.join("docs", "live_dom_elements*.json"),
    ]:
        candidates = sorted(glob.glob(pattern))
        if candidates:
            latest = max(candidates, key=os.path.getmtime)
            with open(latest) as f:
                return json.load(f)
    print(f"No DOM file found for {project_key}")
    return {}


def _load_prd(project_key: str) -> str:
    for path in [
        os.path.join("docs", "requirements", f"{project_key}_PRD.md"),
        os.path.join("docs", f"{project_key}_prd.md"),
        os.path.join("docs", "prd.md"),
    ]:
        if os.path.exists(path):
            return open(path).read().strip()
    return ""


def _sanitize(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_') or 'collection'


def main():
    parser = argparse.ArgumentParser(description="Gherkin Agent v2")
    parser.add_argument("--project", required=True, help="Project key e.g. SCRUM-70")
    parser.add_argument("--output",  default="",    help="Output .feature file path")
    args = parser.parse_args()

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from llm_gateway import get_llm_gateway

    project_key = args.project
    dom_data    = _load_dom(project_key)
    prd_text    = _load_prd(project_key)
    gateway     = get_llm_gateway()

    gherkin, coverage = run_gherkin_agent(
        jira_story              = None,
        dom_data                = dom_data,
        req_payloads            = [],
        project_key             = project_key,
        gateway                 = gateway,
        prd_text                = prd_text,
        inbox_issues            = [],
        qdrant_url              = QDRANT_URL,
        dom_collection          = _sanitize(f"{project_key}_ui_memory"),
        requirements_collection = _sanitize(f"{project_key}_requirements"),
    )

    out_path = args.output or os.path.join("tests", "features", f"{project_key}.feature")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(gherkin)
    print(f"\nFeature file → {out_path}")
    print(f"Coverage     → docs/gherkin_coverage_{project_key}.json")


if __name__ == "__main__":
    main()