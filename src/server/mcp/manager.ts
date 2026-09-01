import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import type { McpServerConfig } from "@/lib/types";
import { RIFTX_VERSION } from "@/lib/version";
import { createSerializer } from "@/server/serializer";

/**
 * Process-wide MCP connection manager with reference counting. Sessions
 * acquire entries at creation and release them at shutdown; a connection is
 * shared while any session holds it (stable cache key = whole server config)
 * and closed only when the last reference drops. Reconciling against a changed
 * config never closes connections that live sessions still hold. A dead server
 * never fails session creation — it shows up as an error entry with zero tools
 * and is retried on the next acquire.
 */

export const CONNECT_TIMEOUT_MS = 10_000;

export type McpListedTool = { name: string; description?: string; inputSchema: unknown };

export type McpServerHandle = {
  tools: McpListedTool[];
  call(rawName: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<unknown>;
  close(): Promise<void>;
  /** Set when the underlying connection died (server exited, transport closed). The next acquire() replaces the entry instead of reusing the corpse. */
  dead?: boolean;
};

export type McpServerEntry =
  | { state: "connected"; key: string; config: McpServerConfig; handle: McpServerHandle; refs: number }
  | { state: "error"; key: string; config: McpServerConfig; error: string; refs: number };

/** Test seam: the only SDK dependency of the manager logic. Production impl below. */
export type ConnectFactory = (config: McpServerConfig, timeoutMs: number) => Promise<McpServerHandle>;

/** Stable identity of a server config: sorted env/headers keys, so any edit means a new connection. */
export function serverKey(config: McpServerConfig): string {
  const sorted = (value?: Record<string, string>) => JSON.stringify(Object.keys(value ?? {}).sort().map((key) => [key, value![key]]));
  return JSON.stringify({
    name: config.name,
    transport: config.transport,
    command: config.command,
    args: config.args ?? [],
    env: sorted(config.env),
    url: config.url,
    headers: sorted(config.headers)
  });
}

export class McpManager {
  private readonly serialize = createSerializer();
  private readonly entries = new Map<string, McpServerEntry>();
  private readonly timeoutMs: number;

  constructor(deps: { connect: ConnectFactory; timeoutMs?: number }) {
    this.deps = deps;
    this.timeoutMs = deps.timeoutMs ?? CONNECT_TIMEOUT_MS;
  }

  private readonly deps: { connect: ConnectFactory };

  /** Bring the connection set in line with `desired`. Runs entirely under the serializer. */
  reconcile(desired: McpServerConfig[]): Promise<McpServerEntry[]> {
    return this.serialize(() => this.reconcileLocked(desired));
  }

  /** The serializer is a non-reentrant tail chain: acquire must not call the locking wrapper. */
  private async reconcileLocked(desired: McpServerConfig[]): Promise<McpServerEntry[]> {
    const wanted = new Map(desired.map((config) => [serverKey(config), config] as const));
    // Drop connections that are no longer wanted — unless a live session
    // still holds a reference; those keep serving until their last release.
    for (const [key, entry] of this.entries) {
      if (wanted.has(key) || entry.refs > 0) continue;
      if (entry.state === "connected") await entry.handle.close().catch(() => undefined);
      this.entries.delete(key);
    }
    // Connect missing / retry errored / replace dead connections in parallel,
    // bounded by the connect timeout. A connected entry whose server died
    // stays "connected" forever without this check — every future session
    // would reuse the corpse until restart or a config change.
    const pending = [...wanted].filter(([key]) => {
      const existing = this.entries.get(key);
      if (!existing || existing.state === "error") return true;
      if (existing.state === "connected" && existing.handle.dead) {
        // Holders keep the stale object (their calls fail with its errors);
        // the map moves to a fresh connection for the next acquire.
        this.entries.delete(key);
        return true;
      }
      return false;
    });
    await Promise.all(pending.map(async ([key, config]) => {
      try {
        const handle = await this.raceTimeout(this.deps.connect(config, this.timeoutMs), this.timeoutMs);
        this.entries.set(key, { state: "connected", key, config, handle, refs: 0 });
      } catch (error) {
        console.warn(`RiftX MCP server "${config.name}" unavailable (will retry on the next new session):`, error instanceof Error ? error.message : error);
        this.entries.set(key, { state: "error", key, config, error: error instanceof Error ? error.message : String(error), refs: 0 });
      }
    }));
    return desired.map((config) => this.entries.get(serverKey(config))).filter((entry): entry is McpServerEntry => Boolean(entry));
  }

  /** Reconcile + take a reference on each returned entry. The caller must eventually release() them. */
  acquire(desired: McpServerConfig[]): Promise<McpServerEntry[]> {
    return this.serialize(async () => {
      const entries = await this.reconcileLocked(desired);
      for (const entry of entries) entry.refs += 1;
      return entries;
    });
  }

  /** Drop references taken by acquire(). The last release closes the connection. */
  release(entries: readonly McpServerEntry[]): Promise<void> {
    return this.serialize(async () => {
      for (const entry of entries) {
        entry.refs -= 1;
        if (entry.refs > 0) continue;
        // Identity check: a replaced entry (retried error → new connection)
        // must not close the successor that another session now holds.
        if (this.entries.get(entry.key) !== entry) continue;
        if (entry.state === "connected") await entry.handle.close().catch(() => undefined);
        this.entries.delete(entry.key);
      }
    });
  }

  /** Belt-and-braces: a misbehaving connect factory can never hang session creation. */
  private raceTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`connect timeout after ${timeoutMs}ms`)), timeoutMs);
      promise.then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error instanceof Error ? error : new Error(String(error))); });
    });
  }
}

type McpGlobal = typeof globalThis & { __riftxMcpManager?: McpManager };
const mcpGlobal = globalThis as McpGlobal;

/** process.env carries `string | undefined` values; the stdio transport wants a clean string map. */
function cleanEnv(env: NodeJS.ProcessEnv): Record<string, string> {
  return Object.fromEntries(Object.entries(env).filter((entry): entry is [string, string] => entry[1] !== undefined));
}

function sdkConnect(config: McpServerConfig, timeoutMs: number): Promise<McpServerHandle> {
  const transport = config.transport === "stdio"
    // stderr: "inherit" — a piped-but-undrained stream would eventually
    // block a chatty server when the OS pipe buffer fills.
    ? new StdioClientTransport({ command: config.command!, args: config.args ?? [], env: { ...cleanEnv(process.env), ...config.env }, stderr: "inherit" })
    : new StreamableHTTPClientTransport(new URL(config.url!), config.headers ? { requestInit: { headers: config.headers } } : {});
  return wireClient(new Client({ name: "riftx", version: RIFTX_VERSION }, { capabilities: {} }), transport, config, timeoutMs);
}

/** Client wiring shared by stdio/http and the in-memory integration test. */
export function wireClient(client: Client, transport: Transport, config: McpServerConfig, timeoutMs: number): Promise<McpServerHandle> {
  return (async () => {
    const guard = <T>(promise: Promise<T>) => new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`connect timeout after ${timeoutMs}ms`)), timeoutMs);
      promise.then((value) => { clearTimeout(timer); resolve(value); }, (error: unknown) => { clearTimeout(timer); reject(error instanceof Error ? error : new Error(String(error))); });
    });
    let closed = false;
    // The protocol invokes this when the transport closes for ANY reason —
    // server exit, network drop, or our own close() — flipping the handle
    // dead so the next acquire() reconnects instead of reusing it.
    client.onclose = () => { closed = true; };
    try {
      await guard(client.connect(transport));
      const listed = await guard(client.listTools());
      const tools: McpListedTool[] = (listed.tools ?? []).map((tool) => ({ name: tool.name, description: tool.description, inputSchema: tool.inputSchema }));
      return {
        tools,
        call: (rawName, args, signal) => {
          if (closed) return Promise.reject(new Error(`MCP server "${config.name}" was removed or reconfigured — reopen the session to use it`));
          return client.callTool({ name: rawName, arguments: args }, undefined, { signal }) as Promise<unknown>;
        },
        close: async () => {
          closed = true;
          await client.close();
        },
        get dead() {
          return closed;
        }
      };
    } catch (error) {
      await client.close().catch(() => undefined);
      throw error;
    }
  })();
}

/** Session-manager entry point: reconcile and take a reference on each entry. */
export function acquireMcpServers(configs: McpServerConfig[]): Promise<McpServerEntry[]> {
  // A no-MCP session only needs the manager when stale connections from an
  // earlier config are still around to sweep (refs 0 → closed).
  if (!configs.length && !mcpGlobal.__riftxMcpManager) return Promise.resolve([]);
  mcpGlobal.__riftxMcpManager ??= new McpManager({ connect: sdkConnect });
  return mcpGlobal.__riftxMcpManager.acquire(configs);
}

/** Session-shutdown counterpart of acquireMcpServers. */
export function releaseMcpServers(entries: readonly McpServerEntry[]): Promise<void> {
  if (!entries.length) return Promise.resolve();
  return mcpGlobal.__riftxMcpManager?.release(entries) ?? Promise.resolve();
}

/**
 * acquire → build → release-on-throw. When session construction fails partway
 * there is no record to shut down, so nothing else would ever release the
 * references — without this, repeated failed creations leak refs and stdio
 * child processes for the process lifetime.
 */
export async function withMcpReferences<T>(configs: McpServerConfig[], build: (entries: McpServerEntry[]) => Promise<T>): Promise<T> {
  const entries = await acquireMcpServers(configs);
  try {
    return await build(entries);
  } catch (error) {
    await releaseMcpServers(entries).catch(() => undefined);
    throw error;
  }
}

export type McpTestResult = { name: string; ok: boolean; toolCount?: number; error?: string };

/**
 * One-off connectivity test for the settings page. Uses a throwaway manager
 * (never the shared global), so probing a draft list cannot disturb the
 * connections live sessions are bound to; everything it opens is closed again.
 */
export async function testMcpServers(configs: McpServerConfig[], connect: ConnectFactory = sdkConnect): Promise<McpTestResult[]> {
  const manager = new McpManager({ connect });
  const entries = await manager.reconcile(configs);
  try {
    return configs.map((config, index) => {
      const entry = entries[index];
      if (entry?.state === "connected") return { name: config.name, ok: true, toolCount: entry.handle.tools.length };
      return { name: config.name, ok: false, error: entry?.state === "error" ? entry.error : "no result" };
    });
  } finally {
    await Promise.all(entries.filter((entry) => entry.state === "connected").map((entry) => entry.handle.close().catch(() => undefined)));
  }
}
