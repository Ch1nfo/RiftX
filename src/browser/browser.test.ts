import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { BrowserManager } from "./runtime/browser-manager";
import { RequestStore, redactBody, redactHeaders } from "./network/request-store";

function closeServers(...servers: Server[]) {
  return Promise.all(servers.map((server) => new Promise<void>((resolve, reject) => {
    server.closeAllConnections?.();
    server.close((error) => error ? reject(error) : resolve());
  })));
}

test("redacts sensitive request metadata", () => {
  assert.equal(redactHeaders({ Authorization: "Bearer secret", Accept: "text/plain" }).Accept, "text/plain");
  assert.equal(redactHeaders({ Authorization: "Bearer secret", Accept: "text/plain" }).Authorization, "[REDACTED]");
  assert.equal(redactBody('{"password":"secret"}'), "[REDACTED]");
  const store = new RequestStore();
  const item = store.start({ pageId: "p", identity: "default", method: "GET", url: "https://example.test", resourceType: "document", requestHeaders: {}, startedAt: new Date().toISOString() });
  assert.equal(store.get(item.ref)?.ref, "r1");
  store.clear();
  assert.equal(store.get(item.ref), undefined);
});

test("scope rules authorize intranet hosts, CIDR ranges, and session grants", () => {
  const browser = new BrowserManager({ scope: { rules: ["10.0.0.0/8", "*.target.com"] } });
  // A host rule covers every port, which multi-port intranet targets need.
  assert.equal(browser.checkNavigationScope("http://10.0.181.248:8000/").allowed, true);
  assert.equal(browser.checkNavigationScope("http://10.0.181.248:10086/").allowed, true);
  assert.equal(browser.checkNavigationScope("https://app.target.com/").allowed, true);
  assert.equal(browser.checkNavigationScope("http://172.16.0.1/").allowed, false);
  assert.equal(browser.checkNavigationScope("ftp://10.0.0.1/").allowed, false);
  // A one-shot authorization does not mutate the scope rules; navigate consumes it.
  browser.authorizeOnce("http://172.16.0.1:9000/");
  assert.equal(browser.checkNavigationScope("http://172.16.0.1:9000/").allowed, false);
  // A granted host stays authorized for the whole session, on every port.
  browser.grantScope("http://192.168.1.10/");
  assert.equal(browser.checkNavigationScope("http://192.168.1.10:8080/").allowed, true);
  assert.equal(browser.checkNavigationScope("http://192.168.1.11/").allowed, false);
});

test("exact-port session grants do not authorize sibling ports", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  browser.grantScope("http://10.0.0.9:8000/", true);
  assert.equal(browser.checkNavigationScope("http://10.0.0.9:8000/").allowed, true);
  assert.equal(browser.checkNavigationScope("http://10.0.0.9:8001/").allowed, false);
});

test("browser blocks navigation outside configured scope", async () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  await assert.rejects(() => browser.navigate("https://outside.test"), /outside the authorized browser scope/);
  await browser.close();
});

test("invalid-only scope rules fail closed instead of allowing any host", () => {
  const broken = new BrowserManager({ scope: { rules: ["https://target.test/path", "not a rule"] } });
  assert.equal(broken.checkNavigationScope("http://outside.test/").allowed, false);
  assert.equal(broken.checkNavigationScope("http://target.test/").allowed, false);
  // A mix keeps the valid rules and ignores the invalid entry.
  const mixed = new BrowserManager({ scope: { rules: ["authorized.test", "not a rule"] } });
  assert.equal(mixed.checkNavigationScope("http://authorized.test/").allowed, true);
  assert.equal(mixed.checkNavigationScope("http://outside.test/").allowed, false);
});

test("host-mapping targets are physical destinations and must be in scope", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  assert.deepEqual(browser.checkHostMappingScope({ "vhost.authorized.test": "10.0.0.9:8000" }), [{ host: "vhost.authorized.test", target: "10.0.0.9:8000" }]);
  assert.deepEqual(browser.checkHostMappingScope({ "alias.authorized.test": "authorized.test" }), []);
});

test("host mappings are validated consistently before approval and execution", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  const invalid = { "valid.test": "10.0.0.9", "": "" };
  assert.throws(() => browser.checkHostMappingScope(invalid), /Invalid host mapping/);
  assert.throws(() => browser.authorizeMappingTargetsOnce(invalid), /Invalid host mapping/);
  assert.throws(() => browser.setHostMappings(invalid), /Invalid host mapping/);
  assert.throws(() => browser.setHostMappings({ "host.test:8000": "10.0.0.9" }), /Invalid host mapping/);
});

test("host-mapping scope checks honor ports, schemes, and IPv6", () => {
  const browser = new BrowserManager({ scope: { rules: ["10.0.0.9:80"] } });
  assert.equal(browser.checkHostMappingScope({ "a.test": "10.0.0.9:8000" }).length, 1);
  assert.deepEqual(browser.checkHostMappingScope({ "a.test": "10.0.0.9:80" }), []);
  // A port-less target is broader than a port-specific rule, so it needs approval.
  assert.equal(browser.checkHostMappingScope({ "a.test": "10.0.0.9" }).length, 1);
  // IPv6 targets probe valid bracketed URLs instead of always failing closed.
  assert.equal(browser.checkHostMappingScope({ "a.test": "::1" }).length, 1);
  const v6 = new BrowserManager({ scope: { rules: ["::1"] } });
  assert.deepEqual(v6.checkHostMappingScope({ "a.test": "::1" }), []);
});

test("allow-once authorizes only the exact scheme, host, and port", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  browser.authorizeOnce("http://127.0.0.1:8080/app");
  assert.equal(browser.isUrlAuthorized("http://127.0.0.1:8080/assets/app.js"), true);
  assert.equal(browser.isUrlAuthorized("http://127.0.0.1:8081/other"), false);
  assert.equal(browser.isUrlAuthorized("https://127.0.0.1:8080/app"), false);
  assert.equal(browser.isUrlAuthorized("http://127.0.0.2:8080/app"), false);
});

test("allow-once is scoped to the identity that requested it", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  browser.authorizeOnce("http://127.0.0.1:8080/app", "admin");
  assert.equal(browser.isUrlAuthorized("http://127.0.0.1:8080/x", "admin"), true);
  // The default identity never received the authorization.
  assert.equal(browser.isUrlAuthorized("http://127.0.0.1:8080/x"), false);
});

test("allow-once cannot cross identities or survive leaving its exact origin", async () => {
  const server = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>${request.headers.host}</main>`);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["localhost"] } });
  try {
    await browser.navigate(`http://localhost:${port}/start`, "admin");
    const approvedUrl = `http://127.0.0.1:${port}/approved`;
    browser.authorizeOnce(approvedUrl, "admin");
    await assert.rejects(() => browser.navigate(approvedUrl, "default"), /outside the authorized browser scope/);
    await browser.navigate(approvedUrl, "admin");
    await browser.back("admin");
    assert.equal(browser.isUrlAuthorized(approvedUrl, "admin"), false);
    await assert.rejects(() => browser.navigate(approvedUrl, "admin"), /outside the authorized browser scope/);
  } finally {
    await browser.close();
    await closeServers(server);
  }
});

test("blocked top-level navigation preserves once windows and successful navigation filters them", async () => {
  let afterHits = 0;
  const first = createServer((request, response) => {
    if (request.url === "/after") afterHits += 1;
    response.writeHead(200, { "content-type": request.url === "/after" ? "text/plain" : "text/html" });
    response.end(request.url === "/after" ? "after-ok" : "<main>first</main>");
  });
  const second = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<main>second</main>");
  });
  await Promise.all([
    new Promise<void>((resolve) => first.listen(0, "127.0.0.1", () => resolve())),
    new Promise<void>((resolve) => second.listen(0, "127.0.0.1", () => resolve()))
  ]);
  const firstUrl = `http://127.0.0.1:${(first.address() as { port: number }).port}/`;
  const secondUrl = `http://127.0.0.1:${(second.address() as { port: number }).port}/`;
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  try {
    browser.authorizeOnce(firstUrl, "admin");
    await browser.navigate(firstUrl, "admin");
    browser.authorizeOnce(secondUrl, "admin");

    await browser.evaluate(`window.open("http://outside.test/", "_blank")`, "admin");
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(browser.isUrlAuthorized(firstUrl, "admin"), true);
    assert.equal(browser.isUrlAuthorized(secondUrl, "admin"), true);
    assert.equal(await browser.evaluate(`fetch("/after").then((response) => response.text())`, "admin"), '"after-ok"');
    assert.equal(afterHits, 1);

    await browser.navigate(secondUrl, "admin");
    assert.equal(browser.isUrlAuthorized(firstUrl, "admin"), false);
    assert.equal(browser.isUrlAuthorized(secondUrl, "admin"), true);
  } finally {
    await browser.close();
    await closeServers(first, second);
  }
});

test("failed explicit navigation preserves the current once window", async () => {
  const current = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>current ${request.url}</main>`);
  });
  await new Promise<void>((resolve) => current.listen(0, "127.0.0.1", () => resolve()));
  const currentUrl = `http://127.0.0.1:${(current.address() as { port: number }).port}/`;

  const portProbe = createServer();
  await new Promise<void>((resolve) => portProbe.listen(0, "127.0.0.1", () => resolve()));
  const closedPort = (portProbe.address() as { port: number }).port;
  await new Promise<void>((resolve, reject) => portProbe.close((error) => error ? reject(error) : resolve()));

  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  try {
    browser.authorizeOnce(currentUrl, "admin");
    await browser.navigate(currentUrl, "admin");
    const failedUrl = `https://127.0.0.1:${closedPort}/`;
    browser.authorizeOnce(failedUrl, "admin");
    await assert.rejects(() => browser.navigate(failedUrl, "admin"), /ERR_|Navigation failed/);

    await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(browser.isUrlAuthorized(currentUrl, "admin"), true);
    assert.match((await browser.navigate(`${currentUrl}after`, "admin")).text, /current \/after/);
  } finally {
    await browser.close();
    await closeServers(current);
  }
});

test("empty scope requires approval for the first mapping", () => {
  const browser = new BrowserManager({});
  // Without any baseline (rules, lock, or prior grant), nothing pre-authorizes a physical target.
  assert.equal(browser.checkHostMappingScope({ "vhost4.test": "10.9.9.9:7070" }).length, 1);
  // The once approval covers the mapping set logically and physically.
  browser.authorizeMappingTargetsOnce({ "vhost4.test": "10.9.9.9:7070" });
  assert.deepEqual(browser.checkHostMappingScope({ "vhost4.test": "10.9.9.9:7070" }), []);
  browser.setHostMappings({ "vhost4.test": "10.9.9.9:7070" });
  assert.equal(browser.isUrlAuthorized("http://vhost4.test:1234/x"), true);
});

test("replacing or clearing mappings invalidates their authorizations", () => {  const browser = new BrowserManager({ scope: { rules: ["vhost5.test"] } });
  browser.setHostMappings({ "vhost5.test": "10.9.9.9:7070" });
  browser.authorizeMappingTargetsOnce({ "vhost5.test": "10.9.9.9:7070" });
  assert.equal(browser.isUrlAuthorized("http://vhost5.test/x"), true);
  // Re-setting the identical mapping set keeps the authorization alive.
  browser.setHostMappings({ "vhost5.test": "10.9.9.9:7070" });
  assert.equal(browser.isUrlAuthorized("http://vhost5.test/x"), true);
  // Clearing the mappings drops the physical authorization with them; the
  // logical host itself stays in scope, the physical destination does not.
  browser.setHostMappings({});
  assert.equal(browser.isUrlAuthorized("http://vhost5.test/x"), true);
  assert.equal(browser.isUrlAuthorized("http://10.9.9.9:7070/x"), false);
});

test("mapped requests are authorized against their physical destination", () => {
  const browser = new BrowserManager({ scope: { rules: ["vhost.test", "10.0.0.9:8000"] } });
  browser.setHostMappings({ "vhost.test": "10.0.0.9:8000" });
  // The mapping overrides the port, so the physical origin stays in scope.
  assert.equal(browser.isUrlAuthorized("http://vhost.test:1234/x"), true);
  // An unapproved physical destination is blocked even though the logical host is in scope.
  browser.setHostMappings({ "vhost.test": "10.9.9.9:7080" });
  assert.equal(browser.isUrlAuthorized("http://vhost.test/x"), false);
  // Allow-once covers exactly the approved physical origin.
  browser.authorizeMappingTargetsOnce({ "vhost.test": "10.9.9.9:7080" });
  assert.equal(browser.isUrlAuthorized("http://vhost.test/x"), true);
  // Changing the mapping port afterwards needs a fresh authorization.
  browser.setHostMappings({ "vhost.test": "10.9.9.9:7081" });
  assert.equal(browser.isUrlAuthorized("http://vhost.test/x"), false);
});

test("browser snapshot refs drive form interaction, evaluate, and console capture", async () => {
  const server = createServer((request, response) => {
    if (request.url === "/submit" && request.method === "POST") {
      response.writeHead(200, { "content-type": "text/html" });
      response.end("<main>submitted</main>");
      return;
    }
    response.writeHead(200, { "content-type": "text/html" });
    response.end('<main><h1>Login</h1><script>console.log("page-ready");</script><form method="post" action="/submit"><label>Username<input name="username" /></label><label>Password<input name="password" type="password" /></label><button>Login</button></form></main>');
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
    // The first navigation host is locked; a different host needs authorization.
    await assert.rejects(() => browser.navigate("https://outside.test"), /outside the authorized browser scope/);
    assert.equal(await browser.evaluate("1 + 1"), "2");
    assert.match(await browser.evaluate("document.querySelector(\"h1\").textContent"), /Login/);
    // alert() during evaluation is captured as a dialog record instead of blocking.
    await browser.evaluate("alert(\"xss-proof\")");
    assert.match(browser.consoleLog(), /page-ready/);
    assert.match(browser.consoleLog(), /dialog: alert: xss-proof/);
    await browser.fill("e1", "operator");
    await browser.click("e3");
    assert.match((await browser.snapshot()).text, /submitted/);
    assert.match(await browser.requestsList(), /POST\s+.*\/submit/);
    await assert.rejects(() => browser.fill("e1", "stale"), /Unknown or stale element ref/);
    // Same host on a different port stays within the lock (fails at the network layer).
    await assert.rejects(() => browser.navigate("http://127.0.0.1:1/"), /ERR_UNSAFE_PORT|ERR_CONNECTION_REFUSED|outside the authorized browser scope/);
  } finally {
    await browser.close();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test("identities isolate cookie jars and bridge cookies in and out", async () => {
  const server = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<main>identity page</main>");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  assert.equal(typeof address, "object");
  const origin = `http://127.0.0.1:${(address as { port: number }).port}`;
  const browser = new BrowserManager({});
  try {
    await browser.navigate(origin, "anon");
    await browser.navigate(origin, "admin");
    await browser.evaluate('document.cookie = "role=anon; path=/"', "anon");
    await browser.evaluate('document.cookie = "role=admin; path=/"', "admin");
    // Parallel identities hold independent authenticated state.
    assert.match(await browser.evaluate("document.cookie", "anon"), /role=anon/);
    assert.doesNotMatch(await browser.evaluate("document.cookie", "admin"), /anon/);
    assert.match(await browser.evaluate("document.cookie", "admin"), /role=admin/);
    // The active identity is used when no identity is passed.
    browser.useIdentity("admin");
    assert.match(await browser.evaluate("document.cookie"), /role=admin/);
    // Export -> import moves an authenticated session into a third identity.
    const jar = await browser.cookiesExport("admin");
    assert.match(jar, /"name": "role"/);
    assert.match(jar, /"value": "admin"/);
    await browser.cookiesImport(jar, "third");
    await browser.navigate(`${origin}/third`, "third");
    assert.match(await browser.evaluate("document.cookie", "third"), /role=admin/);
    // A screenshot is attributed to the captured identity's page URL.
    browser.useIdentity("third");
    const shot = await browser.captureScreenshot("admin");
    assert.match(shot.url, /127\.0\.0\.1:\d+\/?$/);
    // Evidence lookup returns the capture-time URL, not the active identity's.
    const evidence = await browser.screenshotEvidence(shot.screenshotId);
    assert.match(evidence.url, /127\.0\.0\.1:\d+\/?$/);
    assert.match(browser.identitiesOverview(), /third/);
    // Recorded requests are tagged with the identity that made them.
    assert.match(await browser.requestsList(), /\[anon\]/);
    assert.match(await browser.requestsList(), /\[admin\]/);
    await assert.rejects(() => browser.cookiesImport("{bad json", "anon"), /JSON array/);
  } finally {
    await browser.close();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test("scope blocks out-of-scope subresources and popup navigations", async () => {
  const outOfScope = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<main>out-of-scope content</main>");
  });
  await new Promise<void>((resolve) => outOfScope.listen(0, "127.0.0.1", () => resolve()));
  const outPort = (outOfScope.address() as { port: number }).port;
  const inScope = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>scope page</main><script>
      window.__probe = "pending";
      fetch("http://127.0.0.1:${outPort}/probe", { mode: "no-cors" }).then(() => { window.__probe = "loaded"; }).catch(() => { window.__probe = "blocked"; });
      window.__popup = "pending";
      const popup = window.open("http://127.0.0.1:${outPort}/");
      setTimeout(() => {
        try { window.__popup = popup && popup.location ? String(popup.location.href) : "blocked-null"; }
        catch { window.__popup = "cross-origin"; }
      }, 800);
    </script>`);
  });
  await new Promise<void>((resolve) => inScope.listen(0, "127.0.0.1", () => resolve()));
  const inPort = (inScope.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["localhost"] } });
  try {
    await browser.navigate(`http://localhost:${inPort}/`);
    const state = await browser.evaluate(`new Promise((resolve) => {
      const started = Date.now();
      const check = () => {
        if ((window.__popup !== "pending" && window.__probe !== "pending") || Date.now() - started > 3000) {
          resolve({ probe: window.__probe, popup: window.__popup });
          return;
        }
        setTimeout(check, 100);
      };
      check();
    })`);
    const observed = JSON.parse(state as string) as { probe: string; popup: string };
    // The subresource fetch and the popup navigation are both blocked by scope.
    assert.equal(observed.probe, "blocked");
    assert.doesNotMatch(observed.popup, new RegExp(`127\\.0\\.0\\.1:${outPort}`), `popup loaded the out-of-scope target: ${observed.popup}`);
  } finally {
    await browser.close();
    await Promise.all([
      new Promise<void>((resolve, reject) => inScope.close((error) => error ? reject(error) : resolve())),
      new Promise<void>((resolve, reject) => outOfScope.close((error) => error ? reject(error) : resolve()))
    ]);
  }
});

test("host mappings keep the Host header and overrides shape the request", async () => {
  const server = createServer((request, response) => {
    const host = request.headers.host ?? "";
    const userAgent = request.headers["user-agent"] ?? "";
    const marker = request.headers["x-riftx-marker"] ?? "";
    if (!host.startsWith("admin.target.test")) {
      response.writeHead(421, { "content-type": "text/plain" });
      response.end(`misdirected: ${host}`);
      return;
    }
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>host-ok ua=${userAgent} marker=${marker}</main>`);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const address = server.address();
  assert.equal(typeof address, "object");
  const port = (address as { port: number }).port;
  // Scope must authorize both the logical virtual host and the physical destination.
  const browser = new BrowserManager({ scope: { rules: ["admin.target.test", `127.0.0.1:${port}`] } });
  try {
    // curl --resolve semantics: connect to 127.0.0.1:port, keep the Host header.
    assert.match(browser.setHostMappings({ "admin.target.test": `127.0.0.1:${port}` }), /admin\.target\.test -> 127\.0\.0\.1/);
    assert.throws(() => browser.setHostMappings({ "bad host": "not a target!!" }), /Invalid host mapping/);
    await browser.setUserAgent("riftx-agent/1.0");
    await browser.setExtraHeaders({ "x-riftx-marker": "probe" });
    const snapshot = await browser.navigate(`http://admin.target.test:${port}/`);
    assert.match(snapshot.text, /host-ok/);
    assert.match(snapshot.text, /ua=riftx-agent\/1\.0/);
    assert.match(snapshot.text, /marker=probe/);
    // Clearing the mappings restores direct addressing.
    assert.match(browser.setHostMappings({}), /cleared/i);
  } finally {
    await browser.close();
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test("websockets are scope-checked like other requests", () => {
  const browser = new BrowserManager({ scope: { rules: ["authorized.test"] } });
  assert.equal(browser.isUrlAuthorized("ws://authorized.test/socket"), true);
  assert.equal(browser.isUrlAuthorized("wss://authorized.test/socket"), true);
  assert.equal(browser.isUrlAuthorized("ws://outside.test/socket"), false);
});

test("websockets cannot reach out-of-scope hosts", async () => {
  let upgrades = 0;
  const outServer = createServer((request, response) => response.end("unused"));
  outServer.on("upgrade", (request, socket) => {
    upgrades += 1;
    const key = String(request.headers["sec-websocket-key"] ?? "");
    const accept = createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  });
  await new Promise<void>((resolve) => outServer.listen(0, "127.0.0.1", () => resolve()));
  const outPort = (outServer.address() as { port: number }).port;
  const inServer = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>ws page</main><script>
      window.__ws = "pending";
      const socket = new WebSocket("ws://127.0.0.1:${outPort}/");
      socket.onopen = () => { window.__ws = "open"; };
      socket.onclose = () => { if (window.__ws === "pending") window.__ws = "closed"; };
    </script>`);
  });
  await new Promise<void>((resolve) => inServer.listen(0, "127.0.0.1", () => resolve()));
  const inPort = (inServer.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["localhost"] } });
  try {
    await browser.navigate(`http://localhost:${inPort}/`);
    const state = await browser.evaluate(`new Promise((resolve) => {
      const started = Date.now();
      const check = () => (window.__ws !== "pending" || Date.now() - started > 2500) ? resolve(window.__ws) : setTimeout(check, 100);
      check();
    })`);
    assert.notEqual(state, "open");
    assert.equal(upgrades, 0);
  } finally {
    await browser.close();
    await closeServers(inServer, outServer);
  }
});

test("mapped websocket connections reach the physical target and preserve Host", async () => {
  let upgrades = 0;
  const hosts: string[] = [];
  const upgradedSockets = new Set<import("node:stream").Duplex>();
  const wsServer = createServer((request, response) => response.end("unused"));
  wsServer.on("upgrade", (request, socket) => {
    upgrades += 1;
    hosts.push(String(request.headers.host ?? ""));
    upgradedSockets.add(socket);
    socket.on("close", () => upgradedSockets.delete(socket));
    socket.on("error", () => undefined);
    const key = String(request.headers["sec-websocket-key"] ?? "");
    const accept = createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  });
  await new Promise<void>((resolve) => wsServer.listen(0, "127.0.0.1", () => resolve()));
  const wsPort = (wsServer.address() as { port: number }).port;
  const pageServer = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>mapped ws</main><script>
      window.__ws = "pending";
      const socket = new WebSocket("ws://ws-mapped.test:${wsPort}/socket");
      socket.onopen = () => { window.__ws = "open"; };
      socket.onerror = () => { window.__ws = "error"; };
    </script>`);
  });
  await new Promise<void>((resolve) => pageServer.listen(0, "127.0.0.1", () => resolve()));
  const pagePort = (pageServer.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["localhost", "ws-mapped.test", `127.0.0.1:${wsPort}`] } });
  try {
    browser.setHostMappings({ "ws-mapped.test": `127.0.0.1:${wsPort}` });
    await browser.navigate(`http://localhost:${pagePort}/`);
    const opened = await browser.evaluate(`new Promise((resolve) => {
      const started = Date.now();
      const check = () => (window.__ws !== "pending" || Date.now() - started > 3000) ? resolve(window.__ws === "open") : setTimeout(check, 50);
      check();
    })`);
    assert.equal(opened, "true");
    assert.equal(upgrades, 1);
    assert.deepEqual(hosts, [`ws-mapped.test:${wsPort}`]);
  } finally {
    await browser.close();
    for (const socket of upgradedSockets) socket.destroy();
    await closeServers(pageServer, wsServer);
  }
});

test("mapping allow-once survives the first navigation", async () => {
  let hits = 0;
  const server = createServer((request, response) => {
    hits += 1;
    if (!(request.headers.host ?? "").startsWith("vhost2.test")) {
      response.writeHead(421, { "content-type": "text/plain" });
      response.end(`misdirected: ${request.headers.host}`);
      return;
    }
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<main>host-ok</main>");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["vhost2.test"] } });
  try {
    browser.setHostMappings({ "vhost2.test": `127.0.0.1:${port}` });
    browser.authorizeMappingTargetsOnce({ "vhost2.test": `127.0.0.1:${port}` });
    // The once-approved physical destination must still be authorized when the
    // navigation itself starts, instead of being cleared by the navigation.
    const snapshot = await browser.navigate(`http://vhost2.test:${port}/`);
    assert.match(snapshot.text, /host-ok/);
    assert.ok(hits >= 1);
  } finally {
    await browser.close();
    await closeServers(server);
  }
});

test("scheme-restricted scope sends partial mappings through approval", () => {
  const browser = new BrowserManager({ scope: { rules: ["a.test", "https://10.0.0.9:8000"] } });
  // The http side of the same mapping is unauthorized, so approval is required.
  assert.equal(browser.checkHostMappingScope({ "a.test": "10.0.0.9:8000" }).length, 1);
  // The task grant adds a scheme-agnostic host rule, closing the loop.
  browser.grantScope("https://10.0.0.9:8000/");
  assert.equal(browser.checkHostMappingScope({ "a.test": "10.0.0.9:8000" }).length, 0);
  browser.setHostMappings({ "a.test": "10.0.0.9:8000" });
  assert.equal(browser.isUrlAuthorized("https://a.test:1234/x"), true);
  assert.equal(browser.isUrlAuthorized("http://a.test:1234/x"), true);
});

test("screenshot evidence survives a runtime rebuild", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-shots-"));
  const server = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<main>captured page</main>");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;
  try {
    const first = new BrowserManager({ evidenceRoot: root, evidenceSessionId: "sess", scope: { rules: ["127.0.0.1"] } });
    await first.navigate(`http://127.0.0.1:${port}/captured`);
    const shot = await first.captureScreenshot();
    assert.equal(shot.url, `http://127.0.0.1:${port}/captured`);
    await first.close();
    // A fresh runtime (dev reload / version bump) resolves the same evidence.
    const second = new BrowserManager({ evidenceRoot: root, evidenceSessionId: "sess", scope: { rules: ["127.0.0.1"] } });
    const evidence = await second.screenshotEvidence(shot.screenshotId);
    assert.equal(evidence.url, `http://127.0.0.1:${port}/captured`);
    // A pre-upgrade screenshot has no sidecar: an empty URL beats a wrong one.
    const legacyId = `s-${randomUUID()}`;
    await writeFile(join(root, "sess", "shots", `${legacyId}.png`), Buffer.from("89504e470d0a1a0a", "hex"));
    const legacy = await second.screenshotEvidence(legacyId);
    assert.equal(legacy.url, "");
    await second.close();
  } finally {
    await closeServers(server);
    await rm(root, { recursive: true, force: true });
  }
});

test("navigation windows expire per identity without mapping interference", async () => {
  const server = createServer((request, response) => {
    response.writeHead(200, { "content-type": "text/html" });
    response.end(`<main>page ${request.url}</main>`);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", () => resolve()));
  const port = (server.address() as { port: number }).port;
  const browser = new BrowserManager({ scope: { rules: ["localhost"] } });
  try {
    await browser.navigate(`http://localhost:${port}/`, "admin");
    browser.authorizeOnce("http://a.test:9999/", "admin");
    assert.equal(browser.isUrlAuthorized("http://a.test:9999/x", "admin"), true);
    // A mapping authorization for a different host must not keep the window alive.
    browser.authorizeMappingTargetsOnce({ "m.test": `127.0.0.1:${port}` });
    await browser.navigate(`http://localhost:${port}/next`, "admin");
    assert.equal(browser.isUrlAuthorized("http://a.test:9999/x", "admin"), false);
    // The mapping authorization itself survives the navigation.
    browser.setHostMappings({ "m.test": `127.0.0.1:${port}` });
    assert.equal(browser.isUrlAuthorized(`http://m.test:${port}/y`), true);
  } finally {
    await browser.close();
    await closeServers(server);
  }
});

test("stale mapping authorizations do not pre-approve new mapping sets", () => {
  const browser = new BrowserManager({});
  browser.authorizeMappingTargetsOnce({ "a.test": "10.9.9.9:7070" });
  // The exact approved set stays pre-authorized; a different logical host does not.
  assert.deepEqual(browser.checkHostMappingScope({ "a.test": "10.9.9.9:7070" }), []);
  assert.equal(browser.checkHostMappingScope({ "b.test": "10.9.9.9:7070" }).length, 1);
  // Fingerprints normalize host case: re-setting with different casing keeps the authorization.
  browser.setHostMappings({ "a.test": "10.9.9.9:7070" });
  assert.equal(browser.isUrlAuthorized("http://a.test:7070/x"), true);
  browser.setHostMappings({ "A.TEST": "10.9.9.9:7070" });
  assert.equal(browser.isUrlAuthorized("http://a.test:7070/x"), true);
});

test("clearing mappings closes both ends of websockets routed through the old set", () => {
  const browser = new BrowserManager({ scope: { rules: ["127.0.0.1"] } });
  browser.setHostMappings({ "m7.test": "127.0.0.1:7070" });
  // Simulate a live routed WebSocket with its page and server sides.
  const closed: string[] = [];
  const fakeRoute = { url: () => "ws://m7.test:7070/socket", close: () => { closed.push("page"); return Promise.resolve(); } };
  const fakeServer = { url: () => "ws://m7.test:7070/socket", close: () => { closed.push("server"); return Promise.resolve(); } };
  const routed = (browser as unknown as { routedWebSockets: Set<{ route: unknown; serverRoute?: unknown; source: string }> }).routedWebSockets;
  routed.add({ route: fakeRoute, serverRoute: fakeServer, source: JSON.stringify([["m7.test", "127.0.0.1:7070"]]) });
  // Clearing the set tears both ends down with their authorization.
  browser.setHostMappings({});
  assert.deepEqual(closed.sort(), ["page", "server"]);
  assert.equal(routed.size, 0);
});

test("run serializes operations on the manager instance", async () => {
  const manager = new BrowserManager({ scope: { rules: ["10.0.0.0/8"] } });
  const order: string[] = [];
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const first = manager.run(async () => {
    order.push("first-start");
    await firstGate;
    order.push("first-end");
  });
  const second = manager.run(async () => {
    order.push("second-start");
  });
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(order, ["first-start"]);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(order, ["first-start", "first-end", "second-start"]);
});

test("a failed run operation does not block the chain", async () => {
  const manager = new BrowserManager({ scope: { rules: ["10.0.0.0/8"] } });
  await assert.rejects(manager.run(async () => { throw new Error("boom"); }), /boom/);
  const order: string[] = [];
  await manager.run(async () => { order.push("ran"); });
  assert.deepEqual(order, ["ran"]);
});

test("queued operations never start after shutdown", async () => {
  const manager = new BrowserManager({ scope: { rules: ["10.0.0.0/8"] } });
  const order: string[] = [];
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const first = manager.run(async () => {
    order.push("first-start");
    await firstGate;
    order.push("first-end");
  });
  const second = manager.run(async () => {
    order.push("second-start");
  });
  await Promise.resolve();
  await Promise.resolve();
  await manager.shutdown();
  releaseFirst();
  await first;
  await assert.rejects(second, /closed/i);
  assert.deepEqual(order, ["first-start", "first-end"]);
});

test("close stays reopenable while shutdown is permanent", async () => {
  const manager = new BrowserManager({ scope: { rules: ["10.0.0.0/8"] } });
  await manager.close();
  const order: string[] = [];
  await manager.run(async () => { order.push("ran-after-close"); });
  assert.deepEqual(order, ["ran-after-close"]);
  await manager.shutdown();
  await assert.rejects(manager.run(async () => { order.push("must-not-run"); }), /closed/i);
  assert.deepEqual(order, ["ran-after-close"]);
});

test("a queued operation whose signal aborts before it starts is dropped", async () => {
  const manager = new BrowserManager({ scope: { rules: ["10.0.0.0/8"] } });
  const controller = new AbortController();
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
  const first = manager.run(async () => {
    await firstGate;
  });
  const second = manager.run(async () => {
    throw new Error("queued operation must not run after abort");
  }, controller.signal);
  controller.abort();
  releaseFirst();
  await first;
  await assert.rejects(second, /abort/i);
});
