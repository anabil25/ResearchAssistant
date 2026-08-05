import { defineConfig, devices } from "@playwright/test";


const deployedBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const localBaseUrl = "http://127.0.0.1:3000";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 300_000,
  expect: {
    timeout: 30_000,
  },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: deployedBaseUrl ?? localBaseUrl,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "tablet",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 820, height: 1180 },
        hasTouch: true,
      },
    },
    {
      name: "mobile",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 390, height: 844 },
        hasTouch: true,
        isMobile: true,
      },
    },
  ],
  webServer: deployedBaseUrl
    ? undefined
    : {
        command: "npm run dev -- --hostname 127.0.0.1 --port 3000",
        url: localBaseUrl,
        reuseExistingServer: true,
        timeout: 120_000,
      },
});
