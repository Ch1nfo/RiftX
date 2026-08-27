/** Web research: provider-backed public-web search with CVE direct routing and OPSEC screening. */

export type WebSearchResult = { title: string; url: string; snippet: string };

export type WebSearchOutcome = {
  results: WebSearchResult[];
  provider: "duckduckgo" | "tavily" | "duckduckgo+cve" | "tavily+cve";
  cveDetail?: string;
};

export type WebSearchOptions = { tavilyApiKey?: string; limit?: number; signal?: AbortSignal };

const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

const CVE_ID_PATTERN = /\bCVE-\d{4}-\d{4,8}\b/i;

/**
 * OPSEC: a search query is sent to third-party engines. It must never carry
 * engagement secrets — only identifiers (CVE ids, product names, versions).
 * Each pattern names what it looks like so the rejection is actionable.
 */
const SENSITIVE_QUERY_PATTERNS: Array<[RegExp, string]> = [
  [/sk-[A-Za-z0-9_-]{16,}/, "an OpenAI-style API key"],
  [/(ghp|gho|github_pat)_[A-Za-z0-9_]{20,}/, "a GitHub token"],
  [/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/, "a JWT"],
  [/[A-Fa-f0-9]{40,}/, "a hex-encoded secret"],
  [/[A-Za-z0-9+/]{48,}={0,2}/, "a base64-encoded secret"]
];

export function screenQuery(query: string): Error | null {
  for (const [pattern, what] of SENSITIVE_QUERY_PATTERNS) {
    if (pattern.test(query)) {
      return new Error(
        `Refusing to search: the query appears to contain ${what}. Web search sends queries to third-party engines — search identifiers (CVE ids, product names, versions) only; never credentials, cookies, tokens, or secrets from the engagement.`
      );
    }
  }
  return null;
}

function decodeEntities(text: string) {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

function stripTags(html: string) {
  return decodeEntities(html.replace(/<[^>]*>/g, "")).replace(/\s+/g, " ").trim();
}

/** Result links point at a DuckDuckGo redirect carrying the real URL in `uddg`. */
function decodeDuckDuckGoHref(href: string) {
  const match = href.match(/[?&]uddg=([^&]+)/);
  if (match) {
    try { return decodeURIComponent(match[1]); } catch { /* fall through */ }
  }
  return href.startsWith("//") ? `https:${href}` : href;
}

export function parseDuckDuckGoHtml(html: string, limit: number): WebSearchResult[] {
  const anchors = [...html.matchAll(/<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g)];
  const snippets = [...html.matchAll(/<a[^>]+class="[^"]*\bresult__snippet\b[^"]*"[^>]*>([\s\S]*?)<\/a>/g)];
  return anchors
    .slice(0, limit)
    .map((anchor, index) => ({
      title: stripTags(anchor[2]),
      url: decodeDuckDuckGoHref(decodeEntities(anchor[1])),
      snippet: snippets[index] ? stripTags(snippets[index][1]) : ""
    }))
    .filter((item) => item.title && /^https?:\/\//.test(item.url));
}

async function searchDuckDuckGo(query: string, limit: number, signal?: AbortSignal): Promise<WebSearchResult[]> {
  const response = await fetch(`https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`, {
    headers: { "user-agent": USER_AGENT, accept: "text/html" },
    signal
  });
  if (response.status === 429) {
    throw new Error("DuckDuckGo rate limit reached. Wait before searching again, or configure a Tavily API key in Settings → Web Research for a more stable provider.");
  }
  if (!response.ok) {
    throw new Error(`DuckDuckGo search failed (HTTP ${response.status}). A Tavily API key can be configured in Settings → Web Research.`);
  }
  return parseDuckDuckGoHtml(await response.text(), limit);
}

async function searchTavily(query: string, apiKey: string, limit: number, signal?: AbortSignal): Promise<WebSearchResult[]> {
  const response = await fetch("https://api.tavily.com/search", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({ query, max_results: limit }),
    signal
  });
  if (response.status === 401 || response.status === 403) {
    throw new Error("Tavily rejected the configured API key. Check Settings → Web Research.");
  }
  if (!response.ok) throw new Error(`Tavily search failed (HTTP ${response.status}).`);
  const data = await response.json() as { results?: Array<{ title?: string; url?: string; content?: string }> };
  return (data.results ?? [])
    .map((item) => ({ title: item.title ?? item.url ?? "", url: item.url ?? "", snippet: item.content ?? "" }))
    .filter((item) => /^https?:\/\//.test(item.url))
    .slice(0, limit);
}

type CveRecord = { id?: string; summary?: string; cvss?: number | null; references?: string[] };

/** Keyless structured lookup via the CIRCL CVE API — no search quota spent. */
export async function fetchCveDetail(cveId: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(`https://cve.circl.lu/api/cve/${cveId.toUpperCase()}`, {
    headers: { accept: "application/json" },
    signal
  });
  if (!response.ok) throw new Error(`CVE lookup failed (HTTP ${response.status}).`);
  const data = await response.json() as CveRecord;
  const lines = [
    `CVE: ${data.id ?? cveId.toUpperCase()}`,
    data.cvss != null ? `CVSS: ${data.cvss}` : null,
    data.summary ? `Summary: ${stripTags(data.summary)}` : null,
    ...((data.references ?? []).slice(0, 5).map((reference, index) => `Reference ${index + 1}: ${reference}`))
  ].filter((line): line is string => Boolean(line));
  return lines.join("\n");
}

export async function webSearch(query: string, options: WebSearchOptions = {}): Promise<WebSearchOutcome> {
  const screened = screenQuery(query);
  if (screened) throw screened;
  const limit = Math.min(10, Math.max(1, options.limit ?? 5));
  const useTavily = Boolean(options.tavilyApiKey?.trim());
  const search = useTavily
    ? () => searchTavily(query, options.tavilyApiKey!.trim(), limit, options.signal)
    : () => searchDuckDuckGo(query, limit, options.signal);

  const cveId = query.trim().match(CVE_ID_PATTERN)?.[0];
  if (cveId) {
    // CVE-bearing queries get the structured record plus ordinary results.
    // The CVE detail is optional: a failed lookup (unknown id, API down) must
    // not discard the already-fetched search results or re-run the provider —
    // that would double rate-limit usage for every unknown id.
    const [results, cveDetail] = await Promise.all([
      search(),
      fetchCveDetail(cveId, options.signal).then((detail) => detail, () => undefined)
    ]);
    return { results, provider: useTavily ? "tavily+cve" : "duckduckgo+cve", cveDetail };
  }
  return { results: await search(), provider: useTavily ? "tavily" : "duckduckgo" };
}
