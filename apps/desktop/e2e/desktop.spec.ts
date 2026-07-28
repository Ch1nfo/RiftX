import { expect, test } from "@playwright/test";
import { installTauriMock } from "./tauri-mock";

test("new users complete model setup without editing configuration files", async ({
  page,
}) => {
  await page.setViewportSize({ width: 960, height: 640 });
  await installTauriMock(page, "firstRun");
  await page.goto("/");

  await expect(page.getByText("Finish model setup")).toBeVisible();
  await expect(page.getByRole("button", { name: "New task" }).first()).toBeDisabled();

  await page.getByRole("button", { name: "Open settings" }).first().click();
  await expect(page.getByText("First-time setup")).toBeVisible();
  await expect(page.getByText("Tools directories", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Model" }).click();

  const apiKey = page.getByLabel("API key", { exact: true });
  await expect(apiKey).toHaveAttribute("type", "password");
  await apiKey.fill("e2e-secret-must-not-render");
  await page.getByRole("button", { name: "Save key and test" }).click();

  await expect(page.getByText("Connection test passed", { exact: true })).toBeVisible();
  await expect(page.getByText(/function tools: passed/)).toBeVisible();
  await expect(page.getByText("e2e-secret-must-not-render")).toHaveCount(0);
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(page.getByText("Finish model setup")).toHaveCount(0);

  const newTaskButtons = page.getByRole("button", { name: "New task" });
  await expect(newTaskButtons).toHaveCount(2);
  for (let index = 0; index < (await newTaskButtons.count()); index += 1) {
    await expect(newTaskButtons.nth(index)).toBeEnabled();
  }
  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(horizontalOverflow).toBeLessThanOrEqual(0);
});

test("provider protocol failures are redacted and route back to Settings", async ({
  page,
}) => {
  await installTauriMock(page, "providerError");
  await page.goto("/");

  await page.getByRole("button", { name: "Open settings" }).first().click();
  await page.getByRole("tab", { name: "Model" }).click();
  await page.getByLabel("API key", { exact: true }).fill("local-input-secret");
  await page.getByRole("button", { name: "Save key and test" }).click();

  await expect(
    page.getByText("API key saved, but the connection test could not complete."),
  ).toBeVisible();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Model provider request failed");
  await expect(alert).toContainText("run Test connection");
  await expect(alert).not.toContainText("provider-secret");
  await expect(alert).not.toContainText("authorization");
  await expect(
    alert.getByRole("button", { name: "Open settings" }),
  ).toBeVisible();
});

test("runtime reconnect reconciles persisted safety and task state", async ({
  page,
}) => {
  await installTauriMock(page, "reconnect");
  await page.goto("/");

  await expect(page.getByText("Initial subgoal")).toBeVisible();
  await page.evaluate(() => {
    const browserWindow = window as typeof window & {
      __RIFTX_E2E__: { reconnect: () => void };
    };
    browserWindow.__RIFTX_E2E__.reconnect();
  });

  await expect(page.getByText("Kill Switch")).toBeVisible();
  await expect(page.getByText("Confirm the recovered target scope")).toBeVisible();
  await expect(
    page.getByText("Recovered conversation after reconnect"),
  ).toBeVisible();
  await expect(page.getByText("nmap -sV lab.example.test")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Interrupt execution" }),
  ).toBeVisible();
  await expect(page.getByText("live")).toBeVisible();
});
