import { createServer, request as httpRequest, type ClientRequest, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { connect as netConnect, type Socket } from "node:net";

export type HostMappingTarget = { host: string; port?: number };

/**
 * Parse a CONNECT authority or Host header ("host", "host:port", "[::1]:443")
 * using the WHATWG URL parser so IPv6 literals survive. A non-special scheme
 * keeps explicitly written default ports (`host:80` stays 80 instead of being
 * normalized away like `http://` would), so the proxy always dials the port
 * the browser actually asked for.
 */
export function parseProxyAuthority(authority: string, defaultPort: number): { host: string; port: number } {
  try {
    const parsed = new URL(`riftx-authority://${authority}`);
    const host = parsed.hostname.replace(/^\[|\]$/g, "");
    if (!host) return { host: "", port: defaultPort };
    return { host, port: Number(parsed.port || defaultPort) };
  } catch {
    return { host: "", port: defaultPort };
  }
}

/**
 * A loopback HTTP proxy that applies host mappings with curl --resolve
 * semantics: the connection goes to the mapped address while the original
 * Host header (and, for HTTPS, the TLS SNI) is preserved. Requests for
 * unmapped hosts are forwarded unchanged.
 */
export class HostMappingProxy {
  readonly mappings = new Map<string, HostMappingTarget>();
  private server?: Server;
  private portValue?: number;
  private startPromise?: Promise<void>;
  /** Connections this proxy dialed itself; closeAllConnections() only covers accepted sockets. */
  private readonly upstreams = new Set<Socket | ClientRequest>();
  /** Accepted client connections (HTTP and CONNECT tunnels) kept for forceful teardown. */
  private readonly clients = new Set<import("node:stream").Duplex>();

  private trackUpstream<T extends Socket | ClientRequest>(upstream: T): T {
    this.upstreams.add(upstream);
    upstream.on("close", () => this.upstreams.delete(upstream));
    return upstream;
  }

  get proxyUrl() {
    return this.portValue !== undefined ? `http://127.0.0.1:${this.portValue}` : undefined;
  }

  async start() {
    if (this.portValue !== undefined) return;
    if (this.startPromise) return this.startPromise;
    const startPromise = this.startServer();
    this.startPromise = startPromise;
    try {
      await startPromise;
    } finally {
      if (this.startPromise === startPromise) this.startPromise = undefined;
    }
  }

  private async startServer() {
    const server = createServer((request, response) => this.handleRequest(request, response));
    this.server = server;
    server.on("connect", (request, socket, head) => this.handleConnect(request, socket, head));
    server.on("upgrade", (request, socket, head) => this.handleUpgrade(request, socket, head));
    // Keep a listener after startup so a late server error cannot crash the
    // process; a separate one-shot listener rejects startup failures.
    server.on("error", () => undefined);
    try {
      await new Promise<void>((resolve, reject) => {
        const onError = (error: Error) => {
          server.off("listening", onListening);
          reject(error);
        };
        const onListening = () => {
          server.off("error", onError);
          resolve();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(0, "127.0.0.1");
      });
    } catch (error) {
      if (this.server === server) this.server = undefined;
      try { server.close(); } catch { /* listen never completed */ }
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`Failed to start browser host-mapping proxy: ${reason}`);
    }
    const address = server.address();
    this.portValue = typeof address === "object" && address ? address.port : undefined;
  }

  private resolve(host: string, port: number): HostMappingTarget & { port: number } {
    // Match the host normalization used by the scope checker and
    // setHostMappings (lowercase, trailing dot stripped), so an authorized
    // mapping is always the one the connection actually uses.
    const mapped = this.mappings.get(host.toLowerCase().replace(/\.$/, ""));
    return mapped ? { host: mapped.host, port: mapped.port ?? port } : { host, port };
  }

  private handleRequest(request: IncomingMessage, response: ServerResponse) {
    try {
      const responseSocket = response.socket;
      if (responseSocket) {
        this.clients.add(responseSocket);
        response.on("close", () => this.clients.delete(responseSocket));
      }
      const rawUrl = request.url ?? "";
      let target: HostMappingTarget & { port: number };
      let path: string;
      if (/^https?:\/\//i.test(rawUrl)) {
        const parsed = new URL(rawUrl);
        target = this.resolve(parsed.hostname.replace(/^\[|\]$/g, ""), Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80)));
        // Forward origin-form (path only): upstream servers expect a plain
        // request line, not the proxy's absolute URL.
        path = `${parsed.pathname}${parsed.search}`;
      } else {
        const authority = parseProxyAuthority(request.headers.host ?? "", 80);
        target = this.resolve(authority.host, authority.port);
        path = rawUrl;
      }
      const upstream = this.trackUpstream(httpRequest({ host: target.host, port: target.port, method: request.method, path, headers: request.headers }));
      upstream.on("response", (upstreamResponse) => {
        response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
        upstreamResponse.pipe(response);
      });
      upstream.on("error", () => {
        if (!response.headersSent) response.writeHead(502, { "content-type": "text/plain" });
        response.end("riftx proxy: upstream connection failed");
      });
      request.pipe(upstream);
    } catch {
      if (!response.headersSent) response.writeHead(502, { "content-type": "text/plain" });
      response.end("riftx proxy: request failed");
    }
  }

  private handleConnect(request: IncomingMessage, socket: import("node:stream").Duplex, head: Buffer) {
    this.clients.add(socket);
    socket.on("close", () => this.clients.delete(socket));
    const authority = parseProxyAuthority(request.url ?? "", 443);
    const target = this.resolve(authority.host, authority.port);
    const upstream = this.trackUpstream(netConnect(target, () => {
      socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      upstream.pipe(socket);
      socket.pipe(upstream);
    }));
    upstream.on("error", () => socket.destroy());
    socket.on("error", () => upstream.destroy());
  }

  private handleUpgrade(request: IncomingMessage, socket: import("node:stream").Duplex, head: Buffer) {
    this.clients.add(socket);
    socket.on("close", () => this.clients.delete(socket));
    try {
      const rawUrl = request.url ?? "";
      const parsed = /^wss?:\/\//i.test(rawUrl) ? new URL(rawUrl) : undefined;
      const authority = parsed
        ? { host: parsed.hostname.replace(/^\[|\]$/g, ""), port: Number(parsed.port || (parsed.protocol === "wss:" ? 443 : 80)) }
        : parseProxyAuthority(request.headers.host ?? "", 80);
      const target = this.resolve(authority.host, authority.port);
      const path = parsed ? `${parsed.pathname}${parsed.search}` : rawUrl;
      const upstream = this.trackUpstream(netConnect(target, () => {
        const headers = request.rawHeaders.reduce<string[]>((lines, value, index) => {
          if (index % 2 === 0) lines.push(`${value}: ${request.rawHeaders[index + 1] ?? ""}`);
          return lines;
        }, []);
        upstream.write(`${request.method ?? "GET"} ${path || "/"} HTTP/${request.httpVersion}\r\n${headers.join("\r\n")}\r\n\r\n`);
        if (head.length) upstream.write(head);
        upstream.pipe(socket);
        socket.pipe(upstream);
      }));
      upstream.on("error", () => socket.destroy());
      socket.on("error", () => upstream.destroy());
    } catch {
      socket.destroy();
    }
  }

  /**
   * Tear down every connection the proxy owns. Called when the mapping set
   * changes: established tunnels keep flowing to their old destination, so
   * they must be forced closed for the browser to re-CONNECT under the new
   * mappings.
   */
  closeUpstreams() {
    for (const upstream of [...this.upstreams]) upstream.destroy();
    this.upstreams.clear();
    for (const client of [...this.clients]) client.destroy();
    this.clients.clear();
  }

  close() {
    this.closeUpstreams();
    this.server?.closeAllConnections?.();
    this.server?.close();
    this.server = undefined;
    this.portValue = undefined;
  }
}
