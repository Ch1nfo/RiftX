import assert from "node:assert/strict";
import test from "node:test";
import { requiredText, validateApprovalMode, validateDismissed, validateFindingConfidence } from "./api-validation";

test("API text validation rejects blank prompt and title input", () => {
  assert.equal(requiredText("   ", "text is required"), "text is required");
  assert.equal(requiredText("Inspect auth", "text is required"), null);
  assert.equal(requiredText(undefined, "text is required"), "text is required");
});

test("API enum validation accepts only supported protocol values", () => {
  assert.equal(validateApprovalMode("request"), true);
  assert.equal(validateApprovalMode("invalid"), false);
  assert.equal(validateFindingConfidence("confirmed"), true);
  assert.equal(validateFindingConfidence("invalid"), false);
  assert.equal(validateDismissed(undefined), true);
  assert.equal(validateDismissed(false), true);
  assert.equal(validateDismissed("false"), false);
});
