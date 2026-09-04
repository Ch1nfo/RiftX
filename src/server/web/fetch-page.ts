import { assertFetchableUrl, type UrlGuardOptions } from "./url-guard";

/** Web research: fetch a public page as clean text. Jina Reader first, local extraction as fallback. */

const USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const JINA_READER_PREFIX = "https://r.jina.ai/";
const FETCH_TIMEOUT_MS = 25_000;
const MAX_REDIRECTS = 4;

const MAX_CONTENT_CHARS = 30_000;
/** Byte ceiling while reading: the body is never fully buffered before truncation. */
const MAX_READ_BYTES = MAX_CONTENT_CHARS * 4;

/** Tag-stripping fallback for when the reader service is unavailable: crude, dependency-free, structure-preserving where cheap. */
export function htmlToText(html: string) {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<\/(p|div|h[1-6]|li|tr|table|section|article|pre|blockquote|ul|ol)>/gi, "\n")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;|&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/[ \t]+/g, " ")
    .replace(/\n +/g, "\n")
    .replace(/\n\s*\n\s*\n+/g, "\n\n")
    .trim();
}

function timedSignal(signal: AbortSignal | undefined, timeoutMs: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("fetch timed out")), timeoutMs);
  const onAbort = () => controller.abort(signal?.reason);
  if (signal?.aborted) {
    clearTimeout(timer);
    controller.abort(signal.reason);
  } else {
    signal?.addEventListener("abort", onAbort, { once: true });
  }
  return { signal: controller.signal, done: () => { clearTimeout(timer); signal?.removeEventListener("abort", onAbort); } };
}

/**
 * Stream-read a response body and stop at the byte budget, cancelling the
 * stream: a huge page can never make the process buffer it whole.
 */
async function readCapped(response: Response, maxBytes = MAX_READ_BYTES) {
  const body = response.body;
  if (!body) return await response.text();
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let received = 0;
  let text = "";
  let cut = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    text += decoder.decode(value, { stream: true });
    if (received >= maxBytes) {
      cut = true;
      await reader.cancel().catch(() => undefined);
      break;
    }
  }
  text += decoder.decode();
  return cut ? `${text}\n\n[... stopped reading: the response exceeded the size budget]` : text;
}

type FetchedPage = { content: string; source: "jina" | "direct" };

export async function fetchPage(url: string, options: { signal?: AbortSignal } & UrlGuardOptions = {}): Promise<FetchedPage> {
  if (options.signal?.aborted) throw new Error("fetch aborted before start");
  // Entry guard covers BOTH fetch paths: the direct request runs from this
  // process (SSRF), and the reader path would hand an internal address to a
  // third-party service (OPSEC) — neither is acceptable for research URLs.
  await assertFetchableUrl(url, { resolveDns: options.resolveDns });

  // Primary: the reader service returns clean markdown without a key
  // (rate-limited); it also handles JS-heavy pages a plain fetch cannot.
  const reader = timedSignal(options.signal, FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(`${JINA_READER_PREFIX}${url}`, {
      headers: { "user-agent": USER_AGENT, accept: "text/plain" },
      signal: reader.signal
    });
    if (response.ok) {
      const text = await readCapped(response);
      if (text.trim()) return { content: text, source: "jina" };
    }
  } catch {
    // fall through to the direct fetch
  } finally {
    reader.done();
  }

  // Direct fallback with manual redirects: every hop re-validates against the
  // SSRF guard so a public page cannot bounce the fetch inward.
  let current = url;
  const direct = timedSignal(options.signal, FETCH_TIMEOUT_MS);
  try {
    let response: Response | undefined;
    for (let hop = 0; hop <= MAX_REDIRECTS; hop += 1) {
      response = await fetch(current, { redirect: "manual", headers: { "user-agent": USER_AGENT, accept: "text/html,*/*" }, signal: direct.signal });
      if (![301, 302, 303, 307, 308].includes(response.status)) break;
      const location = response.headers.get("location");
      if (!location) throw new Error("the page redirected without a Location header");
      const next = await assertFetchableUrl(new URL(location, current).toString(), { resolveDns: options.resolveDns });
      if (hop === MAX_REDIRECTS) throw new Error("too many redirects");
      current = next.toString();
    }
    if (!response || !response.ok) throw new Error(`fetch failed (HTTP ${response?.status})`);
    const body = await readCapped(response);
    const contentType = response.headers.get("content-type") ?? "";
    const content = contentType.includes("html") ? htmlToText(body) : body;
    if (!content.trim()) throw new Error("the page rendered to empty content");
    return { content, source: "direct" };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`Could not fetch ${url} via the reader service or directly: ${reason}`);
  } finally {
    direct.done();
  }
}
