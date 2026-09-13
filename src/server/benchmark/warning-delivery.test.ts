import assert from "node:assert/strict";
import test from "node:test";
import { BenchmarkWarningDelivery } from "./warning-delivery";

function setup() {
  const acknowledgements: unknown[][] = [];
  const delivery = new BenchmarkWarningDelivery({
    acknowledgeAttemptWarning: async (...args) => { acknowledgements.push(args); return true; }
  }, "main");
  return { delivery, acknowledgements };
}
const warning = { uniqueCode: "fixture", currentAttemptStartedAt: 100 };

test("warning acknowledgements retain the sampled attempt identity across a new round", async () => {
  const { delivery, acknowledgements } = setup();
  delivery.prepare(warning, "old-attempt-packet");
  delivery.sample([{ content: "old-attempt-packet" }]);
  delivery.prepare({ ...warning, currentAttemptStartedAt: 200 }, "new-attempt-packet");
  await delivery.complete({ role: "assistant", stopReason: "toolUse" });
  assert.deepEqual(acknowledgements, [["main", "fixture", 100]]);
  delivery.sample([{ content: "new-attempt-packet" }]);
  await delivery.complete({ role: "assistant", stopReason: "stop" });
  assert.deepEqual(acknowledgements, [["main", "fixture", 100], ["main", "fixture", 200]]);
});

test("rebuilding a warning packet does not acknowledge it before a successful sampled reply", async () => {
  const { delivery, acknowledgements } = setup();
  delivery.prepare(warning, "first-packet");
  delivery.prepare(warning, "rebuilt-packet");
  await delivery.complete({ role: "assistant", stopReason: "stop" });
  assert.equal(acknowledgements.length, 0);
  delivery.sample([{ content: [{ type: "text", text: "rebuilt-packet" }] }]);
  delivery.prepare(undefined); // A later context refresh cannot erase the in-flight packet.
  await delivery.complete({ role: "assistant", stopReason: "toolUse" });
  assert.deepEqual(acknowledgements, [["main", "fixture", 100]]);
  await delivery.complete({ role: "assistant", stopReason: "stop" });
  assert.equal(acknowledgements.length, 1);
});

test("failed, aborted, or transformed-away warnings remain eligible for the next request", async () => {
  const { delivery, acknowledgements } = setup();
  delivery.prepare(warning, "notice-packet");
  delivery.sample([{ content: "unrelated" }]);
  await delivery.complete({ role: "assistant", stopReason: "stop" });
  for (const stopReason of ["error", "aborted"]) {
    delivery.sample([{ content: "notice-packet" }]);
    await delivery.complete({ role: "assistant", stopReason });
  }
  assert.equal(acknowledgements.length, 0);
  delivery.sample([{ content: "notice-packet" }]);
  await delivery.complete({ role: "toolResult" });
  await delivery.complete({ role: "assistant", stopReason: "stop" });
  assert.deepEqual(acknowledgements, [["main", "fixture", 100]]);
});
