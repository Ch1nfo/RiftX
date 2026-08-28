/** Pure crawl helpers, SDK-import-free so they are unit-testable under tsx. */

const ASSET_EXTENSIONS = /\.(js|mjs|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|mp[34]|webm|pdf|zip|gz|wav)(\?|$)/i;
// Conservative on purpose: broad tokens like auth/session flag ordinary API paths as walls.
const LOGIN_PATTERN = /(login|sign-?in|logon|sso|oauth|\bcas\b)/i;

/** Fragment/default-port normalization so the same page is visited once. */
export function normalizeUrl(raw: string) {
  try {
    const url = new URL(raw);
    url.hash = "";
    if ((url.protocol === "http:" && url.port === "80") || (url.protocol === "https:" && url.port === "443")) url.port = "";
    let path = url.pathname;
    if (path.length > 1 && path.endsWith("/")) path = path.slice(0, -1);
    return `${url.protocol}//${url.host}${path}${url.search}`;
  } catch {
    return "";
  }
}

export function sameHost(a: string, b: string) {
  try {
    return new URL(a).host === new URL(b).host;
  } catch {
    return false;
  }
}

export function looksLikeRoute(candidate: string) {
  if (candidate.length < 3 || candidate.length > 120) return false;
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return false;
  if (ASSET_EXTENSIONS.test(candidate)) return false;
  if (/[\s{}<>\\]/.test(candidate)) return false;
  // At least one path segment beyond the root, or an explicit api prefix.
  return candidate.slice(1).includes("/") || /^\/(api|v\d)/i.test(candidate);
}

/** Filter raw quoted-string matches from a JS bundle down to plausible endpoints. */
export function extractApiRoutes(matches: readonly string[], limit = 200) {
  const seen = new Set<string>();
  for (const raw of matches) {
    const value = raw.slice(1, -1);
    if (!looksLikeRoute(value)) continue;
    if (seen.size >= limit) break;
    seen.add(value);
  }
  return [...seen];
}

export function authSignal(finalUrl: string) {
  try {
    const url = new URL(finalUrl);
    return LOGIN_PATTERN.test(url.pathname) || LOGIN_PATTERN.test(url.search) ? "login-redirect" : "none";
  } catch {
    return "unknown";
  }
}

