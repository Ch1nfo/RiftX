import test, { expect, type BrowserContext, type Page } from "@playwright/test";
import { MockApi } from "./mock-api";

test.setTimeout(180_000);

/**
 * Browser-level regression for the long-standing "auto-follow silently dies
 * on long conversations" bug: with the viewport untouched, streamed updates
 * (tall growth + thinking/tool auto-collapse + the 200-message window
 * sliding) must keep the conversation pinned to the latest message. A user
 * scroll-up must still pause following with the jump affordance, and the jump
 * button must re-engage it for later rounds.
 */
test.describe("conversation auto-follow", () => {
  let mock: MockApi;
  let mockPort: number;

  test.beforeAll(async () => {
    mock = new MockApi();
    mockPort = await mock.start();
  });

  test.afterAll(() => mock.close());

  const routeApiToMock = (context: BrowserContext) =>
    context.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      return route.continue({ url: `http://127.0.0.1:${mockPort}${url.pathname}${url.search}` });
    });

  test("follows streaming without user input and honors intentional scroll-up", async ({ page, context }) => {
    await routeApiToMock(context);
    await page.setViewportSize({ width: 900, height: 700 });
    await page.goto("/");
    // The 210 seed messages render (the window shows the latest 200, so the
    // load-earlier control is present and every append slides the window).
    await page.waitForFunction(() => document.querySelectorAll(".conversation .message").length >= 200);
    await expect(page.locator(".load-earlier")).toBeVisible();

    // Phase A: the scripted stream replays the kill conditions repeatedly.
    await mock.phaseADone;

    // No user interaction happened: still bottomed, no jump button. This is
    // the assertion the pre-fix build failed — an anchoring adjustment during
    // the one-frame pin lag classified as an upward scroll and permanently
    // disabled follow.
    await expectBottomed(page, true);
    await expect(page.locator(".jump-latest")).toHaveCount(0);

    // The user scrolls up: following pauses and the jump affordance appears.
    const box = await page.locator(".conversation").boundingBox();
    await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
    await page.mouse.wheel(0, -600);
    await expect(page.locator(".jump-latest")).toBeVisible({ timeout: 5_000 });

    // Jumping back re-engages follow, and the remaining rounds keep it.
    await page.locator(".jump-latest").click();
    await expectBottomed(page, true);
    mock.releasePhaseC();
    await mock.allDone;
    await expectBottomed(page, true);
    await expect(page.locator(".jump-latest")).toHaveCount(0);
  });
});

async function expectBottomed(page: Page, bottomed: boolean) {
  await expect
    .poll(async () => page.evaluate(() => {
      const el = document.querySelector(".conversation") as HTMLElement | null;
      return el ? el.scrollTop >= el.scrollHeight - el.clientHeight - 1 : false;
    }), { timeout: 10_000, message: `conversation expected bottomed=${bottomed}` })
    .toBe(bottomed);
}
