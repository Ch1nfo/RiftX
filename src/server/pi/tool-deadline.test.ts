import assert from "node:assert/strict";
import test from "node:test";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";
import { withToolDeadline } from "./tool-deadline";

test("withToolDeadline bounds a Pi tool even when its implementation ignores cancellation", async () => {
  let receivedSignal: AbortSignal | undefined;
  const tool = withToolDeadline({
    name: "read",
    label: "read",
    description: "Read a file.",
    parameters: {} as ToolDefinition["parameters"],
    execute: async (_id, _params, signal) => {
      receivedSignal = signal;
      return new Promise(() => undefined);
    }
  } as ToolDefinition, 10);

  await assert.rejects(tool.execute("read-1", {}, undefined, undefined, {} as never), /read timed out after 10ms/);
  assert.equal(receivedSignal?.aborted, true);
  assert.match(tool.description, /RiftX stops this operation/);
});
