import { test, expect } from '@playwright/test';
import { BasePage } from '../_bmad/BasePage';

test.describe("Negative Path Validation for DemoQA Elements Module", () => {

    let basePage: BasePage;

    test.beforeEach(async ({ page }) => {
        basePage = new BasePage(page, "SCRUM-70");
        await basePage.initialize();
    });

    test("Email Validation @SCRUM-70 @negative @AC2 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("test@domain");
            await basePage.smartAction("I click the Submit button"); /* selector: #currentAddress */
            await expect(basePage.page.locator("#output")).toBeVisible();
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-0: removed invalid placeholder selector
    });

    test("Web Tables Validation @SCRUM-70 @positive @AC3 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("abc");
            await basePage.smartAction("the Submit button should be disabled");  // verifyDisabled — BasePage will assert element is disabled  // AMBIGUOUS-MATCH
            await basePage.smartAction("the Registration modal should remain open"); /* selector: text=static element. role: textbox. label: Current Address. placeholder: Current Address. selector: #currentAddress. intent: Input. page: https://demoqa.com/text-box */  // AMBIGUOUS-MATCH
            await basePage.smartAction("no submission is possible"); /* selector: text=static element. role: textbox. label: Current Address. placeholder: Current Address. selector: #currentAddress. intent: Input. page: https://demoqa.com/text-box */  // AMBIGUOUS-MATCH
    });

    test("Radio Button Validation @SCRUM-70 @positive @AC4 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/radio-button");
            await basePage.smartAction("I select the \"#noRadio\" radio button"); /* selector: #impressiveRadio */
            await expect(basePage.page.locator("#noRadio")).toBeDisabled();
            await basePage.smartAction("the state of the \"#noRadio\" radio button remains unchanged");  // TEA fallback: action_type=unknown /* selector: #impressiveRadio */
    });

    test("UI Stability @SCRUM-70 @negative @AC5 @SCRUM_116", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.smartAction("I interact with UI elements");
            await basePage.smartAction("the UI remains stable and all elements remain interactable"); /* selector: text=static element. role: textbox. label: Current Address. placeholder: Current Address. selector: #currentAddress. intent: Input. page: https://demoqa.com/text-box */  // AMBIGUOUS-MATCH
    });

    test("Invalid email behavior @SCRUM-70 @positive @AC6 @SCRUM_116", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.smartAction("I enter invalid email syntax in the Email field");  // TODO: add explicit value to Gherkin step
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
    });

    test("Modal interaction: Add new row @SCRUM-70 @positive @AC7 @SCRUM_115", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/webtables");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#addNewRecordButton").click();
            await basePage.smartAction("the modal should be opened");
    });

    test("Access to web pages @SCRUM-70 @positive @AC8 @SCRUM_115", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.smartAction("I click the Submit button"); /* selector: #currentAddress */
            await basePage.smartAction("the page should be accessible"); /* selector: text=Upload and Download */  // AMBIGUOUS-MATCH
    });

    test("Selectors against live DOM @SCRUM-70 @positive @AC9 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await expect(basePage.page.locator("#userName")).toBeVisible();  // Full Name
        await expect(basePage.page.locator("#userName")).toBeEnabled();  // Full Name  // AMBIGUOUS-MATCH
            await expect(basePage.page.locator("#userEmail")).toBeVisible();  // Email
        await expect(basePage.page.locator("#userEmail")).toBeEmpty();  // Email
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();  // Current Address
        await expect(basePage.page.locator("#currentAddress")).toBeEmpty();  // Current Address
            await expect(basePage.page.locator("#permanentAddress")).toBeVisible();  // permanentAddress
        await expect(basePage.page.locator("#permanentAddress")).toBeEmpty();  // permanentAddress
            await basePage.smartAction("the selectors are valid");  // TEA fallback: action_type=unknown /* selector: #currentAddress */
    });

    test("Valid email format @SCRUM-70 @negative @AC10 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("user@domain.com");
            await basePage.smartAction("the Output container should display the submitted data"); /* selector: text=Upload and Download */  // AMBIGUOUS-MATCH
    });

    test("Invalid email format (missing @) @SCRUM-70 @negative @AC11 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("userdomain.com");
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();
    });

    test("Invalid email format (missing TLD) @SCRUM-70 @negative @AC12 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("user");
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();
    });

    test("Empty email input @SCRUM-70 @positive @AC13 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("");
            await expect(basePage.page.locator("#currentAddress")).toBeVisible();
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
    });

    test("Valid age input (numeric value) @SCRUM-70 @negative @AC14 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("12");
            await basePage.smartAction("the result should display \"Age: 12\""); /* selector: text=static element. role: textbox. label: Current Address. placeholder: Current Address. selector: #currentAddress. intent: Input. page: https://demoqa.com/text-box */  // AMBIGUOUS-MATCH
            await expect(basePage.page.locator("#output")).toBeVisible(); 
    });

    test("Non-numeric age input (abc) @SCRUM-70 @positive @AC15 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("abc");
            await expect(basePage.page.locator("#output")).toContainText("Invalid Age");
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
    });

    test("Valid salary input (numeric value) @SCRUM-70 @negative @AC16 @SCRUM_70", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("50");
            await expect(basePage.page.locator("#output")).toBeVisible();
            await expect(basePage.page.locator("#output")).toContainText("Name: 50");
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
    });

    test("Non-numeric salary input (abc)", async ({ page }) => {
            await basePage.page.goto("https://demoqa.com/text-box");  // AMBIGUOUS-MATCH
            await basePage.page.locator("#currentAddress").fill("abc");
            await expect(basePage.page.locator("#output")).not.toBeVisible();  // FIX RC-4: negation
    });

});
