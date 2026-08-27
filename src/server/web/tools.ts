import { Type } from "@sinclair/typebox";
import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { webSearch } from "./search";
import { fetchPage } from "./fetch-page";

/**
 * Web research tools: keyless DuckDuckGo search by default, Tavily when a key
 * is configured. These are read-only research actions against the public web —
 * they take no approval and never touch the target-scoped browser state.
 */
export type WebToolOptions = {
  /** Read at execution time so a saved key applies to running sessions immediately. */
  getTavilyApiKey?: () => Promise<string | undefined>;
};

export function createWebTools(options: WebToolOptions = {}): ToolDefinition[] {
  const webSearchTool = defineTool({
    name: "web_search",
    label: "Web search",
    description:
      "Search the public web for research: known vulnerabilities for a fingerprinted product/version, CVE details (a bare CVE id also returns structured CVE data), exploit references, product documentation, unfamiliar error triage. OPSEC: queries go to third-party engines — include identifiers only (CVE ids, product names, versions); never credentials, cookies, tokens, or target-internal hostnames. Queries that look like secrets are rejected. The default provider is keyless DuckDuckGo and can rate-limit; a Tavily API key can be configured in settings for a more stable provider.",
    promptSnippet: "web_search(query, limit?)",
    parameters: Type.Object({
      query: Type.String({ description: "Search query — identifiers only, no secrets" }),
      limit: Type.Optional(Type.Number({ minimum: 1, maximum: 10, description: "Maximum results (default 5)" }))
    }),
    async execute(_toolCallId, params, signal) {
      const tavilyApiKey = (await options.getTavilyApiKey?.())?.trim() || undefined;
      const outcome = await webSearch(params.query, { tavilyApiKey, limit: params.limit, signal });
      const listing = outcome.results.map((result, index) =>
        `${index + 1}. ${result.title}\n   ${result.url}${result.snippet ? `\n   ${result.snippet}` : ""}`
      );
      const text = [outcome.cveDetail, listing.join("\n")].filter(Boolean).join("\n\n") || "No results found.";
      return {
        content: [{ type: "text" as const, text: `web_search (${outcome.provider}):\n\n${text}` }],
        details: { provider: outcome.provider, resultCount: outcome.results.length, query: params.query }
      };
    }
  });

  const webFetchTool = defineTool({
    name: "web_fetch",
    label: "Web fetch",
    description:
      "Fetch a public web page or document as clean text for research (CVE advisories, exploit write-ups, product docs, API references). Use it for out-of-target research URLs; use the browser tool for interactions with the target. Long pages are truncated; use web_search first when you do not have a URL.",
    promptSnippet: "web_fetch(url)",
    parameters: Type.Object({
      url: Type.String({ description: "http(s) URL to fetch" })
    }),
    async execute(_toolCallId, params, signal) {
      const page = await fetchPage(params.url, { signal });
      return {
        content: [{ type: "text" as const, text: page.content }],
        details: { url: params.url, source: page.source }
      };
    }
  });

  return [webSearchTool, webFetchTool];
}
