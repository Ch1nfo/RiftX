import { isIP } from "node:net";

/**
 * Scope rules authorize which hosts a RiftX browser may navigate to.
 *
 * A rule is one of:
 *   - `10.0.0.0/8`            IPv4/IPv6 CIDR block (any port, any scheme)
 *   - `10.0.181.248`          exact host, any port
 *   - `10.0.181.248:8000`     exact host and port
 *   - `*.target.com`          target.com and any subdomain, any port
 *   - `https://target.com`    scheme-restricted host
 *
 * Hosts are matched case-insensitively with trailing dots normalized. A rule
 * without a port matches every port; a rule without a scheme matches both
 * http and https. Single-label intranet hostnames are valid rules. Invalid
 * rules parse to undefined and are ignored.
 */

export type ParsedScopeRule = {
  raw: string;
  scheme?: "http" | "https";
  host: string;
  wildcard: boolean;
  cidr?: { value: bigint; bits: number; family: 4 | 6 };
  port?: number;
};

export type ScopeTarget = {
  url: string;
  scheme: "http" | "https";
  host: string;
  port: number;
};

export type ScopeDecision = {
  allowed: boolean;
  host: string;
  port: number;
  suggestedRule: string;
  reason?: string;
};

const MAX_BITS: Record<4 | 6, bigint> = { 4: 32n, 6: 128n };

function ipToBigInt(ip: string): { value: bigint; family: 4 | 6 } | undefined {
  const family = isIP(ip);
  if (family === 4) {
    const parts = ip.split(".").map(Number);
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return undefined;
    return { value: parts.reduce((total, part) => (total << 8n) | BigInt(part), 0n), family: 4 };
  }
  if (family === 6) {
    const groups = expandIPv6(ip);
    if (!groups) return undefined;
    return { value: groups.reduce((total, group) => (total << 16n) | BigInt(parseInt(group, 16)), 0n), family: 6 };
  }
  return undefined;
}

function expandIPv6(raw: string): string[] | undefined {
  let ip = raw.split("%")[0];
  // Convert an embedded IPv4 tail (e.g. ::ffff:192.168.0.1) into hex groups.
  const lastColon = ip.lastIndexOf(":");
  if (lastColon >= 0 && ip.slice(lastColon + 1).includes(".")) {
    const v4 = ip.slice(lastColon + 1);
    if (isIP(v4) !== 4) return undefined;
    const [a, b, c, d] = v4.split(".").map(Number);
    ip = `${ip.slice(0, lastColon + 1)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const sections = ip.split("::");
  if (sections.length > 2) return undefined;
  const head = sections[0] ? sections[0].split(":") : [];
  const tail = sections.length === 2 && sections[1] ? sections[1].split(":") : [];
  const missing = 8 - head.length - tail.length;
  if (sections.length === 2 && missing < 0) return undefined;
  if (sections.length === 1 && head.length !== 8) return undefined;
  const groups = sections.length === 2 ? [...head, ...Array.from({ length: missing }, () => "0"), ...tail] : head;
  if (groups.length !== 8 || groups.some((group) => !/^[0-9a-f]{1,4}$/.test(group))) return undefined;
  return groups.map((group) => group.padStart(4, "0"));
}

function splitPort(host: string): { host: string; port?: number } {
  const bracketMatch = host.match(/^\[(.+)\](?::(\d+))?$/);
  if (bracketMatch) {
    const port = bracketMatch[2] ? Number(bracketMatch[2]) : undefined;
    return port && (port < 1 || port > 65535) ? { host } : { host: bracketMatch[1], port };
  }
  // A single colon separates host and port only when the tail is all digits;
  // bare IPv6 literals (multiple colons) never carry a port without brackets.
  if ((host.match(/:/g) ?? []).length === 1) {
    const [name, portText] = host.split(":");
    if (/^\d+$/.test(portText)) {
      const port = Number(portText);
      if (port >= 1 && port <= 65535) return { host: name, port };
    }
  }
  return { host };
}

function normalizeHost(host: string) {
  return host.replace(/\.$/, "").toLowerCase();
}

const HOST_BODY = /^[a-z0-9_]([a-z0-9_*.-]*[a-z0-9_])?$/;

export function parseScopeRule(rawInput: string): ParsedScopeRule | undefined {
  const raw = rawInput.trim().toLowerCase();
  if (!raw || raw.length > 280) return undefined;
  let scheme: "http" | "https" | undefined;
  let rest = raw;
  const schemeMatch = rest.match(/^(https?):\/\//);
  if (schemeMatch) {
    scheme = schemeMatch[1] as "http" | "https";
    rest = rest.slice(schemeMatch[0].length);
  }
  rest = rest.replace(/^\/+|\/+$/g, "");
  if (!rest) return undefined;
  const { host: hostWithPort, port } = splitPort(rest);
  const host = normalizeHost(hostWithPort);
  if (!host) return undefined;

  const slashIndex = host.indexOf("/");
  if (slashIndex >= 0) {
    // CIDR rules apply to a network, not one origin; a port suffix is
    // ambiguous and must not silently broaden to every port.
    if (port !== undefined) return undefined;
    const addressText = host.slice(0, slashIndex);
    const bitsText = host.slice(slashIndex + 1);
    if (!/^\d+$/.test(bitsText) || bitsText.length > 3) return undefined;
    const bits = Number(bitsText);
    const address = ipToBigInt(addressText);
    if (!address || BigInt(bits) > MAX_BITS[address.family]) return undefined;
    return { raw: rawInput.trim(), scheme, host, wildcard: false, cidr: { value: address.value, bits, family: address.family } };
  }
  const wildcard = host.startsWith("*.");
  const body = wildcard ? host.slice(2) : host;
  // Bare IPv6 literals contain colons and are exempt from hostname-shape checks.
  if (!body || (!HOST_BODY.test(body) && isIP(body) === 0)) return undefined;
  if (!wildcard && host.includes("*")) return undefined;
  return { raw: rawInput.trim(), scheme, host: body, wildcard, port };
}

export function parseScopeTarget(rawUrl: string): ScopeTarget | undefined {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return undefined;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return undefined;
  // WHATWG URLs serialize IPv6 hostnames with brackets; strip them for matching.
  const bracketed = parsed.hostname.match(/^\[(.+)\]$/);
  const host = normalizeHost(bracketed ? bracketed[1] : parsed.hostname);
  if (!host) return undefined;
  const port = parsed.port ? Number(parsed.port) : parsed.protocol === "https:" ? 443 : 80;
  return { url: parsed.toString(), scheme: parsed.protocol.slice(0, -1) as "http" | "https", host, port };
}

export function hostMatches(rule: ParsedScopeRule, host: string) {
  if (rule.wildcard) return host === rule.host || host.endsWith(`.${rule.host}`);
  if (rule.cidr) {
    const address = ipToBigInt(host);
    if (!address || address.family !== rule.cidr.family) return false;
    const networkBits = MAX_BITS[rule.cidr.family] - BigInt(rule.cidr.bits);
    return address.value >> networkBits === rule.cidr.value >> networkBits;
  }
  return host === rule.host;
}

export function matchScopeUrl(rules: readonly ParsedScopeRule[], target: ScopeTarget): boolean {
  return rules.some((rule) => hostMatches(rule, target.host)
    && (!rule.scheme || rule.scheme === target.scheme)
    && (rule.port === undefined || rule.port === target.port));
}

export function parseScopeRules(rawRules: readonly string[]): ParsedScopeRule[] {
  return rawRules.map(parseScopeRule).filter((rule): rule is ParsedScopeRule => Boolean(rule));
}
