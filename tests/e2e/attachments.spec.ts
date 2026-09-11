import { expect, test } from "@playwright/test";
import { composeAttachmentText, type PromptAttachment } from "../../src/lib/attachments";
import { MockApi } from "./mock-api";

test("text attachments show filenames only during send and after history reload", async ({ page, context }, testInfo) => {
  const mock = new MockApi();
  const port = await mock.start();
  const text = "Please review these notes.";
  const attachments = [{ name: "notes.md", content: "# Hidden attachment body\n\n```js\nconst secret = 123;\n```" }];
  let received: { text: string; attachments: PromptAttachment[] } | undefined;
  let persisted = "";
  let accept!: () => void;
  const accepted = new Promise<void>((resolve) => { accept = resolve; });
  try {
    await context.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      return route.continue({ url: `http://127.0.0.1:${port}${url.pathname}${url.search}` });
    });
    await page.route("**/api/sessions/e2e-second/prompt", async (route) => {
      received = route.request().postDataJSON();
      await accepted;
      persisted = received!.text + composeAttachmentText(received!.attachments);
      await route.fulfill({ json: { composedText: persisted, requestState: "accepted" } });
    });
    await page.route("**/api/sessions/e2e-second/messages", (route) => route.fulfill({ json: {
      messages: persisted ? [{ id: "persisted-user", role: "user", content: persisted }, { id: "assistant", role: "assistant", content: "# Hidden attachment body" }] : [],
      promptRequestStates: {}, failedRequestIds: []
    } }));
    await page.goto("/");
    await page.locator(".session-item", { hasText: "E2E second session" }).click();
    await page.locator('.composer input[type="file"]').setInputFiles({ name: attachments[0].name, mimeType: "text/markdown", buffer: Buffer.from(attachments[0].content) });
    await expect(page.locator(".composer-attachment-name")).toHaveText("notes.md");
    await page.locator(".composer textarea").fill(text);
    await page.locator(".send-button").click();
    const userMessage = page.locator(".message.user");
    await expect(userMessage.locator(".message-attachments")).toHaveText("notes.md");
    await expect(userMessage.locator(".markdown")).toHaveText(text);
    await expect(userMessage).not.toContainText("Hidden attachment body");
    await expect.poll(() => received).toEqual({ text, mode: "prompt", requestId: expect.any(String), attachments });
    accept();
    await expect.poll(() => persisted).toContain(attachments[0].content);
    await page.reload();
    await page.locator(".session-item", { hasText: "E2E second session" }).click();
    await expect(userMessage).toHaveCount(1);
    await expect(userMessage.locator(".message-attachments")).toHaveText("notes.md");
    await expect(userMessage.locator(".markdown")).toHaveText(text);
    await expect(userMessage).not.toContainText("Hidden attachment body");
    await expect(page.locator(".message.assistant")).toContainText("Hidden attachment body");
    await page.screenshot({ path: testInfo.outputPath("attachments-desktop.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.locator(".sidebar")).not.toBeInViewport();
    await expect(userMessage.locator(".message-attachments")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("attachments-mobile.png") });
  } finally {
    accept();
    mock.close();
  }
});
