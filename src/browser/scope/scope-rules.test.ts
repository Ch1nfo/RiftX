import assert from "node:assert/strict";
import test from "node:test";
import { hostMatches, matchScopeUrl, parseScopeRule, parseScopeRules, parseScopeTarget } from "./scope-rules";

function matches(rule: string, url: string): boolean {
  const parsed = parseScopeRule(rule);
  const target = parseScopeTarget(url);
  assert.ok(parsed, `rule should parse: ${rule}`);
  assert.ok(target, `url should parse: ${url}`);
  return matchScopeUrl([parsed], target);
}

test("host rules match any port and scheme", () => {
  assert.equal(matches("10.0.181.248", "http://10.0.181.248:8000/"), true);
  assert.equal(matches("10.0.181.248", "https://10.0.181.248:10086/app"), true);
  assert.equal(matches("10.0.181.248", "http://10.0.181.250/"), false);
});

test("host with port restricts the port but not the scheme", () => {
  assert.equal(matches("10.0.181.248:8000", "http://10.0.181.248:8000/"), true);
  assert.equal(matches("10.0.181.248:8000", "https://10.0.181.248:8000/"), true);
  assert.equal(matches("10.0.181.248:8000", "http://10.0.181.248:9101/"), false);
  // A URL without an explicit port resolves to the scheme default.
  assert.equal(matches("target.test:80", "http://target.test/"), true);
  assert.equal(matches("target.test:443", "http://target.test/"), false);
});

test("CIDR rules cover private ranges", () => {
  assert.equal(matches("10.0.0.0/8", "http://10.255.0.1:8000/"), true);
  assert.equal(matches("10.0.0.0/8", "https://10.0.181.248/"), true);
  assert.equal(matches("10.0.0.0/8", "http://192.168.1.1/"), false);
  assert.equal(matches("192.168.0.0/16", "http://192.168.13.37/"), true);
  assert.equal(matches("192.168.0.0/16", "http://192.169.0.1/"), false);
  assert.equal(matches("127.0.0.0/8", "http://127.0.0.1:3000/"), true);
  assert.equal(matches("10.0.0.1/32", "http://10.0.0.1/"), true);
  assert.equal(matches("10.0.0.1/32", "http://10.0.0.2/"), false);
  assert.equal(matches("0.0.0.0/0", "http://203.0.113.9/"), true);
});

test("IPv6 rules and targets", () => {
  assert.equal(matches("::1/128", "http://[::1]:8080/"), true);
  assert.equal(matches("::1/128", "http://[::2]/"), false);
  assert.equal(matches("2001:db8::/32", "http://[2001:db8:1::1]/"), true);
  assert.equal(matches("2001:db8::/32", "http://[2001:db9::1]/"), false);
  assert.equal(matches("[::1]", "http://[::1]:9999/"), true);
  assert.equal(matches("[::1]:8080", "http://[::1]:8080/"), true);
  assert.equal(matches("[::1]:8080", "http://[::1]:9090/"), false);
});

test("wildcard rules match the apex and subdomains only", () => {
  assert.equal(matches("*.target.com", "https://app.target.com/"), true);
  assert.equal(matches("*.target.com", "https://target.com/"), true);
  assert.equal(matches("*.target.com", "https://deep.api.target.com/"), true);
  assert.equal(matches("*.target.com", "https://target.com.evil.test/"), false);
  assert.equal(matches("*.target.com", "https://nottarget.com/"), false);
});

test("scheme-restricted rules only match their scheme", () => {
  assert.equal(matches("https://target.com", "https://target.com/"), true);
  assert.equal(matches("https://target.com", "http://target.com/"), false);
  assert.equal(matches("http://10.0.0.9", "http://10.0.0.9:8000/"), true);
});

test("hosts normalize case and trailing dots", () => {
  assert.equal(matches("API.Target.COM.", "http://api.target.com:443/"), true);
  assert.equal(matches("target.com", "http://TARGET.com/"), true);
});

test("single-label intranet hostnames are valid rules", () => {
  assert.equal(matches("intranet", "http://intranet:8080/"), true);
  assert.equal(matches("my_host.corp", "http://my_host.corp/"), true);
});

test("invalid rules are rejected", () => {
  for (const rule of ["", "  ", "*", "ftp://target.com", "10.0.0.0/33", "10.0.0.0/abc", "10.0.0.0/8:8000", "target.com:0", "target.com:70000", "a b.test", "*target.com", "*.**", "https://", "10.0.0.1/-1", "host name"]) {
    assert.equal(parseScopeRule(rule), undefined, `expected invalid: "${rule}"`);
  }
});

test("parseScopeRules drops invalid entries and keeps valid ones", () => {
  const rules = parseScopeRules(["10.0.0.0/8", "not a rule", "*.target.com", ""]);
  assert.equal(rules.length, 2);
});

test("parseScopeTarget rejects non-http schemes and malformed URLs", () => {
  assert.equal(parseScopeTarget("ftp://target.com"), undefined);
  assert.equal(parseScopeTarget("javascript:alert(1)"), undefined);
  assert.equal(parseScopeTarget("not a url"), undefined);
  const target = parseScopeTarget("https://Target.com/app");
  assert.ok(target);
  assert.equal(target.host, "target.com");
  assert.equal(target.port, 443);
});

test("hostMatches rejects names against CIDR rules and IPs against name rules", () => {
  const cidr = parseScopeRule("10.0.0.0/8")!;
  assert.equal(hostMatches(cidr, "10.1.2.3"), true);
  assert.equal(hostMatches(cidr, "example.com"), false);
  const name = parseScopeRule("example.com")!;
  assert.equal(hostMatches(name, "10.0.0.1"), false);
  assert.equal(hostMatches(name, "example.com"), true);
});

test("multiple rules allow when any rule matches", () => {
  const rules = parseScopeRules(["10.0.0.0/8", "*.target.com"]);
  assert.equal(matchScopeUrl(rules, parseScopeTarget("http://10.1.2.3/")!), true);
  assert.equal(matchScopeUrl(rules, parseScopeTarget("https://api.target.com/")!), true);
  assert.equal(matchScopeUrl(rules, parseScopeTarget("https://other.test/")!), false);
});
