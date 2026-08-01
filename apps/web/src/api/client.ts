import type {
  Approval,
  ApprovalDecisionPayload,
  ApprovalList,
  APIErrorEnvelope,
  Artifact,
  ArtifactList,
  CreateRunPayload,
  ExecutionList,
  Finding,
  FindingList,
  CreateFindingPayload,
  UpdateFindingPayload,
  RegisterArtifactPayload,
  GenerateReportsPayload,
  ModelProfile,
  ModelProfileList,
  ModelProfileSummaryList,
  NodeList,
  NodeStatus,
  ReportList,
  Run,
  RunAction,
  RunActionList,
  RunEventList,
  RunList,
  RunStatus,
  SecurityProfile,
  TerminalSession,
  CreateTerminalPayload,
  ToolRegistrySnapshot,
  ToolRegistrySummary,
  UpdateModelProfilePayload,
  UpdateToolPayload,
} from "./types";

const API_BASE_URL = (import.meta.env.VITE_RIFTX_API_URL as string | undefined)?.replace(
  /\/$/,
  "",
) ?? "";

const LOCAL_OPERATOR_WS_PROTOCOL = "riftx.local-operator.v1";
const LOCAL_OPERATOR_WS_CREDENTIAL_PREFIX = "riftx.local-operator.bearer.v1.";
export const AUTHENTICATED_DOWNLOAD_LIMIT_MIB = 64;
export const MAX_AUTHENTICATED_DOWNLOAD_BYTES =
  AUTHENTICATED_DOWNLOAD_LIMIT_MIB * 1024 * 1024;
let localOperatorToken = "";

export function setLocalOperatorToken(token: string) {
  const normalized = token.trim();
  if (normalized && !isLoopbackAPI()) {
    throw new Error("The local operator token may only be sent to a loopback Control Plane");
  }
  localOperatorToken = normalized;
}

export function clearLocalOperatorToken() {
  localOperatorToken = "";
}

export function localOperatorHeaders(): Record<string, string> {
  return localOperatorToken
    ? { Authorization: `Bearer ${localOperatorToken}` }
    : {};
}

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
      ...localOperatorHeaders(),
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
  getSecurityProfile(): Promise<SecurityProfile> {
    return request<SecurityProfile>("/api/v1/security/profile");
  },

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

  cancelRun(runId: string): Promise<{ accepted: boolean; run: Run }> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
    });
  },

  appendMessage(
    runId: string,
    message: string,
    messageEventId?: string,
  ): Promise<{ accepted: boolean; run: Run }> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/message`, {
      method: "POST",
      body: JSON.stringify({
        message,
        ...(messageEventId ? { message_event_id: messageEventId } : {}),
      }),
    });
  },

  listModelProfiles(): Promise<ModelProfileSummaryList> {
    return request<ModelProfileSummaryList>("/api/v1/model-profiles");
  },

  getModelProfile(profileName: string, adminToken: string): Promise<ModelProfile> {
    return request<ModelProfile>(
      `/api/v1/model-profiles/${encodeURIComponent(profileName)}`,
      { headers: { Authorization: `Bearer ${adminToken}` } },
    );
  },

  updateModelProfile(
    profileName: string,
    payload: UpdateModelProfilePayload,
    adminToken: string,
  ): Promise<ModelProfile> {
    return request<ModelProfile>(
      `/api/v1/model-profiles/${encodeURIComponent(profileName)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
        headers: { Authorization: `Bearer ${adminToken}` },
      },
    );
  },

  setDefaultModelProfile(
    profileName: string,
    adminToken: string,
  ): Promise<ModelProfileList> {
    return request<ModelProfileList>("/api/v1/model-profiles/default", {
      method: "PUT",
      body: JSON.stringify({ profile: profileName }),
      headers: { Authorization: `Bearer ${adminToken}` },
    });
  },

  deleteModelProfile(
    profileName: string,
    adminToken: string,
  ): Promise<ModelProfileList> {
    return request<ModelProfileList>(
      `/api/v1/model-profiles/${encodeURIComponent(profileName)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${adminToken}` },
      },
    );
  },

  listEvents(runId: string, afterSequence = 0): Promise<RunEventList> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}&limit=1000`,
    );
  },

  listRunActions(
    runId: string,
    cursor?: string,
    limit = 50,
  ): Promise<RunActionList> {
    const query = new URLSearchParams({
      limit: String(limit),
      sort: "created_at_desc",
    });
    if (cursor) query.set("cursor", cursor);
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/actions?${query.toString()}`,
    );
  },

  getRunAction(runId: string, actionId: string): Promise<RunAction> {
    return request(
      `/api/v1/runs/${encodeURIComponent(runId)}/actions/${encodeURIComponent(actionId)}`,
    );
  },

  listExecutions(runId: string): Promise<ExecutionList> {
    return request(`/api/v1/runs/${encodeURIComponent(runId)}/executions?limit=1000`);
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

  terminalWebSocketProtocols(): string[] {
    if (!localOperatorToken) return [LOCAL_OPERATOR_WS_PROTOCOL];
    const encoded = base64UrlEncode(localOperatorToken);
    return [
      LOCAL_OPERATOR_WS_PROTOCOL,
      `${LOCAL_OPERATOR_WS_CREDENTIAL_PREFIX}${encoded}`,
    ];
  },

  async downloadAuthenticatedUrl(
    path: string,
    filename?: string,
    options: { maxBytes?: number } = {},
  ): Promise<void> {
    const maxBytes = authenticatedDownloadLimit(options.maxBytes);
    const url = authenticatedURL(path);
    const response = await fetch(url, {
      cache: "no-store",
      headers: localOperatorHeaders(),
    });
    if (!response.ok) {
      throw await errorFromResponse(response);
    }

    const blob = await readBoundedDownload(response, maxBytes);
    let objectUrl: string | null = null;
    let anchor: HTMLAnchorElement | null = null;
    try {
      objectUrl = URL.createObjectURL(blob);
      anchor = document.createElement("a");
      anchor.href = objectUrl;
      if (filename) anchor.download = filename;
      else {
        anchor.target = "_blank";
        anchor.rel = "noreferrer";
      }
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
    } finally {
      try {
        anchor?.remove();
      } finally {
        if (objectUrl !== null) {
          // Revoking on the next task lets the browser consume the URL after a
          // successful click while still cleaning it up when setup or click fails.
          const urlToRevoke = objectUrl;
          window.setTimeout(() => URL.revokeObjectURL(urlToRevoke), 0);
        }
      }
    }
  },

  listNodes(status?: NodeStatus): Promise<NodeList> {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<NodeList>(`/api/v1/nodes${query}`);
  },

  listTools(nodeId = "local"): Promise<ToolRegistrySummary> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/tools`);
  },

  listToolsForAdmin(
    nodeId = "local",
    adminToken = "",
  ): Promise<ToolRegistrySnapshot> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/tools/admin`, {
      headers: { Authorization: `Bearer ${adminToken}` },
    });
  },

  refreshTools(nodeId = "local", adminToken = ""): Promise<ToolRegistrySummary> {
    return request(`/api/v1/nodes/${encodeURIComponent(nodeId)}/refresh-tools`, {
      method: "POST",
      headers: { Authorization: `Bearer ${adminToken}` },
    });
  },

  updateTool(
    nodeId: string,
    toolId: string,
    payload: UpdateToolPayload,
    adminToken = "",
  ): Promise<ToolRegistrySummary> {
    return request(
      `/api/v1/nodes/${encodeURIComponent(nodeId)}/tools/${encodeURIComponent(toolId)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
        headers: { Authorization: `Bearer ${adminToken}` },
      },
    );
  },
};

function isLoopbackAPI(): boolean {
  if (typeof window === "undefined") return true;
  const base = new URL(API_BASE_URL || window.location.origin, window.location.origin);
  return isLoopbackHostname(base.hostname);
}

function authenticatedURL(path: string): string {
  const base = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  const apiBase = new URL(API_BASE_URL || base, base);
  const target = new URL(path, apiBase);
  if (!isLoopbackHostname(target.hostname) || target.origin !== apiBase.origin) {
    throw new Error("Authenticated downloads must stay on the loopback Control Plane");
  }
  return target.toString();
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  return normalized === "localhost"
    || normalized === "::1"
    || /^127(?:\.\d{1,3}){3}$/.test(normalized);
}

function base64UrlEncode(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function readBoundedDownload(response: Response, maxBytes: number): Promise<Blob> {
  const declaredBytes = usableContentLength(response.headers.get("content-length"));
  if (declaredBytes !== null && declaredBytes > BigInt(maxBytes)) {
    await cancelBody(response.body);
    throw downloadTooLargeError(maxBytes, {
      declared_bytes: declaredBytes.toString(),
    });
  }

  if (response.body === null) {
    return new Blob([], { type: response.headers.get("content-type") ?? "" });
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let receivedBytes = 0;
  let completed = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        completed = true;
        break;
      }
      const nextSize = receivedBytes + value.byteLength;
      if (nextSize > maxBytes) {
        throw downloadTooLargeError(maxBytes, { received_bytes: nextSize });
      }
      const ownedChunk: Uint8Array<ArrayBuffer> = new Uint8Array(value.byteLength);
      ownedChunk.set(value);
      chunks.push(ownedChunk);
      receivedBytes = nextSize;
    }
  } finally {
    if (!completed) await cancelReader(reader);
    reader.releaseLock();
  }

  return new Blob(chunks, { type: response.headers.get("content-type") ?? "" });
}

function usableContentLength(value: string | null): bigint | null {
  const normalized = value?.trim();
  if (!normalized || !/^\d+$/.test(normalized)) return null;
  try {
    return BigInt(normalized);
  } catch {
    return null;
  }
}

function authenticatedDownloadLimit(requestedMaxBytes: number | undefined): number {
  if (requestedMaxBytes === undefined) return MAX_AUTHENTICATED_DOWNLOAD_BYTES;
  if (!Number.isSafeInteger(requestedMaxBytes) || requestedMaxBytes < 1) {
    throw new Error("Authenticated download maxBytes must be a positive safe integer");
  }
  return Math.min(requestedMaxBytes, MAX_AUTHENTICATED_DOWNLOAD_BYTES);
}

async function cancelBody(body: ReadableStream<Uint8Array> | null): Promise<void> {
  if (body === null) return;
  try {
    await body.cancel();
  } catch {
    // The stable size error must not be hidden by a transport cancellation error.
  }
}

async function cancelReader(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // The stable size error must not be hidden by a transport cancellation error.
  }
}

function downloadTooLargeError(
  maxBytes: number,
  observed: Record<string, unknown>,
): RiftXAPIError {
  return new RiftXAPIError(
    413,
    "download_too_large",
    `Download blocked because it exceeds the ${formatByteLimit(maxBytes)} safety limit.`,
    {
      limit_bytes: maxBytes,
      ...observed,
    },
  );
}

function formatByteLimit(value: number): string {
  const mib = 1024 * 1024;
  if (value % mib === 0) return `${value / mib} MiB`;
  if (value % 1024 === 0) return `${value / 1024} KiB`;
  return `${value} B`;
}

async function errorFromResponse(response: Response): Promise<RiftXAPIError> {
  const payload = (await response.json().catch(() => null)) as APIErrorEnvelope | null;
  if (payload?.error) {
    return new RiftXAPIError(
      response.status,
      payload.error.code,
      payload.error.message,
      payload.error.details,
    );
  }
  return new RiftXAPIError(
    response.status,
    "http_error",
    `RiftX API returned HTTP ${response.status}`,
  );
}
