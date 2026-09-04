import { expect, test, type Page } from "@playwright/test";
import { DEFAULT_PROFILE } from "../../src/lib/types";

async function mockSettingsPage(page: Page) {
  await page.addInitScript(() => window.localStorage.setItem("riftx-language", "en"));
  await page.route("**/api/settings/model-profiles", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({
      profiles: [DEFAULT_PROFILE],
      activeProfileId: DEFAULT_PROFILE.id,
      childProfileId: null,
      childInherit: true,
      maxConcurrentSubagents: 3,
      subagentAggressiveness: "default",
      systemPromptEnabled: false,
      systemPrompt: "",
      browserScope: [],
      browserIgnoreTlsErrors: true,
      mcpServers: []
    })
  }));
}

function archivedSession(id: string, name: string, restoreBlock?: "wrong-workspace" | "missing") {
  return {
    id,
    path: `/tmp/${id}.jsonl`,
    name,
    firstMessage: "inspect target",
    updatedAt: "2026-09-04T00:00:00.000Z",
    archived: true,
    ...(restoreBlock ? { restoreBlock } : {})
  };
}

test("restores an archived session without deleting its data", async ({ page }) => {
  const archived = archivedSession("archived-1", "Archived investigation");
  let restoreMethod = "";
  let deletedSession = false;

  await mockSettingsPage(page);
  await page.route("**/api/sessions/archived-1/archive", (route) => {
    restoreMethod = route.request().method();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ sessions: [{ ...archived, archived: false }] }) });
  });
  await page.route("**/api/sessions/archived-1", (route) => {
    if (route.request().method() === "DELETE") deletedSession = true;
    return route.fulfill({ contentType: "application/json", body: "{}" });
  });
  await page.route("**/api/sessions", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify([archived]) }));

  await page.goto("/settings");
  await page.locator('a[href="#archived"]').click();
  await expect(page.getByText("Archived investigation")).toBeVisible();
  await expect(page.getByText(/can be restored or permanently deleted/).first()).toBeVisible();

  await page.getByRole("button", { name: "Restore session Archived investigation" }).click();

  await expect.poll(() => restoreMethod).toBe("DELETE");
  expect(deletedSession).toBe(false);
  await expect(page.getByText("Archived investigation")).toHaveCount(0);
});

test("marks other-workspace and missing archived sessions as unrestorable", async ({ page }) => {
  let restoreCalled = false;
  await mockSettingsPage(page);
  await page.route("**/api/sessions/**/archive", (route) => {
    restoreCalled = true;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ sessions: [] }) });
  });
  await page.route("**/api/sessions", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify([
      archivedSession("local-1", "Local archived"),
      archivedSession("other-1", "Other workspace", "wrong-workspace"),
      archivedSession("gone-1", "Missing session", "missing")
    ])
  }));

  await page.goto("/settings");
  await page.locator('a[href="#archived"]').click();

  await expect(page.getByRole("button", { name: "Restore session Local archived" })).toBeEnabled();
  await expect(page.getByRole("button", { name: /Restore session Other workspace/ })).toBeDisabled();
  await expect(page.getByText("Switch to this session's working directory before restoring.")).toBeVisible();
  await expect(page.getByRole("button", { name: /Restore session Missing session/ })).toBeDisabled();
  await expect(page.getByText("This session's data is no longer available and cannot be restored.")).toBeVisible();
  expect(restoreCalled).toBe(false);
});
