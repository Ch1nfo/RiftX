export interface HeaderValue {
  name: string;
  value: string;
}

export interface HttpCapture {
  capture_id: string;
  source: "browser";
  method: string;
  url: string;
  http_version: string;
  request_headers: HeaderValue[];
  request_body_base64: string | null;
  response_status: number;
  response_reason: string | null;
  response_headers: HeaderValue[];
  response_body_base64: string | null;
  observed_at: string;
  metadata: Record<string, string | number | boolean | null>;
}

export interface ConnectorTarget {
  runId?: string;
  newRun?: {
    objective: string;
    engagementName: string;
    nodeId?: string;
  };
}

export interface ConnectorReceipt {
  submission: {
    run_id: string;
    capture_id: string;
    request_artifact_id: string;
    response_artifact_id: string | null;
    manifest_artifact_id: string;
  };
  created_run: boolean;
  webui_path: string;
  events_path: string;
  cancel_path: string;
}

export class RiftXConnectorClient {
  readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  async submit(capture: HttpCapture, target: ConnectorTarget): Promise<ConnectorReceipt> {
    const targetPayload = target.runId
      ? { run_id: target.runId }
      : {
          new_run: {
            objective: target.newRun?.objective || "Analyze captured HTTP exchange",
            node_id: target.newRun?.nodeId || undefined,
            engagement: {
              name: target.newRun?.engagementName || "Browser connector capture",
            },
          },
        };
    const response = await fetch(`${this.baseUrl}/api/v1/connectors/submissions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ capture, ...targetPayload }),
    });
    const payload = await readJson(response);
    return payload.receipt as ConnectorReceipt;
  }

  async listRuns(): Promise<Array<{ id: string; objective: { description: string } }>> {
    const response = await fetch(`${this.baseUrl}/api/v1/connectors/runs?limit=100`);
    const payload = await readJson(response);
    return payload.items;
  }

  async cancel(runId: string): Promise<void> {
    await readJson(
      await fetch(`${this.baseUrl}/api/v1/connectors/runs/${runId}/cancel`, {
        method: "POST",
      }),
    );
  }

  async webuiUrl(runId: string): Promise<string> {
    const payload = await readJson(
      await fetch(`${this.baseUrl}/api/v1/connectors/runs/${runId}/webui`),
    );
    return payload.url;
  }

  async streamEvents(
    runId: string,
    onEvent: (event: { id: string; type: string; data: unknown }) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const response = await fetch(
      `${this.baseUrl}/api/v1/connectors/runs/${runId}/events`,
      { headers: { Accept: "text/event-stream" }, signal },
    );
    if (!response.ok || !response.body) {
      throw new Error(`SSE failed (${response.status})`);
    }
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    let current = { id: "", type: "message", data: "" };
    while (true) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += value;
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line) {
          if (current.data) {
            onEvent({
              id: current.id,
              type: current.type,
              data: parseEventData(current.data),
            });
          }
          current = { id: "", type: "message", data: "" };
        } else if (line.startsWith("id:")) {
          current.id = line.slice(3).trim();
        } else if (line.startsWith("event:")) {
          current.type = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          current.data += line.slice(5).trim();
        }
      }
    }
  }
}

export async function harEntryToCapture(entry: RiftXHarEntry): Promise<HttpCapture> {
  const [responseContent, responseEncoding] = await new Promise<[string, string]>((resolve) => {
    entry.getContent((content, encoding) => resolve([content, encoding]));
  });
  return {
    capture_id: crypto.randomUUID(),
    source: "browser",
    method: entry.request.method,
    url: entry.request.url,
    http_version: entry.request.httpVersion || "HTTP/1.1",
    request_headers: entry.request.headers,
    request_body_base64: encodeHarText(
      entry.request.postData?.text || "",
      entry.request.postData?.encoding || "",
    ),
    response_status: entry.response.status,
    response_reason: entry.response.statusText || null,
    response_headers: entry.response.headers,
    response_body_base64: encodeHarText(responseContent, responseEncoding),
    observed_at: entry.startedDateTime || new Date().toISOString(),
    metadata: {
      resource_type: entry._resourceType || "unknown",
      mime_type: entry.response.content?.mimeType || null,
    },
  };
}

export function shouldCapture(entry: RiftXHarEntry): boolean {
  const type = (entry._resourceType || "").toLowerCase();
  return type === "xhr" || type === "fetch";
}

function encodeHarText(content: string, encoding: string): string | null {
  if (!content) return null;
  if (encoding.toLowerCase() === "base64") return content;
  const bytes = new TextEncoder().encode(content);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function readJson(response: Response): Promise<any> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.error?.message || `RiftX API failed (${response.status})`);
  }
  return payload;
}

function parseEventData(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return data;
  }
}
