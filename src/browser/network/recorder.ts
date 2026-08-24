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
    // Only responses with a known, small length and a textual media type are
    // captured automatically: response.text() buffers the whole body first,
    // so chunked/large/unknown-size payloads would spike memory. A missing or
    // unparsable Content-Length means skip — never assume it is small.
    const headers = response.headers();
    const contentType = String(headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
    const lengthText = String(headers["content-length"] ?? "").trim();
    const contentLength = /^\d+$/.test(lengthText) ? Number(lengthText) : NaN;
    const binaryPayload = ["image", "media", "font"].includes(request.resourceType())
      || /^(image|video|audio|font)\//.test(contentType)
      || contentType === "application/octet-stream";
    if (!binaryPayload && Number.isFinite(contentLength) && contentLength >= 0 && contentLength <= 262144) {
      try { responseBody = redactBody(await response.text()); } catch { /* opaque or already disposed */ }
    }
    store.update(ref, {
      status: response.status(),
      statusText: response.statusText(),
      responseHeaders: redactHeaders(headers),
      responseBody,
      durationMs: Date.now() - Date.parse(store.get(ref)?.startedAt ?? new Date().toISOString())
    });
  });
}

export function attachRequestRecorder(page: Page, pageId: string, identity: string, store: RequestStore) {
  attach(page, pageId, identity, store);
}
