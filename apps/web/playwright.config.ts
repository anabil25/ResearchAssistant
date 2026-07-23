import { defineConfig, devices } from "@playwright/test";

const deployedBaseUrl = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  timeout: deployedBaseUrl ? 300_000 : 30_000,
  expect: {
    timeout: deployedBaseUrl ? 240_000 : 5_000,
  },
  use: {
    baseURL: deployedBaseUrl ?? "http://127.0.0.1:3000",
    trace: "on-first-retry",
  },
  webServer: deployedBaseUrl
    ? undefined
    : [
        {
          command:
            "uv --directory ../.. run --package research-assistant-connector-adapter uvicorn research_assistant_connector_adapter.app:app --host 127.0.0.1 --port 8200",
          url: "http://127.0.0.1:8200/health",
          reuseExistingServer: !process.env.CI,
        },
        {
          command:
            "uv --directory ../.. run --package research-assistant-api uvicorn research_assistant_api.app:app --host 127.0.0.1 --port 8100",
          env: {
            RESEARCH_CONNECTOR_GATEWAY_URL: "http://127.0.0.1:8200",
          },
          url: "http://127.0.0.1:8100/health",
          reuseExistingServer: !process.env.CI,
        },
        {
          command: "npm run start",
          env: {
            INTERNAL_API_URL: "http://127.0.0.1:8100",
          },
          url: "http://127.0.0.1:3000",
          reuseExistingServer: !process.env.CI,
        },
      ],
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
