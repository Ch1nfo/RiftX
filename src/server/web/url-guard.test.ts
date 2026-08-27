import assert from "node:assert/strict";
import test from "node:test";
import { assertFetchableUrl, isBlockedIp } from "./url-guard";

const publicResolve = async () => ["93.184.216.34"];

test("blocks loopback, private, link-local, and reserved IPv4", () => {
  for (const ip of ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1"]) {
    assert.ok(isBlockedIp(ip), `${ip} should be blocked`);
  }
  assert.equal(isBlockedIp("8.8.8.8"), null);
  assert.equal(isBlockedIp("1.1.1.1"), null);
});

test("blocks dangerous IPv6 forms including v4-mapped", () => {
  for (const ip of ["::1", "::ffff:127.0.0.1", "fe80::1", "fc00::1", "fd00::1", "2001:db8::1"]) {
    assert.ok(isBlockedIp(ip), `${ip} should be blocked`);
  }
  assert.equal(isBlockedIp("2606:4700:4700::1111"), null);
});

test("blocks every textual form of IPv4-mapped and embedded IPv6", () => {
  // ::ffff:127.0.0.1 in dotted, hex, and fully-expanded hex spellings.
  for (const ip of [
    "::ffff:7f00:1",
    "::ffff:a00:1",
    "::ffff:a9fe:a9fe",
    "::ffff:c0a8:101",
    "0:0:0:0:0:ffff:7f00:1",
    "0:0:0:0:0:ffff:127.0.0.1",
    "::127.0.0.1",
    "::7f00:1",
    "64:ff9b::a00:1",
    "64:ff9b::169.254.169.254"
  ]) {
    assert.ok(isBlockedIp(ip), `${ip} should be blocked`);
  }
  // A mapped PUBLIC IPv4 stays allowed: ::ffff:8.8.8.8 is just 8.8.8.8.
  assert.equal(isBlockedIp("::ffff:8.8.8.8"), null);
  assert.equal(isBlockedIp("::ffff:808:808"), null);
});

test("assertFetchableUrl rejects schemes, credentials, and local targets", async () => {
  await assert.rejects(assertFetchableUrl("file:///etc/passwd"), /http\(s\)/);
  await assert.rejects(assertFetchableUrl("https://user:pass@example.com/page"), /credentials/);
  await assert.rejects(assertFetchableUrl("http://localhost:3000/api"), /local hostnames/);
  await assert.rejects(assertFetchableUrl("http://127.0.0.1:3000/api/settings"), /loopback/);
  await assert.rejects(assertFetchableUrl("http://169.254.169.254/latest/meta-data/"), /link-local/);
  await assert.rejects(assertFetchableUrl("http://[::1]/"), /loopback/);
});

test("assertFetchableUrl validates DNS answers so a public name cannot point inward", async () => {
  await assertFetchableUrl("https://research.example.com/docs", { resolveDns: publicResolve });
  await assert.rejects(
    assertFetchableUrl("https://research.example.com/docs", { resolveDns: async () => ["203.0.113.10", "10.0.0.5"] }),
    /resolves to a private network/
  );
  await assert.rejects(
    assertFetchableUrl("https://research.example.com/docs", { resolveDns: async () => ["169.254.169.254"] }),
    /link-local/
  );
});
