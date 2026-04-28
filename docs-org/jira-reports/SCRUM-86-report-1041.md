# ❌ Execution Report: SCRUM-86

## Requirement Status: [FAILED]

**SCRUM ID:** SCRUM-86
**Date:** 2026-04-23 10:41:02
**Status:** FAILED

## 📊 Execution Summary

- Total Tests: 5
- Passed: 0
- Failed: 5

## 🧪 Test Results

- ❌ Invalid login with incorrect password
- ❌ Locked login account shows deterministic error
- ❌ Invalid logins show deterministic errors for different reasons (username=standard_user, password=wrong_password, expected_error=Invalid password. Please try again.)
- ❌ Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- ❌ Invalid logins show deterministic errors for different reasons (username=random_user, password=incorrect_password, expected_error=Invalid username or password. Please try again.)

## 🔍 Analysis

- One or more tests failed
- Possible regression or locator issue detected
- Requires investigation using artifacts below

## ❌ Failure Details

### Test: Invalid login with incorrect password

![Failure Screenshot](test-results/steps-SCRUM-86-Invalid-and-0495f-se-try-again-in-30-minutes--chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> Invalid and Locked SauceDemo Logins Show Deterministic Errors >> Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- Location: tests/steps/SCRUM-86.spec.ts:31:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("standard_user")
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
  357 |  
```
</details>

### Test: Locked login account shows deterministic error

![Failure Screenshot](test-results/steps-SCRUM-86-Invalid-and-0495f-se-try-again-in-30-minutes--chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> Invalid and Locked SauceDemo Logins Show Deterministic Errors >> Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- Location: tests/steps/SCRUM-86.spec.ts:31:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("standard_user")
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
  357 |  
```
</details>

### Test: Invalid logins show deterministic errors for different reasons (username=standard_user, password=wrong_password, expected_error=Invalid password. Please try again.)

![Failure Screenshot](test-results/steps-SCRUM-86-Invalid-and-0495f-se-try-again-in-30-minutes--chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> Invalid and Locked SauceDemo Logins Show Deterministic Errors >> Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- Location: tests/steps/SCRUM-86.spec.ts:31:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("standard_user")
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
  357 |  
```
</details>

### Test: Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)

![Failure Screenshot](test-results/steps-SCRUM-86-Invalid-and-0495f-se-try-again-in-30-minutes--chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> Invalid and Locked SauceDemo Logins Show Deterministic Errors >> Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- Location: tests/steps/SCRUM-86.spec.ts:31:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("standard_user")
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
  357 |  
```
</details>

### Test: Invalid logins show deterministic errors for different reasons (username=random_user, password=incorrect_password, expected_error=Invalid username or password. Please try again.)

![Failure Screenshot](test-results/steps-SCRUM-86-Invalid-and-0495f-se-try-again-in-30-minutes--chromium/test-failed-1.png)

<details>
<summary>Error Context</summary>

```
# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: steps/SCRUM-86.spec.ts >> Invalid and Locked SauceDemo Logins Show Deterministic Errors >> Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)
- Location: tests/steps/SCRUM-86.spec.ts:31:9

# Error details

```
Error: locator.fill: Error: Input of type "submit" cannot be filled
Call log:
  - waiting for locator('//*[@id="login-button"]')
    - locator resolved to <input type="submit" value="Login" id="login-button" name="login-button" data-test="login-button" class="submit-button btn_action"/>
    - fill("standard_user")
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
  357 |  
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
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-110
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-111
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-112
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-113
- Bug: https://paularpitaseis.atlassian.net/browse/SCRUM-114

---
*Generated by TEA Reporting Agent*