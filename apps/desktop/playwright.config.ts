import { defineConfig } from "@playwright/test";

const configuredChannel = process.env.RIFTX_E2E_BROWSER_CHANNEL;
const channel =
  configuredChannel === "bundled"
    ? undefined
    : (configuredChannel ?? "chrome");

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  projects: [
    {
      name: "functional",
      grepInvert: /@visual/,
    },
    {
      name: "visual-960x640",
      grep: /@visual/,
      use: { viewport: { width: 960, height: 640 }, deviceScaleFactor: 1 },
    },
    {
      name: "visual-1440x900",
      grep: /@visual/,
      use: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
    },
    {
      name: "visual-windows-125",
      grep: /@visual/,
      use: { viewport: { width: 1152, height: 720 }, deviceScaleFactor: 1.25 },
    },
    {
      name: "visual-windows-150",
      grep: /@visual/,
      use: { viewport: { width: 960, height: 600 }, deviceScaleFactor: 1.5 },
    },
    {
      name: "visual-retina",
      grep: /@visual/,
      use: { viewport: { width: 960, height: 640 }, deviceScaleFactor: 2 },
    },
  ],
  use: {
    baseURL: "http://127.0.0.1:4173",
    channel,
    headless: true,
    screenshot: "only-on-failure",
    trace: "on-first-retry",
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: "pnpm dev --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
