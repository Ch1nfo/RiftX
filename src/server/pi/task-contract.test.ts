import assert from "node:assert/strict";
import test from "node:test";
import { buildTaskContract, MAX_TASK_CONTRACT_CHARS, userRequestsFromBranch } from "./task-contract";

test("task contract rebuilds the root request and latest directives from the full branch", () => {
  const requests = userRequestsFromBranch([
    { type: "message", message: { role: "user", content: [{ type: "text", text: "Audit <target>" }, { type: "image", data: "x" }] } },
    { type: "compaction", summary: "lossy" },
    { type: "message", message: { role: "assistant", content: "ignored" } },
    { type: "message", message: { role: "user", content: "Do not modify data" } }
  ]);
  const contract = buildTaskContract(requests, { cwd: "/tmp/work", browserScope: ["https://target.test/*"] });
  assert.match(contract, /Audit &lt;target&gt;/);
  assert.match(contract, /1 image attachment/);
  assert.match(contract, /Do not modify data/);
  assert.match(contract, /working_directory=\/tmp\/work/);
  assert.match(contract, /browser_scope=https:\/\/target\.test\/\*/);
});

test("task contract stays inside its deterministic context budget", () => {
  const contract = buildTaskContract(["<&".repeat(30_000), "y".repeat(30_000)], {
    cwd: "/tmp/work",
    browserScope: ["z".repeat(30_000)]
  });
  assert.ok(contract.length <= MAX_TASK_CONTRACT_CHARS);
  assert.ok(contract.endsWith("</riftx-task-contract>"));
  assert.doesNotMatch(contract, /&(?:a|am|amp|l|lt|g|gt)$/);
});

test("task contract excludes synthetic subagent terminal messages", () => {
  const requests = userRequestsFromBranch([
    { type: "message", message: { role: "user", content: "Audit the target" } },
    { type: "message", message: { role: "user", content: "[RiftX subagent result]\nSubagent: API scan\nStatus: completed\nSummary:\nFound /admin" } },
    { type: "message", message: { role: "user", content: "[RiftX subagent status]\nSubagent: authz\nStatus: failed" } },
    { type: "message", message: { role: "user", content: "Continue with authorization testing" } }
  ]);
  assert.deepEqual(requests, ["Audit the target", "Continue with authorization testing"]);
  const contract = buildTaskContract(requests, { cwd: "/tmp/work", browserScope: [] });
  assert.doesNotMatch(contract, /API scan|Found \/admin|Subagent: authz/);
});
