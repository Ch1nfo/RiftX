import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import type { BrowserHandoffState } from "@/browser";
import type { BenchmarkController, Challenge } from "./controller";
import { reconcileExpiredHandoffs } from "./handoff";
import { BenchmarkLedger } from "./ledger";

const realHome = process.env.HOME;
const realToken = process.env.BENCHMARK_TOKEN;
let tempDir: string;

test.before(async () => {
  tempDir = await mkdtemp(join(tmpdir(), "riftx-handoff-"));
  process.env.HOME = tempDir;
});

test.after(async () => {
  process.env.HOME = realHome;
  if (realToken === undefined) delete process.env.BENCHMARK_TOKEN;
  else process.env.BENCHMARK_TOKEN = realToken;
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
});

function challenge(): Challenge {
  return {
    unique_code: "ch-1", description: "multi-stage", difficulty: "hard", level: 3,
    total_score: 300, flag_count: 2, correct_flag_count: 0, is_completed: false,
    container_status: "stopped", container_addr: []
  };
}

async function recoveryLedger(now: () => number) {
  const sessionId = `handoff-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const ledger = await new BenchmarkLedger(sessionId, now).initialize();
  await ledger.syncFromPlatform([challenge()], true, "ip");
  await ledger.acquire("ch-1", "main", ["old"]);
  await ledger.recordSubmission("ch-1", "flag{one}", true, 100, 1, 0, "main");
  await ledger.defer("ch-1", "first pass complete", "try a new family", "main");
  await ledger.confirmClosed("ch-1");
  await ledger.maybeAdvancePhase();
  await ledger.acquire("ch-1", "subagent:old", ["live:8080"]);
  return { ledger, sessionId };
}

test("expired warm handoff is closed and released by reconciliation", async () => {
  let now = 1_000_000;
  const { ledger } = await recoveryLedger(() => now);
  await ledger.defer("ch-1", "rotate worker", "source audit", "subagent:old", { preserveContainer: true });
  now += 2 * 60 * 1000;
  let closeCalls = 0;
  const controller = {
    closeChallenge: async (uniqueCode: string) => {
      closeCalls += 1;
      return { unique_code: uniqueCode, closed: true };
    }
  } as unknown as BenchmarkController;

  await reconcileExpiredHandoffs(controller, ledger);

  assert.equal(closeCalls, 1);
  assert.equal(ledger.getChallenge("ch-1")?.status, "deferred");
  assert.equal(ledger.getChallenge("ch-1")?.containerStatus, "stopped");
  assert.equal(ledger.getState().activeContainers, 0);
});

test("browser authentication handoff is local, bounded, and benchmark-token redacted", async () => {
  const now = 2_000_000;
  const { ledger, sessionId } = await recoveryLedger(() => now);
  process.env.BENCHMARK_TOKEN = "benchmark-secret";
  const browserState: BrowserHandoffState = {
    version: 1,
    activeIdentity: "admin",
    identities: [{
      id: "admin",
      storageState: {
        cookies: [
          { name: "session", value: "benchmark-secret", domain: "target", path: "/" },
          { name: "progress", value: "flag{real_secret_flag_value}", domain: "target", path: "/" }
        ],
        origins: [{ origin: "http://target", localStorage: [{ name: "note", value: "flag{another_real_flag}" }] }]
      }
    }]
  };
  await ledger.saveBrowserHandoffState("ch-1", browserState, "subagent:old");
  await ledger.defer("ch-1", "rotate worker", "continue authenticated flow", "subagent:old", { preserveContainer: true });

  const persisted = await readFile(join(tempDir, ".riftx", "benchmark", sessionId, "state.json"), "utf8");
  assert.ok(!persisted.includes("benchmark-secret"));
  assert.ok(!persisted.includes("real_secret_flag_value"), "flag-shaped cookie values must be redacted");
  assert.ok(!persisted.includes("another_real_flag"), "flag-shaped storage values must be redacted");
  assert.equal(ledger.getChallenge("ch-1")?.browserHandoffState?.activeIdentity, "admin");
  const cookies = ledger.getChallenge("ch-1")?.browserHandoffState?.identities[0]?.storageState.cookies as Array<{ name: string; value: string }>;
  assert.equal(cookies.find((cookie) => cookie.name === "progress")?.value, "flag{REDACTED}", "redaction keeps the flag shape so the cookie stays structurally valid");

  await ledger.reserve("ch-1", "subagent:new", { isSubagent: true });
  assert.equal(ledger.getChallenge("ch-1")?.browserHandoffState?.activeIdentity, "admin");
});
