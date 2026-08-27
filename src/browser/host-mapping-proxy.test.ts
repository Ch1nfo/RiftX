import assert from "node:assert/strict";
import { connect as netConnect, createServer as tcpServer, type Server, type Socket } from "node:net";
import test from "node:test";
import { once } from "node:events";
import { HostMappingProxy, parseProxyAuthority } from "./runtime/host-mapping-proxy";

test("parseProxyAuthority handles IPv6, ports, and bare hosts", () => {
  assert.deepEqual(parseProxyAuthority("[::1]:443", 80), { host: "::1", port: 443 });
  assert.deepEqual(parseProxyAuthority("[2001:db8::2]:8443", 80), { host: "2001:db8::2", port: 8443 });
  assert.deepEqual(parseProxyAuthority("example.test:8080", 443), { host: "example.test", port: 8080 });
  assert.deepEqual(parseProxyAuthority("example.test", 8080), { host: "example.test", port: 8080 });
  // An explicitly written default port is preserved, never folded into the caller default.
  assert.deepEqual(parseProxyAuthority("example.test:80", 443), { host: "example.test", port: 80 });
  assert.deepEqual(parseProxyAuthority("[::1]:80", 443), { host: "::1", port: 80 });
  assert.deepEqual(parseProxyAuthority("", 8080), { host: "", port: 8080 });
});

test("concurrent proxy starts wait for the same listening socket", async () => {
  const proxy = new HostMappingProxy();
  const first = proxy.start();
  await proxy.start();
  assert.match(proxy.proxyUrl ?? "", /^http:\/\/127\.0\.0\.1:\d+$/);
  await first;
  proxy.close();
});

test("changing mappings forces CONNECT tunnels to re-establish", async () => {
  const seenA: string[] = [];
  const seenB: string[] = [];
  const makeTarget = (seen: string[]) => new Promise<{ server: Server; sockets: Set<Socket> }>((resolve) => {
    const sockets = new Set<Socket>();
    const server = tcpServer((socket) => {
      sockets.add(socket);
      socket.on("data", (chunk) => seen.push(String(chunk)));
      socket.on("close", () => sockets.delete(socket));
    });
    server.listen(0, "127.0.0.1", () => resolve({ server, sockets }));
  });
  const a = await makeTarget(seenA);
  const b = await makeTarget(seenB);
  const portA = (a.server.address() as { port: number }).port;
  const portB = (b.server.address() as { port: number }).port;

  const proxy = new HostMappingProxy();
  proxy.mappings.set("tunnel.test", { host: "127.0.0.1", port: portA });
  await proxy.start();
  const proxyPort = Number(new URL(proxy.proxyUrl ?? "").port);

  const connectTunnel = async () => {
    const client = netConnect({ host: "127.0.0.1", port: proxyPort });
    await once(client, "connect");
    client.write("CONNECT tunnel.test:443 HTTP/1.1\r\nHost: tunnel.test:443\r\n\r\n");
    const chunks: Buffer[] = [];
    const handshake = new Promise<void>((resolve) => {
      const onData = (chunk: Buffer) => {
        chunks.push(chunk);
        if (chunk.toString().includes("\r\n\r\n")) {
          client.off("data", onData);
          resolve();
        }
      };
      client.on("data", onData);
    });
    await handshake;
    assert.match(Buffer.concat(chunks).toString(), /200/);
    return client;
  };

  const first = await connectTunnel();
  first.write("before");
  await once(a.sockets.values().next().value as Socket, "data").catch(() => undefined);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.deepEqual(seenA, ["before"]);

  // Swapping the mapping must force the established tunnel down, so the next
  // CONNECT reaches the new physical destination instead of the old one.
  proxy.mappings.set("tunnel.test", { host: "127.0.0.1", port: portB });
  proxy.closeUpstreams();
  await once(first, "close");
  const second = await connectTunnel();
  second.write("after");
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.deepEqual(seenA, ["before"]);
  assert.deepEqual(seenB, ["after"]);

  // A trailing-dot host ("tunnel.test.") still resolves through the mapping,
  // never falling back to DNS, matching the scope checker's normalization.
  const dotted = netConnect({ host: "127.0.0.1", port: proxyPort });
  await once(dotted, "connect");
  dotted.write("CONNECT tunnel.test.:443 HTTP/1.1\r\nHost: tunnel.test.:443\r\n\r\n");
  await new Promise<void>((resolve) => {
    dotted.once("data", () => resolve());
  });
  dotted.write("dotted");
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.deepEqual(seenB, ["after", "dotted"]);
  dotted.destroy();

  second.destroy();
  for (const socket of [...a.sockets, ...b.sockets]) socket.destroy();
  proxy.close();
  await new Promise<void>((resolve) => a.server.close(() => resolve()));
  await new Promise<void>((resolve) => b.server.close(() => resolve()));
});

test("reset drops every mapping so a later relaunch starts clean", async () => {
  const proxy = new HostMappingProxy();
  proxy.mappings.set("target.internal", { host: "10.0.9.9", port: 8443 });
  proxy.mappings.set("other.internal", { host: "10.0.9.10" });
  assert.equal(proxy.mappings.size, 2);
  proxy.reset();
  assert.equal(proxy.mappings.size, 0);
  // close() after reset keeps the object usable (fresh start on next start()).
  proxy.close();
});
