# Self-Healing Log — SCRUM-70
**Generated:** 2026-04-27T15:33:24.974231  

## Summary
| Metric | Count |
|--------|-------|
| Auto-healed | 2 |
| Manual review needed | 1 |
| Unchanged | 2 |

## Healed Selectors

| Old | New | Strategy | Confidence |
|-----|-----|----------|------------|
| `xpath=/html/body/div/header/a` | `#addNewRecordButton` | 🏷 LABEL_MATCH | 0.7 |
| `xpath=/html/body/div/header/a` | `#addNewRecordButton` | 🏷 LABEL_MATCH | 0.7 |
| `#output` | `/* TODO: manually map '#output' — not fo` | ⚠️ TODO_PLACEHOLDER | 0.0 |

## ⚠️ Needs Manual Review

These selectors could not be automatically healed:

- `#output` — test: **