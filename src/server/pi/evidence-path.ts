import { join, resolve } from "node:path";

const SCREENSHOT_ID = /^s-[0-9a-f-]{36}$/;

export function isScreenshotId(value: string) {
  return SCREENSHOT_ID.test(value);
}

export function getScreenshotPath(root: string, sessionId: string, screenshotId: string) {
  if (!isScreenshotId(screenshotId)) throw new Error("Invalid screenshot id");
  const shotsDirectory = resolve(join(root, sessionId, "shots"));
  const path = resolve(join(shotsDirectory, `${screenshotId}.png`));
  if (path !== join(shotsDirectory, `${screenshotId}.png`) || !path.startsWith(`${shotsDirectory}/`)) {
    throw new Error("Invalid screenshot path");
  }
  return path;
}
