import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

type MockChallenge = {
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
  flags: string[];
  /** Per-challenge cumulative score, as the real submit response reports (该题累计总得分). */
  obtainedScore: number;
};

type SubmittedFlag = { code: string; flag: string };

/**
 * In-process mock of the REAL TSec benchmark API contract:
 * - Auth: BENCHMARK_TOKEN header
 * - List: GET /openapi/v1/challenges → bare JSON array
 * - Start: POST /openapi/v1/challenges/start?unique_code=...
 * - Submit: POST /openapi/v1/challenges/submit with { unique_code, flag } body
 * - Hint: GET /openapi/v1/challenges/hint?unique_code=...
 * - Close: POST /openapi/v1/challenges/close?unique_code=...
 */
export class MockBenchmarkApi {
  private server?: Server;
  port = 0;
  private challenges: Map<string, MockChallenge> = new Map();
  private submittedFlags: SubmittedFlag[] = [];
  private activeContainers = 0;
  private maxActive = 3;
  private hintCalls: string[] = [];
  private closeCalls: string[] = [];
  private startCalls: string[] = [];
  cumulativeScore = 0;
  vpnOk = true;
  taskEnded = false;

  seedChallenges(count: number) {
    for (let index = 0; index < count; index++) {
      const code = `ch-${String(index + 1).padStart(3, "0")}`;
      const difficulties = ["easy", "medium", "hard"];
      this.challenges.set(code, {
        unique_code: code,
        description: `Mock challenge ${index + 1}: find the flag. Difficulty: ${difficulties[index % 3]}.`,
        difficulty: difficulties[index % 3],
        level: (index % 3) + 1,
        total_score: 100 + (index % 3) * 100,
        flag_count: 1 + (index % 3),
        correct_flag_count: 0,
        is_completed: false,
        container_status: "stopped",
        container_addr: [],
        flags: Array.from({ length: 1 + (index % 3) }, (_, flagIndex) => `flag{mock_${code}_${flagIndex}}`),
        obtainedScore: 0
      });
    }
  }

  getChallengeCount() { return this.challenges.size; }
  getSubmittedFlags() { return [...this.submittedFlags]; }
  getHintCalls() { return [...this.hintCalls]; }
  getCloseCalls() { return [...this.closeCalls]; }
  getStartCalls() { return [...this.startCalls]; }
  getActiveContainers() { return this.activeContainers; }
  getSolvedCount() { return [...this.challenges.values()].filter((challenge) => challenge.is_completed).length; }

  async start(): Promise<number> {
    const server = createServer((request, response) => {
      const url = new URL(request.url ?? "/", `http://127.0.0.1`);
      const method = request.method ?? "GET";
      const path = url.pathname;
      const uniqueCode = url.searchParams.get("unique_code") ?? "";
      const json = (status: number, body: unknown) => {
        response.writeHead(status, { "Content-Type": "application/json", Connection: "close" });
        response.end(JSON.stringify(body));
      };
      // Root path = VPN connectivity check — no auth required (connectivity only).
      if (path === "/" || path === "") {
        json(200, { status: "ok" });
        return;
      }
      // Real contract uses BENCHMARK_TOKEN header, not Bearer.
      const token = request.headers.benchmark_token;
      if (!token) {
        json(401, { error: "unauthorized", message: "missing BENCHMARK_TOKEN header" });
        return;
      }
      if (this.taskEnded) {
        json(409, { code: "invalid_state", message: "task has ended" });
        return;
      }
      if (method === "GET" && path === "/openapi/v1/challenges") {
        // Real contract: bare JSON array.
        json(200, [...this.challenges.values()]);
        return;
      }
      if (method === "POST" && path === "/openapi/v1/challenges/start") {
        const challenge = this.challenges.get(uniqueCode);
        if (!challenge) { json(404, { code: "challenge_not_found", message: `no ${uniqueCode}` }); return; }
        if (this.activeContainers >= this.maxActive) {
          json(409, { code: "invalid_state", message: "max active challenges reached" });
          return;
        }
        if (challenge.container_status === "available") {
          json(200, { unique_code: uniqueCode, container_addr: challenge.container_addr });
          return;
        }
        this.activeContainers += 1;
        this.startCalls.push(uniqueCode);
        challenge.container_status = "available";
        challenge.container_addr = [`10.0.${Math.floor(Math.random() * 255)}.${Math.floor(Math.random() * 255)}:8080`];
        json(200, { unique_code: uniqueCode, container_addr: challenge.container_addr });
        return;
      }
      if (method === "POST" && path === "/openapi/v1/challenges/submit") {
        let body = "";
        request.on("data", (chunk) => { body += String(chunk); });
        request.on("end", () => {
          const parsed = JSON.parse(body || "{}") as { unique_code?: string; flag?: string };
          const code = parsed.unique_code ?? "";
          const flag = parsed.flag ?? "";
          const challenge = this.challenges.get(code);
          if (!challenge) { json(404, { code: "challenge_not_found", message: `no ${code}` }); return; }
          if (!flag) { json(422, { code: "validation_error", message: "flag required" }); return; }
          const isDuplicate = this.submittedFlags.some((entry) => entry.code === code && entry.flag === flag && challenge.flags.includes(flag));
          if (isDuplicate) {
            json(409, { code: "duplicate", message: "already submitted" });
            return;
          }
          this.submittedFlags.push({ code, flag });
          const flagIndex = challenge.flags.indexOf(flag);
          const correct = flagIndex >= 0;
          if (correct) {
            challenge.correct_flag_count += 1;
            const awarded = Math.floor(challenge.total_score / challenge.flag_count);
            this.cumulativeScore += awarded;
            challenge.obtainedScore += awarded;
            if (challenge.correct_flag_count >= challenge.flag_count) {
              challenge.is_completed = true;
              challenge.container_status = "stopped";
              challenge.container_addr = [];
              this.activeContainers = Math.max(0, this.activeContainers - 1);
            }
          }
          json(200, {
            unique_code: code, correct, awarded: correct ? Math.floor(challenge.total_score / challenge.flag_count) : 0,
            // Real contract: cumulative_score is the CHALLENGE's total, not the run total.
            cumulative_score: challenge.obtainedScore,
            correct_flag_count: challenge.correct_flag_count,
            total_flag_count: challenge.flag_count,
            matched_flag_index: correct ? flagIndex : null
          });
        });
        return;
      }
      if (method === "GET" && path === "/openapi/v1/challenges/hint") {
        this.hintCalls.push(uniqueCode);
        json(200, { unique_code: uniqueCode, hint: `Look at endpoint /api/${uniqueCode}/secret` });
        return;
      }
      if (method === "POST" && path === "/openapi/v1/challenges/close") {
        this.closeCalls.push(uniqueCode);
        const challenge = this.challenges.get(uniqueCode);
        if (challenge && challenge.container_status !== "stopped") {
          challenge.container_status = "stopped";
          challenge.container_addr = [];
          this.activeContainers = Math.max(0, this.activeContainers - 1);
        }
        json(200, { unique_code: uniqueCode, closed: true });
        return;
      }
      json(404, { error: "not found", message: `${method} ${path}` });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    this.server = server;
    this.port = (server.address() as AddressInfo).port;
    return this.port;
  }

  close() {
    this.server?.close();
    this.server?.closeAllConnections?.();
  }
}
