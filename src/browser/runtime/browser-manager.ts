import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import type { BrowserContext, CDPSession, Page, Route, WebSocketRoute } from "playwright";
import { ContextManager } from "./context-manager";
import { HostMappingProxy, type HostMappingTarget } from "./host-mapping-proxy";
import { PageManager } from "./page-manager";
import { createSnapshot } from "../snapshot/snapshot";
import { ElementRefMapper } from "../snapshot/element-refs";
import { RequestStore, redactHeaders } from "../network/request-store";
import { hostMatches, matchScopeUrl, parseScopeRule, parseScopeRules, parseScopeTarget, type ParsedScopeRule, type ScopeDecision, type ScopeTarget } from "../scope/scope-rules";
import type { BrowserManagerOptions, BrowserPageInfo, BrowserScope, PageSnapshot } from "../types";
import { getScreenshotPath } from "@/server/pi/evidence-path";

const EVALUATION_OUTPUT_LIMIT = 8000;
const IDENTITY_PATTERN = /^[a-z0-9_-]{1,32}$/;

type IdentityState = {
  userAgent?: string;
  extraHeaders?: Record<string, string>;
  activePageId?: string;
};

function parseEnvironmentRules() {
  return (process.env.RIFTX_BROWSER_ALLOWED_ORIGINS ?? "").split(",").map((item) => item.trim()).filter(Boolean);
}

function serializeEvaluation(value: unknown) {
  let text: string;
  try {
    text = JSON.stringify(value, (_key, item) => (typeof item === "bigint" ? item.toString() : item), 2) ?? String(value);
  } catch {
    text = String(value);
  }
  return text.length > EVALUATION_OUTPUT_LIMIT ? `${text.slice(0, EVALUATION_OUTPUT_LIMIT)}\n[truncated]` : text;
}

function hostRule(host: string): ParsedScopeRule {
  return { raw: host, host, wildcard: false };
}

/** Canonical form of a mapping set so authorizations can be bound to exactly one set. */
function canonicalizeMappings(mappings: Record<string, string>) {
  // Normalize before sorting: sorting raw keys makes {"B":1,"a":2} and
  // {"a":1,"b":2} produce different fingerprints for the same mapping set.
  return JSON.stringify(Object.keys(mappings)
    .map((key) => [key.trim().toLowerCase(), String(mappings[key]).trim().toLowerCase()])
    .sort((left, right) => left[0].localeCompare(right[0])));
}

/** Parse a host-mapping value like `10.0.0.5`, `10.0.0.5:8443`, or `[::1]:8443`. */
function parseMappingTarget(value: string): { host: string; port?: number } | undefined {
  const raw = value.trim().toLowerCase();
  if (!raw || !/^[[\]a-z0-9.:_-]+$/.test(raw)) return undefined;
  const bracketed = raw.match(/^\[(.+)\](?::(\d+))?$/);
  if (bracketed) {
    const port = bracketed[2] ? Number(bracketed[2]) : undefined;
    if (port !== undefined && (port < 1 || port > 65535)) return undefined;
    return { host: bracketed[1], port };
  }
  if ((raw.match(/:/g) ?? []).length === 1) {
    const [host, portText] = raw.split(":");
    if (/^\d+$/.test(portText)) {
      const port = Number(portText);
      if (port >= 1 && port <= 65535) return { host, port };
    }
  }
  return { host: raw };
}

type ParsedHostMapping = {
  rawHost: string;
  rawTarget: string;
  host: string;
  target: string;
  parsed: HostMappingTarget;
};

function parseHostMappingEntries(mappings: Record<string, string>): ParsedHostMapping[] {
  return Object.entries(mappings).map(([rawHost, rawTarget]) => {
    const target = typeof rawTarget === "string" ? rawTarget.trim().toLowerCase() : "";
    const parsed = parseMappingTarget(target);
    const logicalRule = parseScopeRule(rawHost);
    const targetRule = parsed ? parseScopeRule(parsed.host) : undefined;
    if (!logicalRule || logicalRule.scheme || logicalRule.wildcard || logicalRule.cidr || logicalRule.port !== undefined
      || !target || !parsed || !targetRule || targetRule.scheme || targetRule.wildcard || targetRule.cidr || targetRule.port !== undefined) {
      throw new Error(`Invalid host mapping "${rawHost}" -> "${String(rawTarget)}": source must be a host and target must be a host or IP with an optional :port`);
    }
    return { rawHost, rawTarget, host: logicalRule.host, target, parsed };
  });
}

export class BrowserManager {
  private operationChain: Promise<unknown> = Promise.resolve();
  private closed = false;

  /**
   * Serialize every browser operation on this manager instance. Playwright
   * pages must not be driven concurrently and the manager's own state
   * (pages, identities, network log) is mutable, so read-only and mutating
   * actions alike run through one chain. This is the guarantee that lets the
   * browser tool execute in the parallel lane without read/write
   * interleaving; instances are per-session, so chains never cross sessions.
   * A queued operation re-checks its AbortSignal and the closed flag when
   * dequeued: an aborted caller or a closed manager must never let a stale
   * operation start, or it would relaunch browser resources and perform side
   * effects the user already stopped.
   */
  run<T>(operation: () => Promise<T>, signal?: AbortSignal): Promise<T> {
    const start = async () => {
      if (this.closed) throw new Error("Browser session is closed");
      signal?.throwIfAborted();
      return operation();
    };
    const next = this.operationChain.then(start, start);
    this.operationChain = next.then(() => undefined, () => undefined);
    return next;
  }

  private readonly contextManager = new ContextManager();
  private readonly requests = new RequestStore();
  private readonly pages = new Map<string, PageManager>();
  private readonly refs = new Map<string, ElementRefMapper>();
  private readonly identities = new Map<string, IdentityState>();
  private readonly attachedContexts = new WeakSet<BrowserContext>();
  private readonly cdpSessions = new Map<Page, CDPSession>();
  private readonly overriddenPages = new WeakSet<Page>();
  private readonly hostMappings = new Map<string, string>();
  private readonly mappingProxy = new HostMappingProxy();
  private activeIdentity = "default";
  private readonly configuredRules: ParsedScopeRule[];
  private readonly sessionRules = new Set<ParsedScopeRule>();
  private lockedHost?: string;
  /** Precise, transient authorizations from allow-once decisions, scoped per identity ("*" = global). */
  private tempAuthorizations: Array<{ identity: string; rules: ParsedScopeRule[]; kind: "navigation" | "mapping"; source?: string }> = [];
  private readonly parsedMappings = new Map<string, HostMappingTarget>();
  /** Live WebSockets routed through the current mapping set, with the fingerprint they were authorized under. */
  private readonly routedWebSockets = new Set<{ route: WebSocketRoute; serverRoute?: WebSocketRoute; source: string }>();
  private currentMappingSource = canonicalizeMappings({});
  private readonly screenshotUrls = new Map<string, string>();
  private readonly rawScopeRuleCount: number;
  private readonly evidenceRoot?: string;
  private readonly evidenceSessionId?: string;
  private readonly ignoreTlsErrors: boolean;
  private latestScreenshotId?: string;

  constructor(options: BrowserManagerOptions) {
    this.evidenceRoot = options.evidenceRoot;
    this.evidenceSessionId = options.evidenceSessionId;
    this.ignoreTlsErrors = options.ignoreTlsErrors ?? true;
    const rawRules = [...(options.scope?.rules ?? []), ...parseEnvironmentRules()].map((rule) => rule.trim()).filter(Boolean);
    this.rawScopeRuleCount = rawRules.length;
    this.configuredRules = parseScopeRules(rawRules);
  }

  /** Check a navigation URL against config rules, session grants, and the first-host lock. */
  checkNavigationScope(rawUrl: string): ScopeDecision {
    const target = parseScopeTarget(rawUrl);
    if (!target) return { allowed: false, host: "", port: 0, suggestedRule: "", reason: "Browser navigation requires an absolute http(s) URL" };
    // Fail closed when rules were configured but none of them parsed: silently
    // treating that as "no scope configured" would allow any first host.
    if (this.rawScopeRuleCount > 0 && this.configuredRules.length === 0 && this.sessionRules.size === 0) {
      return { allowed: false, host: target.host, port: target.port, suggestedRule: target.host, reason: "all configured browser scope rules are invalid; fix browserScope in settings" };
    }
    const rules = [...this.configuredRules, ...this.sessionRules];
    if (rules.length) {
      return matchScopeUrl(rules, target)
        ? { allowed: true, host: target.host, port: target.port, suggestedRule: target.host }
        : { allowed: false, host: target.host, port: target.port, suggestedRule: target.host, reason: "no authorized scope rule matches this host" };
    }
    if (this.lockedHost) {
      return hostMatches(hostRule(this.lockedHost), target.host)
        ? { allowed: true, host: target.host, port: target.port, suggestedRule: target.host }
        : { allowed: false, host: target.host, port: target.port, suggestedRule: target.host, reason: `scope is locked to ${this.lockedHost}` };
    }
    return { allowed: true, host: target.host, port: target.port, suggestedRule: target.host };
  }

  /** Permanently add the URL's host, or exact host+port, to this session's authorized scope. */
  grantScope(rawUrl: string, exactPort = false) {
    const target = parseScopeTarget(rawUrl);
    if (!target) return;
    // The first grant transitions from the implicit first-host lock to explicit
    // rules; seed the locked host so it stays authorized alongside the grant.
    if (!this.configuredRules.length && !this.sessionRules.size && this.lockedHost) this.sessionRules.add(hostRule(this.lockedHost));
    this.sessionRules.add(exactPort ? { ...hostRule(target.host), port: target.port } : hostRule(target.host));
  }

  /** Allow one exact navigation after a user approved it without granting the host. */
  authorizeOnce(rawUrl: string, identityId?: string) {
    const target = parseScopeTarget(rawUrl);
    if (!target) return;
    // The approved page may load its own subresources — but only from the
    // exact approved scheme, host, and port, never the whole host.
    this.addTempAuthorization([{ raw: target.url, scheme: target.scheme, host: target.host, wildcard: false, port: target.port }], this.resolveIdentity(identityId), "navigation");
  }

  /** Roll back a temporary navigation authorization when its approved tool call fails. */
  revokeOnce(rawUrl: string, identityId?: string) {
    const target = parseScopeTarget(rawUrl);
    if (!target) return;
    const identity = this.resolveIdentity(identityId);
    this.tempAuthorizations = this.tempAuthorizations.filter((entry) => !(entry.kind === "navigation"
      && entry.identity === identity
      && entry.rules.some((rule) => rule.scheme === target.scheme && rule.host === target.host && rule.port === target.port)));
  }

  /**
   * Temporarily authorize host-mapping targets after an explicit approval.
   * Both the logical mapping host and the physical destination are covered, so
   * an approved mapping works from a completely empty scope. Port-less targets
   * authorize the host (the physical port then follows the request's logical
   * port, which the per-request physical check re-verifies).
   */
  authorizeMappingTargetsOnce(mappings: Record<string, string>) {
    const rules: ParsedScopeRule[] = [];
    for (const entry of parseHostMappingEntries(mappings)) {
      rules.push({ raw: entry.host, host: entry.host, wildcard: false });
      rules.push({ raw: entry.rawTarget, host: entry.parsed.host, wildcard: false, ...(entry.parsed.port !== undefined ? { port: entry.parsed.port } : {}) });
    }
    // Mappings are global browser state, so their authorization applies to every identity.
    // The authorization is bound to this exact mapping set: replacing or
    // clearing the mappings invalidates it.
    this.addTempAuthorization(rules, "*", "mapping", canonicalizeMappings(mappings));
  }

  private addTempAuthorization(rules: ParsedScopeRule[], identity: string, kind: "navigation" | "mapping", source?: string) {
    if (!rules.length) return;
    this.tempAuthorizations.push({ identity, rules, kind, ...(source !== undefined ? { source } : {}) });
  }

  private tempRulesFor(identity: string) {
    return this.tempAuthorizations.filter((entry) => entry.identity === "*" || entry.identity === identity).flatMap((entry) => entry.rules);
  }

  private expireNavigationAuthorizations(identity: string, destination: ScopeTarget) {
    this.tempAuthorizations = this.tempAuthorizations.filter((entry) => entry.identity !== identity
      || entry.kind !== "navigation"
      || entry.rules.some((rule) => matchScopeUrl([rule], destination)));
  }

  private hasScopeRules() {
    return this.configuredRules.length > 0 || this.sessionRules.size > 0;
  }

  /**
   * Mapping targets are physical destinations: return every target whose
   * concrete origin (scheme x host x port) is not already authorized.
   */
  checkHostMappingScope(mappings: Record<string, string>): Array<{ host: string; target: string }> {
    const violating: Array<{ host: string; target: string }> = [];
    // Before any baseline exists (no rules, no first-host lock, no mapping
    // authorization), "everything is allowed" would silently authorize any
    // physical target — but the first navigation then locks the logical host
    // and the physical target becomes unreachable with no approval path.
    // Require approval in that state instead.
    const hasBaseline = this.hasScopeRules() || this.lockedHost !== undefined || this.tempAuthorizations.some((entry) => entry.kind === "mapping");
    // Only authorizations bound to this exact mapping set count: a stale set's
    // rules must not pre-approve a different mapping (they are about to be
    // invalidated by its fingerprint anyway).
    const candidateTemp = this.tempAuthorizations
      .filter((entry) => entry.kind === "mapping" && entry.source === canonicalizeMappings(mappings))
      .flatMap((entry) => entry.rules);
    const probe = (url: string) => {
      const target = parseScopeTarget(url);
      if (!target) return false;
      const rules = [...this.configuredRules, ...this.sessionRules, ...candidateTemp];
      if (rules.length) return matchScopeUrl(rules, target);
      return this.lockedHost !== undefined && hostMatches(hostRule(this.lockedHost), target.host);
    };
    for (const entry of parseHostMappingEntries(mappings)) {
      // Probe both schemes on the concrete port: a mapping keeps the request's
      // scheme, so partially scheme-restricted targets must go through
      // approval rather than dead-end later. Port-less targets probe a dummy
      // port so only port-agnostic rules can pre-authorize them.
      const port = entry.parsed.port ?? 1;
      const hostText = (entry.parsed.host.match(/:/g) ?? []).length > 1 ? `[${entry.parsed.host}]` : entry.parsed.host;
      const allowed = hasBaseline && ["http", "https"].every((scheme) => probe(`${scheme}://${hostText}:${port}/`));
      if (!allowed) violating.push({ host: entry.rawHost, target: entry.rawTarget });
    }
    return violating;
  }

  /**
   * The physical destination the proxy will connect to for this request:
   * the mapping target's host, with the mapping port or the request's port.
   */
  private physicalTarget(target: { scheme: "http" | "https"; host: string; port: number }): { scheme: "http" | "https"; host: string; port: number } {
    const mapped = this.parsedMappings.get(target.host);
    if (!mapped) return target;
    return { scheme: target.scheme, host: mapped.host, port: mapped.port ?? target.port };
  }

  /** Every request (documents and subresources) is scope-checked; page content is untrusted. WebSocket URLs count as their http(s) counterparts. */
  isUrlAuthorized(rawUrl: string, identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const normalized = rawUrl.replace(/^ws:/i, "http:").replace(/^wss:/i, "https:");
    const target = parseScopeTarget(normalized);
    if (!target) return false;
    const tempRules = this.tempRulesFor(identity);
    const logicalOk = this.checkNavigationScope(normalized).allowed || (tempRules.length > 0 && matchScopeUrl(tempRules, target));
    if (!logicalOk) return false;
    // A mapped request must also be authorized for its physical destination.
    const physical = this.physicalTarget(target);
    if (physical === target) return true;
    const physicalUrl = `${physical.scheme}://${(physical.host.match(/:/g) ?? []).length > 1 ? `[${physical.host}]` : physical.host}:${physical.port}/`;
    const physicalTarget = parseScopeTarget(physicalUrl);
    return physicalTarget !== undefined && (this.checkNavigationScope(physicalUrl).allowed || (tempRules.length > 0 && matchScopeUrl(tempRules, physicalTarget)));
  }

  /**
   * Host mappings are applied by the loopback proxy (curl --resolve semantics:
   * connect to the mapped address, keep the Host header and TLS SNI), because
   * Playwright's route URL rewriting recomputes the Host header.
   */
  private async ensureContext(identity: string) {
    await this.mappingProxy.start();
    const context = await this.contextManager.getContext(identity, { ignoreHTTPSErrors: this.ignoreTlsErrors, proxyUrl: this.mappingProxy.proxyUrl });
    if (!this.attachedContexts.has(context)) {
      this.attachedContexts.add(context);
      // Context-level interception is installed before any page — including
      // window.open popups — can issue its first request.
      await context.route("**/*", (route) => { void this.handleRoute(route, identity); });
      // WebSockets bypass the HTTP route; intercept them separately so they
      // cannot exfiltrate to unauthorized hosts either.
      await context.routeWebSocket("**/*", (wsRoute) => {
        if (!this.isUrlAuthorized(wsRoute.url(), identity)) {
          // close() returns a rejected Promise on races; consume it so the
          // process never sees an unhandled rejection.
          try { void wsRoute.close().catch(() => undefined); } catch { /* sync guard */ }
          return;
        }
        let serverRoute: WebSocketRoute | undefined;
        try {
          serverRoute = wsRoute.connectToServer();
        } catch { /* server unreachable; frames die */ }
        // Track connections routed through the mapping set so replacing or
        // clearing the mappings tears them down with their authorization. Both
        // ends are kept: an explicit close of the page side does not reliably
        // fire its own onClose (which is what forwards to the server side).
        const target = parseScopeTarget(wsRoute.url().replace(/^ws:/i, "http:").replace(/^wss:/i, "https:"));
        const routed = target !== undefined && this.parsedMappings.has(target.host);
        const entry: { route: WebSocketRoute; serverRoute?: WebSocketRoute; source: string } = { route: wsRoute, source: this.currentMappingSource };
        if (serverRoute) entry.serverRoute = serverRoute;
        if (routed) this.routedWebSockets.add(entry);
        // Registering onClose disables Playwright's default close forwarding,
        // so forward closures both ways manually — and drop the tracking entry
        // as soon as either side ends, so naturally closed sockets do not leak.
        wsRoute.onClose((code, reason) => {
          this.routedWebSockets.delete(entry);
          if (serverRoute) try { void serverRoute.close({ code, reason }).catch(() => undefined); } catch { /* already closed */ }
        });
        serverRoute?.onClose((code, reason) => {
          this.routedWebSockets.delete(entry);
          try { void wsRoute.close({ code, reason }).catch(() => undefined); } catch { /* already closed */ }
        });
      });
      context.on("page", (page) => { this.registerPage(page, identity); });
    }
    return context;
  }

  private async handleRoute(route: Route, identity: string) {
    try {
      const request = route.request();
      // Every request is scope-checked (documents and subresources): page
      // content is untrusted, so an authorized page must not fetch or
      // exfiltrate to unauthorized hosts. CDN-style dependencies require
      // explicit scope rules. Host mappings are applied downstream by the
      // loopback proxy.
      if (!this.isUrlAuthorized(request.url(), identity)) {
        await route.abort("blockedbyclient");
        return;
      }
      // Extra headers are merged per request: Chromium drops CDP-added
      // headers on proxied requests, so the route layer is the reliable spot.
      const extra = this.identities.get(identity)?.extraHeaders;
      if (extra && Object.keys(extra).length) await route.continue({ headers: { ...request.headers(), ...extra } });
      else await route.continue();
    } catch {
      // A route may already be handled or its target may have closed. Never
      // let cleanup failure escape the fire-and-forget Playwright callback.
      try { await route.abort("blockedbyclient"); } catch { /* already handled */ }
    }
  }

  private resolveIdentity(identityId?: string) {
    const identity = identityId?.trim().toLowerCase();
    if (!identity) return this.activeIdentity;
    if (!IDENTITY_PATTERN.test(identity)) throw new Error(`Invalid browser identity "${identityId}": use 1-32 letters, digits, hyphens, or underscores.`);
    return identity;
  }

  private ensureIdentityState(identity: string) {
    let state = this.identities.get(identity);
    if (!state) {
      state = {};
      this.identities.set(identity, state);
    }
    return state;
  }

  private registerPage(page: Page, identity: string) {
    const existing = [...this.pages.values()].find((item) => item.page === page);
    if (existing) return existing;
    const manager = new PageManager(page, identity, this.requests);
    this.pages.set(manager.id, manager);
    this.refs.set(manager.id, new ElementRefMapper());
    // A committed main-frame navigation is the single point that advances an
    // identity's once window. Blocked or failed requests leave the old page's
    // authorization intact; click, popup, back, and explicit goto share this path.
    page.on("framenavigated", (frame) => {
      if (frame.parentFrame() !== null) return;
      const target = parseScopeTarget(frame.url());
      if (target) this.expireNavigationAuthorizations(identity, target);
    });
    page.on("close", () => {
      this.pages.delete(manager.id);
      this.refs.delete(manager.id);
      this.cdpSessions.delete(page);
      this.overriddenPages.delete(page);
      for (const state of this.identities.values()) {
        if (state.activePageId === manager.id) state.activePageId = [...this.pages.values()].find((item) => item.identity === identity)?.id;
      }
    });
    void this.applyOverrides(page, identity);
    return manager;
  }

  /** Apply the identity's User-Agent override to one page via CDP (best effort). */
  private async applyOverrides(page: Page, identity: string) {
    const state = this.identities.get(identity) ?? {};
    if (state.userAgent === undefined && !this.overriddenPages.has(page)) return;
    this.overriddenPages.add(page);
    try {
      let session = this.cdpSessions.get(page);
      if (!session) {
        session = await page.context().newCDPSession(page);
        this.cdpSessions.set(page, session);
      }
      const userAgent = state.userAgent ?? await this.contextManager.defaultUserAgent();
      await session.send("Emulation.setUserAgentOverride", { userAgent });
    } catch {
      // Overrides are best-effort; pages still work without them.
    }
  }

  private async applyOverridesToIdentity(identity: string) {
    await Promise.all([...this.pages.values()].filter((item) => item.identity === identity).map((item) => this.applyOverrides(item.page, identity)));
  }

  private async ensurePage(identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const state = this.ensureIdentityState(identity);
    const context = await this.ensureContext(identity);
    if (!state.activePageId || !this.pages.has(state.activePageId)) {
      const page = await context.newPage();
      const manager = this.registerPage(page, identity);
      // Overrides must be in place before the first navigation is issued.
      await this.applyOverrides(page, identity);
      state.activePageId = manager.id;
    }
    return this.pages.get(state.activePageId)!;
  }

  private pageManager(identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const state = this.identities.get(identity);
    const manager = this.pages.get(state?.activePageId ?? "");
    if (!manager) throw new Error(`No browser page is open for identity "${identity}". Run browser navigate first.`);
    return manager;
  }

  async navigate(url: string, identityId?: string) {
    const target = parseScopeTarget(url);
    if (!target) throw new Error("Browser navigation requires an absolute http(s) URL");
    const check = this.checkNavigationScope(url);
    // A temporary navigation or mapping authorization covers the URL exactly
    // as the route layer will judge it; there is no second global exemption.
    if (!check.allowed && !this.isUrlAuthorized(url, identityId)) {
      throw new Error(`Navigation to ${target.host} is outside the authorized browser scope (${check.reason}). Scope authorization is required before this navigation can run.`);
    }
    if (!this.hasScopeRules() && !this.lockedHost) this.lockedHost = target.host;
    const manager = await this.ensurePage(identityId);
    await manager.page.goto(target.url, { waitUntil: "domcontentloaded", timeout: 30_000 });
    return this.snapshot(identityId);
  }

  async snapshot(identityId?: string): Promise<PageSnapshot> {
    const manager = await this.ensurePage(identityId);
    return createSnapshot(manager.page, this.refs.get(manager.id)!);
  }

  private locator(ref: string, identityId?: string) {
    const manager = this.pageManager(identityId);
    const element = this.refs.get(manager.id)?.get(ref);
    if (!element) throw new Error(`Unknown or stale element ref ${ref}; call browser snapshot again`);
    return manager.page.locator(element.selector).first();
  }

  async click(ref: string, identityId?: string) { await this.locator(ref, identityId).click(); return this.snapshot(identityId); }
  async fill(ref: string, value: string, identityId?: string) { await this.locator(ref, identityId).fill(value); return this.snapshot(identityId); }
  async press(ref: string, key: string, identityId?: string) { await this.locator(ref, identityId).press(key); return this.snapshot(identityId); }
  async select(ref: string, values: string[], identityId?: string) { await this.locator(ref, identityId).selectOption(values); return this.snapshot(identityId); }
  async back(identityId?: string) { await this.pageManager(identityId).page.goBack({ waitUntil: "domcontentloaded" }).catch(() => undefined); return this.snapshot(identityId); }
  async reload(identityId?: string) { await this.pageManager(identityId).page.reload({ waitUntil: "domcontentloaded" }); return this.snapshot(identityId); }

  /** Evaluate a JavaScript expression in the identity's active page and return a serialized result. */
  async evaluate(expression: string, identityId?: string) {
    const manager = this.pageManager(identityId);
    return serializeEvaluation(await manager.page.evaluate(expression));
  }

  consoleLog(identityId?: string, limit = 50) {
    const manager = this.pages.get(this.identities.get(this.resolveIdentity(identityId))?.activePageId ?? "");
    if (!manager) return "(no browser page is open)";
    const lines = manager.consoleLog(limit);
    return lines.length ? lines.join("\n") : "(no console output captured)";
  }

  /** Recent error-level console output since a timestamp; empty when there is none. */
  recentConsoleErrors(identityId: string | undefined, sinceMs: number, limit = 5) {
    const manager = this.pages.get(this.identities.get(this.resolveIdentity(identityId))?.activePageId ?? "");
    if (!manager) return "";
    return manager.consoleLog(limit, new Set(["error", "pageerror", "dialog"]), sinceMs).join("\n");
  }

  useIdentity(identityId: string) {
    const identity = this.resolveIdentity(identityId);
    this.ensureIdentityState(identity);
    this.activeIdentity = identity;
    return this.identitiesOverview();
  }

  identitiesOverview() {
    const identities = [...this.identities.keys()];
    if (!identities.length) identities.push("default");
    const lines = identities.map((identity) => {
      const state = this.identities.get(identity) ?? {};
      const pageCount = [...this.pages.values()].filter((item) => item.identity === identity).length;
      const userAgent = state.userAgent ? `UA: ${state.userAgent}` : "UA: (browser default)";
      const headers = state.extraHeaders && Object.keys(state.extraHeaders).length ? `, headers: ${Object.entries(state.extraHeaders).map(([key, value]) => `${key}=${value}`).join(", ")}` : "";
      return `${identity === this.activeIdentity ? "* " : "  "}${identity} — pages: ${pageCount}, ${userAgent}${headers}`;
    });
    if (this.hostMappings.size) {
      lines.push("host mappings:");
      lines.push(...[...this.hostMappings.entries()].map(([host, target]) => `  ${host} -> ${target}`));
    }
    return lines.join("\n");
  }

  async cookies(identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const context = await this.ensureContext(identity);
    return JSON.stringify(await context.cookies(), null, 2);
  }

  /** Export the identity's cookie jar as JSON so scripts (for example curl) can reuse the authenticated state. */
  async cookiesExport(identityId?: string) {
    return this.cookies(identityId);
  }

  /** Import cookies (Playwright cookie JSON) into the identity, bridging curl sessions into the browser. */
  async cookiesImport(serialized: string, identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    let parsed: unknown;
    try {
      parsed = JSON.parse(serialized);
    } catch {
      throw new Error("cookies_import requires a JSON array of cookies");
    }
    if (!Array.isArray(parsed)) throw new Error("cookies_import requires a JSON array of cookies");
    const cookies = parsed.map((item): { name: string; value: string; url?: string; domain?: string; path?: string; expires?: number; httpOnly?: boolean; secure?: boolean; sameSite?: "Strict" | "Lax" | "None" } => {
      if (!item || typeof item !== "object") throw new Error("cookies_import: every entry needs string name and value fields");
      const { name, value } = item as { name?: unknown; value?: unknown };
      if (typeof name !== "string" || typeof value !== "string") throw new Error("cookies_import: every entry needs string name and value fields");
      const raw = item as Record<string, unknown>;
      const cookie: { name: string; value: string; url?: string; domain?: string; path?: string; expires?: number; httpOnly?: boolean; secure?: boolean; sameSite?: "Strict" | "Lax" | "None" } = { name, value };
      for (const field of ["url", "domain", "path"] as const) {
        if (typeof raw[field] === "string") cookie[field] = raw[field];
      }
      if (raw.sameSite === "Strict" || raw.sameSite === "Lax" || raw.sameSite === "None") cookie.sameSite = raw.sameSite;
      if (typeof raw.expires === "number") cookie.expires = raw.expires;
      if (typeof raw.httpOnly === "boolean") cookie.httpOnly = raw.httpOnly;
      if (typeof raw.secure === "boolean") cookie.secure = raw.secure;
      return cookie;
    });
    const context = await this.ensureContext(identity);
    if (cookies.length) await context.addCookies(cookies);
    return `Imported ${cookies.length} cookies into identity "${identity}".`;
  }

  /** Replace all host mappings (curl --resolve semantics); an empty object clears them. */
  setHostMappings(mappings: Record<string, string>) {
    const entries = parseHostMappingEntries(mappings);
    this.hostMappings.clear();
    this.parsedMappings.clear();
    this.mappingProxy.mappings.clear();
    for (const entry of entries) {
      this.hostMappings.set(entry.host, entry.target);
      this.parsedMappings.set(entry.host, entry.parsed);
      this.mappingProxy.mappings.set(entry.host, entry.parsed);
    }
    // Mapping authorizations live exactly as long as the mapping set they
    // approved: replacing or clearing the mappings invalidates them — and the
    // WebSockets already routed through the old set are torn down with them.
    const source = canonicalizeMappings(mappings);
    this.tempAuthorizations = this.tempAuthorizations.filter((entry) => entry.kind !== "mapping" || entry.source === source);
    if (source !== this.currentMappingSource) {
      for (const entry of [...this.routedWebSockets]) {
        if (entry.source !== source) {
          this.routedWebSockets.delete(entry);
          this.closeRoutedWebSocket(entry);
        }
      }
      // Established HTTPS tunnels keep flowing to their old destination until
      // they are forced down; the browser then re-CONNECTs under the new set.
      this.mappingProxy.closeUpstreams();
      this.currentMappingSource = source;
    }
    return entries.length
      ? `Host mappings active:\n${entries.map((entry) => `  ${entry.host} -> ${entry.target}`).join("\n")}`
      : "Host mappings cleared.";
  }

  async setUserAgent(userAgent: string | undefined, identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const state = this.ensureIdentityState(identity);
    if (userAgent?.trim()) state.userAgent = userAgent.trim();
    else delete state.userAgent;
    await this.applyOverridesToIdentity(identity);
    return `User-Agent for identity "${identity}" set to: ${state.userAgent ?? "(browser default)"}`;
  }

  async setExtraHeaders(headers: Record<string, string>, identityId?: string) {
    const identity = this.resolveIdentity(identityId);
    const state = this.ensureIdentityState(identity);
    const cleaned = Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.trim(), String(value)]).filter(([key]) => key));
    if (Object.keys(cleaned).length) state.extraHeaders = cleaned;
    else delete state.extraHeaders;
    await this.applyOverridesToIdentity(identity);
    return Object.keys(cleaned).length
      ? `Extra headers for identity "${identity}": ${Object.entries(cleaned).map(([key, value]) => `${key}: ${value}`).join(", ")}`
      : `Extra headers cleared for identity "${identity}".`;
  }

  async requestsList() {
    return this.requests.list().map((item) => `${item.ref} [${item.identity}] ${item.method.padEnd(6)} ${item.url} ${item.status ?? "pending"}`).join("\n") || "(no requests recorded)";
  }

  requestDetail(ref: string) {
    const item = this.requests.get(ref);
    if (!item) throw new Error(`Unknown request ref ${ref}`);
    const requestHeaders = Object.entries(redactHeaders(item.requestHeaders)).map(([key, value]) => `${key}: ${value}`).join("\n");
    const responseHeaders = Object.entries(item.responseHeaders ?? {}).map(([key, value]) => `${key}: ${value}`).join("\n");
    return [`# identity: ${item.identity}`, `${item.method} ${item.url} HTTP/1.1`, requestHeaders ? `\n${requestHeaders}` : "", item.requestBody ? `\n\n${item.requestBody}` : "", `\n\nResponse:\n${item.status ?? "pending"} ${item.statusText ?? ""}`, responseHeaders ? `\n${responseHeaders}` : ""].join("");
  }

  requestEvidence(ref: string) {
    const item = this.requests.get(ref);
    if (!item) throw new Error(`Unknown request ref ${ref}`);
    return {
      type: "request" as const,
      requestRef: item.ref,
      method: item.method,
      url: item.url,
      status: item.status
    };
  }

  responseBody(ref: string) {
    const item = this.requests.get(ref);
    if (!item) throw new Error(`Unknown request ref ${ref}`);
    return item.responseBody ?? "(response body unavailable or still pending)";
  }

  async storage(identityId?: string) {
    const page = this.pageManager(identityId).page;
    const storage = await page.evaluate(() => {
      try {
        return { localStorage: Object.fromEntries(Object.entries(localStorage)), sessionStorage: Object.fromEntries(Object.entries(sessionStorage)) };
      } catch {
        return { localStorage: {}, sessionStorage: {} };
      }
    });
    return JSON.stringify(storage, null, 2);
  }

  async captureScreenshot(identityId?: string) {
    const screenshotId = `s-${randomUUID()}`;
    const page = this.pageManager(identityId).page;
    const base64 = (await page.screenshot({ type: "png" })).toString("base64");
    this.latestScreenshotId = screenshotId;
    // Remember the capture-time URL so evidence lookup never attributes the
    // screenshot to whichever identity happens to be active later.
    this.screenshotUrls.set(screenshotId, page.url());
    if (this.evidenceRoot && this.evidenceSessionId) {
      const directory = join(this.evidenceRoot, this.evidenceSessionId, "shots");
      await mkdir(directory, { recursive: true, mode: 0o700 });
      await writeFile(getScreenshotPath(this.evidenceRoot, this.evidenceSessionId, screenshotId), Buffer.from(base64, "base64"), { mode: 0o600 });
      // Sidecar metadata so evidence lookups survive a runtime rebuild.
      await writeFile(join(directory, `${screenshotId}.url`), `${page.url()}\n`, { mode: 0o600 });
    }
    // Attribute the screenshot to the captured identity's page, not the active one.
    return { screenshotId, base64, url: page.url() };
  }

  async screenshotEvidence(screenshotId: string) {
    const resolvedId = screenshotId === "latest" ? this.latestScreenshotId : screenshotId;
    if (!resolvedId) throw new Error("No browser screenshot is available");
    if (this.evidenceRoot && this.evidenceSessionId) {
      const path = getScreenshotPath(this.evidenceRoot, this.evidenceSessionId, resolvedId);
      await stat(path).catch(() => { throw new Error(`Unknown screenshot id ${resolvedId}`); });
    } else if (resolvedId !== this.latestScreenshotId) {
      throw new Error(`Unknown screenshot id ${resolvedId}`);
    }
    // Prefer the in-memory capture-time URL, then the persisted sidecar; an
    // empty URL is honest for pre-upgrade screenshots instead of guessing the
    // currently active page.
    let url = this.screenshotUrls.get(resolvedId);
    if (!url && this.evidenceRoot && this.evidenceSessionId) {
      const sidecar = await readFile(join(this.evidenceRoot, this.evidenceSessionId, "shots", `${resolvedId}.url`), "utf8").catch(() => undefined);
      url = sidecar?.trim() || undefined;
    }
    return { type: "screenshot" as const, screenshotId: resolvedId, url: url ?? "" };
  }

  /** Tabs grouped by identity; the active identity and its active tab are marked. */
  async tabs(): Promise<string> {
    const infos: BrowserPageInfo[] = await Promise.all([...this.pages.entries()].map(async ([id, manager]) => manager.info(id === this.identities.get(manager.identity)?.activePageId)));
    const identities = [...new Set([...this.identities.keys(), ...infos.map((info) => info.identity), this.activeIdentity])];
    const lines: string[] = [];
    for (const identity of identities) {
      const identityTabs = infos.filter((info) => info.identity === identity);
      lines.push(`${identity === this.activeIdentity ? "*" : " "} identity: ${identity}${identityTabs.length ? "" : " (no pages yet)"}`);
      for (const info of identityTabs) lines.push(`    [${info.id.slice(0, 8)}${info.active ? ", active" : ""}] ${info.url} — ${info.title || "(untitled)"}`);
    }
    return lines.join("\n");
  }

  /** Close both ends of a routed WebSocket explicitly. */
  private closeRoutedWebSocket(entry: { route: WebSocketRoute; serverRoute?: WebSocketRoute }) {
    try { void entry.route.close().catch(() => undefined); } catch { /* already closed */ }
    if (entry.serverRoute) {
      try { void entry.serverRoute.close().catch(() => undefined); } catch { /* already closed */ }
    }
  }

  /**
   * Release the browser and all tracked state, but stay usable: the
   * model-facing browser "close" action expects a later navigate to lazily
   * relaunch everything. Queued operations still run afterwards.
   */
  close() {
    return this.teardown();
  }

  /**
   * Permanent teardown for session shutdown/abort. The closed flag barriers
   * the operation chain: every queued operation rejects at its dequeue check
   * instead of starting against torn-down (or re-launched) resources. The
   * in-flight operation fails naturally against the teardown rather than
   * delaying shutdown.
   */
  async shutdown() {
    this.closed = true;
    await this.teardown();
  }

  private async teardown() {
    // Tear down routed WebSockets first: closing the browser while a routed
    // WebSocket is still live can hang the Playwright context shutdown.
    for (const entry of [...this.routedWebSockets]) this.closeRoutedWebSocket(entry);
    this.routedWebSockets.clear();
    this.requests.clear();
    this.refs.clear();
    this.pages.clear();
    this.identities.clear();
    this.cdpSessions.clear();
    this.hostMappings.clear();
    this.parsedMappings.clear();
    this.currentMappingSource = canonicalizeMappings({});
    this.activeIdentity = "default";
    this.lockedHost = undefined;
    this.sessionRules.clear();
    this.tempAuthorizations = [];
    this.screenshotUrls.clear();
    this.latestScreenshotId = undefined;
    // Close the browser before the proxy: killing the proxy first makes the
    // still-live browser reconnect, leaving TCP handles behind.
    await this.contextManager.close();
    this.mappingProxy.close();
  }

  get currentUrl() {
    const manager = this.pages.get(this.identities.get(this.activeIdentity)?.activePageId ?? "");
    return manager?.page.url() ?? "about:blank";
  }
}
