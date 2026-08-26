import assert from "node:assert/strict";
import test from "node:test";
import { MASKED_API_KEY, resolveProfileApiKey } from "./profile-api-key";

test("a new explicit key is persisted", () => {
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: "sk-new" }, { apiKey: "sk-old" }), "sk-new");
});

test("a masked echo keeps the stored key", () => {
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: MASKED_API_KEY }, { apiKey: "sk-old" }), "sk-old");
});

test("a cleared field removes the stored key", () => {
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: "" }, { apiKey: "sk-old" }), "");
});

test("an absent field keeps the stored key", () => {
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: undefined }, { apiKey: "sk-old" }), "sk-old");
});

test("a keyless profile stays keyless", () => {
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: "" }), "");
  assert.equal(resolveProfileApiKey({ id: "p1", apiKey: MASKED_API_KEY }), "");
});
