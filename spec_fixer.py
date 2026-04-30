#!/usr/bin/env python3
"""
Spec Fixer — Post-processor for generated Playwright TypeScript specs
=====================================================================
Generalised v2 — works for ANY site / Jira project, not just demoqa.

Root causes fixed
─────────────────
  RC-1  Wrong selector from Qdrant ambiguous match
        Fills mapped to the wrong field (e.g. #currentAddress for an email step).
        FIX: Re-map using field-keyword → DOM index lookup, fully driven by the
             live DOM JSON (no hardcoded selectors).

  RC-2  Fragile positional XPath
        xpath=/html/body/.../li[N]/a matches multiple elements across page contexts.
        FIX: Replace all positional XPaths with the best stable selector derived
             from the step intent + DOM index (id > aria-label > name > text).

  RC-3  smartAction fill called with no value
        "I enter invalid email syntax" has no quoted value → throws at runtime.
        FIX: Deduce a domain-appropriate value from intent + test name; use the
             DOM index to pick the right field.

  RC-4  Assertion polarity inverted (verifyAbsent emits toBeVisible)
        Steps like "the output should not be displayed" resolve to toBeVisible()
        instead of not.toBeVisible() / toBeHidden().
        FIX: Pattern-match a generalised set of negation phrases and flip
             toBeVisible() → not.toBeVisible() (or emit toBeHidden() with a
             direct selector).

  RC-5  toBeDisabled() on an always-enabled element
        spec_fixer or step_generator deduced "disabled" when the element is
        always enabled on that page (e.g. #submit on /text-box).
        FIX: Validate against the DOM index; if element is not marked disabled,
             revert to toBeEnabled() and emit a warning comment.

  RC-6  assert_text / toContainText on a wrong (input) element
        Text assertions sent to an input field instead of an output/result
        element.
        FIX: Generalised output-element detection via DOM index (elements whose
             id/label matches output/result/message/alert/toast patterns); falls
             back to the first non-input element on the page.

Usage
─────
  python3 spec_fixer.py --project SCRUM-70
  python3 spec_fixer.py --project PROJ-42 --spec path/to/custom.spec.ts
  python3 spec_fixer.py --project SCRUM-70 --dry-run

Reads
─────
  tests/steps/{PROJECT}.spec.ts        — generated spec to fix
  tests/features/{PROJECT}.feature     — original Gherkin (context)
  docs/live_dom_elements_*.json        — crawled DOM (selector source of truth)

Writes
──────
  tests/steps/{PROJECT}.spec.ts        — fixed spec (in-place, unless --dry-run)
  tests/steps/{PROJECT}_fix_log.md     — human-readable change log
"""

from __future__ import annotations

import os
import re
import sys
import json
import glob
import argparse
import datetime
from typing import Dict, List, Tuple, Optional


# ══════════════════════════════════════════════════════════════════════════════
# §1  DOM INDEX — built from live_dom_elements JSON, no project-specific logic
# ══════════════════════════════════════════════════════════════════════════════

def load_dom_index(project_key: str) -> Dict[str, Dict]:
    """
    Build a normalised label → element index from the live DOM capture.

    The index is keyed by every human-readable name we can extract from an
    element (lowercased).  Each value carries:
      selector  — most stable CSS selector (id preferred)
      type      — tagName or element group
      page      — URL where the element was captured
      label     — original label text
      disabled  — bool (from isDisabled / disabled attribute)
      id        — raw id string (empty if none)
      is_output — True when the element looks like a result/output container
                  (not an input field)

    Heuristics for is_output (site-agnostic):
      • tagName is div, p, span, section, article, aside, output
      • role is status, alert, log, region, main
      • id/label/class contains: output, result, message, alert, toast,
        feedback, error, success, notification, summary, response
    """
    index: Dict[str, Dict] = {}

    patterns = [
        os.path.join("docs", f"live_dom_elements_{project_key}_*.json"),
        os.path.join("docs", f"live_dom_elements_{project_key}.json"),
        os.path.join("docs", "live_dom_elements*.json"),
    ]
    dom_file = ""
    for pat in patterns:
        candidates = sorted(glob.glob(pat))
        if candidates:
            dom_file = max(candidates, key=os.path.getmtime)
            break

    if not dom_file:
        print(f"  ⚠ No DOM file found for {project_key} — selector fixes will use fallbacks only")
        return index

    with open(dom_file) as f:
        dom_data = json.load(f)

    # Tags / roles that indicate a display/output element (not an editable field)
    OUTPUT_TAGS  = {"div", "p", "span", "section", "article", "aside", "output",
                    "li", "ul", "ol", "table", "tbody", "tr", "td", "blockquote"}
    OUTPUT_ROLES = {"status", "alert", "log", "region", "main", "contentinfo",
                    "complementary", "note"}
    OUTPUT_KEYWORDS = {"output", "result", "message", "alert", "toast", "feedback",
                       "error", "success", "notification", "summary", "response",
                       "display", "report", "info", "warning"}

    INPUT_GROUPS = {"input_elements", "textarea_elements", "select_elements",
                    "dropdown_elements", "custom_dropdown_elements"}

    def _is_output_element(el: Dict, group: str) -> bool:
        if group not in INPUT_GROUPS:
            tag  = str(el.get("tagName") or el.get("type") or "").lower()
            role = str(el.get("role") or el.get("ariaRole") or "").lower()
            if tag in OUTPUT_TAGS or role in OUTPUT_ROLES:
                return True
        # Keyword scan across id, label, class, placeholder
        haystack = " ".join([
            str(el.get("id") or ""),
            str(el.get("label") or ""),
            str(el.get("class") or el.get("className") or ""),
            str(el.get("placeholder") or ""),
        ]).lower()
        return any(kw in haystack for kw in OUTPUT_KEYWORDS)

    all_groups = [
        "input_elements", "button_elements", "textarea_elements",
        "dropdown_elements", "custom_dropdown_elements",
        "output_elements", "display_elements", "text_elements",
        "elements",  # generic fallback key some crawlers use
    ]

    for group in all_groups:
        for el in dom_data.get(group, []):
            el_id    = el.get("id", "")
            label    = (
                el.get("label") or el.get("placeholder") or
                el.get("text")  or el.get("name") or el_id or ""
            ).strip()
            sel      = f"#{el_id}" if el_id else el.get("selector", "")
            page     = el.get("page_url") or el.get("url", "")
            el_type  = el.get("type") or el.get("tagName") or group.replace("_elements", "")
            disabled = bool(el.get("isDisabled") or el.get("disabled"))
            is_out   = _is_output_element(el, group)

            if label and sel:
                entry = {
                    "selector":  sel,
                    "type":      el_type,
                    "page":      page,
                    "label":     label,
                    "disabled":  disabled,
                    "id":        el_id,
                    "is_output": is_out,
                }
                for key in _label_keys(label, el_id):
                    if key not in index:          # first occurrence wins
                        index[key] = entry

    print(f"  ✓ DOM index loaded: {len(index)} elements from {dom_file}")
    return index


def _label_keys(label: str, el_id: str) -> List[str]:
    """Return all index keys for an element (lowercase)."""
    keys = [label.lower().strip()]
    if el_id:
        keys.append(el_id.lower())
        # Also index without common prefixes: userEmail → email
        stripped = re.sub(r'^(user|form|input|field|txt|txt_)', '', el_id.lower())
        if stripped and stripped != el_id.lower():
            keys.append(stripped)
    return list(dict.fromkeys(keys))


# ══════════════════════════════════════════════════════════════════════════════
# §2  FIELD KEYWORD MAP  — site-agnostic, driven by common Gherkin vocabulary
# ══════════════════════════════════════════════════════════════════════════════

# Each entry: (gherkin_keywords, dom_label_hints, fallback_selector)
# dom_label_hints are tried against the DOM index first; fallback used only
# when the DOM index has no match.
#
# This list intentionally covers standard web-form vocabulary.  For a new
# project with unusual field names, add entries here OR ensure the DOM JSON
# captures those labels — no other changes needed.
FIELD_KEYWORD_MAP: List[Tuple[List[str], List[str], str]] = [
    (["full name", "name field", "your name"],  ["full name", "username", "name"],  "#userName"),
    (["email", "e-mail", "email address"],       ["email", "e-mail"],               "#userEmail"),
    (["current address"],                        ["current address"],               "#currentAddress"),
    (["permanent address"],                      ["permanent address"],             "#permanentAddress"),
    (["age"],                                    ["age"],                           "#age"),
    (["salary"],                                 ["salary"],                        "#salary"),
    (["department"],                             ["department"],                    "#department"),
    (["first name"],                             ["first name", "firstname"],       "#firstName"),
    (["last name", "surname"],                   ["last name", "lastname"],         "#lastName"),
    (["mobile", "phone", "telephone"],           ["mobile", "phone", "tel"],       "#userNumber"),
    (["subject"],                                ["subject"],                       "#subjectsInput"),
    (["message", "comment", "note"],             ["message", "comment", "note"],   "#currentAddress"),
    (["username", "user name", "login"],         ["username", "user name"],        "#userName"),
    (["password"],                               ["password"],                      "#password"),
    (["search"],                                 ["search"],                        "#searchBox"),
    (["submit", "submit button"],                ["submit"],                        "#submit"),
    (["add", "add button", "add new"],           ["add", "addnewrecord", "new"],   "#addNewRecordButton"),
]

# Output/result element patterns — checked against id, label, class, placeholder
OUTPUT_KEYWORD_PATTERNS: List[str] = [
    "output", "result", "message", "alert", "toast", "feedback",
    "error", "success", "notification", "summary", "response", "display",
]


# ══════════════════════════════════════════════════════════════════════════════
# §3  NEGATION VOCABULARY  — generalised "should not be visible" detection
# ══════════════════════════════════════════════════════════════════════════════

# Any substring match (case-insensitive) in the line triggers a polarity flip
# from toBeVisible() → not.toBeVisible() / toBeHidden().
ABSENT_PHRASES: List[str] = [
    "should not be displayed",
    "should not be visible",
    "should not be rendered",
    "should not be shown",
    "should not appear",
    "should not be present",
    "should not show",
    "should not see",
    "not displayed",
    "not rendered",
    "not visible",
    "not shown",
    "not be present",
    "not be visible",
    "not be displayed",
    "should be hidden",
    "should be absent",
    "should be gone",
    "should disappear",
    "must not appear",
    "no longer visible",
    "no longer shown",
    "no longer displayed",
    "is not displayed",
    "is not rendered",
    "is not visible",
    "is not shown",
    "verifyabsent",            # generator annotation injected as a comment
]


def line_is_absent_assertion(line: str) -> bool:
    lower = line.lower()
    return any(phrase in lower for phrase in ABSENT_PHRASES)


# ══════════════════════════════════════════════════════════════════════════════
# §4  SELECTOR UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def is_positional_xpath(selector: str) -> bool:
    """True for positional XPaths that break when the DOM changes."""
    return bool(re.match(r'^(xpath=)?/html/body/', selector or ""))


def is_ambiguous_selector(selector: str, intent: str, dom_index: Dict) -> bool:
    """
    True when the CSS id selector in the spec line does NOT match the field
    described in the intent / test name.

    Fully generalised: compares the id against every key in FIELD_KEYWORD_MAP
    and returns True when there is a mismatch.  No project-specific strings.
    """
    if not selector or not selector.startswith("#"):
        return False
    sel_id    = selector.lstrip("#").lower()
    intent_l  = intent.lower()

    # If the selector id directly appears in the intent, it's plausibly correct
    if sel_id in intent_l:
        return False

    # Walk the field map: for every group, check if the intent mentions a field
    # from that group but the selector points to a *different* group
    for step_kws, dom_hints, fallback in FIELD_KEYWORD_MAP:
        intent_names_this_field = any(kw in intent_l for kw in step_kws)
        fallback_id = fallback.lstrip("#").lower()
        selector_is_this_field = (sel_id == fallback_id or
                                  any(h in sel_id for h in dom_hints))
        if intent_names_this_field and not selector_is_this_field:
            return True

    return False


def resolve_field_selector(intent: str, dom_index: Dict,
                           for_output: bool = False) -> Tuple[str, str]:
    """
    Given step intent text, return (selector, label).
    for_output=True → look for a display/result element, not an input field.
    """
    lower = intent.lower()

    if for_output:
        # 1. Check DOM index for output elements
        for key, el in dom_index.items():
            if el.get("is_output") and any(kw in key for kw in OUTPUT_KEYWORD_PATTERNS):
                return el["selector"], el["label"]
        # 2. Check keyword patterns against all ids/labels in DOM index
        for kw in OUTPUT_KEYWORD_PATTERNS:
            for key, el in dom_index.items():
                if kw in key:
                    return el["selector"], el["label"]
        return "", ""

    # Input field resolution
    for step_kws, dom_hints, fallback in FIELD_KEYWORD_MAP:
        if any(kw in lower for kw in step_kws):
            for hint in dom_hints:
                if hint in dom_index:
                    el = dom_index[hint]
                    return el["selector"], el["label"]
            return fallback, step_kws[0]

    return "", ""


def best_output_selector(dom_index: Dict) -> str:
    """
    Return the best output element selector from the DOM index.
    Falls back to a '#output' convention if nothing is found.
    """
    sel, _ = resolve_field_selector("", dom_index, for_output=True)
    return sel or "#output"


# ══════════════════════════════════════════════════════════════════════════════
# §5  VALUE DEDUCTION  — for fill steps with no quoted value in Gherkin
# ══════════════════════════════════════════════════════════════════════════════

# Field → sensible invalid test data (for "invalid", "missing", "wrong" steps)
FIELD_INVALID_VALUES: Dict[str, str] = {
    "email":      "invalid-email",
    "age":        "abc",
    "salary":     "xyz",
    "name":       "123",
    "phone":      "notaphone",
    "mobile":     "notamobile",
    "department": "!@#",
    "subject":    "???",
    "password":   "!!bad!!",
    "username":   "!!!",
}

# Field → sensible valid test data
FIELD_VALID_VALUES: Dict[str, str] = {
    "email":      "user@example.com",
    "age":        "25",
    "salary":     "50000",
    "name":       "John Doe",
    "phone":      "1234567890",
    "mobile":     "1234567890",
    "department": "Engineering",
    "subject":    "Maths",
    "password":   "Test@1234",
    "username":   "testuser",
}

INVALID_SIGNALS = ["invalid", "missing", "wrong", "bad", "non-numeric",
                   "empty", "blank", "no @", "no tld", "incorrect",
                   "malformed", "special char", "special character"]
VALID_SIGNALS   = ["valid", "correct", "proper", "success", "numeric",
                   "acceptable", "legal"]


def deduce_fill_value(intent: str, test_name: str) -> str:
    lower_i = intent.lower()
    lower_t = test_name.lower()

    is_invalid = any(w in lower_i or w in lower_t for w in INVALID_SIGNALS)
    pool = FIELD_INVALID_VALUES if is_invalid else FIELD_VALID_VALUES

    # Check every field keyword against intent + test name
    for step_kws, _, _ in FIELD_KEYWORD_MAP:
        for kw in step_kws:
            if kw in lower_i or kw in lower_t:
                # Use the first matching key in the pool
                for pool_key in pool:
                    if pool_key in kw or kw in pool_key:
                        return pool[pool_key]

    return "invalid-input" if is_invalid else "test-value"


# ══════════════════════════════════════════════════════════════════════════════
# §6  LINE-LEVEL EXTRACTORS
# ══════════════════════════════════════════════════════════════════════════════

def classify_action(line: str) -> str:
    lower = line.lower()
    stripped = line.strip()
    if "page.goto" in lower:
        return "navigate"
    if re.search(r'locator\(["\'][^"\']+["\']\)\.fill\(', stripped):
        return "fill"
    if re.search(r'locator\(["\'][^"\']+["\']\)\.click\(\)', stripped):
        return "click"
    if "tobedisabled" in lower:
        return "assert_disabled"
    if "tobeenabled" in lower:
        return "assert_enabled"
    if "not.tobevisible" in lower or "tobehidden" in lower:
        return "assert_hidden"
    if "tobevisible" in lower or "tobeempty" in lower:
        return "assert_visible"
    if "tocontaintext" in lower:
        return "assert_text"
    if "smartaction" in lower:
        # Empty-value fill via smartAction("intent", "")
        if re.search(r'smartAction\(["\'][^"\']+["\'],\s*["\']["\']', stripped):
            return "fill_empty"
        return "smart"
    return "unknown"


def extract_selector(line: str) -> str:
    m = re.search(r'locator\(["\']([^"\']+)["\']\)', line)
    return m.group(1) if m else ""


def extract_fill_value(line: str) -> str:
    m = re.search(r'\.fill\(["\']([^"\']*)["\']', line)
    return m.group(1) if m else ""


def extract_intent(line: str) -> str:
    m = re.search(r'smartAction\(["\']([^"\']+)["\']', line)
    return m.group(1) if m else ""


def extract_assert_value(line: str) -> str:
    m = re.search(r'toContainText\(["\']([^"\']+)["\']', line)
    return m.group(1) if m else ""


def extract_indent(line: str) -> str:
    return re.match(r'^(\s*)', line).group(1)


def selector_is_placeholder(selector: str) -> bool:
    sel = (selector or "").strip()
    if not sel:
        return False
    upper = sel.upper()
    return "TODO" in upper or "PLACEHOLDER" in upper or "/*" in sel or "*/" in sel


# ══════════════════════════════════════════════════════════════════════════════
# §7  LINE FIXER  — one rule per root cause, fully generalised
# ══════════════════════════════════════════════════════════════════════════════

def fix_line(line: str, test_name: str, dom_index: Dict,
             fixes: List[str]) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("//"):
        return line

    action  = classify_action(stripped)
    indent  = extract_indent(line)
    original = line
    sel = extract_selector(stripped)

    # ─────────────────────────────────────────────────────────────────────────
    # RC-0  Placeholder / invalid selector cleanup
    # ─────────────────────────────────────────────────────────────────────────
    if sel and selector_is_placeholder(sel):
        out_sel = best_output_selector(dom_index) or "#output"

        if action == "assert_hidden":
            new_line = f'{indent}await expect(basePage.page.locator("{out_sel}")).not.toBeVisible();  // FIX RC-0: removed placeholder selector\n'
        elif action == "assert_visible":
            new_line = f'{indent}await expect(basePage.page.locator("{out_sel}")).toBeVisible();  // FIX RC-0: removed placeholder selector\n'
        elif action == "assert_text":
            val = extract_assert_value(stripped)
            new_line = f'{indent}await expect(basePage.page.locator("{out_sel}")).toContainText("{val}");  // FIX RC-0: removed placeholder selector\n'
        else:
            new_line = line

        if new_line != line:
            fixes.append(f"RC-0 Placeholder selector removed: `{sel[:40]}` → `{out_sel}`")
            return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-1  Wrong selector on fill — re-map to correct field
    # ─────────────────────────────────────────────────────────────────────────
    if action == "fill":
        val = extract_fill_value(stripped)
        ctx = test_name + " " + stripped
        if sel and is_ambiguous_selector(sel, ctx, dom_index):
            better, label = resolve_field_selector(ctx, dom_index)
            if better and better != sel:
                new_line = f'{indent}await basePage.page.locator("{better}").fill("{val}");\n'
                fixes.append(f"RC-1 Wrong fill selector: `{sel}` → `{better}` ({label})")
                return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-2  Positional XPath → stable selector or smartAction
    # ─────────────────────────────────────────────────────────────────────────
    if action in ("fill", "click") and is_positional_xpath(extract_selector(stripped)):
        # Try to get intent from trailing comment
        comment_m = re.search(r'//\s*(.+?)(?:\s*/\*|$)', stripped)
        intent    = comment_m.group(1).strip() if comment_m else "interact with element"
        better, _ = resolve_field_selector(intent, dom_index)
        if better and not is_positional_xpath(better):
            if action == "fill":
                val      = extract_fill_value(stripped)
                new_line = f'{indent}await basePage.page.locator("{better}").fill("{val}");\n'
            else:
                new_line = f'{indent}await basePage.page.locator("{better}").click();\n'
        else:
            if action == "fill":
                val      = extract_fill_value(stripped)
                new_line = f'{indent}await basePage.smartAction("{intent}", "{val}");\n'
            else:
                new_line = f'{indent}await basePage.smartAction("{intent}");  // FIX RC-2: replaced positional XPath\n'
        fixes.append(f"RC-2 XPath→stable: `{sel[:60]}` → `{new_line.strip()[:70]}`")
        return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-3  smartAction fill with empty / missing value — deduce it
    # ─────────────────────────────────────────────────────────────────────────
    if action == "fill_empty":
        intent    = extract_intent(stripped)
        better, _ = resolve_field_selector(intent + " " + test_name, dom_index)
        fill_val  = deduce_fill_value(intent + " " + test_name, test_name)
        if better:
            new_line = f'{indent}await basePage.page.locator("{better}").fill("{fill_val}");  // FIX RC-3: deduced value\n'
        else:
            new_line = f'{indent}await basePage.smartAction("{intent}", "{fill_val}");  // FIX RC-3: deduced value\n'
        fixes.append(f"RC-3 Missing fill value: `{intent[:50]}` → fill({fill_val!r}) on {better or 'smartAction'}")
        return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-4  toBeVisible() with absent/negation intent → not.toBeVisible()
    # ─────────────────────────────────────────────────────────────────────────
    if action == "assert_visible" and line_is_absent_assertion(line):
        # Prefer a direct output selector if the assertion target is unclear
        if not sel or is_ambiguous_selector(sel, "output display result", dom_index):
            out_sel = best_output_selector(dom_index)
            sel     = out_sel if out_sel else sel
        if sel:
            new_line = f'{indent}await expect(basePage.page.locator("{sel}")).not.toBeVisible();  // FIX RC-4: negation\n'
        else:
            # No selector — delegate to smartAction which reads ABSENT_INTENT_SIGNALS
            intent   = extract_intent(stripped) or "element should not be visible"
            new_line = f'{indent}await basePage.smartAction("{intent}");  // FIX RC-4: verifyAbsent\n'
        fixes.append(f"RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)")
        return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-4b  smartAction with absent-intent comment → emit not.toBeVisible()
    #        Handles: smartAction("…");  // verifyAbsent …
    # ─────────────────────────────────────────────────────────────────────────
    if action == "smart" and line_is_absent_assertion(line):
        intent    = extract_intent(stripped)
        out_sel   = best_output_selector(dom_index)
        if out_sel:
            new_line = f'{indent}await expect(basePage.page.locator("{out_sel}")).not.toBeVisible();  // FIX RC-4b: verifyAbsent\n'
        else:
            # Keep as smartAction — BasePage G1 will handle polarity
            new_line = f'{indent}await basePage.smartAction("{intent}");  // verifyAbsent\n'
            return new_line    # no change in behaviour; skip fix log entry
        fixes.append(f"RC-4b smartAction verifyAbsent → direct not.toBeVisible(): `{intent[:50]}`")
        return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-5  toBeDisabled() on an element that is not disabled in the DOM
    # ─────────────────────────────────────────────────────────────────────────
    if action == "assert_disabled":
        if sel:
            sel_id  = sel.lstrip("#").lower()
            dom_el  = dom_index.get(sel_id) or dom_index.get(sel.lower())
            if dom_el and not dom_el.get("disabled"):
                # Element is always-enabled → switch to toBeEnabled()
                new_line = (
                    f'{indent}await expect(basePage.page.locator("{sel}")).toBeEnabled();'
                    f'  // FIX RC-5: DOM says enabled — changed from toBeDisabled()\n'
                )
                fixes.append(f"RC-5 toBeDisabled→toBeEnabled: `{sel}` is not disabled per DOM index")
                return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-6  assert_text / toContainText on an input field — move to output el
    # ─────────────────────────────────────────────────────────────────────────
    if action == "assert_text":
        if sel and not is_ambiguous_selector(sel, "output display result", dom_index):
            # Selector already looks like an output element — leave it alone
            pass
        elif sel:
            val     = extract_assert_value(stripped)
            out_sel = best_output_selector(dom_index)
            if out_sel and out_sel != sel:
                new_line = (
                    f'{indent}await expect(basePage.page.locator("{out_sel}")).toContainText("{val}");'
                    f'  // FIX RC-6: text assertion moved to output element\n'
                )
                fixes.append(f"RC-6 assert_text wrong element: `{sel}` → `{out_sel}` for value {val!r}")
                return new_line

    # ─────────────────────────────────────────────────────────────────────────
    # RC-1b  Wrong selector on visible assertion — remap to output element
    # ─────────────────────────────────────────────────────────────────────────
    if action == "assert_visible":
        if sel and is_ambiguous_selector(sel, "output display result", dom_index):
            out_sel = best_output_selector(dom_index)
            if out_sel and out_sel != sel:
                # Preserve .toBeVisible() vs .toBeEnabled() vs .toBeEmpty()
                if "toBeEmpty" in stripped:
                    assertion = "toBeEmpty()"
                elif "toBeEnabled" in stripped:
                    assertion = "toBeEnabled()"
                else:
                    assertion = "toBeVisible()"
                new_line = (
                    f'{indent}await expect(basePage.page.locator("{out_sel}")).{assertion};'
                    f'  // FIX RC-1b: assertion selector remapped\n'
                )
                fixes.append(f"RC-1b Wrong assert selector: `{sel}` → `{out_sel}`")
                return new_line

    return line


# ══════════════════════════════════════════════════════════════════════════════
# §8  SPEC FILE PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

def fix_spec_file(spec_path: str, dom_index: Dict) -> Tuple[str, List[str]]:
    with open(spec_path) as f:
        lines = f.readlines()

    fixed_lines: List[str] = []
    all_fixes:   List[str] = []
    current_test = ""

    for line in lines:
        m = re.match(r'\s*test\(["\'](.+?)["\']', line)
        if m:
            current_test = m.group(1)

        fixed = fix_line(line, current_test, dom_index, all_fixes)
        fixed_lines.append(fixed)

    return "".join(fixed_lines), all_fixes


# ══════════════════════════════════════════════════════════════════════════════
# §9  FIX LOG WRITER
# ══════════════════════════════════════════════════════════════════════════════

def write_fix_log(fixes: List[str], project_key: str,
                  spec_path: str, log_path: str) -> None:
    lines = [
        f"# Spec Fixer Log — {project_key}",
        f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Spec:** {spec_path}",
        f"**Total fixes applied:** {len(fixes)}",
        "",
        "## Root Causes (generalised — site-agnostic)",
        "",
        "| Code | Root Cause | Fix Applied |",
        "|------|-----------|-------------|",
        "| RC-1  | Wrong fill selector (Qdrant ambiguous match) | Remapped via FIELD_KEYWORD_MAP + DOM index |",
        "| RC-1b | Wrong assertion selector (input instead of output) | Remapped to output element from DOM index |",
        "| RC-2  | Positional XPath (li[N]/a matches multiple) | Replaced with stable id/aria/role selector |",
        "| RC-3  | smartAction fill with no value | Value deduced from intent + test name context |",
        "| RC-4  | toBeVisible() on absent/negation intent | Flipped to not.toBeVisible() |",
        "| RC-4b | smartAction with verifyAbsent comment | Converted to direct not.toBeVisible() |",
        "| RC-5  | toBeDisabled() on always-enabled element | Changed to toBeEnabled() with warning |",
        "| RC-6  | toContainText on input field instead of output | Moved assertion to output element |",
        "",
        "## Fixes Applied",
        "",
    ]
    for i, fix in enumerate(fixes, 1):
        lines.append(f"{i}. {fix}")

    with open(log_path, "w") as f:
        f.write("\n".join(lines))
    print(f"  ✓ Fix log saved → {log_path}")


# ══════════════════════════════════════════════════════════════════════════════
# §10  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Spec Fixer — generalised Playwright spec post-processor")
    parser.add_argument("--project", required=True, help="Project key, e.g. SCRUM-70")
    parser.add_argument("--spec",    default="",    help="Path to spec (default: tests/steps/{PROJECT}.spec.ts)")
    parser.add_argument("--dry-run", action="store_true", help="Print fixes but do not write files")
    args = parser.parse_args()

    project_key = args.project
    spec_path   = args.spec or os.path.join("tests", "steps", f"{project_key}.spec.ts")
    log_path    = os.path.join("tests", "steps", f"{project_key}_fix_log.md")

    print(f"\n{'='*60}")
    print(f"Spec Fixer — {project_key}")
    print(f"{'='*60}")
    print(f"  Spec : {spec_path}")

    if not os.path.exists(spec_path):
        print(f"  ✗ Spec file not found: {spec_path}")
        sys.exit(1)

    dom_index = load_dom_index(project_key)

    print(f"\n  Applying fixes…")
    fixed_content, all_fixes = fix_spec_file(spec_path, dom_index)

    print(f"\n  Fixes applied: {len(all_fixes)}")
    for fix in all_fixes:
        print(f"    • {fix}")

    if args.dry_run:
        print("\n  [DRY RUN] No files written.")
        return

    with open(spec_path, "w") as f:
        f.write(fixed_content)
    print(f"\n  ✓ Fixed spec written → {spec_path}")

    write_fix_log(all_fixes, project_key, spec_path, log_path)

    print(f"\n{'='*60}")
    print(f"✓ Spec Fixer complete — {len(all_fixes)} fix(es) applied")
    print(f"  Now run: npx playwright test {spec_path} --project=chromium")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
