import { expect, Locator, Page } from '@playwright/test';
import fs from 'fs';

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

type ActionType = 'click' | 'fill' | 'verify' | 'verifyDisabled' | 'verifyAbsent';

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

        if (/(should be disabled|is disabled|remain[s]? disabled|not interactable)/i.test(normalized)) {
            return 'verifyDisabled';
        }
        if (/(not visible|not be visible|not displayed|not shown|not present|should be hidden)/i.test(normalized)) {
            return 'verifyAbsent';
        }
        if (normalized.includes('enter') || normalized.includes('fill') || normalized.includes('type')) {
            return 'fill';
        }
        if (normalized.includes('click') || normalized.includes('select') || normalized.includes('press') || normalized.includes('tap')) {
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
                const type = (element as HTMLInputElement).type?.toLowerCase() || '';
                const contentEditable = (element as HTMLElement).isContentEditable;
                return { tag, type, contentEditable };
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

    private extractCallerFrame(): { file: string; line: number } | null {
        const stack = new Error().stack || '';
        const lines = stack.split('\n');

        for (const line of lines) {
            if (!line.includes('.spec.ts:')) {
                continue;
            }
            const match = line.match(/\(?(.+\.spec\.ts):(\d+):(\d+)\)?/);
            if (match) {
                return { file: match[1], line: Number(match[2]) };
            }
        }

        return null;
    }

    private extractSelectorHintsFromLine(sourceLine: string): string[] {
        const hints: string[] = [];

        const blockMatches = Array.from(sourceLine.matchAll(/\/\*\s*selector:\s*([\s\S]*?)\*\//g));
        for (const match of blockMatches) {
            const raw = match[1].trim();
            if (this.isValidSelector(raw)) {
                hints.push(raw);
            }

            const nested = raw.match(/\bselector:\s*([^.;]+(?:#[\w-]+|text=[^.;]+|role=[^.;]+)[^.;]*)/i);
            if (nested) {
                const candidate = nested[1].trim();
                if (this.isValidSelector(candidate)) {
                    hints.push(candidate);
                }
            }
        }

        const inlineSelector = sourceLine.match(/["'](#[\w-]+|text=[^"']+|role=[^"']+)["']/);
        if (inlineSelector && this.isValidSelector(inlineSelector[1])) {
            hints.push(inlineSelector[1]);
        }

        return [...new Set(hints)];
    }

    private getCallsiteSelectorHints(): string[] {
        try {
            const frame = this.extractCallerFrame();
            if (!frame || !fs.existsSync(frame.file)) {
                return [];
            }

            const fileContent = fs.readFileSync(frame.file, 'utf-8').split('\n');
            const sourceLine = fileContent[frame.line - 1] || '';
            return this.extractSelectorHintsFromLine(sourceLine);
        } catch {
            return [];
        }
    }

    private extractIntentSelectors(intent: string): string[] {
        const matches = intent.match(/#[A-Za-z0-9_-]+/g) || [];
        return [...new Set(matches.filter((selector) => this.isValidSelector(selector)))];
    }

    private buildHeuristicLocators(intent: string, actionType: ActionType): Locator[] {
        const normalized = this.normalizeIntent(intent);
        const locators: Locator[] = [];
        const seen = new Set<string>();

        const addSelector = (selector: string) => {
            if (!this.isValidSelector(selector) || seen.has(selector)) {
                return;
            }
            seen.add(selector);
            locators.push(this.page.locator(selector));
        };

        const callsiteHints = this.getCallsiteSelectorHints();
        for (const hint of callsiteHints) {
            addSelector(hint);
        }

        const explicitSelectors = this.extractIntentSelectors(intent);
        for (const selector of explicitSelectors) {
            addSelector(selector);
        }

        if (/\bsubmit button\b/.test(normalized)) {
            locators.push(this.page.getByRole('button', { name: /submit/i }));
            addSelector('#submit');
        }

        if (normalized.includes('radio button')) {
            locators.push(this.page.getByRole('radio'));
        }

        if (normalized.includes('modal')) {
            locators.push(this.page.getByRole('dialog'));
        }

        if (/(output container|result should display|submitted data|invalid age|#output|output)/.test(normalized)) {
            addSelector('#output');
        }

        if (/(page should be accessible|selectors are valid|ui remains stable|remain interactable)/.test(normalized)) {
            locators.push(this.page.locator('body'));
        }

        if (actionType === 'verifyDisabled' && /\bsubmit\b/.test(normalized)) {
            addSelector('#submit');
        }

        return locators;
    }

    private async findUsableLocator(
        candidates: Locator[],
        actionType: ActionType,
        intent: string,
        recordFailures: boolean,
    ): Promise<Locator | null> {
        for (const locator of candidates) {
            try {
                const count = await locator.count();
                if (count < 1) {
                    continue;
                }

                const usable = count === 1 ? locator : locator.first();
                const compatible = await this.validateActionCompatibility(usable, actionType);
                if (!compatible) {
                    if (recordFailures) {
                        const desc = await locator.first().evaluate((element) => {
                            if (element.id) return `#${element.id}`;
                            return element.tagName.toLowerCase();
                        }).catch(() => '');
                        if (desc && this.isValidSelector(desc)) {
                            await this.recordHealingOutcome(intent, desc, actionType, 0);
                        }
                    }
                    continue;
                }

                return usable;
            } catch {
                continue;
            }
        }

        return null;
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

        const qdrantCandidates: Locator[] = [];

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

            qdrantCandidates.push(this.page.locator(selector));
        }

        const healedLocator = await this.findUsableLocator(qdrantCandidates, actionType, intent, true);
        if (healedLocator) {
            const selector = await healedLocator.evaluate((element) => {
                if (element.id) return `#${element.id}`;
                return element.tagName.toLowerCase();
            });
            console.log(`✓ Healed selector → ${selector}`);
            this.healingAttempts.set(learningKey, {
                selector,
                confidence: 0.9,
                intent,
                actionType,
            });
            return healedLocator;
        }

        return this.findUsableLocator(this.buildHeuristicLocators(intent, actionType), actionType, intent, false);
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
            switch (actionType) {
                case 'click':
                    await locator.click({ trial: true });
                    await locator.click();
                    break;
                case 'fill': {
                    const compatible = await this.validateActionCompatibility(locator, actionType);
                    if (!compatible) {
                        if (heal) {
                            await this.recordHealingOutcome(intent, heal.selector, actionType, 0);
                        }
                        throw new Error(`❌ Healed selector is not fill-compatible for: ${intent}`);
                    }
                    await locator.fill(value || '');
                    break;
                }
                case 'verifyDisabled':
                    await expect(locator).toBeDisabled();
                    break;
                case 'verifyAbsent':
                    await expect(locator).not.toBeVisible();
                    break;
                default:
                    await expect(locator).toBeVisible();
                    break;
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
