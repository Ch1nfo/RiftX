import { createHash } from "node:crypto";
import { evidenceBackedRuleOuts, handoffCandidate } from "./blackboard";
import type { BenchmarkLedger, ChallengeState } from "./ledger";

export const MAX_BENCHMARK_MEMORY_PAGE_CHARS = 12_000;
export type BenchmarkMemoryKind = "overview" | "blackboard" | "attempts";

/** This locator is mandatory; the surrounding preview is deliberately partial. */
export function benchmarkMemoryLocator(uniqueCode?: string) {
  return {
    memory: {
      source: "canonical_ledger", previewOnly: true,
      tool: "benchmark_control", arguments: { action: "read_memory", ...(uniqueCode ? { uniqueCode } : {}), cursor: 0 }
    }
  };
}

type MemoryRecord = { kind: string; [key: string]: unknown };

function textRecords(kind: string, text: string): MemoryRecord[] {
  const records: MemoryRecord[] = [];
  for (let offset = 0; offset < text.length;) {
    let end = Math.min(text.length, offset + 1_500);
    if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
    records.push({ kind, part: records.length, text: text.slice(offset, end) });
    offset = end;
  }
  return records;
}

function challengeRecords(challenge: ChallengeState): MemoryRecord[] {
  const candidate = handoffCandidate(challenge.currentApproach, challenge.nextProbe);
  return [
    {
      kind: "challenge", uniqueCode: challenge.uniqueCode, difficulty: challenge.difficulty, level: challenge.level,
      status: challenge.status, owner: challenge.owner, containerStatus: challenge.containerStatus, containerAddrs: challenge.containerAddrs,
      totalScore: challenge.totalScore, scoreObtained: challenge.scoreObtained, scoreKnown: challenge.scoreKnown,
      correctFlagCount: challenge.correctFlagCount, flagCount: challenge.flagCount, isCompleted: challenge.isCompleted,
      attemptCount: challenge.attemptCount, currentAttemptStartedAt: challenge.currentAttemptStartedAt,
      currentAttemptWorker: challenge.currentAttemptWorker, currentAttemptPhase: challenge.currentAttemptPhase,
      flagsAtAttemptStart: challenge.flagsAtAttemptStart, hardDeadlineAt: challenge.hardDeadlineAt,
      attemptExtensionGrantedAt: challenge.attemptExtensionGrantedAt,
      triedFamilies: challenge.triedFamilies, ruledOutFamilies: evidenceBackedRuleOuts(challenge),
      passwordEnumerationMs: challenge.passwordEnumerationMs, hintUsed: challenge.hintUsed
    },
    ...textRecords("description", challenge.description),
    ...textRecords("hint", challenge.hintContent ?? ""),
    ...(candidate ? [{ kind: "candidate", source: "checkpoint", ...candidate }] : []),
    ...challenge.blackboard.map((entry) => ({ kind: "blackboard", entry })),
    ...(challenge.supersededBlackboard ?? []).map((record) => ({
      kind: "historical_superseded", effective: false, ...record
    })),
    ...challenge.approachHistory.map((attempt) => ({
      kind: "attempt", historical: true, attempt, flagsDelta: attempt.flagsAfter - attempt.flagsBefore,
      previousCandidate: handoffCandidate(attempt.approach, attempt.nextDistinctApproach)
    }))
  ];
}

function encodeCursor(version: string, offset: number): string {
  return Buffer.from(JSON.stringify({ version, offset })).toString("base64url");
}

function cursorOffset(cursor: string | number | undefined, version: string): number {
  if (cursor === undefined || cursor === 0) return 0;
  if (typeof cursor !== "string" || cursor.length > 512) throw new Error("Use the nextCursor returned by read_memory.");
  let decoded: { version?: unknown; offset?: unknown };
  try { decoded = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8")); }
  catch { throw new Error("Invalid memory cursor."); }
  if (!decoded || decoded.version !== version) throw new Error("Memory changed since this page cursor was issued; restart read_memory with cursor 0.");
  if (!Number.isSafeInteger(decoded.offset) || Number(decoded.offset) < 0) throw new Error("Invalid memory cursor offset.");
  return Number(decoded.offset);
}

function pageRecords(records: MemoryRecord[]): MemoryRecord[] {
  return records.flatMap((record, recordIndex) => {
    const serialized = JSON.stringify(record);
    if (serialized.length <= 8_000) return [record];
    const fragments = textRecords("record_chunk", serialized);
    return fragments.map((fragment, part) => ({
      kind: "record_chunk", originalKind: record.kind, recordIndex, part, parts: fragments.length,
      jsonFragment: fragment.text
    }));
  });
}

/** Read committed, complete records; neither history nor artifact references are clipped. */
export async function readBenchmarkMemory(
  ledger: BenchmarkLedger, uniqueCode?: string, cursor?: string | number, kind?: BenchmarkMemoryKind
) {
  const challenges = await ledger.memorySnapshot(uniqueCode);
  const allRecords: MemoryRecord[] = uniqueCode ? [
    ...challengeRecords(challenges[0]),
    ...ledger.allIntelForChallenge(challenges[0]).map((entry) => ({ kind: "shared_intel", ...entry }))
  ] : [
    ...challenges.sort((left, right) => left.uniqueCode.localeCompare(right.uniqueCode)).map((challenge) => ({
      kind: "challenge_index", uniqueCode: challenge.uniqueCode, status: challenge.status, owner: challenge.owner,
      correctFlagCount: challenge.correctFlagCount, flagCount: challenge.flagCount, isCompleted: challenge.isCompleted,
      blackboardEntries: challenge.blackboard.length, attempts: challenge.approachHistory.length
    })),
    ...ledger.getState().sharedIntel.map((entry) => ({ kind: "shared_intel", ...entry }))
  ];
  const selected = !kind || !uniqueCode ? allRecords : allRecords.filter((record) =>
    kind === "blackboard" ? record.kind === "blackboard" || record.kind === "historical_superseded"
      : kind === "attempts" ? record.kind === "attempt"
        : record.kind !== "blackboard" && record.kind !== "historical_superseded" && record.kind !== "attempt"
  );
  const records = pageRecords(selected);
  const version = createHash("sha256").update(JSON.stringify({ uniqueCode: uniqueCode ?? null, kind: kind ?? null, records: allRecords })).digest("hex");
  const offset = cursorOffset(cursor, version);
  if (offset > records.length) throw new Error("Memory cursor is beyond the end of this snapshot.");
  const page = {
    source: "canonical_ledger", uniqueCode: uniqueCode ?? null, kind: kind ?? null,
    version, offset, total: records.length, totalRecords: selected.length, nextCursor: null as string | null, items: [] as MemoryRecord[]
  };
  for (let index = offset; index < records.length; index++) {
    const nextCursor = index + 1 < records.length ? encodeCursor(version, index + 1) : null;
    const candidate = { ...page, items: [...page.items, records[index]], nextCursor };
    if (JSON.stringify(candidate).length > MAX_BENCHMARK_MEMORY_PAGE_CHARS) {
      if (!page.items.length) throw new Error(`Memory record ${index} exceeds the ${MAX_BENCHMARK_MEMORY_PAGE_CHARS}-character page budget; it was not skipped or truncated.`);
      break;
    }
    page.items.push(records[index]);
    page.nextCursor = nextCursor;
  }
  return page;
}
