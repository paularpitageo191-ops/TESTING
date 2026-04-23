Feature: Invalid and Locked SauceDemo Logins Show Deterministic Errors
@SCRUM-86 @negative @smoke

Background:
Given I am on the SauceDemo login page at "https://www.saucedemo.com/"
And I enter invalid username "standard_user" and password ""

Scenario: Invalid login with incorrect password
When I submit the login form with username "standard_user" and password "wrong_password"
Then the error message "Invalid password. Please try again." is displayed

Scenario: Locked login account shows deterministic error
Given the login account is locked
When I submit the login form with username "locked_out_user" and password ""
Then the error message "Your account is currently locked due to too many failed attempts. Please try again in 30 minutes." is displayed

Scenario Outline: Invalid logins show deterministic errors for different reasons
Examples:
| username | password | expected_error |
| standard_user | wrong_password | Invalid password. Please try again. |
| locked_out_user |  | Your account is currently locked due to too many failed attempts. Please try again in 30 minutes. |
| random_user | incorrect_password | Invalid username or password. Please try again. |

When I submit the login form with username "<username>" and password "<password>"
Then the error message "<expected_error>" is displayed