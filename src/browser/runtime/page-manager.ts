import type { Page } from "playwright";
import { randomUUID } from "node:crypto";
import { attachRequestRecorder } from "../network/recorder";
import { RequestStore } from "../network/request-store";
import type { BrowserConsoleEntry, BrowserConsoleKind, BrowserPageInfo } from "../types";

const CONSOLE_LIMITS = { entries: 200, text: 2000 } as const;

export class PageManager {
  readonly id = randomUUID();
  private readonly consoleEntries: BrowserConsoleEntry[] = [];

  constructor(readonly page: Page, readonly identity: string, private readonly requests: RequestStore) {
    attachRequestRecorder(page, this.id, identity, requests);
    // Attach a dialog listener so alert/confirm/prompt are recorded instead of
    // being silently auto-dismissed by Playwright: captured dialogs are the
    // runtime proof for DOM-XSS payloads.
    page.on("console", (message) => {
      const kind: BrowserConsoleKind = message.type() === "error" ? "error" : message.type() === "warning" ? "warning" : "log";
      const location = message.location();
      this.pushConsole({ kind, text: message.text(), location: location.url ? `${location.url}:${location.lineNumber ?? 0}` : undefined });
    });
    page.on("pageerror", (error) => this.pushConsole({ kind: "pageerror", text: error.message }));
    page.on("dialog", (dialog) => {
      this.pushConsole({ kind: "dialog", text: `${dialog.type()}: ${dialog.message()}` });
      void dialog.dismiss().catch(() => undefined);
    });
  }

  private pushConsole(entry: { kind: BrowserConsoleKind; text: string; location?: string }) {
    this.consoleEntries.push({ id: randomUUID(), at: Date.now(), ...entry, text: entry.text.slice(0, CONSOLE_LIMITS.text) });
    if (this.consoleEntries.length > CONSOLE_LIMITS.entries) this.consoleEntries.splice(0, this.consoleEntries.length - CONSOLE_LIMITS.entries);
  }

  consoleLog(limit: number, kinds?: ReadonlySet<BrowserConsoleKind>, sinceMs?: number) {
    return this.consoleEntries
      .filter((entry) => (!kinds || kinds.has(entry.kind)) && (sinceMs === undefined || entry.at >= sinceMs))
      .slice(-limit)
      .map((entry) => `[${new Date(entry.at).toISOString()}] ${entry.kind}${entry.location ? ` ${entry.location}` : ""}: ${entry.text}`);
  }

  async info(active: boolean): Promise<BrowserPageInfo> {
    return { id: this.id, identity: this.identity, url: this.page.url(), title: await this.page.title().catch(() => ""), active };
  }
}
