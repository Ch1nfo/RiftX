import { assertSessionRunnable, subscribeSession } from "@/server/pi/session-manager";
import { errorMessage, errorResponse } from "@/server/errors";

export const runtime = "nodejs";

function encode(data: unknown) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    await assertSessionRunnable(id);
  } catch (error) {
    return errorResponse(error, "读取会话失败");
  }
  const encoder = new TextEncoder();
  let unsubscribe: () => void = () => undefined;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let cancelled = false;
  const cleanup = () => {
    cancelled = true;
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = undefined;
    unsubscribe();
  };
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(encode({ type: "connected", sessionId: id })));
      // The SSE headers are already committed, so a failure here (archive
      // race, corrupt session file, model registration error) must surface as
      // an in-stream error event — otherwise the browser just reconnect-loops
      // with no typed feedback.
      try {
        const cancel = await subscribeSession(id, (event) => {
          try {
            controller.enqueue(encoder.encode(encode(event)));
          } catch {
            cleanup();
          }
        });
        // The client may have disconnected WHILE subscribeSession was
        // connecting: immediately detach the listener and skip the heartbeat
        // instead of leaking both until the next event fires.
        if (cancelled) {
          cancel();
          // The stream is already closed by the client's cancel() — calling
          // controller.close() here throws Invalid state. Just detach and
          // return.
          return;
        }
        unsubscribe = cancel;
      } catch (error) {
        controller.enqueue(encoder.encode(encode({ type: "error", error: errorMessage(error, "读取会话失败") })));
        controller.close();
        cleanup();
        return;
      }
      heartbeat = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(": keep-alive\n\n"));
        } catch {
          cleanup();
        }
      }, 15_000);
    },
    cancel() {
      cleanup();
    }
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive"
    }
  });
}
