import type {
  APIErrorEnvelope,
  CreateRunPayload,
  FindingList,
  Run,
  RunEventList,
  RunList,
  RunStatus,
  ToolRegistrySnapshot,
} from "./types";

const API_BASE_URL = (import.meta.env.VITE_RIFTX_API_URL as string | undefined)?.replace(
  /\/$/,
  "",
) ?? "";

export class RiftXAPIError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown> | unknown[];

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> | unknown[] = {},
  ) {
    super(message);
    this.name = "RiftXAPIError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const payload = (await response.json().catch(() => null)) as
    | T
    | APIErrorEnvelope
    | null;
  if (!response.ok) {
    if (payload && typeof payload === "object" && "error" in payload) {
      throw new RiftXAPIError(
        response.status,
        payload.error.code,
        payload.error.message,
        payload.error.details,
      );
    }
    throw new RiftXAPIError(
      response.status,
      "http_error",
      `RiftX API returned HTTP ${response.status}`,
    );
  }
  if (payload === null) {
    throw new RiftXAPIError(
      response.status,
      "invalid_response",
      "RiftX API returned an empty response",
    );
  }
  return payload as T;
}

export const api = {
  listRuns(status?: RunStatus): Promise<RunList> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<RunList>(`/api/v1/runs${query}`);
  },

  getRun(runId: string): Promise<Run> {
    return request<Run>(`/api/v1/runs/${encodeURIComponent(runId)}`);
  },

  createRun(payload: CreateRunPayload): Promise<Run> {
    return request<Run>("/api/v1/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  pauseRun(runId: string): Promise<{ accepted: boolean; run: Run }> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/pause`, {
      method: "POST",
    });
  },

  resumeRun(runId: string): Promise<{ accepted: boolean; run: Run }> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
    });
  },

  cancelCurrentExecution(runId: string): Promise<{ accepted: boolean; run: Run }> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/cancel-current-execution`,
      { method: "POST" },
    );
  },

  appendMessage(
    runId: string,
    message: string,
  ): Promise<{ accepted: boolean; run: Run }> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/message`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
  },

  listEvents(runId: string, afterSequence = 0): Promise<RunEventList> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}&limit=1000`,
    );
  },

  eventStreamUrl(runId: string, afterSequence = 0): string {
    return `${API_BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/events/stream?after_sequence=${afterSequence}`;
  },

  listFindings(runId: string): Promise<FindingList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/findings`);
  },

  listTools(nodeId = "local"): Promise<ToolRegistrySnapshot> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/tools`);
  },

  refreshTools(nodeId = "local"): Promise<ToolRegistrySnapshot> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/refresh-tools`, {
      method: "POST",
    });
  },
};
