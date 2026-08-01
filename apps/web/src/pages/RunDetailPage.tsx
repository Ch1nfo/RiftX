import {
  Activity,
  Archive,
  AlertTriangle,
  ArrowLeft,
  Ban,
  Bot,
  CheckCircle2,
  ChevronRight,
  CirclePause,
  Clock3,
  Download,
  ExternalLink,
  FileText,
  FileWarning,
  Loader2,
  MessageSquareText,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Save,
  Send,
  ShieldAlert,
  TerminalSquare,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import {
  FormEvent,
  lazy,
  Suspense,
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams, useSearchParams } from "react-router-dom";

import {
  api,
  AUTHENTICATED_DOWNLOAD_LIMIT_MIB,
  RiftXAPIError,
} from "../api/client";
import type {
  Approval,
  Artifact,
  Finding,
  FindingEvidence,
  GraphViewKind,
  Report,
  Run,
  RunEvent,
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { StatusBadge } from "../components/StatusBadge";
import { TerminalPanel } from "../components/TerminalPanel";
import {
  useApprovalControl,
  useApprovals,
  useArtifactControl,
  useArtifacts,
  flattenRunActionPages,
  useFindingControl,
  useFindings,
  useReportControl,
  useReports,
  useRun,
  useRunAction,
  useRunActions,
  useRunControl,
  useRunEvents,
} from "../hooks/queries";
import { useEventStream } from "../hooks/useEventStream";
import { useI18n, type Language } from "../i18n";
import { ActionInspector, ActionTimeline } from "./RunActionTimeline";
import {
  reduceRunEvents,
  type ConversationMessage,
  type TimelineItem,
} from "./runStreamReducer";

const RunGraphWorkspace = lazy(() => import("./RunGraphWorkspace"));

type DetailTab =
  | "overview"
  | "agent"
  | "actions"
  | "graph"
  | "timeline"
  | "raw-events"
  | "approvals"
  | "terminal"
  | "artifacts"
  | "findings"
  | "report";

type MessageRetry = {
  message: string;
  eventId: string;
};

type MessageDraft = {
  runId: string;
  message: string;
  retry: MessageRetry | null;
};

type MessageSubmissionToken = MessageRetry & {
  runId: string;
};

type ActionAuthorizationLatch = {
  error: RiftXAPIError;
  listDataUpdatedAt: number;
  runId: string;
};

const MESSAGE_RETRY_STORAGE_PREFIX = "riftx.run-message-retry:";

function messageRetryStorageKey(runId: string) {
  return `${MESSAGE_RETRY_STORAGE_PREFIX}${runId}`;
}

function readMessageRetry(runId: string): MessageRetry | null {
  if (!runId || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(messageRetryStorageKey(runId));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<MessageRetry>;
    if (
      typeof value.message !== "string" ||
      !value.message.trim() ||
      typeof value.eventId !== "string" ||
      !value.eventId
    ) {
      window.sessionStorage.removeItem(messageRetryStorageKey(runId));
      return null;
    }
    return { message: value.message, eventId: value.eventId };
  } catch {
    // Storage can be unavailable in hardened/private browser contexts. Memory
    // retry still works for the current mount, and malformed data is ignored.
    return null;
  }
}

function writeMessageRetry(runId: string, retry: MessageRetry | null) {
  if (!runId || typeof window === "undefined") return;
  try {
    const key = messageRetryStorageKey(runId);
    if (retry) {
      window.sessionStorage.setItem(key, JSON.stringify(retry));
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch {
    // See readMessageRetry: storage durability is a defense in depth, not a
    // prerequisite for sending a message in restricted browser contexts.
  }
}

function messageRetriesMatch(left: MessageRetry | null, right: MessageRetry) {
  return left?.message === right.message && left.eventId === right.eventId;
}

function replaceMessageRetryIfMatches(
  runId: string,
  expected: MessageRetry,
  replacement: MessageRetry | null,
) {
  if (!messageRetriesMatch(readMessageRetry(runId), expected)) return;
  writeMessageRetry(runId, replacement);
}

function messageDraftForRun(runId: string): MessageDraft {
  const retry = readMessageRetry(runId);
  return {
    runId,
    message: retry?.message ?? "",
    retry,
  };
}

function actionAuthorizationFailure(...errors: unknown[]): RiftXAPIError | null {
  for (const error of errors) {
    if (error instanceof RiftXAPIError && [401, 403].includes(error.status)) {
      return error;
    }
  }
  return null;
}

export function RunDetailPage() {
  const { language, t } = useI18n();
  const { runId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const actionParam = searchParams.get("action") ?? "";
  const graphViewParam = parseGraphView(searchParams.get("graph_view"));
  const graphFocusParam = searchParams.get("graph_focus") ?? "";
  const graphRouteActive = Boolean(graphViewParam || graphFocusParam);
  const routeDefaultTab = detailTabForRoute(
    actionParam,
    graphViewParam,
    graphFocusParam,
  );
  const [selectionRoute, setSelectionRoute] = useState(() => ({
    actionId: actionParam,
    runId,
  }));
  const selectedActionId =
    selectionRoute.runId === runId && selectionRoute.actionId === actionParam
      ? actionParam || null
      : null;
  const run = useRun(runId);
  const events = useRunEvents(runId);
  const actions = useRunActions(runId);
  const selectedAction = useRunAction(runId, selectedActionId ?? "");
  const findings = useFindings(runId);
  const artifacts = useArtifacts(runId);
  const approvals = useApprovals(runId);
  const reports = useReports(runId);
  const approvalControls = useApprovalControl(runId);
  const artifactControls = useArtifactControl(runId);
  const findingControls = useFindingControl(runId);
  const reportControls = useReportControl(runId);
  const controls = useRunControl(runId);
  const eventStream = useEventStream(runId, events.isSuccess);
  const [tabSelection, setTabSelection] = useState(() => ({
    runId,
    value: routeDefaultTab,
  }));
  const tab = tabSelection.runId === runId ? tabSelection.value : routeDefaultTab;
  function setTab(value: DetailTab) {
    setTabSelection({ runId, value });
  }
  const [inspectorFocusKey, setInspectorFocusKey] = useState<string | null>(null);
  const [actionAnnouncement, setActionAnnouncement] = useState("");
  const [actionAuthorizationLatch, setActionAuthorizationLatch] =
    useState<ActionAuthorizationLatch | null>(null);
  const [messageDraft, setMessageDraft] = useState<MessageDraft>(() =>
    messageDraftForRun(runId),
  );
  const currentRunIdRef = useRef(runId);
  const activeMessageSubmissionRef = useRef<MessageSubmissionToken | null>(null);
  const actionTriggerRefs = useRef(new Map<string, HTMLButtonElement>());
  const previousActionRouteRef = useRef({
    actionId: actionParam,
    graphActive: graphRouteActive,
    runId,
  });
  const actionRevisionRef = useRef({ revision: 0, runId });
  const tabRefs = useRef(new Map<DetailTab, HTMLButtonElement>());
  currentRunIdRef.current = runId;

  useEffect(() => {
    const previous = previousActionRouteRef.current;
    let focusTimer: number | null = null;
    if (previous.runId === runId && previous.actionId && !actionParam) {
      const trigger = actionTriggerRefs.current.get(previous.actionId);
      const focusTarget = graphRouteActive
        ? tabRefs.current.get("graph")
        : trigger?.isConnected
          ? trigger
          : tabRefs.current.get("actions");
      focusTimer = window.setTimeout(() => {
        const current = previousActionRouteRef.current;
        if (
          current.runId === runId &&
          current.actionId === actionParam &&
          current.graphActive === graphRouteActive
        ) {
          focusTarget?.focus();
        }
      }, 0);
    }
    if (
      previous.runId === runId &&
      previous.graphActive &&
      actionParam
    ) {
      setInspectorFocusKey(`${runId}:${actionParam}`);
    }
    if (previous.runId !== runId) actionTriggerRefs.current.clear();
    previousActionRouteRef.current = {
      actionId: actionParam,
      graphActive: graphRouteActive,
      runId,
    };
    setSelectionRoute({ actionId: actionParam, runId });
    if (graphViewParam || graphFocusParam) setTab("graph");
    else if (actionParam) setTab("actions");
    else if (previous.runId !== runId) setTab("agent");
    return () => {
      if (focusTimer !== null) window.clearTimeout(focusTimer);
    };
  }, [actionParam, graphFocusParam, graphRouteActive, graphViewParam, runId]);

  const focusInspector =
    selectedActionId !== null && inspectorFocusKey === `${runId}:${selectedActionId}`;
  useEffect(() => {
    if (focusInspector) setInspectorFocusKey(null);
  }, [focusInspector]);

  const actionRevision = eventStream?.actionUpdateRevision ?? 0;
  useEffect(() => {
    const previous = actionRevisionRef.current;
    if (previous.runId !== runId) {
      actionRevisionRef.current = { revision: actionRevision, runId };
      setActionAnnouncement("");
      return;
    }
    const count = actionRevision - previous.revision;
    actionRevisionRef.current = { revision: actionRevision, runId };
    if (count > 0) {
      setActionAnnouncement(
        t("Action data updated. Live revision {revision}.", {
          revision: actionRevision,
        }),
      );
    }
  }, [actionRevision, runId, t]);

  useEffect(() => {
    if (!selectedActionId) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      setInspectorFocusKey(null);
      setSelectionRoute({ actionId: "", runId });
      setSearchParams({}, { replace: true });
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [runId, selectedActionId, setSearchParams]);

  useEffect(() => {
    setMessageDraft((current) =>
      current.runId === runId ? current : messageDraftForRun(runId),
    );
  }, [runId]);

  // Route parameters update one render before the effect above restores that
  // Run's draft. Never expose the previous Run's draft to the new Run's form
  // during that transition.
  const currentMessageDraft =
    messageDraft.runId === runId
      ? messageDraft
      : { runId, message: "", retry: null };

  const eventItems = events.data?.items ?? [];
  const actionItems = flattenRunActionPages(actions.data?.pages);
  const observedActionAuthorizationError = actionAuthorizationFailure(
    eventStream?.error,
    actions.error,
    selectedAction.error,
  );
  useEffect(() => {
    setActionAuthorizationLatch((current) => {
      if (observedActionAuthorizationError) {
        if (
          current?.runId === runId &&
          current.error === observedActionAuthorizationError
        ) {
          return current;
        }
        return {
          error: observedActionAuthorizationError,
          listDataUpdatedAt: actions.dataUpdatedAt,
          runId,
        };
      }
      if (!current) return current;
      if (current.runId !== runId) return null;
      if (
        actions.isSuccess &&
        actions.dataUpdatedAt > current.listDataUpdatedAt
      ) {
        return null;
      }
      return current;
    });
  }, [
    actions.dataUpdatedAt,
    actions.isSuccess,
    observedActionAuthorizationError,
    runId,
  ]);
  const actionAuthorizationError =
    observedActionAuthorizationError ??
    (actionAuthorizationLatch?.runId === runId
      ? actionAuthorizationLatch.error
      : null);
  const visibleActionItems = actionAuthorizationError ? [] : actionItems;
  const eventProjection = reduceRunEvents(eventItems);
  const planEvent = [...eventItems]
    .reverse()
    .find((event) => event.event_type === "agent.plan_updated");
  const terminalEvent = [...eventItems]
    .reverse()
    .find(
      (event) =>
        event.event_type === "terminal.opened" &&
        typeof event.payload.session_id === "string",
    );
  const terminalSessionId = terminalEvent?.payload.session_id as string | undefined;
  const pendingApprovals =
    approvals.data?.items.filter((approval) => approval.status === "pending") ?? [];
  const approvalToResync = findApprovalToResync(
    run.data?.status,
    eventItems,
    approvals.data?.items ?? [],
  );

  async function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // The component is retained when navigating between /runs/:runId routes.
    // Refuse a submit from the transition render until this Run owns the
    // composer state; otherwise Run A's retry could be sent through Run B's
    // mutation hook before the restoration effect runs.
    if (messageDraft.runId !== runId) return;
    const normalized = messageDraft.message.trim();
    if (!normalized) return;
    const messageEventId =
      messageDraft.retry?.message === normalized
        ? messageDraft.retry.eventId
        : globalThis.crypto.randomUUID();
    const submission: MessageSubmissionToken = {
      runId,
      message: normalized,
      eventId: messageEventId,
    };
    activeMessageSubmissionRef.current = submission;
    // Persist the client-generated UUID before awaiting the network. Even if
    // the HTTP response itself is lost, the next unchanged submit reuses the
    // same database/Workflow idempotency key.
    const pendingRetry = { message: normalized, eventId: messageEventId };
    setMessageDraft((current) =>
      current.runId === submission.runId && current.message.trim() === normalized
        ? { ...current, retry: pendingRetry }
        : current,
    );
    // Keep the same idempotency key across a refresh after an ambiguous HTTP /
    // Temporal outcome. Session storage is scoped to this browser tab and is
    // cleared as soon as the server confirms delivery.
    writeMessageRetry(runId, pendingRetry);
    try {
      await controls.message.mutateAsync({
        message: normalized,
        messageEventId,
      });
      // A later retry/edit may already own this Run's storage slot. Clear only
      // the exact operation the server confirmed, never whichever entry happens
      // to be current when this promise settles.
      replaceMessageRetryIfMatches(submission.runId, pendingRetry, null);
      if (
        activeMessageSubmissionRef.current === submission &&
        currentRunIdRef.current === submission.runId
      ) {
        activeMessageSubmissionRef.current = null;
        setMessageDraft((current) =>
          current.runId === submission.runId &&
          current.message.trim() === submission.message &&
          messageRetriesMatch(current.retry, pendingRetry)
            ? { ...current, message: "", retry: null }
            : current,
        );
      }
    } catch (error) {
      // The mutation exposes its structured API error above the conversation.
      // Keep both the draft and its persisted event ID. Retrying that exact ID
      // is safe even when Temporal accepted the first request but its response
      // was lost, because the Workflow de-duplicates message event IDs.
      if (
        error instanceof RiftXAPIError &&
        !Array.isArray(error.details) &&
        error.details.retry_same_message === true &&
        typeof error.details.message_event_id === "string"
      ) {
        const retry = {
          message: normalized,
          eventId: error.details.message_event_id,
        };
        replaceMessageRetryIfMatches(submission.runId, pendingRetry, retry);
        if (
          activeMessageSubmissionRef.current === submission &&
          currentRunIdRef.current === submission.runId
        ) {
          setMessageDraft((current) =>
            current.runId === submission.runId &&
            current.message.trim() === submission.message &&
            messageRetriesMatch(current.retry, pendingRetry)
              ? { ...current, retry }
              : current,
          );
        }
      }
      if (activeMessageSubmissionRef.current === submission) {
        activeMessageSubmissionRef.current = null;
      }
    }
  }

  if (run.isLoading) return <LoadingState label="Loading durable run" />;
  if (run.error) return <ErrorState error={run.error} />;
  if (!run.data) return null;

  const isFinal = ["completed", "failed", "cancelled"].includes(run.data.status);
  const anyControlPending =
    controls.pause.isPending ||
    controls.resume.isPending ||
    controls.emergencyStop.isPending;
  const detailTabs: Array<[DetailTab, string]> = [
    ["overview", t("Overview")],
    ["agent", t("Conversation")],
    ["actions", `${t("Actions")} ${visibleActionItems.length}`],
    ["graph", t("Graph")],
    ["timeline", `${t("Timeline")} ${eventProjection.highLevelTimeline.length}`],
    ["raw-events", `${t("Raw events")} ${eventProjection.rawEvents.length}`],
    ["approvals", `${t("Approvals")} ${pendingApprovals.length}`],
    ["terminal", t("Terminal")],
    ["artifacts", `${t("Artifacts")} ${artifacts.data?.items.length ?? 0}`],
    ["findings", `${t("Findings")} ${findings.data?.items.length ?? 0}`],
    ["report", `${t("Reports")} ${reports.data?.items.length ?? 0}`],
  ];
  const selectedActionData =
    !actionAuthorizationError &&
    selectedAction.data?.run_id === runId &&
    selectedAction.data.action_id === selectedActionId
      ? selectedAction.data
      : undefined;

  function selectAction(actionId: string, trigger: HTMLButtonElement) {
    actionTriggerRefs.current.set(actionId, trigger);
    setSelectionRoute({ actionId, runId });
    setInspectorFocusKey(`${runId}:${actionId}`);
    setTab("actions");
    setSearchParams({ action: actionId });
  }

  function activateDetailTab(nextTab: DetailTab) {
    setTab(nextTab);
    if (nextTab === "graph") {
      setSelectionRoute({ actionId: "", runId });
      setInspectorFocusKey(null);
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("action");
        next.set("graph_view", graphViewParam ?? "task");
        return next;
      });
      return;
    }
    if (tab === "graph" || graphViewParam || graphFocusParam) {
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.delete("graph_view");
        next.delete("graph_focus");
        return next;
      });
    }
  }

  function changeGraphView(nextView: GraphViewKind) {
    setTab("graph");
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("action");
      if (nextView !== graphViewParam) next.delete("graph_focus");
      next.set("graph_view", nextView);
      return next;
    });
  }

  function changeGraphFocus(focusId: string) {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("action");
      next.set("graph_view", graphViewParam ?? "task");
      if (focusId) next.set("graph_focus", focusId);
      else next.delete("graph_focus");
      return next;
    });
  }

  function openGraphAction(actionId: string) {
    setSelectionRoute({ actionId, runId });
    setInspectorFocusKey(`${runId}:${actionId}`);
    setTab("actions");
    setSearchParams({ action: actionId });
  }

  function openActionGraph(nodeId: string) {
    setSelectionRoute({ actionId: "", runId });
    setInspectorFocusKey(null);
    setTab("graph");
    setSearchParams({
      graph_view: "task",
      graph_focus: nodeId,
    });
  }

  function closeActionInspector() {
    setInspectorFocusKey(null);
    setSelectionRoute({ actionId: "", runId });
    setSearchParams({}, { replace: true });
  }

  function moveTabFocus(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % detailTabs.length;
    if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + detailTabs.length) % detailTabs.length;
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = detailTabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTab = detailTabs[nextIndex]![0];
    activateDetailTab(nextTab);
    tabRefs.current.get(nextTab)?.focus();
  }

  return (
    <div className="page-stack">
      <div className="detail-heading">
        <div>
          <Link className="back-link" to="/">
            <ArrowLeft size={15} /> {t("Dashboard")}
          </Link>
          <div className="detail-title-row">
            <h2>{run.data.objective.description}</h2>
            <StatusBadge status={run.data.status} />
          </div>
          <p className="run-identity">
            <span>{run.data.id}</span>
            <span>{t("node")} / {run.data.node_id}</span>
            <span>{t("mode")} / {t(run.data.approval_mode)}</span>
            <span>{t("model")} / {run.data.model_profile ?? t("default")}</span>
          </p>
        </div>
        <div className="control-cluster">
          <button
            className="secondary-button"
            disabled={isFinal || anyControlPending}
            onClick={() => controls.pause.mutate()}
          >
            <CirclePause size={16} /> {t("Pause")}
          </button>
          <button
            className="secondary-button"
            disabled={isFinal || anyControlPending}
            onClick={() => controls.resume.mutate()}
          >
            <Play size={16} /> {t("Resume")}
          </button>
          <button
            className="danger-button"
            disabled={anyControlPending}
            onClick={() => controls.emergencyStop.mutate()}
            title={t("Emergency stop — cancel the entire Run")}
            aria-label={t("Emergency stop — cancel the entire Run")}
          >
            <Ban size={16} /> {t("Emergency stop")}
          </button>
        </div>
      </div>

      {controls.pause.error ||
      controls.resume.error ||
      controls.emergencyStop.error ||
      controls.message.error ? (
        <ErrorState
          error={
            controls.pause.error ??
            controls.resume.error ??
            controls.emergencyStop.error ??
            controls.message.error ??
            new Error()
          }
        />
      ) : null}

      {eventStream?.error ? <ErrorState error={eventStream.error} /> : null}
      {actionAuthorizationError &&
      actionAuthorizationError !== eventStream?.error &&
      tab !== "actions" &&
      !selectedActionId ? (
        <ErrorState error={actionAuthorizationError} />
      ) : null}
      {eventStream?.stale ? (
        <section className="stream-stale-alert" role="alert">
          <AlertTriangle size={17} />
          <span>{t("Live updates are stale while RiftX repairs the durable snapshots.")}</span>
        </section>
      ) : null}
      <span
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {actionAnnouncement}
      </span>

      {pendingApprovals.length ? (
        <button className="approval-alert" onClick={() => activateDetailTab("approvals")}>
          <ShieldAlert size={19} />
          <span>
            <strong>{t(
              pendingApprovals.length === 1
                ? "{count} tool call awaiting approval"
                : "{count} tool calls awaiting approval",
              { count: pendingApprovals.length },
            )}</strong>
            {t("Review the exact command, target, and environment before resuming the Agent.")}
          </span>
          <ChevronRight size={18} />
        </button>
      ) : null}

      {approvalToResync ? (
        <>
          <section className="approval-alert approval-resync-alert" role="alert">
            <ShieldAlert size={19} />
            <span>
              <strong>
                {t("A saved {decision} decision still needs workflow synchronization", {
                  decision: t(approvalToResync.status),
                })}
              </strong>
              {t(
                "The Run is still waiting for this approval. Re-send the immutable saved decision to the durable workflow.",
              )}
            </span>
            <button
              className="primary-button"
              disabled={
                approvalControls.approve.isPending || approvalControls.reject.isPending
              }
              onClick={() => {
                if (approvalToResync.status === "approved") {
                  approvalControls.approve.mutate({
                    approvalId: approvalToResync.id,
                  });
                } else {
                  approvalControls.reject.mutate({
                    approvalId: approvalToResync.id,
                  });
                }
              }}
            >
              {approvalControls.approve.isPending || approvalControls.reject.isPending ? (
                <Loader2 className="spin" size={15} />
              ) : (
                <RefreshCw size={15} />
              )}
              {t("Resync saved decision")}
            </button>
          </section>
          {approvalControls.approve.error || approvalControls.reject.error ? (
            <ErrorState
              error={
                approvalControls.approve.error ??
                approvalControls.reject.error ??
                new Error()
              }
            />
          ) : null}
        </>
      ) : null}

      <div className="detail-layout">
        <section className="detail-main panel">
          <div className="detail-tabs" role="tablist" aria-label={t("Run detail views")}>
            {detailTabs.map(([value, label], index) => (
              <button
                key={value}
                ref={(node) => {
                  if (node) tabRefs.current.set(value, node);
                  else tabRefs.current.delete(value);
                }}
                id={`run-detail-tab-${value}`}
                className={tab === value ? "active" : ""}
                onClick={() => activateDetailTab(value)}
                onKeyDown={(event) => moveTabFocus(event, index)}
                role="tab"
                aria-selected={tab === value}
                aria-controls="run-detail-panel"
                tabIndex={tab === value ? 0 : -1}
              >
                {label}
              </button>
            ))}
          </div>

          <div
            className="detail-tab-content"
            id="run-detail-panel"
            role="tabpanel"
            aria-labelledby={`run-detail-tab-${tab}`}
          >
            {tab === "overview" ? (
              <RunOverview
                successCriteria={run.data.success_criteria}
                planEvent={planEvent}
                eventCount={eventItems.length}
              />
            ) : null}
            {tab === "agent" ? (
              <AgentConversation
                run={run.data}
                messages={eventProjection.conversationMessages}
                loading={events.isLoading}
              />
            ) : null}
            {tab === "actions" ? (
              <ActionTimeline
                items={visibleActionItems}
                loading={actions.isLoading}
                error={
                  actionAuthorizationError ??
                  (actions.isFetchNextPageError ? null : actions.error)
                }
                paginationError={actions.isFetchNextPageError ? actions.error : null}
                selectedActionId={selectedActionId}
                hasMore={Boolean(actions.hasNextPage)}
                loadingMore={actions.isFetchingNextPage}
                onLoadMore={() => void actions.fetchNextPage()}
                onSelect={selectAction}
              />
            ) : null}
            {tab === "graph" ? (
              <Suspense fallback={<LoadingState label="Loading Graph workspace" />}>
                <RunGraphWorkspace
                  runId={runId}
                  expectedEngagementId={run.data.engagement_id}
                  view={graphViewParam ?? "task"}
                  focusId={graphFocusParam}
                  onViewChange={changeGraphView}
                  onFocusChange={changeGraphFocus}
                  onOpenAction={openGraphAction}
                />
              </Suspense>
            ) : null}
            {tab === "timeline" ? (
              <Timeline items={eventProjection.highLevelTimeline} loading={events.isLoading} />
            ) : null}
            {tab === "raw-events" ? (
              <RawEvents events={eventProjection.rawEvents} loading={events.isLoading} />
            ) : null}
            {tab === "approvals" ? (
              <Approvals
                approvals={approvals.data?.items ?? []}
                loading={approvals.isLoading}
                controls={approvalControls}
                actionable={!isFinal}
              />
            ) : null}
            {tab === "terminal" ? (
              <TerminalPanel runId={runId} initialSessionId={terminalSessionId} />
            ) : null}
            {tab === "artifacts" ? (
              <Artifacts
                artifacts={artifacts.data?.items ?? []}
                loading={artifacts.isLoading}
                controls={artifactControls}
              />
            ) : null}
            {tab === "findings" ? (
              <Findings
                findings={findings.data?.items ?? []}
                loading={findings.isLoading}
                controls={findingControls}
              />
            ) : null}
            {tab === "report" ? (
              <Reports
                reports={reports.data?.items ?? []}
                loading={reports.isLoading}
                reportable={isFinal}
                controls={reportControls}
              />
            ) : null}
          </div>

          {!isFinal && tab === "agent" ? (
            <form className="message-composer" onSubmit={(event) => void submitMessage(event)}>
              <MessageSquareText size={18} />
              <input
                value={currentMessageDraft.message}
                onChange={(event) => {
                  const nextMessage = event.target.value;
                  // Once the user edits, an older response is no longer allowed
                  // to clear or restore this draft.
                  activeMessageSubmissionRef.current = null;
                  setMessageDraft((current) => {
                    const retry =
                      current.runId === runId ? current.retry : readMessageRetry(runId);
                    const keepRetry = retry?.message === nextMessage.trim();
                    if (retry && !keepRetry) {
                      replaceMessageRetryIfMatches(runId, retry, null);
                    }
                    return {
                      runId,
                      message: nextMessage,
                      retry: keepRetry ? retry : null,
                    };
                  });
                }}
                placeholder={t(
                  run.data.started_at
                    ? "Send guidance to the durable Agent session…"
                    : "Tell the Agent what to do first…",
                )}
                aria-label={t("Message to Agent")}
              />
              <button
                className="composer-send"
                type="submit"
                disabled={!currentMessageDraft.message.trim() || controls.message.isPending}
                aria-label={t("Send message")}
              >
                {controls.message.isPending ? (
                  <Loader2 className="spin" size={17} />
                ) : (
                  <Send size={17} />
                )}
              </button>
            </form>
          ) : null}
        </section>

        <aside className="detail-sidebar">
          {selectedActionId ? (
            <ActionInspector
              key={`${runId}:${selectedActionId}`}
              actionId={selectedActionId}
              action={selectedActionData}
              loading={selectedAction.isLoading}
              error={actionAuthorizationError ?? selectedAction.error}
              focusOnOpen={focusInspector}
              onClose={closeActionInspector}
              onOpenGraph={openActionGraph}
            />
          ) : null}
          <article className="panel compact-panel">
            <div className="panel-header">
              <div>
                <span className="panel-kicker">{t("Lifecycle")}</span>
                <h3>{t("Run facts")}</h3>
              </div>
            </div>
            <dl className="fact-list">
              <div>
                <dt>{t("Created")}</dt>
                <dd>{formatTimestamp(run.data.created_at, language)}</dd>
              </div>
              <div>
                <dt>{t("Started")}</dt>
                <dd>{run.data.started_at ? formatTimestamp(run.data.started_at, language) : t("Pending")}</dd>
              </div>
              <div>
                <dt>{t("Workspace")}</dt>
                <dd title={run.data.workspace_path}>{run.data.workspace_path}</dd>
              </div>
              <div>
                <dt>{t("Workflow")}</dt>
                <dd title={run.data.started_at ? (run.data.temporal_workflow_id ?? "") : ""}>
                  {run.data.started_at
                    ? (run.data.temporal_workflow_id ?? t("Not started"))
                    : t("Not started")}
                </dd>
              </div>
            </dl>
          </article>

          <article className="panel compact-panel">
            <div className="panel-header">
              <div>
                <span className="panel-kicker">{t("Boundary")}</span>
                <h3>{t("Scope")}</h3>
              </div>
            </div>
            <div className="scope-list">
              {[
                ...run.data.scope.cidrs,
                ...run.data.scope.ips,
                ...run.data.scope.domains,
                ...run.data.scope.url_prefixes,
              ].map((item) => (
                <span className="mono-chip" key={item}>
                  {item}
                </span>
              ))}
              {!run.data.scope.cidrs.length &&
              !run.data.scope.ips.length &&
              !run.data.scope.domains.length &&
              !run.data.scope.url_prefixes.length ? (
                <span className="muted-caption">{t("No explicit scope values")}</span>
              ) : null}
            </div>
            {run.data.scope.exclusions.length ? (
              <div className="exclusion-list">
                <strong>{t("Exclusions")}</strong>
                {run.data.scope.exclusions.map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            ) : null}
          </article>
        </aside>
      </div>
    </div>
  );
}

function AgentConversation({
  run,
  messages,
  loading,
}: {
  run: Run;
  messages: ConversationMessage[];
  loading: boolean;
}) {
  const { t } = useI18n();
  const positiveScope = [
    ...run.scope.cidrs,
    ...run.scope.ips,
    ...run.scope.domains,
    ...run.scope.url_prefixes,
  ];
  const entryPoints = run.entry_points ?? [];

  return (
    <div className="conversation-view">
      <section
        className="conversation-context"
        aria-label={t("Objective and authorized boundary")}
      >
        <div className="conversation-context-heading">
          <span className="conversation-avatar system-avatar"><ShieldAlert size={18} /></span>
          <div>
            <span className="panel-kicker">{t("Task context")}</span>
            <h3>{t("Objective and authorized boundary")}</h3>
          </div>
        </div>
        <p className="conversation-objective">{run.objective.description}</p>
        <div className="conversation-boundary-grid">
          <div>
            <strong>{t("Entry points")}</strong>
            <div className="conversation-chips">
              {entryPoints.length ? entryPoints.map((entry) => (
                <span className="mono-chip" key={`${entry.kind}:${entry.value}`}>
                  {entry.kind}={entry.value}
                </span>
              )) : <span className="muted-caption">{t("No entry points")}</span>}
            </div>
          </div>
          <div>
            <strong>{t("Authorized scope")}</strong>
            <div className="conversation-chips">
              {positiveScope.length ? positiveScope.map((item) => (
                <span className="mono-chip" key={item}>{item}</span>
              )) : <span className="muted-caption">{t("No explicit scope values")}</span>}
            </div>
          </div>
          {run.scope.exclusions.length ? (
            <div className="conversation-exclusions">
              <strong>{t("Exclusions")}</strong>
              <div className="conversation-chips">
                {run.scope.exclusions.map((item) => <span key={item}>{item}</span>)}
              </div>
            </div>
          ) : null}
        </div>
        {!run.started_at ? (
          <div className="conversation-waiting">
            <Clock3 size={17} />
            <div>
              <strong>{t("Waiting for your first instruction")}</strong>
              <span>{t("No model or tool action will start until you send a specific instruction below.")}</span>
            </div>
          </div>
        ) : null}
      </section>

      {loading ? <LoadingState label="Loading conversation" /> : null}
      {!loading && !messages.length ? (
        <div className="conversation-empty">
          <Bot size={21} />
          <div>
            <strong>{t("What should I do first?")}</strong>
            <span>{t("For example: review the scope, inspect one endpoint, or propose a plan without executing tools.")}</span>
          </div>
        </div>
      ) : null}
      {messages.length ? (
        <div className="conversation-messages">
          {messages.map((item) => (
            <article className={`conversation-message ${item.role}`} key={item.key}>
              <span className="conversation-avatar">
                {item.role === "user" ? <MessageSquareText size={17} /> : <Bot size={17} />}
              </span>
              <div className="conversation-bubble">
                <span className="conversation-role">
                  {item.role === "user" ? t("You") : t("Agent")}
                </span>
                <div className="event-markdown"><ReactMarkdown>{item.content}</ReactMarkdown></div>
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Timeline({ items, loading }: { items: TimelineItem[]; loading: boolean }) {
  const { language, t } = useI18n();
  if (loading) return <LoadingState label="Loading event timeline" />;
  if (!items.length) {
    return (
      <EmptyState icon={Clock3} title="Timeline is empty">
        {t("Durable events appear here as the workflow progresses.")}
      </EmptyState>
    );
  }
  return (
    <div className="timeline">
      {items.map((item) => {
        const eventType =
          item.kind === "event" ? item.event.event_type : "agent.assistant_stream";
        return (
          <article className="timeline-event" key={item.key}>
            <div className="timeline-rail">
              <div className={`event-icon event-${eventFamily(eventType)}`}>
                <EventIcon eventType={eventType} />
              </div>
            </div>
            <div className="event-card">
              <div className="event-header">
                <div>
                  <span className="event-sequence">
                    {formatSequenceRange(item.startSequence, item.endSequence)}
                  </span>
                  <strong>
                    {item.kind === "event"
                      ? t(eventTitle(item.event.event_type))
                      : t("Agent response")}
                  </strong>
                </div>
                <time>{formatTimestamp(item.createdAt, language)}</time>
              </div>
              {item.kind === "event" ? (
                <EventPayload event={item.event} />
              ) : (
                <div className="event-markdown">
                  <ReactMarkdown>{item.content}</ReactMarkdown>
                </div>
              )}
            </div>
          </article>
        );
      })}
    </div>
  );
}

function RawEvents({ events, loading }: { events: RunEvent[]; loading: boolean }) {
  const { t } = useI18n();
  if (loading) return <LoadingState label="Loading raw events" />;
  const visible = events.slice(-200);
  if (!visible.length) {
    return (
      <EmptyState icon={Archive} title="No raw events yet">
        {t("Durable audit events will appear here.")}
      </EmptyState>
    );
  }
  const items: TimelineItem[] = visible.map((event) => ({
    kind: "event",
    key: `raw:${event.id}`,
    event,
    startSequence: event.sequence,
    endSequence: event.sequence,
    createdAt: event.created_at,
  }));
  return (
    <div className="raw-events-view">
      <p className="muted-caption">
        {t("Showing latest {visible} of {loaded} loaded durable events.", {
          visible: visible.length,
          loaded: events.length,
        })}
        {events.length > visible.length
          ? ` ${t("This Raw Events window is partial; older loaded events are hidden.")}`
          : ""}
      </p>
      <Timeline items={items} loading={false} />
    </div>
  );
}

function EventPayload({ event }: { event: RunEvent }) {
  const { t } = useI18n();
  const narrative = [
    event.payload.assistant_message,
    event.payload.message,
    event.payload.summary,
    event.payload.plan_summary,
  ].find((value): value is string => typeof value === "string" && Boolean(value));
  if (narrative) {
    return (
      <div className="event-markdown">
        <ReactMarkdown>{narrative}</ReactMarkdown>
      </div>
    );
  }
  if (!Object.keys(event.payload).length) {
    return <p className="muted-caption">{t("No additional payload.")}</p>;
  }
  return <pre className="event-json">{JSON.stringify(event.payload, null, 2)}</pre>;
}

export function findApprovalToResync(
  runStatus: Run["status"] | undefined,
  events: RunEvent[],
  approvals: Approval[],
): Approval | null {
  if (runStatus !== "waiting_approval") return null;
  const latestYield = events.reduce<RunEvent | undefined>((latest, event) => {
    if (event.event_type !== "runtime.cycle_yielded") return latest;
    return !latest || event.sequence > latest.sequence ? event : latest;
  }, undefined);
  if (latestYield?.payload.yield_reason !== "approval_required") return null;
  const waitingObjectId = latestYield.payload.waiting_object_id;
  if (typeof waitingObjectId !== "string" || !waitingObjectId) return null;
  const approval = approvals.find((item) => item.id === waitingObjectId);
  return approval?.status === "approved" || approval?.status === "rejected"
    ? approval
    : null;
}

function RunOverview({
  successCriteria,
  planEvent,
  eventCount,
}: {
  successCriteria: Array<{ description: string; required: boolean }>;
  planEvent?: RunEvent;
  eventCount: number;
}) {
  const { t } = useI18n();
  return (
    <div className="overview-grid">
      <article className="overview-card">
        <span className="overview-icon">
          <CheckCircle2 size={18} />
        </span>
        <div>
          <span className="panel-kicker">{t("Success criteria")}</span>
          <h3>{successCriteria.length || t("None defined")}</h3>
          {successCriteria.length ? (
            <ul className="criteria-list">
              {successCriteria.map((criterion) => (
                <li key={criterion.description}>
                  <ChevronRight size={14} /> {criterion.description}
                </li>
              ))}
            </ul>
          ) : (
            <p>{t("The Agent will infer completion from the objective.")}</p>
          )}
        </div>
      </article>
      <article className="overview-card">
        <span className="overview-icon">
          <Activity size={18} />
        </span>
        <div>
          <span className="panel-kicker">{t("Durable activity")}</span>
          <h3>{t("{count} events", { count: eventCount })}</h3>
          <p>{t("Every state transition is replayable from the database timeline.")}</p>
        </div>
      </article>
      <article className="overview-card overview-wide">
        <span className="overview-icon">
          <Bot size={18} />
        </span>
        <div>
          <span className="panel-kicker">{t("Latest plan")}</span>
          {planEvent ? (
            <EventPayload event={planEvent} />
          ) : (
            <p>{t("The Agent has not published a plan summary yet.")}</p>
          )}
        </div>
      </article>
    </div>
  );
}

function Findings({
  findings,
  loading,
  controls,
}: {
  findings: Finding[];
  loading: boolean;
  controls: ReturnType<typeof useFindingControl>;
}) {
  const { t } = useI18n();
  const [editingId, setEditingId] = useState<string | null>(null);
  if (loading) return <LoadingState label="Loading findings" />;
  if (!findings.length) {
    return (
      <EmptyState icon={FileWarning} title="No findings yet">
        {t("Evidence-backed findings created by the Agent will appear here.")}
      </EmptyState>
    );
  }
  return (
    <div className="finding-list">
      {findings.map((finding) =>
        editingId === finding.id ? (
          <FindingEditor
            key={finding.id}
            finding={finding}
            saving={controls.update.isPending}
            onCancel={() => setEditingId(null)}
            onSave={async (payload) => {
              await controls.update.mutateAsync({ findingId: finding.id, payload });
              setEditingId(null);
            }}
          />
        ) : (
          <article className="finding-card" key={finding.id}>
            <div className={`severity-marker severity-${finding.severity}`} />
            <div>
              <div className="finding-head">
                <span className={`severity-label severity-${finding.severity}`}>
                  {t(finding.severity)}
                </span>
                <span>{t(finding.status.replaceAll("_", " "))}</span>
                <button
                  className="finding-edit-button"
                  onClick={() => setEditingId(finding.id)}
                  aria-label={t("Edit {title}", { title: finding.title })}
                >
                  <Pencil size={13} /> {t("Edit")}
                </button>
              </div>
              <h3>{finding.title}</h3>
              <p>{finding.description || t("No description supplied.")}</p>
              {finding.affected_assets.length ? (
                <div className="scope-list">
                  {finding.affected_assets.map((asset) => (
                    <span className="mono-chip" key={asset}>
                      {asset}
                    </span>
                  ))}
                </div>
              ) : null}
              <FindingEvidenceList evidence={finding.evidence} />
              {finding.impact || finding.recommendation ? (
                <div className="finding-guidance">
                  {finding.impact ? (
                    <div>
                      <strong>{t("Impact")}</strong>
                      <p>{finding.impact}</p>
                    </div>
                  ) : null}
                  {finding.recommendation ? (
                    <div>
                      <strong>{t("Recommendation")}</strong>
                      <p>{finding.recommendation}</p>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          </article>
        ),
      )}
      {controls.update.error ? <ErrorState error={controls.update.error} /> : null}
    </div>
  );
}

function FindingEvidenceList({ evidence }: { evidence: FindingEvidence[] }) {
  const { t } = useI18n();
  const download = useAuthenticatedDownload();
  if (!evidence.length) return null;
  return (
    <div className="finding-evidence-list">
      {evidence.map((item, index) => (
        <article
          className="finding-evidence"
          key={`${item.artifact_id ?? "artifact"}-${item.execution_id ?? "execution"}-${index}`}
        >
          <div>
            <strong>{t("Evidence {count}", { count: index + 1 })}</strong>
            <span>{item.location || t("No location marker")}</span>
          </div>
          <p>{item.description || t("No evidence description.")}</p>
          <div className="finding-evidence-links">
            {item.artifact_id ? (
              <button
                className="link-button"
                disabled={download.isPending}
                onClick={() => void download.start(
                  api.artifactContentUrlById(item.artifact_id!),
                )}
                type="button"
              >
                <ExternalLink size={12} /> {t("Artifact")} {item.artifact_id}
              </button>
            ) : null}
            {item.execution_id ? <code>{t("Execution")} {item.execution_id}</code> : null}
          </div>
        </article>
      ))}
      {download.error ? <ErrorState error={download.error} /> : null}
    </div>
  );
}

function FindingEditor({
  finding,
  saving,
  onCancel,
  onSave,
}: {
  finding: Finding;
  saving: boolean;
  onCancel: () => void;
  onSave: (payload: {
    title: string;
    severity: Finding["severity"];
    status: Finding["status"];
    affected_assets: string[];
    description: string;
    evidence: FindingEvidence[];
    reproduction_steps: string[];
    impact: string;
    recommendation: string;
  }) => Promise<void>;
}) {
  const { t } = useI18n();
  const [title, setTitle] = useState(finding.title);
  const [severity, setSeverity] = useState(finding.severity);
  const [status, setStatus] = useState(finding.status);
  const [affectedAssets, setAffectedAssets] = useState(finding.affected_assets.join("\n"));
  const [description, setDescription] = useState(finding.description);
  const [evidence, setEvidence] = useState<FindingEvidence[]>(finding.evidence);
  const [reproductionSteps, setReproductionSteps] = useState(
    finding.reproduction_steps.join("\n"),
  );
  const [impact, setImpact] = useState(finding.impact);
  const [recommendation, setRecommendation] = useState(finding.recommendation);

  function updateEvidence(index: number, patch: Partial<FindingEvidence>) {
    setEvidence((current) =>
      current.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    );
  }

  return (
    <form
      className="finding-editor"
      onSubmit={(event) => {
        event.preventDefault();
        if (!title.trim()) return;
        void onSave({
          title: title.trim(),
          severity,
          status,
          affected_assets: splitLines(affectedAssets),
          description,
          evidence,
          reproduction_steps: splitLines(reproductionSteps),
          impact,
          recommendation,
        });
      }}
    >
      <div className="finding-editor-grid">
        <label className="finding-editor-title">
          <span>{t("Title")}</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} required />
        </label>
        <label>
          <span>{t("Severity")}</span>
          <select
            value={severity}
            onChange={(event) => setSeverity(event.target.value as Finding["severity"])}
          >
            {(["info", "low", "medium", "high", "critical"] as const).map((value) => (
              <option key={value} value={value}>
                {t(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t("Status")}</span>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value as Finding["status"])}
          >
            {(["draft", "confirmed", "resolved", "false_positive"] as const).map(
              (value) => (
                <option key={value} value={value}>
                  {t(value.replaceAll("_", " "))}
                </option>
              ),
            )}
          </select>
        </label>
        <label className="finding-editor-wide">
          <span>{t("Description")}</span>
          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>{t("Affected assets · one per line")}</span>
          <textarea
            value={affectedAssets}
            onChange={(event) => setAffectedAssets(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>{t("Reproduction steps · one per line")}</span>
          <textarea
            value={reproductionSteps}
            onChange={(event) => setReproductionSteps(event.target.value)}
            rows={4}
          />
        </label>
        <label>
          <span>{t("Impact")}</span>
          <textarea value={impact} onChange={(event) => setImpact(event.target.value)} rows={4} />
        </label>
        <label>
          <span>{t("Recommendation")}</span>
          <textarea
            value={recommendation}
            onChange={(event) => setRecommendation(event.target.value)}
            rows={4}
          />
        </label>
      </div>

      <div className="finding-evidence-editor">
        <div className="finding-editor-section-head">
          <div>
            <span className="panel-kicker">{t("Evidence links")}</span>
            <h4>{t("Artifacts and executions")}</h4>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={() =>
              setEvidence((current) => [
                ...current,
                {
                  artifact_id: null,
                  execution_id: null,
                  description: "",
                  location: null,
                },
              ])
            }
          >
            <Plus size={14} /> {t("Add evidence")}
          </button>
        </div>
        {evidence.map((item, index) => (
          <div className="finding-evidence-row" key={index}>
            <label>
              <span>{t("Artifact ID")}</span>
              <input
                value={item.artifact_id ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { artifact_id: event.target.value || null })
                }
              />
            </label>
            <label>
              <span>{t("Execution ID")}</span>
              <input
                value={item.execution_id ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { execution_id: event.target.value || null })
                }
              />
            </label>
            <label>
              <span>{t("Location")}</span>
              <input
                value={item.location ?? ""}
                onChange={(event) =>
                  updateEvidence(index, { location: event.target.value || null })
                }
              />
            </label>
            <label className="finding-evidence-description">
              <span>{t("Description")}</span>
              <input
                value={item.description}
                onChange={(event) =>
                  updateEvidence(index, { description: event.target.value })
                }
              />
            </label>
            <button
              className="finding-remove-evidence"
              type="button"
              onClick={() =>
                setEvidence((current) => current.filter((_, itemIndex) => itemIndex !== index))
              }
              aria-label={t("Remove evidence {count}", { count: index + 1 })}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>

      <div className="finding-editor-actions">
        <button className="secondary-button" type="button" onClick={onCancel} disabled={saving}>
          <X size={14} /> {t("Cancel")}
        </button>
        <button className="primary-button" type="submit" disabled={saving || !title.trim()}>
          {saving ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
          {t("Save finding")}
        </button>
      </div>
    </form>
  );
}

function Artifacts({
  artifacts,
  loading,
  controls,
}: {
  artifacts: Artifact[];
  loading: boolean;
  controls: ReturnType<typeof useArtifactControl>;
}) {
  const { t } = useI18n();
  const download = useAuthenticatedDownload();
  const [sourcePath, setSourcePath] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalized = sourcePath.trim();
    if (!normalized) return;
    await controls.register.mutateAsync({
      source_path: normalized,
      ...(name.trim() ? { name: name.trim() } : {}),
      ...(description.trim() ? { description: description.trim() } : {}),
    });
    setSourcePath("");
    setName("");
    setDescription("");
  }

  return (
    <div className="artifact-stack">
      <form className="artifact-register" onSubmit={(event) => void submit(event)}>
        <div>
          <span className="panel-kicker">{t("Immutable evidence")}</span>
          <h3>{t("Register a Run-owned file")}</h3>
          <p>{t("The path must be inside this Run workspace or its Runner state directory.")}</p>
        </div>
        <div className="artifact-register-grid">
          <label className="artifact-source-field">
            <span>{t("Source path")}</span>
            <input
              value={sourcePath}
              onChange={(event) => setSourcePath(event.target.value)}
              placeholder="/path/to/run/workspace/result.xml"
              required
            />
          </label>
          <label>
            <span>{t("Name (optional)")}</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="result.xml"
            />
          </label>
          <label className="artifact-description-field">
            <span>{t("Description (optional)")}</span>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t("What this evidence proves")}
            />
          </label>
          <button
            className="primary-button"
            type="submit"
            disabled={!sourcePath.trim() || controls.register.isPending}
          >
            {controls.register.isPending ? (
              <Loader2 className="spin" size={15} />
            ) : (
              <Plus size={15} />
            )}
            {t("Register")}
          </button>
        </div>
        {controls.register.error ? <ErrorState error={controls.register.error} /> : null}
      </form>

      {loading ? <LoadingState label="Loading artifacts" /> : null}
      {!loading && !artifacts.length ? (
        <EmptyState icon={Archive} title="No artifacts registered">
          {t("Tool outputs, screenshots, logs, and report attachments will appear here.")}
        </EmptyState>
      ) : null}
      {!loading && artifacts.length ? (
        <div className="artifact-list">
          {artifacts.map((artifact) => (
            <article className="artifact-card" key={artifact.id}>
              <span className="artifact-icon">
                <Archive size={18} />
              </span>
              <div className="artifact-main">
                <div className="artifact-head">
                  <h3>{artifact.name}</h3>
                  <span>{formatBytes(artifact.size)}</span>
                </div>
                <p>{artifact.description || t("No description supplied.")}</p>
                <div className="artifact-meta">
                  <span>{artifact.mime_type}</span>
                  <code title={artifact.sha256}>sha256:{artifact.sha256}</code>
                  {artifact.execution_id ? <span>exec / {artifact.execution_id}</span> : null}
                </div>
              </div>
              <button
                className="secondary-button artifact-download"
                disabled={download.isPending}
                onClick={() => void download.start(
                  api.artifactContentUrl(artifact),
                  artifact.name,
                )}
                type="button"
              >
                <Download size={15} /> {t("Download")}
              </button>
            </article>
          ))}
        </div>
      ) : null}
      {download.error ? <ErrorState error={download.error} /> : null}
    </div>
  );
}

function Approvals({
  approvals,
  loading,
  controls,
  actionable,
}: {
  approvals: Approval[];
  loading: boolean;
  controls: ReturnType<typeof useApprovalControl>;
  actionable: boolean;
}) {
  const { language, t } = useI18n();
  const [reasons, setReasons] = useState<Record<string, string>>({});
  if (loading) return <LoadingState label="Loading approvals" />;
  if (!approvals.length) {
    return (
      <EmptyState icon={ShieldAlert} title="No approval requests">
        {t("Sensitive or manually controlled Tool calls will appear here with their exact execution snapshot.")}
      </EmptyState>
    );
  }
  return (
    <div className="approval-list">
      {[...approvals].reverse().map((approval) => {
        const pending = approval.status === "pending";
        const busy = controls.approve.isPending || controls.reject.isPending;
        return (
          <article className={`approval-card approval-${approval.status}`} key={approval.id}>
            <div className="approval-card-head">
              <div>
                <span className="panel-kicker">{t(approval.status.replaceAll("_", " "))}</span>
                <h3>{approval.tool_name}</h3>
              </div>
              <span className="mono-chip">{approval.id}</span>
            </div>
            <dl className="approval-facts">
              <div>
                <dt>{t("Command")}</dt>
                <dd><code>{approval.command.join(" ") || t("No command snapshot")}</code></dd>
              </div>
              <div>
                <dt>{t("Working directory")}</dt>
                <dd><code>{approval.cwd || "—"}</code></dd>
              </div>
              <div>
                <dt>{t("Target")}</dt>
                <dd>{approval.target_summary || "—"}</dd>
              </div>
              <div>
                <dt>{t("Environment changes")}</dt>
                <dd>
                  {Object.keys(approval.env_diff).length ? (
                    <pre>{JSON.stringify(approval.env_diff, null, 2)}</pre>
                  ) : t("None")}
                </dd>
              </div>
              <div>
                <dt>{t("Agent reason")}</dt>
                <dd>{approval.reason || t("No reason supplied.")}</dd>
              </div>
            </dl>
            {pending && actionable ? (
              <div className="approval-actions">
                <textarea
                  value={reasons[approval.id] ?? ""}
                  onChange={(event) =>
                    setReasons((current) => ({ ...current, [approval.id]: event.target.value }))
                  }
                  placeholder={t("Optional rejection reason…")}
                  aria-label={t("Rejection reason for {tool}", { tool: approval.tool_name })}
                  rows={2}
                />
                <div>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.reject.mutate({
                        approvalId: approval.id,
                        payload: { reason: reasons[approval.id]?.trim() || null },
                      })
                    }
                  >
                    <Ban size={15} /> {t("Reject")}
                  </button>
                  <button
                    className="secondary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.approve.mutate({ approvalId: approval.id })
                    }
                  >
                    <CheckCircle2 size={15} /> {t("Approve once")}
                  </button>
                  <button
                    className="primary-button"
                    disabled={busy}
                    onClick={() =>
                      controls.approve.mutate({
                        approvalId: approval.id,
                        payload: { approve_for_run: true },
                      })
                    }
                  >
                    <ShieldAlert size={15} /> {t("Approve for Run")}
                  </button>
                </div>
              </div>
            ) : pending ? (
              <p className="approval-decision">
                {t("This Run has ended; the pending approval can no longer be decided.")}
              </p>
            ) : (
              <p className="approval-decision">
                {t("Decided by {name}", { name: approval.decided_by ?? t("unknown") })}
                {approval.decided_at ? ` · ${formatTimestamp(approval.decided_at, language)}` : ""}
              </p>
            )}
          </article>
        );
      })}
      {controls.approve.error || controls.reject.error ? (
        <ErrorState error={controls.approve.error ?? controls.reject.error ?? new Error()} />
      ) : null}
    </div>
  );
}

function Reports({
  reports,
  loading,
  reportable,
  controls,
}: {
  reports: Report[];
  loading: boolean;
  reportable: boolean;
  controls: ReturnType<typeof useReportControl>;
}) {
  const { language, t } = useI18n();
  const download = useAuthenticatedDownload();
  if (loading) return <LoadingState label="Loading generated reports" />;
  const grouped = [...reports].sort(
    (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
  );
  return (
    <div className="report-stack">
      <div className="section-toolbar">
        <div>
          <span className="panel-kicker">{t("Restricted source · immutable output")}</span>
          <h3>{t("Run reports")}</h3>
          <p>
            {reportable
              ? t("Generate Markdown, HTML, and JSON from findings, artifact summaries, and key activity only.")
              : t("Report generation unlocks after the Run reaches a final status.")}
          </p>
        </div>
        <button
          className="primary-button"
          disabled={!reportable || controls.generate.isPending}
          onClick={() => controls.generate.mutate(undefined)}
        >
          {controls.generate.isPending ? (
            <Loader2 className="spin" size={16} />
          ) : (
            <FileText size={16} />
          )}
          {t("Generate reports")}
        </button>
      </div>
      {controls.generate.error ? <ErrorState error={controls.generate.error} /> : null}
      {!grouped.length ? (
        <EmptyState icon={FileText} title="No reports generated yet">
          {t("Generate a report set now, or let the durable workflow create one after Agent completion.")}
        </EmptyState>
      ) : (
        <div className="report-grid">
          {grouped.map((report) => (
            <article className="report-card" key={report.id}>
              <div className="report-format">
                <FileText size={19} />
                <strong>{report.format.toUpperCase()}</strong>
              </div>
              <p>{t(report.finding_ids.length === 1 ? "{count} linked finding" : "{count} linked findings", { count: report.finding_ids.length })}</p>
              <code>{report.id}</code>
              <span>{formatTimestamp(report.created_at, language)}</span>
              <button
                className="secondary-button report-open-button"
                disabled={download.isPending}
                onClick={() => void download.start(
                  api.artifactContentUrl({ content_url: report.content_url }),
                )}
                type="button"
              >
                <ExternalLink size={14} /> {t("Open report")}
              </button>
            </article>
          ))}
        </div>
      )}
      {download.error ? <ErrorState error={download.error} /> : null}
    </div>
  );
}

function useAuthenticatedDownload() {
  const { t } = useI18n();
  const [error, setError] = useState<Error | null>(null);
  const [isPending, setIsPending] = useState(false);

  async function start(path: string, filename?: string) {
    setError(null);
    setIsPending(true);
    try {
      await api.downloadAuthenticatedUrl(path, filename);
    } catch (cause) {
      const message =
        cause instanceof RiftXAPIError && cause.code === "download_too_large"
          ? t("Download blocked because it exceeds the {size} safety limit.", {
              size: `${AUTHENTICATED_DOWNLOAD_LIMIT_MIB} MiB`,
            })
          : t("Download failed. Please try again.");
      setError(
        cause instanceof RiftXAPIError
          ? new RiftXAPIError(cause.status, cause.code, message, cause.details)
          : new Error(message),
      );
    } finally {
      setIsPending(false);
    }
  }

  return { error, isPending, start };
}

function parseGraphView(value: string | null): GraphViewKind | null {
  return value === "task" || value === "evidence" || value === "operation"
    ? value
    : null;
}

function detailTabForRoute(
  actionId: string,
  graphView: GraphViewKind | null,
  graphFocus: string,
): DetailTab {
  if (graphView || graphFocus) return "graph";
  if (actionId) return "actions";
  return "agent";
}

function EventIcon({ eventType }: { eventType: string }) {
  if (eventType.includes("failed")) return <AlertTriangle size={16} />;
  if (eventType.startsWith("agent.")) return <Bot size={16} />;
  if (eventType.includes("tool")) return <Wrench size={16} />;
  if (eventType.includes("approval")) return <ShieldAlert size={16} />;
  if (eventType.includes("execution") || eventType.startsWith("terminal.")) {
    return <TerminalSquare size={16} />;
  }
  if (eventType.startsWith("user.")) return <MessageSquareText size={16} />;
  return <Activity size={16} />;
}

function eventFamily(eventType: string) {
  if (eventType.includes("failed")) return "failed";
  if (eventType.startsWith("agent.")) return "agent";
  if (
    eventType.includes("tool") ||
    eventType.includes("execution") ||
    eventType.startsWith("terminal.")
  ) {
    return "tool";
  }
  if (eventType.startsWith("user.")) return "user";
  return "run";
}

function eventTitle(eventType: string) {
  return eventType
    .split(".")
    .map((part) => part.replaceAll("_", " "))
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" · ");
}

function formatSequenceRange(start: number, end: number) {
  return start === end ? `#${start}` : `#${start}–#${end}`;
}

function formatTimestamp(value: string, language: Language = "en") {
  return new Intl.DateTimeFormat(language, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let amount = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
}

function splitLines(value: string) {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}
