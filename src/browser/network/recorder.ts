import type { Page, Request, Response } from "playwright";
import { redactBody, redactHeaders, RequestStore } from "./request-store";

function attach(page: Page, pageId: string, identity: string, store: RequestStore) {
  page.on("request", (request: Request) => {
    const record = store.start({
      pageId,
      identity,
      method: request.method(),
      url: request.url(),
      resourceType: request.resourceType(),
      requestHeaders: redactHeaders(request.headers()),
      requestBody: redactBody(request.postData()),
      startedAt: new Date().toISOString()
    });
    (request as Request & { __riftxRef?: string }).__riftxRef = record.ref;
  });
  page.on("requestfinished", (request: Request) => {
    const ref = (request as Request & { __riftxRef?: string }).__riftxRef;
    if (ref) store.update(ref, { durationMs: Date.now() - Date.parse(store.get(ref)?.startedAt ?? new Date().toISOString()) });
  });
  page.on("requestfailed", (request: Request) => {
    const ref = (request as Request & { __riftxRef?: string }).__riftxRef;
    if (ref) store.update(ref, { durationMs: Date.now() - Date.parse(store.get(ref)?.startedAt ?? new Date().toISOString()) });
  });
  page.on("response", async (response: Response) => {
    const request = response.request() as Request & { __riftxRef?: string };
    const ref = request.__riftxRef;
    if (!ref) return;
    let responseBody: string | undefined;
    try { responseBody = redactBody(await response.text()); } catch { /* opaque or already disposed */ }
    store.update(ref, {
      status: response.status(),
      statusText: response.statusText(),
      responseHeaders: redactHeaders(response.headers()),
      responseBody,
      durationMs: Date.now() - Date.parse(store.get(ref)?.startedAt ?? new Date().toISOString())
    });
  });
}

export function attachRequestRecorder(page: Page, pageId: string, identity: string, store: RequestStore) {
  attach(page, pageId, identity, store);
}
