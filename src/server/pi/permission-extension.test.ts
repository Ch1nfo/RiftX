import assert from "node:assert/strict";
import test from "node:test";
import { ApprovalGate } from "./approval-gate";
import { createPermissionExtension, type BrowserScopeGuard } from "./permission-extension";
import { BashConcurrency } from "./bash-concurrency";
import { MutationLock } from "./mutation-lock";
import type { ScopeDecision } from "@/browser/scope/scope-rules";
import type { ApprovalRequest, RiftxEvent } from "@/lib/types";
import type { ApprovalEvaluation } from "./approval-evaluator";

type ToolCallHandler = (event: { toolName: string; toolCallId: string; input: unknown }, ctx: { signal?: AbortSignal }) => Promise<unknown>;
type ToolExecutionEndHandler = (event: { toolCallId: string; isError: boolean }) => unknown;

/** Fail fast with a clear message instead of hanging when lock coupling regresses. */
function stallGuard<T>(promise: Promise<T>, message: string, ms = 250): Promise<T> {
  return Promise.race([promise, new Promise<never>((_, reject) => setTimeout(() => reject(new Error(message)), ms))]);
}

function makeHarness(guard?: BrowserScopeGuard, bashConcurrency?: BashConcurrency, mutationLock?: MutationLock, mode: "request" | "auto" | "full" = "request", evaluate?: (request: ApprovalRequest) => Promise<ApprovalEvaluation>, emit?: (event: Record<string, unknown>) => void) {
  const gate = new ApprovalGate(mode);
  const events: RiftxEvent[] = [];
  const onEvent = emit ?? ((event: Record<string, unknown>) => events.push(event as RiftxEvent));
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
    onEvent,
    evaluate,
    mutationLock,
    bashConcurrency,
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

test("Bash reports running only after acquiring its dedicated concurrency slot", async () => {
  const limiter = new BashConcurrency(1);
  const first = makeHarness(undefined, limiter);
  const second = makeHarness(undefined, limiter);
  const firstPending = first.call({ command: "echo first" }, "bash", "bash-1");
  first.gate.decide("bash-1", true);
  await firstPending;
  const secondPending = second.call({ command: "echo second" }, "bash", "bash-2");
  second.gate.decide("bash-2", true);
  await Promise.resolve();
  assert.equal(second.events.some((event) => event.type === "tool_status"), false);
  first.end("bash-1");
  await secondPending;
  assert.deepEqual(second.events.find((event) => event.type === "tool_status"), { type: "tool_status", toolName: "bash", toolCallId: "bash-2", toolStatus: "running" });
  second.end("bash-2");
});

test("Bash waits for an exclusive mutation and then reports running", async () => {
  const mutationLock = new MutationLock();
  const releaseMutation = await mutationLock.acquire();
  const harness = makeHarness(undefined, new BashConcurrency(1), mutationLock);
  const pending = harness.call({ command: "echo protected" }, "bash", "bash-mutation");
  harness.gate.decide("bash-mutation", true);

  await Promise.resolve();
  assert.equal(harness.events.some((event) => event.type === "tool_status"), false);
  releaseMutation();
  await pending;
  assert.deepEqual(harness.events.find((event) => event.type === "tool_status"), {
    type: "tool_status",
    toolName: "bash",
    toolCallId: "bash-mutation",
    toolStatus: "running"
  });
  harness.end("bash-mutation");
});

test("a running Bash scan does not block a browser call", async () => {
  const mutationLock = new MutationLock();
  const bash = makeHarness(undefined, new BashConcurrency(1), mutationLock, "full");
  const browser = makeHarness(undefined, undefined, mutationLock, "full");

  const bashPending = bash.call({ command: "find . -type f" }, "bash", "bash-scan");
  await bashPending;
  assert.deepEqual(bash.events.find((event) => event.type === "tool_status"), { type: "tool_status", toolName: "bash", toolCallId: "bash-scan", toolStatus: "running" });

  // Browser coordinates no cross-tool lock (BrowserManager serializes its own
  // state per instance), so it must complete while the Bash scan still holds
  // the shared file lock.
  const browserPending = browser.call({ action: "navigate", url: "http://authorized.test/" }, "browser", "browser-nav");
  await stallGuard(browserPending, "browser call stalled behind the Bash-held file lock");
  assert.deepEqual(browser.events.find((event) => event.type === "tool_status"), { type: "tool_status", toolName: "browser", toolCallId: "browser-nav", toolStatus: "running" });

  bash.end("bash-scan");
  browser.end("browser-nav");
});

test("an approved browser call does not block Bash or file mutations", async () => {
  const mutationLock = new MutationLock();
  const browser = makeHarness(undefined, undefined, mutationLock, "full");
  await browser.call({ action: "navigate", url: "http://authorized.test/" }, "browser", "browser-nav");

  const bash = makeHarness(undefined, new BashConcurrency(1), mutationLock, "full");
  await stallGuard(bash.call({ command: "echo probe" }, "bash", "bash-1"), "Bash stalled behind a pending browser call");
  bash.end("bash-1");

  const write = makeHarness(undefined, undefined, mutationLock, "full");
  await stallGuard(write.call({ path: "a.txt", content: "x" }, "write", "write-1"), "write stalled behind a pending browser call");
  write.end("write-1");
  browser.end("browser-nav");
});

test("write still waits for shared Bash holders on the file lock", async () => {
  const mutationLock = new MutationLock();
  const releaseShared = await mutationLock.acquireShared();
  const harness = makeHarness(undefined, undefined, mutationLock, "full");
  const pending = harness.call({ path: "a.txt", content: "x" }, "write", "write-shared");
  await Promise.resolve();
  assert.equal(harness.events.some((event) => event.type === "tool_status"), false);
  releaseShared();
  await pending;
  assert.deepEqual(harness.events.find((event) => event.type === "tool_status"), { type: "tool_status", toolName: "write", toolCallId: "write-shared", toolStatus: "running" });
  harness.end("write-shared");
});

test("Bash without a limiter is blocked instead of running without coordination", async () => {
  const harness = makeHarness();
  const pending = harness.call({ command: "echo unguarded" }, "bash", "bash-no-limiter");
  harness.gate.decide("bash-no-limiter", true);
  assert.deepEqual(await pending, { block: true, reason: "Bash concurrency limiter is required" });
});

test("Bash does not treat the shared mutation lock as its concurrency limiter", async () => {
  const harness = makeHarness(undefined, undefined, new MutationLock());
  const pending = harness.call({ command: "echo unguarded" }, "bash", "bash-only-mutation-lock");
  harness.gate.decide("bash-only-mutation-lock", true);

  assert.deepEqual(await pending, { block: true, reason: "Bash concurrency limiter is required" });
});

test("a tool status listener failure releases Bash and mutation slots", async () => {
  const bashConcurrency = new BashConcurrency(1);
  const mutationLock = new MutationLock();
  const harness = makeHarness(undefined, bashConcurrency, mutationLock, "full", undefined, () => { throw new Error("status listener failed"); });
  const result = await harness.call({ command: "echo release" }, "bash", "bash-status-listener");

  assert.deepEqual(result, { block: true, reason: "status listener failed" });
  assert.equal(bashConcurrency.running, 0);
  const release = await mutationLock.acquire();
  release();
});

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

test("full access bypasses both regular and browser-scope approvals", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard, undefined, undefined, "full");
  const pending = harness.call({ action: "navigate", url: "http://10.0.0.9:8000/" }, "browser", "full-nav");

  assert.equal(await pending, undefined);
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
  assert.deepEqual(scope.authorizedOnce, [{ url: "http://10.0.0.9:8000/", identity: undefined }]);
});

test("full access bypasses every guarded tool without an approval event", async () => {
  for (const [toolName, input] of [["write", { path: "file.txt", content: "x" }], ["edit", { path: "file.txt", oldText: "x", newText: "y" }]] as const) {
    const harness = makeHarness(undefined, undefined, undefined, "full");
    assert.equal(await harness.call(input, toolName, `full-${toolName}`), undefined);
    assert.equal(harness.gate.pendingRequests().length, 0);
    assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
  }
  const bash = makeHarness(undefined, new BashConcurrency(1), new MutationLock(), "full");
  assert.equal(await bash.call({ command: "echo full" }, "bash", "full-bash"), undefined);
  assert.equal(bash.gate.pendingRequests().length, 0);
  assert.equal(bash.events.some((event) => event.type === "approval_required"), false);
  bash.end("full-bash");
});

test("full access bypasses out-of-scope host mapping approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard, undefined, undefined, "full");
  const pending = harness.call({ action: "set_host_mappings", mappings: { "vhost.authorized.test": "10.0.0.9:8000" } }, "browser", "full-map");

  assert.equal(await pending, undefined);
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
  harness.end("full-map");
  assert.equal(scope.mappingsOnce, 1);
});

test("auto approval evaluates browser scope expansion without human approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const evaluations: ApprovalRequest[] = [];
  const harness = makeHarness(scope.guard, undefined, undefined, "auto", async (request) => {
    evaluations.push(request);
    return { approved: true, reason: "approved by test evaluator" };
  });
  const pending = harness.call({ action: "navigate", url: "http://10.0.0.9/" }, "browser", "auto-nav");

  assert.equal(await pending, undefined);
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
  assert.equal(evaluations.length, 1, "the scope evaluator authorizes the complete out-of-scope navigation call");
});

test("auto approval blocks when its evaluator rejects scope expansion", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard, undefined, undefined, "auto", async () => ({ approved: false, reason: "outside test scope" }));
  const result = await harness.call({ action: "navigate", url: "http://10.0.0.9/" }, "browser", "auto-reject");

  assert.deepEqual(result, { block: true, reason: "Rejected by RiftX approval evaluator: outside test scope" });
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
});

test("auto approval explains how to recover when its evaluator is unavailable", async () => {
  const harness = makeHarness(undefined, undefined, undefined, "auto");
  const result = await harness.call({ path: "file.txt", content: "x" }, "write", "auto-no-evaluator");

  assert.deepEqual(result, {
    block: true,
    reason: "Automatic approval is unavailable. Switch approval mode to request approval or full access, then retry."
  });
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
});

test("auto approval evaluates host mapping scope without human approval", async () => {
  const scope = scopeGuard(["authorized.test"]);
  const harness = makeHarness(scope.guard, undefined, undefined, "auto", async () => ({ approved: true, reason: "approved by test evaluator" }));
  const pending = harness.call({ action: "set_host_mappings", mappings: { "vhost.authorized.test": "10.0.0.9:8000" } }, "browser", "auto-map");

  assert.equal(await pending, undefined);
  assert.equal(harness.gate.pendingRequests().length, 0);
  assert.equal(harness.events.some((event) => event.type === "approval_required"), false);
  harness.end("auto-map");
  assert.equal(scope.mappingsOnce, 1);
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
