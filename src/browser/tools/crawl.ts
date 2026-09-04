import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { BrowserManager, BrowserDegradedError } from "../runtime/browser-manager";
import { authSignal, extractApiRoutes, normalizeUrl, sameHost } from "./crawl-core";
import type { ToolOutputStore } from "@/server/tool-output";

/**
 * The crawl tool: breadth-first attack-surface discovery through the scoped
 * browser. Collects links, forms (with hidden fields), JS-bundle API routes,
 * and auth boundaries into one structured inventory the model can plan
 * against — closing the "never found that odd endpoint" failure mode.
 *
 * Scope safety is inherited from BrowserManager.navigate: every hop is
 * validated against the configured scope and out-of-scope URLs throw instead
 * of being followed. Only same-host links are followed; cross-host links are
 * recorded as leads, not crawled.
 */

/** Resource-destruction failure: pierces the best-effort route-probe catch — the crawl loop must stop, not degrade. */
class CrawlDestructionError extends Error {}

type PageFacts = {
  url: string;
  title: string;
  links: Array<{ href: string; text: string }>;
  forms: Array<{ action: string; method: string; fields: Array<{ name: string; type: string; hidden: boolean }> }>;
  metaGenerator: string;
  routes: string[];
};

const PAGE_PROBE = `(async () => {
  const clean = (s, n) => (s || "").replace(/\\s+/g, " ").trim().slice(0, n);
  const links = [...document.querySelectorAll("a[href]")].slice(0, 120).map(a => ({ href: a.href, text: clean(a.textContent, 60) }));
  const forms = [...document.querySelectorAll("form")].slice(0, 30).map(f => ({
    action: f.action || location.href,
    method: (f.method || "get").toUpperCase(),
    fields: [...f.querySelectorAll("input,select,textarea")].slice(0, 40).map(i => ({ name: i.name || "", type: i.type || i.tagName.toLowerCase(), hidden: i.type === "hidden" }))
  }));
  const out = { url: location.href, title: clean(document.title, 100), links, forms, metaGenerator: clean((document.querySelector('meta[name="generator"]') || {}).content, 80) };
  // evaluate() truncates its serialized result at 8KB — shrink until the JSON
  // fits, or the whole page silently degrades to empty facts.
  while (JSON.stringify(out, null, 2).length > 7000 && out.links.length) out.links.pop();
  while (JSON.stringify(out, null, 2).length > 7000 && out.forms.length) out.forms.pop();
  return out;
})()`;

// Per-bundle: 8s timeout (a hanging same-origin script must not stall the
// whole crawl) and a 512KB read budget (streamed, then cancelled — a huge
// bundle is never buffered whole).
const ROUTE_PROBE = `(async () => {
  const srcs = [...document.scripts].filter(s => s.src && new URL(s.src).origin === location.origin).map(s => s.src).slice(0, 10);
  const raw = [];
  const deadline = Date.now() + 12000; // whole-probe budget, not per-bundle
  for (const src of srcs) {
    if (Date.now() > deadline) break; // slow bundles must not own the browser lane
    try {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      const response = await fetch(src, { signal: AbortSignal.timeout(Math.min(8000, remaining)) });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let text = "";
      let budget = 524288; // bytes (byteLength), not UTF-16 chars
      while (budget > 0) {
        if (Date.now() > deadline) break;
        const { done, value } = await reader.read();
        if (done) break;
        const take = value.subarray(0, budget);
        budget -= take.byteLength;
        text += decoder.decode(take, { stream: true });
      }
      text += decoder.decode(); // flush the streaming tail
      await reader.cancel().catch(() => {});
      raw.push(...(text.match(/["'\\\`]\\/[A-Za-z0-9_\\\\\\-~%.\\/]{3,120}["'\\\`]/g) || []).slice(0, 400));
    } catch (e) { /* unreadable/timed-out bundle: skip */ }
  }
  // Same 8KB serialization budget as the page probe.
  const routes = [...new Set(raw)];
  while (JSON.stringify(routes, null, 2).length > 7000) routes.pop();
  return routes;
})()`;

/** browser.evaluate returns a SERIALIZED string (JSON or String()); truncated output degrades gracefully. */
function parseEvaluation<T>(raw: string, fallback: T): T {
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

const EMPTY_FACTS: PageFacts = { url: "", title: "", links: [], forms: [], metaGenerator: "", routes: [] };

/**
 * One page = ONE critical section: navigate + both probes run inside a single
 * browser.run() so a concurrent browser/crawl call can never switch the active
 * page between navigation and extraction (that misattributed pages' facts).
 * The signal propagates into the chain so a stop drops queued hops.
 */
async function crawlPage(browser: BrowserManager, url: string, signal?: AbortSignal): Promise<PageFacts> {
  return browser.run(async () => {
    // Whole-page cap via the manager's timing contract: the internal
    // snapshot() evaluate is bounded; a timeout destroys the page (settling
    // the navigate) before the lane releases. undefined = timed-out skip.
    const navResult = await browser.navigateWithDeadline(url, 30_000);
    if (navResult === undefined) throw new Error(`page navigation exceeded 30s (page destroyed): ${url}`);
    const base = parseEvaluation<PageFacts>(await browser.evaluateWithDeadline(PAGE_PROBE, 10_000) ?? "", EMPTY_FACTS);
    if (!base.url) throw new Error("page probe result was truncated or unparseable");
    let routes: string[] = [];
    // The timing contract lives in BrowserManager.evaluateWithDeadline:
    // bounded patience, forced destruction on timeout, lane held until the
    // evaluate settles. undefined = deadline hit (destruction confirmed):
    // routes degrade to empty, page facts stay. A destruction failure throws
    // (the manager flags itself degraded) and stops the crawl.
    let evaluateResult: string | undefined;
    try {
      evaluateResult = await browser.evaluateWithDeadline(ROUTE_PROBE, 15_000);
    } catch (error) {
      // Only a degradation (destruction NOT confirmed) stops the crawl;
      // ordinary evaluate errors (syntax, page closed, Playwright errors)
      // propagate as normal page errors into the errors[] list.
      if (error instanceof BrowserDegradedError) throw new CrawlDestructionError(error.message);
      throw error;
    }
    if (evaluateResult !== undefined) {
      const raw = parseEvaluation<string[]>(evaluateResult, []);
      routes = extractApiRoutes(Array.isArray(raw) ? raw : []);
    }
    return { ...base, routes };
  }, signal);
}

export function createCrawlTool(browser: BrowserManager, outputStore?: ToolOutputStore): ToolDefinition {
  return defineTool({
    name: "crawl",
    label: "Crawl attack surface",
    description:
      "Breadth-first crawl of an in-scope web application through the scoped browser: collects every link, form (including hidden fields), API routes extracted from loaded JS bundles, and auth boundaries into one structured inventory. Use it right after the first navigate to map the attack surface before hypothesizing vulnerabilities, and feed the discovered endpoints into the matching exploit skills. Only same-host links are followed (cross-host links are recorded as leads); every hop is scope-checked. Verbose inventories return a bounded preview plus a local full-output path — record actual exposures with record_finding.",
    promptSnippet: "crawl(entry, maxPages?, maxDepth?)",
    executionMode: "parallel",
    parameters: Type.Object({
      entry: Type.String({ description: "Absolute http(s) URL to start from" }),
      maxPages: Type.Optional(Type.Integer({ minimum: 1, maximum: 40, description: "Page budget (default 15)" })),
      maxDepth: Type.Optional(Type.Integer({ minimum: 0, maximum: 4, description: "Link-follow depth from the entry (default 2)" }))
    }),
    async execute(_toolCallId, params, signal) {
      const entry = normalizeUrl(params.entry);
      if (!entry) throw new Error("crawl requires an absolute http(s) entry URL");
      const maxPages = Math.min(40, Math.max(1, Math.round(params.maxPages ?? 15)));
      const maxDepth = Math.min(4, Math.max(0, Math.round(params.maxDepth ?? 2)));
      const visited = new Set<string>();
      const queue: Array<{ url: string; depth: number }> = [{ url: entry, depth: 0 }];
      const pages: Array<PageFacts & { auth: string; depth: number }> = [];
      const crossHostLeads = new Set<string>();
      const allRoutes = new Set<string>();
      const errors: string[] = [];
      let aborted = false;
      let abortReason: "stop" | "degraded" | "deadline" = "stop";

      // Whole-crawl deadline: the per-page caps (30s nav + 15s probe) bound a
      // single hop, but 40 pages could otherwise run ~30 minutes.
      const crawlDeadline = Date.now() + maxPages * 50_000;
      while (queue.length > 0 && pages.length < maxPages) {
        if (signal?.aborted) { aborted = true; break; }
        if (Date.now() > crawlDeadline) { aborted = true; abortReason = "deadline"; break; }
        const { url, depth } = queue.shift()!;
        const normalized = normalizeUrl(url);
        if (!normalized || visited.has(normalized)) continue;
        visited.add(normalized);
        try {
          const facts = await crawlPage(browser, normalized, signal);
          const auth = authSignal(facts.url);
          pages.push({ ...facts, routes: facts.routes, auth, depth });
          for (const route of facts.routes) allRoutes.add(route);
          if (depth < maxDepth) {
            for (const link of facts.links) {
              const target = normalizeUrl(link.href);
              if (!target) continue;
              if (!sameHost(target, entry)) {
                if (crossHostLeads.size < 30) crossHostLeads.add(new URL(target).host);
                continue;
              }
              if (!visited.has(target)) queue.push({ url: target, depth: depth + 1 });
            }
          }
        } catch (error) {
          if (signal?.aborted) { aborted = true; break; }
          errors.push(`${normalized}: ${error instanceof Error ? error.message : String(error)}`);
          if (error instanceof CrawlDestructionError) { aborted = true; abortReason = "degraded"; break; }
        }
      }

      const forms = pages.flatMap((page) => page.forms.map((form) => ({ page: page.url, ...form })));
      const links = new Set<string>();
      for (const page of pages) for (const link of page.links) links.add(normalizeUrl(link.href) || link.href);
      const anonymousReachable = pages.filter((page) => page.auth === "none").map((page) => page.url);
      const loginWalled = pages.filter((page) => page.auth === "login-redirect").map((page) => page.url);
      const generators = [...new Set(pages.map((page) => page.metaGenerator).filter(Boolean))];

      const report = [
        `${aborted ? `CRAWL ABORTED (${abortReason === "degraded" ? "browser degraded — context destruction failed, browser unusable" : abortReason === "deadline" ? "time budget exceeded" : "user stop"}) — partial inventory` : `Crawled ${pages.length} page(s) from ${entry}`} (depth ≤ ${maxDepth}).`,
        `\n## Pages (${pages.length})`,
        ...pages.map((page) => `- [${page.auth === "login-redirect" ? "AUTH" : "ok"}] ${page.url}${page.title ? ` — ${page.title}` : ""}`),
        loginWalled.length ? `\nLogin-walled (${loginWalled.length}): test with authenticated identities (use_identity + cookies_import).` : "",
        `\n## Links (${links.size})`,
        ...[...links].slice(0, 120).map((link) => `- ${link}`),
        crossHostLeads.size ? `\nCross-host leads (NOT crawled): ${[...crossHostLeads].join(", ")}` : "",
        `\n## Forms (${forms.length})`,
        ...forms.slice(0, 40).map((form) => {
          const fields = form.fields.map((field) => field.hidden ? `${field.name}(hidden)` : field.name).filter(Boolean).join(", ");
          return `- ${form.method} ${form.action}${fields ? ` — ${fields}` : ""}`;
        }),
        `\n## JS-discovered routes (${allRoutes.size})`,
        ...[...allRoutes].sort().slice(0, 200).map((route) => `- ${route}`),
        anonymousReachable.length ? `\nAnonymous-reachable pages: ${anonymousReachable.length}/${pages.length}` : "",
        generators.length ? `\nGenerators: ${generators.join(", ")}` : "",
        errors.length ? `\nSkipped/errors (${errors.length}):\n${errors.slice(0, 10).map((line) => `- ${line}`).join("\n")}` : ""
      ].filter(Boolean).join("\n");
      const projected = outputStore
        ? await outputStore.project("crawl", [report], `crawl mapped ${pages.length} page(s), ${links.size} link(s), ${forms.length} form(s), and ${allRoutes.size} JS route(s).`)
        : { text: report };
      return {
        content: [{ type: "text" as const, text: projected.text }],
        details: { entry, pages: pages.length, links: links.size, forms: forms.length, routes: allRoutes.size, artifactPath: projected.artifactPath, truncation: projected.truncation }
      };
    }
  });
}
