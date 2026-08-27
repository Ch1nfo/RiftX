import assert from "node:assert/strict";
import test from "node:test";
import { createSerializer } from "./serializer";

test("runs operations in submission order", async () => {
  const serialize = createSerializer();
  const order: string[] = [];
  const slow = serialize(async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
    order.push("slow");
  });
  serialize(async () => { order.push("fast"); });
  await slow;
  await serialize(async () => { order.push("after"); });
  assert.deepEqual(order, ["slow", "fast", "after"]);
});

test("a rejected operation never poisons the chain", async () => {
  const serialize = createSerializer();
  await serialize(async () => { throw new Error("boom"); }).catch(() => undefined);
  assert.equal(await serialize(async () => "still works"), "still works");
});

test("results and rejections propagate to the caller", async () => {
  const serialize = createSerializer();
  assert.equal(await serialize(async () => 42), 42);
  await assert.rejects(serialize(async () => { throw new Error("propagated"); }), /propagated/);
});
