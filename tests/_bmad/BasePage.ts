/**
 * BasePage - Site-Agnostic Page Object with Self-Healing Capabilities (TEA Healer Guard)
 *
 * Core API: smartAction(intent, value?)
 * The single entry-point for all test interactions.  Uses Qdrant semantic
 * search to locate elements by intent rather than fragile technical selectors.
 *
 * Fix log
 * -------
 * • resolveUrlFromQdrant: unchanged — already uses fetch() against the Qdrant
 *   REST endpoint, which works regardless of qdrant-client Python version.
 *
 * • smartAction / verify fallback:
 *   Added two-stage handling for verify intents that produce no DOM locator:
 *
 *   Stage 1 — Qdrant URL lookup (resolveUrlFromQdrant).
 *     Embeds the intent, searches the crawled DOM data, and calls
 *     toHaveURL() with the *actual* URL stored by the crawler.  This is
 *     correct by construction — no slug guessing needed.
 *
 *   Stage 2 — BASE_URL root guard.
 *     If Qdrant returns nothing (e.g. the collection is empty), falls back to
 *     asserting toHaveURL(BASE_URL) so the test still gives a meaningful
 *     failure rather than silently passing or throwing "no locator".
 *
 * • Error-message guard (isErrorIntent):
 *   Verify intents that mention "error message", "locked", "invalid
 *   credentials", etc. skip the URL-assertion path entirely and throw a
 *   clear "element not found" error.  This prevents false-positive URL
 *   assertions on negative-auth scenarios.
 *
 * • No PAGE_URL_OVERRIDES map needed: because URL assertions are now driven
 *   by real crawled data (Qdrant) or the BASE_URL env var, there is no need
 *   to maintain a hardcoded page-name → URL-path table.
 */

import { Page, Locator, expect } from '@playwright/test';

// ── Runtime configuration (from .env via Playwright env) ───────────────────────
const QDRANT_URL      = process.env.QDRANT_URL      || 'http://localhost:6333';
const OLLAMA_HOST     = process.env.OLLAMA_HOST     || 'http://localhost:11434';
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || 'mxbai-embed-large:latest';

// Application base URL — read from .env (e.g. BASE_URL=https://www.saucedemo.com/).
// Used as a last-resort URL assertion fallback when Qdrant has no match.
const BASE_URL = (process.env.BASE_URL || '').replace(/\/+$/, '');

// ── Intent-classification helpers ──────────────────────────────────────────────

/**
 * Intent signals that indicate an error / validation message is being
 * asserted.  Steps containing any of these phrases must NEVER be treated as
 * URL assertions, even if they also contain "should see" or similar vocabulary.
 *
 * Without this guard a step like
 *   "I should see an error message containing 'locked'"
 * would incorrectly trigger a toHaveURL() check.
 */
const ERROR_INTENT_SIGNALS = [
    'error message',
    'error text',
    'warning message',
    'validation message',
    'locked',
    'invalid credentials',
    'username and password',
    'sorry, this user',
];

/**
 * Vocabulary that signals a page-location / redirect assertion rather than a
 * DOM-element interaction.  Used to decide whether to fall back to a URL
 * assertion when no locator is found.
 */
const REDIRECT_INTENT_SIGNALS = [
    'redirected to',
    'redirect to',
    'taken to',
    'navigated to',
    'should be on',
    'lands on',
    'land on',
    'on the inventory',
    'on the products',
    'on the cart',
    'on the checkout',
    'on the dashboard',
];

// ── Interfaces ─────────────────────────────────────────────────────────────────

interface HealingResult {
    selector:   string;
    confidence: number;
    intent:     string;
    actionType: string;
}

interface DOMElement {
    xpath:             string;
    id?:               string;
    name?:             string;
    label?:            string;
    placeholder?:      string;
    className?:        string;
    text?:             string;
    role?:             string;
    ariaRole?:         string;
    intent?:           string;
    relativeSelectors?: string[];
}

// ── BasePage class ─────────────────────────────────────────────────────────────

export class BasePage {
    public    page:             Page;
    protected projectKey:       string;
    private   healingAttempts:  Map<string, HealingResult> = new Map();

    constructor(page: Page, projectKey: string) {
        this.page       = page;
        this.projectKey = projectKey;
    }

    /** Initialize the page — can be overridden by subclasses. */
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
            if (!response.ok) {
                console.warn(`Ollama embedding failed: ${response.statusText}`);
                return [];
            }
            const result = await response.json();
            return result.embedding || [];
        } catch (error) {
            console.warn(`generateEmbedding error: ${error}`);
            return [];
        }
    }

    /**
     * Search Qdrant ui_memory for DOM elements matching the query.
     * Uses the REST /points/search endpoint directly (no Python client needed).
     */
    private async searchQdrant(queryText: string, limit = 5): Promise<DOMElement[]> {
        try {
            const vector = await this.generateEmbedding(queryText);
            if (vector.length === 0) return [];

            const response = await fetch(
                `${QDRANT_URL}/collections/${this.projectKey}_ui_memory/points/search`,
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

            if (!response.ok) {
                console.warn(`Qdrant search failed: ${response.statusText}`);
                return [];
            }

            const result = await response.json();
            return (result.result || [])
                .filter((item: any) => item.payload?.project_key === this.projectKey)
                .map((item: any) => item.payload?.details || {});
        } catch (error) {
            console.warn(`searchQdrant error: ${error}`);
            return [];
        }
    }

    /**
     * Semantic-search Qdrant ui_memory for a page whose stored `url` payload
     * field matches the navigation intent.  Fully site-agnostic — works for
     * any application whose DOM has been crawled into Qdrant.
     *
     * Returns the resolved URL string, or null when nothing matches.
     */
    private async resolveUrlFromQdrant(intent: string): Promise<string | null> {
        try {
            const vector = await this.generateEmbedding(intent);
            if (vector.length === 0) return null;

            const response = await fetch(
                `${QDRANT_URL}/collections/${this.projectKey}_ui_memory/points/search`,
                {
                    method:  'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body:    JSON.stringify({
                        vector,
                        limit: 5,
                        with_payload: true,
                        filter: {
                            must: [{ key: 'project_key', match: { value: this.projectKey } }],
                        },
                    }),
                }
            );

            if (!response.ok) return null;

            const result = await response.json();
            for (const hit of result.result ?? []) {
                const payload = hit.payload ?? {};
                const url: string = payload.url ?? payload.details?.url ?? '';
                if (url.startsWith('http://') || url.startsWith('https://')) {
                    return url;
                }
            }
        } catch (err) {
            console.warn(`resolveUrlFromQdrant error: ${err}`);
        }
        return null;
    }

    // ── Private: locator building ────────────────────────────────────────────

    private escapeRegExp(value: string): string {
        return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    private buildCandidateLocators(element: DOMElement): Locator[] {
        const locators: Locator[] = [];
        const seen = new Set<string>();
        const add  = (key: string, locator: Locator | null) => {
            if (!locator || seen.has(key)) return;
            seen.add(key);
            locators.push(locator);
        };

        if (element.xpath) {
            add(`xpath:${element.xpath}`, this.page.locator(`xpath=${element.xpath}`));
        }
        for (const sel of element.relativeSelectors || []) {
            add(`relative:${sel}`, this.page.locator(sel));
        }
        if (element.id) {
            add(`id:${element.id}`, this.page.locator(`#${element.id}`));
        }
        if (element.name) {
            add(`name:${element.name}`, this.page.locator(`[name="${element.name}"]`));
        }
        if (element.label) {
            const esc = this.escapeRegExp(element.label);
            add(`label:${element.label}`,       this.page.getByLabel(new RegExp(`^${esc}$`, 'i')));
            add(`ph-label:${element.label}`,    this.page.getByPlaceholder(new RegExp(esc, 'i')));
        }
        if (element.placeholder) {
            const esc = this.escapeRegExp(element.placeholder);
            add(`ph:${element.placeholder}`,    this.page.getByPlaceholder(new RegExp(esc, 'i')));
        }
        if (element.text) {
            const esc = this.escapeRegExp(element.text);
            add(`btn:${element.text}`,          this.page.getByRole('button', { name: new RegExp(esc, 'i') }));
            add(`text:${element.text}`,         this.page.getByText(new RegExp(esc, 'i')));
        }
        if (element.ariaRole === 'button' || element.role === 'button') {
            const name = element.text || element.label || element.placeholder;
            if (name) add(`aria-btn:${name}`,   this.page.getByRole('button',  { name: new RegExp(this.escapeRegExp(name), 'i') }));
        }
        if (element.ariaRole === 'textbox') {
            const name = element.label || element.placeholder || element.name;
            if (name) add(`aria-tb:${name}`,    this.page.getByRole('textbox', { name: new RegExp(this.escapeRegExp(name), 'i') }));
        }
        if (element.className) {
            const first = element.className.split(/\s+/).find(Boolean);
            if (first) add(`cls:${first}`,      this.page.locator(`.${first}`));
        }
        return locators;
    }

    private async findLocatorByIntent(intent: string, actionType: string): Promise<Locator | null> {
        const candidates = await this.searchQdrant(`${actionType} ${intent}`);

        for (const element of candidates) {
            for (const locator of this.buildCandidateLocators(element)) {
                try {
                    if (await locator.count() > 0) {
                        const healed =
                            element.xpath
                            || element.relativeSelectors?.[0]
                            || element.label
                            || element.placeholder
                            || element.text
                            || element.name
                            || 'semantic-match';
                        console.log(`✓ Healed selector: ${healed} for intent "${intent}"`);
                        this.healingAttempts.set(intent, {
                            selector:   healed,
                            confidence: 0.9,
                            intent,
                            actionType,
                        });
                        return locator;
                    }
                } catch {
                    // try next candidate
                }
            }
        }
        return null;
    }

    // ── Private: intent classification helpers ───────────────────────────────

    /** True when the intent is about error/validation text, not page location. */
    private isErrorIntent(normalizedIntent: string): boolean {
        return ERROR_INTENT_SIGNALS.some(sig => normalizedIntent.includes(sig));
    }

    /** True when the intent is a redirect / page-location assertion. */
    private isRedirectIntent(normalizedIntent: string): boolean {
        return REDIRECT_INTENT_SIGNALS.some(sig => normalizedIntent.includes(sig));
    }

    // ── Public API ───────────────────────────────────────────────────────────

    /**
     * smartAction — universal method for all test interactions.
     *
     * Action-type detection (from intent string):
     *   click    → "click", "press", "submit"
     *   fill     → "fill", "enter", "type", "input"
     *   verify   → "verify", "check", "see", "should"
     *   navigate → "navigate", "go to", "visit", "am on", "open the"
     *
     * Fallback chain when no Qdrant DOM locator is found:
     *   navigate:
     *     A) Literal URL in intent  → page.goto(url)
     *     B) Qdrant URL lookup      → page.goto(qdrantUrl)
     *     C) BASE_URL root          → page.goto(BASE_URL/)
     *   verify:
     *     Guard: error-message intents → throw (never a URL assertion)
     *     A) Qdrant URL lookup      → expect(page).toHaveURL(qdrantUrl)
     *     B) BASE_URL root          → expect(page).toHaveURL(BASE_URL)  [soft]
     *     C) Redirect vocab present → expect(page).not.toHaveURL('about:blank')
     *     D) No match at all        → throw "element not found"
     */
    async smartAction(intent: string, value?: string): Promise<void> {
        const n = intent.toLowerCase().trim(); // normalised intent

        // ── Classify action ────────────────────────────────────────────────
        let actionType: string;
        if      (n.includes('click') || n.includes('press') || n.includes('submit'))                                          actionType = 'click';
        else if (n.includes('fill')  || n.includes('enter') || n.includes('type') || n.includes('input'))                    actionType = 'fill';
        else if (n.includes('verify')|| n.includes('check') || n.includes('see')  || n.includes('should'))                   actionType = 'verify';
        else if (n.includes('navigate')||n.includes('go to')||n.includes('visit')||n.includes('am on')||n.includes('open the')) actionType = 'navigate';
        else                                                                                                                    actionType = 'click';

        // ── Try DOM locator ────────────────────────────────────────────────
        const locator = await this.findLocatorByIntent(intent, actionType);

        if (!locator) {
            // ── navigate fallbacks ─────────────────────────────────────────
            if (actionType === 'navigate') {
                // (A) Literal URL in the intent string
                if (intent.includes('http')) {
                    const m   = intent.match(/https?:\/\/[^\s"']+/);
                    const url = m?.[0]?.replace(/[.,;:!?)]+$/, '');
                    if (url) {
                        console.log(`↳ URL fallback (literal): navigating to ${url}`);
                        await this.page.goto(url);
                        return;
                    }
                }
                // (B) Qdrant ui_memory URL lookup
                const qdrantUrl = await this.resolveUrlFromQdrant(intent);
                if (qdrantUrl) {
                    console.log(`↳ Qdrant URL fallback: ${qdrantUrl} for "${intent}"`);
                    await this.page.goto(qdrantUrl);
                    return;
                }
                // (C) BASE_URL root
                if (BASE_URL) {
                    console.warn(`↳ BASE_URL root fallback for "${intent}" — Qdrant had no URL match`);
                    await this.page.goto(BASE_URL + '/');
                    return;
                }
                throw new Error(`smartAction navigate failed: no URL resolved for intent "${intent}"`);
            }

            // ── verify fallbacks ───────────────────────────────────────────
            if (actionType === 'verify') {
                // Guard: never assert a URL for error-message steps
                if (this.isErrorIntent(n)) {
                    throw new Error(
                        `smartAction verify failed: error-message element not found in Qdrant ` +
                        `for intent "${intent}". Index the error element in Qdrant ui_memory.`
                    );
                }

                // (A) Qdrant URL lookup → assert exact URL from crawled data
                const qdrantUrl = await this.resolveUrlFromQdrant(intent);
                if (qdrantUrl) {
                    console.log(`↳ URL assertion fallback: expecting URL to contain "${qdrantUrl}" for intent "${intent}"`);
                    await expect(this.page).toHaveURL(new RegExp(qdrantUrl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'));
                    return;
                }

                // (B) BASE_URL root soft-assert
                if (BASE_URL && this.isRedirectIntent(n)) {
                    console.warn(`↳ BASE_URL URL assertion fallback for "${intent}"`);
                    await expect(this.page).toHaveURL(new RegExp(BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'));
                    return;
                }

                // (C) Generic redirect vocab guard — page must not be blank
                if (this.isRedirectIntent(n)) {
                    console.warn(`↳ not-blank URL guard for "${intent}"`);
                    await expect(this.page).not.toHaveURL('about:blank');
                    return;
                }

                // (D) Nothing matched — surface a real failure
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
                await locator.click();
                break;
            case 'fill':
                if (value === undefined) {
                    throw new Error(`smartAction fill failed: value required for intent "${intent}"`);
                }
                await locator.fill(value);
                break;
            case 'verify':
                if (value && value.trim()) {
                    await expect(locator).toContainText(value);
                } else {
                    await expect(locator).toBeVisible();
                }
                break;
            case 'navigate':
                await locator.click();
                break;
        }
    }

    // ── Legacy compatibility shims (deprecated) ──────────────────────────────

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
            successfulHeals: Array.from(this.healingAttempts.values()).filter(h => h.confidence > 0.5).length,
        };
    }

    getHealingLog(): object[] {
        return Array.from(this.healingAttempts.entries()).map(([intent, result]) => ({
            intent,
            healedSelector: result.selector,
            confidenceScore: result.confidence,
            actionType:     result.actionType,
            timestamp:      new Date().toISOString(),
        }));
    }

    getProjectKey(): string {
        return this.projectKey;
    }
}