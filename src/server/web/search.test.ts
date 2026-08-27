import assert from "node:assert/strict";
import test from "node:test";
import { isCveLookup, parseDuckDuckGoHtml, screenQuery, webSearch } from "./search";

const DDG_HTML = `
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnvd.nist.gov%2Fvuln%2Fdetail%2FCVE-2021-41773&amp;rut=abc">Path Traversal in Apache</a>
  <a class="result__snippet" href="...">Apache 2.4.49 &amp; 2.4.50 path traversal and file disclosure <b>bug</b></a>
</div>
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="https://example.com/direct">Direct Link</a>
  <a class="result__snippet">plain snippet</a>
</div>
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="javascript:alert(1)">Bad scheme</a>
</div>`;

test("parses DuckDuckGo results, decodes redirects, filters non-http", () => {
  const results = parseDuckDuckGoHtml(DDG_HTML, 10);
  assert.equal(results.length, 2);
  assert.equal(results[0].url, "https://nvd.nist.gov/vuln/detail/CVE-2021-41773");
  assert.equal(results[0].title, "Path Traversal in Apache");
  assert.equal(results[0].snippet.includes("path traversal and file disclosure bug"), true);
  assert.equal(results[1].url, "https://example.com/direct");
});

test("parse respects the result limit", () => {
  assert.equal(parseDuckDuckGoHtml(DDG_HTML, 1).length, 1);
});

test("screenQuery rejects credential-shaped queries", () => {
  assert.match(screenQuery("sk-abcdefghij0123456789")!.message, /API key/);
  assert.match(screenQuery("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx")!.message, /JWT/);
  assert.match(screenQuery("hash aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")!.message, /hex/);
  assert.equal(screenQuery("Apache 2.4.49 path traversal CVE"), null);
});

test("bare CVE ids route to the structured CVE API", async () => {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: string | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes("cve.circl.lu")) {
      return new Response(JSON.stringify({ id: "CVE-2021-41773", summary: "Path traversal in Apache HTTP Server 2.4.49", cvss: 7.5, references: ["https://nvd.nist.gov/vuln/detail/CVE-2021-41773"] }), { status: 200 });
    }
    return new Response(DDG_HTML, { status: 200 });
  }) as typeof fetch;
  try {
    const outcome = await webSearch("CVE-2021-41773");
    assert.equal(outcome.provider, "duckduckgo+cve");
    assert.match(outcome.cveDetail!, /Path traversal in Apache HTTP Server/);
    assert.match(outcome.cveDetail!, /CVSS: 7.5/);
    assert.equal(outcome.results.length, 2);
  } finally {
    globalThis.fetch = original;
  }
});

test("duckduckgo rate limits surface an upgrade hint", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response("too many requests", { status: 429 })) as typeof fetch;
  try {
    await assert.rejects(webSearch("apache struts rce"), /Tavily API key/);
  } finally {
    globalThis.fetch = original;
  }
});

test("a configured tavily key is used as the provider", async () => {
  const original = globalThis.fetch;
  let seen: { url: string; init: RequestInit } | undefined;
  globalThis.fetch = (async (input: string | URL, init?: RequestInit) => {
    seen = { url: String(input), init: init ?? {} };
    return new Response(JSON.stringify({ results: [{ title: "Advisory", url: "https://example.org/advisory", content: "RCE in example" }] }), { status: 200 });
  }) as typeof fetch;
  try {
    const outcome = await webSearch("example rce", { tavilyApiKey: "tvly-test" });
    assert.equal(outcome.provider, "tavily");
    assert.equal(outcome.results[0].url, "https://example.org/advisory");
    assert.equal(seen!.url, "https://api.tavily.com/search");
    assert.equal((seen!.init.headers as Record<string, string>).authorization, "Bearer tvly-test");
  } finally {
    globalThis.fetch = original;
  }
});
