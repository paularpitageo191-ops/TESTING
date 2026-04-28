#!/usr/bin/env python3
"""
Discovery Engine — QA Intelligence Engine  (v3)
================================================
Captures the live UI state of any web application and stores the result as:
  • A timestamped JSON file  (docs/live_dom_elements_{PROJECT}_{TS}.json)
  • Vectors in Qdrant        ({PROJECT_KEY}_ui_memory)

New in v3 (backward-compatible extensions)
───────────────────────────────────────────
  1.  QA Signal Enrichment   — visibility, bounding box, obstruction detection,
                               clickable_score, qa_status per element.
  2.  Overlay Detection      — fixed-position / high-z-index elements flagged.
  3.  QA Summary             — total_elements, risky_elements, overlay_present.
  4.  Screenshot Linking     — full-page screenshot path stored in dom_data.
  5.  URL priority           — CLI --url > BASE_URL (.env) > error.
  6.  All output keys are ADDITIVE — existing consumers are unaffected.

Usage
─────
  python3 dom_capture.py --project SCRUM-86
  python3 dom_capture.py --project SCRUM-86 --url https://staging.example.com
  python3 dom_capture.py --project SCRUM-86 --vectorize
  python3 dom_capture.py --project SCRUM-86 --urls /login /dashboard
  python3 dom_capture.py --project SCRUM-86 --session docs/session.json
  python3 dom_capture.py --project SCRUM-86 --mobile
"""
from __future__ import annotations
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import urljoin, urldefrag, urlparse
import time

 
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
from pdfplumber import page

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────
BASE_URL       = os.getenv("BASE_URL", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
QDRANT_URL     = os.getenv("QDRANT_URL", "http://localhost:6333")

OUTPUT_DIR     = "docs"
LOG_DIR        = os.path.join(OUTPUT_DIR, "logs")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")
TIMESTAMP      = datetime.now().strftime("%Y%m%d_%H%M%S")

os.makedirs(LOG_DIR,        exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR,     exist_ok=True)

# Globals — set by main() after argparse
PROJECT_KEY          = None
UI_MEMORY_COLLECTION = None
DOM_ELEMENTS_FILE    = None
SCREENSHOT_PATH      = None
logger               = None   # deferred until PROJECT_KEY is known


def _init_logger():
    """Set up logging AFTER PROJECT_KEY is known so filenames are correct."""
    global logger
    log_path = os.path.join(LOG_DIR, f"dom_capture_{PROJECT_KEY}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logger = logging.getLogger(__name__)
    return logger


# ══════════════════════════════════════════════════════════════════════════
# Embedding — fully provider-agnostic via llm_gateway
# ══════════════════════════════════════════════════════════════════════════

_gateway      = None
_vector_size  = None   # probed once from a real embedding call


def _get_gateway():
    global _gateway
    if _gateway is None:
        from llm_gateway import get_llm_gateway
        _gateway = get_llm_gateway()
    return _gateway


def generate_embedding(text: str) -> List[float]:
    """
    Generate embedding via llm_gateway.py — works with any LLM_PROVIDER.
    Automatically discovers the real vector dimension on first call.
    """
    gateway = _get_gateway()
    model_override = gateway.resolve_model_for_agent(
        "dom_capture_v1",
        purpose="embedding",
        fallback_model=None,
    )
    return gateway.generate_embedding(text, model_override=model_override)


def get_vector_size() -> int:
    """
    Probe the actual embedding dimension rather than hardcoding 1024.
    Dimension table:
      ollama  mxbai-embed-large     → 1024
      openai  text-embedding-ada-002 → 1536
      gemini  text-embedding-004    → 768
    """
    global _vector_size
    if _vector_size is not None:
        return _vector_size
    probe = generate_embedding("probe")
    _vector_size = len(probe) if probe else 1024
    logger.info(f"[Embedding] Vector size probed: {_vector_size}")
    return _vector_size


# ══════════════════════════════════════════════════════════════════════════
# Authentication helpers
# ══════════════════════════════════════════════════════════════════════════

USERNAME_SELECTORS = [
    'input[name="username"]', 'input#username',
    'input[name="email"]',    'input[type="email"]',
    'input[type="text"]',     'input[name*="user"]',
    'input[placeholder*="user" i]', 'input[placeholder*="email" i]',
    'input[aria-label*="user" i]',  'input[data-testid*="user"]',
    'input[data-cy*="user"]',
]

PASSWORD_SELECTORS = [
    'input[type="password"]',       'input[name="password"]',
    'input#password',               'input[placeholder*="pass" i]',
    'input[aria-label*="pass" i]',  'input[data-testid*="pass"]',
    'input[data-cy*="pass"]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Login")', 'button:has-text("Sign In")',
    'button:has-text("Sign in")', 'button:has-text("Log in")',
    'button:has-text("Continue")', 'button:has-text("Next")',
    '[role="button"]:has-text("Login")',
    '[role="button"]:has-text("Sign in")',
]


def _try_fill(page, selectors: List[str], value: str, field: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.fill(value)
                logger.info(f"  [Auth] Filled {field} via: {sel}")
                return True
        except Exception:
            continue
    logger.warning(f"  [Auth] Could not fill {field}")
    return False


def _try_click(page, selectors: List[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click()
                logger.info(f"  [Auth] Clicked submit via: {sel}")
                return True
        except Exception:
            continue
    logger.warning("  [Auth] Could not find submit button")
    return False


def heuristic_login(page, username: str, password: str) -> bool:
    """
    Multi-step–aware heuristic login:
      1. Fill username → click next/submit
      2. If password field then appeared, fill it → submit
      3. Otherwise fill both on same page and submit
    """
    logger.info(f"[Auth] Attempting login on {page.url}")

    username_filled = _try_fill(page, USERNAME_SELECTORS, username, "username")
    if not username_filled:
        logger.warning("[Auth] Username field not found — continuing anyway")

    # Detect multi-step: if no password field is visible yet, click next first
    pw_visible = False
    for sel in PASSWORD_SELECTORS[:3]:
        try:
            if page.locator(sel).first.is_visible(timeout=1000):
                pw_visible = True
                break
        except Exception:
            pass

    if not pw_visible:
        _try_click(page, SUBMIT_SELECTORS)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    password_filled = _try_fill(page, PASSWORD_SELECTORS, password, "password")
    if not password_filled:
        logger.warning("[Auth] Password field not found — continuing anyway")

    _try_click(page, SUBMIT_SELECTORS)

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    current = page.url
    still_on_login = any(kw in current.lower() for kw in ["login", "auth", "signin", "sign-in"])
    if still_on_login:
        logger.warning("[Auth] Still on login page — credentials may be wrong")
        return False

    logger.info("[Auth] Login successful")
    return True


def safe_navigate(page, url: str) -> bool:
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp and resp.status >= 400:
            logger.warning(f"[Navigate] HTTP {resp.status} for {url}")
            if resp.status in (401, 403, 429, 503):
                return False
        return True
    except Exception as e:
        logger.error(f"[Navigate] Failed: {e}")
        return False


def check_system_status(page) -> Optional[str]:
    """
    Detect blocking via last navigation response status — not text scanning —
    to avoid false positives on pages that discuss error codes.
    """
    try:
        resp = page.request.get(page.url, timeout=8000)
        if resp.status in (401, 403, 429, 503):
            logger.warning(f"[Status] Blocking HTTP status: {resp.status}")
            return "Blocked"
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════
# Page stabilisation
# ══════════════════════════════════════════════════════════════════════════

LOADING_SELECTORS = [
    ".spinner", ".loader", ".loading", ".mat-progress-spinner",
    ".ant-spin", ".el-loading-mask", ".n-spin-mask",
    "[data-loading='true']", ".pulse", ".sk-spinner",
    ".fa-spinner", ".loading-overlay", ".page-loader", ".ajax-loader",
]


def stabilize_page(page, timeout: int = 30000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception as e:
        logger.warning(f"[Stabilize] networkidle timeout: {e}")

    for sel in LOADING_SELECTORS:
        try:
            page.wait_for_selector(sel, state="detached", timeout=3000)
        except Exception:
            pass

    # Scroll to trigger lazy-loaded content, then back to top
    try:
        page.evaluate("""
            () => new Promise(resolve => {
                const step = 400;
                let y = 0;
                const id = setInterval(() => {
                    window.scrollTo(0, y);
                    y += step;
                    if (y >= document.body.scrollHeight) {
                        window.scrollTo(0, 0);
                        clearInterval(id);
                        resolve();
                    }
                }, 80);
            })
        """)
    except Exception:
        pass

    page.wait_for_timeout(800)
    logger.info("[Stabilize] Page stabilized")


# ══════════════════════════════════════════════════════════════════════════
# DOM extraction JavaScript
# ══════════════════════════════════════════════════════════════════════════

DOM_EXTRACTION_JS = r"""
() => {
    const result = {
        url: window.location.href,
        timestamp: new Date().toISOString(),
        pageTitle: document.title,
        input_elements: [],
        button_elements: [],
        dropdown_elements: [],
        textarea_elements: [],
        custom_dropdown_elements: [],
        all_interactive_elements: [],
        form_structure: {},
        label_associations: [],
        accessibility_hints: []
    };

    // ── Helpers ─────────────────────────────────────────────────────────

    function isVisible(el) {
        if (!el) return false;
        const s = window.getComputedStyle(el);
        return s.display !== 'none' &&
               s.visibility !== 'hidden' &&
               parseFloat(s.opacity) > 0 &&
               el.offsetWidth > 0 &&
               el.offsetHeight > 0;
    }

    function getXPath(el) {
        if (el.id) return `//*[@id="${el.id}"]`;
        const parts = [];
        let cur = el;
        while (cur && cur.nodeType === Node.ELEMENT_NODE) {
            const tag = cur.tagName.toLowerCase();
            let idx = 1;
            let sib = cur.parentNode ? cur.parentNode.firstChild : null;
            while (sib) {
                if (sib.nodeType === Node.ELEMENT_NODE &&
                    sib.tagName.toLowerCase() === tag && sib !== cur) idx++;
                if (sib === cur) break;
                sib = sib.nextSibling;
            }
            parts.unshift(idx > 1 ? `${tag}[${idx}]` : tag);
            cur = cur.parentNode;
        }
        return '/' + parts.join('/');
    }

    function findLabel(el) {
        const lbid = el.getAttribute('aria-labelledby');
        if (lbid) {
            const t = lbid.split(' ')
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(n => n.textContent.trim())
                .join(' ');
            if (t) return t;
        }
        const al = el.getAttribute('aria-label');
        if (al) return al.trim();
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return lbl.textContent.trim();
        }
        const pl = el.closest('label');
        if (pl) return pl.textContent.trim().split('\n')[0].trim();
        let sib = el.previousElementSibling;
        while (sib) {
            if (sib.tagName === 'LABEL') return sib.textContent.trim();
            sib = sib.previousElementSibling;
        }
        const pLabel = el.parentElement && el.parentElement.querySelector('label');
        if (pLabel) return pLabel.textContent.trim();
        const title = el.getAttribute('title');
        if (title) return title.trim();
        const dl = el.getAttribute('data-label');
        if (dl) return dl.trim();
        if (el.placeholder) return el.placeholder.trim();
        return null;
    }

    function testIds(el) {
        return {
            testid: el.getAttribute('data-testid') || null,
            cy:     el.getAttribute('data-cy')     || null,
            qa:     el.getAttribute('data-qa')     || null,
            test:   el.getAttribute('data-test')   || null,
        };
    }

    function getRelativeSelectors(el, label) {
        const sels = [];
        if (el.id) sels.push(`#${el.id}`);
        const t = testIds(el);
        if (t.testid) sels.push(`[data-testid="${t.testid}"]`);
        if (t.cy)     sels.push(`[data-cy="${t.cy}"]`);
        if (label)    sels.push(`[aria-label="${label}"]`);
        if (el.placeholder) sels.push(`[placeholder="${el.placeholder}"]`);
        if (el.name)  sels.push(`[name="${el.name}"]`);
        if (el.className) {
            const cls = String(el.className).split(/\s+/)
                .filter(c => c && c.length < 40).slice(0, 2);
            if (cls.length) sels.push(`${el.tagName.toLowerCase()}.${cls.join('.')}`);
        }
        return sels;
    }

    function inferAriaRole(el) {
        const r = el.getAttribute('role');
        if (r) return r;
        const tag  = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        const cls  = (el.className || '').toLowerCase();
        switch (tag) {
            case 'button':   return 'button';
            case 'a':        return 'link';
            case 'select':   return 'combobox';
            case 'textarea': return 'textbox';
            case 'form':     return 'form';
            case 'input':
                if (['submit','button'].includes(type)) return 'button';
                if (type === 'checkbox') return 'checkbox';
                if (type === 'radio')    return 'radio';
                if (type === 'range')    return 'slider';
                return 'textbox';
            default:
                if (cls.includes('dropdown') || cls.includes('select')) return 'combobox';
                if (cls.includes('modal')    || cls.includes('dialog'))  return 'dialog';
                if (cls.includes('tab'))    return 'tab';
                if (cls.includes('menu'))   return 'menu';
                if (cls.includes('switch') || cls.includes('toggle')) return 'switch';
                return 'generic';
        }
    }

    function classifyIntent(el, label, textContent) {
        const tag  = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        const cls  = (el.className || '').toLowerCase();
        const role = inferAriaRole(el);
        const t    = (textContent || '').toLowerCase().trim();
        const lbl  = (label || '').toLowerCase().trim();
        const both = t + ' ' + lbl;

        if (type === 'checkbox' || role === 'switch' ||
            cls.includes('toggle') || cls.includes('switch')) {
            return { intent: 'Toggle', confidence: 0.9 };
        }
        if (type === 'range' || role === 'slider') {
            return { intent: 'Slider', confidence: 0.9 };
        }
        if (type === 'date' || type === 'datetime-local' || type === 'month' ||
            cls.includes('datepicker') || cls.includes('date-picker') ||
            el.getAttribute('data-datepicker') != null) {
            return { intent: 'DatePicker', confidence: 0.9 };
        }
        if (type === 'file' || cls.includes('file-upload') || cls.includes('dropzone')) {
            return { intent: 'FileUpload', confidence: 0.9 };
        }
        if (type === 'search' || cls.includes('search') ||
            ['search','find','lookup'].some(w => both.includes(w))) {
            return { intent: 'Search', confidence: 0.85 };
        }
        if (tag === 'button' || role === 'button' || type === 'submit') {
            const primary   = ['submit','apply','save','create','add','update','confirm','ok','send','post'];
            const secondary = ['cancel','clear','reset','close','back','dismiss'];
            if (primary.some(w   => both.includes(w))) return { intent: 'PrimaryAction',   confidence: 0.9 };
            if (secondary.some(w => both.includes(w))) return { intent: 'SecondaryAction', confidence: 0.8 };
            return { intent: 'PrimaryAction', confidence: 0.6 };
        }
        if (tag === 'a' || role === 'link' ||
            cls.includes('nav') || cls.includes('menu') || cls.includes('breadcrumb')) {
            return { intent: 'Navigation', confidence: 0.8 };
        }
        if (role === 'tab' || cls.includes('tab')) {
            return { intent: 'Tab', confidence: 0.85 };
        }
        if (role === 'dialog' || cls.includes('modal')) {
            return { intent: 'Dialog', confidence: 0.8 };
        }
        if (['input','select','textarea'].includes(tag) ||
            ['textbox','combobox','listbox','spinbutton'].includes(role)) {
            return { intent: 'Input', confidence: 0.9 };
        }
        const infoKw = ['help','hint','description','status','error','info','note','alert','badge'];
        if (infoKw.some(w => both.includes(w))) {
            return { intent: 'Informational', confidence: 0.7 };
        }
        return { intent: 'Unknown', confidence: 0.3 };
    }

    // ── Shadow DOM recursive query ──────────────────────────────────────

    function queryAllDeep(root, selector) {
        const found = [];
        try { found.push(...root.querySelectorAll(selector)); } catch(e) {}
        const shadowHosts = root.querySelectorAll('*');
        shadowHosts.forEach(host => {
            if (host.shadowRoot) {
                found.push(...queryAllDeep(host.shadowRoot, selector));
            }
        });
        return found;
    }

    // ── Inputs ──────────────────────────────────────────────────────────

    queryAllDeep(document, 'input').forEach(inp => {
        if (!isVisible(inp)) return;
        const label = findLabel(inp);
        const sels  = getRelativeSelectors(inp, label);
        const ids   = testIds(inp);
        const item  = {
            type:             inp.type,
            id:               inp.id || null,
            name:             inp.name || null,
            placeholder:      inp.placeholder || null,
            className:        inp.className || null,
            label,
            xpath:            getXPath(inp),
            relativeSelectors: sels,
            isVisible:        true,
            isDisabled:       inp.disabled,
            isRequired:       inp.required,
            ariaRole:         inferAriaRole(inp),
            intent:           classifyIntent(inp, label, inp.placeholder || inp.value || '').intent,
            intentConfidence: classifyIntent(inp, label, inp.placeholder || inp.value || '').confidence,
            ...ids,
        };
        result.input_elements.push(item);
        if (label) result.label_associations.push({
            fieldType: 'input', label,
            placeholder: inp.placeholder, xpath: item.xpath,
            relativeSelector: sels[0] || null, elementType: inp.type
        });
    });

    // ── Buttons ─────────────────────────────────────────────────────────

    queryAllDeep(document,
        'button, input[type="submit"], input[type="button"], [role="button"]'
    ).forEach(btn => {
        if (!isVisible(btn)) return;
        const text = (btn.textContent || btn.value || '').trim();
        const ids  = testIds(btn);
        const ic   = classifyIntent(btn, null, text);
        result.button_elements.push({
            text: text || null,
            id:   btn.id || null,
            name: btn.name || null,
            className: btn.className || null,
            xpath: getXPath(btn),
            ariaRole: inferAriaRole(btn),
            intent: ic.intent, intentConfidence: ic.confidence,
            isDisabled: btn.disabled,
            ...ids,
        });
    });

    // ── Textareas ────────────────────────────────────────────────────────

    queryAllDeep(document, 'textarea').forEach(ta => {
        if (!isVisible(ta)) return;
        const label = findLabel(ta);
        const sels  = getRelativeSelectors(ta, label);
        const ids   = testIds(ta);
        const ic    = classifyIntent(ta, label, ta.placeholder || '');
        const item  = {
            id: ta.id || null, name: ta.name || null,
            placeholder: ta.placeholder || null,
            className: ta.className || null,
            label, xpath: getXPath(ta),
            relativeSelectors: sels,
            isVisible: true, isDisabled: ta.disabled,
            ariaRole: inferAriaRole(ta),
            intent: ic.intent, intentConfidence: ic.confidence,
            ...ids,
        };
        result.textarea_elements.push(item);
        if (label) result.label_associations.push({
            fieldType: 'textarea', label,
            placeholder: ta.placeholder, xpath: item.xpath,
            relativeSelector: sels[0] || null
        });
    });

    // ── Native selects ───────────────────────────────────────────────────

    queryAllDeep(document, 'select').forEach(sel => {
        if (!isVisible(sel)) return;
        const label   = findLabel(sel);
        const sels    = getRelativeSelectors(sel, label);
        const ids     = testIds(sel);
        const options = Array.from(sel.options).map(o => ({
            value: o.value, text: o.textContent.trim()
        }));
        const ic = classifyIntent(sel, label, label || '');
        const item = {
            id: sel.id || null, name: sel.name || null,
            className: sel.className || null,
            label, xpath: getXPath(sel),
            relativeSelectors: sels,
            isVisible: true, isDisabled: sel.disabled,
            options, ariaRole: inferAriaRole(sel),
            intent: ic.intent, intentConfidence: ic.confidence,
            ...ids,
        };
        result.dropdown_elements.push(item);
        if (label) result.label_associations.push({
            fieldType: 'select', label, xpath: item.xpath,
            relativeSelector: sels[0] || null,
            options: options.map(o => o.text).join(', ')
        });
    });

    // ── Custom dropdowns (div/ul-based) ──────────────────────────────────

    const customDDSelectors = [
        '[role="listbox"]', '[role="combobox"]',
        '[aria-haspopup="listbox"]', '[aria-haspopup="true"]',
    ];
    queryAllDeep(document, customDDSelectors.join(',')).forEach(dd => {
        if (!isVisible(dd)) return;
        const label = findLabel(dd);
        const ids   = testIds(dd);
        const options = Array.from(dd.querySelectorAll('[role="option"]'))
            .map(o => o.textContent.trim()).filter(Boolean);
        result.custom_dropdown_elements.push({
            tagName: dd.tagName.toLowerCase(),
            id: dd.id || null, className: dd.className || null,
            label, text: dd.textContent.trim().substring(0, 80),
            xpath: getXPath(dd), ariaRole: dd.getAttribute('role') || 'combobox',
            options, intent: 'Input', intentConfidence: 0.85,
            ...ids,
        });
    });

    // ── All interactive elements ─────────────────────────────────────────

    const interactiveQ = [
        'input', 'button', 'select', 'textarea',
        'a[href]', '[role="button"]', '[role="link"]',
        '[role="tab"]', '[role="menuitem"]', '[role="option"]',
        '[role="switch"]', '[role="slider"]', '[role="checkbox"]',
        '[role="radio"]', '[tabindex]:not([tabindex="-1"])',
    ].join(',');

    queryAllDeep(document, interactiveQ).forEach(el => {
        if (!isVisible(el)) return;
        const ic  = classifyIntent(el, null, el.textContent?.trim() || '');
        const ids = testIds(el);
        result.all_interactive_elements.push({
            tagName:          el.tagName.toLowerCase(),
            id:               el.id || null,
            className:        el.className || null,
            text:             (el.textContent || '').trim().substring(0, 100) || null,
            xpath:            getXPath(el),
            role:             el.getAttribute('role') || null,
            type:             el.type || null,
            name:             el.name || null,
            placeholder:      el.placeholder || null,
            ariaRole:         inferAriaRole(el),
            intent:           ic.intent,
            intentConfidence: ic.confidence,
            href:             el.href  || null,
            ariaLabel:        el.getAttribute('aria-label') || null,
            ...ids,
        });
    });

    // ── Form structure ───────────────────────────────────────────────────

    document.querySelectorAll('form').forEach((form, i) => {
        const formId = form.id || form.name || `form_${i}`;
        const fields = [];
        form.querySelectorAll('input, select, textarea, button').forEach(el => {
            if (!isVisible(el)) return;
            fields.push({
                tag:   el.tagName.toLowerCase(),
                type:  el.type  || null,
                id:    el.id    || null,
                name:  el.name  || null,
                label: findLabel(el),
                xpath: getXPath(el),
            });
        });
        result.form_structure[formId] = {
            id:     form.id    || null,
            name:   form.name  || null,
            action: form.action || null,
            method: form.method || 'GET',
            fields,
        };
    });

    return result;
}
"""


def extract_dom_elements(page) -> Dict:
    """Run the JS extraction, then enrich with the accessibility tree."""
    logger.info("[DOM] Running JS extraction (shadow DOM, custom dropdowns, forms)…")
    dom_data = page.evaluate(DOM_EXTRACTION_JS)

    try:
        ax_snapshot = page.accessibility.snapshot(interesting_only=True)
        dom_data["accessibility_tree"] = ax_snapshot
        logger.info("[DOM] Accessibility tree captured")
    except Exception as e:
        logger.warning(f"[DOM] Accessibility tree unavailable: {e}")
        dom_data["accessibility_tree"] = None

    return dom_data


def safe_extract_dom(page) -> Dict:
    try:
        return extract_dom_elements(page)
    except Exception as e:
        logger.error(f"[DOM] Extraction failed: {e}")
        return {
            "url": page.url, "timestamp": datetime.now().isoformat(),
            "pageTitle": page.title(), "error": str(e),
            "input_elements": [], "button_elements": [],
            "dropdown_elements": [], "textarea_elements": [],
            "custom_dropdown_elements": [],
            "all_interactive_elements": [],
            "form_structure": {}, "label_associations": [],
            "accessibility_tree": None,
        }


# ══════════════════════════════════════════════════════════════════════════
# Iframe capture (same-origin only)
# ══════════════════════════════════════════════════════════════════════════

def extract_iframe_elements(page) -> List[Dict]:
    """
    Try to extract DOM from same-origin iframes.
    Cross-origin iframes are silently skipped (browser security).
    """
    iframe_data = []
    frames = page.frames[1:]  # skip main frame
    for frame in frames:
        try:
            url = frame.url
            if not url or url in ("about:blank", ""):
                continue
            data = frame.evaluate(DOM_EXTRACTION_JS)
            data["iframe_url"] = url
            iframe_data.append(data)
            logger.info(f"[iframe] Captured: {url}")
        except Exception as e:
            logger.debug(f"[iframe] Skipped (likely cross-origin): {e}")
    return iframe_data


# ══════════════════════════════════════════════════════════════════════════
# QA Signal Enrichment  (v3 — new)
# ══════════════════════════════════════════════════════════════════════════

# JavaScript run once per element to compute bounding box + obstruction.
_QA_SIGNAL_JS = """
(xpath) => {
    // Resolve element from XPath
    const el = document.evaluate(
        xpath, document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null
    ).singleNodeValue;

    if (!el) return null;

    const rect = el.getBoundingClientRect();
    if (!rect || rect.width === 0 || rect.height === 0) {
        return { visible: false, bounding_box: null, obstructed: false };
    }

    // Sample the centre point
    const cx = Math.round(rect.left + rect.width  / 2);
    const cy = Math.round(rect.top  + rect.height / 2);
    const topEl = document.elementFromPoint(cx, cy);

    // An element is obstructed when the topmost element at its centre
    // is neither itself nor a descendant/ancestor of itself.
    const obstructed = topEl !== null &&
                       !el.contains(topEl) &&
                       !topEl.contains(el);

    return {
        visible: true,
        bounding_box: {
            x:      Math.round(rect.left),
            y:      Math.round(rect.top),
            width:  Math.round(rect.width),
            height: Math.round(rect.height),
        },
        obstructed,
    };
}
"""

# ══════════════════════════════════════════════════════════════════════════
# §1  SELECTOR HELPER  (replaces get_canonical_selector + _build_selector_for_element)
# ══════════════════════════════════════════════════════════════════════════
 
# Brittle-selector guard: reject selectors that contain 5+ consecutive digits
# (auto-generated IDs like "comp-1234567") or are trivially short.
import re as _re
_DIGIT_RUN = _re.compile(r"\d{5,}")
 
 
def _is_fragile(selector: str) -> bool:
    """Return True if the selector looks auto-generated or too vague to trust."""
    if not selector:
        return True
    if _DIGIT_RUN.search(selector):
        return True
    # Bare single-char class or id: ".a", "#b"
    if _re.fullmatch(r"[.#][a-z0-9]{1,2}", selector, _re.I):
        return True
    return False
 
 
def _derive_selector_with_fallback(el: Dict, page_url: str = "") -> str:
    """
    Return the most stable CSS/attribute selector for *el*, with a guaranteed
    non-empty fallback.
 
    Priority (highest → lowest stability)
    ──────────────────────────────────────
    1. [data-testid="…"]   — dedicated QA attribute
    2. [data-cy="…"]       — Cypress QA attribute
    3. [data-qa="…"] / [data-test="…"]
    4. #id                 — stable unless auto-generated
    5. [aria-label="…"]    — accessible and usually stable
    6. [name="…"]          — good for form fields
    7. [placeholder="…"]   — weaker but better than xpath
    8. relativeSelectors[0] from the JS crawler (already priority-sorted)
    9. xpath               — fragile last resort (penalised in confidence)
   10. text-based fallback  — absolute last resort; logs a warning
 
    Brittle-selector guard: if a candidate contains 5+ consecutive digits
    (typical of auto-generated IDs) it is skipped and the next candidate
    is tried.
    """
    # ── Priority attributes ────────────────────────────────────────────────────
    for attr_key, css_prefix in [
        ("testid",   '[data-testid="{}"]'),
        ("cy",       '[data-cy="{}"]'),
        ("qa",       '[data-qa="{}"]'),
        ("test",     '[data-test="{}"]'),
    ]:
        val = (el.get(attr_key) or "").strip()
        if val and not _is_fragile(val):
            return css_prefix.format(val)
 
    # ── Element id ────────────────────────────────────────────────────────────
    eid = (el.get("id") or "").strip()
    if eid and not _is_fragile(eid):
        return f'#{eid}'
 
    # ── aria-label ────────────────────────────────────────────────────────────
    aria = (
        el.get("ariaLabel")
        or el.get("aria-label")
        or el.get("label")
        or ""
    ).strip()
    if aria and len(aria) < 80:   # guard against giant label strings
        return f'[aria-label="{aria}"]'
 
    # ── name attribute ────────────────────────────────────────────────────────
    name = (el.get("name") or "").strip()
    if name and not _is_fragile(name):
        return f'[name="{name}"]'
 
    # ── placeholder ────────────────────────────────────────────────────────────
    ph = (el.get("placeholder") or "").strip()
    if ph and len(ph) < 60:
        return f'[placeholder="{ph}"]'
 
    # ── relativeSelectors (from JS crawler — already priority-sorted) ──────────
    for rs in (el.get("relativeSelectors") or []):
        rs = rs.strip()
        if rs and not _is_fragile(rs):
            return rs
 
    # ── xpath (fragile but always present for DOM elements) ───────────────────
    xpath = (el.get("xpath") or "").strip()
    if xpath:
        # Log as warning so we know this element has no stable selector
        if logger:
            logger.warning(
                f"[Selector] Falling back to xpath for element "
                f"tag={el.get('tagName','?')} id={el.get('id','–')} "
                f"page={page_url or '?'} | xpath={xpath[:80]}"
            )
        return xpath
 
    # ── absolute last resort: text-based selector ─────────────────────────────
    text = (el.get("text") or el.get("label") or "").strip()[:60]
    tag  = (el.get("tagName") or "button").lower()
    if text:
        if logger:
            logger.warning(
                f"[Selector] No stable selector found — using text locator "
                f"tag={tag} text={text!r:.40} page={page_url or '?'}"
            )
        return f'{tag}:has-text("{text}")'
 
    # ── truly unfindable ──────────────────────────────────────────────────────
    if logger:
        logger.warning(
            f"[Selector] ⚠ Could not derive ANY selector for element "
            f"tag={el.get('tagName','?')} page={page_url or '?'} — "
            f"element will be stored with selector=''"
        )
    return ""


def _compute_clickable_score(visible: bool, obstructed: bool,
                              is_disabled: bool) -> float:
    """
    Heuristic clickability score in [0, 1].
      • Not visible           → 0.0
      • Disabled              → 0.1  (exists but not operable)
      • Obstructed            → 0.4  (may be reachable with scroll/hover)
      • Visible, clear, enabled → 1.0
    """
    if not visible:
        return 0.0
    if is_disabled:
        return 0.1
    if obstructed:
        return 0.4
    return 1.0


def enrich_with_qa_signals(page, dom_data: Dict) -> None:
    """
    Iterate over all_interactive_elements, compute QA signals for each, and
    append the results under dom_data["qa_analysis"].

    Signals per element:
      visible        — bool
      bounding_box   — {x, y, width, height} | null
      obstructed     — bool (elementFromPoint check at centre of bounding box)
      clickable_score — float 0→1
      qa_status      — "GOOD" | "RISKY"

    Non-breaking: any per-element failure is caught and logged; the element
    is still included in qa_analysis with qa_status="RISKY".
    """
    elements = dom_data.get("all_interactive_elements", [])
    qa_results: List[Dict] = []

    logger.info(f"[QA] Enriching {len(elements)} interactive elements with QA signals…")

    for el in elements:
        entry: Dict[str, Any] = {
            "xpath":          el.get("xpath"),
            "id":             el.get("id"),
            "name":           el.get("name"),
            "intent":         el.get("intent"),
            "tagName":        el.get("tagName"),
            "visible":        False,
            "bounding_box":   None,
            "obstructed":     False,
            "clickable_score": 0.0,
            "qa_status":      "RISKY",
        }

        xpath = el.get("xpath")
        if not xpath:
            qa_results.append(entry)
            continue

        try:
            signals = page.evaluate(_QA_SIGNAL_JS, xpath)

            if signals is None:
                # Element no longer in DOM (dynamic removal after capture)
                qa_results.append(entry)
                continue

            visible    = signals.get("visible", False)
            obstructed = signals.get("obstructed", False)
            bbox       = signals.get("bounding_box")
            is_disabled = bool(el.get("isDisabled", False))

            score = _compute_clickable_score(visible, obstructed, is_disabled)

            entry.update({
                "visible":         visible,
                "bounding_box":    bbox,
                "obstructed":      obstructed,
                "clickable_score": score,
                "qa_status":       "GOOD" if score >= 0.8 else "RISKY",
            })

        except Exception as exc:
            logger.warning(f"[QA] Signal error for xpath={xpath!r}: {exc}")
            entry["qa_status"] = "RISKY"

        qa_results.append(entry)

    dom_data["qa_analysis"] = qa_results
    good  = sum(1 for r in qa_results if r["qa_status"] == "GOOD")
    risky = len(qa_results) - good
    logger.info(f"[QA] Enrichment complete — GOOD: {good}, RISKY: {risky}")

# ══════════════════════════════════════════════════════════════════════════
# Multi Page extraction
# ══════════════════════════════════════════════════════════════════════════

def _normalize_page_url(url: str, base_url: str = "") -> str:
    """
    Return a stable absolute page URL for crawl storage and Qdrant payloads.
    Removes fragments, ignores non-page schemes, and trims a trailing slash
    except for the site root.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    absolute = urljoin(base_url or raw, raw)
    absolute, _fragment = urldefrag(absolute)
    parsed = urlparse(absolute)

    if parsed.scheme not in ("http", "https"):
        return ""

    if parsed.path and parsed.path != "/":
        normalized_path = parsed.path.rstrip("/")
        absolute = parsed._replace(path=normalized_path).geturl()

    return absolute


def extract_navigation_links(dom_data: Dict, base_url: str) -> List[str]:
    links = []

    for el in dom_data.get("all_interactive_elements", []):
        tag = (el.get("tagName") or "").lower()
        intent = (el.get("intent") or "").lower()

        # 🔥 Try multiple sources for URL
        href = (
            el.get("href")
            or el.get("url")
            or el.get("link")
            or (el.get("attributes", {}) or {}).get("href")
        )

        # 🔥 fallback: build URL from text (demoqa specific but safe)
        if not href:
            continue

        href = _normalize_page_url(str(href), base_url)
        if not href:
            continue

        # Only navigation-like
        if tag == "a" or intent in ["navigation", "click", "action"]:
            links.append(href)

    return list(dict.fromkeys(links))


def crawl_pages(page, base_url: str, max_pages: int = 5) -> List[Dict]:
    start_url = _normalize_page_url(base_url, base_url) or base_url
    visited = set()
    to_visit = [start_url]
    results = []

    base_domain = urlparse(start_url).netloc

    while to_visit and len(visited) < max_pages:
        url = _normalize_page_url(to_visit.pop(0), start_url)

        if not url or url in visited:
            continue

        print(f"[Crawl] Visiting: {url}")

        try:
            page.goto(url, timeout=15000)
            time.sleep(1)
        except Exception as e:
            print(f"  ⚠ Failed to load {url}: {e}")
            continue

        # ── Extract DOM ─────────────────────────
        dom_data = safe_extract_dom(page)

        if not dom_data:
            print(f"  ⚠ Empty DOM at {url}")
            continue

        # 🔥 Page-aware context (CRITICAL)
        current_url = _normalize_page_url(page.url, start_url) or url
        dom_data["page_url"] = current_url

        # 🔥 Attach page_url to every element
        for key in [
            "all_interactive_elements",
            "input_elements",
            "button_elements",
            "dropdown_elements",
            "textarea_elements",
        ]:
            for el in dom_data.get(key, []):
                el["page_url"] = current_url

        # ── QA enrichment ───────────────────────
        try:
            enrich_with_qa_signals(page, dom_data)
        except Exception as e:
            print(f"  ⚠ QA enrichment failed: {e}")

        results.append(dom_data)
        visited.add(current_url)

        # ── Discover links ──────────────────────
        new_links = extract_navigation_links(dom_data, start_url)

        for link in new_links:
            normalized_link = _normalize_page_url(link, start_url)
            if not normalized_link:
                continue

            # 1️⃣ Stay in same domain
            if urlparse(normalized_link).netloc != base_domain:
                continue

            # # 2️⃣ Optional smart filtering (safe fallback if empty)
            # if not any(x in link.lower() for x in ["text", "form", "input", "login", "register"]):
            #     # comment this line if crawling feels too restrictive
            #     continue

            # 3️⃣ Avoid loops
            if normalized_link not in visited and normalized_link not in to_visit:
                to_visit.append(normalized_link)

    return results


def _should_skip_for_vectorization(el: Dict, page_url: str) -> bool:
    """
    Keep real page-level navigation so later generators can resolve URLs, but
    drop obvious crawl noise like fragments and javascript pseudo-links.
    """
    tag = (el.get("tagName") or "").lower()
    href = (
        el.get("href")
        or el.get("url")
        or el.get("link")
        or (el.get("attributes", {}) or {}).get("href")
        or ""
    )
    href = str(href).strip()
    text = str(el.get("text") or "").strip()
    intent = str(el.get("intent") or "").strip().lower()

    if tag == "a":
        if not href and not text:
            return True
        lowered = href.lower()
        if lowered.startswith(("javascript:", "mailto:", "tel:")):
            return True
        if href in ("#", "/#") or lowered.endswith("#"):
            return True

        normalized_href = _normalize_page_url(href, page_url)
        normalized_page = _normalize_page_url(page_url, page_url)
        if normalized_href and normalized_page and normalized_href == normalized_page and not text:
            return True

    if intent == "unknown" and not text and not href:
        return True

    return False

def merge_dom_results(dom_list: List[Dict]) -> Dict:
    merged = {
        "all_interactive_elements": [],
        "input_elements": [],
        "button_elements": [],
        "dropdown_elements": [],
        "textarea_elements": [],
    }

    for dom in dom_list:
        for key in merged.keys():
            merged[key].extend(dom.get(key, []))

    return merged
# ══════════════════════════════════════════════════════════════════════════
# Overlay Detection  (v3 — new)
# ══════════════════════════════════════════════════════════════════════════

_OVERLAY_JS = """
() => {
    const overlays = [];
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    // Consider an element a potential overlay when it covers > 20% of the
    // viewport in each axis — adjust threshold as needed for your app.
    const MIN_COVERAGE = 0.20;

    document.querySelectorAll('*').forEach(el => {
        const s = window.getComputedStyle(el);
        const pos = s.position;
        const zi  = parseInt(s.zIndex, 10);

        if ((pos === 'fixed' || pos === 'sticky') && zi > 10) {
            const rect = el.getBoundingClientRect();
            const wCov = rect.width  / vw;
            const hCov = rect.height / vh;

            if (wCov >= MIN_COVERAGE && hCov >= MIN_COVERAGE) {
                overlays.push({
                    tagName:   el.tagName.toLowerCase(),
                    id:        el.id        || null,
                    className: el.className || null,
                    position:  pos,
                    zIndex:    zi,
                    bounding_box: {
                        x:      Math.round(rect.left),
                        y:      Math.round(rect.top),
                        width:  Math.round(rect.width),
                        height: Math.round(rect.height),
                    },
                    text:  (el.textContent || '').trim().substring(0, 120) || null,
                    role:  el.getAttribute('role') || null,
                });
            }
        }
    });
    return overlays;
}
"""


def detect_overlays(page, dom_data: Dict) -> None:
    """
    Identify fixed/sticky high-z-index elements that cover a significant
    portion of the viewport (modal backdrops, cookie banners, chat widgets,
    notification toasts, sticky headers/footers).

    Results are stored in dom_data["overlays_detected"] as a list of dicts.
    Non-breaking: any JS error yields an empty list.
    """
    try:
        overlays = page.evaluate(_OVERLAY_JS)
        dom_data["overlays_detected"] = overlays or []
        logger.info(f"[QA] Overlays detected: {len(dom_data['overlays_detected'])}")
    except Exception as exc:
        logger.warning(f"[QA] Overlay detection failed: {exc}")
        dom_data["overlays_detected"] = []


# ══════════════════════════════════════════════════════════════════════════
# QA Summary  (v3 — new)
# ══════════════════════════════════════════════════════════════════════════

def generate_qa_summary(dom_data: Dict) -> Dict:
    """
    Produce a concise QA health summary from the enriched dom_data.

    Returns (and stores under dom_data["qa_summary"]):
      {
        total_elements:   int   — all interactive elements analysed
        risky_elements:   int   — elements with qa_status == "RISKY"
        good_elements:    int   — elements with qa_status == "GOOD"
        overlay_present:  bool  — True if any overlay was detected
        health_score:     float — good / total (0.0 if no elements)
      }
    """
    qa_analysis = dom_data.get("qa_analysis", [])
    total  = len(qa_analysis)
    risky  = sum(1 for e in qa_analysis if e.get("qa_status") == "RISKY")
    good   = total - risky
    overlays = dom_data.get("overlays_detected", [])

    summary = {
        "total_elements":  total,
        "good_elements":   good,
        "risky_elements":  risky,
        "overlay_present": len(overlays) > 0,
        "health_score":    round(good / total, 3) if total else 0.0,
    }
    dom_data["qa_summary"] = summary
    logger.info(
        f"[QA] Summary — total={total}, good={good}, risky={risky}, "
        f"overlays={len(overlays)}, health={summary['health_score']:.1%}"
    )
    return summary


# ══════════════════════════════════════════════════════════════════════════
# Screenshot capture  (v3 — extended)
# ══════════════════════════════════════════════════════════════════════════

def capture_screenshot(page, path: str, dom_data: Dict) -> None:
    """
    Capture a full-page screenshot and store the path in dom_data.
    Non-breaking: logs a warning on failure, does not raise.
    """
    try:
        page.screenshot(path=path, full_page=True)
        dom_data["screenshot_path"] = path
        logger.info(f"[Screenshot] → {path}")
    except Exception as exc:
        logger.warning(f"[Screenshot] Capture failed: {exc}")
        dom_data["screenshot_path"] = None


# ══════════════════════════════════════════════════════════════════════════
# Qdrant vectorisation — provider-agnostic
# ══════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════
# §2  EMBEDDING TEXT BUILDER  (replaces _build_embedding_text)
# ══════════════════════════════════════════════════════════════════════════
 

def _build_embedding_text(kind: str, el: Dict, page_url: str = "") -> str:
    """
    Build a structured, semantically rich natural-language string for embedding.
 
    Design goals
    ─────────────
    • Human-readable sentences → better cosine similarity with Gherkin steps
      like "click the Login button" or "fill the email input field".
    • All high-signal fields are included: type, role, label, placeholder,
      text, selector, and page context.
    • Fixed field order so similar elements produce similar vector neighbourhoods.
    • page_url appended as context so cross-page disambiguation improves.
 
    Example output (button)
    ───────────────────────
      "button element. role: button. text: Login. label: Login.
       selector: [data-testid=\"login-btn\"]. intent: PrimaryAction.
       page: https://app.example.com/login"
 
    Example output (input)
    ──────────────────────
      "input element. type: email. role: textbox. label: Email address.
       placeholder: Enter your email. name: email.
       selector: [name=\"email\"]. intent: Input.
       page: https://app.example.com/login"
    """
    parts: List[str] = []
 
    # ── 1. Element kind / type ────────────────────────────────────────────
    el_type = (el.get("type") or "").strip()
    if el_type and el_type not in ("button", "submit"):
        parts.append(f"{kind} element. type: {el_type}.")
    else:
        parts.append(f"{kind} element.")
 
    # ── 2. Role (aria / inferred) ─────────────────────────────────────────
    role = (el.get("ariaRole") or el.get("role") or "").strip()
    if role and role not in ("generic",):
        parts.append(f"role: {role}.")
 
    tag = (el.get("tagName") or "").strip().lower()
    if tag and tag not in (kind, ""):
        parts.append(f"tag: {tag}.")
 
    # ── 3. Visible text / label (highest semantic signal) ─────────────────
    text = (el.get("text") or "").strip()[:120]
    if text:
        parts.append(f"text: {text}.")
 
    label = (el.get("label") or "").strip()[:80]
    if label and label.lower() != text.lower():
        parts.append(f"label: {label}.")
 
    # ── 4. Placeholder / hint ─────────────────────────────────────────────
    ph = (el.get("placeholder") or "").strip()[:80]
    if ph:
        parts.append(f"placeholder: {ph}.")
 
    # ── 5. Name attribute ─────────────────────────────────────────────────
    name = (el.get("name") or "").strip()
    if name:
        parts.append(f"name: {name}.")
 
    # ── 6. aria-label (when different from label) ─────────────────────────
    aria = (el.get("ariaLabel") or el.get("aria-label") or "").strip()[:80]
    if aria and aria.lower() != label.lower():
        parts.append(f"aria-label: {aria}.")
 
    # ── 7. Selector (key for retrieval confidence) ────────────────────────
    sel = _derive_selector_with_fallback(el, page_url).strip()
    if sel:
        parts.append(f"selector: {sel}.")
 
    # ── 8. Intent (semantic classification from JS crawler) ───────────────
    intent = (el.get("intent") or "").strip()
    if intent and intent not in ("Unknown",):
        parts.append(f"intent: {intent}.")
 
    # ── 9. Dropdown options (for select / custom dropdowns) ───────────────
    options = el.get("options", [])
    if options:
        opt_texts = [
            (o.get("text", o) if isinstance(o, dict) else str(o))
            for o in options[:6]
        ]
        parts.append(f"options: {', '.join(opt_texts)}.")
 
    # ── 10. Page context (critical for cross-page disambiguation) ─────────
    url = (page_url or el.get("page_url") or "").strip()
    if url:
        parts.append(f"page: {url}.")
 
    result = " ".join(parts)
 
    # Guard: if nothing was captured, fallback to element kind at minimum
    if not result.strip():
        result = f"{kind} element"
 
    return result
 


def classify_element(el: Dict) -> str:
    # 🔥 Normalize signals (robust to different DOM formats)
    tag = (el.get("tagName") or el.get("tag") or "").lower()
    role = (el.get("ariaRole") or el.get("role") or "").lower()
    typ = (el.get("type") or "").lower()
    intent = (el.get("intent") or "").lower()
    href = el.get("href")
    text = (el.get("text") or "").lower()

    attrs = el.get("attributes", {}) or {}

    # 🔥 scoring
    scores = {
        "input": 0,
        "button": 0,
        "dropdown": 0,
        "interactive": 0,
    }

    # ── INPUT signals ─────────────────────────
    if tag in ["input", "textarea"]:
        scores["input"] += 3
    if typ in ["text", "email", "password", "number"]:
        scores["input"] += 2
    if any(k in attrs for k in ["placeholder", "name", "value"]):
        scores["input"] += 1

    # ── DROPDOWN signals ─────────────────────
    if tag == "select":
        scores["dropdown"] += 3
    if role in ["listbox", "combobox"]:
        scores["dropdown"] += 2

    # ── BUTTON signals ───────────────────────
    if tag == "button":
        scores["button"] += 3
    if role == "button":
        scores["button"] += 2
    if typ in ["submit", "button"]:
        scores["button"] += 2

    # ── INTERACTIVE signals (critical for your case) ──
    if tag == "a" and href:
        scores["interactive"] += 3
    if intent in ["navigation", "click", "action"]:
        scores["interactive"] += 2
    if href:
        scores["interactive"] += 1
    if "onclick" in attrs:
        scores["interactive"] += 2

    # ── fallback clickable heuristics ────────
    if tag in ["a", "button"]:
        scores["interactive"] += 1

    # ── decision ─────────────────────────────
    best = max(scores, key=scores.get)
    best_score = scores[best]

    # 🔥 fallback safety
    if best_score == 0:
        return "static"

    return best

# ══════════════════════════════════════════════════════════════════════════
# §3  VECTORIZE AND UPLOAD  (replaces vectorize_and_upload_dom_elements)
# ══════════════════════════════════════════════════════════════════════════
 
def vectorize_and_upload_dom_elements(dom_data: Dict,
                                      collection_name: str) -> int:
    """
    Vectorize DOM elements and upload to the project-specific Qdrant collection.
 
    Changes vs v1
    ──────────────
    ✅ selector   — derived via _derive_selector_with_fallback(); always
                    present (or logged as warning if truly unfindable).
    ✅ page_url   — extracted from element payload (set by crawl_pages())
                    and stored at the TOP LEVEL of the Qdrant payload so
                    step_generator can access it directly with:
                      payload.get("page_url")
    ✅ is_visible, is_obstructed, qa_status
                  — lifted from element and QA analysis; stored flat in
                    the payload so step_generator reads them without
                    digging into nested "details".
    ✅ embedding text — uses the new _build_embedding_text() with page_url.
    ✅ debug logging  — warnings for: missing selector, missing page_url,
                        bad embeddings, upload errors.
    ✅ uid collision guard — uid now includes page_url fragment to prevent
                    hash collisions for identical elements on different pages.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct
 
    client   = QdrantClient(url=QDRANT_URL)
    vec_size = get_vector_size()
 
    # ── Ensure collection exists ───────────────────────────────────────────────
    collections = {c.name for c in client.get_collections().collections}
    if collection_name not in collections:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vec_size, distance=Distance.COSINE),
        )
        logger.info(f"[Qdrant] Created '{collection_name}' (dim={vec_size})")
    else:
        logger.info(f"[Qdrant] Using existing '{collection_name}'")
 
    # ── Pre-build the QA signal lookup keyed by xpath ──────────────────────────
    # qa_analysis entries are keyed by xpath (set in enrich_with_qa_signals).
    # We build a dict once rather than scanning the list for every element.
    qa_by_xpath: Dict[str, Dict] = {}
    for qa_entry in dom_data.get("qa_analysis", []):
        xp = qa_entry.get("xpath") or ""
        if xp:
            qa_by_xpath[xp] = qa_entry
 
    # ── Collect all element groups ─────────────────────────────────────────────
    element_groups = [
        ("input",          dom_data.get("input_elements", [])),
        ("button",         dom_data.get("button_elements", [])),
        ("textarea",       dom_data.get("textarea_elements", [])),
        ("dropdown",       dom_data.get("dropdown_elements", [])),
        ("custom_dropdown",dom_data.get("custom_dropdown_elements", [])),
        ("interactive",    dom_data.get("all_interactive_elements", [])),
    ]
 
    # ── Build point descriptors ────────────────────────────────────────────────
    point_descriptors = []
    type_counts: Dict[str, int] = {
        "input": 0, "button": 0, "dropdown": 0, "interactive": 0, "static": 0,
    }
 
    # Counters for summary
    missing_selector_count = 0
    missing_page_url_count = 0

    for declared_kind, items in element_groups:
        for i, el in enumerate(items):
            page_url = (
                el.get("page_url")
                or dom_data.get("page_url")     # top-level fallback
                or dom_data.get("url")           # older field name
                or ""
            ).strip()

            # Keep real page navigation for downstream step resolution, but
            # drop obvious non-page anchor noise.
            if _should_skip_for_vectorization(el, page_url):
                continue
            
            actual_kind = classify_element(el)        
            type_counts[actual_kind] = type_counts.get(actual_kind, 0) + 1
 
            if not page_url:
                missing_page_url_count += 1
                logger.warning(
                    f"[PageURL] Missing page_url for {actual_kind}[{i}] "
                    f"tag={el.get('tagName','?')} id={el.get('id','–')} "
                    f"— element will be stored without page context"
                )
 
            # ── selector ──────────────────────────────────────────────────
            selector = _derive_selector_with_fallback(el, page_url)
            if not selector:
                missing_selector_count += 1
                # warning already emitted inside _derive_selector_with_fallback
 
            # ── QA signals: lift from qa_analysis (flat, not nested) ───────
            xpath   = (el.get("xpath") or "").strip()
            qa_info = qa_by_xpath.get(xpath, {})
 
            is_visible    = qa_info.get("visible",         el.get("isVisible"))
            is_obstructed = qa_info.get("obstructed",      False)
            qa_status     = (qa_info.get("qa_status") or "ok").lower()
            clickable     = qa_info.get("clickable_score", None)
 
            # ── Unique ID: kind + index + page fragment ────────────────────
            # Use last path segment of page_url to avoid hash collisions
            # when the same element exists on multiple pages.
            from urllib.parse import urlparse as _urlparse
            page_slug = _urlparse(page_url).path.strip("/").replace("/", "_") if page_url else ""
            uid = f"{actual_kind}_{i}_{page_slug}"
 
            # ── Embedding text ─────────────────────────────────────────────
            emb_text = _build_embedding_text(actual_kind, el, page_url)
 
            point_descriptors.append({
                "uid":      uid,
                "text":     emb_text,
                "kind":     actual_kind,
                "el":       el,
                "selector": selector,
                "page_url": page_url,
                # QA signals lifted flat
                "is_visible":    is_visible,
                "is_obstructed": is_obstructed,
                "qa_status":     qa_status,
                "clickable_score": clickable,
            })
    

    logger.info(
        f"[Vectorize] {len(point_descriptors)} elements to upload "
        f"| missing_selector={missing_selector_count} "
        f"| missing_page_url={missing_page_url_count}"
    )

    # ── Deduplicate AFTER the loop ─────────────────────────────────────────────
    original_count = len(point_descriptors)
    seen_uids = set()
    deduped = []
    for p in point_descriptors:
        dedup_key = f"{p.get('selector', '')}|{p.get('page_url', '')}"
        if dedup_key not in seen_uids:
            seen_uids.add(dedup_key)
            deduped.append(p)
    point_descriptors = deduped
    print(f"  ✓ Deduplicated: {len(deduped)} unique points (was {original_count} before)")
    # ── Upload loop ────────────────────────────────────────────────────────────
    uploaded = 0
    skipped  = 0
    print(f"  Vectorizing {len(point_descriptors)} DOM elements (dim={vec_size})…")
 
    for i, p in enumerate(point_descriptors):
        text = p["text"]
        if not text or not text.strip():
            logger.warning(f"[Vectorize] Skipping element with empty embedding text: uid={p['uid']}")
            skipped += 1
            continue
 
        try:
            vec = generate_embedding(text)
        except Exception as exc:
            logger.error(f"[Vectorize] Embedding failed for uid={p['uid']}: {exc}")
            skipped += 1
            continue
 
        if not vec or len(vec) != vec_size:
            logger.warning(
                f"[Vectorize] Bad embedding for uid={p['uid']} "
                f"(got {len(vec) if vec else 0} dims, expected {vec_size})"
            )
            skipped += 1
            continue
 
        # ── Qdrant payload — ALL critical fields at TOP LEVEL ─────────────
        # step_generator reads: payload.get("page_url"), payload.get("selector"),
        # payload.get("is_visible"), payload.get("is_obstructed"),
        # payload.get("qa_status") — all must be top-level, not nested in "details".
        payload = {
            "source":         "dom_capture",
            "text":           text,
            "element_type":   p["kind"],
            "project_key":    PROJECT_KEY,
            # ── CRITICAL: page context ──────────────────────────────────
            "page_url":       p["page_url"],          # ← NEW: top-level
            # ── CRITICAL: selector ─────────────────────────────────────
            "selector":       p["selector"],           # ← always non-empty
            # ── CRITICAL: QA signals at top-level ──────────────────────
            "is_visible":     p["is_visible"],
            "is_obstructed":  p["is_obstructed"],
            "qa_status":      p["qa_status"],
            "clickable_score":p["clickable_score"],
            # ── Full element detail (for diagnostics) ──────────────────
            "details":        p["el"],
            "timestamp":      datetime.now().isoformat(),
        }
 
        try:
            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=abs(hash(p["uid"] + (PROJECT_KEY or ""))) % (2 ** 31),
                    vector=vec,
                    payload=payload,
                )],
            )
            uploaded += 1
 
        except Exception as exc:
            logger.error(
                f"[Vectorize] Qdrant upsert failed for uid={p['uid']} "
                f"selector={p['selector']!r:.40} error={exc}"
            )
            skipped += 1
 
        if (i + 1) % 20 == 0:
            print(f"    [{i+1}/{len(point_descriptors)}] uploaded={uploaded} skipped={skipped}…")
 
    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n  ✓ Uploaded {uploaded}/{len(point_descriptors)} vectors → '{collection_name}'")
    if skipped:
        print(f"  ⚠ Skipped {skipped} elements (see log for details)")
 
    print("\n[Classification Summary]")
    for k, v in type_counts.items():
        print(f"  {k}: {v}")
 
    print("\n[Payload Quality Summary]")
    print(f"  Elements with selector   : {uploaded - missing_selector_count} / {len(point_descriptors)}")
    print(f"  Elements with page_url   : {len(point_descriptors) - missing_page_url_count} / {len(point_descriptors)}")
    print(f"  Missing selector (warned): {missing_selector_count}")
    print(f"  Missing page_url (warned): {missing_page_url_count}")
 
    return uploaded

# Add this function near the top of dom_capture_v1.py, after the imports
def sanitize_collection_name(name: str) -> str:
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    return sanitized.strip('_') or 'collection'
# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    global PROJECT_KEY, UI_MEMORY_COLLECTION, DOM_ELEMENTS_FILE, SCREENSHOT_PATH

    parser = argparse.ArgumentParser(
        description="Discovery Engine — QA Intelligence Engine v3"
    )
    parser.add_argument("--project",   required=True,
                        help="Project key, e.g. SCRUM-86")
    parser.add_argument("--url",       default=None,
                        help="Override target URL (default: BASE_URL from .env)")
    parser.add_argument("--vectorize", action="store_true",
                        help="Upload DOM vectors to Qdrant after capture")
    parser.add_argument("--urls",      nargs="+", default=[],
                        help="Additional URL paths to capture, e.g. /dashboard /settings")
    parser.add_argument("--session",   default=None,
                        help="Path to Playwright storage_state JSON (skip login)")
    parser.add_argument("--mobile",    action="store_true",
                        help="Use a mobile viewport (375×812, iPhone 12)")
    parser.add_argument("--no-qa",     action="store_true",
                        help="Skip QA signal enrichment (faster, for DOM-only captures)")
    # ADD this argument
    parser.add_argument("--no-login", action="store_true",
                        help="Skip login attempt entirely (for public sites or pre-authenticated sessions)")
    parser.add_argument("--max-pages", type=int, default=5,
                        help="Maximum number of same-domain pages to crawl per run")
    args = parser.parse_args()

    # ── URL resolution: CLI --url > BASE_URL (.env) > error ────────────────
    target_base_url = args.url or BASE_URL
    if not target_base_url:
        print("✗ No target URL provided. Set BASE_URL in .env or pass --url <url>")
        sys.exit(1)

    # Set globals
    PROJECT_KEY          = args.project
    UI_MEMORY_COLLECTION = sanitize_collection_name(f"{PROJECT_KEY}_ui_memory")
    DOM_ELEMENTS_FILE    = os.path.join(
        OUTPUT_DIR, f"live_dom_elements_{PROJECT_KEY}_{TIMESTAMP}.json")
    SCREENSHOT_PATH      = os.path.join(
        SCREENSHOT_DIR, f"discovery_snapshot_{PROJECT_KEY}_{TIMESTAMP}.png")

    _init_logger()

    print("=" * 60)
    print("Discovery Engine — QA Intelligence Engine v3")
    print("=" * 60)
    print(f"  Project:    {PROJECT_KEY}")
    print(f"  Target URL: {target_base_url}")
    print(f"  Vectorize:  {args.vectorize}")
    print(f"  Mobile:     {args.mobile}")
    print(f"  Max pages:  {args.max_pages}")
    print(f"  Extra URLs: {args.urls or 'none'}")
    print(f"  Session:    {args.session or 'none (will login)'}")
    print(f"  QA signals: {'disabled' if args.no_qa else 'enabled'}")
    print(f"  LLM provider: {os.getenv('LLM_PROVIDER','ollama')}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        viewport = ({"width": 375,  "height": 812}
                    if args.mobile else
                    {"width": 1920, "height": 1080})
        browser = pw.chromium.launch(headless=True, slow_mo=300)

        if args.session and os.path.exists(args.session):
            context = browser.new_context(
                storage_state=args.session, viewport=viewport)
            logger.info(f"[Auth] Session restored from {args.session}")
        else:
            context = browser.new_context(viewport=viewport)

        page = context.new_page()

        try:
            # 1. Navigate
            logger.info(f"[Navigate] → {target_base_url}")
            if not safe_navigate(page, target_base_url):
                logger.warning("[Navigate] Navigation warning — continuing")

            # 2. Blocking check
            if check_system_status(page) == "Blocked":
                logger.error("[Blocked] Site is blocking — saving diagnostic")
                page.screenshot(path=os.path.join(
                    SCREENSHOT_DIR, f"blocked_{PROJECT_KEY}_{TIMESTAMP}.png"),
                    full_page=True)
                sys.exit(1)

            # 3. Login (skip if session provided)
            login_ok = True
            if args.no_login:
                print("[Login] Skipped (--no-login flag set)")
            elif not args.session:
                login_ok = heuristic_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
                print(f"[Login] {'✓' if login_ok else '⚠ may have failed — continuing'}")

            # 4. Stabilise
            stabilize_page(page)

            # 5. Multi-page crawl
            # Base URL → organic crawl (follows links up to max_pages)
            # Explicit --urls → direct navigation only, no link-following

            # AFTER — domain-aware, works for any site
            from urllib.parse import urlparse as _up
            _parsed   = _up(target_base_url)
            _domain   = f"{_parsed.scheme}://{_parsed.netloc}"  # e.g. https://demoqa.com

            extra_urls = [
                _domain + p          if p.startswith("/")    # /text-box → https://demoqa.com/text-box
                else target_base_url.rstrip("/") + "/" + p   if not p.startswith("http")  # relative → absolute
                else p                                        # already absolute URL → use as-is
                for p in (args.urls or [])
            ]

            dom_list = []
            visited_urls = set()

            # ── Base URL: organic crawl ────────────────────────────────────
            base_results = crawl_pages(page, target_base_url, max_pages=max(1, args.max_pages))
            for d in base_results:
                pu = d.get("page_url", "")
                if pu not in visited_urls:
                    visited_urls.add(pu)
                    dom_list.append(d)

            # ── Explicit --urls: direct capture, no link-following ─────────
            for seed_url in extra_urls:
                if seed_url in visited_urls:
                    print(f"[Crawl] Already visited {seed_url} — skipping")
                    continue

                print(f"[Crawl] Direct capture: {seed_url}")
                try:
                    page.goto(seed_url, timeout=15000)
                    time.sleep(1)
                except Exception as e:
                    print(f"  ⚠ Failed to load {seed_url}: {e}")
                    continue

                dom_data = safe_extract_dom(page)
                if not dom_data:
                    print(f"  ⚠ Empty DOM at {seed_url}")
                    continue

                current_url = page.url.rstrip("/")
                dom_data["page_url"] = current_url

                for key in ["all_interactive_elements", "input_elements",
                            "button_elements", "dropdown_elements", "textarea_elements"]:
                    for el in dom_data.get(key, []):
                        el["page_url"] = current_url

                visited_urls.add(current_url)
                dom_list.append(dom_data)

            # ── Enrich and finalise all captured pages ─────────────────────
            all_dom: List[Dict] = []

            for i, dom in enumerate(dom_list):
                dom["project_key"]          = PROJECT_KEY
                dom["login_success"]        = login_ok
                dom["system_status"]        = "Normal"
                dom["extraction_timestamp"] = datetime.now().isoformat()
                dom["mobile_viewport"]      = args.mobile
                dom["iframe_elements"]      = dom.get("iframe_elements", [])

                if not args.no_qa and "qa_analysis" not in dom:
                    enrich_with_qa_signals(page, dom)
                    detect_overlays(page, dom)
                    generate_qa_summary(dom)

                shot = os.path.join(
                    SCREENSHOT_DIR,
                    f"snapshot_{PROJECT_KEY}_{TIMESTAMP}_{i+1}.png"
                )
                capture_screenshot(page, shot, dom)
                all_dom.append(dom)

            # 6. Merge all DOM data (first page is primary)
            merged = all_dom[0] if all_dom else {}
            for extra in all_dom[1:]:
                for key in ["input_elements", "button_elements",
                             "textarea_elements", "dropdown_elements",
                             "custom_dropdown_elements",
                             "all_interactive_elements", "label_associations",
                             "qa_analysis", "overlays_detected"]:
                    merged.setdefault(key, []).extend(extra.get(key, []))
                merged.setdefault("form_structure", {}).update(
                    extra.get("form_structure", {}))

            # Re-compute summary over merged data if multiple pages were captured
            if len(all_dom) > 1 and not args.no_qa:
                generate_qa_summary(merged)

            # Primary screenshot path recorded at top-level for easy access
            merged["screenshot_path"] = merged.get("screenshot_path")

            # 7. Save
            with open(DOM_ELEMENTS_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
            print(f"\n[Save] ✓ DOM data → {DOM_ELEMENTS_FILE}")

            # 8. Vectorise
            if args.vectorize:
                print(f"\n[Vectorize] Uploading to '{UI_MEMORY_COLLECTION}'…")
                n = vectorize_and_upload_dom_elements(merged, UI_MEMORY_COLLECTION)
                print(f"[Vectorize] ✓ {n} vectors uploaded")

            # 9. Summary
            qa_sum = merged.get("qa_summary", {})
            print("\n" + "=" * 60)
            print("DOM EXTRACTION SUMMARY")
            print("=" * 60)
            print(f"  Pages captured:     {len(all_dom)}")
            print(f"  Input elements:     {len(merged.get('input_elements', []))}")
            print(f"  Button elements:    {len(merged.get('button_elements', []))}")
            print(f"  Dropdowns (native): {len(merged.get('dropdown_elements', []))}")
            print(f"  Dropdowns (custom): {len(merged.get('custom_dropdown_elements', []))}")
            print(f"  Textareas:          {len(merged.get('textarea_elements', []))}")
            print(f"  All interactive:    {len(merged.get('all_interactive_elements', []))}")
            print(f"  Forms:              {len(merged.get('form_structure', {}))}")
            print(f"  Label associations: {len(merged.get('label_associations', []))}")
            print(f"  Iframe pages:       {sum(len(d.get('iframe_elements',[])) for d in all_dom)}")
            print(f"  Accessibility tree: {'yes' if merged.get('accessibility_tree') else 'no'}")
            if not args.no_qa:
                print(f"  ── QA Signals ──────────────────────────────")
                print(f"  QA total elements:  {qa_sum.get('total_elements', 0)}")
                print(f"  QA good elements:   {qa_sum.get('good_elements', 0)}")
                print(f"  QA risky elements:  {qa_sum.get('risky_elements', 0)}")
                print(f"  QA health score:    {qa_sum.get('health_score', 0.0):.1%}")
                print(f"  Overlays detected:  {len(merged.get('overlays_detected', []))}")
                print(f"  Overlay present:    {qa_sum.get('overlay_present', False)}")
            print(f"  Screenshot:         {merged.get('screenshot_path') or 'none'}")
            print(f"  DOM file:           {DOM_ELEMENTS_FILE}")
            print("=" * 60)
            print("✓ QA Intelligence Engine v3 completed!")

        except Exception as e:
            logger.error(f"✗ Fatal error: {e}")
            try:
                page.screenshot(path=os.path.join(
                    SCREENSHOT_DIR, f"error_{PROJECT_KEY}_{TIMESTAMP}.png"))
            except Exception:
                pass
            raise
        finally:
            browser.close()
            logger.info("[Browser] Closed")


if __name__ == "__main__":
    main()
