import { isIP } from "node:net";
import { lookup } from "node:dns/promises";

/**
 * SSRF guard for web research: web_fetch runs from the RiftX process and must
 * never be steered at loopback, private, link-local (cloud metadata), or
 * reserved addresses — that would bypass the browser scope and the approval
 * gate. Every fetch, including each redirect hop, passes through this check,
 * and DNS answers are validated too so a public name cannot point inward.
 */

const BLOCKED_HOSTNAMES = new Set(["localhost", "ip6-localhost", "ip6-loopback"]);

function isBlockedHostnameName(hostname: string) {
  const name = hostname.toLowerCase();
  return BLOCKED_HOSTNAMES.has(name) || name.endsWith(".localhost") || name.endsWith(".local") || name.endsWith(".internal");
}

function ipv4ToLong(ip: string) {
  const parts = ip.split(".").map(Number);
  return (((parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]) >>> 0);
}

const BLOCKED_IPV4_CIDRS: Array<[network: number, maskBits: number, label: string]> = [
  [ipv4ToLong("0.0.0.0"), 8, "the unspecified range"],
  [ipv4ToLong("10.0.0.0"), 8, "a private network"],
  [ipv4ToLong("100.64.0.0"), 10, "the carrier-grade NAT range"],
  [ipv4ToLong("127.0.0.0"), 8, "a loopback address"],
  [ipv4ToLong("169.254.0.0"), 16, "a link-local address (including cloud metadata)"],
  [ipv4ToLong("172.16.0.0"), 12, "a private network"],
  [ipv4ToLong("192.0.0.0"), 24, "a reserved range"],
  [ipv4ToLong("192.168.0.0"), 16, "a private network"],
  [ipv4ToLong("198.18.0.0"), 15, "a benchmarking range"],
  [ipv4ToLong("224.0.0.0"), 4, "a multicast address"],
  [ipv4ToLong("240.0.0.0"), 4, "a reserved range"]
];

/**
 * Expand an IPv6 literal into eight numeric 16-bit groups. Handles `::`
 * compression and dotted-IPv4 tails so every textual representation of an
 * address normalizes to the same groups — string pattern-matching alone
 * misses forms like `::ffff:7f00:1` (== 127.0.0.1 in hex).
 */
function expandIpv6(address: string): number[] | null {
  const dblIndex = address.indexOf("::");
  const hasCompression = dblIndex >= 0;
  if (hasCompression && address.indexOf("::", dblIndex + 1) !== -1) return null;
  const parseGroups = (part: string): number[] | null => {
    if (part === "") return [];
    const segments = part.split(":");
    const groups: number[] = [];
    for (let index = 0; index < segments.length; index += 1) {
      const segment = segments[index];
      if (segment.includes(".")) {
        // An embedded dotted IPv4 is only legal as the final segment.
        if (index !== segments.length - 1 || isIP(segment) !== 4) return null;
        const long = ipv4ToLong(segment);
        groups.push((long >>> 16) & 0xffff, long & 0xffff);
      } else {
        if (!/^[0-9a-fA-F]{1,4}$/.test(segment)) return null;
        groups.push(parseInt(segment, 16));
      }
    }
    return groups;
  };
  const left = parseGroups(hasCompression ? address.slice(0, dblIndex) : address);
  const right = hasCompression ? parseGroups(address.slice(dblIndex + 2)) : [];
  if (left === null || right === null) return null;
  if (hasCompression) {
    if (left.length + right.length >= 8) return null;
    return [...left, ...new Array<number>(8 - left.length - right.length).fill(0), ...right];
  }
  return left.length === 8 ? left : null;
}

function embeddedIpv4(groups: number[]): string {
  const long = (groups[6] << 16) | groups[7];
  return `${(long >>> 24) & 0xff}.${(long >>> 16) & 0xff}.${(long >>> 8) & 0xff}.${long & 0xff}`;
}

export function isBlockedIp(address: string): string | null {
  const family = isIP(address);
  if (family === 4) {
    const value = ipv4ToLong(address);
    for (const [network, bits, label] of BLOCKED_IPV4_CIDRS) {
      const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
      if ((value & mask) === (network & mask)) return label;
    }
    return null;
  }
  if (family === 6) {
    const groups = expandIpv6(address);
    if (!groups) return "an unparseable IPv6 address";
    if (groups.every((group) => group === 0)) return "an unspecified address";
    if (groups.slice(0, 7).every((group) => group === 0) && groups[7] === 1) return "a loopback address";
    // IPv4-mapped ::ffff:0:0/96 — dotted AND hex tails must reduce to the
    // embedded IPv4 and re-run the v4 checks (::ffff:7f00:1 == 127.0.0.1).
    if (groups.slice(0, 5).every((group) => group === 0) && groups[5] === 0xffff) {
      return isBlockedIp(embeddedIpv4(groups));
    }
    // IPv4-compatible (deprecated) ::a.b.c.d / ::7f00:1 forms.
    if (groups.slice(0, 6).every((group) => group === 0)) {
      return isBlockedIp(embeddedIpv4(groups));
    }
    // NAT64 64:ff9b::/96 embeds an IPv4 destination too.
    if (groups[0] === 0x64 && groups[1] === 0xff9b && groups.slice(2, 6).every((group) => group === 0)) {
      return isBlockedIp(embeddedIpv4(groups));
    }
    const first = groups[0];
    if ((first & 0xfe00) === 0xfc00) return "a unique-local address";
    if ((first & 0xffc0) === 0xfe80) return "a link-local address";
    if (first === 0x2001 && groups[1] === 0x0db8) return "a documentation address";
    return null;
  }
  return null;
}

export type UrlGuardOptions = { resolveDns?: (hostname: string) => Promise<string[]> };

export async function assertFetchableUrl(rawUrl: string, options: UrlGuardOptions = {}): Promise<URL> {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    throw new Error(`web_fetch received an invalid URL: ${rawUrl}`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`web_fetch only supports http(s) URLs, got: ${parsed.protocol}`);
  }
  // Credentials in the URL are engagement material at worst and never needed
  // for public research pages.
  if (parsed.username || parsed.password) {
    throw new Error("web_fetch refuses URLs with embedded credentials.");
  }
  const hostname = parsed.hostname.replace(/^\[|\]$/g, "");
  const blockedName = isBlockedHostnameName(hostname);
  if (blockedName) throw new Error(`web_fetch refuses local hostnames (${hostname}). Only public research URLs are allowed.`);
  const literalBlock = isBlockedIp(hostname);
  if (literalBlock) throw new Error(`web_fetch refuses ${literalBlock}: ${hostname}. Only public research URLs are allowed.`);
  // Resolve and validate: a public-looking domain must not answer to an
  // internal address (coversDNS-pointed-inward setups; re-binding races
  // remain possible and are out of scope for a research tool).
  if (isIP(hostname) === 0) {
    const resolve = options.resolveDns ?? (async (name: string) => (await lookup(name, { all: true })).map((entry) => entry.address));
    let addresses: string[];
    try {
      addresses = await resolve(hostname);
    } catch {
      throw new Error(`web_fetch could not resolve ${hostname}.`);
    }
    for (const address of addresses) {
      const blocked = isBlockedIp(address);
      if (blocked) throw new Error(`web_fetch refuses ${hostname}: it resolves to ${blocked} (${address}). Only public research URLs are allowed.`);
    }
  }
  return parsed;
}
