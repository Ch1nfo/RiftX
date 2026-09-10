/**
 * TSec Benchmark platform API client, matched to the real platform contract:
 * - Auth header: `BENCHMARK_TOKEN: <token>` (NOT Bearer)
 * - VPN check: optional explicit BENCHMARK_VPN_URL health endpoint. The public
 *   Challenges API does not define a VPN-check route.
 * - Challenge list: GET /openapi/v1/challenges → direct JSON array
 * - Start/close/hint: ?unique_code=<code> query parameter
 * - Submit: POST /openapi/v1/challenges/submit with { unique_code, flag } body
 * Token is read from environment and never persisted or logged.
 */

export const BENCHMARK_QUERY_TIMEOUT_MS = 15_000;
export const BENCHMARK_MUTATION_TIMEOUT_MS = 30_000;
/** The operational spec mandates a VPN preflight before anything else; this
 * endpoint is only reachable from inside the lab VPN, which is exactly what
 * makes it a reliable probe. Set BENCHMARK_VPN_URL="" to disable. */
export const DEFAULT_BENCHMARK_VPN_URL = "http://10.0.100.58";
const ASYNC_STATE_POLL_INTERVAL_MS = 3_000;
const ASYNC_STATE_POLL_ROUNDS = 4;

export type Challenge = {
  unique_code: string;
  description: string;
  difficulty: string;
  level: number;
  total_score: number;
  flag_count: number;
  correct_flag_count: number;
  is_completed: boolean;
  container_status: string;
  container_addr: string[];
};

export type StartResult = {
  unique_code: string;
  container_addr: string[];
};

export type HintResult = {
  unique_code: string;
  hint: string | null;
};

export type SubmitResult = {
  unique_code?: string;
  correct: boolean;
  awarded: number;
  cumulative_score: number;
  correct_flag_count: number;
  total_flag_count: number;
  matched_flag_index: number | null;
};

export type CloseResult = {
  unique_code: string;
  closed: boolean;
};

export type VpnCheckResult = {
  status: string;
  client_ip: string;
  ok: boolean;
};

export type BenchmarkErrorKind =
  | "vpn_check_failed"
  | "challenge_not_found"
  | "invalid_state_max_active"
  | "invalid_state_task_ended"
  | "invalid_state"
  | "duplicate_submit"
  | "resource_unavailable"
  | "validation_error"
  | "not_found"
  | "internal_error"
  | "connection_error"
  | "timeout";

export class BenchmarkError extends Error {
  constructor(readonly kind: BenchmarkErrorKind, message: string, readonly status_code?: number) {
    super(message);
    this.name = "BenchmarkError";
  }
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function classifyError(status: number, body: Record<string, unknown> | null): BenchmarkErrorKind {
  const code = typeof body?.code === "string" ? body.code : typeof body?.error === "string" ? body.error : "";
  const message = typeof body?.message === "string" ? body.message : typeof body?.detail === "string" ? body.detail : "";
  if (code === "challenge_not_found") return "challenge_not_found";
  if (code === "duplicate") return "duplicate_submit";
  if (code === "resource_unavailable") return "resource_unavailable";
  if (code === "task_not_found") return "not_found";
  if (code === "internal_error") return "internal_error";
  if (code === "invalid_state") {
    if (message.includes("max active") || message.includes("最大") || message.includes("上限")) return "invalid_state_max_active";
    if (message.includes("ended") || message.includes("timeout") || message.includes("finished") || message.includes("结束")) return "invalid_state_task_ended";
    return "invalid_state";
  }
  if (status === 404) return "not_found";
  if (status === 409) {
    if (message.includes("max active") || message.includes("最大") || message.includes("上限")) return "invalid_state_max_active";
    return "invalid_state";
  }
  if (status === 422) return "validation_error";
  if (status === 503) return "resource_unavailable";
  if (status >= 500) return "internal_error";
  return "invalid_state";
}

async function parseBody(response: Response): Promise<unknown> {
  try {
    return await response.json() as unknown;
  } catch {
    return null;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function finiteNumber(value: unknown, field: string): number {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new BenchmarkError("validation_error", `Platform returned invalid ${field}`);
  return number;
}

function wait(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

export class BenchmarkController {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly vpnUrl: string;
  private readonly fetchImpl: FetchLike;

  constructor(options: { baseUrl?: string; token?: string; vpnUrl?: string; fetchImpl?: FetchLike } = {}) {
    this.baseUrl = (options.baseUrl ?? process.env.BENCHMARK_BASE_URL ?? "").replace(/\/$/, "");
    this.token = options.token ?? process.env.BENCHMARK_TOKEN ?? "";
    this.vpnUrl = (options.vpnUrl ?? process.env.BENCHMARK_VPN_URL ?? DEFAULT_BENCHMARK_VPN_URL).trim();
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
    if (!this.baseUrl || !this.token) {
      throw new BenchmarkError("validation_error", "BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set");
    }
  }

  private async request(path: string, init?: RequestInit, timeoutMs = BENCHMARK_QUERY_TIMEOUT_MS): Promise<Record<string, unknown>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          BENCHMARK_TOKEN: this.token,
          ...init?.headers
        },
        signal: controller.signal
      });
      const parsed = await parseBody(response);
      const body = asRecord(parsed);
      if (!response.ok) {
        throw new BenchmarkError(classifyError(response.status, body), typeof body?.message === "string" ? body.message : `HTTP ${response.status}`, response.status);
      }
      return body ?? {};
    } catch (error) {
      if (error instanceof BenchmarkError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw new BenchmarkError("timeout", `Request to ${path} timed out after ${timeoutMs}ms`);
      }
      throw new BenchmarkError("connection_error", error instanceof Error ? error.message : String(error));
    } finally {
      clearTimeout(timer);
    }
  }

  /** Optional VPN health check. The raw Challenges API defines no such route. */
  async checkVpn(): Promise<VpnCheckResult> {
    if (!this.vpnUrl) return { status: "unchecked", client_ip: "", ok: false };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), BENCHMARK_QUERY_TIMEOUT_MS);
    try {
      const response = await this.fetchImpl(this.vpnUrl, { signal: controller.signal });
      if (!response.ok) {
        throw new BenchmarkError("vpn_check_failed", `VPN endpoint returned HTTP ${response.status}`);
      }
      const body = asRecord(await parseBody(response));
      const status = String(body?.status ?? "");
      const clientIp = String(body?.client_ip ?? "");
      const ok = status === "ok";
      if (!ok) {
        throw new BenchmarkError("vpn_check_failed", `VPN check failed: status=${status || "no-status"} (client_ip=${clientIp || "unknown"})`);
      }
      return { status, client_ip: clientIp, ok };
    } catch (error) {
      if (error instanceof BenchmarkError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw new BenchmarkError("vpn_check_failed", "VPN check timed out — check VPN connection");
      }
      throw new BenchmarkError("vpn_check_failed", `Cannot reach VPN endpoint ${this.vpnUrl} — check VPN connection`);
    } finally {
      clearTimeout(timer);
    }
  }

  async listChallenges(): Promise<Challenge[]> {
    let lastError: unknown;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await this.listChallengesOnce();
      } catch (error) {
        lastError = error;
        const retryable = error instanceof BenchmarkError && (error.kind === "timeout" || error.kind === "connection_error" || error.kind === "internal_error");
        if (!retryable || attempt === 2) throw error;
        await wait(200 * (attempt + 1));
      }
    }
    throw lastError;
  }

  private async listChallengesOnce(): Promise<Challenge[]> {
    // Real platform returns a direct JSON array from /openapi/v1/challenges.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), BENCHMARK_QUERY_TIMEOUT_MS);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}/openapi/v1/challenges`, {
        headers: { BENCHMARK_TOKEN: this.token },
        signal: controller.signal
      });
      const parsed = await parseBody(response);
      const body = asRecord(parsed);
      if (!response.ok) {
        throw new BenchmarkError(classifyError(response.status, body), typeof body?.message === "string" ? body.message : `HTTP ${response.status}`, response.status);
      }
      // The real contract returns a bare array; also accept { challenges: [...] } for testability.
      const raw = Array.isArray(parsed) ? parsed : Array.isArray(body?.challenges) ? body.challenges as unknown[] : null;
      if (!raw) throw new BenchmarkError("validation_error", "Challenge list response is not an array");
      return raw.map((item) => {
        const record = item as Record<string, unknown>;
        const challenge = {
          unique_code: String(record.unique_code ?? ""),
          description: typeof record.description === "string" ? record.description.slice(0, 20_000) : "",
          difficulty: String(record.difficulty ?? "medium"),
          level: finiteNumber(record.level ?? 0, "level"),
          total_score: finiteNumber(record.total_score ?? 0, "total_score"),
          flag_count: finiteNumber(record.flag_count ?? 1, "flag_count"),
          correct_flag_count: finiteNumber(record.correct_flag_count ?? 0, "correct_flag_count"),
          is_completed: record.is_completed === true,
          container_status: String(record.container_status ?? "stopped"),
          container_addr: Array.isArray(record.container_addr) ? record.container_addr.map(String).filter(Boolean) : []
        };
        if (challenge.flag_count < 1 || challenge.correct_flag_count < 0 || challenge.correct_flag_count > challenge.flag_count) {
          throw new BenchmarkError("validation_error", `Platform returned invalid flag counts for ${challenge.unique_code || "unknown challenge"}`);
        }
        return challenge;
      }).filter((challenge) => challenge.unique_code);
    } catch (error) {
      if (error instanceof BenchmarkError) throw error;
      if (error instanceof Error && error.name === "AbortError") {
        throw new BenchmarkError("timeout", `listChallenges timed out after ${BENCHMARK_QUERY_TIMEOUT_MS}ms`);
      }
      throw new BenchmarkError("connection_error", error instanceof Error ? error.message : String(error));
    } finally {
      clearTimeout(timer);
    }
  }

  async startChallenge(uniqueCode: string): Promise<StartResult> {
    return this.startChallengeUnlocked(uniqueCode);
  }

  private async startChallengeUnlocked(uniqueCode: string): Promise<StartResult> {
    // Real contract uses query parameter: POST /openapi/v1/challenges/start?unique_code=...
    // A dropped response is ambiguous AND the platform start is asynchronous:
    // the container may still be `pending` and become `available` seconds
    // later. Treating pending as failure would release the local reservation
    // while the platform keeps the slot — every later start would hit the
    // max-active cap. So poll the authoritative list with a bounded budget.
    try {
      const body = await this.request(`/openapi/v1/challenges/start?unique_code=${encodeURIComponent(uniqueCode)}`, { method: "POST" }, BENCHMARK_MUTATION_TIMEOUT_MS);
      const result = {
        unique_code: String(body.unique_code ?? uniqueCode),
        container_addr: Array.isArray(body.container_addr) ? body.container_addr.map(String) : []
      };
      if (result.unique_code !== uniqueCode || result.container_addr.length === 0) {
        throw new BenchmarkError("resource_unavailable", `Platform did not return a usable container for ${uniqueCode}`);
      }
      return result;
    } catch (error) {
      if (error instanceof BenchmarkError && (
        error.kind === "timeout" || error.kind === "connection_error" || error.kind === "internal_error"
        || error.kind === "invalid_state" || error.kind === "invalid_state_max_active"
      )) {
        const original = error;
        const ambiguous = error.kind === "timeout" || error.kind === "connection_error" || error.kind === "internal_error";
        if (!ambiguous) {
          // An explicit platform rejection is not a dropped response. Adopt the
          // container only if a prior attempt already brought it up; otherwise
          // fail immediately — the caller must see the real reason.
          try {
            const match = (await this.listChallenges()).find((challenge) => challenge.unique_code === uniqueCode);
            if (match?.container_status === "available" && match.container_addr.length) {
              return { unique_code: uniqueCode, container_addr: match.container_addr };
            }
          } catch {
            // Preserve the original rejection.
          }
          throw original;
        }
        for (let round = 0; round < ASYNC_STATE_POLL_ROUNDS; round += 1) {
          await wait(ASYNC_STATE_POLL_INTERVAL_MS);
          let match;
          try {
            match = (await this.listChallenges()).find((challenge) => challenge.unique_code === uniqueCode);
          } catch {
            // A failed readback keeps polling; only the original failure is
            // reported if the platform stays unreachable.
            continue;
          }
          if (!match) throw original;
          if (match.container_status === "available" && match.container_addr.length) {
            return { unique_code: uniqueCode, container_addr: match.container_addr };
          }
          if (match.container_status === "stopped") throw original;
          // pending / stop_pending → the platform is still transitioning; keep polling.
        }
        // Still pending after the budget: the platform may still bring the
        // container up. Close it best-effort so it cannot leak a slot, then
        // surface a retryable failure.
        await this.closeChallengeUnlocked(uniqueCode).catch(() => undefined);
        throw new BenchmarkError("resource_unavailable", `Container for ${uniqueCode} was still pending after ${ASYNC_STATE_POLL_ROUNDS} reconciliation rounds; a cleanup close was attempted. Retry this challenge or pick another`);
      }
      throw error;
    }
  }

  async submitFlag(uniqueCode: string, flag: string): Promise<SubmitResult> {
    return this.submitFlagUnlocked(uniqueCode, flag);
  }

  private async submitFlagUnlocked(uniqueCode: string, flag: string): Promise<SubmitResult> {
    // Real contract: POST /openapi/v1/challenges/submit with { unique_code, flag } body.
    // The caller owns bounded retry policy because a dropped response is
    // ambiguous: it reconciles platform progress first and permits at most one
    // retry instead of allowing fetch-level blind replay loops.
    const body = await this.request(`/openapi/v1/challenges/submit`, {
      method: "POST",
      body: JSON.stringify({ unique_code: uniqueCode, flag })
    }, BENCHMARK_MUTATION_TIMEOUT_MS);
    if (typeof body.correct !== "boolean") throw new BenchmarkError("validation_error", `Platform submit response omitted correct for ${uniqueCode}`);
    const result = {
      unique_code: String(body.unique_code ?? uniqueCode),
      correct: body.correct,
      awarded: finiteNumber(body.awarded ?? 0, "awarded"),
      cumulative_score: finiteNumber(body.cumulative_score ?? 0, "cumulative_score"),
      correct_flag_count: finiteNumber(body.correct_flag_count, "correct_flag_count"),
      total_flag_count: finiteNumber(body.total_flag_count, "total_flag_count"),
      matched_flag_index: body.matched_flag_index === null || body.matched_flag_index === undefined ? null : finiteNumber(body.matched_flag_index, "matched_flag_index")
    };
    if (result.unique_code !== uniqueCode || result.total_flag_count < 1 || result.correct_flag_count < 0 || result.correct_flag_count > result.total_flag_count) {
      throw new BenchmarkError("validation_error", `Platform returned inconsistent submit progress for ${uniqueCode}`);
    }
    return result;
  }

  async getHint(uniqueCode: string): Promise<HintResult> {
    const body = await this.request(`/openapi/v1/challenges/hint?unique_code=${encodeURIComponent(uniqueCode)}`, undefined, BENCHMARK_QUERY_TIMEOUT_MS);
    return {
      unique_code: String(body.unique_code ?? uniqueCode),
      hint: typeof body.hint === "string" ? body.hint : null
    };
  }

  async closeChallenge(uniqueCode: string): Promise<CloseResult> {
    return this.closeChallengeUnlocked(uniqueCode);
  }

  private async closeChallengeUnlocked(uniqueCode: string): Promise<CloseResult> {
    // Closing is asynchronous too: the platform transitions through
    // stop_pending before stopped. A single readback that catches stop_pending
    // is NOT a failure — poll to stopped (bounded), and if the container is
    // still available after the mutation, send one more close before giving
    // up so the caller's ledger can record an honest close failure.
    const closeOnce = () => this.request(`/openapi/v1/challenges/close?unique_code=${encodeURIComponent(uniqueCode)}`, { method: "POST" }, BENCHMARK_MUTATION_TIMEOUT_MS);
    try {
      const body = await closeOnce();
      const closed = body.closed === true || body.status === "closed";
      if (!closed) {
        throw new BenchmarkError("invalid_state", `Platform returned closed:false for ${uniqueCode} — container is still running`);
      }
      return { unique_code: String(body.unique_code ?? uniqueCode), closed };
    } catch (error) {
      if (!(error instanceof BenchmarkError && (
        error.kind === "timeout" || error.kind === "connection_error" || error.kind === "internal_error"
      ))) throw error;
      let retriedClose = false;
      for (let round = 0; round < ASYNC_STATE_POLL_ROUNDS; round += 1) {
        await wait(ASYNC_STATE_POLL_INTERVAL_MS);
        let match;
        try {
          match = (await this.listChallenges()).find((challenge) => challenge.unique_code === uniqueCode);
        } catch {
          continue;
        }
        if (match?.container_status === "stopped") {
          return { unique_code: uniqueCode, closed: true };
        }
        if (match?.container_status === "available" && !retriedClose) {
          retriedClose = true;
          try {
            const body = await closeOnce();
            if (body.closed === true || body.status === "closed") {
              return { unique_code: uniqueCode, closed: true };
            }
          } catch {
            // Fall through to the next reconciliation round.
          }
        }
        // stop_pending / pending → still transitioning; keep polling.
      }
      throw new BenchmarkError("timeout", `Close for ${uniqueCode} was not confirmed stopped after ${ASYNC_STATE_POLL_ROUNDS} reconciliation rounds — the container may still occupy a platform slot; sync will reconcile`);
    }
  }
}
