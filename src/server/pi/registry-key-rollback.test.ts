import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

type RegistryLike = {
  registerProvider(provider: string, config: Record<string, unknown>): void;
  unregisterProvider(provider: string): void;
  find(provider: string, model: string): unknown;
  getApiKeyAndHeaders(model: unknown): Promise<{ ok: boolean; apiKey?: string; error?: string }>;
};

/**
 * Verifies credential rollback against the real SDK resolution chain — the
 * registry keeps the provider request config as a second key store next to
 * the AuthStorage runtime override, so a rollback that only removes the
 * override leaves the new key serving the old model.
 */
async function loadSdk() {
  const module = await import(pathToFileURL(join(process.cwd(), "node_modules/@mariozechner/pi-coding-agent/dist/index.js")).href) as {
    AuthStorage: { inMemory: () => unknown };
    ModelRegistry: { create(authStorage: unknown, modelsPath: string): RegistryLike };
  };
  return module;
}

function providerConfig(apiKey: string) {
  return {
    baseUrl: "https://x.test/v1",
    apiKey,
    api: "openai-completions",
    models: [{
      id: "m-old",
      name: "m-old",
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 8000,
      maxTokens: 512
    }]
  };
}

test("a failed switch's rollback restores the key the old model resolves to", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-registry-"));
  try {
    const { AuthStorage, ModelRegistry } = await loadSdk();
    const auth = AuthStorage.inMemory() as { setRuntimeApiKey(provider: string, key: string): void; removeRuntimeApiKey(provider: string): void };
    const registry = ModelRegistry.create(auth, join(root, "models.json"));
    // Pre-switch state: the old profile registered its provider and key.
    registry.registerProvider("prov", providerConfig("old-key"));

    // Reproduce the bug shape first: switching registers the new key and a
    // rollback that only removes the runtime override leaves the registry
    // still serving the new key to the old model.
    registry.registerProvider("prov", providerConfig("new-key"));
    auth.removeRuntimeApiKey("prov");
    const unrolled = await registry.getApiKeyAndHeaders(registry.find("prov", "m-old"));
    assert.equal(unrolled.ok, true);
    assert.equal((unrolled as { apiKey?: string }).apiKey, "new-key");

    // The production rollback re-registers the previous profile (restoring
    // both the runtime override path and the provider request config).
    registry.registerProvider("prov", providerConfig("old-key"));
    const restored = await registry.getApiKeyAndHeaders(registry.find("prov", "m-old"));
    assert.equal(restored.ok, true);
    assert.equal((restored as { apiKey?: string }).apiKey, "old-key");

    // A provider the old profile never used is unregistered again, so the
    // next provider lookup cannot inherit the new profile's registration.
    registry.registerProvider("other", providerConfig("new-key"));
    registry.unregisterProvider("other");
    assert.equal(registry.find("other", "m-old"), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a cross-provider rollback keeps a pre-existing provider registration intact", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-registry-"));
  try {
    const { AuthStorage, ModelRegistry } = await loadSdk();
    const { registerProfileModel, registerTrackedProfile, restoreProviderRegistration } = await import("./model-registration") as {
      registerProfileModel: typeof import("./model-registration").registerProfileModel;
      registerTrackedProfile: typeof import("./model-registration").registerTrackedProfile;
      restoreProviderRegistration: typeof import("./model-registration").restoreProviderRegistration;
    };
    const auth = AuthStorage.inMemory() as unknown as Parameters<typeof registerTrackedProfile>[1];
    const registry = ModelRegistry.create(auth, join(root, "models.json")) as unknown as Parameters<typeof registerTrackedProfile>[2];
    const registrations = new Map<string, import("@/lib/types").ModelProfile>();

    const mainProfile: import("@/lib/types").ModelProfile = {
      id: "main", name: "main", provider: "prov-a", model: "m-a", baseUrl: "https://a.test/v1",
      api: "openai-completions", transport: "auto", contextWindow: 8000, maxTokens: 512, thinkingLevel: "off", apiKey: "a-key"
    };
    const titleProfile: import("@/lib/types").ModelProfile = {
      id: "title", name: "title", provider: "prov-b", model: "title-model", baseUrl: "https://b.test/v1",
      api: "openai-completions", transport: "auto", contextWindow: 4000, maxTokens: 128, thinkingLevel: "off", apiKey: "title-key"
    };
    // Session creation registers the main and title profiles on the tracked map.
    registerTrackedProfile(registrations, auth, registry, mainProfile);
    registerTrackedProfile(registrations, auth, registry, titleProfile);
    const beforeModel = registry.find("prov-b", "title-model");
    assert.ok(beforeModel);
    const before = await registry.getApiKeyAndHeaders(beforeModel);
    assert.equal((before as { apiKey?: string }).apiKey, "title-key");

    // A failed switch to a different profile on provider B overwrites B's key
    // and endpoint mid-registration.
    const switchProfile = { ...titleProfile, id: "new", model: "m-new", apiKey: "new-key", baseUrl: "https://evil.test/v1" };
    registerProfileModel(auth, registry, switchProfile, true);

    // The production rollback restores B's pre-existing registration instead
    // of unregistering the provider.
    const outcome = restoreProviderRegistration(
      { authStorage: auth, modelRegistry: registry, registrations },
      "prov-b",
      registrations.get("prov-b")
    );
    assert.equal(outcome, "restored");
    const titleModel = registry.find("prov-b", "title-model");
    assert.ok(titleModel, "pre-existing title model must survive the rollback");
    const after = await registry.getApiKeyAndHeaders(titleModel);
    assert.equal(after.ok, true);
    assert.equal((after as { apiKey?: string }).apiKey, "title-key", "credentials resolve to the pre-existing key");
    assert.equal((titleModel as { baseUrl?: string }).baseUrl, "https://b.test/v1", "endpoint restored");

    // A provider the failed switch introduced is still fully removed.
    registerProfileModel(auth, registry, { ...switchProfile, provider: "prov-c" }, true);
    const introduced = restoreProviderRegistration(
      { authStorage: auth, modelRegistry: registry, registrations },
      "prov-c",
      registrations.get("prov-c")
    );
    assert.equal(introduced, "unregistered");
    assert.equal(registry.find("prov-c", "m-new"), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("the tracking map restores the last successful registration, not a static snapshot", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-registry-"));
  try {
    const { AuthStorage, ModelRegistry } = await loadSdk();
    const { registerProfileModel, registerTrackedProfile, restoreProviderRegistration } = await import("./model-registration") as {
      registerProfileModel: typeof import("./model-registration").registerProfileModel;
      registerTrackedProfile: typeof import("./model-registration").registerTrackedProfile;
      restoreProviderRegistration: typeof import("./model-registration").restoreProviderRegistration;
    };
    const auth = AuthStorage.inMemory() as unknown as Parameters<typeof registerTrackedProfile>[1];
    const registry = ModelRegistry.create(auth, join(root, "models.json")) as unknown as Parameters<typeof registerTrackedProfile>[2];
    const registrations = new Map<string, import("@/lib/types").ModelProfile>();

    const profile = (id: string, provider: string, key: string, baseUrl: string): import("@/lib/types").ModelProfile => ({
      id, name: id, provider, model: `${id}-model`, baseUrl,
      api: "openai-completions", transport: "auto", contextWindow: 4000, maxTokens: 128, thinkingLevel: "off", apiKey: key
    });
    const mainA = profile("main", "shared", "main-key", "https://main.test/v1");
    const childA = profile("child", "shared", "child-key", "https://child.test/v1");
    // Scenario 1: main and child share a provider; child registered last, so
    // child's key is the one live before a failed switch.
    registerTrackedProfile(registrations, auth, registry, mainA);
    registerTrackedProfile(registrations, auth, registry, childA);
    registerProfileModel(auth, registry, profile("bad", "shared", "bad-key", "https://evil.test/v1"), true);
    restoreProviderRegistration({ authStorage: auth, modelRegistry: registry, registrations }, "shared", registrations.get("shared"));
    const shared = registry.find("shared", "child-model");
    assert.ok(shared, "child model survives");
    const sharedAuth = await registry.getApiKeyAndHeaders(shared);
    assert.equal((sharedAuth as { apiKey?: string }).apiKey, "child-key", "last successful registration (child) is restored, not the creation snapshot");

    // Scenario 2: a runtime title registration on another provider is the
    // captured state even though it happened after creation.
    const titleProfile = profile("title", "prov-t", "title-key", "https://t.test/v1");
    registerTrackedProfile(registrations, auth, registry, titleProfile);
    registerProfileModel(auth, registry, profile("bad-t", "prov-t", "bad-key", "https://evil.test/v1"), true);
    restoreProviderRegistration({ authStorage: auth, modelRegistry: registry, registrations }, "prov-t", registrations.get("prov-t"));
    const titleModel = registry.find("prov-t", "title-model");
    const titleAuth = await registry.getApiKeyAndHeaders(titleModel!);
    assert.equal((titleAuth as { apiKey?: string }).apiKey, "title-key");

    // Scenario 3: a historically successful switch to B (tracked), then a
    // successful switch to C, then a FAILED switch back to B — B's tracked
    // registration is restored rather than the provider being unregistered.
    const historicalB = profile("hist-b", "prov-b", "b-key", "https://b.test/v1");
    registerTrackedProfile(registrations, auth, registry, historicalB);
    registerTrackedProfile(registrations, auth, registry, profile("hist-c", "prov-c", "c-key", "https://c.test/v1"));
    registerProfileModel(auth, registry, { ...historicalB, id: "again", apiKey: "again-key", baseUrl: "https://again.test/v1" }, true);
    const outcome = restoreProviderRegistration({ authStorage: auth, modelRegistry: registry, registrations }, "prov-b", registrations.get("prov-b"));
    assert.equal(outcome, "restored");
    const histModel = registry.find("prov-b", "hist-b-model");
    const histAuth = await registry.getApiKeyAndHeaders(histModel!);
    assert.equal((histAuth as { apiKey?: string }).apiKey, "b-key", "historical successful registration survives a failed re-switch");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
