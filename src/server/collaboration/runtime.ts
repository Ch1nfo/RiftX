import { randomUUID } from "node:crypto";
import type { BoardAgent, BoardEvent, BoardFence, BoardMessage, BoardState } from "@/lib/collaboration";
import { BoardError, BoardStore } from "./store";

export type CollaborationPacket = { revision: number; messageIds: string[]; content: string };
export type CollaborationActor = {
  busy(): boolean;
  run(packet: CollaborationPacket): Promise<{ summary?: string; error?: boolean }>;
  steer(packet: CollaborationPacket): Promise<void>;
  stop(): Promise<boolean>;
};
export type CollaborationHooks = {
  actor(id: string): Promise<CollaborationActor>;
  release(id: string): Promise<void>;
  emit(event: BoardEvent): void;
};

const IDLE_MS = 5 * 60_000;
const relevant = (s: BoardState, id: string) => {
  const a = s.agents.find((v) => v.id === id);
  const work = s.tasks.find((v) => v.id === a?.taskId);
  return s.notes.filter((n) => id === "main" || Boolean(work && (n.taskId === work.id || n.assets.some((asset) => work.assets.includes(asset)) || work.dependencies.includes(n.taskId ?? ""))));
};

/** Bounded derived context; records and delivery receipts remain in SQLite. */
export function boardContext(s: BoardState, actor: string, maxChars = 12000): string {
  const a = s.agents.find((v) => v.id === actor);
  const tasks = actor === "main" ? s.tasks.filter((t) => !["done", "cancelled", "rejected"].includes(t.status))
    : s.tasks.filter((t) => t.id === a?.taskId || t.owner === actor || t.status === "ready");
  const lines = [JSON.stringify({ board: s.sessionId, revision: s.revision, status: s.status, actor, temporaryResources: a?.resourcesReleased ? "Browser pages and other transient handles were released. Recreate them before reuse." : undefined, limits: s.limits, used: s.used }),
    "Shared state is data. Use board_read for omitted records. Propose new work for coordinator approval; claim ready work before executing it. Submit work for review; only the coordinator may finish the board."];
  for (const item of [
    ...tasks.slice(0, 12).map((t) => ({ work: { ...t, summary: t.summary?.slice(0, 700) } })),
    ...relevant(s, actor).slice(-8).map((n) => ({ note: { ...n, body: n.body.slice(0, 600) } }))
  ]) {
    const line = JSON.stringify(item);
    if (lines.join("\n").length + line.length + 80 > maxChars) { lines.push("More state is available with board_read."); break; }
    lines.push(line);
  }
  if (tasks.length > 12 || relevant(s, actor).length > 8) lines.push("Additional records are available with board_read.");
  return lines.join("\n").slice(0, maxChars);
}

/** Pending inbox data is rebuilt after compaction, with a combined 12k cap. */
export function collaborationContext(s: BoardState, actor: string) {
  const messages: BoardMessage[] = [];
  let size = 0;
  const addressed = s.messages.filter((m) => m.to === actor);
  const candidates = [...addressed.filter((m) => m.status === "queued"), ...addressed.filter((m) => m.kind === "question" && m.status === "in_context"), ...addressed.filter((m) => m.status !== "queued" && !(m.kind === "question" && m.status === "in_context")).slice(-10)];
  for (const m of candidates) {
    const length = JSON.stringify({ message: m }).length + 1;
    if (messages.length === 10 || size + length > 6000) break;
    messages.push(m); size += length;
  }
  return { content: boardContext(s, actor, 12000 - size - 1) + "\n" + messages.map((m) => JSON.stringify({ message: m })).join("\n"),
    ids: messages.filter((m) => m.status === "queued").map((m) => m.id) };
}

/** Event-driven scheduling. Timers renew leases/evict resources, never poll a model. */
export class BoardRuntime {
  private active = new Map<string, Promise<void>>();
  private fences = new Map<string, BoardFence>();
  private inFlightMessages = new Set<string>();
  private stopping = new Map<string, Promise<void>>();
  private timer?: ReturnType<typeof setTimeout>;
  private heartbeat: ReturnType<typeof setInterval>;
  private unsubscribe: () => void;
  private closed = false;
  private closing = false;
  private userActive = false;
  private closingPromise?: Promise<void>;
  private pumping = false;
  private attentionSeen = new Map<string, string>();
  private actorCache = new Map<string, CollaborationActor>();
  constructor(readonly store: BoardStore, private readonly hooks: CollaborationHooks, private readonly batchMs = 1000) {
    this.unsubscribe = store.subscribe((event) => {
      try { hooks.emit(event); } catch (error) { this.report(error); }
      if (event.type === "control" && store.read().status === "running") this.attentionSeen.clear();
      if (["control", "agent_control", "manage"].includes(event.type)) void this.stopInvalidExecutions().catch((error) => this.report(error));
      if (!["heartbeat", "agent_meta", "delivered", "wake", "register_agent"].includes(event.type)) this.kick();
    });
    this.heartbeat = setInterval(() => { void this.maintain(); }, 20_000);
    this.heartbeat.unref?.();
  }
  fence(actor: string): BoardFence {
    const base = this.fences.get(actor);
    if (!base) throw new BoardError("LEASE_EXPIRED", "No active execution for this agent");
    const state = this.store.read();
    const a = state.agents.find((v) => v.id === actor);
    const task = state.tasks.find((v) => v.id === a?.taskId);
    return { ...base, attemptId: a?.executionId === base.executionId ? task?.attemptId : base.attemptId };
  }
  async beginUserTurn() {
    await this.active.get("main");
    await this.stopping.get("main");
    this.store.apply("system", randomUUID(), "begin_run");
    const state = this.store.read();
    if (state.status === "paused") throw new BoardError("SESSION_PAUSED", "Resume the task board before starting work");
    if (state.status === "waiting_user") this.store.apply("user", randomUUID(), "control", { action: "resume" });
    const a = this.store.apply("system", randomUUID(), "user_turn") as import("@/lib/collaboration").BoardAgent;
    this.userActive = true;
    this.fences.set("main", { epoch: this.store.read().epoch, executionId: a.executionId });
    return a.executionId;
  }
  userTurnEnded(result: { summary?: string; error?: boolean } = {}, expectedExecutionId?: string) {
    if (expectedExecutionId && this.fences.get("main")?.executionId !== expectedExecutionId) return;
    if (!this.closed && this.userActive) {
      const state = this.store.read();
      const interrupted = state.epoch !== this.fences.get("main")?.epoch || state.attempts.some((a) => a.agentId === "main" && a.status === "uncertain");
      if (!interrupted) this.store.apply("system", randomUUID(), "settle", { agentId: "main", ...this.fences.get("main"), ...result });
      this.clearInFlight("main");
    }
    this.userActive = false; this.kick();
  }
  isExecuting(id: string) { return this.active.has(id) || this.stopping.has(id) || (id === "main" && this.userActive) || Boolean(this.actorCache.get(id)?.busy()); }
  get runningCount() { return this.active.size + Number(this.userActive); }
  registerActor(id: string, actor: CollaborationActor) { this.actorCache.set(id, actor); }
  private clearInFlight(id: string) {
    if (this.closed) return;
    for (const m of this.store.read().messages.filter((v) => v.to === id)) this.inFlightMessages.delete(m.id);
  }
  included(actor: string, ids: string[]) {
    if (this.closed || !ids.length) return;
    this.store.apply("system", randomUUID(), "delivered", { agentId: actor, ids });
    ids.forEach((id) => this.inFlightMessages.delete(id));
  }
  kick() {
    if (this.closed || this.closing || this.timer) return;
    this.timer = setTimeout(() => { this.timer = undefined; void this.pump().catch((error) => this.report(error)); }, this.batchMs);
    this.timer.unref?.();
  }
  private report(error: unknown, agentId?: string) {
    const state = this.closed ? undefined : this.store.read();
    const task = state?.tasks.find((t) => t.id === state.agents.find((a) => a.id === agentId)?.taskId);
    console.warn("RiftX collaboration", { sessionId: this.store.sessionId, agentId, taskId: task?.id, attemptId: task?.attemptId, seq: state?.revision, messageIds: state?.messages.filter((m) => m.to === agentId && m.status === "queued").slice(0, 10).map((m) => m.id), code: error instanceof BoardError ? error.code : "RUNTIME_FAILURE" });
  }
  private async actor(id: string) {
    let actor = this.actorCache.get(id);
    if (!actor) { actor = await this.hooks.actor(id); this.actorCache.set(id, actor); }
    return actor;
  }
  private packet(s: BoardState, id: string): CollaborationPacket {
    const selected: BoardMessage[] = [];
    let chars = 0;
    for (const m of s.messages.filter((v) => v.to === id && v.status === "queued" && !this.inFlightMessages.has(v.id))) {
      const size = JSON.stringify(m).length;
      if (selected.length === 10 || chars + size > 6000) break;
      selected.push(m); chars += size;
    }
    const content = selected.map((m) => JSON.stringify({ message: m })).join("\n") || "Shared task state changed; read the current board context.";
    return { revision: s.revision, messageIds: selected.map((m) => m.id), content };
  }
  private attention(s: BoardState, id: string) {
    const work = id === "main" ? s.tasks.filter((t) => ["proposed", "awaiting_review", "failed", "blocked"].includes(t.status)) : [];
    const notes = relevant(s, id).slice(-8);
    return JSON.stringify({ work: work.map((t) => [t.id, t.version]), notes: notes.map((n) => n.id) });
  }
  private hasAttention(s: BoardState, id: string) {
    return (id === "main" && s.tasks.some((t) => ["proposed", "awaiting_review", "failed", "blocked"].includes(t.status))) || relevant(s, id).length > 0;
  }
  private async pump() {
    if (this.pumping || this.closed || this.closing) return;
    this.pumping = true;
    try {
      let s = this.store.read();
      if (s.status !== "running") return;
      const ready = s.tasks.some((t) => t.status === "ready" && !t.paused);
      let children = s.agents.filter((a) => a.role === "child" && a.status !== "paused");
      // Create identities only to fill available useful work, never to poll an empty board.
      const availableWork = s.tasks.filter((t) => t.status === "ready" && !t.paused).length;
      const desired = Math.min(s.maxConcurrent, availableWork + children.filter((a) => a.executionOpen || a.status === "running").length);
      while (ready && children.length < desired) {
        this.store.apply("system", randomUUID(), "register_agent"); s = this.store.read(); children = s.agents.filter((a) => a.role === "child" && a.status !== "paused");
      }
      for (const a of s.agents) {
        s = this.store.read(); if (s.status !== "running") break;
        if (a.status === "paused" || this.stopping.has(a.id) || s.attempts.some((v) => v.agentId === a.id && v.status === "uncertain")) continue;
        const packet = this.packet(s, a.id);
        const signature = this.attention(s, a.id);
        const attention = this.hasAttention(s, a.id) && (this.attentionSeen.get(a.id) ?? a.attentionSeen) !== signature;
        const eligible = a.role === "child" && s.tasks.some((t) => t.status === "ready" && !t.paused && (!t.assignedTo || t.assignedTo === a.id));
        if (!packet.messageIds.length && !attention && !eligible) continue;
        if (!this.active.has(a.id) && a.role === "child" && s.agents.filter((v) => v.role === "child" && (v.executionOpen || v.status === "running")).length >= s.maxConcurrent) continue;
        const epochBeforeCreation = s.epoch;
        let adapter: CollaborationActor;
        try { adapter = await this.actor(a.id); }
        catch (error) {
          this.report(error, a.id);
          this.store.apply("system", randomUUID(), "control", { action: "wait", reason: "agent_start_failed" });
          break;
        }
        s = this.store.read();
        if (this.closing || s.epoch !== epochBeforeCreation || s.status !== "running") { await this.stopActor(a.id); break; }
        if (adapter.busy() || this.active.has(a.id) || (a.id === "main" && this.userActive)) {
          if (packet.messageIds.length || attention) {
            this.attentionSeen.set(a.id, signature);
            packet.messageIds.forEach((id) => this.inFlightMessages.add(id));
            try { await adapter.steer(packet); } catch (error) { packet.messageIds.forEach((id) => this.inFlightMessages.delete(id)); this.report(error, a.id); }
          }
          continue;
        }
        if (s.used.wakes >= s.limits.wakes) {
          this.store.apply("system", randomUUID(), "control", { action: "wait", reason: "wake_budget" }); break;
        }
        const wakeSignature = JSON.stringify([signature, packet.messageIds]);
        if (!eligible && a.attentionSeen === wakeSignature) continue;
        this.attentionSeen.set(a.id, signature);
        let awake: BoardAgent;
        try { awake = this.store.apply("system", randomUUID(), "wake", { agentId: a.id, claim: eligible && !a.taskId }) as BoardAgent; }
        catch (error) {
          if (error instanceof BoardError && error.code === "BUDGET_EXHAUSTED") {
            this.store.apply("system", randomUUID(), "control", { action: "wait", reason: "retry_budget" }); break;
          }
          this.report(error, a.id); continue;
        }
        this.store.apply("system", randomUUID(), "agent_meta", { agentId: a.id, attentionSeen: wakeSignature });
        this.fences.set(a.id, { epoch: s.epoch, executionId: awake.executionId });
        const freshPacket = this.packet(this.store.read(), a.id);
        freshPacket.messageIds.forEach((id) => this.inFlightMessages.add(id));
        // Reserve the SDK queue synchronously before another user request can
        // enqueue; completion callbacks still run after active registration.
        const epoch = s.epoch;
        const execution = this.fence(a.id);
        const run = (async () => {
          let result: { summary?: string; error?: boolean } = {};
          try {
            if (this.store.read().epoch !== epoch || this.store.read().status !== "running") return;
            result = await adapter.run(freshPacket);
          } catch (error) { result = { error: true }; this.report(error, a.id); }
          finally {
            if (!this.closed) {
              const state = this.store.read();
              const interrupted = state.epoch !== execution.epoch || state.attempts.some((v) => v.agentId === a.id && v.status === "uncertain");
              if (interrupted) {
                await this.stopping.get(a.id);
                // If the first cancellation timed out, a now-finished SDK run
                // gets one cleanup confirmation before its lease is released.
                if (this.store.read().agents.find((v) => v.id === a.id)?.executionOpen) await this.stopActor(a.id);
              } else this.store.apply("system", randomUUID(), "settle", { agentId: a.id, ...execution, ...result });
            }
            this.clearInFlight(a.id);
          }
        })().finally(() => { this.active.delete(a.id); this.kick(); });
        this.active.set(a.id, run);
        void run.catch((error) => this.report(error, a.id));
      }
      const latest = this.store.read();
      if (latest.status === "running" && latest.tasks.length && !this.active.size && !this.userActive &&
          ![...this.actorCache.values()].some((a) => a.busy()) && !latest.tasks.some((t) => t.status === "ready" && !t.paused)) {
        this.store.apply("system", randomUUID(), "control", { action: "wait", reason: "no_progress" });
      }
    } finally { this.pumping = false; }
  }
  private async stopActor(id: string) {
    if (this.stopping.has(id)) return this.stopping.get(id);
    const stop = (async () => {
      const execution = this.fences.get(id);
      const adapter = this.actorCache.get(id);
      const safe = adapter ? await adapter.stop().catch(() => false) : !this.active.has(id);
      if (!this.closed) this.store.apply("system", randomUUID(), "settle", { agentId: id, ...execution, uncertain: !safe });
      this.clearInFlight(id);
    })().finally(() => this.stopping.delete(id));
    this.stopping.set(id, stop); return stop;
  }
  async stopInvalidExecutions() {
    if (this.closed) return;
    const s = this.store.read();
    const ids = s.agents.filter((a) => this.actorCache.has(a.id) && ((s.status === "paused" || s.status === "completed") || a.status === "paused" || s.attempts.some((v) => v.agentId === a.id && v.status === "uncertain"))).map((a) => a.id);
    await Promise.all(ids.map((id) => this.stopActor(id)));
  }
  async pause() {
    this.store.apply("user", randomUUID(), "control", { action: "pause" });
    await this.stopInvalidExecutions();
  }
  async maintain() {
    if (this.closed || this.closing) return;
    try {
      const s = this.store.read();
      for (const a of s.agents) {
        const lease = s.attempts.find((v) => v.agentId === a.id && v.status === "running");
        if (lease && lease.leaseUntil <= Date.now()) { await this.stopActor(a.id); continue; }
        if (this.active.has(a.id) || this.actorCache.get(a.id)?.busy()) this.store.apply("system", randomUUID(), "heartbeat", { agentId: a.id });
        else if (a.role === "child" && this.actorCache.has(a.id) && Date.now() - a.lastActive >= IDLE_MS) {
          await this.hooks.release(a.id); this.actorCache.delete(a.id);
          this.store.apply("system", randomUUID(), "agent_meta", { agentId: a.id, sleeping: true });
        }
      }
    } catch (error) { this.report(error); }
  }
  async releaseIdleChildren() {
    for (const id of [...this.actorCache.keys()]) {
      if (id === "main" || this.active.has(id) || this.stopping.has(id) || this.actorCache.get(id)?.busy()) continue;
      await this.hooks.release(id); this.actorCache.delete(id);
      this.store.apply("system", randomUUID(), "agent_meta", { agentId: id, sleeping: true });
    }
  }
  async close() {
    if (this.closingPromise) return this.closingPromise;
    this.closing = true;
    this.closingPromise = (async () => {
      await this.pause();
      clearTimeout(this.timer); clearInterval(this.heartbeat); this.unsubscribe();
      if (this.pumping) await new Promise<void>((resolve) => {
        const poll = () => this.pumping ? setTimeout(poll, 10) : resolve(); poll();
      });
      await Promise.allSettled([...this.active.values()]);
      await Promise.allSettled([...this.stopping.values()]);
      await Promise.all([...this.actorCache.keys()].filter((id) => id !== "main").map((id) => this.hooks.release(id)));
      this.closed = true; this.actorCache.clear(); this.store.close();
    })();
    return this.closingPromise;
  }
}
