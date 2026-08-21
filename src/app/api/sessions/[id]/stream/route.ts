import { assertSessionRunnable, subscribeSession } from "@/server/pi/session-manager";
import { errorMessage, errorStatus } from "@/server/errors";

export const runtime = "nodejs";

function encode(data: unknown) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  try {
    await assertSessionRunnable(id);
  } catch (error) {
    return Response.json({ error: errorMessage(error, "读取会话失败") }, { status: errorStatus(error, 500) });
  }
  const encoder = new TextEncoder();
  let unsubscribe: () => void = () => undefined;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  const cleanup = () => {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = undefined;
    unsubscribe();
  };
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(encode({ type: "connected", sessionId: id })));
      unsubscribe = await subscribeSession(id, (event) => {
        try {
          controller.enqueue(encoder.encode(encode(event)));
        } catch {
          cleanup();
        }
      });
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
