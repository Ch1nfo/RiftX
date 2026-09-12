import type { Page } from "playwright";
import { StringDecoder } from "node:string_decoder";
import { boundedBody, MAX_CAPTURE_BYTES, RequestStore } from "./request-store";
import type { RecordedRequest } from "../types";

const CAPTURE_TIMEOUT_MS = 60_000;
const MAX_ACTIVE_CAPTURES = 200;

/** Observe Chromium's original response without consuming it or replaying a POST. */
export async function attachRequestRecorder(page: Page, pageId: string, identity: string, store: RequestStore) {
  const session = await page.context().newCDPSession(page);
  type Capture = {
    ref: string;
    bytes: number;
    observedBytes: number;
    decoder: StringDecoder;
    timer: ReturnType<typeof setTimeout>;
    ready?: Promise<void>;
    prefixReady: boolean;
    pending: string[];
    pendingBytes: number;
    overflow: boolean;
    fallbackToCache: boolean;
  };
  const active = new Map<string, Capture>();
  // Chromium omits Cookie/Set-Cookie from the ordinary Network events. Extra
  // events may precede their base event; redirects also reuse the request id.
  type Exchange = { ref: string; extra?: boolean; requestDone?: boolean; responseDone?: boolean };
  const metadata = new Map<string, { exchanges: Exchange[]; requests: Record<string, string>[]; responses: Record<string, string>[] }>();
  const headersFor = (id: string) => {
    let entry = metadata.get(id);
    if (!entry) {
      entry = { exchanges: [], requests: [], responses: [] };
      metadata.set(id, entry);
      if (metadata.size > 200) metadata.delete(metadata.keys().next().value!);
    }
    return entry;
  };
  const flushHeaders = (id: string) => {
    const entry = headersFor(id);
    for (const exchange of entry.exchanges) {
      if (exchange.extra === undefined) break;
      if (!exchange.extra) continue;
      const record = store.get(exchange.ref);
      if (!exchange.requestDone && entry.requests.length) {
        const headers = entry.requests.shift()!;
        store.update(exchange.ref, { requestHeaders: { ...record?.requestHeaders, ...headers } });
        exchange.requestDone = true;
      }
      if (!exchange.responseDone && entry.responses.length) {
        const headers = entry.responses.shift()!;
        store.update(exchange.ref, { responseHeaders: { ...record?.responseHeaders, ...headers } });
        exchange.responseDone = true;
      }
    }
  };
  session.on("Network.requestWillBeSentExtraInfo", (event) => {
    const entry = headersFor(event.requestId);
    if (entry.requests.length < 20) entry.requests.push(event.headers);
    flushHeaders(event.requestId);
  });
  session.on("Network.responseReceivedExtraInfo", (event) => {
    const entry = headersFor(event.requestId);
    if (entry.responses.length < 20) entry.responses.push(event.headers);
    flushHeaders(event.requestId);
  });
  const finish = (id: string, state: RecordedRequest["captureState"]) => {
    const capture = active.get(id);
    if (!capture) return;
    clearTimeout(capture.timer);
    active.delete(id);
    const record = store.get(capture.ref);
    if (!record) return;
    store.update(capture.ref, {
      responseBody: (record.responseBody ?? "") + capture.decoder.end(),
      // Chromium can evict a fast response before streamResourceContent runs,
      // returning an empty buffer despite having observed received bytes.
      captureState: state === "complete" && capture.bytes < capture.observedBytes
        ? (capture.bytes ? "truncated" : "unavailable") : state,
      durationMs: Date.now() - Date.parse(record.startedAt)
    });
  };
  const appendBytes = (id: string, bytes: Buffer) => {
    const capture = active.get(id);
    if (!capture) return;
    const record = store.get(capture.ref);
    if (!record) { finish(id, "unavailable"); return; }
    const room = MAX_CAPTURE_BYTES - capture.bytes;
    capture.bytes += Math.min(room, bytes.length);
    store.update(capture.ref, { responseBody: (record.responseBody ?? "") + capture.decoder.write(bytes.subarray(0, room)) });
    if (bytes.length > room) finish(id, "truncated");
  };
  const append = (id: string, data: string) => appendBytes(id, Buffer.from(data, "base64"));
  session.on("Network.requestWillBeSent", (event) => {
    const previousExchange = headersFor(event.requestId).exchanges.at(-1);
    if (previousExchange && event.redirectResponse) previousExchange.extra = event.redirectHasExtraInfo;
    if (active.has(event.requestId)) {
      const previous = active.get(event.requestId)!;
      if (event.redirectResponse) store.update(previous.ref, {
        status: event.redirectResponse.status,
        statusText: event.redirectResponse.statusText,
        responseHeaders: event.redirectResponse.headers
      });
      finish(event.requestId, event.redirectResponse ? "complete" : "unavailable");
    }
    if (active.size >= MAX_ACTIVE_CAPTURES) finish(active.keys().next().value!, "truncated");
    const record = store.start({
      pageId, identity, method: event.request.method, url: event.request.url,
      resourceType: (event.type ?? "other").toLowerCase(),
      requestHeaders: event.request.headers, requestBody: boundedBody(event.request.postData),
      startedAt: new Date(event.wallTime * 1000).toISOString(), captureState: "pending"
    });
    const timer = setTimeout(() => finish(event.requestId, "timed_out"), CAPTURE_TIMEOUT_MS);
    timer.unref();
    active.set(event.requestId, {
      ref: record.ref, bytes: 0, observedBytes: 0, decoder: new StringDecoder("utf8"), timer,
      prefixReady: false, pending: [], pendingBytes: 0, overflow: false, fallbackToCache: false
    });
    const exchanges = headersFor(event.requestId).exchanges;
    exchanges.push({ ref: record.ref });
    if (exchanges.length > 20) exchanges.shift();
    flushHeaders(event.requestId);
  });
  session.on("Network.responseReceived", (event) => {
    const capture = active.get(event.requestId);
    if (!capture) return;
    const response = event.response;
    store.update(capture.ref, { status: response.status, statusText: response.statusText, responseHeaders: response.headers });
    const exchange = headersFor(event.requestId).exchanges.at(-1);
    if (exchange) exchange.extra = event.hasExtraInfo;
    flushHeaders(event.requestId);
    if (["Image", "Media", "Font"].includes(event.type)
      || /^(image|video|audio|font)\//i.test(response.mimeType)
      || response.mimeType === "application/octet-stream") {
      finish(event.requestId, "unavailable");
      return;
    }
    store.update(capture.ref, { captureState: "streaming" });
    capture.ready = session.send("Network.streamResourceContent", { requestId: event.requestId }).then(({ bufferedData }) => {
      if (active.get(event.requestId) !== capture) return;
      append(event.requestId, bufferedData);
      capture.prefixReady = true;
      for (const chunk of capture.pending) append(event.requestId, chunk);
      capture.pending = [];
      if (capture.overflow) finish(event.requestId, "truncated");
    }).catch(() => {
      // loadingFinished will read the cached body, even when that event
      // arrived before the stream setup promise rejected.
      if (active.get(event.requestId) === capture) capture.fallbackToCache = true;
    });
  });
  session.on("Network.dataReceived", (event) => {
    const capture = active.get(event.requestId);
    if (!capture) return;
    capture.observedBytes += event.dataLength;
    if (!event.data) return;
    if (capture.prefixReady) append(event.requestId, event.data);
    else {
      // Prefix and chunks may cross on the protocol transport. Bound the
      // pending queue too and decode UTF-8 only after restoring byte order.
      const bytes = Buffer.from(event.data, "base64");
      const room = MAX_CAPTURE_BYTES - capture.pendingBytes;
      if (room > 0) capture.pending.push(bytes.subarray(0, room).toString("base64"));
      capture.pendingBytes += Math.min(bytes.length, room);
      if (bytes.length > room) capture.overflow = true;
    }
  });
  session.on("Network.loadingFinished", (event) => {
    const capture = active.get(event.requestId);
    if (!capture) return;
    void Promise.resolve(capture.ready).then(async () => {
      if (active.get(event.requestId) !== capture) return;
      if (capture.fallbackToCache) {
        // A fast response can finish before streamResourceContent reaches CDP.
        // Read that exchange's browser cache after loadingFinished; never replay
        // the HTTP request. The existing capture timer and byte cap still apply.
        try {
          const { body, base64Encoded } = await session.send("Network.getResponseBody", { requestId: event.requestId });
          if (active.get(event.requestId) !== capture) return;
          capture.bytes = 0;
          capture.decoder = new StringDecoder("utf8");
          capture.pending = [];
          store.update(capture.ref, { responseBody: "" });
          appendBytes(event.requestId, Buffer.from(body, base64Encoded ? "base64" : "utf8"));
        } catch {
          if (active.get(event.requestId) === capture) finish(event.requestId, "unavailable");
          return;
        }
      }
      if (active.get(event.requestId) === capture) finish(event.requestId, "complete");
    });
  });
  session.on("Network.loadingFailed", (event) => finish(event.requestId, "failed"));
  page.once("close", () => {
    for (const id of active.keys()) finish(id, "failed");
    metadata.clear();
    void session.detach().catch(() => undefined);
  });
  await session.send("Network.enable", {
    maxTotalBufferSize: 8 * 1024 * 1024, maxResourceBufferSize: MAX_CAPTURE_BYTES * 4,
    maxPostDataSize: MAX_CAPTURE_BYTES
  });
}
