import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { gzipSync } from "node:zlib";
import { EventEmitter } from "node:events";
import { BrowserManager } from "../runtime/browser-manager";
import { MAX_CAPTURE_BYTES, RequestStore } from "./request-store";
import { attachRequestRecorder } from "./recorder";
import { chromium } from "playwright";

async function eventually<T>(read: () => Promise<T>, accept: (value: T) => boolean) {
  for (let i = 0; i < 100; i++) {
    const value = await read();
    if (accept(value)) return value;
    await delay(30);
  }
  throw new Error(`Condition not met: ${JSON.stringify(await read())}`);
}

async function cachedCapture(body: () => Promise<{ body: string; base64Encoded: boolean }>) {
  const session = new EventEmitter() as EventEmitter & { send: (method: string) => Promise<unknown>; detach: () => Promise<void> };
  const calls: string[] = [];
  session.send = async (method) => {
    calls.push(method);
    if (method === "Network.streamResourceContent") throw new Error("Request has already finished loading");
    if (method === "Network.getResponseBody") return body();
    return {};
  };
  session.detach = async () => {};
  const page = Object.assign(new EventEmitter(), { context: () => ({ newCDPSession: async () => session }) });
  const store = new RequestStore();
  await attachRequestRecorder(page as never, "fixture-page", "worker-a", store);
  session.emit("Network.requestWillBeSent", { requestId: "id", type: "Fetch", wallTime: Date.now() / 1000, request: { method: "GET", url: "http://fixture.invalid/cached", headers: {} } });
  session.emit("Network.responseReceived", { requestId: "id", type: "Fetch", hasExtraInfo: false, response: { status: 200, statusText: "OK", headers: {}, mimeType: "application/json" } });
  session.emit("Network.loadingFinished", { requestId: "id" });
  return { session, page, store, calls };
}

test("completed responses fall back to cached CDP bodies with byte and identity bounds", async () => {
  const fixture = await cachedCapture(async () => ({ body: Buffer.from("x".repeat(MAX_CAPTURE_BYTES + 20)).toString("base64"), base64Encoded: true }));
  try {
    const record = await eventually(async () => fixture.store.list()[0], (record) => record.captureState === "truncated");
    assert.equal(record.responseBody, "x".repeat(MAX_CAPTURE_BYTES));
    assert.equal(record.identity, "worker-a");
    assert.equal(record.pageId, "fixture-page");
    assert.deepEqual(fixture.calls, ["Network.enable", "Network.streamResourceContent", "Network.getResponseBody"]);
  } finally { fixture.page.emit("close"); }
});

test("cache fallback failure is unavailable and never replays the request", async () => {
  const fixture = await cachedCapture(async () => { throw new Error("cache evicted"); });
  try {
    const record = await eventually(async () => fixture.store.list()[0], (record) => record.captureState === "unavailable");
    assert.equal(record.responseBody, "");
    assert.deepEqual(fixture.calls, ["Network.enable", "Network.streamResourceContent", "Network.getResponseBody"]);
  } finally { fixture.page.emit("close"); }
});

test("late cache reads cannot overwrite a timed-out or closed capture", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const stop of ["timeout", "close"] as const) {
    let complete!: (value: { body: string; base64Encoded: boolean }) => void;
    const response = new Promise<{ body: string; base64Encoded: boolean }>((resolve) => { complete = resolve; });
    const fixture = await cachedCapture(() => response);
    for (let turn = 0; turn < 5; turn++) await Promise.resolve();
    assert.ok(fixture.calls.includes("Network.getResponseBody"));
    if (stop === "timeout") t.mock.timers.tick(60_000);
    else fixture.page.emit("close");
    complete({ body: "late-body", base64Encoded: false });
    for (let turn = 0; turn < 5; turn++) await Promise.resolve();
    const record = fixture.store.list()[0];
    assert.equal(record.captureState, stop === "timeout" ? "timed_out" : "failed");
    assert.equal(record.responseBody, "");
    fixture.page.emit("close");
  }
});

test("parallel fast gzip responses retain decoded bodies without another HTTP request", async () => {
  const visits = new Map<string, number>();
  const server = createServer((req, res) => {
    visits.set(req.url!, (visits.get(req.url!) ?? 0) + 1);
    if (req.url === "/") {
      res.writeHead(200, { "content-type": "text/html" });
      res.end("<main>fixture</main>");
    } else {
      res.writeHead(200, { "content-type": "application/json", "content-encoding": "gzip" });
      res.end(gzipSync(JSON.stringify({ path: req.url, value: "fixture-数据" })));
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const store = new RequestStore();
    await attachRequestRecorder(page, "fixture-page", "worker-gzip", store);
    await page.goto(origin);
    await page.evaluate(async () => Promise.all(Array.from({ length: 30 }, (_, index) => fetch(`/cached-${index}`).then((response) => response.json()))));
    const records = await eventually(async () => store.list().filter((record) => record.resourceType === "fetch"), (records) => records.length === 30 && records.every((record) => record.captureState === "complete"));
    for (const record of records) {
      const pathname = new URL(record.url).pathname;
      assert.deepEqual(JSON.parse(record.responseBody!), { path: pathname, value: "fixture-数据" });
      assert.equal(record.identity, "worker-gzip");
      assert.equal(visits.get(pathname), 1);
    }
  } finally {
    await browser.close();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("captures chunked/gzip bodies and live SSE without replay; pins evidence across eviction and restart", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-network-"));
  const visits = new Map<string, number>();
  const server = createServer((req, res) => {
    const path = req.url!;
    visits.set(path, (visits.get(path) ?? 0) + 1);
    if (path === "/") {
      res.writeHead(200, { "content-type": "text/html", "set-cookie": "session=target-secret; Path=/" });
      res.end("<main>fixture</main>");
    } else if (path === "/gzip") {
      res.writeHead(200, { "content-type": "application/json", "content-encoding": "gzip" });
      res.end(gzipSync('{"result":"gzip-secret"}'));
    } else if (path === "/events") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write('data: {"token":"first","text":"业务信息"}\n\n');
      const timer = setInterval(() => res.write('data: {"token":"next"}\n\n'), 50);
      res.on("close", () => clearInterval(timer));
    } else if (path === "/redirect") {
      res.writeHead(302, { location: '/redirected', 'set-cookie': 'redirect=hop-one; Path=/' });
      res.end();
    } else if (path === "/oversize") {
      res.writeHead(200, { 'content-type': 'text/plain' });
      res.end('x'.repeat(2 * 1024 * 1024));
    } else if (path === "/large") {
      res.writeHead(200, { "content-type": "text/plain" });
      res.end("a".repeat(MAX_CAPTURE_BYTES + 10000));
    } else {
      req.resume();
      res.writeHead(200, { "content-type": "application/json", "set-cookie": "result=target-value" });
      res.write('{"token_count":128,"message":"invalid password",');
      setTimeout(() => res.end('"result":"完整业务信息"}'), 40);
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  const options = { evidenceRoot: root, evidenceSessionId: "session", scope: { rules: [origin] } };
  const browser = new BrowserManager(options);
  let reopened: BrowserManager | undefined;
  try {
    await browser.navigate(origin);
    await browser.evaluate(`void fetch('/chunked', {method:'POST', headers:{Authorization:'Bearer challenge-token'}, body:JSON.stringify({password:'challenge-password'})}); void fetch('/gzip'); void fetch('/large'); void fetch('/oversize'); void fetch('/redirect'); window.events = new EventSource('/events'); 'started'`);
    const list = await eventually(() => browser.requestsList(), (value) => ["/chunked", "/gzip", "/large", "/events", "/oversize", "/redirected"].every((path) => value.includes(path)));
    const ref = (path: string) => list.split("\n").find((line) => line.includes(`${origin}${path} `))!.split(" ")[0];
    const chunked = ref("/chunked");
    const text = await eventually(() => browser.responseBody(chunked), (value) => value.includes("Capture: complete"));
    assert.match(text, /完整业务信息/);
    assert.match(text, /invalid password/);
    assert.match(text, /token_count/);
    const detail = await browser.requestDetail(chunked);
    assert.match(detail, /Bearer challenge-token/);
    assert.match(detail, /challenge-password/);
    assert.match(detail, /session=target-secret/);
    assert.match(detail, /result=target-value/);
    const compressed = await eventually(() => browser.responseBody(ref("/gzip")), (value) => value.includes("Capture: complete"));
    assert.match(compressed, /gzip-secret/);
    const live = await eventually(() => browser.responseBody(ref("/events")), (value) => value.includes("业务信息"));
    assert.match(live, /Capture: streaming/);
    await eventually(() => browser.responseBody(ref("/large")), (value) => value.includes("Capture: truncated"));
    const oversize = await eventually(() => browser.responseBody(ref("/oversize")), (value) => /Capture: (truncated|unavailable)/.test(value));
    assert.doesNotMatch(oversize, /Capture: complete/);
    const redirected = await eventually(() => browser.responseBody(ref("/redirected")), (value) => value.includes("Capture: complete"));
    assert.match(redirected, /完整业务信息/);
    assert.match(await browser.requestDetail(ref("/redirected")), /redirect=hop-one/);
    const redirectDetail = await browser.requestDetail(ref("/redirect"));
    assert.match(redirectDetail, /302/);
    assert.doesNotMatch(redirectDetail, /result=target-value[\s\S]*Capture/);
    const evidence = await browser.requestEvidence(chunked);
    assert.ok(evidence.artifactPath);
    assert.equal(evidence.identity, "default");
    const saved = JSON.parse(await readFile(evidence.artifactPath, "utf8"));
    assert.equal(JSON.parse(saved.responseBody).result, "完整业务信息");
    const largeEvidence = await browser.requestEvidence(ref("/large"));
    const large = JSON.parse(await readFile(largeEvidence.artifactPath!, "utf8"));
    assert.equal(Buffer.byteLength(large.responseBody), MAX_CAPTURE_BYTES);
    for (const path of ["/chunked", "/gzip", "/large", "/events"]) assert.equal(visits.get(path), 1, `must not replay ${path}`);
    await browser.close();
    assert.match(await browser.responseBody(chunked), /完整业务信息/);
    reopened = new BrowserManager(options);
    assert.match(await reopened.requestDetail(chunked), /challenge-password/);
    assert.match(await reopened.responseBody(chunked), /完整业务信息/);
  } finally {
    await browser.shutdown();
    await reopened?.shutdown();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    await rm(root, { recursive: true, force: true });
  }
});

test("inspected snapshots survive the rolling log, are unique across workers, and reject path traversal", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-requests-"));
  try {
    const store = new RequestStore(root);
    const input = { pageId: "p", identity: "admin", method: "GET", url: "http://fixture/", resourceType: "fetch", requestHeaders: { Cookie: "session=fixture" }, responseBody: "evidence", startedAt: new Date().toISOString() };
    const first = store.start(input);
    await store.snapshot(first.ref);
    for (let i = 0; i < 201; i++) store.start(input);
    assert.equal(store.get(first.ref), undefined);
    assert.equal((await store.snapshot(first.ref)).responseBody, "evidence");
    const second = new RequestStore(root);
    assert.notEqual(second.start(input).ref, first.ref);
    assert.equal((await second.snapshot(first.ref)).identity, "admin");
    await assert.rejects(store.snapshot("../../config"), /Unknown request ref/);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("capture timeout stops observation and reports partial data without cancelling the request", async (t) => {
  const { attachRequestRecorder } = await import("./recorder");
  const session = new EventEmitter() as EventEmitter & { send: (method: string) => Promise<unknown>; detach: () => Promise<void> };
  const calls: string[] = [];
  session.send = async (method) => { calls.push(method); return method === "Network.streamResourceContent" ? { bufferedData: Buffer.from("prefix").toString("base64") } : {}; };
  session.detach = async () => {};
  const page = Object.assign(new EventEmitter(), { context: () => ({ newCDPSession: async () => session }) });
  const store = new RequestStore();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  await attachRequestRecorder(page as never, "p", "admin", store);
  session.emit("Network.requestWillBeSent", { requestId: "id", type: "Fetch", wallTime: Date.now() / 1000, request: { method: "GET", url: "http://fixture/events", headers: {} } });
  session.emit("Network.responseReceived", { requestId: "id", type: "Fetch", hasExtraInfo: false, response: { status: 200, statusText: "OK", headers: {}, mimeType: "text/event-stream" } });
  await Promise.resolve();
  t.mock.timers.tick(60_000);
  const record = store.list()[0];
  assert.equal(record.captureState, "timed_out");
  assert.equal(record.responseBody, "prefix");
  session.emit("Network.dataReceived", { requestId: "id", dataLength: 4, data: Buffer.from("late").toString("base64") });
  assert.equal(store.get(record.ref)?.responseBody, "prefix");
  assert.deepEqual(calls, ["Network.enable", "Network.streamResourceContent"]);
  page.emit("close");
});
