import { chromium, type Browser, type BrowserContext } from "playwright";

const CONTEXT_CLOSE_TIMEOUT_MS = 10_000;

/**
 * One isolated browser context per identity. Contexts own their cookie jar and
 * storage, so parallel identities (anonymous / low-privilege / admin) never
 * share authenticated state.
 */
export class ContextManager {
  private browser?: Browser;
  private browserPromise?: Promise<Browser>;
  private defaultUA?: string;
  private readonly contexts = new Map<string, BrowserContext>();
  private readonly contextPromises = new Map<string, Promise<BrowserContext>>();

  private async ensureBrowser() {
    if (this.browser) return this.browser;
    if (!this.browserPromise) {
      const launch = chromium.launch({ headless: true });
      this.browserPromise = launch.then((browser) => {
        this.browser = browser;
        return browser;
      }).catch((error) => {
        this.browserPromise = undefined;
        throw error;
      });
    }
    return this.browserPromise;
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
    const pending = this.contextPromises.get(identity);
    if (pending) return pending;
    const creation = this.ensureBrowser().then((browser) => browser.newContext({
      serviceWorkers: "block",
      ignoreHTTPSErrors: options?.ignoreHTTPSErrors ?? true,
      ...(options?.proxyUrl ? { proxy: { server: options.proxyUrl } } : {})
    })).then(async (context) => {
      if (this.contextPromises.get(identity) !== creation) {
        await context.close().catch(() => undefined);
        throw new Error("Browser context was closed during initialization");
      }
      this.contextPromises.delete(identity);
      this.contexts.set(identity, context);
      return context;
    }).catch((error) => {
      if (this.contextPromises.get(identity) === creation) this.contextPromises.delete(identity);
      throw error;
    });
    this.contextPromises.set(identity, creation);
    return creation;
  }

  /** Returns false when any close failed: the execution contexts may still be alive. */
  async close(): Promise<boolean> {
    let closed = true;
    const contexts = [...this.contexts.values()];
    const browser = this.browser;
    const browserPromise = this.browserPromise;
    this.contexts.clear();
    this.contextPromises.clear();
    this.browserPromise = undefined;
    this.browser = undefined;
    const cleanup = (async () => {
      await Promise.all(contexts.map((context) => context.close().catch(() => { closed = false; })));
      const activeBrowser = browser ?? await browserPromise?.catch(() => undefined);
      await activeBrowser?.close().catch(() => { closed = false; });
      return closed;
    })();
    // Playwright protocol shutdown can itself wedge. Report an unconfirmed
    // close within a finite period; callers mark the manager degraded and
    // refuse to launch a second browser beside possibly-live resources.
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), CONTEXT_CLOSE_TIMEOUT_MS);
      cleanup.then(
        (result) => { clearTimeout(timer); resolve(result); },
        () => { clearTimeout(timer); resolve(false); }
      );
    });
  }
}
