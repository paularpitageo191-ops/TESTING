# Risk-Based Test Plan — SCRUM-70
**Generated:** 2026-04-27T15:30:34.564286  
**Time budget:** 60 min  

## Executive Summary

| Metric | Value |
|--------|-------|
| Total scenarios | 16 |
| 🔴 High risk | 0 |
| 🟡 Medium risk | 0 |
| 🟢 Low risk | 16 |
| Within budget | 16 |
| Deferred | 0 |
| Estimated total time | 48.1 min |

## Prioritised Execution Order

| # | Risk | Score | Title | Est. Time | Factors |
|---|------|-------|-------|-----------|---------|
| 1 | 🟢 LOW | 19.23 | Email Validation | 3.2 min | dom confidence=9.23 | business criticality=10.0 |
| 2 | 🟢 LOW | 19.23 | Web Tables Validation | 3.2 min | dom confidence=9.23 | business criticality=10.0 |
| 3 | 🟢 LOW | 19.23 | Radio Button Validation | 3.0 min | dom confidence=9.23 | business criticality=10.0 |
| 4 | 🟢 LOW | 19.23 | UI Stability | 2.8 min | dom confidence=9.23 | business criticality=10.0 |
| 5 | 🟢 LOW | 19.23 | Invalid email behavior | 2.8 min | dom confidence=9.23 | business criticality=10.0 |
| 6 | 🟢 LOW | 19.23 | Modal interaction: Add new row | 2.8 min | dom confidence=9.23 | business criticality=10.0 |
| 7 | 🟢 LOW | 19.23 | Access to web pages | 2.8 min | dom confidence=9.23 | business criticality=10.0 |
| 8 | 🟢 LOW | 19.23 | Selectors against live DOM | 3.5 min | dom confidence=9.23 | business criticality=10.0 |
| 9 | 🟢 LOW | 19.23 | Valid email format | 2.8 min | dom confidence=9.23 | business criticality=10.0 |
| 10 | 🟢 LOW | 19.23 | Invalid email format (missing @) | 3.0 min | dom confidence=9.23 | business criticality=10.0 |
| 11 | 🟢 LOW | 19.23 | Invalid email format (missing TLD) | 3.2 min | dom confidence=9.23 | business criticality=10.0 |
| 12 | 🟢 LOW | 19.23 | Empty email input | 3.0 min | dom confidence=9.23 | business criticality=10.0 |
| 13 | 🟢 LOW | 19.23 | Valid age input (numeric value) | 3.0 min | dom confidence=9.23 | business criticality=10.0 |
| 14 | 🟢 LOW | 19.23 | Non-numeric age input (abc) | 3.0 min | dom confidence=9.23 | business criticality=10.0 |
| 15 | 🟢 LOW | 19.23 | Valid salary input (numeric value) | 3.2 min | dom confidence=9.23 | business criticality=10.0 |
| 16 | 🟢 LOW | 19.23 | Non-numeric salary input (abc) | 2.8 min | dom confidence=9.23 | business criticality=10.0 |

## Within Budget

- ✅ Email Validation
- ✅ Web Tables Validation
- ✅ Radio Button Validation
- ✅ UI Stability
- ✅ Invalid email behavior
- ✅ Modal interaction: Add new row
- ✅ Access to web pages
- ✅ Selectors against live DOM
- ✅ Valid email format
- ✅ Invalid email format (missing @)
- ✅ Invalid email format (missing TLD)
- ✅ Empty email input
- ✅ Valid age input (numeric value)
- ✅ Non-numeric age input (abc)
- ✅ Valid salary input (numeric value)
- ✅ Non-numeric salary input (abc)

## Risk Scoring Model

| Factor | Weight | Source |
|--------|--------|--------|
| AC coverage gap | 25% | gherkin_coverage JSON |
| DOM confidence | 20% | quality_alignment report |
| Historical failure rate | 20% | Playwright test-results/ |
| Business criticality | 20% | Jira priority field |
| Step ambiguity | 15% | step_generator coverage JSON |