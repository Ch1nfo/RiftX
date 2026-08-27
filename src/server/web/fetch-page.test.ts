import assert from "node:assert/strict";
import test from "node:test";
import { fetchPage, htmlToText, truncateContent } from "./fetch-page";

const publicResolve = async () => ["93.184.216.34"];

test("htmlToText strips scripts and styles, decodes entities, keeps structure", () => {
  const html = `<html><head><style>body{color:red}</style><script>alert("x")</script></head>
<body><h1>Title &amp; more</h1><p>First &lt;paragraph&gt;</p><p>Second</p><br/><div>after br</div></body></html>`;
  const text = htmlToText(html);
  assert.equal(text.includes("color:red"), false);
  assert.equal(text.includes("alert"), false);
  assert.equal(text.includes("Title & more"), true);
  assert.equal(text.includes("First <paragraph>"), true);
  assert.match(text, /Second\n+after br/);
});

test("truncateContent marks the cut", () => {
  const truncated = truncateContent("a".repeat(40_000));
  assert.match(truncated, /truncated at 30000 characters/);
  assert.equal(truncateContent("short").includes("truncated"), false);
});

test("fetchPage rejects non-http schemes and internal addresses at the entry guard", async () => {
  await assert.rejects(fetchPage("file:///etc/passwd"), /http\(s\)/);
  await assert.rejects(fetchPage("http://127.0.0.1:3000/api/settings"), /loopback/);
  await assert.rejects(fetchPage("http://169.254.169.254/latest/meta-data/"), /link-local/);
});

test("fetchPage prefers the reader service and falls back to direct extraction", async () => {
  const original = globalThis.fetch;
  const seen: string[] = [];
  globalThis.fetch = (async (input: string | URL) => {
    const url = String(input);
    seen.push(url);
    if (url.startsWith("https://r.jina.ai/")) {
      return url.endsWith("good")
        ? new Response("# Page\n\nReader markdown.", { status: 200 })
        : new Response("unavailable", { status: 500 });
    }
    return new Response("<html><body><p>Direct extraction &amp; text</p></body></html>", { status: 200, headers: { "content-type": "text/html" } });
  }) as typeof fetch;
  try {
    const viaReader = await fetchPage("https://example.com/good", { resolveDns: publicResolve });
    assert.equal(viaReader.source, "jina");
    assert.match(viaReader.content, /Reader markdown/);

    const viaDirect = await fetchPage("https://example.com/broken", { resolveDns: publicResolve });
    assert.equal(viaDirect.source, "direct");
    assert.equal(viaDirect.content.includes("Direct extraction & text"), true);
    assert.deepEqual(seen, [
      "https://r.jina.ai/https://example.com/good",
      "https://r.jina.ai/https://example.com/broken",
      "https://example.com/broken"
    ]);
  } finally {
    globalThis.fetch = original;
  }
});

test("redirects are followed only when each hop stays public", async () => {
  const original = globalThis.fetch;
  const requests: string[] = [];
  globalThis.fetch = (async (input: string | URL) => {
    const url = String(input);
    if (url.startsWith("https://r.jina.ai/")) return new Response("unavailable", { status: 500 });
    requests.push(url);
    if (url === "https://example.com/redirect") {
      return new Response(null, { status: 302, headers: { location: "http://127.0.0.1:8080/admin" } });
    }
    if (url === "https://example.com/hop") {
      return new Response(null, { status: 302, headers: { location: "https://example.org/final" } });
    }
    return new Response("final page", { status: 200, headers: { "content-type": "text/plain" } });
  }) as typeof fetch;
  try {
    await assert.rejects(fetchPage("https://example.com/redirect", { resolveDns: publicResolve }), /loopback/);
    const followed = await fetchPage("https://example.com/hop", { resolveDns: publicResolve });
    assert.equal(followed.content, "final page");
    assert.deepEqual(requests, ["https://example.com/redirect", "https://example.com/hop", "https://example.org/final"]);
  } finally {
    globalThis.fetch = original;
  }
});

test("oversized responses stop reading at the byte budget", async () => {
  const original = globalThis.fetch;
  const bigStream = new ReadableStream<Uint8Array>({
    start(controller) {
      const chunk = new Uint8Array(16 * 1024).fill(97); // "a"
      for (let i = 0; i < 40; i += 1) controller.enqueue(chunk); // 640 KiB > budget
      controller.close();
    }
  });
  globalThis.fetch = (async (input: string | URL) => {
    if (String(input).startsWith("https://r.jina.ai/")) return new Response(bigStream, { status: 200 });
    throw new Error("unexpected direct fetch");
  }) as typeof fetch;
  try {
    const page = await fetchPage("https://example.com/huge", { resolveDns: publicResolve });
    assert.equal(page.source, "jina");
    // 640 KiB of input collapses to the bounded content: reading stopped at
    // the byte budget and the final projection truncated to the char budget.
    assert.match(page.content, /truncated at 30000 characters/);
    assert.ok(page.content.length < 200_000, `content should be bounded, got ${page.content.length}`);
  } finally {
    globalThis.fetch = original;
  }
});
