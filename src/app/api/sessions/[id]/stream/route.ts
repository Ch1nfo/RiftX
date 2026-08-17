import { subscribeSession } from "@/server/pi/session-manager";

export const runtime = "nodejs";

function encode(data: unknown) {
  return `data: ${JSON.stringify(data)}\n\n`;
}

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const encoder = new TextEncoder();
  let cleanup: () => void = () => undefined;
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(encode({ type: "connected", sessionId: id })));
      cleanup = await subscribeSession(id, (event) => {
        try {
          controller.enqueue(encoder.encode(encode(event)));
        } catch {
          cleanup();
        }
      });
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
