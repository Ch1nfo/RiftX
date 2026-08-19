import { URL } from "node:url";
import type { Page } from "playwright";
import { ContextManager } from "./context-manager";
import { PageManager } from "./page-manager";
import { createSnapshot } from "../snapshot/snapshot";
import { ElementRefMapper } from "../snapshot/element-refs";
import { RequestStore, redactHeaders } from "../network/request-store";
import type { BrowserManagerOptions, BrowserPageInfo, BrowserScope, PageSnapshot } from "../types";

function parseAllowedOrigins(value?: string) {
  return value?.split(",").map((item) => item.trim()).filter(Boolean) ?? [];
}

export class BrowserManager {
  private readonly contextManager = new ContextManager();
  private readonly requests = new RequestStore();
  private readonly pages = new Map<string, PageManager>();
  private readonly refs = new Map<string, ElementRefMapper>();
  private activeId?: string;
  private contextListenerAttached = false;
  private lockedOrigin?: string;
  private scope: BrowserScope;

  constructor(private readonly options: BrowserManagerOptions) {
    this.scope = {
      allowedOrigins: options.scope?.allowedOrigins ?? parseAllowedOrigins(process.env.RIFTX_BROWSER_ALLOWED_ORIGINS),
      allowedPaths: options.scope?.allowedPaths,
      allowSubdomains: options.scope?.allowSubdomains ?? false
    };
  }

  private registerPage(page: Page) {
    const existing = [...this.pages.values()].find((item) => item.page === page);
    if (existing) return existing;
    void page.route("**/*", async (route) => {
      try {
        this.assertInScope(route.request().url());
        await route.continue();
      } catch {
        await route.abort("blockedbyclient");
      }
    });
    const manager = new PageManager(page, this.requests);
    this.pages.set(manager.id, manager);
    this.refs.set(manager.id, new ElementRefMapper());
    page.on("close", () => {
      this.pages.delete(manager.id);
      this.refs.delete(manager.id);
      if (this.activeId === manager.id) this.activeId = this.pages.keys().next().value;
    });
    return manager;
  }

  private async ensurePage() {
    const context = await this.contextManager.getContext();
    if (!this.contextListenerAttached) {
      this.contextListenerAttached = true;
      context.on("page", (page) => { this.registerPage(page); });
    }
    if (!this.activeId || !this.pages.has(this.activeId)) {
      const page = await context.newPage();
      const manager = this.registerPage(page);
      this.activeId = manager.id;
    }
    return this.pages.get(this.activeId)!;
  }

  private pageManager(id?: string) {
    const manager = id ? this.pages.get(id) : this.pages.get(this.activeId ?? "");
    if (!manager) throw new Error("No browser page is open. Run browser navigate first.");
    return manager;
  }

  private assertInScope(rawUrl: string) {
    let target: URL;
    try { target = new URL(rawUrl); } catch { throw new Error("Browser navigation requires an absolute URL"); }
    if (!/^https?:$/.test(target.protocol)) throw new Error("Browser navigation only supports http(s) URLs");
    const origins = this.scope.allowedOrigins?.length ? this.scope.allowedOrigins : this.lockedOrigin ? [this.lockedOrigin] : [];
    if (!origins.length) return target.toString();
    const allowed = origins.some((origin) => {
      try {
        const parsed = new URL(origin);
        return target.origin === parsed.origin || (this.scope.allowSubdomains && target.hostname.endsWith(`.${parsed.hostname}`) && target.protocol === parsed.protocol && target.port === parsed.port);
      } catch { return false; }
    });
    if (!allowed) throw new Error(`Navigation blocked by RiftX scope: ${target.origin} is not allowed`);
    if (this.scope.allowedPaths?.length && !this.scope.allowedPaths.some((path) => target.pathname.startsWith(path))) {
      throw new Error(`Navigation blocked by RiftX scope: ${target.pathname} is not allowed`);
    }
    return target.toString();
  }

  async navigate(url: string) {
    const target = this.assertInScope(url);
    if (!this.scope.allowedOrigins?.length && !this.lockedOrigin) this.lockedOrigin = new URL(target).origin;
    const manager = await this.ensurePage();
    await manager.page.goto(target, { waitUntil: "domcontentloaded", timeout: 30_000 });
    if (this.scope.allowedOrigins?.length) this.assertInScope(manager.page.url());
    return this.snapshot();
  }

  async snapshot(): Promise<PageSnapshot> {
    const manager = await this.ensurePage();
    return createSnapshot(manager.page, this.refs.get(manager.id)!);
  }

  private locator(ref: string) {
    const manager = this.pageManager();
    const element = this.refs.get(manager.id)?.get(ref);
    if (!element) throw new Error(`Unknown or stale element ref ${ref}; call browser snapshot again`);
    return manager.page.locator(element.selector).first();
  }

  async click(ref: string) { await this.locator(ref).click(); return this.snapshot(); }
  async fill(ref: string, value: string) { await this.locator(ref).fill(value); return this.snapshot(); }
  async press(ref: string, key: string) { await this.locator(ref).press(key); return this.snapshot(); }
  async select(ref: string, values: string[]) { await this.locator(ref).selectOption(values); return this.snapshot(); }
  async back() { await this.pageManager().page.goBack({ waitUntil: "domcontentloaded" }).catch(() => undefined); return this.snapshot(); }
  async reload() { await this.pageManager().page.reload({ waitUntil: "domcontentloaded" }); return this.snapshot(); }

  async requestsList() {
    return this.requests.list().map((item) => `${item.ref} ${item.method.padEnd(6)} ${item.url} ${item.status ?? "pending"}`).join("\n") || "(no requests recorded)";
  }

  requestDetail(ref: string) {
    const item = this.requests.get(ref);
    if (!item) throw new Error(`Unknown request ref ${ref}`);
    const requestHeaders = Object.entries(redactHeaders(item.requestHeaders)).map(([key, value]) => `${key}: ${value}`).join("\n");
    const responseHeaders = Object.entries(item.responseHeaders ?? {}).map(([key, value]) => `${key}: ${value}`).join("\n");
    return [`${item.method} ${item.url} HTTP/1.1`, requestHeaders ? `\n${requestHeaders}` : "", item.requestBody ? `\n\n${item.requestBody}` : "", `\n\nResponse:\n${item.status ?? "pending"} ${item.statusText ?? ""}`, responseHeaders ? `\n${responseHeaders}` : ""].join("");
  }

  responseBody(ref: string) {
    const item = this.requests.get(ref);
    if (!item) throw new Error(`Unknown request ref ${ref}`);
    return item.responseBody ?? "(response body unavailable or still pending)";
  }

  async cookies() {
    const context = await this.contextManager.getContext();
    return JSON.stringify(await context.cookies(), null, 2);
  }

  async storage() {
    const page = this.pageManager().page;
    const storage = await page.evaluate(() => {
      try {
        return { localStorage: Object.fromEntries(Object.entries(localStorage)), sessionStorage: Object.fromEntries(Object.entries(sessionStorage)) };
      } catch {
        return { localStorage: {}, sessionStorage: {} };
      }
    });
    return JSON.stringify(storage, null, 2);
  }

  async screenshot() {
    const page = this.pageManager().page;
    return (await page.screenshot({ type: "png" })).toString("base64");
  }

  async tabs(): Promise<BrowserPageInfo[]> {
    return Promise.all([...this.pages.entries()].map(([id, manager]) => manager.info(id === this.activeId)));
  }

  async close() {
    this.refs.clear();
    this.pages.clear();
    this.activeId = undefined;
    this.lockedOrigin = undefined;
    this.contextListenerAttached = false;
    await this.contextManager.close();
  }

  get currentUrl() {
    return this.pages.get(this.activeId ?? "")?.page.url() ?? "about:blank";
  }
}
