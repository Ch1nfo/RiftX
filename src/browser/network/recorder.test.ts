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

async function eventually<T>(read: () => Promise<T>, accept: (value: T) => boolean) {
  for (let i = 0; i < 100; i++) {
    const value = await read();
    if (accept(value)) return value;
    await delay(30);
  }
  throw new Error(`Condition not met: ${JSON.stringify(await read())}`);
}

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
