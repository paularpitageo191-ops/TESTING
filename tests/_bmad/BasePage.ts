import { Page, Locator, expect } from '@playwright/test';

// ── Config ───────────────────────────────────────────────────
const QDRANT_URL      = process.env.QDRANT_URL      || 'http://localhost:6333';
const OLLAMA_HOST     = process.env.OLLAMA_HOST     || 'http://localhost:11434';
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || 'mxbai-embed-large:latest';

// ── Types ────────────────────────────────────────────────────
interface HealingResult {
    selector: string;
    confidence: number;
    intent: string;
    actionType: string;
}

// ── BasePage ─────────────────────────────────────────────────
export class BasePage {

    private page: Page;
    private projectKey: string;
    private healingAttempts: Map<string, HealingResult> = new Map();

    constructor(page: Page, projectKey: string) {
        this.page = page;
        this.projectKey = projectKey;
    }

    // ─────────────────────────────────────────────────────────
    // 🔒 SELECTOR VALIDATION (CRITICAL)
    // ─────────────────────────────────────────────────────────
    private isValidSelector(selector: string): boolean {
        if (!selector) return false;
        if (selector.includes("TODO") || selector.includes("PLACEHOLDER")) return false;
        if (selector.includes("/*") || selector.includes("*/")) return false;
        return true;
    }

    // ─────────────────────────────────────────────────────────
    // 🧠 EMBEDDING
    // ─────────────────────────────────────────────────────────
    private async generateEmbedding(text: string): Promise<number[]> {
        try {
            const res = await fetch(`${OLLAMA_HOST}/api/embeddings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: EMBEDDING_MODEL, prompt: text })
            });
            const data = await res.json();
            return data.embedding || [];
        } catch {
            return [];
        }
    }

    // ─────────────────────────────────────────────────────────
    // 🧠 QDRANT SEARCH (PREFER SUCCESS)
    // ─────────────────────────────────────────────────────────
    private async searchQdrant(intent: string): Promise<any[]> {
        const vector = await this.generateEmbedding(intent);
        if (!vector.length) return [];

        const res = await fetch(
            `${QDRANT_URL}/collections/${this.projectKey.replace(/-/g, '_')}_ui_memory/points/search`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    vector,
                    limit: 5,
                    with_payload: true
                })
            }
        );

        const data = await res.json();

        return (data.result || [])
            .sort((a: any, b: any) =>
                (b.payload?.success || 0) - (a.payload?.success || 0)
            )
            .map((x: any) => x.payload || {});
    }

    // ─────────────────────────────────────────────────────────
    // 🧠 STORE SUCCESS (FEEDBACK LOOP)
    // ─────────────────────────────────────────────────────────
    private async recordSuccess(intent: string, selector: string, action: string) {
        try {
            if (!this.isValidSelector(selector)) return;

            const vector = await this.generateEmbedding(intent);
            if (!vector.length) return;

            await fetch(
                `${QDRANT_URL}/collections/${this.projectKey.replace(/-/g, '_')}_ui_memory/points`,
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        points: [{
                            id: crypto.randomUUID(),
                            vector,
                            payload: {
                                project_key: this.projectKey,
                                intent,
                                selector,
                                action,
                                success: 1,
                                ts: new Date().toISOString()
                            }
                        }]
                    })
                }
            );

            console.log(`🧠 Learned selector: ${selector}`);
        } catch {
            // silent
        }
    }

    // ─────────────────────────────────────────────────────────
    // 🔍 FIND LOCATOR (SAFE HEALING)
    // ─────────────────────────────────────────────────────────
    private async findLocator(intent: string): Promise<Locator | null> {

        const results = await this.searchQdrant(intent);

        for (const r of results) {
            const selector = r.selector;

            if (!this.isValidSelector(selector)) {
                console.warn(`⚠ Invalid selector skipped: ${selector}`);
                continue;
            }

            const locator = this.page.locator(selector);

            try {
                const count = await locator.count();

                if (count === 1) {
                    console.log(`✓ Healed selector → ${selector}`);
                    this.healingAttempts.set(intent, {
                        selector,
                        confidence: 0.9,
                        intent,
                        actionType: 'auto'
                    });
                    return locator;
                }

                if (count > 1) {
                    console.warn(`⚠ Multiple match → using first: ${selector}`);
                    return locator.first();
                }

            } catch {
                continue;
            }
        }

        return null;
    }

    // ─────────────────────────────────────────────────────────
    // 🚀 MAIN ACTION ENGINE
    // ─────────────────────────────────────────────────────────
    async smartAction(intent: string, value?: string) {

        const normalized = intent.toLowerCase();

        let locator = await this.findLocator(intent);

        if (!locator) {
            throw new Error(`❌ No locator found for: ${intent}`);
        }

        // ─────────────────────────────────────────────────────
        // CLICK
        // ─────────────────────────────────────────────────────
        if (normalized.includes("click")) {

            try {
                await locator.click({ trial: true });
            } catch (e: any) {
                if (e.message?.includes("intercepts pointer events")) {
                    console.warn("⚠ Modal blocking → retrying");
                    await this.page.waitForTimeout(1000);
                }
            }

            await locator.click();

            const heal = this.healingAttempts.get(intent);
            await this.recordSuccess(intent, heal?.selector || intent, 'click');
        }

        // ─────────────────────────────────────────────────────
        // FILL
        // ─────────────────────────────────────────────────────
        else if (normalized.includes("enter") || normalized.includes("fill")) {

            await locator.fill(value || "");

            const heal = this.healingAttempts.get(intent);
            await this.recordSuccess(intent, heal?.selector || intent, 'fill');
        }

        // ─────────────────────────────────────────────────────
        // VERIFY
        // ─────────────────────────────────────────────────────
        else if (normalized.includes("verify") || normalized.includes("see")) {

            await expect(locator).toBeVisible();

            const heal = this.healingAttempts.get(intent);
            await this.recordSuccess(intent, heal?.selector || intent, 'verify');
        }
    }
}
