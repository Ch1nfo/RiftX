import type {
  Approval,
  ApprovalDecisionPayload,
  ApprovalList,
  APIErrorEnvelope,
  Artifact,
  ArtifactList,
  CreateRunPayload,
  Finding,
  FindingList,
  CreateFindingPayload,
  UpdateFindingPayload,
  RegisterArtifactPayload,
  GenerateReportsPayload,
  NodeList,
  NodeStatus,
  ReportList,
  Run,
  RunEventList,
  RunList,
  RunStatus,
  TerminalSession,
  CreateTerminalPayload,
  ToolRegistrySnapshot,
  UpdateToolPayload,
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

  createFinding(runId: string, payload: CreateFindingPayload): Promise<Finding> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/findings`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getFinding(findingId: string): Promise<Finding> {
    return request(`/api/v1/findings/${encodeURIComponent(findingId)}`);
  },

  updateFinding(
    findingId: string,
    payload: UpdateFindingPayload,
  ): Promise<Finding> {
    return request(`/api/v1/findings/${encodeURIComponent(findingId)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  listArtifacts(runId: string): Promise<ArtifactList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts`);
  },

  registerArtifact(
    runId: string,
    payload: RegisterArtifactPayload,
  ): Promise<Artifact> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/artifacts`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  artifactContentUrl(artifact: Pick<Artifact, "content_url">): string {
    return `${API_BASE_URL}${artifact.content_url}`;
  },

  artifactContentUrlById(artifactId: string): string {
    return `${API_BASE_URL}/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`;
  },

  listReports(runId: string): Promise<ReportList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/reports`);
  },

  generateReports(
    runId: string,
    payload: GenerateReportsPayload = { formats: ["markdown", "html", "json"] },
  ): Promise<ReportList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/reports`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  listApprovals(runId: string): Promise<ApprovalList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/approvals`);
  },

  approve(
    approvalId: string,
    payload: ApprovalDecisionPayload = {},
  ): Promise<Approval> {
    return request(`/api/v1/approvals/${encodeURIComponent(approvalId)}/approve`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  reject(approvalId: string, payload: ApprovalDecisionPayload = {}): Promise<Approval> {
    return request(`/api/v1/approvals/${encodeURIComponent(approvalId)}/reject`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  createTerminal(
    runId: string,
    payload: CreateTerminalPayload = {},
  ): Promise<TerminalSession> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/terminals`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getTerminal(sessionId: string): Promise<TerminalSession> {
    return request(`/api/v1/terminals/${encodeURIComponent(sessionId)}`);
  },

  closeTerminal(sessionId: string): Promise<TerminalSession> {
    return request(`/api/v1/terminals/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  },

  terminalWebSocketUrl(sessionId: string, cursor = 0): string {
    const base = typeof window === "undefined" ? "http://localhost" : window.location.origin;
    const url = new URL(
      `${API_BASE_URL}/api/v1/terminals/${encodeURIComponent(sessionId)}/ws`,
      base,
    );
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("cursor", String(Math.max(cursor, 0)));
    return url.toString();
  },

  listNodes(status?: NodeStatus): Promise<NodeList> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<NodeList>(`/api/v1/nodes${query}`);
  },

  listTools(nodeId = "local"): Promise<ToolRegistrySnapshot> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/tools`);
  },

  refreshTools(nodeId = "local"): Promise<ToolRegistrySnapshot> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/refresh-tools`, {
      method: "POST",
    });
  },

  updateTool(
    nodeId: string,
    toolId: string,
    payload: UpdateToolPayload,
  ): Promise<ToolRegistrySnapshot> {
    return request(
      `/api/v1/nodes/${encodeURIComponent(nodeId)}/tools/${encodeURIComponent(toolId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
  },
};
