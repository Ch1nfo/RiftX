import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { EvidenceStore } from "./evidence-store";

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
      evidence: [{ type: "tool", toolCallId: "tool-1", toolName: "browser" }]
    }, "subagent", "child-1");
    assert.equal(second.id, first.id);
    assert.equal((await store.list()).length, 1);
    assert.equal(second.confidence, "confirmed");
    assert.equal(second.source, "main");
    assert.equal(second.subagentId, "child-1");
    assert.equal(second.evidence.length, 2);
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
