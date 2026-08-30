import type { ExtensionFactory, ToolCallEvent } from "@mariozechner/pi-coding-agent";
import type { ApprovalRequest } from "@/lib/types";
import { ApprovalGate } from "./approval-gate";
import type { ApprovalEvaluation } from "./approval-evaluator";
import type { ScopeDecision } from "@/lib/scope-rules";

/** Tools whose calls require permission evaluation. Shared with the event mapper so tool_status stays consistent with gating. */
export const guardedTools = new Set(["bash", "write", "edit", "browser", "crawl"]);
/** MCP tools are gated like any other state-changing tool: they run arbitrary external code. */
export const MCP_TOOL_PREFIX = "mcp__";

export function isGuardedTool(toolName: string) {
  return guardedTools.has(toolName) || toolName.startsWith(MCP_TOOL_PREFIX);
}
const readOnlyBrowserActions = new Set(["snapshot", "console", "requests", "request_detail", "response_body", "identities", "cookies", "cookies_export", "storage", "screenshot", "tabs"]);
type ResolvedApproval = { approved: boolean; task: boolean; reason?: string };
const AUTO_APPROVAL_UNAVAILABLE = "Automatic approval is unavailable. Switch approval mode to request approval or full access, then retry.";

function mappingTargetUrl(target: string) {
  const needsBrackets = (target.match(/:/g) ?? []).length > 1 && !target.startsWith("[");
  return `http://${needsBrackets ? `[${target}]` : target}/`;
}

function mappingTargetHasExplicitPort(target: string) {
  const value = target.trim();
  return /^\[[^\]]+\]:\d+$/.test(value) || /^[^:]+:\d+$/.test(value);
}

/** Lets the permission gate authorize browser navigations that fall outside the declared scope. */
export type BrowserScopeGuard = {
  check(url: string): ScopeDecision;
  authorizeOnce(url: string, identity?: string): void;
  revokeOnce(url: string, identity?: string): void;
  grantScope(url: string, exactPort?: boolean): void;
  /** Return every host-mapping target outside the authorized scope (mapping targets are physical destinations). */
  checkMappings?(mappings: Record<string, string>): Array<{ host: string; target: string }>;
  /** Temporarily authorize mapping-target hosts after an allow-once approval. */
  authorizeMappingsOnce?(mappings: Record<string, string>): void;
};

export function createPermissionExtension(
  gate: ApprovalGate,
  onEvent: (event: Record<string, unknown>) => void,
  evaluate?: (request: ApprovalRequest) => Promise<ApprovalEvaluation>,
  browserScope?: BrowserScopeGuard
): ExtensionFactory {
  return (agent) => {
    const scopeEffects = new Map<string, { commit: () => void; rollback: () => void }>();
    const settleScopeEffect = (toolCallId: string, succeeded: boolean) => {
      const effect = scopeEffects.get(toolCallId);
      if (!effect) return;
      scopeEffects.delete(toolCallId);
      if (succeeded) effect.commit();
      else effect.rollback();
    };
    agent.on("tool_execution_end", (event) => {
      settleScopeEffect(event.toolCallId, !event.isError);
    });
    agent.on("tool_call", async (event: ToolCallEvent, ctx) => {
      if (!isGuardedTool(event.toolName)) return;
      let scopeAuthorized = false;
      if (event.toolName === "browser") {
        const action = (event.input as { action?: unknown }).action;
        if (typeof action === "string" && readOnlyBrowserActions.has(action)) {
          onEvent({ type: "tool_status", toolName: event.toolName, toolCallId: event.toolCallId, toolStatus: "running" });
          return;
        }
        // Scope expansion follows the selected approval mode just like every
        // other guarded tool: full access bypasses it, auto delegates it to
        // the evaluator, and request mode waits for the user.
        if (browserScope && action === "navigate") {
          const url = (event.input as { url?: unknown }).url;
          if (typeof url === "string") {
            const scope = browserScope.check(url);
            if (!scope.allowed && scope.host) {
              const scopeRequest: ApprovalRequest = {
                id: event.toolCallId,
                toolName: "browser",
                input: { action: "navigate", url, scopeExpansion: true, suggestedRule: scope.suggestedRule, reason: scope.reason },
                createdAt: new Date().toISOString()
              };
              const decision = await resolveApproval(scopeRequest, gate, evaluate, onEvent, ctx.signal);
              if (!decision.approved) {
                return { block: true, reason: decision.reason ?? `Navigation to ${scope.host} is outside the authorized browser scope and the user rejected expanding it. Continue with in-scope targets only.` };
              }
              const identityInput = (event.input as { identity?: unknown }).identity;
              const identity = typeof identityInput === "string" ? identityInput : undefined;
              // The navigation needs a temporary authorization to execute. A
              // task grant is committed only after success; failures roll the
              // temporary rule back instead of silently expanding scope.
              browserScope.authorizeOnce(url, identity);
              scopeEffects.set(event.toolCallId, {
                commit: () => {
                  if (decision.task) {
                    browserScope.grantScope(url);
                    browserScope.revokeOnce(url, identity);
                  }
                },
                rollback: () => browserScope.revokeOnce(url, identity)
              });
              scopeAuthorized = true;
            }
          }
        } else if (browserScope?.checkMappings && action === "set_host_mappings") {
          const mappings = (event.input as { mappings?: unknown }).mappings;
          if (mappings && typeof mappings === "object" && !Array.isArray(mappings)) {
            let violating: Array<{ host: string; target: string }>;
            try {
              violating = browserScope.checkMappings(mappings as Record<string, string>);
            } catch (error) {
              return { block: true, reason: error instanceof Error ? error.message : "Invalid host mappings" };
            }
            if (violating.length) {
              const targets = violating.map((item) => item.target).join(", ");
              const scopeRequest: ApprovalRequest = {
                id: event.toolCallId,
                toolName: "browser",
                input: { action: "set_host_mappings", mappings, scopeExpansion: true, suggestedRule: violating.map((item) => item.target), reason: "mapping targets are outside the authorized browser scope" },
                createdAt: new Date().toISOString()
              };
              const decision = await resolveApproval(scopeRequest, gate, evaluate, onEvent, ctx.signal);
              if (!decision.approved) {
                return { block: true, reason: decision.reason ?? `Host-mapping target(s) ${targets} are outside the authorized browser scope and the user rejected expanding it. Map only authorized destinations.` };
              }
              // setHostMappings itself does not need the expanded scope. Commit
              // its authorization only after the mutation succeeds.
              scopeEffects.set(event.toolCallId, {
                commit: () => {
                  if (decision.task) {
                    // Grant both the physical destination and logical mapping
                    // host. Preserve an explicitly approved physical port.
                    violating.forEach((item) => {
                      browserScope.grantScope(mappingTargetUrl(item.target), mappingTargetHasExplicitPort(item.target));
                      browserScope.grantScope(mappingTargetUrl(item.host));
                    });
                  } else browserScope.authorizeMappingsOnce?.(mappings as Record<string, string>);
                },
                rollback: () => undefined
              });
              scopeAuthorized = true;
            }
          }
        }
      }
      const request: ApprovalRequest = {
        id: event.toolCallId,
        toolName: event.toolName as ApprovalRequest["toolName"],
        input: event.input,
        createdAt: new Date().toISOString()
      };
      if (!scopeAuthorized) {
        const decision = await resolveApproval(request, gate, evaluate, onEvent, ctx.signal);
        if (!decision.approved) return { block: true, reason: decision.reason ?? "Blocked by RiftX safety gate" };
      }
      // Locks are NOT acquired here. The SDK's parallel executor runs ALL
      // beforeToolCall handlers before starting ANY execution — acquiring a
      // lock here means bash's shared lock is held while write's exclusive
      // acquisition blocks, but bash hasn't actually started running so it
      // will never release: deadlock. Locks are acquired inside each tool's
      // execute() wrapper (session-manager.ts) at actual execution time.
      onEvent({ type: "tool_status", toolName: event.toolName, toolCallId: event.toolCallId, toolStatus: "running" });
    });
  };
}

async function resolveApproval(
  request: ApprovalRequest,
  gate: ApprovalGate,
  evaluate: ((request: ApprovalRequest) => Promise<ApprovalEvaluation>) | undefined,
  onEvent: (event: Record<string, unknown>) => void,
  signal?: AbortSignal
): Promise<ResolvedApproval> {
  if (gate.shouldBypass(request)) return { approved: true, task: false };
  if (gate.approvalMode === "auto") {
    if (!evaluate) return { approved: false, task: false, reason: AUTO_APPROVAL_UNAVAILABLE };
    try {
      const evaluation = await evaluate(request);
      onEvent({ type: "approval_evaluated", approval: request, evaluation });
      return { approved: evaluation.approved, task: false, reason: evaluation.approved ? undefined : `Rejected by RiftX approval evaluator: ${evaluation.reason}` };
    } catch (error) {
      const reason = error instanceof Error ? error.message : "Approval evaluator failed";
      onEvent({ type: "approval_evaluation_error", approval: request, error: reason });
      return { approved: false, task: false, reason: AUTO_APPROVAL_UNAVAILABLE };
    }
  }
  onEvent({ type: "approval_required", approval: request });
  return gate.waitForApproval(request, 120_000, signal);
}
