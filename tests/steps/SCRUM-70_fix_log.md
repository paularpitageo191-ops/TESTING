# Spec Fixer Log — SCRUM-70
**Generated:** 2026-04-27 13:43:32
**Spec:** tests/steps/SCRUM-70.spec.ts
**Total fixes applied:** 7

## Root Causes (generalised — site-agnostic)

| Code | Root Cause | Fix Applied |
|------|-----------|-------------|
| RC-1  | Wrong fill selector (Qdrant ambiguous match) | Remapped via FIELD_KEYWORD_MAP + DOM index |
| RC-1b | Wrong assertion selector (input instead of output) | Remapped to output element from DOM index |
| RC-2  | Positional XPath (li[N]/a matches multiple) | Replaced with stable id/aria/role selector |
| RC-3  | smartAction fill with no value | Value deduced from intent + test name context |
| RC-4  | toBeVisible() on absent/negation intent | Flipped to not.toBeVisible() |
| RC-4b | smartAction with verifyAbsent comment | Converted to direct not.toBeVisible() |
| RC-5  | toBeDisabled() on always-enabled element | Changed to toBeEnabled() with warning |
| RC-6  | toContainText on input field instead of output | Moved assertion to output element |

## Fixes Applied

1. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
2. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
3. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
4. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
5. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
6. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)
7. RC-4 Polarity flip: toBeVisible → not.toBeVisible (absent intent detected)