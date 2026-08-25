import { APPROVAL_MODES, type ApprovalMode, type FindingConfidence } from "./types";

export function requiredText(value: unknown, error: string) {
  return typeof value === "string" && value.trim() ? null : error;
}

export function isJsonObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
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
