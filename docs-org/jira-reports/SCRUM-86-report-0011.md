# ❌ Execution Report: SCRUM-86

## Requirement Status: [FAILED]

**SCRUM ID:** SCRUM-86
**Date:** 2026-04-23 00:11:15
**Status:** FAILED

## 📊 Execution Summary

- Total Tests: 8
- Passed: 6
- Failed: 2

## 🧪 Test Results

- ❌ Invalid username and password
- ✅ Locked account with incorrect password
- ✅ Invalid credentials combinations (username=not_a_real_username, password=wrong_password, expected_error=Epic failure)
- ✅ Invalid credentials combinations (username=real_username, password=no_password, expected_error=Password required)
- ✅ Invalid credentials combinations (username=no_username, password=real_password, expected_error=Username required)
- ✅ Invalid credentials combinations (username=real_username, password=wrong_password, expected_error=Wrong password)
- ❌ Login button disabled with invalid credentials
- ✅ Error message shown on locked account

## 🔍 Analysis

- One or more tests failed
- Possible regression or locator issue detected
- Requires investigation using artifacts below

## ❌ Failure Details

### Test: Invalid username and password

![Failure Screenshot](test-results/steps-SCRUM-86-SauceDemo-L-35b79-ed-with-invalid-credentials-chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> SauceDemo Login with Invalid Credentials >> Login button disabled with invalid credentials
- Location: tests/steps/SCRUM-86.spec.ts:37:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("not_a_real_username|wrong_password")
  - attempting fill action
    - waiting for element to be visible, enabled and editable

```

# Page snapshot

```yaml
- generic [ref=e3]:
  - generic [ref=e4]: Swag Labs
  - generic [ref=e5]:
    - generic [ref=e9]:
      - textbox "Username" [ref=e11]
      - textbox "Password" [ref=e13]
      - button "Login" [ref=e15] [cursor=pointer]
    - generic [ref=e17]:
      - generic [ref=e18]:
        - heading "Accepted usernames are:" [level=4] [ref=e19]
        - text: standard_user
        - text: locked_out_user
        - text: problem_user
        - text: performance_glitch_user
        - text: error_user
        - text: visual_user
      - generic [ref=e20]:
        - heading "Password for all users:" [level=4] [ref=e21]
        - text: secret_sauce
```

# Test source

```ts
  353 |      *     A) Qdrant URL lookup      → expect(page).toHaveURL(qdrantUrl)
  354 |      *     B) BASE_URL root          → expect(page).toHaveURL(BASE_URL)  [soft]
  355 |      *     C) Redirect vocab present → expect(page).not.toHaveURL('about:blank')
  356 |      *     D) No match at all        → throw "element not found"
  357 |      */
  358 |     async smartAction(intent: string, value?: string): Promise<void> {
  359 |         const n = intent.toLowerCase().trim(); // normalised in
```
</details>

### Test: Login button disabled with invalid credentials

![Failure Screenshot](test-results/steps-SCRUM-86-SauceDemo-L-35b79-ed-with-invalid-credentials-chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> SauceDemo Login with Invalid Credentials >> Login button disabled with invalid credentials
- Location: tests/steps/SCRUM-86.spec.ts:37:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("not_a_real_username|wrong_password")
  - attempting fill action
    - waiting for element to be visible, enabled and editable

```

# Page snapshot

```yaml
- generic [ref=e3]:
  - generic [ref=e4]: Swag Labs
  - generic [ref=e5]:
    - generic [ref=e9]:
      - textbox "Username" [ref=e11]
      - textbox "Password" [ref=e13]
      - button "Login" [ref=e15] [cursor=pointer]
    - generic [ref=e17]:
      - generic [ref=e18]:
        - heading "Accepted usernames are:" [level=4] [ref=e19]
        - text: standard_user
        - text: locked_out_user
        - text: problem_user
        - text: performance_glitch_user
        - text: error_user
        - text: visual_user
      - generic [ref=e20]:
        - heading "Password for all users:" [level=4] [ref=e21]
        - text: secret_sauce
```

# Test source

```ts
  353 |      *     A) Qdrant URL lookup      → expect(page).toHaveURL(qdrantUrl)
  354 |      *     B) BASE_URL root          → expect(page).toHaveURL(BASE_URL)  [soft]
  355 |      *     C) Redirect vocab present → expect(page).not.toHaveURL('about:blank')
  356 |      *     D) No match at all        → throw "element not found"
  357 |      */
  358 |     async smartAction(intent: string, value?: string): Promise<void> {
  359 |         const n = intent.toLowerCase().trim(); // normalised in
```
</details>

## 💼 Business Impact

- Feature behavior is impacted
- May affect user workflows
- Immediate attention required

## 🛠 Recommended Actions

- Investigate failure logs and screenshots
- Verify selectors and DOM structure
- Re-run after fixes

## 🔗 References

- Story: https://paularpitaseis.atlassian.net/browse/SCRUM-86
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-108
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-109

---
*Generated by TEA Reporting Agent*