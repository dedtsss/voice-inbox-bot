const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/browser",
  testMatch: "**/*.spec.js",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["line"]],
  use: {
    baseURL: "http://127.0.0.1:8765",
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "PYTHONPATH=src:. python -m uvicorn tests.browser.fixture_server:app --host 127.0.0.1 --port 8765",
    url: "http://127.0.0.1:8765/healthz",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
