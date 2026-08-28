import assert from "node:assert/strict";
import test from "node:test";
import { authSignal, extractApiRoutes, looksLikeRoute, normalizeUrl, sameHost } from "./crawl-core";

test("normalizeUrl strips fragments, default ports, and trailing slashes", () => {
  assert.equal(normalizeUrl("https://a.test:443/x/#frag"), "https://a.test/x");
  assert.equal(normalizeUrl("http://a.test:80/x/"), "http://a.test/x");
  assert.equal(normalizeUrl("https://a.test/x?b=2&a=1"), "https://a.test/x?b=2&a=1");
  assert.equal(normalizeUrl("not a url"), "");
});

test("sameHost compares hosts only", () => {
  assert.equal(sameHost("https://a.test/x", "http://a.test:8080/y"), false);
  assert.equal(sameHost("https://a.test/x", "https://a.test/y"), true);
});

test("looksLikeRoute accepts endpoints and rejects assets/non-routes", () => {
  assert.equal(looksLikeRoute("/api/v1/users"), true);
  assert.equal(looksLikeRoute("/admin/login"), true);
  assert.equal(looksLikeRoute("/health"), false, "root-only path without api prefix");
  assert.equal(looksLikeRoute("/api/health"), true);
  assert.equal(looksLikeRoute("/static/app.js"), false);
  assert.equal(looksLikeRoute("//evil.test"), false);
  assert.equal(looksLikeRoute("/a b"), false);
});

test("extractApiRoutes dedupes and caps quoted matches", () => {
  const raw = ['"/api/users"', '"/api/users"', '"/static/x.css"', '"/api/orders/42"', '"/no"', '"just-text"'];
  assert.deepEqual(extractApiRoutes(raw), ["/api/users", "/api/orders/42"]);
  assert.equal(extractApiRoutes(raw, 1).length, 1);
});

test("authSignal flags login redirects", () => {
  assert.equal(authSignal("https://a.test/login?next=/admin"), "login-redirect");
  assert.equal(authSignal("https://a.test/signin"), "login-redirect");
  assert.equal(authSignal("https://a.test/dashboard"), "none");
  assert.equal(authSignal("https://a.test/api/sessions"), "none", "ordinary API path is not a wall");
  assert.equal(authSignal("https://a.test/session/status"), "none");
  assert.equal(authSignal("::bad::"), "unknown");
});
