import Database from "better-sqlite3";
import { randomUUID, createHash } from "node:crypto";
import { mkdirSync, existsSync } from "node:fs";
import { dirname } from "node:path";
import { COLLABORATION_PROTOCOL, DEFAULT_BOARD_LIMITS, type BoardAgent, type BoardEvent, type BoardFence, type BoardLimits, type BoardOperation, type BoardReference, type BoardSnapshot, type BoardState, type BoardWork } from "@/lib/collaboration";

export class BoardError extends Error {
  constructor(readonly code: string, message: string, readonly status = 409, readonly current?: unknown) { super(message); }
}
const fail = (code: string, message: string, status = 409): never => { throw new BoardError(code, message, status); };
const terminal = new Set(["done", "cancelled", "rejected"]);
const isHuman = (actor: string) => actor === "user" || actor === "system";
function text(value: unknown, label: string, max = 2000): string {
  if (typeof value !== "string" || !value.trim() || value.length > max) return fail("INVALID_INPUT", `${label} must contain 1–${max} characters`, 400);
  return value.trim();
}
function strings(value: unknown): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 32 || value.some((v) => typeof v !== "string" || !v || v.length > 1000)) return fail("INVALID_INPUT", "Invalid string list", 400);
  return [...new Set(value as string[])];
}
function refs(value: unknown): BoardReference[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 32) return fail("INVALID_INPUT", "Invalid references", 400);
  return value.map((v) => {
    if (!v || !["finding", "request", "screenshot", "artifact", "tool"].includes(v.type)) return fail("INVALID_INPUT", "Invalid reference type", 400);
    return { type: v.type, id: text(v.id, "reference", 1000) };
  });
}
function number(value: unknown, label: string, min: number, max: number): number {
  if (!Number.isSafeInteger(value) || Number(value) < min || Number(value) > max) return fail("INVALID_INPUT", `${label} must be an integer between ${min} and ${max}`, 400);
  return Number(value);
}

/** One transaction contains the aggregate state, command receipt and ordered event.
 * JSON keeps schema evolution local; SQLite provides cross-connection claim isolation. */
export class BoardStore {
  private readonly db: Database.Database;
  private listeners = new Set<(event: BoardEvent) => void>();
  private closed = false;
  constructor(readonly path: string, readonly sessionId: string, options: { create?: boolean; maxConcurrent?: number; recover?: boolean; now?: () => number } = {}) {
    this.now = options.now ?? Date.now;
    if (!options.create && !existsSync(path)) throw new BoardError("BOARD_MISSING", "This session's collaboration database is missing", 503);
    if (options.create && existsSync(path)) throw new BoardError("BOARD_EXISTS", "Collaboration database already exists");
    if (options.create) mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    try { this.db = new Database(path, { fileMustExist: !options.create }); }
    catch { throw new BoardError("BOARD_UNAVAILABLE", "Collaboration database could not be opened; execution is paused", 503); }
    try {
      this.db.pragma("foreign_keys = ON");
      this.db.pragma("busy_timeout = 5000");
      this.db.pragma("journal_mode = WAL");
      this.db.pragma("synchronous = FULL");
      if (options.create) {
        this.db.exec(`CREATE TABLE board (id TEXT PRIMARY KEY, state TEXT NOT NULL);
          CREATE TABLE events (seq INTEGER PRIMARY KEY, board_id TEXT NOT NULL REFERENCES board(id), data TEXT NOT NULL);
          CREATE TABLE commands (id TEXT PRIMARY KEY, board_id TEXT NOT NULL REFERENCES board(id), digest TEXT NOT NULL, result TEXT NOT NULL);
          PRAGMA user_version = 1;`);
        const state: BoardState = { protocol: 1, sessionId, revision: 0, runId: randomUUID(), epoch: 1, status: "running",
          limits: { ...DEFAULT_BOARD_LIMITS }, used: { tasks: 0, wakes: 0, messages: 0 }, maxConcurrent: options.maxConcurrent ?? 3,
          agents: [{ id: "main", role: "main", name: "Main Agent", status: "idle", lastActive: this.now(), lastWakeRevision: 0, pendingApprovalCount: 0 }],
          tasks: [], attempts: [], messages: [], notes: [], findingVersions: {} };
        this.db.prepare("INSERT INTO board VALUES (?, ?)").run(sessionId, JSON.stringify(state));
      }
      if (this.db.pragma("user_version", { simple: true }) !== COLLABORATION_PROTOCOL || this.db.pragma("quick_check", { simple: true }) !== "ok") throw new Error("Unsupported or corrupt board database");
      this.read();
      if (options.recover) this.apply("system", randomUUID(), "control", { action: "recover" });
    } catch {
      this.db.close();
      throw new BoardError("BOARD_UNAVAILABLE", "Collaboration database cannot be read or migrated; execution is paused", 503);
    }
  }
  private readonly now: () => number;
  read(): BoardState {
    if (this.closed) return fail("BOARD_CLOSED", "Collaboration database is closed", 503);
    const row = this.db.prepare("SELECT state FROM board WHERE id = ?").get(this.sessionId) as { state: string } | undefined;
    if (!row) return fail("BOARD_INVALID", "Collaboration session identity mismatch", 503);
    const state = JSON.parse(row.state) as BoardState;
    if (state.sessionId !== this.sessionId || state.protocol !== 1 || !Array.isArray(state.agents) || !Array.isArray(state.tasks)) return fail("BOARD_INVALID", "Invalid collaboration state", 503);
    return state;
  }
  snapshot(after = 0, limit = 100): BoardSnapshot {
    return this.db.transaction(() => {
      const state = this.read();
      const rows = this.db.prepare("SELECT data FROM events WHERE seq > ? ORDER BY seq LIMIT ?").all(after, Math.min(500, Math.max(1, limit))) as { data: string }[];
      const events = rows.map((row) => JSON.parse(row.data) as BoardEvent);
      const nextCursor = events.at(-1)?.seq ?? after;
      return { mode: "shared" as const, state, events, nextCursor, hasMore: nextCursor < state.revision };
    })();
  }
  subscribe(listener: (event: BoardEvent) => void) { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; }
  close() { if (!this.closed) { this.closed = true; this.listeners.clear(); this.db.close(); } }
  assertFence(state: BoardState, actor: string, fence?: BoardFence) {
    if (isHuman(actor)) return;
    if (!fence || fence.epoch !== state.epoch) return fail("LEASE_EXPIRED", "This execution generation is no longer current");
    const agent = state.agents.find((a) => a.id === actor);
    if (!agent) return fail("OUT_OF_SCOPE", "Unknown agent", 403);
    if (fence.executionId && fence.executionId !== agent.executionId) return fail("LEASE_EXPIRED", "This agent execution is no longer current");
    if (agent.status === "paused" || state.status === "paused" || state.status === "completed") return fail("SESSION_PAUSED", "Collaboration is paused or complete");
    if (fence.attemptId) {
      const attempt = state.attempts.find((a) => a.id === fence.attemptId);
      if (!attempt || attempt.agentId !== actor || attempt.epoch !== fence.epoch || attempt.status !== "running" || attempt.leaseUntil <= this.now()) return fail("LEASE_EXPIRED", "This work lease is no longer valid");
    }
  }
  apply(actor: string, key: string, operation: BoardOperation, input: Record<string, unknown> = {}, fence?: BoardFence): unknown {
    text(key, "command ID", 300);
    const digest = createHash("sha256").update(JSON.stringify({ operation, input })).digest("hex");
    let event: BoardEvent | undefined;
    const result = this.db.transaction(() => {
      const id = `${actor}:${key}`;
      const previous = this.db.prepare("SELECT digest, result FROM commands WHERE id = ?").get(id) as { digest: string; result: string } | undefined;
      if (previous) {
        if (previous.digest !== digest) return fail("VERSION_CONFLICT", "Command ID already used for different input");
        return JSON.parse(previous.result);
      }
      const state = this.read();
      this.assertFence(state, actor, fence);
      const result = this.reduce(state, actor, operation, input, fence);
      const silent = operation === "heartbeat" || (operation === "agent_meta" && input.attentionSeen !== undefined);
      if (!silent) {
        state.revision += 1;
        const entity = result && typeof result === "object" ? result as { id?: string; version?: number } : undefined;
        event = { seq: state.revision, type: operation, actor, entityId: entity?.id ?? (typeof input.taskId === "string" ? input.taskId : undefined), entityVersion: entity?.version, createdAt: this.now() };
        this.db.prepare("INSERT INTO events VALUES (?, ?, ?)").run(event.seq, this.sessionId, JSON.stringify(event));
      }
      this.db.prepare("UPDATE board SET state = ? WHERE id = ?").run(JSON.stringify(state), this.sessionId);
      // Heartbeats have no externally retryable side effect and do not grow receipts.
      if (operation !== "heartbeat" && operation !== "agent_meta") this.db.prepare("INSERT INTO commands VALUES (?, ?, ?, ?)").run(id, this.sessionId, digest, JSON.stringify(result ?? null));
      return JSON.parse(JSON.stringify(result ?? null));
    }).immediate();
    if (event) for (const listener of this.listeners) { try { listener(event); } catch { /* committed events replay from SQLite */ } }
    return result;
  }
  private reduce(s: BoardState, actor: string, op: BoardOperation, p: Record<string, unknown>, fence?: BoardFence): unknown {
    const now = this.now();
    const main = () => { if (actor !== "main" && !isHuman(actor)) fail("OUT_OF_SCOPE", "Only the coordinator can manage work", 403); };
    const system = () => { if (actor !== "system") fail("OUT_OF_SCOPE", "Runtime operation", 403); };
    const task = () => { const t = s.tasks.find((v) => v.id === p.taskId); if (!t) return fail("OUT_OF_SCOPE", "Unknown work item", 404); return t; };
    const agent = (id = String(p.agentId)) => { const a = s.agents.find((v) => v.id === id); if (!a) return fail("OUT_OF_SCOPE", "Unknown agent", 404); return a; };
    const revision = (t: BoardWork) => { if (p.version !== t.version) throw new BoardError("VERSION_CONFLICT", "Work item changed; read it again", 409, t); };
    const bump = (t: BoardWork) => { t.version++; t.updatedAt = now; };
    const checkReady = (t: BoardWork) => {
      if (t.paused) return false;
      return t.dependencies.every((id) => s.tasks.find((v) => v.id === id)?.status === "done");
    };
    const validateDependencies = (id: string, deps: string[]) => {
      const visit = (next: string, seen: Set<string>): boolean => {
        if (next === id) return true;
        if (seen.has(next)) return false;
        seen.add(next);
        return s.tasks.find((v) => v.id === next)?.dependencies.some((d) => visit(d, seen)) ?? false;
      };
      if (deps.some((d) => !s.tasks.some((v) => v.id === d) || visit(d, new Set()))) fail("INVALID_INPUT", "Dependencies must exist in this board and must not form a cycle", 400);
    };
    const capacity = (name: "tasks" | "wakes" | "messages") => {
      if (s.used[name] >= s.limits[name]) fail("BUDGET_EXHAUSTED", `${name} budget exhausted; increase the current run limit`);
    };
    const running = () => { if (s.status !== "running") fail("SESSION_PAUSED", "Resume collaboration before scheduling work"); };
    const retire = (t: BoardWork, reason: string) => {
      const a = s.attempts.find((v) => v.id === t.attemptId);
      if (a && a.status === "running") { a.status = "uncertain"; a.reason = reason; }
    };
    if (op === "begin_run") {
      system();
      if (s.status === "completed") { s.runId = randomUUID(); s.epoch++; s.used = { tasks: 0, wakes: 0, messages: 0 }; s.status = "running"; s.reason = undefined; }
      return { status: s.status };
    }
    if (op === "create" || op === "propose") {
      if (op === "create") main();
      running();
      if (op === "create") capacity("tasks");
      if (op === "propose" && s.tasks.filter((t) => t.status === "proposed").length >= 32) fail("BUDGET_EXHAUSTED", "Too many pending proposals");
      const objective = text(p.objective, "objective");
      const duplicate = s.tasks.find((t) => !terminal.has(t.status) && t.objective.toLowerCase() === objective.toLowerCase());
      if (duplicate) return duplicate;
      const id = randomUUID(); const dependencies = strings(p.dependencies); validateDependencies(id, dependencies);
      const t: BoardWork = { id, objective, acceptance: text(p.acceptance ?? objective, "acceptance"), priority: number(p.priority ?? 0, "priority", 0, 10),
        dependencies, assets: strings(p.assets), status: op === "propose" ? "proposed" : "ready", version: 1, createdAt: now, updatedAt: now,
        author: actor, references: [], attempts: 0, retries: 0, admitted: op === "create", admittedRun: op === "create" ? s.runId : undefined };
      if (op === "create") { s.used.tasks++; if (!checkReady(t)) { t.status = "blocked"; t.blockedReason = "dependencies"; } }
      s.tasks.push(t); return t;
    }
    if (op === "manage") {
      main(); const t = task(); revision(t);
      if (actor === "user" && p.confirmStopped === true && ["retry", "cancel"].includes(String(p.action))) {
        for (const attempt of s.attempts.filter((a) => a.taskId === t.id && a.status === "uncertain")) {
          attempt.status = "settled"; attempt.finishedAt = now; attempt.reason = "user_confirmed_stopped";
          const previousOwner = s.agents.find((a) => a.id === attempt.agentId);
          if (previousOwner?.taskId === t.id) previousOwner.taskId = undefined;
          if (previousOwner) previousOwner.executionOpen = false;
          if (previousOwner?.status === "running") previousOwner.status = "idle";
        }
      }
      switch (p.action) {
        case "accept":
          if (t.status !== "proposed") return fail("STATE_NOT_ALLOWED", "Only proposals can be accepted");
          capacity("tasks"); s.used.tasks++; t.admitted = true; t.admittedRun = s.runId;
          t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; break;
        case "reject":
          if (t.status !== "proposed") return fail("STATE_NOT_ALLOWED", "Only proposals can be rejected");
          t.status = "rejected"; break;
        case "edit":
          if (s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) return fail("STATE_NOT_ALLOWED", "Wait for old execution to settle before editing");
          if (terminal.has(t.status) || t.status === "running" || t.status === "awaiting_review") return fail("STATE_NOT_ALLOWED", "Pause active work before editing");
          if (p.objective !== undefined) t.objective = text(p.objective, "objective");
          if (p.acceptance !== undefined) t.acceptance = text(p.acceptance, "acceptance");
          if (p.priority !== undefined) t.priority = number(p.priority, "priority", 0, 10);
          if (p.dependencies !== undefined) { const deps = strings(p.dependencies); validateDependencies(t.id, deps); t.dependencies = deps; }
          if (p.assets !== undefined) t.assets = strings(p.assets);
          if (t.admitted && !t.paused) { t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; }
          break;
        case "approve":
          if (t.status !== "awaiting_review" || s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) return fail("STATE_NOT_ALLOWED", "Wait for execution to settle before accepting results");
          t.status = "done";
          for (const other of s.tasks) if (other.status === "blocked" && other.blockedReason === "dependencies" && checkReady(other)) { other.status = "ready"; other.blockedReason = undefined; bump(other); }
          break;
        case "retry":
        case "return":
          if (!["failed", "blocked", "awaiting_review"].includes(t.status) || s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) return fail("STATE_NOT_ALLOWED", "Old execution must settle before retry");
          if (p.action === "return") text(p.reason, "review rejection reason");
          if ((t.retries ?? 0) >= s.limits.retries) return fail("BUDGET_EXHAUSTED", "Work retry limit reached");
          if (t.retryPending) return fail("STATE_NOT_ALLOWED", "A retry is already pending");
          t.retryPending = true;
          t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; t.owner = undefined; t.attemptId = undefined;
          if (p.reason) t.summary = text(p.reason, "reason"); break;
        case "pause":
          if (terminal.has(t.status)) return fail("STATE_NOT_ALLOWED", "Work already ended");
          t.paused = true; retire(t, "paused"); t.status = "blocked"; t.blockedReason = "paused"; break;
        case "resume":
          if (!t.paused || s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) return fail("STATE_NOT_ALLOWED", "Work is not safely paused");
          t.paused = false; t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; break;
        case "cancel":
          if (terminal.has(t.status)) return t;
          retire(t, "cancelled"); t.status = "cancelled"; t.paused = false; break;
        default: return fail("INVALID_INPUT", "Unknown work action", 400);
      }
      bump(t); return t;
    }
    if (op === "claim") {
      running(); const a = agent(actor);
      if (a.taskId) { const existing = s.tasks.find((v) => v.id === a.taskId); if (existing?.status === "running") return existing; return fail("STATE_NOT_ALLOWED", "Previous work must settle before another claim"); }
      const t = p.taskId ? task() : s.tasks.filter((v) => v.status === "ready" && checkReady(v) && (!v.assignedTo || v.assignedTo === actor)).sort((a, b) => b.priority - a.priority || a.createdAt - b.createdAt)[0];
      if (!t) return null;
      if (t.status !== "ready" || !checkReady(t) || (t.assignedTo && t.assignedTo !== actor) || s.attempts.some((v) => v.taskId === t.id && v.status !== "settled")) return fail("STATE_NOT_ALLOWED", "Work is unavailable");
      if (a.role === "child" && a.status !== "running") return fail("STATE_NOT_ALLOWED", "Runtime must reserve capacity before claiming");
      if (t.retryPending) {
        if ((t.retries ?? 0) >= s.limits.retries) return fail("BUDGET_EXHAUSTED", "Work retry limit reached");
        t.retries = (t.retries ?? 0) + 1; t.retryPending = false;
      }
      const attemptId = randomUUID(); s.attempts.push({ id: attemptId, taskId: t.id, agentId: actor, epoch: s.epoch, startedAt: now, leaseUntil: now + 120_000, status: "running" });
      t.status = "running"; t.attemptId = attemptId; t.owner = actor; t.attempts++; t.blockedReason = undefined; a.taskId = t.id; bump(t); return t;
    }
    if (op === "update") {
      const t = task(); revision(t);
      if (t.owner !== actor || t.status !== "running" || !fence?.attemptId || fence.attemptId !== t.attemptId) return fail("LEASE_EXPIRED", "Only the current execution can update work");
      if (p.action === "submit") { t.status = "awaiting_review"; t.summary = text(p.summary, "summary"); t.references = refs(p.references); }
      else if (p.action === "block") { t.status = "blocked"; t.blockedReason = text(p.reason, "blocked reason"); }
      else if (p.action === "progress") t.summary = text(p.summary, "progress");
      else return fail("INVALID_INPUT", "Unknown progress action", 400);
      bump(t); return t;
    }
    if (op === "message") {
      const to = text(p.to, "recipient", 100); agent(to);
      if (to === actor || (!isHuman(actor) && actor !== "main" && to !== "main")) return fail("OUT_OF_SCOPE", "Only parent/child messages are allowed", 403);
      if (p.kind === "task_update") main();
      if (!["information", "question", "answer", "task_update"].includes(String(p.kind))) return fail("INVALID_INPUT", "Invalid message kind", 400);
      if (p.taskId) task();
      if (!isHuman(actor)) capacity("messages");
      const body = text(p.body, "message");
      const reply = p.replyTo ? s.messages.find((m) => m.id === p.replyTo) : undefined;
      if (p.replyTo && (!reply || reply.to !== actor || reply.from !== to)) return fail("OUT_OF_SCOPE", "Reply must match a received message", 403);
      const message = { id: randomUUID(), from: actor, to, kind: p.kind as "information" | "question" | "answer" | "task_update", body, taskId: p.taskId as string | undefined, replyTo: p.replyTo as string | undefined, createdAt: now, status: "queued" as const };
      s.messages.push(message); if (!isHuman(actor)) s.used.messages++;
      if (reply) {
        reply.status = "replied";
        const t = s.tasks.find((v) => v.id === reply.taskId);
        if (t?.status === "blocked" && !t.paused && t.blockedReason === `question:${reply.id}` && checkReady(t) && !s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) {
          t.status = "ready"; t.assignedTo = reply.from; t.blockedReason = undefined; bump(t);
        }
      }
      return message;
    }
    if (op === "publish") {
      if (!["observation", "hypothesis", "ruled_out", "conflict"].includes(String(p.kind))) return fail("INVALID_INPUT", "Invalid note kind", 400);
      if (p.taskId) task(); capacity("messages");
      const references = refs(p.references); if (!references.length) return fail("INVALID_INPUT", "Public updates require source references", 400);
      const note = { id: randomUUID(), author: actor, kind: p.kind as "observation", body: text(p.body, "note"), taskId: p.taskId as string | undefined, assets: strings(p.assets), references, createdAt: now };
      s.notes.push(note); s.used.messages++; return note;
    }
    if (op === "finish") {
      main(); if (s.tasks.some((t) => !terminal.has(t.status)) || s.attempts.some((a) => a.status !== "settled") || s.messages.some((m) => m.status === "queued")) return fail("STATE_NOT_ALLOWED", "Resolve pending work and messages before finishing");
      s.status = "completed"; s.reason = undefined; return { status: s.status };
    }
    if (op === "control") {
      if (!isHuman(actor)) return fail("OUT_OF_SCOPE", "Only the user can control execution", 403);
      if (p.action === "pause" || p.action === "recover") {
        if (p.action === "recover") system();
        s.stopping = p.action === "recover" ? [] : s.agents.filter((a) => a.status === "running").map((a) => a.id);
        s.status = s.status === "completed" ? "completed" : "paused"; s.epoch++; s.reason = s.status === "completed" ? undefined : p.action === "recover" ? "restart" : s.stopping.length ? "stopping" : "user";
        for (const t of s.tasks) if (s.attempts.some((a) => a.taskId === t.id && a.status === "running")) { retire(t, String(p.action)); if (t.status === "running") { t.status = "blocked"; t.blockedReason = p.action === "recover" ? "execution_uncertain" : "paused"; bump(t); } }
        for (const a of s.agents) {
          if (a.status === "running") a.status = "idle";
          if (p.action === "recover") a.executionOpen = s.attempts.some((attempt) => attempt.agentId === a.id && attempt.status === "uncertain");
        }
      } else if (p.action === "resume") {
        if (s.status === "completed") return fail("STATE_NOT_ALLOWED", "Start a new user task after completion");
        s.status = "running"; s.reason = undefined;
        for (const a of s.agents) a.attentionSeen = undefined;
        for (const t of s.tasks) if (t.status === "blocked" && t.blockedReason === "paused" && !t.paused && !s.attempts.some((a) => a.taskId === t.id && a.status !== "settled")) { t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; bump(t); }
      } else if (p.action === "limits") {
        for (const k of Object.keys(s.limits) as (keyof BoardLimits)[]) if (p[k] !== undefined) s.limits[k] = number(p[k], k, k === "retries" ? 0 : 1, 10000);
      } else if (p.action === "wait") { system(); s.status = "waiting_user"; s.reason = typeof p.reason === "string" ? p.reason : "attention"; }
      else return fail("INVALID_INPUT", "Invalid board action", 400);
      return { status: s.status, limits: s.limits };
    }
    if (op === "agent_control") {
      if (!isHuman(actor)) return fail("OUT_OF_SCOPE", "Only the user controls agents", 403);
      const a = agent(); if (a.role === "main") return fail("STATE_NOT_ALLOWED", "Use the board controls for the main agent");
      if (p.action === "pause") { a.status = "paused"; const t = s.tasks.find((v) => v.id === a.taskId); if (t) { retire(t, "agent_paused"); t.status = "blocked"; t.blockedReason = "paused"; t.paused = true; bump(t); } }
      else if (p.action === "resume") {
        a.status = "idle";
        for (const t of s.tasks.filter((v) => v.owner === a.id && v.paused)) {
          if (s.attempts.some((v) => v.taskId === t.id && v.status !== "settled")) continue;
          t.paused = false; t.status = checkReady(t) ? "ready" : "blocked"; t.blockedReason = t.status === "blocked" ? "dependencies" : undefined; bump(t);
        }
      }
      else return fail("INVALID_INPUT", "Invalid agent action", 400);
      return a;
    }
    system();
    if (op === "register_agent") {
      const a: BoardAgent = { id: randomUUID(), role: "child", name: `Agent ${s.agents.length}`, status: "idle", lastActive: now, lastWakeRevision: 0, pendingApprovalCount: 0 };
      s.agents.push(a); return a;
    }
    if (op === "agent_meta") {
      const a = agent(); if (typeof p.attentionSeen === "string") a.attentionSeen = p.attentionSeen;
      if (typeof p.transcript === "string") a.transcript = p.transcript;
      if (typeof p.model === "string") a.model = p.model;
      if (p.sleeping && a.status === "idle") { a.status = "sleeping"; a.resourcesReleased = true; }
      if (p.recreated) a.resourcesReleased = true;
      if (p.maxConcurrent !== undefined) s.maxConcurrent = number(p.maxConcurrent, "concurrency", 1, 8);
      if (p.pendingApprovalCount !== undefined) a.pendingApprovalCount = number(p.pendingApprovalCount, "approvals", 0, 1000);
      return a;
    }
    if (op === "user_turn") {
      running(); const a = agent("main");
      if (a.executionOpen) return fail("STATE_NOT_ALLOWED", "Previous main execution has not finished cleanup");
      a.status = "running"; a.executionOpen = true; a.executionId = randomUUID(); a.lastActive = now; return a;
    }
    if (op === "wake") {
      running(); const a = agent(); capacity("wakes");
      if (a.executionOpen || a.status === "running" || a.status === "paused") return fail("STATE_NOT_ALLOWED", "Agent is already active or paused");
      if (a.role === "child" && s.agents.filter((v) => v.role === "child" && (v.executionOpen || v.status === "running")).length >= s.maxConcurrent) return fail("STATE_NOT_ALLOWED", "Agent concurrency is full");
      a.status = "running"; a.executionOpen = true; a.executionId = randomUUID(); a.lastActive = now; a.lastWakeRevision = s.revision; s.used.wakes++;
      if (p.claim === true && !this.reduce(s, a.id, "claim", {}, { epoch: s.epoch, executionId: a.executionId })) return fail("STATE_NOT_ALLOWED", "No eligible work remains");
      return a;
    }
    if (op === "settle") {
      const a = agent();
      if (p.executionId && p.executionId !== a.executionId) return a;
      if (p.attemptId && p.attemptId !== s.tasks.find((t) => t.id === a.taskId)?.attemptId) return a;
      a.executionOpen = Boolean(p.uncertain);
      if (!p.uncertain) s.stopping = (s.stopping ?? []).filter((id) => id !== a.id);
      if (s.reason === "stopping" && !s.stopping?.length) s.reason = "user";
      if (a.status !== "paused") a.status = "idle"; a.lastActive = now;
      const t = s.tasks.find((v) => v.id === a.taskId);
      if (t) {
        const attempt = s.attempts.find((v) => v.id === t.attemptId);
        if (attempt?.status === "settled") return a;
        if (attempt) { attempt.status = p.uncertain ? "uncertain" : "settled"; attempt.finishedAt = now; }
        if (t.status === "running") {
          t.status = p.error || !p.summary ? "failed" : "awaiting_review";
          t.summary = typeof p.summary === "string" ? p.summary.slice(0, 2000) : undefined;
          t.blockedReason = p.error ? "execution_failed" : undefined; bump(t);
        }
        if (p.uncertain && !terminal.has(t.status)) { t.status = "blocked"; t.blockedReason = "execution_uncertain"; bump(t); }
        if (!p.uncertain && t.status === "blocked" && t.blockedReason?.startsWith("question:") && !t.paused && checkReady(t)) {
          const question = s.messages.find((m) => m.id === t.blockedReason!.slice(9));
          if (question?.status === "replied") { t.status = "ready"; t.assignedTo = a.id; t.blockedReason = undefined; bump(t); }
        }
        if (!p.uncertain) a.taskId = undefined;
      }
      return a;
    }
    if (op === "heartbeat") {
      const a = agent(); const attempt = s.attempts.find((v) => v.agentId === a.id && v.status === "running");
      if (attempt && attempt.epoch === s.epoch && attempt.leaseUntil > now) attempt.leaseUntil = now + 120_000;
      return null;
    }
    if (op === "delivered") {
      const a = agent(); for (const id of strings(p.ids)) { const m = s.messages.find((v) => v.id === id && v.to === a.id); if (m?.status === "queued") m.status = "in_context"; }
      return null;
    }
    if (op === "finding") {
      const id = text(p.id, "finding ID", 100); const version = text(p.updatedAt, "finding version", 100);
      if (s.findingVersions[id] === version) return null;
      s.findingVersions[id] = version;
      s.notes.push({ id: randomUUID(), kind: "finding", author: "system", body: text(p.title, "finding title"), assets: strings(p.assets), references: [{ type: "finding", id }], createdAt: now });
      return null;
    }
    return fail("INVALID_INPUT", "Unknown collaboration operation", 400);
  }
}
