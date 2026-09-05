export type PromptRequestState = "pending" | "accepted" | "failed";

export type PromptRequestDisposition = "keep" | "clear" | "restore";

/** Missing state is deliberately "keep": absence cannot distinguish a queued
 * dispatch from capped history or a server restart. */
export function promptRequestDisposition(state: PromptRequestState | undefined, composedTextKnown: boolean): PromptRequestDisposition {
  if (state === "failed") return "restore";
  if (state === "accepted" && composedTextKnown) return "clear";
  return "keep";
}

