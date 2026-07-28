import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { installTauriMock } from "./tauri-mock";

const FORBIDDEN_BRANDS = ["OpenAI", "ChatGPT", "Codex"];

async function assertDesktopLayout(page: Page): Promise<void> {
  const layout = await page.evaluate(() => {
    const rectangle = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector);
      if (!element) {
        return null;
      }
      const bounds = element.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) {
        return null;
      }
      return {
        selector,
        left: bounds.left,
        right: bounds.right,
        top: bounds.top,
        bottom: bounds.bottom,
      };
    };
    const columns = [
      rectangle(".task-sidebar"),
      rectangle(".conversation"),
      rectangle(".inspector"),
    ].filter((value) => value !== null);
    const overlaps: string[] = [];
    for (let index = 1; index < columns.length; index += 1) {
      const previous = columns[index - 1];
      const current = columns[index];
      if (previous.right > current.left + 1) {
        overlaps.push(`${previous.selector} overlaps ${current.selector}`);
      }
    }
    const dialog = rectangle(".settings-dialog");
    return {
      horizontalOverflow:
        document.documentElement.scrollWidth - document.documentElement.clientWidth,
      verticalOverflow:
        document.documentElement.scrollHeight - document.documentElement.clientHeight,
      overlaps,
      dialogOutsideViewport:
        dialog !== null &&
        (dialog.left < -1 ||
          dialog.right > window.innerWidth + 1 ||
          dialog.top < -1 ||
          dialog.bottom > window.innerHeight + 1),
      bodyText: document.body.innerText,
    };
  });

  expect(layout.horizontalOverflow).toBeLessThanOrEqual(1);
  expect(layout.verticalOverflow).toBeLessThanOrEqual(1);
  expect(layout.overlaps).toEqual([]);
  expect(layout.dialogOutsideViewport).toBe(false);
  for (const brand of FORBIDDEN_BRANDS) {
    expect(layout.bodyText).not.toContain(brand);
  }
}

async function captureEvidence(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({
    path,
    animations: "disabled",
    fullPage: false,
  });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

test("@visual Desktop workbench and Settings remain stable under scaling", async ({
  page,
}, testInfo) => {
  await installTauriMock(page, "visualStress");
  await page.goto("/");

  await expect(
    page.getByText(
      "在明确授权的实验室范围内验证超长中文目标、命令、模型名称和审批信息在桌面工作台中保持可读且不会发生布局重叠。",
      { exact: true },
    ).first(),
  ).toBeVisible();
  await expect(page.getByText(/very-long-authorized-validation-script/)).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  await assertDesktopLayout(page);
  await captureEvidence(page, testInfo, "workbench");

  await page.getByRole("button", { name: "Open settings" }).click();
  await expect(
    page.getByRole("textbox", { name: "Model", exact: true }),
  ).toBeVisible();
  await assertDesktopLayout(page);
  await captureEvidence(page, testInfo, "model-settings");
});
