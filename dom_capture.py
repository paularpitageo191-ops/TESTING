#!/usr/bin/env python3
"""
Discovery Engine — Site-Agnostic DOM Capture  (v2)
===================================================
Captures the live UI state of any web application and stores the result as:
  • A timestamped JSON file  (docs/live_dom_elements_{PROJECT}_{TS}.json)
  • Vectors in Qdrant        ({PROJECT_KEY}_ui_memory)

Improvements over v1
─────────────────────
  1.  Embeddings via llm_gateway.py — provider-agnostic (ollama / openai /
      claude / gemini).  VECTOR_SIZE is probed at runtime, not hardcoded.
  2.  Shadow DOM traversal — recursively queries every shadow root so Web
      Components (Lit, Stencil, Angular Elements, etc.) are captured.
  3.  Custom dropdowns — queries [role="listbox"], [role="combobox"],
      [aria-haspopup], [data-*] in addition to native <select>.
  4.  Richer label resolution — aria-label, aria-labelledby, title,
      data-label, placeholder, and ancestor text all considered.
  5.  data-testid / data-cy / data-qa captured on every element.
  6.  form_structure actually populated (forms → fields mapping).
  7.  Lazy content — scrolls the full page before extraction.
  8.  iframe traversal — tries to extract elements from same-origin iframes.
  9.  No framework-specific class filters (removed hardcoded oxd).
  10. Enhanced classifyIntent — covers toggle, slider, date-picker,
      file-upload, badge, search, tab, dialog.
  11. Correct getXPath — counts all same-tag siblings, not just previous ones.
  12. Multi-step login — detects if password field appears after username.
  13. Session restore — --session flag loads saved Playwright storage_state.
  14. Blocking detection uses HTTP response status, not page text scanning.
  15. Logging deferred until PROJECT_KEY is known (no more _None.log).
  16. --urls flag for multi-page capture; --mobile flag for mobile viewport.
  17. Accessibility tree snapshot included in output.

Usage
─────
  # Basic (captures BASE_URL from .env)
  python3 dom_capture.py --project SCRUM-86

  # With immediate vectorisation
  python3 dom_capture.py --project SCRUM-86 --vectorize

  # Capture multiple pages
  python3 dom_capture.py --project SCRUM-86 --urls /login /dashboard /settings

  # Restore a saved session (skip login entirely)
  python3 dom_capture.py --project SCRUM-86 --session docs/session.json

  # Mobile viewport
  python3 dom_capture.py --project SCRUM-86 --mobile
"""

import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from dotenv import load_dotenv

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
    return _get_gateway().generate_embedding(text)


def get_vector_size() -> int:
    """
    Probe the actual embedding dimension rather than hardcoding 1024.
    Dimension table:
      ollama  mxbai-embed-large  → 1024
      openai  text-embedding-ada-002 → 1536
      gemini  text-embedding-004 → 768
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
        # Click "Next" / "Continue" to reveal password step
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
        # Use HTTP status from navigation response, not page text
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

# The entire JS payload that runs in the browser context.
# Fixes vs v1:
#  • getXPath — counts all siblings (not just previous), correct index
#  • findLabel — also checks aria-label, aria-labelledby, title, data-label
#  • getRelativeSelector — no framework-specific class filter
#  • classifyIntent — adds toggle/switch, slider, date-picker, file-upload,
#                     search, tab, dialog, badge, notification
#  • Custom dropdowns — queries [role="listbox"], [aria-haspopup="listbox"]
#  • data-testid / data-cy / data-qa captured on every element
#  • form_structure populated
#  • Shadow DOM recursive traversal
#  • iframe content (same-origin only, silently skips cross-origin)

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

    // FIX: correct XPath — counts ALL siblings with same tag
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

    // FIX: comprehensive label resolution
    function findLabel(el) {
        // 1. aria-labelledby
        const lbid = el.getAttribute('aria-labelledby');
        if (lbid) {
            const t = lbid.split(' ')
                .map(id => document.getElementById(id))
                .filter(Boolean)
                .map(n => n.textContent.trim())
                .join(' ');
            if (t) return t;
        }
        // 2. aria-label
        const al = el.getAttribute('aria-label');
        if (al) return al.trim();
        // 3. <label for="id">
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return lbl.textContent.trim();
        }
        // 4. Wrapping <label>
        const pl = el.closest('label');
        if (pl) return pl.textContent.trim().split('\n')[0].trim();
        // 5. Preceding <label> sibling
        let sib = el.previousElementSibling;
        while (sib) {
            if (sib.tagName === 'LABEL') return sib.textContent.trim();
            sib = sib.previousElementSibling;
        }
        // 6. Parent's first label child
        const pLabel = el.parentElement && el.parentElement.querySelector('label');
        if (pLabel) return pLabel.textContent.trim();
        // 7. title attribute
        const title = el.getAttribute('title');
        if (title) return title.trim();
        // 8. data-label
        const dl = el.getAttribute('data-label');
        if (dl) return dl.trim();
        // 9. placeholder as last resort
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

    // FIX: no framework-specific class filter
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

    // FIX: expanded intent classification
    function classifyIntent(el, label, textContent) {
        const tag  = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        const cls  = (el.className || '').toLowerCase();
        const role = inferAriaRole(el);
        const t    = (textContent || '').toLowerCase().trim();
        const lbl  = (label || '').toLowerCase().trim();
        const both = t + ' ' + lbl;

        // Toggle / Switch
        if (type === 'checkbox' || role === 'switch' ||
            cls.includes('toggle') || cls.includes('switch')) {
            return { intent: 'Toggle', confidence: 0.9 };
        }
        // Slider / Range
        if (type === 'range' || role === 'slider') {
            return { intent: 'Slider', confidence: 0.9 };
        }
        // Date picker
        if (type === 'date' || type === 'datetime-local' || type === 'month' ||
            cls.includes('datepicker') || cls.includes('date-picker') ||
            el.getAttribute('data-datepicker') != null) {
            return { intent: 'DatePicker', confidence: 0.9 };
        }
        // File upload
        if (type === 'file' || cls.includes('file-upload') || cls.includes('dropzone')) {
            return { intent: 'FileUpload', confidence: 0.9 };
        }
        // Search
        if (type === 'search' || cls.includes('search') ||
            ['search','find','lookup'].some(w => both.includes(w))) {
            return { intent: 'Search', confidence: 0.85 };
        }
        // Buttons
        if (tag === 'button' || role === 'button' || type === 'submit') {
            const primary   = ['submit','apply','save','create','add','update','confirm','ok','send','post'];
            const secondary = ['cancel','clear','reset','close','back','dismiss'];
            if (primary.some(w   => both.includes(w))) return { intent: 'PrimaryAction',   confidence: 0.9 };
            if (secondary.some(w => both.includes(w))) return { intent: 'SecondaryAction', confidence: 0.8 };
            return { intent: 'PrimaryAction', confidence: 0.6 };
        }
        // Navigation
        if (tag === 'a' || role === 'link' ||
            cls.includes('nav') || cls.includes('menu') || cls.includes('breadcrumb')) {
            return { intent: 'Navigation', confidence: 0.8 };
        }
        // Tab
        if (role === 'tab' || cls.includes('tab')) {
            return { intent: 'Tab', confidence: 0.85 };
        }
        // Dialog trigger
        if (role === 'dialog' || cls.includes('modal')) {
            return { intent: 'Dialog', confidence: 0.8 };
        }
        // Input / Select
        if (['input','select','textarea'].includes(tag) ||
            ['textbox','combobox','listbox','spinbutton'].includes(role)) {
            return { intent: 'Input', confidence: 0.9 };
        }
        // Informational / Badge
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

    # Accessibility tree snapshot (Playwright built-in)
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
# Qdrant vectorisation — provider-agnostic
# ══════════════════════════════════════════════════════════════════════════

def _build_embedding_text(kind: str, el: Dict) -> str:
    """Build a rich natural-language string for embedding."""
    parts = [kind]
    for field in ["label", "placeholder", "text", "name", "type",
                  "ariaRole", "intent", "ariaLabel"]:
        v = (el.get(field) or "").strip()
        if v:
            parts.append(f"{field}={v!r}")
    # Include option values for dropdowns
    opts = el.get("options", [])
    if opts:
        opt_texts = [o.get("text", o) if isinstance(o, dict) else str(o)
                     for o in opts[:8]]
        parts.append(f"options=[{', '.join(opt_texts)}]")
    return " ".join(parts)


def vectorize_and_upload_dom_elements(dom_data: Dict,
                                       collection_name: str) -> int:
    """
    Vectorize DOM elements and upload to the project-specific Qdrant collection.
    Vector size is probed from the embedding provider, not hardcoded.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct

    client   = QdrantClient(url=QDRANT_URL)
    vec_size = get_vector_size()

    collections = {c.name for c in client.get_collections().collections}
    if collection_name not in collections:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vec_size, distance=Distance.COSINE),
        )
        logger.info(f"[Qdrant] Created '{collection_name}' (dim={vec_size})")
    else:
        logger.info(f"[Qdrant] Using existing '{collection_name}'")

    # Build point list from all element categories
    points = []
    for kind, items in [
        ("input",          dom_data.get("input_elements",          [])),
        ("button",         dom_data.get("button_elements",         [])),
        ("textarea",       dom_data.get("textarea_elements",       [])),
        ("dropdown",       dom_data.get("dropdown_elements",       [])),
        ("custom_dropdown",dom_data.get("custom_dropdown_elements",[])),
        ("interactive",    dom_data.get("all_interactive_elements",[])),
    ]:
        for i, el in enumerate(items):
            points.append({
                "uid":  f"{kind}_{i}",
                "text": _build_embedding_text(kind, el),
                "kind": kind,
                "el":   el,
            })

    uploaded = 0
    print(f"  Vectorizing {len(points)} DOM elements (dim={vec_size})…")
    for i, p in enumerate(points):
        try:
            vec = generate_embedding(p["text"])
            if not vec:
                continue
            client.upsert(
                collection_name=collection_name,
                points=[PointStruct(
                    id=hash(p["uid"] + PROJECT_KEY) % (2**31),
                    vector=vec,
                    payload={
                        "source":       "dom_capture",
                        "text":         p["text"],
                        "element_type": p["kind"],
                        "project_key":  PROJECT_KEY,
                        "details":      p["el"],
                        "timestamp":    datetime.now().isoformat(),
                    },
                )],
            )
            uploaded += 1
            if (i + 1) % 20 == 0:
                print(f"    [{i+1}/{len(points)}]…")
        except Exception as e:
            logger.error(f"    ✗ {e}")

    print(f"  ✓ Uploaded {uploaded}/{len(points)} vectors → '{collection_name}'")
    return uploaded


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main():
    global PROJECT_KEY, UI_MEMORY_COLLECTION, DOM_ELEMENTS_FILE, SCREENSHOT_PATH

    parser = argparse.ArgumentParser(
        description="Discovery Engine — Site-Agnostic DOM Capture v2"
    )
    parser.add_argument("--project",   required=True,
                        help="Project key, e.g. SCRUM-86")
    parser.add_argument("--vectorize", action="store_true",
                        help="Upload DOM vectors to Qdrant after capture")
    parser.add_argument("--urls",      nargs="+", default=[],
                        help="Additional URL paths to capture, e.g. /dashboard /settings")
    parser.add_argument("--session",   default=None,
                        help="Path to Playwright storage_state JSON (skip login)")
    parser.add_argument("--mobile",    action="store_true",
                        help="Use a mobile viewport (375×812, iPhone 12)")
    args = parser.parse_args()

    # Set globals
    PROJECT_KEY          = args.project
    UI_MEMORY_COLLECTION = f"{PROJECT_KEY}_ui_memory"
    DOM_ELEMENTS_FILE    = os.path.join(
        OUTPUT_DIR, f"live_dom_elements_{PROJECT_KEY}_{TIMESTAMP}.json")
    SCREENSHOT_PATH      = os.path.join(
        SCREENSHOT_DIR, f"discovery_snapshot_{PROJECT_KEY}_{TIMESTAMP}.png")

    _init_logger()  # NOW we can log with the correct project key

    print("=" * 60)
    print("Discovery Engine — DOM Capture v2")
    print("=" * 60)
    print(f"  Project:    {PROJECT_KEY}")
    print(f"  Target URL: {BASE_URL}")
    print(f"  Vectorize:  {args.vectorize}")
    print(f"  Mobile:     {args.mobile}")
    print(f"  Extra URLs: {args.urls or 'none'}")
    print(f"  Session:    {args.session or 'none (will login)'}")
    print(f"  LLM provider: {os.getenv('LLM_PROVIDER','ollama')}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        viewport = ({"width": 375,  "height": 812}
                    if args.mobile else
                    {"width": 1920, "height": 1080})
        browser = pw.chromium.launch(headless=True, slow_mo=300)

        # Session restore or fresh context
        if args.session and os.path.exists(args.session):
            context = browser.new_context(
                storage_state=args.session, viewport=viewport)
            logger.info(f"[Auth] Session restored from {args.session}")
        else:
            context = browser.new_context(viewport=viewport)

        page = context.new_page()

        try:
            # 1. Navigate
            logger.info(f"[Navigate] → {BASE_URL}")
            if not safe_navigate(page, BASE_URL):
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
            if not args.session:
                login_ok = heuristic_login(page, ADMIN_USERNAME, ADMIN_PASSWORD)
                print(f"[Login] {'✓' if login_ok else '⚠ may have failed — continuing'}")

            # 4. Stabilise
            stabilize_page(page)

            # 5. Collect URLs to capture
            target_urls = [page.url]  # post-login landing page
            for rel in args.urls:
                full = BASE_URL.rstrip("/") + "/" + rel.lstrip("/")
                if full not in target_urls:
                    target_urls.append(full)

            # 6. Capture each URL
            all_dom: List[Dict] = []
            for url in target_urls:
                if url != page.url:
                    if not safe_navigate(page, url):
                        continue
                    stabilize_page(page)

                print(f"[DOM] Extracting: {page.url}")
                dom = safe_extract_dom(page)
                dom["project_key"]          = PROJECT_KEY
                dom["login_success"]        = login_ok
                dom["system_status"]        = "Normal"
                dom["extraction_timestamp"] = datetime.now().isoformat()
                dom["mobile_viewport"]      = args.mobile

                # Iframe elements
                iframe_data = extract_iframe_elements(page)
                dom["iframe_elements"] = iframe_data

                all_dom.append(dom)

                # Per-page screenshot
                shot = os.path.join(
                    SCREENSHOT_DIR,
                    f"snapshot_{PROJECT_KEY}_{TIMESTAMP}_{len(all_dom)}.png")
                try:
                    page.screenshot(path=shot, full_page=True)
                    print(f"[Screenshot] → {shot}")
                except Exception as e:
                    logger.warning(f"[Screenshot] {e}")

            # 7. Merge all DOM data (first page is primary)
            merged = all_dom[0] if all_dom else {}
            for extra in all_dom[1:]:
                for key in ["input_elements", "button_elements",
                             "textarea_elements", "dropdown_elements",
                             "custom_dropdown_elements",
                             "all_interactive_elements", "label_associations"]:
                    merged.setdefault(key, []).extend(extra.get(key, []))
                merged.setdefault("form_structure", {}).update(
                    extra.get("form_structure", {}))

            # 8. Save
            with open(DOM_ELEMENTS_FILE, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
            print(f"\n[Save] ✓ DOM data → {DOM_ELEMENTS_FILE}")

            # 9. Vectorise
            if args.vectorize:
                print(f"\n[Vectorize] Uploading to '{UI_MEMORY_COLLECTION}'…")
                n = vectorize_and_upload_dom_elements(merged, UI_MEMORY_COLLECTION)
                print(f"[Vectorize] ✓ {n} vectors uploaded")

            # 10. Summary
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
            print(f"  DOM file:           {DOM_ELEMENTS_FILE}")
            print("=" * 60)
            print("✓ Discovery Engine completed!")

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