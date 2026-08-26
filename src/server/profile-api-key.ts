import type { ModelProfile } from "@/lib/types";

/** The masked value the settings UI echoes back for a profile that has a stored key. */
export const MASKED_API_KEY = "••••••••";

/**
 * Resolve the apiKey to persist for an incoming profile.
 * - undefined (field absent) or the masked echo: keep the stored key — the
 *   request is not about the credential.
 * - "": explicit clear. The settings input is initialized from the masked
 *   echo, so an empty value only arrives when the user deleted it; treating
 *   it as "keep" would make stored credentials impossible to remove.
 * - any other string: the new key.
 */
export function resolveProfileApiKey(incoming: Pick<ModelProfile, "id" | "apiKey">, previous?: Pick<ModelProfile, "apiKey">) {
  if (incoming.apiKey === undefined || incoming.apiKey === MASKED_API_KEY) return previous?.apiKey ?? "";
  return incoming.apiKey;
}
