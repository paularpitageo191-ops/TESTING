import { test, expect } from '@playwright/test';
import { BasePage } from '../_bmad/BasePage';

test.describe("Invalid and Locked SauceDemo Logins Show Deterministic Errors", () => {

    let basePage: BasePage;

    test.beforeEach(async ({ page }) => {
        basePage = new BasePage(page, "SCRUM-86");
        await basePage.initialize();
            await basePage.page.goto("https://www.saucedemo.com/");
            await basePage.smartAction("I enter invalid username \"standard_user\" and password \"\"", "standard_user");
    });

    test("Invalid login with incorrect password", async ({ page }) => {
            await basePage.smartAction("I submit the login form with username \"standard_user\" and password \"wrong_password\"", "standard_user");
            await basePage.smartAction("the error message \"Invalid password. Please try again.\" is displayed");
    });

    test("Locked login account shows deterministic error", async ({ page }) => {
            await basePage.smartAction("the login account is locked");
            await basePage.smartAction("I submit the login form with username \"locked_out_user\" and password \"\"", "locked_out_user");
            await basePage.smartAction("the error message \"Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.\" is displayed");
    });

    test("Invalid logins show deterministic errors for different reasons (username=standard_user, password=wrong_password, expected_error=Invalid password. Please try again.)", async ({ page }) => {
            await basePage.smartAction("I submit the login form with username \"standard_user\" and password \"wrong_password\"");
            await basePage.smartAction("the error message \"Invalid password. Please try again.\" is displayed");
    });

    test("Invalid logins show deterministic errors for different reasons (username=locked_out_user, expected_error=Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.)", async ({ page }) => {
            await basePage.smartAction("I submit the login form with username \"locked_out_user\" and password \"\"", "locked_out_user");
            await basePage.smartAction("the error message \"Your account is currently locked due to too many failed attempts. Please try again in 30 minutes.\" is displayed");
    });

    test("Invalid logins show deterministic errors for different reasons (username=random_user, password=incorrect_password, expected_error=Invalid username or password. Please try again.)", async ({ page }) => {
            await basePage.smartAction("I submit the login form with username \"random_user\" and password \"incorrect_password\"", "random_user");
            await basePage.smartAction("the error message \"Invalid username or password. Please try again.\" is displayed");
    });

});
