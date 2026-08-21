import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { BrowserManager } from "./runtime/browser-manager";
import { RequestStore, redactBody, redactHeaders } from "./network/request-store";

test("redacts sensitive request metadata", () => {
  assert.equal(redactHeaders({ Authorization: "Bearer secret", Accept: "text/plain" }).Authorization, "[REDACTED]");
  assert.equal(redactBody('{"password":"secret"}'), "[REDACTED]");
  const store = new RequestStore();
  const item = store.start({ pageId: "p", method: "GET", url: "https://example.test", resourceType: "document", requestHeaders: {}, startedAt: new Date().toISOString() });
  assert.equal(store.get(item.ref)?.ref, "r1");
  store.clear();
  assert.equal(store.get(item.ref), undefined);
});

test("browser blocks navigation outside configured scope", async () => {
  const browser = new BrowserManager({ scope: { allowedOrigins: ["https://authorized.test"] } });
  await assert.rejects(() => browser.navigate("https://outside.test"), /blocked by RiftX scope/);
  await browser.close();
});

test("browser snapshot refs drive form interaction and request recording", async () => {
  const server = createServer((request, response) => {
    if (request.url === "/submit" && request.method === "POST") {
      response.writeHead(200, { "content-type": "text/html" });
      response.end("<main>submitted</main>");
      return;
    }
    response.writeHead(200, { "content-type": "text/html" });
    response.end('<main><h1>Login</h1><form method="post" action="/submit"><label>Username<input name="username" /></label><label>Password<input name="password" type="password" /></label><button>Login</button></form></main>');
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  assert.equal(typeof address, "object");
  const origin = `http://127.0.0.1:${(address as { port: number }).port}`;
  const browser = new BrowserManager({});
  try {
    const snapshot = await browser.navigate(origin);
    assert.match(snapshot.text, /\[e1\] input/);
    assert.match(snapshot.text, /\[e3\] button/);
    await assert.rejects(() => browser.navigate("https://outside.test"), /blocked by RiftX scope/);
    await browser.fill("e1", "operator");
    await browser.click("e3");
    assert.match((await browser.snapshot()).text, /submitted/);
    assert.match(await browser.requestsList(), /POST\s+.*\/submit/);
    await assert.rejects(() => browser.fill("e1", "stale"), /Unknown or stale element ref/);
  } finally {
    await browser.close();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
