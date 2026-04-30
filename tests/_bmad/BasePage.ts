import { expect, Locator, Page } from '@playwright/test';

const QDRANT_URL = process.env.QDRANT_URL || 'http://localhost:6333';
const OLLAMA_HOST = process.env.OLLAMA_HOST || 'http://localhost:11434';
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || 'mxbai-embed-large:latest';

const VAGUE_INTENT_PATTERNS = [
    /\bi interact with ui elements\b/i,
    /\bi interact with\b$/i,
    /\bdo something\b/i,
    /\bperform action\b/i,
    /\bhandle element\b/i,
    /\bui elements\b$/i,
];

type ActionType = 'click' | 'fill' | 'verify';

interface HealingResult {
    selector: string;
    confidence: number;
    intent: string;
    actionType: ActionType;
}

interface MemoryPayload {
    selector?: string;
    success?: number;
    action?: string;
    actionType?: string;
    learning_key?: string;
}

export class BasePage {
    readonly page: Page;
    private readonly projectKey: string;
    private readonly healingAttempts: Map<string, HealingResult> = new Map();

    constructor(page: Page, projectKey: string) {
        this.page = page;
        this.projectKey = projectKey;
    }

    async initialize(): Promise<void> {
        try {
            await this.page.waitForLoadState('domcontentloaded');
            await this.page.waitForLoadState('networkidle');
            console.log('✓ BasePage initialized');
        } catch {
            console.warn('⚠ Initialization skipped');
        }
    }

    private normalizeIntent(intent: string): string {
        return intent.trim().replace(/\s+/g, ' ').toLowerCase();
    }

    private detectActionType(intent: string): ActionType {
        const normalized = this.normalizeIntent(intent);
        if (normalized.includes('enter') || normalized.includes('fill') || normalized.includes('type')) {
            return 'fill';
        }
        if (normalized.includes('click') || normalized.includes('submit') || normalized.includes('press')) {
            return 'click';
        }
        return 'verify';
    }

    private buildLearningKey(intent: string, actionType: ActionType): string {
        return `${this.normalizeIntent(intent)}::${actionType}`;
    }

    private isVagueIntent(intent: string): boolean {
        const normalized = this.normalizeIntent(intent);
        if (!normalized) {
            return true;
        }
        if (normalized.split(' ').length < 3) {
            return true;
        }
        return VAGUE_INTENT_PATTERNS.some((pattern) => pattern.test(normalized));
    }

    private isValidSelector(selector: string, confidence = 1): boolean {
        const candidate = selector?.trim();
        if (!candidate) return false;
        if (confidence <= 0) return false;
        if (candidate.length < 2) return false;
        if (candidate.includes('TODO') || candidate.includes('PLACEHOLDER')) return false;
        if (candidate.includes('/*') || candidate.includes('*/')) return false;
        return true;
    }

    private async generateEmbedding(text: string): Promise<number[]> {
        try {
            const res = await fetch(`${OLLAMA_HOST}/api/embeddings`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: EMBEDDING_MODEL, prompt: text }),
            });
            const data = await res.json();
            return data.embedding || [];
        } catch {
            return [];
        }
    }

    private async searchQdrant(learningKey: string): Promise<MemoryPayload[]> {
        try {
            const vector = await this.generateEmbedding(learningKey);
            if (!vector.length) return [];

            const res = await fetch(
                `${QDRANT_URL}/collections/${this.projectKey.replace(/-/g, '_')}_ui_memory/points/search`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        vector,
                        limit: 10,
                        with_payload: true,
                    }),
                },
            );

            const data = await res.json();
            return (data.result || []).map((entry: any) => entry.payload || {});
        } catch {
            return [];
        }
    }

    private async recordHealingOutcome(
        intent: string,
        selector: string,
        actionType: ActionType,
        success: 0 | 1,
    ): Promise<void> {
        if (!this.isValidSelector(selector)) {
            return;
        }

        const learningKey = this.buildLearningKey(intent, actionType);
        const vector = await this.generateEmbedding(learningKey);
        if (!vector.length) {
            return;
        }

        try {
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
                                intent: this.normalizeIntent(intent),
                                selector,
                                action: actionType,
                                actionType,
                                learning_key: learningKey,
                                success,
                                ts: new Date().toISOString(),
                            },
                        }],
                    }),
                },
            );

            console.log(success === 1 ? `🧠 Learned selector: ${selector}` : `🧠 Learned failed selector: ${selector}`);
        } catch {
        }
    }

    private async validateActionCompatibility(locator: Locator, actionType: ActionType): Promise<boolean> {
        try {
            const meta = await locator.first().evaluate((element) => {
                const tag = element.tagName.toLowerCase();
                const role = element.getAttribute('role')?.toLowerCase() || '';
                const type = (element as HTMLInputElement).type?.toLowerCase() || '';
                const contentEditable = (element as HTMLElement).isContentEditable;
                return { tag, role, type, contentEditable };
            });

            if (actionType === 'fill') {
                return (
                    meta.contentEditable ||
                    meta.tag === 'textarea' ||
                    meta.tag === 'select' ||
                    (meta.tag === 'input' && meta.type !== 'button' && meta.type !== 'submit')
                );
            }

            return true;
        } catch {
            return false;
        }
    }

    private async findLocator(intent: string, actionType: ActionType): Promise<Locator | null> {
        const learningKey = this.buildLearningKey(intent, actionType);
        const results = await this.searchQdrant(learningKey);
        const successfulSelectors = new Set(
            results
                .filter((result) => result.learning_key === learningKey && result.success === 1 && result.selector)
                .map((result) => result.selector as string),
        );
        const failedSelectors = new Set(
            results
                .filter(
                    (result) =>
                        result.learning_key === learningKey &&
                        result.success === 0 &&
                        result.selector &&
                        !successfulSelectors.has(result.selector),
                )
                .map((result) => result.selector as string),
        );

        for (const result of results) {
            const selector = result.selector?.trim();
            const resultAction = result.actionType || result.action;
            const success = Number(result.success || 0);

            if (!selector || result.learning_key !== learningKey) {
                continue;
            }

            if (resultAction !== actionType) {
                continue;
            }

            if (success !== 1) {
                continue;
            }

            if (failedSelectors.has(selector)) {
                console.warn(`⚠ Known failed selector skipped: ${selector}`);
                continue;
            }

            if (!this.isValidSelector(selector, 1)) {
                console.warn(`⚠ Invalid selector skipped: ${selector}`);
                continue;
            }

            const locator = this.page.locator(selector);

            try {
                const count = await locator.count();
                if (count !== 1) {
                    await this.recordHealingOutcome(intent, selector, actionType, 0);
                    continue;
                }

                const compatible = await this.validateActionCompatibility(locator, actionType);
                if (!compatible) {
                    console.warn(`⚠ Incompatible healed selector skipped: ${selector} for ${actionType}`);
                    await this.recordHealingOutcome(intent, selector, actionType, 0);
                    continue;
                }

                console.log(`✓ Healed selector → ${selector}`);
                this.healingAttempts.set(learningKey, {
                    selector,
                    confidence: 0.9,
                    intent,
                    actionType,
                });
                return locator;
            } catch {
                await this.recordHealingOutcome(intent, selector, actionType, 0);
            }
        }

        return null;
    }

    async smartAction(intent: string, value?: string): Promise<void> {
        const actionType = this.detectActionType(intent);
        const learningKey = this.buildLearningKey(intent, actionType);

        if (this.isVagueIntent(intent)) {
            throw new Error(`❌ invalid_intent: ${intent}`);
        }

        const locator = await this.findLocator(intent, actionType);
        if (!locator) {
            throw new Error(`❌ No locator found for: ${intent}`);
        }

        const heal = this.healingAttempts.get(learningKey);

        try {
            if (actionType === 'click') {
                await locator.click({ trial: true });
                await locator.click();
            } else if (actionType === 'fill') {
                const compatible = await this.validateActionCompatibility(locator, actionType);
                if (!compatible) {
                    if (heal) {
                        await this.recordHealingOutcome(intent, heal.selector, actionType, 0);
                    }
                    throw new Error(`❌ Healed selector is not fill-compatible for: ${intent}`);
                }
                await locator.fill(value || '');
            } else {
                await expect(locator).toBeVisible();
            }

            if (heal) {
                await this.recordHealingOutcome(intent, heal.selector, actionType, 1);
            }
        } catch (error) {
            if (heal) {
                await this.recordHealingOutcome(intent, heal.selector, actionType, 0);
            }
            throw error;
        }
    }
}
