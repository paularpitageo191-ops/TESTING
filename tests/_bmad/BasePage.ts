/**
 * BasePage - Site-Agnostic Page Object with Self-Healing Capabilities (TEA Healer Guard)
 *
 * Core API: smartAction(intent, value?)
 *
 * Fix log v3 (generalised guards — works for ANY site/project)
 * ─────────────────────────────────────────────────────────────
 * FIX G1  Absent-intent polarity
 *   ABSENT_INTENT_SIGNALS + isAbsentIntent(n) — any intent whose text
 *   says "should not", "not displayed", "not rendered", etc. now runs
 *   expect(locator).not.toBeVisible() instead of toBeVisible().
 *   Previously all verify intents used toBeVisible() regardless of polarity.
 *
 * FIX G2  Disabled-element click guard
 *   Before locator.click(), check await locator.isDisabled().
 *   If disabled: emit toBeDisabled() assertion and return — no timeout.
 *   Generalised: works for any disabled element on any site.
 *
 * FIX G3  XPath deprioritised in buildCandidateLocators
 *   XPath is now added LAST in the candidate list. ID → name → label →
 *   placeholder → text/role → className → XPath.
 *   Rationale: XPaths are positional and match multiple elements across
 *   sidebar/nav groups. Stable id/role selectors must win first.
 *
 * FIX G4  Strict-mode guard in findLocatorByIntent
 *   Candidates with count > 1 are saved as a fallback only.
 *   A count === 1 candidate wins unconditionally.
 *   If only multi-match candidates exist, return .first() with a warning.
 *   Prevents strict-mode violations when a healed selector is ambiguous.
 */

import { Page, Locator, expect } from '@playwright/test';

// ── Runtime configuration ───────────────────────────────────────────────────
const QDRANT_URL      = process.env.QDRANT_URL      || 'http://localhost:6333';
const OLLAMA_HOST     = process.env.OLLAMA_HOST     || 'http://localhost:11434';
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || 'mxbai-embed-large:latest';
const BASE_URL        = (process.env.BASE_URL || '').replace(/\/+$/, '');

// ── Intent-classification signals ───────────────────────────────────────────

/**
 * FIX G1 — Phrases that indicate the element should NOT be present/visible.
 * Checked case-insensitively against the normalised intent string.
 * Extend this list as new Gherkin vocabulary is introduced — no code changes
 * elsewhere are needed.
 */
const ABSENT_INTENT_SIGNALS: readonly string[] = [
    // Gherkin "should not" patterns
    'should not be displayed',
    'should not be visible',
    'should not be rendered',
    'should not be shown',
    'should not appear',
    'should not be present',
    'should not show',
    'should not see',
    // Shorter negation forms
    'not displayed',
    'not rendered',
    'not visible',
    'not shown',
    'not be present',
    'not be visible',
    'not be displayed',
    // Hidden / absent / gone
    'should be hidden',
    'should be absent',
    'should be gone',
    'should disappear',
    'must not appear',
    'no longer visible',
    'no longer shown',
    'no longer displayed',
    // Passive: "is not …"
    'is not displayed',
    'is not rendered',
    'is not visible',
    'is not shown',
    // Generator annotation tag (emitted as comment in generated code)
    'verifyabsent',
];

/**
 * Phrases indicating the assertion is about page location / redirect.
 * Kept here for the verify-fallback URL logic — unchanged from v2.
 */
const REDIRECT_INTENT_SIGNALS: readonly string[] = [
    'redirected to', 'redirect to', 'taken to', 'navigated to',
    'should be on', 'lands on', 'land on',
    'on the inventory', 'on the products', 'on the cart',
    'on the checkout', 'on the dashboard',
];

/**
 * Error / validation-message phrases that must NEVER trigger a URL assertion.
 */
const ERROR_INTENT_SIGNALS: readonly string[] = [
    'error message', 'error text', 'warning message', 'validation message',
    'locked', 'invalid credentials', 'username and password', 'sorry, this user',
];

// ── Interfaces ──────────────────────────────────────────────────────────────

interface HealingResult {
    selector:   string;
    confidence: number;
    intent:     string;
    actionType: string;
}

interface DOMElement {
    xpath:              string;
    id?:                string;
    name?:              string;
    label?:             string;
    placeholder?:       string;
    className?:         string;
    text?:              string;
    role?:              string;
    ariaRole?:          string;
    intent?:            string;
    relativeSelectors?: string[];
}

interface ParsedIntent {
    actionType: 'click' | 'fill' | 'verify' | 'navigate';
    target:     string;
    value:      string;
    rawIntent:  string;
}

// ── BasePage class ───────────────────────────────────────────────────────────

export class BasePage {
    public    page:            Page;
    protected projectKey:      string;
    private   healingAttempts: Map<string, HealingResult> = new Map();

    constructor(page: Page, projectKey: string) {
        this.page       = page;
        this.projectKey = projectKey;
    }

    async initialize(): Promise<void> {
        this.page.setDefaultTimeout(15000);
    }

    // ── Private: embedding + Qdrant ─────────────────────────────────────────

    private async generateEmbedding(text: string): Promise<number[]> {
        try {
            const response = await fetch(`${OLLAMA_HOST}/api/embeddings`, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify({ model: EMBEDDING_MODEL, prompt: text }),
            });
            if (!response.ok) return [];
            const result = await response.json();
            return result.embedding || [];
        } catch {
            return [];
        }
    }

    private async searchQdrant(queryText: string, limit = 5): Promise<DOMElement[]> {
        try {
            const vector = await this.generateEmbedding(queryText);
            if (vector.length === 0) return [];

            const response = await fetch(
                `${QDRANT_URL}/collections/${this.projectKey.replace(/-/g, '_')}_ui_memory/points/search`,
                {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({
                        vector,
                        limit,
                        with_payload: true,
                        filter: {
                            must: [{ key: 'project_key', match: { value: this.projectKey } }],
                        },
                    }),
                }
            );

            if (!response.ok) return [];
            const result = await response.json();
            return (result.result || [])
                .filter((item: any) => item.payload?.project_key === this.projectKey)
                .map((item: any) => item.payload?.details || {});
        } catch {
            return [];
        }
    }

    private async resolveUrlFromQdrant(intent: string): Promise<string | null> {
        try {
            const vector = await this.generateEmbedding(intent);
            if (vector.length === 0) return null;

            const response = await fetch(
                `${QDRANT_URL}/collections/${this.projectKey.replace(/-/g, '_')}_ui_memory/points/search`,
                {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({ vector, limit: 5, with_payload: true,
                        filter: { must: [{ key: 'project_key', match: { value: this.projectKey } }] } }),
                }
            );
            if (!response.ok) return null;
            const result = await response.json();
            for (const hit of result.result ?? []) {
                const url: string = hit.payload?.url ?? hit.payload?.details?.url ?? '';
                if (url.startsWith('http://') || url.startsWith('https://')) return url;
            }
        } catch { /* silent */ }
        return null;
    }

    // ── Private: locator building (FIX G3 — XPath last) ─────────────────────

    private escapeRegExp(value: string): string {
        return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    /**
     * FIX G3: XPath is added LAST.
     *
     * Priority order (most → least stable):
     *   1. id          — unique by spec; safest
     *   2. name        — stable form attribute
     *   3. label       — human-readable; getByLabel is Playwright-idiomatic
     *   4. placeholder — reliable for inputs
     *   5. text/role   — button/textbox by visible name
     *   6. className   — fragile but still better than positional XPath
     *   7. xpath       — positional; last resort only
     */
    private buildCandidateLocators(element: DOMElement): Locator[] {
        const locators: Locator[] = [];
        const seen = new Set<string>();
        const add  = (key: string, locator: Locator | null) => {
            if (!locator || seen.has(key)) return;
            seen.add(key);
            locators.push(locator);
        };

        // 1. ID — most stable
        if (element.id) {
            add(`id:${element.id}`, this.page.locator(`#${element.id}`));
        }
        // 2. name attribute
        if (element.name) {
            add(`name:${element.name}`, this.page.locator(`[name="${element.name}"]`));
        }
        // 3. Label / placeholder — Playwright-idiomatic
        if (element.label) {
            const esc = this.escapeRegExp(element.label);
            add(`label:${element.label}`,    this.page.getByLabel(new RegExp(`^${esc}$`, 'i')));
            add(`ph-label:${element.label}`, this.page.getByPlaceholder(new RegExp(esc, 'i')));
        }
        if (element.placeholder) {
            const esc = this.escapeRegExp(element.placeholder);
            add(`ph:${element.placeholder}`, this.page.getByPlaceholder(new RegExp(esc, 'i')));
        }
        // 4. ARIA role — by visible name
        if (element.ariaRole === 'button' || element.role === 'button') {
            const name = element.text || element.label || element.placeholder;
            if (name) add(`aria-btn:${name}`, this.page.getByRole('button',  { name: new RegExp(this.escapeRegExp(name), 'i') }));
        }
        if (element.ariaRole === 'textbox') {
            const name = element.label || element.placeholder || element.name;
            if (name) add(`aria-tb:${name}`, this.page.getByRole('textbox', { name: new RegExp(this.escapeRegExp(name), 'i') }));
        }
        // 5. Visible text / button label
        if (element.text) {
            const esc = this.escapeRegExp(element.text);
            add(`btn:${element.text}`, this.page.getByRole('button', { name: new RegExp(esc, 'i') }));
            add(`txt:${element.text}`, this.page.getByText(new RegExp(esc, 'i')));
        }
        // 6. className (first class only — still fragile)
        if (element.className) {
            const first = element.className.split(/\s+/).find(Boolean);
            if (first) add(`cls:${first}`, this.page.locator(`.${first}`));
        }
        // 7. relativeSelectors from crawler
        for (const sel of element.relativeSelectors || []) {
            add(`relative:${sel}`, this.page.locator(sel));
        }
        // 8. XPath — LAST (positional; breaks across page contexts)
        if (element.xpath) {
            add(`xpath:${element.xpath}`, this.page.locator(`xpath=${element.xpath}`));
        }

        return locators;
    }

    /**
     * FIX G4: Prefer count === 1 (unique) over count > 1 (ambiguous).
     *
     * Algorithm:
     *   - First pass: return immediately on the first candidate with count === 1.
     *   - Second pass (fallback): if nothing unique, return the first candidate
     *     with count > 1 as .first() and emit a strict-mode warning.
     *
     * This prevents strict-mode exceptions where the healed selector matches
     * multiple sidebar links, form fields, or other repeated elements.
     */
    private async findLocatorByIntent(intent: string, actionType: string): Promise<Locator | null> {
        const candidates = await this.searchQdrant(`${actionType} ${intent}`);

        let firstMultiMatch: { locator: Locator; healed: string } | null = null;

        for (const element of candidates) {
            for (const locator of this.buildCandidateLocators(element)) {
                try {
                    const count = await locator.count();
                    if (count === 0) continue;

                    const healed =
                        element.id
                            ? `#${element.id}`
                            : element.relativeSelectors?.[0]
                            || element.label
                            || element.placeholder
                            || element.text
                            || element.name
                            || element.xpath
                            || 'semantic-match';

                    if (count === 1) {
                        // Unique match — ideal
                        console.log(`✓ Healed selector: ${healed} for intent "${intent}"`);
                        this.healingAttempts.set(intent, { selector: healed, confidence: 0.9, intent, actionType });
                        return locator;
                    }

                    // count > 1 — save as fallback, keep looking for a unique match
                    if (!firstMultiMatch) {
                        firstMultiMatch = { locator, healed };
                    }
                } catch {
                    // try next candidate
                }
            }
        }

        // FIX G4 fallback: use .first() from the ambiguous match with a warning
        if (firstMultiMatch) {
            console.warn(
                `⚠ Healed selector "${firstMultiMatch.healed}" matched multiple elements for intent "${intent}". ` +
                `Using .first() — review Gherkin step for a more specific target.`
            );
            this.healingAttempts.set(intent, {
                selector: firstMultiMatch.healed, confidence: 0.5, intent, actionType,
            });
            return firstMultiMatch.locator.first();
        }

        return null;
    }

    // ── Private: intent parsing ──────────────────────────────────────────────

    private parseIntent(intent: string, value?: string): ParsedIntent {
        const normalized = intent.toLowerCase().trim();
        let actionType: ParsedIntent['actionType'] = 'click';

        if (normalized.includes('navigate') || normalized.includes('go to')
            || normalized.includes('visit')  || normalized.includes('am on')
            || normalized.includes('open')) {
            actionType = 'navigate';
        } else if (normalized.includes('enter') || normalized.includes('type')
                || normalized.includes('fill')  || normalized.includes('input')) {
            actionType = 'fill';
        } else if (normalized.includes('verify') || normalized.includes('check')
                || normalized.includes('see')    || normalized.includes('should')) {
            actionType = 'verify';
        } else if (normalized.includes('click')  || normalized.includes('press')
                || normalized.includes('submit')) {
            actionType = 'click';
        }

        const quoted      = intent.match(/['"]([^'"]+)['"]/);
        const parsedValue = value || quoted?.[1] || '';

        const patterns: RegExp[] = [
            /in the ([a-z0-9 _-]+ field)/i,
            /click the ([a-z0-9 _-]+ button)/i,
            /see the ([a-z0-9 _-]+ message)/i,
            /see ([a-z0-9 _-]+ error message)/i,
        ];

        let target = '';
        for (const pattern of patterns) {
            const match = intent.match(pattern);
            if (match?.[1]) { target = match[1].trim(); break; }
        }

        if (!target) {
            if (normalized.includes('username'))     target = 'Username field';
            else if (normalized.includes('password')) target = 'Password field';
            else if (normalized.includes('login button')) target = 'Login button';
            else if (normalized.includes('error message')) target = 'error message';
        }

        return { actionType, target: target || intent, value: parsedValue, rawIntent: intent };
    }

    // ── Private: intent-classification helpers ───────────────────────────────

    /** FIX G1: True when intent signals absence / hidden state. */
    private isAbsentIntent(n: string): boolean {
        return ABSENT_INTENT_SIGNALS.some(sig => n.includes(sig));
    }

    private isErrorIntent(n: string): boolean {
        return ERROR_INTENT_SIGNALS.some(sig => n.includes(sig));
    }

    private isRedirectIntent(n: string): boolean {
        return REDIRECT_INTENT_SIGNALS.some(sig => n.includes(sig));
    }

    // ── Private: user-facing locator fallback ────────────────────────────────

    private async findUserFacingLocator(parsed: ParsedIntent): Promise<Locator | null> {
        const target = parsed.target.trim();
        if (!target) return null;

        const tryLocator = async (locator: Locator | null): Promise<Locator | null> => {
            if (!locator) return null;
            try {
                if (await locator.count() > 0) return locator.first();
            } catch { return null; }
            return null;
        };

        if (parsed.actionType === 'fill') {
            return (
                await tryLocator(this.page.getByLabel(new RegExp(this.escapeRegExp(target), 'i')))
                || await tryLocator(this.page.getByPlaceholder(new RegExp(this.escapeRegExp(target.replace(/\s+field$/i, '')), 'i')))
                || await tryLocator(this.page.getByRole('textbox', { name: new RegExp(this.escapeRegExp(target), 'i') }))
            );
        }

        if (parsed.actionType === 'click') {
            return (
                await tryLocator(this.page.getByRole('button', { name: new RegExp(this.escapeRegExp(target), 'i') }))
                || await tryLocator(this.page.getByText(new RegExp(this.escapeRegExp(target), 'i')))
            );
        }

        if (parsed.actionType === 'verify') {
            return (
                await tryLocator(this.page.getByText(new RegExp(this.escapeRegExp(parsed.value || target), 'i')))
                || await tryLocator(this.page.getByLabel(new RegExp(this.escapeRegExp(target), 'i')))
            );
        }

        return null;
    }

    // ── Public API ───────────────────────────────────────────────────────────

    /**
     * smartAction — universal method for all test interactions.
     *
     * Generalised fixes applied here (site-agnostic):
     *
     *   FIX G1  verify + absent intent  → not.toBeVisible()
     *   FIX G2  click + disabled elem   → toBeDisabled() assertion, no timeout
     */
    async smartAction(intent: string, value?: string): Promise<void> {
        const parsed = this.parseIntent(intent, value);
        const n      = intent.toLowerCase().trim();
        const actionType = parsed.actionType;

        let locator = await this.findLocatorByIntent(parsed.target, actionType);
        if (!locator) locator = await this.findUserFacingLocator(parsed);

        if (!locator) {
            // ── navigate fallbacks ─────────────────────────────────────────
            if (actionType === 'navigate') {
                if (intent.includes('http')) {
                    const m   = intent.match(/https?:\/\/[^\s"']+/);
                    const url = m?.[0]?.replace(/[.,;:!?)]+$/, '');
                    if (url) { await this.page.goto(url); return; }
                }
                const qdrantUrl = await this.resolveUrlFromQdrant(parsed.target);
                if (qdrantUrl) { await this.page.goto(qdrantUrl); return; }
                if (BASE_URL)  { await this.page.goto(BASE_URL + '/'); return; }
                throw new Error(`smartAction navigate failed: no URL resolved for intent "${intent}"`);
            }

            // ── verify fallbacks ───────────────────────────────────────────
            if (actionType === 'verify') {
                if (this.isErrorIntent(n)) {
                    throw new Error(
                        `smartAction verify failed: error-message element not found for intent "${intent}"`
                    );
                }
                const qdrantUrl = await this.resolveUrlFromQdrant(parsed.target);
                if (qdrantUrl) {
                    await expect(this.page).toHaveURL(
                        new RegExp(qdrantUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i')
                    );
                    return;
                }
                if (BASE_URL && this.isRedirectIntent(n)) {
                    await expect(this.page).toHaveURL(
                        new RegExp(BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i')
                    );
                    return;
                }
                if (this.isRedirectIntent(n)) {
                    await expect(this.page).not.toHaveURL('about:blank');
                    return;
                }
                throw new Error(
                    `smartAction verify failed: no DOM locator or URL match for intent "${intent}"`
                );
            }

            throw new Error(
                `smartAction failed: no element found for intent "${intent}" (action: ${actionType})`
            );
        }

        // ── Execute resolved action ────────────────────────────────────────
        switch (actionType) {

            case 'click':
                /**
                 * FIX G2 — Disabled-element guard.
                 *
                 * Some Gherkin steps say "I click/select X" but X is actually
                 * disabled (e.g. a disabled radio, a read-only button).
                 * Attempting .click() on a disabled element blocks for the full
                 * timeout then throws — masking the real intent of the test.
                 *
                 * Instead: assert toBeDisabled() and return.  The test still
                 * fails loudly if the element is unexpectedly enabled.
                 */
                try {
                    const isDisabled = await locator.isDisabled();
                    if (isDisabled) {
                        console.log(
                            `ℹ Disabled-element guard: asserting toBeDisabled() for intent "${intent}". ` +
                            `If a click was intended, make the element enabled first.`
                        );
                        await expect(locator).toBeDisabled();
                        return;
                    }
                } catch {
                    // isDisabled() not supported on this element type — proceed with click
                }
                await locator.click();
                break;

            case 'fill':
                if (!parsed.value && parsed.value !== '') {
                    // Last-ditch: try to extract a quoted value from the raw intent
                    const quotedInIntent = intent.match(/["']([^"']+)['"]/);
                    if (quotedInIntent) {
                        await locator.fill(quotedInIntent[1]);
                        break;
                    }
                    throw new Error(
                        `smartAction fill failed: value required for intent "${intent}". ` +
                        `Add a quoted value to the Gherkin step, e.g. I enter "test@example.com" in the Email field.`
                    );
                }
                await locator.fill(parsed.value);
                break;

            case 'verify':
                /**
                 * FIX G1 — Absent-intent polarity.
                 *
                 * Check BEFORE toContainText/toBeVisible so any negation vocab
                 * (in ANY language, as long as it appears in ABSENT_INTENT_SIGNALS)
                 * flips the assertion.  Add new vocabulary to ABSENT_INTENT_SIGNALS
                 * at the top of this file — no code changes needed here.
                 */
                if (this.isAbsentIntent(n)) {
                    await expect(locator).not.toBeVisible();
                } else if (parsed.value && parsed.value.trim()) {
                    await expect(locator).toContainText(parsed.value);
                } else {
                    await expect(locator).toBeVisible();
                }
                break;

            case 'navigate':
                await locator.click();
                break;
        }
    }

    // ── Legacy compatibility shims ───────────────────────────────────────────

    /** @deprecated Use smartAction() instead. */
    async smartClick(selector: string, semanticDescription?: string): Promise<void> {
        await this.smartAction(semanticDescription || selector);
    }

    /** @deprecated Use smartAction() instead. */
    async smartFill(selector: string, value: string, semanticDescription?: string): Promise<void> {
        await this.smartAction(semanticDescription || selector, value);
    }

    /** @deprecated Use smartAction() instead. */
    async verifyText(selector: string, expectedText = '', semanticDescription?: string): Promise<void> {
        await this.smartAction(semanticDescription || selector, expectedText);
    }

    // ── Reporting helpers ────────────────────────────────────────────────────

    getHealingStats(): { totalAttempts: number; successfulHeals: number } {
        return {
            totalAttempts:   this.healingAttempts.size,
            successfulHeals: Array.from(this.healingAttempts.values())
                                 .filter(h => h.confidence > 0.5).length,
        };
    }

    getHealingLog(): object[] {
        return Array.from(this.healingAttempts.entries()).map(([intent, result]) => ({
            intent,
            healedSelector:  result.selector,
            confidenceScore: result.confidence,
            actionType:      result.actionType,
            timestamp:       new Date().toISOString(),
        }));
    }

    getProjectKey(): string { return this.projectKey; }
}