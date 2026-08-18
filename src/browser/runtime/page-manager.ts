import type { Page } from "playwright";
import { randomUUID } from "node:crypto";
import { attachRequestRecorder } from "../network/recorder";
import { RequestStore } from "../network/request-store";
import type { BrowserPageInfo } from "../types";

export class PageManager {
  readonly id = randomUUID();

  constructor(readonly page: Page, private readonly requests: RequestStore) {
    attachRequestRecorder(page, this.id, requests);
  }

  async info(active: boolean): Promise<BrowserPageInfo> {
    return { id: this.id, url: this.page.url(), title: await this.page.title().catch(() => ""), active };
  }
}
