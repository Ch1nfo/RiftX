import { APPROVAL_MODES, type ApprovalMode, type FindingConfidence } from "./types";

export function requiredText(value: unknown, error: string) {
  return typeof value === "string" && value.trim() ? null : error;
}

/** Ready-to-return 400 envelope for validation failures. */
export function badRequest(message: string) {
  return Response.json({ error: message }, { status: 400 });
}

export function isJsonObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

/**
 * Parse a JSON object body at the API boundary. Returns the parsed object, or
 * a ready-to-return 400 response — callers do `if (body instanceof Response) return body;`.
 */
export async function parseJsonBody(request: Request): Promise<Record<string, unknown> | Response> {
  try {
    const parsed = await request.json();
    return isJsonObject(parsed) ? parsed : Response.json({ error: "JSON body must be an object" }, { status: 400 });
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }
}

export function validateApprovalMode(value: unknown): value is ApprovalMode {
  return typeof value === "string" && APPROVAL_MODES.includes(value as ApprovalMode);
}

export function validateFindingConfidence(value: unknown): value is FindingConfidence {
  return value === undefined || value === "confirmed" || value === "likely" || value === "suspected" || value === "not_reproducible";
}

export function validateDismissed(value: unknown) {
  return value === undefined || typeof value === "boolean";
}
