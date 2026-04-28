# Gherkin Agent Debug Report
**Project:** SCRUM-70  
**Generated:** 2026-04-26 19:46:11  
**Story:** SCRUM-70 — Negative Path Validation for DemoQA Elements Module  
**Epic:** SCRUM-36 — Automation & Negative Path Validation – DemoQA Elements Module  

---

## Step 0 — Knowledge Builder

Assembles a unified knowledgebase from every source before any agent runs.

| Source | Count |
|--------|-------|
| Acceptance Criteria blocks | 19 |
| Subtasks | 4 |
| Comments | 8 |
| Validation Rules | 9 |
| Test Data items | 9 |
| Attachment rows | 5 |
| DOM element vectors | 74 |
| PRD sections | 12 |
| Negative scenario blocks | 1 |
| Total KB signals (estimated) | 38 |

### Epic
**Key:** SCRUM-36  
**Summary:** Automation & Negative Path Validation – DemoQA Elements Module  
**Status:** To Do  
**Description:** Description
This Epic covers validation of the 
Elements module on DemoQA Elements
 using automated UI testing.
Target Application:


 

The goal is to ensure:
Input validation works correctly
Invalid data is blocked
UI behavior remains stable under edge conditions

Scope
In Scope
Text Box
Web Tables
Radio Button
Out of Scope
Backend validation
Other modules

Success Criteria
All negative scenarios behave as expected
No UI crashes or inconsistent states
Validation errors are correctly displayed

### Story
**Key:** SCRUM-70  
**Summary:** Negative Path Validation for DemoQA Elements Module  
**Description:** As a QA Engineer,
I want to validate negative scenarios in the Elements module,
so that invalid inputs are handled correctly and the UI remains stable under edge conditions.
Target Application:  ✅ 
Acceptance Criteria  AC1 – Email Validation Invalid email input triggers validation error on 
#userEmail
Output section (
#output
) is not displayed for invalid input  AC2 – Web Tables Validation Non-numeric values in Age/Salary fields block submission
Registration modal remains open on validation fai

### Subtasks
- **SCRUM-117** — Validation & Reporting Setup *(Status: To Do)*
  Description
Validate application behavior against expected outcomes and document test execution results for negative-path scenarios.

Activities
Capture validation states:
Verify UI behavior for inval
- **SCRUM-116** — Automated Test Script Development *(Status: To Do)*
  Develop automated UI test scripts to validate negative-path scenarios across the DemoQA Elements module.

Scope / Activities
Implement Text Box validation:
Use selector 
#userEmail
Verify invalid emai
- **SCRUM-115** — Test Data Preparation & Environment Setup *(Status: To Do)*
  Summary
Prepare test data and validate environment readiness for execution.
Context
This issue involves verifying the accessibility of specific web pages and ensuring that test data is ready for execu
- **SCRUM-118** — Execution & Review *(Status: To Do)*
  Description
Execute the complete test suite for the DemoQA Elements module and validate results against defined Acceptance Criteria.

Activities
Execute full test suite:
Run all automated and/or manua

### Acceptance Criteria (raw blocks)
**AC Block 1:**
```
AC1 – Email Validation: Invalid email input triggers validation error on #userEmail, Output section (#output) is not displayed for invalid input.
AC2 – Web Tables Validation: Non-numeric values in Age/Salary fields block submission, Registration modal remains open.
AC3 – Radio Button Validation: “No” option (#noRadio) remains disabled, clicking the option does not trigger any state change.
AC4 – UI Stability: UI remains stable under obstruction (e.g., overlays), elements remain interactable via scroll/visibility handling.
```

**AC Block 2:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Activities Execute full test suite: Run all automated and/or manual test scenarios Validate results: Compare test outcomes against Acceptance Criteria (ACs) Ensure expected validation behavior is observed Review UI behavior and logs: Verify UI states (error indicators, modal behavior, disabled interactions) Check execution logs for anomalies or errors Perform peer review: Share results with QA/Team members Validate test coverage and result accuracy Expected Outcome All test cases executed successfully Results validated against 
```

**AC Block 3:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Target Application: ✅  Acceptance Criteria AC1 – Email Validation Invalid email input triggers validation error on  #userEmail Output section ( #output ) is not displayed for invalid input AC2 – Web Tables Validation Non-numeric values in Age/Salary fields block submission Registration modal remains open on validation failure AC3 – Radio Button Validation “No” option ( #noRadio ) remains disabled Clicking the option does not trigger any state change AC4 – UI Stability UI remains stable under obstruction (e.g., overlays) Element
```

**AC Block 4:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

🧪  Test Data Invalid email formats (e.g.,  test@domain , missing TLD) Non-numeric values for numeric fields (e.g.,  abc ,  12ab ) Empty/null inputs 🎯  Notes Screenshots are provided as  reference (expected behavior) During execution, actual results should be validated against Acceptance Criteria Any deviation should be logged as a defect with supporting evidence
```

**AC Block 5:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Execution & Review Description Execute the complete test suite for the DemoQA Elements module and validate results against defined Acceptance Criteria.
```

**AC Block 6:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Scope / Activities Implement Text Box validation: Use selector  #userEmail Verify invalid email behavior (no  #output  rendered) Automate Web Tables modal interaction: Open modal using  #addNewRecordButton Validate submission is blocked for invalid inputs ( #age ,  #salary ) Validate Radio Button behavior: Verify disabled state of  #noRadio Ensure no state change or success message is triggered Handle overlay and visibility conditions: Ensure elements are interactable (scroll into view if required) Implement retry/visibility ha
```

**AC Block 7:**
```
Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Acceptance criteria Verify accessibility of:    Extract negative test data from Validation Matrix (Excel).
```

**AC Block 8:**
```
Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Activities Capture validation states: Verify UI behavior for invalid inputs (e.g., error states, blocked submission) Capture screenshots: Record screenshots for all failed or unexpected scenarios Include visual evidence of UI behavior (error states, modal persistence, etc.) Log results: Document  Expected vs Actual  behavior for each test case Record results in test report or execution logs Classify outcomes: Mark results as: Expected validation (PASS) Unexpected behavior (FAIL) Ensure correct categorization of validation vs syst
```

**AC Block 9:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

🧪  Test Data Invalid email formats (e.g.,  test@domain , missing TLD) Non-numeric values for numeric fields (e.g.,  abc ,  12ab ) Empty/null inputs  🎯  Notes Screenshots are provided as  reference (expected behavior) During execution, actual results should be validated against Acceptance Criteria Any deviation should be logged as a defect with supporting evidence
```

**AC Block 10:**
```
Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Validation & Reporting Setup Description Validate application behavior against expected outcomes and document test execution results for negative-path scenarios.
```

### Validation Rules
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

It also includes validating selectors against the live DOM.
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Other information All target pages should be accessible.
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: requirement

Subtask SCRUM-116: Automated Test Script Development To Do
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: requirement

Subtask SCRUM-118: Execution & Review To Do
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Subtask SCRUM-117: Validation & Reporting Setup To Do
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: action

Selectors need to be verified and usable for automation. To Do
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Validate selectors against live DOM: #userEmail #age ,  #salary #noRadio Ensure selector references are up-to-date and stable.
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Subtask SCRUM-116: Automated Test Script Development Develop automated UI test scripts to validate negative-path scenarios across the D
- Email input field:
* Valid email format: user@domain.com (PASS)
* Invalid email format: missing @, missing TLD (VALIDATION_ERROR)
* Empty email input (VALIDATION_ERROR)

Age and Salary fields:
* Non-n

### Test Data
- Validation is enforced at the time of form submission.  
 
Selectors  
• Email Field: #userEmail  
• Submit Button: #submit  
• Output Container: #output  
 
Behavior Specification  
Condition  User 

- Source: PRD
Project: SCRUM-70
Section: subtask
Type: requirement

Subtask SCRUM-115: Test Data Preparation & Environment Setup To Do
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Subtask SCRUM-115: Test Data Preparation & Environment Setup Summary Prepare test data and validate environment readiness for execution
- Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

The screenshots cover: Text Box Validation Invalid email → validation error, no output displayed Valid input → output section rendere
- Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Test Data Preparation & Environment Setup Summary Prepare test data and validate environment readiness for execution.
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: assertion

Context This issue involves verifying the accessibility of specific web pages and ensuring that test data is ready for execution.
- Source: PRD
Project: SCRUM-70
Section: subtask
Type: validation

Test data must be prepared and ready for execution.
- Source: PRD
Project: SCRUM-70
Section: jira_main
Type: assertion

Context This issue involves verifying the accessibility of specific web pages and ensuring that test data is ready for execution.
- Source: PRD
Project: SCRUM-70
Section: jira_main
Type: validation

Test data must be prepared and ready for execution.

### Attachment Data (Excel / PDF rows)
- [SCRUM-70_Functional Requirements Document (FRD).pdf] Description  
The system must enforce numeric -only validation for the following fields within the 
Registration Form modal : 
• Age (#age)  
• Salary (#salary)  
Validation occurs at submission time.
- [SCRUM-70_Functional Requirements Document (FRD).pdf] 🎯 Summary  
This FRD defines:  
✔ Exact validation rules  
✔ DOM -level expectations  
✔ Error classification model  
✔ UI state transitions  
✔ Automation constraints  
 
 
If you want next, I can:  
- [SCRUM-70_Functional Requirements Document (FRD).pdf] 📄 Functional Requirements Document 
(FRD)  
Project: Enterprise Web UI Validation Engine – DemoQA 
Elements Module  
 
1. Document Overview  
This Functional Requirements Document (FRD) defines the ex
- [SCRUM-70_Functional Requirements Document (FRD).pdf] 5. Non -Functional Constraints (UI 
Context)  
 
5.1 Lazy Loading & Visibility  
• Elements must be:  
o Present in DOM  
o Visible (isVisible() == true)  
• Automation must wait for readiness before 
- [PRD] SCRUM-70_demoqa_validation_matrix_v2.xlsx

Module: Text Box | Field: Email | Input Type: Missing @ | Input Value: userdomain.com | Expected State: Blocked | Error Type: VALIDATION_ERROR | Severity: Lo

### Comments & Decisions
- **[SCRUM-36]** All automated specs generated in Sub-task 2 must include a 
@traceability
 tag in the Gherkin file linking back to the Requirement ID in the attached FRD. This ensures that if the 'Age' validation log
- **[SCRUM-36]** All automated specs generated in Sub-task 2 must include a 
@traceability
 tag in the Gherkin file linking back to the Requirement ID in the attached FRD. This ensures that if the 'Age' validation log
- **[UNKNOWN]** Source: PRD
Project: SCRUM-70
Section: comment
Type: validation

All automated specs generated in Sub-task 2 must include a  @traceability  tag in the Gherkin file linking back to the Requirement ID i
- **[SCRUM-70]** Source: PRD
Project: SCRUM-70
Section: comments_bulk
Type: validation

[SCRUM-36] Arpita Paul: All automated specs generated in Sub-task 2 must include a  @traceability  tag in the Gherkin file linkin
- **[UNKNOWN]** Source: PRD
Project: SCRUM-70
Section: comment
Type: validation

This ensures that if the 'Age' validation logic changes in the FRD, we can immediately identify which scripts need updating.
- **[SCRUM-70]** Source: PRD
Project: SCRUM-70
Section: comments_bulk
Type: validation

This ensures that if the 'Age' validation logic changes in the FRD, we can immediately identify which scripts need updating.
- **[SCRUM-70]** Source: PRD
Project: SCRUM-70
Section: comments_bulk
Type: validation

All automated specs generated in Sub-task 2 must include a  @traceability  tag in the Gherkin file linking back to the Requiremen
- **[PRD]** All automated specs generated in Sub-task 2 must include a @traceability tag in the Gherkin file linking back to the Requirement ID in the attached FRD.

This ensures that if the 'Age' validation logi

---

## Agent 1 — AC Analyst

Reads the KB in focused chunks (AC+story, subtasks, validation/attachments) and extracts every testable condition as a structured AC list.

**Total ACs extracted:** 16

| ID | Title | Type | Source | Source Ref | Priority |
|----|-------|------|--------|------------|----------|
| AC1 | Email Validation | negative | acceptance_criteria | SCRUM-70 | high |
| AC2 | Web Tables Validation | negative | acceptance_criteria | SCRUM-70 | high |
| AC3 | Radio Button Validation | positive | acceptance_criteria | SCRUM-70 | high |
| AC4 | UI Stability | positive | acceptance_criteria | SCRUM-70 | high |
| AC5 | Invalid email behavior | negative | subtask | SCRUM-116 | high |
| AC6 | Modal interaction: Add new row | positive | subtask | SCRUM-116 | high |
| AC7 | Access to web pages | positive | subtask | SCRUM-115 | high |
| AC8 | Selectors against live DOM | positive | subtask | SCRUM-115 | high |
| AC9 | Valid email format | positive | validation | SCRUM-70 | high |
| AC10 | Invalid email format (missing @) | negative | validation | SCRUM-70 | high |
| AC11 | Invalid email format (missing TLD) | negative | validation | SCRUM-70 | high |
| AC12 | Empty email input | negative | validation | SCRUM-70 | high |
| AC13 | Valid age input (numeric value) | positive | validation | SCRUM-70 | high |
| AC14 | Non-numeric age input (abc) | negative | validation | SCRUM-70 | high |
| AC15 | Valid salary input (numeric value) | positive | validation | SCRUM-70 | high |
| AC16 | Non-numeric salary input (abc) | negative | validation | SCRUM-70 | high |

### AC Details (with steps and expected results)

#### AC1 — Email Validation
**Type:** negative  **Source:** acceptance_criteria (SCRUM-70)  **Priority:** high
**Description:** Invalid email input triggers validation error on #userEmail, Output section (#output) is not displayed for invalid input.
**Test Data:** test@domain, missing TLD
**Expected Result:** No output section displayed, validation error shown
**Steps:**
1. Enter invalid email format in the field  → *Validation error triggered*

#### AC2 — Web Tables Validation
**Type:** negative  **Source:** acceptance_criteria (SCRUM-70)  **Priority:** high
**Description:** Non-numeric values in Age/Salary fields block submission, Registration modal remains open.
**Test Data:** abc, 12ab
**Expected Result:** No submission possible, modal remains open
**Steps:**
1. Enter non-numeric value in the Age field  → *Submission blocked, modal remains open*

#### AC3 — Radio Button Validation
**Type:** positive  **Source:** acceptance_criteria (SCRUM-70)  **Priority:** high
**Description:** 'No' option (#noRadio) remains disabled, clicking the option does not trigger any state change.
**Test Data:** 
**Expected Result:** Radio button remains in the same state, no change triggered
**Steps:**
1. Click on 'No' radio button  → *'No' radio button remains disabled*

#### AC4 — UI Stability
**Type:** positive  **Source:** acceptance_criteria (SCRUM-70)  **Priority:** high
**Description:** UI remains stable under obstruction (e.g., overlays), elements remain interactable via scroll/visibility handling.
**Test Data:** 
**Expected Result:** No UI stability issues found
**Steps:**
1. Interact with UI elements  → *UI remains stable, elements remain interactable*

#### AC5 — Invalid email behavior
**Type:** negative  **Source:** subtask (SCRUM-116)  **Priority:** high
**Description:** Verify invalid email behavior (no #output rendered)
**Test Data:** example_value
**Expected Result:** #output not rendered
**Steps:**
1. Enter invalid email  → *Error state*

#### AC6 — Modal interaction: Add new row
**Type:** positive  **Source:** subtask (SCRUM-116)  **Priority:** high
**Description:** Open modal using #addNewR
**Test Data:** example_value
**Expected Result:** Modal is opened
**Steps:**
1. Click #addNewR  → *Modal opens*

#### AC7 — Access to web pages
**Type:** positive  **Source:** subtask (SCRUM-115)  **Priority:** high
**Description:** Verify accessibility of specific web pages
**Test Data:** example_value
**Expected Result:** Web pages are accessible
**Steps:**
1. Navigate to page  → *Page is accessible*

#### AC8 — Selectors against live DOM
**Type:** positive  **Source:** subtask (SCRUM-115)  **Priority:** high
**Description:** Validate selectors against the live DOM
**Test Data:** example_value
**Expected Result:** Selectors are valid
**Steps:**
1. Compare selectors  → *Selectors match*

#### AC9 — Valid email format
**Type:** positive  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter a valid email address in the format of user@domain.com
**Test Data:** user@domain.com
**Expected Result:** Overall pass condition
**Steps:**
1. Enter value in the field  → *Field accepts it*

#### AC10 — Invalid email format (missing @)
**Type:** negative  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter an invalid email address with missing '@' symbol
**Test Data:** userdomain.com
**Expected Result:** Validation error
**Steps:**
1. Enter value in the field  → *Field rejects it*

#### AC11 — Invalid email format (missing TLD)
**Type:** negative  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter an invalid email address with missing top-level domain (TLD) suffix
**Test Data:** user@domain
**Expected Result:** Validation error
**Steps:**
1. Enter value in the field  → *Field rejects it*

#### AC12 — Empty email input
**Type:** negative  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Leave the email field empty
**Test Data:** 
**Expected Result:** Validation error
**Steps:**
1. Enter value in the field  → *Field rejects it*

#### AC13 — Valid age input (numeric value)
**Type:** positive  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter a valid numeric age value between 1 and 99
**Test Data:** 12
**Expected Result:** Overall pass condition
**Steps:**
1. Enter value in the field  → *Field accepts it*

#### AC14 — Non-numeric age input (abc)
**Type:** negative  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter an invalid non-numeric age value, such as 'abc'
**Test Data:** abc
**Expected Result:** Validation error
**Steps:**
1. Enter value in the field  → *Field rejects it*

#### AC15 — Valid salary input (numeric value)
**Type:** positive  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter a valid numeric salary value between 1 and 99
**Test Data:** 50
**Expected Result:** Overall pass condition
**Steps:**
1. Enter value in the field  → *Field accepts it*

#### AC16 — Non-numeric salary input (abc)
**Type:** negative  **Source:** validation (SCRUM-70)  **Priority:** high
**Description:** Enter an invalid non-numeric salary value, such as 'abc'
**Test Data:** abc
**Expected Result:** Validation error
**Steps:**
1. Enter value in the field  → *Field rejects it*

---

## Agent 2 — DOM Mapper

Maps each AC to real DOM elements. Uses Qdrant ui_memory semantic search first, falls back to keyword matching.

| AC ID | Page URL | Elements | Method |
|-------|----------|----------|--------|
| AC1 | https://demoqa.com/text-box | 2 | keyword |
| AC2 | https://demoqa.com/text-box | 1 | keyword_fallback |
| AC3 | https://demoqa.com/radio-button | 1 | llm |
| AC4 | https://demoqa.com/text-box | 1 | keyword_fallback |
| AC5 | https://demoqa.com/text-box | 3 | keyword |
| AC6 | https://demoqa.com/webtables | 1 | llm |
| AC7 | https://demoqa.com/text-box | 1 | keyword_fallback |
| AC8 | https://demoqa.com/text-box | 4 | llm |
| AC9 | https://demoqa.com/text-box | 1 | llm |
| AC10 | https://demoqa.com/text-box | 1 | llm |
| AC11 | https://demoqa.com/text-box | 1 | llm |
| AC12 | https://demoqa.com/text-box | 1 | llm |
| AC13 | https://demoqa.com/text-box | 1 | keyword_fallback |
| AC14 | https://demoqa.com/text-box | 1 | llm |
| AC15 | https://demoqa.com/text-box | 1 | llm |
| AC16 | https://demoqa.com/text-box | 1 | llm |

### Mapping Details

#### AC1 → `https://demoqa.com/text-box`
- **[FILL]** `#userEmail` — name@example.com value=`test@domain`
- **[CLICK]** `#submit` — Submit value=``

#### AC2 → `https://demoqa.com/text-box`
- **[CLICK]** `#submit` — Submit value=``

#### AC3 → `https://demoqa.com/radio-button`
- **[ASSERT_DISABLED]** `#noRadio` — No value=``
- **[ASSERT]** `` —   expected=`` absent=False

#### AC4 → `https://demoqa.com/text-box`
- **[CLICK]** `#submit` — Submit value=``

#### AC5 → `https://demoqa.com/text-box`
- **[FILL]** `#userName` — Full Name value=`example_value`
- **[FILL]** `#userEmail` — name@example.com value=``
- **[CLICK]** `#submit` — Submit value=``

#### AC6 → `https://demoqa.com/webtables`
- **[]** `#searchBox` — Type to search value=``
- **[ASSERT]** `/html/body/div[2]/div[1]` — Search Results  expected=`example_value` absent=False

#### AC7 → `https://demoqa.com/text-box`
- **[CLICK]** `#submit` — Submit value=``

#### AC8 → `https://demoqa.com/text-box`
- **[FILL]** `#userName` — Full Name value=`example_value`
- **[FILL]** `#userEmail` — name@example.com value=``
- **[FILL]** `#currentAddress` — Current Address value=``
- **[FILL]** `#permanentAddress` — permanentAddress value=``
- **[ASSERT]** `#userName` —   expected=`` absent=False

#### AC9 → `https://demoqa.com/text-box`
- **[FILL]** `#userEmail` — name@example.com value=`user@domain.com`
- **[ASSERT]** `#currentAddress` — Current Address  expected=`expected visible text after action` absent=False

#### AC10 → `https://demoqa.com/text-box`
- **[FILL]** `#userEmail` — name@example.com value=`userdomain.com`
- **[ASSERT]** `` —   expected=`` absent=False

#### AC11 → `https://demoqa.com/text-box`
- **[FILL]** `#userEmail` — name@example.com value=`test user@`
- **[ASSERT]** `` —   expected=`` absent=False

#### AC12 → `https://demoqa.com/text-box`
- **[FILL]** `#userEmail` — name@example.com value=``
- **[ASSERT]** `` —   expected=`` absent=False

#### AC13 → `https://demoqa.com/text-box`
- **[CLICK]** `#submit` — Submit value=``

#### AC14 → `https://demoqa.com/text-box`
- **[FILL]** `#userName` — Full Name value=`abc`
- **[ASSERT]** `` —   expected=`` absent=False

#### AC15 → `https://demoqa.com/text-box`
- **[FILL]** `#userName` — Full Name value=`50`
- **[ASSERT]** `` —   expected=`` absent=False

#### AC16 → `https://demoqa.com/text-box`
- **[FILL]** `#userName` — Full Name value=`abc`
- **[ASSERT]** `#currentAddress` — Current Address  expected=`` absent=False

---

## Agent 3 — Scenario Writer (raw output before QA review)

Writes one Gherkin scenario per AC. Each LLM call is focused on a single AC to stay within Ollama context limits.

```gherkin
@SCRUM-70 @negative @AC1
@SCRUM-70 @negative @AC1 @SCRUM_70
Scenario: Email Validation
Given I am on the "https://demoqa.com/text-box" page
When I enter "test@domain" in the email field
And I click the Submit button
Then validation error should be triggered
And the output section should not be displayed

@SCRUM-70 @negative @AC2
@SCRUM-70 @negative @AC2 @SCRUM_70
Scenario: Web Tables Validation
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the Age field
Then the Submit button should be disabled
And the Registration modal should remain open
Then no submission is possible

@SCRUM-70 @positive @AC3
@SCRUM-70 @positive @AC3 @SCRUM_70
Scenario: Radio Button Validation
Given I am on the "https://demoqa.com/radio-button" page
And I select the "#noRadio" radio button
Then the "#noRadio" radio button should be disabled
And the state of the "#noRadio" radio button remains unchanged

@SCRUM-70 @positive @AC4
@SCRUM-70 @positive @AC4 @SCRUM_70
Scenario: UI Stability
Given I am on the "https://demoqa.com/text-box" page
When I interact with UI elements
Then the UI remains stable and all elements remain interactable

@SCRUM-70 @negative @AC5
@SCRUM-70 @negative @AC5 @SCRUM_116
Scenario: Invalid email behavior
Given I am on the "https://demoqa.com/text-box" page
When I enter invalid email syntax in the Email field
Then the Email field enters an invalid state and the Output container is NOT rendered

@SCRUM-70 @positive @AC6
@SCRUM-70 @positive @AC6 @SCRUM_116
Scenario: Modal interaction: Add new row
Given I am on the "https://demoqa.com/webtables" page
When I click #addNewR
Then the modal should be opened

@SCRUM-70 @positive @AC7
@SCRUM-70 @positive @AC7 @SCRUM_115
Scenario: Access to web pages
Given I am on the "https://demoqa.com/text-box" page
When I click the Submit button
Then the page should be accessible

@SCRUM-70 @positive @AC8
@SCRUM-70 @positive @AC8 @SCRUM_115
Scenario: Selectors against live DOM
Given I am on the "https://demoqa.com/text-box" page
Then the "Full Name" field selector "#userName" exists and is enabled
And the "Email" field selector "#userEmail" exists and is empty
And the "Current Address" field selector "#currentAddress" exists and is empty
And the "permanentAddress" field selector "#permanentAddress" exists and is empty
Then the selectors are valid

@SCRUM-70 @positive @AC9
@SCRUM-70 @positive @AC9 @SCRUM_70
Scenario: Valid email format
Given I am on the "https://demoqa.com/text-box" page
When I enter "user@domain.com" in the Email field
Then the Output container should display the submitted data

@SCRUM-70 @negative @AC10
@SCRUM-70 @negative @AC10 @SCRUM_70
Scenario: Invalid email format (missing @)
Given I am on the "https://demoqa.com/text-box" page
When I enter "userdomain.com" in the email field
Then the field should be invalid
And the validation error message should appear

@SCRUM-70 @negative @AC11
@SCRUM-70 @negative @AC11 @SCRUM_70
Scenario: Invalid email format (missing TLD)
Given I am on the "https://demoqa.com/text-box" page
When I enter "user"@domain in the Email field
Then the Email field should be in an invalid state and the border should turn red
And the Output container should not be rendered
Then the validation error message should appear

@SCRUM-70 @negative @AC12
@SCRUM-70 @negative @AC12 @SCRUM_70
Scenario: Empty email input
Given I am on the "https://demoqa.com/text-box" page
When I leave the Email field empty
Then validation error should be displayed
And the Output container should not be rendered

@SCRUM-70 @positive @AC13
@SCRUM-70 @positive @AC13 @SCRUM_70
Scenario: Valid age input (numeric value)
Given I am on the "https://demoqa.com/text-box" page
When I enter "12" in the age field
Then the result should display "Age: 12"
And I should see the success message

@SCRUM-70 @negative @AC14
@SCRUM-70 @negative @AC14 @SCRUM_70
Scenario: Non-numeric age input (abc)
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the name field
Then the result section should display "Invalid Age"
And I should not see the success message

@SCRUM-70 @positive @AC15
@SCRUM-70 @positive @AC15 @SCRUM_70
Scenario: Valid salary input (numeric value)
Given I am on the "https://demoqa.com/text-box" page
When I enter "50" in the Full Name field
Then the label for the Full Name field should be present
And the result section should display "Name: 50"
And I should not see the success message

@SCRUM-70 @negative @AC16
@SCRUM-70 @negative @AC16 @SCRUM_70
Scenario: Non-numeric salary input (abc)
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the Full Name field
Then the Current Address label should not be displayed
```

---

## Agent 4 — QA Reviewer (final output)

Wraps scenarios in a Feature block, fixes structure, enforces BDD rules, adds TODO placeholders for missing coverage categories.

```gherkin
Feature: Negative Path Validation for DemoQA Elements Module
  # Project: SCRUM-70
  # Epic: SCRUM-36
  # Sources: Jira Story, Epic, 4 Subtasks, 5 Attachment rows, Validation Rules, DOM
  # Total scenarios: 16

@SCRUM-70 @negative @AC1
@SCRUM-70 @negative @AC1 @SCRUM_70
Scenario: Email Validation
Given I am on the "https://demoqa.com/text-box" page
When I enter "test@domain" in the email field
And I click the Submit button
Then validation error should be triggered
And the output section should not be displayed

@SCRUM-70 @negative @AC2
@SCRUM-70 @negative @AC2 @SCRUM_70
Scenario: Web Tables Validation
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the Age field
Then the Submit button should be disabled
And the Registration modal should remain open
Then no submission is possible

@SCRUM-70 @positive @AC3
@SCRUM-70 @positive @AC3 @SCRUM_70
Scenario: Radio Button Validation
Given I am on the "https://demoqa.com/radio-button" page
And I select the "#noRadio" radio button
Then the "#noRadio" radio button should be disabled
And the state of the "#noRadio" radio button remains unchanged

@SCRUM-70 @positive @AC4
@SCRUM-70 @positive @AC4 @SCRUM_70
Scenario: UI Stability
Given I am on the "https://demoqa.com/text-box" page
When I interact with UI elements
Then the UI remains stable and all elements remain interactable

@SCRUM-70 @negative @AC5
@SCRUM-70 @negative @AC5 @SCRUM_116
Scenario: Invalid email behavior
Given I am on the "https://demoqa.com/text-box" page
When I enter invalid email syntax in the Email field
Then the Email field enters an invalid state and the Output container is NOT rendered

@SCRUM-70 @positive @AC6
@SCRUM-70 @positive @AC6 @SCRUM_116
Scenario: Modal interaction: Add new row
Given I am on the "https://demoqa.com/webtables" page
When I click #addNewR
Then the modal should be opened

@SCRUM-70 @positive @AC7
@SCRUM-70 @positive @AC7 @SCRUM_115
Scenario: Access to web pages
Given I am on the "https://demoqa.com/text-box" page
When I click the Submit button
Then the page should be accessible

@SCRUM-70 @positive @AC8
@SCRUM-70 @positive @AC8 @SCRUM_115
Scenario: Selectors against live DOM
Given I am on the "https://demoqa.com/text-box" page
Then the "Full Name" field selector "#userName" exists and is enabled
And the "Email" field selector "#userEmail" exists and is empty
And the "Current Address" field selector "#currentAddress" exists and is empty
And the "permanentAddress" field selector "#permanentAddress" exists and is empty
Then the selectors are valid

@SCRUM-70 @positive @AC9
@SCRUM-70 @positive @AC9 @SCRUM_70
Scenario: Valid email format
Given I am on the "https://demoqa.com/text-box" page
When I enter "user@domain.com" in the Email field
Then the Output container should display the submitted data

@SCRUM-70 @negative @AC10
@SCRUM-70 @negative @AC10 @SCRUM_70
Scenario: Invalid email format (missing @)
Given I am on the "https://demoqa.com/text-box" page
When I enter "userdomain.com" in the email field
Then the field should be invalid
And the validation error message should appear

@SCRUM-70 @negative @AC11
@SCRUM-70 @negative @AC11 @SCRUM_70
Scenario: Invalid email format (missing TLD)
Given I am on the "https://demoqa.com/text-box" page
When I enter "user"@domain in the Email field
Then the Email field should be in an invalid state and the border should turn red
And the Output container should not be rendered
Then the validation error message should appear

@SCRUM-70 @negative @AC12
@SCRUM-70 @negative @AC12 @SCRUM_70
Scenario: Empty email input
Given I am on the "https://demoqa.com/text-box" page
When I leave the Email field empty
Then validation error should be displayed
And the Output container should not be rendered

@SCRUM-70 @positive @AC13
@SCRUM-70 @positive @AC13 @SCRUM_70
Scenario: Valid age input (numeric value)
Given I am on the "https://demoqa.com/text-box" page
When I enter "12" in the age field
Then the result should display "Age: 12"
And I should see the success message

@SCRUM-70 @negative @AC14
@SCRUM-70 @negative @AC14 @SCRUM_70
Scenario: Non-numeric age input (abc)
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the name field
Then the result section should display "Invalid Age"
And I should not see the success message

@SCRUM-70 @positive @AC15
@SCRUM-70 @positive @AC15 @SCRUM_70
Scenario: Valid salary input (numeric value)
Given I am on the "https://demoqa.com/text-box" page
When I enter "50" in the Full Name field
Then the label for the Full Name field should be present
And the result section should display "Name: 50"
And I should not see the success message

@SCRUM-70 @negative @AC16
@SCRUM-70 @negative @AC16 @SCRUM_70
Scenario: Non-numeric salary input (abc)
Given I am on the "https://demoqa.com/text-box" page
When I enter "abc" in the Full Name field
Then the Current Address label should not be displayed
```

---

## Step 5 — Coverage Report

| Metric | Value |
|--------|-------|
| Total KB signals | 38 |
| ACs extracted | 16 |
| Scenarios written | 16 |
| Scenario Outlines | 0 |
| TODO placeholders | 0 |
| ACs covered | 16 |
| ACs missing | 0 |
| **AC coverage %** | **100.0%** |
| Signal coverage % | 42.1% |

### Breakdown by Test Type
| Type | Count |
|------|-------|
| negative | 8 |
| positive | 8 |

### Breakdown by Source
| Source | Count |
|--------|-------|
| acceptance_criteria | 4 |
| subtask | 4 |
| validation | 8 |

### Breakdown by Priority
| Priority | Count |
|----------|-------|
| high | 16 |