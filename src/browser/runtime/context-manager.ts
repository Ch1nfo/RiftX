import { chromium, type Browser, type BrowserContext } from "playwright";

/**
 * One isolated browser context per identity. Contexts own their cookie jar and
 * storage, so parallel identities (anonymous / low-privilege / admin) never
 * share authenticated state.
 */
export class ContextManager {
  private browser?: Browser;
  private defaultUA?: string;
  private readonly contexts = new Map<string, BrowserContext>();

  private async ensureBrowser() {
    if (!this.browser) this.browser = await chromium.launch({ headless: true });
    return this.browser;
  }

  /** The browser's stock User-Agent, used to restore pages after an override is cleared. */
  async defaultUserAgent() {
    if (this.defaultUA) return this.defaultUA;
    const browser = await this.ensureBrowser();
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      this.defaultUA = await page.evaluate(() => navigator.userAgent);
    } finally {
      await context.close().catch(() => undefined);
    }
    return this.defaultUA;
  }

  async getContext(identity: string, options?: { ignoreHTTPSErrors?: boolean; proxyUrl?: string }): Promise<BrowserContext> {
    const existing = this.contexts.get(identity);
    if (existing) return existing;
    const browser = await this.ensureBrowser();
    const context = await browser.newContext({
      serviceWorkers: "block",
      ignoreHTTPSErrors: options?.ignoreHTTPSErrors ?? true,
      ...(options?.proxyUrl ? { proxy: { server: options.proxyUrl } } : {})
    });
    this.contexts.set(identity, context);
    return context;
  }

  async close() {
    const contexts = [...this.contexts.values()];
    this.contexts.clear();
    await Promise.all(contexts.map((context) => context.close().catch(() => undefined)));
    await this.browser?.close().catch(() => undefined);
    this.browser = undefined;
  }
}
