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