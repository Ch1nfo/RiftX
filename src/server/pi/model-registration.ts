import type { AuthStorage, ModelRegistry } from "@mariozechner/pi-coding-agent";
import type { ModelProfile } from "@/lib/types";

/**
 * Session-scoped model registration helpers. Kept SDK-import-free at runtime
 * (type-only imports) so the registration and rollback contracts are
 * unit-testable against the real registry.
 */

export function registerProfileModel(authStorage: AuthStorage, modelRegistry: ModelRegistry, profile: ModelProfile, replace = false) {
  if (profile.apiKey) authStorage.setRuntimeApiKey(profile.provider, profile.apiKey);
  if (replace || !modelRegistry.find(profile.provider, profile.model)) {
    const models = modelRegistry.getAll()
      .filter((model) => model.provider === profile.provider && model.id !== profile.model)
      .map((model) => ({
        id: model.id,
        name: model.name,
        api: model.api,
        baseUrl: model.baseUrl,
        reasoning: model.reasoning,
        thinkingLevelMap: model.thinkingLevelMap,
        input: model.input,
        cost: model.cost,
        contextWindow: model.contextWindow,
        maxTokens: model.maxTokens,
        headers: model.headers,
        compat: model.compat
      }));
    modelRegistry.registerProvider(profile.provider, {
      baseUrl: profile.baseUrl,
      apiKey: profile.apiKey || "riftx-configured",
      api: profile.api,
      models: [...models, {
        id: profile.model,
        name: profile.name,
        reasoning: profile.thinkingLevel !== "off",
        input: profile.supportsImages ? ["text", "image"] : ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: profile.contextWindow,
        maxTokens: profile.maxTokens
      }]
    });
  }
  const model = modelRegistry.find(profile.provider, profile.model) as ReturnType<ModelRegistry["find"]> | undefined;
  if (!model) throw new Error(`Model ${profile.provider}/${profile.model} could not be loaded`);
  return model;
}

/**
 * Session-level tracking of the last profile that successfully registered on
 * each provider. Creation, title generation, and successful model switches
 * all update it; a failed switch's rollback restores the captured previous
 * entry (or unregisters a provider this switch introduced).
 */
const titleRuntimeCache = new Map<string, unknown>();

/**
 * Build the isolated title/name-generation runtime once per distinct profile.
 * ModelRegistry construction loads the full catalog and registerProfileModel
 * with `replace` rebuilds the provider list, so per-task name generation and
 * title summarization must not repeat that work on every call.
 */
export function memoizedTitleRuntime<T>(profile: ModelProfile, build: () => T): T {
  const key = JSON.stringify([profile.provider, profile.model, profile.name, profile.baseUrl, profile.api, profile.apiKey ?? "", profile.transport, profile.thinkingLevel, profile.supportsImages ?? false, profile.contextWindow, profile.maxTokens]);
  if (!titleRuntimeCache.has(key)) {
    titleRuntimeCache.set(key, build());
    // Bound the cache: profile churn must not grow it without limit.
    while (titleRuntimeCache.size > 16) titleRuntimeCache.delete(titleRuntimeCache.keys().next().value!);
  }
  return titleRuntimeCache.get(key) as T;
}

export type ProviderRegistrations = Map<string, ModelProfile>;

export function registerTrackedProfile(registrations: ProviderRegistrations, authStorage: AuthStorage, modelRegistry: ModelRegistry, profile: ModelProfile, replace = false) {
  const model = registerProfileModel(authStorage, modelRegistry, profile, replace);
  registrations.set(profile.provider, profile);
  return model;
}

type RegistrationOwner = {
  authStorage: AuthStorage;
  modelRegistry: ModelRegistry;
  registrations: ProviderRegistrations;
};

/**
 * Undo a failed profile switch's registration side effects for one provider.
 *
 * `captured` is the provider's registration as it was before the failed
 * switch staged its own (the tracked map is only written on success, so it
 * still holds that value). The new profile's key was written to both the
 * runtime API key override and the registry's provider request config;
 * restoring re-registers the captured profile — key, endpoint, and model set
 * — instead of deleting a provider other profiles may rely on. A provider
 * with no previous registration is removed again.
 */
export function restoreProviderRegistration(owner: RegistrationOwner, provider: string, captured: ModelProfile | undefined): "restored" | "unregistered" {
  const restore = captured ?? owner.registrations.get(provider);
  if (!restore) {
    owner.authStorage.removeRuntimeApiKey(provider);
    try { owner.modelRegistry.unregisterProvider(provider); } catch { /* provider never fully registered */ }
    return "unregistered";
  }
  registerProfileModel(owner.authStorage, owner.modelRegistry, restore, true);
  if (!restore.apiKey) owner.authStorage.removeRuntimeApiKey(provider);
  return "restored";
}
