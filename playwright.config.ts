import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright Configuration for OrangeHRM Leave Management Tests
 * 
 * Timeout Configuration:
 * - actionTimeout: 15000 (15s) - Survives demo site lag for individual actions
 * - global timeout: 60000 (60s) - Allows for full test flow including navigation
 */
export default defineConfig({
  testDir: './tests',
  
  // Timeout configuration to survive OrangeHRM demo site lag
  timeout: 60000, // Global test timeout: 60 seconds
  expect: {
    timeout: 15000 // Expect assertion timeout: 15 seconds
  },
  
  // Action timeout for all page actions (click, fill, etc.)
  use: {
    actionTimeout: 15000, // 15 seconds for each action
    navigationTimeout: 30000, // 30 seconds for navigation
    baseURL: process.env.BASE_URL || 'https://opensource-demo.orangehrmlive.com',
    
    // Browser context options
    viewport: { width: 1920, height: 1080 },
    
    // Screenshot on failure for debugging
    screenshot: 'only-on-failure',
    
    // Video recording for failed tests
    video: 'retain-on-failure',
    
    // Trace recording for debugging
    trace: 'retain-on-failure',
  },

  // Reporter configuration
  reporter: [
    ['html', { outputFolder: 'playwright-report' }],
    ['list'],
    ['json', { outputFile: 'test-results/results.json' }],
  ],

  // Shared settings for all browsers
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    
    // Optional: Run tests in other browsers
    // {
    //   name: 'firefox',
    //   use: { ...devices['Desktop Firefox'] },
    // },
    // {
    //   name: 'webkit',
    //   use: { ...devices['Desktop Safari'] },
    // },
  ],

  // Run your local dev server before starting the tests
  // webServer: {
  //   command: 'npm run start',
  //   url: 'http://127.0.0.1:3000',
  //   reuseExistingServer: !process.env.CI,
  // },

  // Worker configuration
  workers: process.env.CI ? 1 : undefined, // Run single worker in CI, auto in local
  
  // Fully parallel test execution
  fullyParallel: false, // Set to true for faster execution (may cause issues with OrangeHRM demo)
  
  // Fail the build on CI if you accidentally left test.only in the source code
  forbidOnly: !!process.env.CI,
  
  // Retry on CI only
  retries: process.env.CI ? 2 : 0,
});