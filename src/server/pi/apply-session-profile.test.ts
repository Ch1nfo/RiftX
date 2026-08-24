import assert from "node:assert/strict";
import test from "node:test";
import type { ModelProfile } from "@/lib/types";
import { switchSessionProfile, withProfileSwitchLock, type ProfileSwitchRecord } from "./apply-session-profile";

const baseProfile = (overrides: Partial<ModelProfile> = {}): ModelProfile => ({
  id: "old", name: "old", provider: "p", model: "m1", baseUrl: "https://x.test",
  api: "openai-completions", transport: "auto", contextWindow: 1000, maxTokens: 100, thinkingLevel: "off",
  ...overrides
});

type Calls = string[];

function makeRecord(id: string, calls: Calls, profile: ModelProfile): ProfileSwitchRecord {
  return {
    id,
    profile,
    model: { id: profile.model },
    session: {
      isStreaming: false,
      setModel: async (model: unknown) => { calls.push(`${id}:setModel:${(model as { id: string }).id}`); },
      setThinkingLevel: () => calls.push(`${id}:setThinkingLevel`)
    },
    settingsManager: { setTransport: () => calls.push(`${id}:setTransport`) }
  };
}

const deps = {
  prepareModel: (_record: ProfileSwitchRecord, profile: ModelProfile) => ({ model: { id: profile.model } }),
  hasConfiguredAuth: () => true,
  applyTransport: () => undefined
};

test("switching one session leaves other live sessions untouched", async () => {
  const calls: Calls = [];
  const recordA = makeRecord("a", calls, baseProfile());
  const recordB = makeRecord("b", calls, baseProfile());
  const next = baseProfile({ id: "new", model: "m2", transport: "sse", thinkingLevel: "low" });
  // Even when the requested profile equals the global default (id "new" is
  // the default elsewhere), the target's current profile decides.
  assert.equal(await switchSessionProfile(recordA, next, deps), true);
  assert.deepEqual(callsA(calls), ["setModel:m2", "setThinkingLevel", "setTransport"]);
  assert.equal(calls.filter((call) => call.startsWith("b:")).length, 0);
  assert.equal(recordA.profile.id, "new");
  assert.equal((recordA.model as { id: string }).id, "m2");
  assert.equal(recordB.profile.id, "old");

  // The same profile again is a successful no-op.
  assert.equal(await switchSessionProfile(recordA, next, deps), false);
  assert.equal(calls.filter((call) => call.startsWith("a:")).length, 3);
});

test("streaming or shutting-down sessions reject the switch as busy", async () => {
  const calls: Calls = [];
  const streaming = makeRecord("s", calls, baseProfile());
  streaming.session.isStreaming = true;
  await assert.rejects(() => switchSessionProfile(streaming, baseProfile({ id: "new" }), deps), /SESSION_BUSY|running/);

  const shuttingDown = makeRecord("t", calls, baseProfile());
  shuttingDown.shutdownPromise = Promise.resolve();
  await assert.rejects(() => switchSessionProfile(shuttingDown, baseProfile({ id: "new" }), deps), /SESSION_BUSY|shutting down/);
  assert.equal(calls.length, 0);
});

test("a failed switch rolls the session back to its previous model", async () => {
  const calls: Calls = [];
  const record = makeRecord("r", calls, baseProfile());
  record.session.setModel = async (model: unknown) => {
    calls.push(`setModel:${(model as { id: string }).id}`);
    if ((model as { id: string }).id === "m2") throw new Error("registry rejected");
  };
  await assert.rejects(() => switchSessionProfile(record, baseProfile({ id: "new", model: "m2" }), deps), /registry rejected/);
  assert.deepEqual(calls, ["setModel:m2", "setModel:m1", "r:setThinkingLevel", "r:setTransport"]);
  assert.equal(record.profile.id, "old");
  assert.equal((record.model as { id: string }).id, "m1");
});

function callsA(calls: Calls) {
  return calls.filter((call) => call.startsWith("a:")).map((call) => call.slice(2));
}

test("endpoint, protocol, credential, and image-capability changes must switch", async () => {
  // Same id/model everywhere; only the runtime-relevant extras differ.
  const cases: Array<[string, Partial<ModelProfile>, Partial<ModelProfile>]> = [
    ["baseUrl", {}, { baseUrl: "https://new.test" }],
    ["apiKey", { apiKey: "k1" }, { apiKey: "k2" }],
    ["supportsImages", {}, { supportsImages: true }],
    ["api", {}, { api: "anthropic-messages" as ModelProfile["api"] }]
  ];
  for (const [label, current, next] of cases) {
    const calls: Calls = [];
    const record = makeRecord(`e-${label}`, calls, baseProfile(current));
    assert.equal(await switchSessionProfile(record, baseProfile({ ...current, ...next }), deps), true, `${label} change must switch`);
    assert.ok(calls.some((call) => call.includes("setModel")), `${label} change must call setModel`);
  }
  // Identical credentials are still a no-op.
  const same: Calls = [];
  const record = makeRecord("e-same", same, baseProfile({ apiKey: "k1" }));
  assert.equal(await switchSessionProfile(record, baseProfile({ apiKey: "k1" }), deps), false);
  assert.equal(same.length, 0);
});

test("a failed switch rolls the runtime API key override back", async () => {
  const authCalls: string[] = [];
  const authStorage = {
    setRuntimeApiKey: (provider: string, key: string) => authCalls.push(`set:${provider}:${key}`),
    removeRuntimeApiKey: (provider: string) => authCalls.push(`remove:${provider}`)
  };
  const record = makeRecord("auth", [], baseProfile({ apiKey: "old-key" }));
  const failDeps = {
    prepareModel: (target: unknown, next: ModelProfile) => {
      const current = (target as { profile: ModelProfile }).profile;
      authStorage.setRuntimeApiKey(next.provider, next.apiKey!);
      return {
        model: { id: next.model },
        rollback: () => {
          // Mirror the production rollback decision.
          if (current.provider === next.provider && current.apiKey) authStorage.setRuntimeApiKey(next.provider, current.apiKey);
          else authStorage.removeRuntimeApiKey(next.provider);
        }
      };
    },
    hasConfiguredAuth: () => true,
    applyTransport: () => undefined
  };
  record.session.setModel = async () => { throw new Error("switch blew up"); };
  await assert.rejects(() => switchSessionProfile(record, baseProfile({ apiKey: "new-key" }), failDeps), /switch blew up/);
  // The new key was applied during prepare and restored to the old one on failure.
  assert.deepEqual(authCalls, ["set:p:new-key", "set:p:old-key"]);
  // A profile without a previous key removes the override instead.
  authCalls.length = 0;
  const bare = makeRecord("auth2", [], baseProfile());
  bare.session.setModel = async () => { throw new Error("switch blew up"); };
  await assert.rejects(() => switchSessionProfile(bare, baseProfile({ apiKey: "new-key" }), failDeps), /switch blew up/);
  assert.deepEqual(authCalls, ["set:p:new-key", "remove:p"]);
});

test("a concurrent second switch is rejected while the first is in flight", async () => {
  const calls: Calls = [];
  const record = makeRecord("race", calls, baseProfile());
  let releaseB: (() => void) | undefined;
  record.session.setModel = async (model: unknown) => {
    const id = (model as { id: string }).id;
    calls.push(`set:${id}`);
    if (id === "m-b") await new Promise<void>((resolve) => { releaseB = resolve; });
  };
  // Production composition: the session manager runs every switch inside
  // withProfileSwitchLock, so this mirrors the real critical section.
  const switchUnderLock = (profile: ModelProfile) => withProfileSwitchLock(record, "reject", () => switchSessionProfile(record, profile, deps));
  // A -> B starts and hangs inside setModel.
  const switchToB = switchUnderLock(baseProfile({ id: "b", model: "m-b" }));
  await new Promise((resolve) => setTimeout(resolve, 20));
  // A -> C arrives mid-flight: it must be rejected with SESSION_BUSY instead
  // of racing B's eventual rollback against its own commit.
  await assert.rejects(() => switchUnderLock(baseProfile({ id: "c", model: "m-c" })), /SESSION_BUSY|already in progress/);
  releaseB!();
  assert.equal(await switchToB, true);
  assert.equal(record.profileSwitch, undefined, "lock released after completion");
  assert.deepEqual(calls, ["set:m-b", "race:setThinkingLevel", "race:setTransport"]);
  // After the lock frees, the next switch proceeds normally.
  assert.equal(await switchUnderLock(baseProfile({ id: "c", model: "m-c" })), true);
  assert.equal(record.profile.id, "c");
});

test("withProfileSwitchLock serializes wait-mode callers and clears on failure", async () => {
  const order: string[] = [];
  const record: import("./apply-session-profile").ProfileSwitchLock = {};
  const first = withProfileSwitchLock(record, "wait", async () => {
    order.push("first:start");
    await new Promise((resolve) => setTimeout(resolve, 40));
    order.push("first:end");
    throw new Error("first failed");
  });
  await new Promise((resolve) => setTimeout(resolve, 10));
  const second = withProfileSwitchLock(record, "wait", async () => {
    order.push("second:start");
    return "second-done";
  });
  await assert.rejects(() => first, /first failed/);
  assert.equal(await second, "second-done");
  assert.deepEqual(order, ["first:start", "first:end", "second:start"]);
  assert.equal(record.profileSwitch, undefined);
});
