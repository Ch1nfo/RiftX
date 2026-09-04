import assert from "node:assert/strict";
import test from "node:test";
import type { Finding, SubagentTask } from "@/lib/types";
import { buildInvestigationCapsule, INVESTIGATION_CAPSULE_TYPE, MAX_INVESTIGATION_CAPSULE_CHARS, upsertInvestigationCapsule } from "./investigation-capsule";

function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    title: "Cross-tenant object access",
    asset: "https://target.test/api/object/7",
    confidence: "confirmed",
    status: "open",
    impact: "Another tenant's object is returned.",
    reproduction: "Compare the same request across identities.",
    evidence: [{ type: "request", requestRef: "r-1", method: "GET", url: "https://target.test/api/object/7", status: 200 }],
    source: "main",
    createdAt: "2026-09-04T00:00:00.000Z",
    updatedAt: "2026-09-04T00:00:00.000Z",
    ...overrides
  };
}

function subagent(overrides: Partial<SubagentTask> = {}): SubagentTask {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    parentSessionId: "parent",
    threadId: "child",
    name: "Authorization review",
    task: "Compare object access across user identities.",
    status: "running",
    model: "provider/model",
    createdAt: "2026-09-04T00:00:00.000Z",
    pendingApprovalCount: 0,
    logs: [],
    ...overrides
  };
}

test("capsule classifies durable findings and active delegated work", () => {
  const capsule = buildInvestigationCapsule([
    finding(),
    finding({ id: "likely", title: "Possible cache confusion", confidence: "likely", evidence: [{ type: "quote", quote: "different cache key" }] }),
    finding({ id: "closed", title: "Rejected SQL injection", confidence: "not_reproducible" })
  ], [subagent()]);

  assert.match(capsule, /## Verified findings/);
  assert.match(capsule, /Cross-tenant object access/);
  assert.match(capsule, /## Active hypotheses/);
  assert.match(capsule, /Possible cache confusion/);
  assert.match(capsule, /## Rejected or closed hypotheses/);
  assert.match(capsule, /Rejected SQL injection/);
  assert.match(capsule, /Await and incorporate SubAgent Authorization review/);
  assert.doesNotMatch(capsule, /different cache key/); // raw evidence never enters the capsule
});

test("capsule is bounded and escapes target-controlled fields", () => {
  const findings = Array.from({ length: 40 }, (_, index) => finding({
    id: String(index),
    title: `<instruction>${"x".repeat(1_000)}</instruction>`,
    impact: "y".repeat(2_000)
  }));
  const artifacts = Array.from({ length: 12 }, (_, index) => ({
    path: `/tmp/${"payload".repeat(40)}-${index}.txt`,
    size: 99_000,
    createdAt: `2026-09-04T01:${String(index).padStart(2, "0")}:00.000Z`
  }));
  const capsule = buildInvestigationCapsule(findings, [], artifacts);
  assert.ok(capsule.length <= MAX_INVESTIGATION_CAPSULE_CHARS);
  assert.doesNotMatch(capsule, /<instruction>/);
  assert.match(capsule, /&lt;instruction&gt;/);
  assert.ok(capsule.endsWith("</riftx-investigation-capsule>"));
  assert.equal((capsule.match(/<\/riftx-investigation-capsule>/g) ?? []).length, 1);
  assert.doesNotMatch(capsule, /&(?!amp;|lt;|gt;|quot;|apos;)/);
});

test("in-memory capsule replacement never accumulates duplicates", () => {
  const messages: unknown[] = [
    { role: "user", content: "task" },
    { role: "custom", customType: INVESTIGATION_CAPSULE_TYPE, content: "old", display: false }
  ];
  upsertInvestigationCapsule(messages, "new");
  const capsules = messages.filter((message) => (message as { customType?: string }).customType === INVESTIGATION_CAPSULE_TYPE);
  assert.equal(capsules.length, 1);
  assert.equal((capsules[0] as { content: string }).content, "new");
  upsertInvestigationCapsule(messages, "");
  assert.equal(messages.length, 1);
});

test("capsule preserves recent full-output pointers across compaction", () => {
  const capsule = buildInvestigationCapsule([], [], [{ path: "/tmp/session/mcp-scan.txt", size: 42_000, createdAt: "2026-09-04T01:00:00.000Z" }]);
  assert.match(capsule, /## Recent full-output artifacts/);
  assert.match(capsule, /mcp-scan\.txt/);
  assert.match(capsule, /bytes=42000/);
});
