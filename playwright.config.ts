import { defineConfig } from "@playwright/test";

const port = 3123;

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 180_000,
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: `npm run build && npx next start -p ${port}`,
    url: `http://127.0.0.1:${port}`,
    // Never reuse: a stale server on the port would test old code and pass.
    // This suite is the safety net for risky refactors — correctness first.
    reuseExistingServer: false,
    timeout: 300_000
  }
});
