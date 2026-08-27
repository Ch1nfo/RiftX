import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, stat, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { EvidenceStore, getEvidenceStore, removeEvidence } from "./evidence-store";
import type { FindingInput } from "@/lib/types";

test("deduplicates findings by normalized asset and title and merges evidence", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    const events: unknown[] = [];
    const store = new EvidenceStore("parent", root, (event) => events.push(event));
    const first = await store.upsert({
      title: "  Reflected XSS  ",
      asset: "https://target.test/search ",
      confidence: "suspected",
      impact: "Initial signal",
      reproduction: "Submit marker",
      evidence: [{ type: "quote", quote: "marker" }]
    }, "main");
    const second = await store.upsert({
      title: "reflected   xss",
      asset: "https://target.test/search",
      confidence: "confirmed",
      impact: "Script executes",
      reproduction: "Submit the marker and observe execution",
      evidence: [{ type: "tool", toolCallId: "tool-1", toolName: "browser", content: "HTTP 200" }]
    }, "subagent", "child-1");
    assert.equal(second.id, first.id);
    assert.equal((await store.list()).length, 1);
    assert.equal(second.confidence, "confirmed");
    assert.equal(second.source, "main");
    assert.equal(second.subagentId, "child-1");
    assert.equal(second.evidence.length, 2);
    assert.equal((second.evidence.find((item) => item.type === "tool") as { content?: string }).content, "HTTP 200");
    assert.equal(events.length, 2);
    const saved = JSON.parse(await readFile(join(root, "parent", "findings.json"), "utf8")) as { findings: unknown[] };
    assert.equal(saved.findings.length, 1);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("drops legacy request detail fields when loading and persisting findings", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    const store = new EvidenceStore("parent", root);
    const finding = await store.upsert({
      title: "Request issue",
      asset: "host.test",
      confidence: "likely",
      impact: "Impact",
      reproduction: "Steps",
      evidence: [{ type: "request", requestRef: "r1", method: "GET", url: "/api", status: 200 }]
    }, "main");
    const file = join(root, "parent", "findings.json");
    const dirty = JSON.parse(await readFile(file, "utf8")) as { findings?: Array<{ evidence?: Array<Record<string, unknown>> }> };
    if (dirty.findings?.[0].evidence?.[0]) dirty.findings[0].evidence[0].requestDetail = { headers: { cookie: "secret" }, body: "private" };
    await writeFile(file, `${JSON.stringify(dirty, null, 2)}\n`);
    const reopened = new EvidenceStore("parent", root);
    const findings = await reopened.list();
    assert.equal(findings[0].evidence[0].type, "request");
    assert.equal((findings[0].evidence[0] as { method?: string }).method, "GET");
    assert.equal((findings[0].evidence[0] as { requestDetail?: unknown }).requestDetail, undefined);
    const cleanAfterLoad = JSON.parse(await readFile(file, "utf8")) as { findings?: Array<{ evidence?: Array<Record<string, unknown>> }> };
    assert.equal(cleanAfterLoad.findings?.[0].evidence?.[0].requestDetail, undefined);
    await reopened.patch(finding.id, { confidence: "confirmed" });
    const clean = JSON.parse(await readFile(file, "utf8")) as { findings?: Array<{ evidence?: Array<Record<string, unknown>> }> };
    assert.equal(clean.findings?.[0].evidence?.[0].requestDetail, undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("patches only confidence and dismissed status and removes the sidecar", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    const store = new EvidenceStore("parent", root);
    const finding = await store.upsert({
      title: "Issue",
      asset: "host.test",
      confidence: "likely",
      impact: "Impact",
      reproduction: "Steps",
      evidence: [{ type: "quote", quote: "evidence" }]
    }, "main");
    const dismissed = await store.patch(finding.id, { confidence: "confirmed", status: "dismissed" });
    assert.equal(dismissed?.confidence, "confirmed");
    assert.equal(dismissed?.status, "dismissed");
    assert.equal((await store.list())[0].status, "dismissed");
    await store.remove();
    await assert.rejects(stat(join(root, "parent")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writes queued behind a permanent removal never resurrect the store", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  const input: FindingInput = {
    title: "Resurrection probe",
    asset: "https://target.test/probe",
    confidence: "confirmed",
    impact: "probe",
    reproduction: "probe",
    evidence: [{ type: "quote", quote: "probe" }]
  };
  try {
    const store = new EvidenceStore("parent", root);
    await store.upsert(input, "main");
    // Queue the removal, then queue a late write behind it on the SAME
    // instance — exactly the delete-vs-concurrent-finding race.
    const removal = store.remove();
    const lateWrite = store.upsert(input, "main").catch((error: Error) => error);
    await removal;
    const outcome = await lateWrite;
    assert.match(outcome instanceof Error ? outcome.message : String(outcome), /permanently deleted/);
    assert.deepEqual(await store.list(), []);
    // The directory must not have been recreated by the queued write.
    await assert.rejects(stat(join(root, "parent", "findings.json")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a fresh store cannot be obtained for a permanently deleted session", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  const input: FindingInput = {
    title: "Tombstone probe",
    asset: "https://target.test/tombstone",
    confidence: "confirmed",
    impact: "probe",
    reproduction: "probe",
    evidence: [{ type: "quote", quote: "probe" }]
  };
  try {
    const store = getEvidenceStore("tombstone-session", root);
    await store.upsert(input, "main");
    await removeEvidence("tombstone-session", root);
    // The exact resurrection order from review: deletion completes, the cache
    // entry is gone, and a late request asks for a NEW instance — it must be
    // refused, not handed a fresh writable store.
    assert.throws(() => getEvidenceStore("tombstone-session", root), /permanently deleted/);
    await assert.rejects(stat(join(root, "tombstone-session", "findings.json")));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a failed removal rolls back the tombstone so the store stays obtainable", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    // A regular file where the session directory's parent should be makes the
    // recursive rm fail deterministically (ENOTDIR) on every platform.
    const blocker = join(root, "not-a-dir");
    await writeFile(blocker, "x");
    await assert.rejects(removeEvidence("rollback-session", blocker));
    // The tombstone must have been rolled back: a fresh store is obtainable
    // again, instead of the process refusing the id forever while the
    // archived-delete request itself failed.
    const store = getEvidenceStore("rollback-session", blocker);
    assert.equal(typeof store.upsert, "function");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("a concurrent deletion failure never rolls back another deletion's success", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    const blocker = join(root, "not-a-dir");
    await writeFile(blocker, "x");
    // Evidence exists on disk but NO store instance is cached: a cached
    // instance would route both deletions through its own root and silently
    // ignore the blocker, turning the failure branch into a second success.
    // Bare-rm paths take each call's own root, so the blocker genuinely fails.
    await mkdir(join(root, "concurrent-session"), { recursive: true });
    await writeFile(join(root, "concurrent-session", "findings.json"), '{"findings":[]}');
    const results = await Promise.allSettled([
      removeEvidence("concurrent-session", blocker),
      removeEvidence("concurrent-session", root)
    ]);
    assert.equal(results[0].status, "rejected", "the blocker-root deletion must actually fail");
    assert.equal(results[1].status, "fulfilled", "the real-root deletion must succeed");
    // One deletion succeeded: terminal, regardless of the concurrent failure.
    assert.throws(() => getEvidenceStore("concurrent-session", root), /permanently deleted/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("all-failing concurrent deletions lift the guard together", async () => {
  const root = await mkdtemp(join(tmpdir(), "riftx-evidence-"));
  try {
    const blocker = join(root, "not-a-dir");
    await writeFile(blocker, "x");
    const results = await Promise.allSettled([removeEvidence("both-fail", blocker), removeEvidence("both-fail", blocker)]);
    assert.equal(results.every((item) => item.status === "rejected"), true);
    // Neither deletion succeeded, so the session must be operable again —
    // the guard lifts only when the last in-flight deletion has finished.
    const store = getEvidenceStore("both-fail", root);
    assert.equal(typeof store.upsert, "function");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
