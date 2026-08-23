import assert from "node:assert/strict";
import test from "node:test";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension, type BrowserScopeGuard } from "./permission-extension";
import type { ScopeDecision } from "@/browser/scope/scope-rules";
import type { RiftxEvent } from "@/lib/types";

type ToolCallHandler = (event: { toolName: string; toolCallId: string; input: unknown }, ctx: { signal?: AbortSignal }) => Promise<unknown>;
type ToolExecutionEndHandler = (event: { toolCallId: string; isError: boolean }) => unknown;

function makeHarness(guard?: BrowserScopeGuard) {
  const gate = new ApprovalGate("request");
  const events: RiftxEvent[] = [];
  let toolCallHandler: ToolCallHandler | undefined;
  let toolExecutionEndHandler: ToolExecutionEndHandler | undefined;
  const agent = {
    on(name: string, handler: unknown) {
      if (name === "tool_call") toolCallHandler = handler as ToolCallHandler;
      if (name === "tool_execution_end") toolExecutionEndHandler = handler as ToolExecutionEndHandler;
    }
  };
  const factory = createPermissionExtension(
    gate,
    (event) => events.push(event as RiftxEvent),
    undefined,
    undefined,
    guard
  );
  factory(agent as never);
  return {
    gate,
    events,
    call: (input: unknown, toolName = "browser", toolCallId = `call-${events.length}`) => {
      assert.ok(toolCallHandler, "tool_call handler not registered");
      return toolCallHandler({ toolName, toolCallId, input }, {});
    },
    end: (toolCallId: string, isError = false) => {
      assert.ok(toolExecutionEndHandler, "tool_execution_end handler not registered");
      toolExecutionEndHandler({ toolCallId, isError });
    }
  };
}

function scopeGuard(allowedHosts: string[]) {
  const granted: Array<{ url: string; exactPort?: boolean }> = [];
  const authorizedOnce: Array<{ url: string; identity?: string }> = [];
  const revokedOnce: Array<{ url: string; identity?: string }> = [];
  let mappingsOnce = 0;
  const check = (url: string): ScopeDecision => {
    const host = new URL(url).hostname;
    const allowed = allowedHosts.includes(host) || granted.some((item) => new URL(item.url).hostname === host);
    return { allowed, host, port: 80, suggestedRule: host, reason: allowed ? undefined : "no rule" };
  };
  const guard: BrowserScopeGuard = {
    check,
    authorizeOnce: (url: string, identity?: string) => { authorizedOnce.push({ url, identity }); },
    revokeOnce: (url: string, identity?: string) => { revokedOnce.push({ url, identity }); },
    grantScope: (url: string, exactPort?: boolean) => { granted.push({ url, exactPort }); },
    checkMappings: (mappings) => Object.entries(mappings)
      .filter(([, target]) => {
        const host = target.split(":")[0];
        return !allowedHosts.includes(host) && !granted.some((item) => new URL(item.url).hostname === host);
      })
      .map(([host, target]) => ({ host, target })),
    authorizeMappingsOnce: () => { mappingsOnce += 1; }
  };
  return { guard, granted, authorizedOnce, revokedOnce, get mappingsOnce() { return mappingsOnce; } };
}

test("out-of-scope navigation asks for scope approval and grants the host on task approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "navigate", url: "http://10.0.0.9:8000/" }, "browser", "nav-1");
  const request = harness.gate.pendingRequests()[0];
  assert.ok(request, "scope approval should be pending");
  assert.deepEqual(request.input, { action: "navigate", url: "http://10.0.0.9:8000/", scopeExpansion: true, suggestedRule: "10.0.0.9", reason: "no rule" });
  assert.equal(request.id, "nav-1");
  harness.gate.decide("nav-1", true, true);
  assert.equal(await pending, undefined);
  assert.deepEqual(scope.granted, []);
  assert.deepEqual(scope.authorizedOnce, [{ url: "http://10.0.0.9:8000/", identity: undefined }]);
  harness.end("nav-1");
  assert.deepEqual(scope.granted, [{ url: "http://10.0.0.9:8000/", exactPort: undefined }]);
  assert.deepEqual(scope.revokedOnce, [{ url: "http://10.0.0.9:8000/", identity: undefined }]);
});

test("out-of-scope navigation uses a one-shot authorization on once approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "navigate", url: "http://10.0.0.9/", identity: "Admin" }, "browser", "nav-2");
  harness.gate.decide("nav-2", true, false);
  assert.equal(await pending, undefined);
  assert.deepEqual(scope.granted, []);
  // The identity from the tool input reaches the manager-side authorization.
  assert.deepEqual(scope.authorizedOnce, [{ url: "http://10.0.0.9/", identity: "Admin" }]);
  harness.end("nav-2");
  assert.deepEqual(scope.revokedOnce, []);
});

test("rejected scope expansion blocks the navigation with an explanatory reason", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "navigate", url: "http://10.0.0.9/" }, "browser", "nav-3");
  harness.gate.decide("nav-3", false);
  const result = await pending;
  assert.deepEqual(result, { block: true, reason: "Navigation to 10.0.0.9 is outside the authorized browser scope and the user rejected expanding it. Continue with in-scope targets only." });
  assert.deepEqual(scope.granted, []);
  assert.deepEqual(scope.authorizedOnce, []);
});

test("in-scope navigation skips the scope flow and uses the regular approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "navigate", url: "http://authorized.test/app" }, "browser", "nav-4");
  const request = harness.gate.pendingRequests()[0];
  assert.ok(request);
  assert.deepEqual(request.input, { action: "navigate", url: "http://authorized.test/app" });
  assert.equal((request.input as { scopeExpansion?: boolean }).scopeExpansion, undefined);
  harness.gate.decide("nav-4", true);
  assert.equal(await pending, undefined);
  assert.deepEqual(scope.granted, []);
});

test("out-of-scope host-mapping targets require scope approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "set_host_mappings", mappings: { "vhost.authorized.test": "10.0.0.9:8000" } }, "browser", "map-1");
  const request = harness.gate.pendingRequests()[0];
  assert.ok(request, "mapping approval should be pending");
  assert.equal((request.input as { scopeExpansion?: boolean }).scopeExpansion, true);
  harness.gate.decide("map-1", true, true);
  assert.equal(await pending, undefined);
  assert.deepEqual(scope.granted, []);
  harness.end("map-1");
  // Both the physical destination and the logical mapping host are granted for the session.
  assert.deepEqual(scope.granted, [
    { url: "http://10.0.0.9:8000/", exactPort: true },
    { url: "http://vhost.authorized.test/", exactPort: undefined }
  ]);
  // In-scope targets skip the scope flow but still use the regular mutation approval.
  const direct = harness.call({ action: "set_host_mappings", mappings: { "alias.authorized.test": "authorized.test" } }, "browser", "map-2");
  const regularRequest = harness.gate.pendingRequests()[0];
  assert.ok(regularRequest, "regular approval expected for the mutation");
  assert.equal((regularRequest.input as { scopeExpansion?: boolean }).scopeExpansion, undefined);
  harness.gate.decide("map-2", true);
  assert.equal(await direct, undefined);
});

test("allow-once for host mappings authorizes targets temporarily", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);
  const pending = harness.call({ action: "set_host_mappings", mappings: { "vhost.authorized.test": "10.0.0.9" } }, "browser", "map-3");
  harness.gate.decide("map-3", true, false);
  assert.equal(await pending, undefined);
  assert.deepEqual(scope.granted, []);
  assert.equal(scope.mappingsOnce, 0);
  harness.end("map-3");
  assert.equal(scope.mappingsOnce, 1);
});

test("failed browser calls roll back or skip their pending scope effects", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard);

  const navigation = harness.call({ action: "navigate", url: "http://10.0.0.9/", identity: "admin" }, "browser", "nav-fail");
  harness.gate.decide("nav-fail", true, true);
  assert.equal(await navigation, undefined);
  harness.end("nav-fail", true);
  assert.deepEqual(scope.granted, []);
  assert.deepEqual(scope.revokedOnce, [{ url: "http://10.0.0.9/", identity: "admin" }]);

  const mapping = harness.call({ action: "set_host_mappings", mappings: { "vhost.test": "10.0.0.10:8443" } }, "browser", "map-fail");
  harness.gate.decide("map-fail", true, false);
  assert.equal(await mapping, undefined);
  harness.end("map-fail", true);
  assert.equal(scope.mappingsOnce, 0);
  assert.deepEqual(scope.granted, []);
});

test("invalid mappings are blocked before an approval can expand scope", async () => {
  const scope = scopeGuard(["authorized.test"]);
  scope.guard.checkMappings = () => { throw new Error("Invalid host mapping"); };
  const harness = makeHarness(scope.guard);
  assert.deepEqual(
    await harness.call({ action: "set_host_mappings", mappings: { "valid.test": "10.0.0.9", "": "" } }, "browser", "map-invalid"),
    { block: true, reason: "Invalid host mapping" }
  );
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.deepEqual(scope.granted, []);
  assert.equal(scope.mappingsOnce, 0);
});

test("read-only browser actions never raise approvals", async () => {
  const harness = makeHarness(scopeGuard([]).guard);
  for (const action of ["snapshot", "console", "requests", "tabs"]) {
    assert.equal(await harness.call({ action }, "browser", `ro-${action}`), undefined);
  }
  assert.equal(harness.gate.pendingRequests().length, 0);
});
