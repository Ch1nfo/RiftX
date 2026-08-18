import { chromium, type Browser, type BrowserContext } from "playwright";

export class ContextManager {
  private browser?: Browser;
  private context?: BrowserContext;

  async getContext() {
    if (!this.context) {
      this.browser = await chromium.launch({ headless: true });
      this.context = await this.browser.newContext({ serviceWorkers: "block" });
    }
    return this.context;
  }

  async close() {
    await this.context?.close().catch(() => undefined);
    await this.browser?.close().catch(() => undefined);
    this.context = undefined;
    this.browser = undefined;
  }
}
