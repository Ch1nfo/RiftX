import assert from "node:assert/strict";
import test from "node:test";
import type { ModelProfile, SessionSummary } from "./types";
import { withSessionProfile } from "./session-profile-sync";

const profile = (id: string, model: string): ModelProfile => ({
  id, name: id, provider: "prov", model, baseUrl: "https://x.test",
  api: "openai-completions", transport: "auto", contextWindow: 1000, maxTokens: 100, thinkingLevel: "off"
});

const summary = (id: string, profileId: string): SessionSummary => ({
  id, path: "", name: id, firstMessage: "", updatedAt: "2026-01-01T00:00:00.000Z", archived: false,
  profileId, provider: "prov", model: "m-old", contextWindow: 1000
});

test("withSessionProfile updates only the switched session's runtime fields", () => {
  const sessions = [summary("a", "p-old"), summary("b", "p-old")];
  const next = withSessionProfile(sessions, "a", profile("p-new", "m-new"));
  // The switched session carries the new profile so the reconnect effect's
  // setActiveProfileId(sessionMeta.profileId) keeps the fresh selection.
  assert.equal(next[0].profileId, "p-new");
  assert.equal(next[0].model, "m-new");
  // The untouched session and identity/order are preserved.
  assert.equal(next[1].profileId, "p-old");
  assert.deepEqual(sessions[0], summary("a", "p-old"));
});

test("withSessionProfile is a no-op for unknown sessions", () => {
  const sessions = [summary("a", "p-old")];
  assert.deepEqual(withSessionProfile(sessions, "missing", profile("p-new", "m-new")), sessions);
});
