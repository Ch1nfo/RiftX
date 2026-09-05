import { createServer, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

function delay(ms: number) {
  return new Promise<void>((resolve) => setTimeout(resolve, ms));
}

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; };

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settler) => { resolve = settler; });
  return { promise, resolve };
}

const SESSION_ID = "e2e";
const SECOND_SESSION_ID = "e2e-second";
const TALL_TEXT = `${"Streaming content that grows the conversation by well over the follow threshold in a single frame. ".repeat(4)}\n\n`;

/** 210 seeds exceed the 200-message window, so every append slides the window. */
function seedMessages(count: number) {
  const seeded = Array.from({ length: count }, (_, index) => ({
    id: `seed-${index}`,
    role: index % 2 === 0 ? "user" : "assistant",
    content: `Seed message ${index}.\n\n${"Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore. ".repeat(10)}`
  }));
  // A settled browser-screenshot tool card plus a user image exercise the
  // inline-image rendering and the lightbox without a live browser runtime.
  seeded.push(
    { id: "seed-tool-screenshot", role: "tool", toolName: "browser", toolCallId: "seed-tool-screenshot", content: "Screenshot captured: s-e2e", status: "done", screenshotId: "s-e2e-00000000-0000-4000-8000-000000000000" },
    { id: "seed-user-image", role: "user", content: "check this picture", images: [{ src: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", mimeType: "image/png" }] }
  );
  return seeded;
}

/**
 * In-process stand-in for the RiftX API: serves the bootstrap/session
 * endpoints the workbench needs and a scripted SSE timeline whose phase A
 * reproduces the exact conditions that used to kill auto-follow — tall
 * per-frame streamed growth, thinking blocks auto-collapsing when assistant
 * text starts, tool cards flipping status, and the message window sliding on
 * every append in a long conversation.
 */
export class MockApi {
  private readonly phaseA = deferred<void>();
  private readonly phaseCRelease = deferred<void>();
  private readonly finished = deferred<void>();
  private server?: Server;
  private port = 0;
  private secondSessionRunning = false;
  private secondSessionMessages: Array<{ id: string; role: "assistant"; content: string }> = [];
  private secondStream?: ServerResponse;

  readonly phaseADone = this.phaseA.promise;
  readonly allDone = this.finished.promise;

  /** Lets the SSE timeline continue with the post-re-engagement rounds. */
  releasePhaseC() {
    this.phaseCRelease.resolve();
  }

  finishSecondSession() {
    this.secondSessionRunning = false;
  }

  /** Completes a turn without sending text_delta, reproducing an SSE gap. */
  completeSecondSessionReply(content: string) {
    this.secondSessionRunning = false;
    this.secondSessionMessages = [{ id: "persisted-reply", role: "assistant", content }];
    this.secondStream?.write(`data: ${JSON.stringify({ type: "message", turnEnd: true, message: { role: "assistant", content } })}\n\n`);
    this.secondStream?.write(`data: ${JSON.stringify({ type: "done" })}\n\n`);
  }

  async start(): Promise<number> {
    const server = createServer((request, response) => {
      const url = request.url ?? "";
      if (url === `/api/sessions/${SESSION_ID}/stream`) {
        response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
        response.write(`data: ${JSON.stringify({ type: "connected", sessionId: SESSION_ID })}\n\n`);
        const closed = new Promise<void>((resolve) => response.on("close", resolve));
        void this.runTimeline(response, closed).catch(() => undefined);
        return;
      }
      if (url === `/api/sessions/${SECOND_SESSION_ID}/stream`) {
        response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
        response.write(`data: ${JSON.stringify({ type: "connected", sessionId: SECOND_SESSION_ID })}\n\n`);
        this.secondStream = response;
        response.on("close", () => {
          if (this.secondStream === response) this.secondStream = undefined;
        });
        return;
      }
      const json = (body: unknown) => {
        response.writeHead(200, { "content-type": "application/json" });
        response.end(JSON.stringify(body));
      };
      if (url === "/api/bootstrap") {
        json({
          sessions: [
            { id: SESSION_ID, name: "E2E scroll regression", updatedAt: new Date().toISOString(), firstMessage: "seed" },
            { id: SECOND_SESSION_ID, name: "E2E second session", updatedAt: new Date().toISOString(), firstMessage: "older seed" }
          ],
          activeSessionId: SESSION_ID,
          cwd: "/tmp/riftx-e2e",
          profiles: [],
          activeProfileId: "",
          approvalMode: "request"
        });
        return;
      }
      if (url === "/api/sessions/status") {
        json({ runningSessionIds: this.secondSessionRunning ? [SECOND_SESSION_ID] : [] });
        return;
      }
      if (url === `/api/sessions/${SECOND_SESSION_ID}/prompt` && request.method === "POST") {
        let body = "";
        request.on("data", (chunk) => { body += String(chunk); });
        request.on("end", () => {
          const parsed = JSON.parse(body || "{}") as { text?: string };
          this.secondSessionRunning = true;
          this.secondSessionMessages = [];
          json({ ok: true, sessionId: SECOND_SESSION_ID, composedText: parsed.text ?? "", requestState: "accepted" });
        });
        return;
      }
      if (url === `/api/sessions/${SESSION_ID}/messages`) {
        json({ messages: seedMessages(210), promptRequestStates: {}, failedRequestIds: [] });
        return;
      }
      if (url === `/api/sessions/${SECOND_SESSION_ID}/messages`) {
        json({ messages: this.secondSessionMessages, promptRequestStates: {}, failedRequestIds: [] });
        return;
      }
      if (url === `/api/sessions/${SESSION_ID}` || url === `/api/sessions/${SECOND_SESSION_ID}`) json({});
      else if (url.startsWith(`/api/sessions/${SESSION_ID}/findings/screenshot/`)) {
        // 1x1 PNG: enough for <img> to load and render in the assertions.
        const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==", "base64");
        response.writeHead(200, { "content-type": "image/png", "cache-control": "no-store" });
        response.end(png);
        return;
      }
      else if (url === `/api/sessions/${SESSION_ID}/subagents` || url === `/api/sessions/${SECOND_SESSION_ID}/subagents`) json({ tasks: [], running: 0, maxConcurrent: 3 });
      else if (url === `/api/sessions/${SESSION_ID}/findings` || url === `/api/sessions/${SECOND_SESSION_ID}/findings`) json({ findings: [] });
      else json({});
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

  private async runTimeline(response: ServerResponse, closed: Promise<void>) {
    const send = async (payload: unknown) => {
      await Promise.race([
        new Promise<void>((resolve, reject) => response.write(`data: ${JSON.stringify(payload)}\n\n`, (error) => (error ? reject(error) : resolve()))),
        closed
      ]);
    };
    const round = async (index: number) => {
      // Thinking grows, then assistant text starts — the thinking block
      // auto-collapses in the same commit that appends tall content.
      for (let i = 0; i < 3; i += 1) await send({ type: "thinking_delta", delta: TALL_TEXT });
      for (let i = 0; i < 3; i += 1) await send({ type: "text_delta", delta: TALL_TEXT });
      // A tool card flips queued → running → done while streaming continues.
      await send({ type: "tool_start", toolCallId: `e2e-tool-${index}`, toolName: "bash", args: { command: `echo round ${index}` }, toolStatus: "running" });
      await send({ type: "tool_end", toolCallId: `e2e-tool-${index}`, result: "ok", isError: false });
      await Promise.race([delay(80), closed]);
    };
    for (let index = 0; index < 8; index += 1) await round(index);
    this.phaseA.resolve();
    await Promise.race([this.phaseCRelease.promise, closed]);
    for (let index = 8; index < 11; index += 1) await round(index);
    this.finished.resolve();
    await closed;
  }
}
