import {
  type InfiniteData,
  type QueryClient,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  FileKey2,
  Loader2,
  Network,
  RefreshCw,
  Route,
  ShieldCheck,
} from "lucide-react";
import {
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, RiftXAPIError } from "../api/client";
import type { TrafficViewKind } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useI18n } from "../i18n";

const TRAFFIC_PAGE_SIZE = 50;
const TRAFFIC_QUERY_RETRY_LIMIT = 1;
const TRAFFIC_QUERY_ROOT = "run-target-http-exchanges";
const SAFE_TOKEN_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const SAFE_TRAFFIC_MEDIA_TYPES = new Set([
  "application/json",
  "application/octet-stream",
  "application/pdf",
  "application/xml",
  "application/zip",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/svg+xml",
  "image/webp",
  "text/css",
  "text/csv",
  "text/html",
  "text/plain",
  "text/xml",
]);
const EMPTY_TRAFFIC_PAGES: readonly TrafficPage[] = [];

type TrafficAvailability = "available" | "unavailable";

type TrafficScope = {
  run_id: string;
  engagement_id: string;
};

type TrafficSnapshot = {
  id: string;
  created_through: string | null;
  stale: false;
};

type TrafficLineage = {
  run_id: string;
  session_id: string;
  tool_call_id: string;
  node_id: string;
  node_status: string;
};

type TrafficUrlSummary = {
  availability: TrafficAvailability;
  scheme: "http" | "https" | null;
  origin: string | null;
  path_shape: string | null;
  path_segment_count: number | null;
  redacted: true;
};

type TrafficResponseSummary = {
  status_code: number;
  status_class: string;
  elapsed_ms: number;
  content_type: string | null;
  content_length: number | null;
  truncated: boolean;
};

type TrafficArtifactSummary = {
  opaque_ref: string | null;
  presence: string;
  access: string;
};

type TrafficBodySummary = {
  availability: string;
  revealable: false;
  truncated: boolean;
};

type TrafficTlsSummary = {
  availability: "available" | "not_applicable" | "unavailable";
  verified: boolean | null;
  client_certificate_used: boolean | null;
};

type TrafficRedirectSummary = {
  availability: TrafficAvailability;
  count: number | null;
  followed: boolean | null;
  origins: string[];
  partial: boolean;
};

type TrafficReplaySummary = {
  availability: "unavailable";
  request_id: null;
  reason: "not_persisted";
};

type TrafficCreatedBy = {
  availability: TrafficAvailability;
  kind: "agent_runtime" | "unknown";
};

type TrafficScopeDecision = {
  availability: "unavailable";
  decision: null;
  reference_kind: "run_scope";
  reason: "decision_not_persisted";
};

type TrafficApprovalSummary = {
  availability: "available" | "not_required" | "unavailable";
  reference_id: string | null;
  status: string | null;
};

type TrafficSafetyGateSummary = {
  availability: "unavailable";
  reference_id: null;
  reason: "not_implemented";
};

type TrafficGovernance = {
  sensitivity: string;
  access: string;
  retention: string;
  reveal_capability: string;
};

type TrafficItem = {
  exchange_id: string;
  request_id: string;
  execution_key: string;
  canonical_request_digest: string;
  digest_stability: string;
  lineage: TrafficLineage;
  method: string;
  url_summary: TrafficUrlSummary;
  tls: TrafficTlsSummary;
  response: TrafficResponseSummary;
  artifacts: {
    request: TrafficArtifactSummary;
    response: TrafficArtifactSummary;
  };
  body: {
    request: TrafficBodySummary;
    response: TrafficBodySummary;
  };
  redirect: TrafficRedirectSummary;
  replay_of: TrafficReplaySummary;
  created_by: TrafficCreatedBy;
  created_at: string;
  scope_decision: TrafficScopeDecision;
  approval: TrafficApprovalSummary;
  safety_gate: TrafficSafetyGateSummary;
  governance: TrafficGovernance;
  projection_quality: string;
  partial_reasons: string[];
};

type TrafficPage = {
  scope: TrafficScope;
  snapshot: TrafficSnapshot;
  items: TrafficItem[];
  truncated: boolean;
  has_more: boolean;
  next_cursor: string | null;
  partial: boolean;
  partial_reasons: string[];
};

type TrafficDetail = {
  scope: TrafficScope;
  item: TrafficItem;
};

type TrafficIntegrity = {
  issues: string[];
  ok: boolean;
  scopeMismatch: boolean;
};

type TrafficAuthLatch = {
  latestEpoch: number;
  rejectionFence: number;
  settledEpoch: number;
  error: RiftXAPIError | null;
};

type ValidationGate = {
  identity: string;
  validated: boolean;
};

type PendingRowFocus = {
  exchangeId: string;
  revalidationObserved: boolean;
  runId: string;
};

export interface RunTrafficWorkspaceProps {
  runId: string;
  expectedEngagementId: string;
  view: TrafficViewKind;
  exchangeId: string;
  onViewChange: (view: TrafficViewKind) => void;
  onExchangeChange: (exchangeId: string) => void;
}

const trafficAuthLatches = new WeakMap<QueryClient, Map<string, TrafficAuthLatch>>();

export function RunTrafficWorkspace({
  runId,
  expectedEngagementId,
  view,
  exchangeId,
  onViewChange,
  onExchangeChange,
}: RunTrafficWorkspaceProps) {
  const { language, t } = useI18n();
  const queryClient = useQueryClient();
  const scopeKey = `${runId}:${expectedEngagementId}`;
  const validExchangeId = isTrafficId(exchangeId) ? exchangeId : "";
  const selectionInvalid = Boolean(exchangeId && !validExchangeId);
  const detailIdentity = `${scopeKey}:${view}:${validExchangeId}`;
  const [historyGate, setHistoryGate] = useState<ValidationGate>({
    identity: scopeKey,
    validated: false,
  });
  const [detailGate, setDetailGate] = useState<ValidationGate>({
    identity: detailIdentity,
    validated: false,
  });
  const previousRunIdRef = useRef(runId);
  const previousRouteRef = useRef({ exchangeId, runId });
  const routeIdentityRef = useRef(`${view}:${exchangeId}`);
  const rowRefs = useRef(new Map<string, HTMLButtonElement>());
  const viewTabRefs = useRef(new Map<TrafficViewKind, HTMLButtonElement>());
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const focusedInspectorRef = useRef("");
  const pendingRowFocusRef = useRef<PendingRowFocus | null>(null);

  useEffect(() => {
    if (historyGate.identity !== scopeKey) {
      setHistoryGate({ identity: scopeKey, validated: false });
    }
  }, [historyGate.identity, scopeKey]);

  useEffect(() => {
    if (detailGate.identity !== detailIdentity) {
      setDetailGate({ identity: detailIdentity, validated: false });
    }
  }, [detailGate.identity, detailIdentity]);

  useEffect(() => {
    const previousRunId = previousRunIdRef.current;
    if (previousRunId !== runId) {
      queryClient.removeQueries({ queryKey: trafficRootKey(previousRunId) });
      clearTrafficAuthLatch(queryClient, previousRunId);
      rowRefs.current.clear();
      focusedInspectorRef.current = "";
      pendingRowFocusRef.current = null;
      previousRunIdRef.current = runId;
    }
  }, [queryClient, runId]);

  const history = useInfiniteQuery({
    queryKey: trafficHistoryKey(runId, expectedEngagementId),
    queryFn: async ({ pageParam, signal }) => {
      const requestEpoch = beginTrafficAuthRequest(queryClient, runId);
      try {
        const raw = await api.listRunTargetHttpExchanges(
          runId,
          { limit: TRAFFIC_PAGE_SIZE, cursor: pageParam ?? undefined },
          signal,
        );
        const response = validateTrafficPageContract(raw);
        if (!resolveTrafficAuthRequest(queryClient, runId, requestEpoch)) {
          throw staleTrafficAuthorizationEpoch();
        }
        return response;
      } catch (error) {
        handleTrafficRequestFailure(queryClient, runId, requestEpoch, error);
        throw error;
      }
    },
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage: TrafficPage) =>
      lastPage.has_more ? lastPage.next_cursor : null,
    enabled: Boolean(runId),
    refetchOnMount: "always",
    retry: retryTrafficQuery,
  });

  const detail = useQuery({
    queryKey: trafficDetailKey(runId, expectedEngagementId, validExchangeId),
    queryFn: async ({ signal }) => {
      const requestEpoch = beginTrafficAuthRequest(queryClient, runId);
      try {
        const raw = await api.getRunTargetHttpExchange(runId, validExchangeId, signal);
        const response = validateTrafficDetailContract(raw);
        if (!resolveTrafficAuthRequest(queryClient, runId, requestEpoch)) {
          throw staleTrafficAuthorizationEpoch();
        }
        return response;
      } catch (error) {
        handleTrafficRequestFailure(queryClient, runId, requestEpoch, error);
        throw error;
      }
    },
    enabled: Boolean(runId && validExchangeId && view === "inspector"),
    refetchOnMount: "always",
    retry: retryTrafficQuery,
  });

  // The shared latch is epoch-aware. Reading a query's raw error here would
  // let an older 403 mask a newer successful list/detail request.
  const authorizationError = readTrafficAuthError(queryClient, runId);
  const historyContractError = trafficContractError(history.error);
  const detailContractError = trafficContractError(detail.error);
  const previousAuthorizationErrorRef = useRef<RiftXAPIError | null>(null);

  useEffect(() => {
    const previous = previousAuthorizationErrorRef.current;
    previousAuthorizationErrorRef.current = authorizationError;
    if (authorizationError) {
      setHistoryGate({ identity: scopeKey, validated: false });
      setDetailGate({ identity: detailIdentity, validated: false });
      return;
    }
    if (previous && validExchangeId && view === "inspector") {
      setDetailGate({ identity: detailIdentity, validated: false });
      void detail.refetch();
    }
  }, [
    authorizationError,
    detail.refetch,
    detailIdentity,
    scopeKey,
    validExchangeId,
    view,
  ]);

  useEffect(() => {
    if (
      !authorizationError &&
      !historyContractError &&
      history.isSuccess &&
      history.isFetchedAfterMount &&
      !history.isFetching
    ) {
      setHistoryGate({ identity: scopeKey, validated: true });
    }
  }, [
    authorizationError,
    history.isFetchedAfterMount,
    history.isFetching,
    history.isSuccess,
    historyContractError,
    scopeKey,
  ]);

  useEffect(() => {
    if (
      validExchangeId &&
      view === "inspector" &&
      !authorizationError &&
      !detailContractError &&
      detail.isSuccess &&
      detail.isFetchedAfterMount &&
      !detail.isFetching
    ) {
      setDetailGate({ identity: detailIdentity, validated: true });
    }
  }, [
    authorizationError,
    detail.isFetchedAfterMount,
    detail.isFetching,
    detail.isSuccess,
    detailContractError,
    detailIdentity,
    validExchangeId,
    view,
  ]);

  useEffect(() => {
    const currentRouteIdentity = `${view}:${exchangeId}`;
    if (routeIdentityRef.current === currentRouteIdentity) return;
    routeIdentityRef.current = currentRouteIdentity;
    setHistoryGate({ identity: scopeKey, validated: false });
    void history.refetch();
  }, [exchangeId, history.refetch, scopeKey, view]);

  const historyAuthorized =
    historyGate.identity === scopeKey &&
    historyGate.validated &&
    !authorizationError &&
    !historyContractError;
  const allPages = history.data?.pages ?? EMPTY_TRAFFIC_PAGES;
  const integrity = useMemo(
    () => validateTrafficPages(allPages, runId, expectedEngagementId),
    [allPages, expectedEngagementId, runId],
  );
  const pages = historyAuthorized && integrity.ok ? allPages : EMPTY_TRAFFIC_PAGES;
  const items = useMemo(() => pages.flatMap((page) => page.items), [pages]);
  const detailNotFound = trafficDetailNotFound(detail.error);
  const detailAuthorized =
    detailGate.identity === detailIdentity &&
    detailGate.validated &&
    !authorizationError &&
    !detailContractError &&
    !detailNotFound;
  const detailScopeMatches =
    detail.data?.scope.run_id === runId &&
    detail.data.scope.engagement_id === expectedEngagementId;
  const selectedDetail =
    detailAuthorized &&
    detailScopeMatches &&
    detail.data?.item.exchange_id === validExchangeId &&
    detail.data.item.lineage.run_id === runId
      ? detail.data.item
      : null;

  const pagePartialReasons = uniqueStrings(
    pages.flatMap((page) => page.partial_reasons),
  );
  const partial = pages.some((page) => page.partial);
  const truncated = pages.some((page) => page.truncated);
  const snapshotStale = pages.some((page) => page.snapshot.stale);
  const hasMore = historyAuthorized && integrity.ok && Boolean(history.hasNextPage);
  const historyFatalError =
    authorizationError ??
    historyContractError ??
    (!historyAuthorized && history.error ? history.error : null);
  const historyRefetchError =
    historyAuthorized &&
    history.isRefetchError &&
    !history.isFetchNextPageError &&
    !trafficCursorError(history.error)
      ? history.error
      : null;
  const detailRefetchError =
    detailAuthorized && detail.isRefetchError && !detailNotFound
      ? detail.error
      : null;

  useEffect(() => {
    const previous = previousRouteRef.current;
    if (previous.runId === runId && previous.exchangeId && !exchangeId) {
      pendingRowFocusRef.current = {
        exchangeId: previous.exchangeId,
        revalidationObserved: false,
        runId,
      };
    } else {
      pendingRowFocusRef.current = null;
    }
    previousRouteRef.current = { exchangeId, runId };
  }, [exchangeId, runId]);

  useEffect(() => {
    const pending = pendingRowFocusRef.current;
    if (!pending || pending.runId !== runId || exchangeId) return;
    if (!historyAuthorized || !integrity.ok) {
      pending.revalidationObserved = true;
      return;
    }
    if (!pending.revalidationObserved) return;
    const trigger = rowRefs.current.get(pending.exchangeId);
    if (!trigger?.isConnected) return;
    pendingRowFocusRef.current = null;
    trigger.focus();
  }, [exchangeId, historyAuthorized, integrity.ok, items, runId]);

  useEffect(() => {
    const identity = selectedDetail ? `${runId}:${selectedDetail.exchange_id}` : "";
    if (!identity) {
      focusedInspectorRef.current = "";
      return undefined;
    }
    if (focusedInspectorRef.current === identity) return undefined;
    focusedInspectorRef.current = identity;
    const timer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [runId, selectedDetail]);

  useEffect(() => {
    if (view !== "inspector" || !exchangeId) return undefined;
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      onExchangeChange("");
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [exchangeId, onExchangeChange, view]);

  function restartHistory() {
    setHistoryGate({ identity: scopeKey, validated: false });
    void queryClient.resetQueries({
      queryKey: trafficHistoryKey(runId, expectedEngagementId),
      exact: true,
    });
  }

  function moveViewFocus(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const views: readonly TrafficViewKind[] = ["history", "inspector"];
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % views.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + views.length) % views.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = views.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextView = views[nextIndex]!;
    onViewChange(nextView);
    viewTabRefs.current.get(nextView)?.focus();
  }

  function moveRowFocus(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = Math.min(index + 1, items.length - 1);
    if (event.key === "ArrowUp") nextIndex = Math.max(index - 1, 0);
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex === null || nextIndex < 0) return;
    event.preventDefault();
    rowRefs.current.get(items[nextIndex]!.exchange_id)?.focus();
  }

  return (
    <section className={`run-traffic-workspace view-${view}`} aria-labelledby="run-traffic-heading">
      <div className="traffic-workspace-heading">
        <div>
          <span className="panel-kicker">{t("Metadata only · read-only")}</span>
          <h3 id="run-traffic-heading">{t("Target HTTP traffic")}</h3>
        </div>
        <button
          className="secondary-button traffic-refresh-button"
          type="button"
          disabled={history.isRefetching}
          aria-label={t("Refresh Traffic metadata")}
          onClick={() => void history.refetch()}
        >
          {history.isRefetching ? (
            <Loader2 className="spin" size={15} />
          ) : (
            <RefreshCw size={15} />
          )}
          {t("Refresh")}
        </button>
      </div>

      <div className="traffic-view-tabs" role="tablist" aria-label={t("Traffic views")}>
        {(["history", "inspector"] as const).map((candidate, index) => (
          <button
            key={candidate}
            ref={(node) => {
              if (node) viewTabRefs.current.set(candidate, node);
              else viewTabRefs.current.delete(candidate);
            }}
            type="button"
            role="tab"
            aria-selected={view === candidate}
            tabIndex={view === candidate ? 0 : -1}
            className={view === candidate ? "active" : ""}
            onClick={() => onViewChange(candidate)}
            onKeyDown={(event) => moveViewFocus(event, index)}
          >
            {t(candidate === "history" ? "History" : "Inspector")}
          </button>
        ))}
      </div>

      <div className="traffic-boundary-note" role="note">
        <ShieldCheck size={17} />
        <span>
          {t(
            "Headers, cookies, authorization, client certificates, and bodies are never loaded in this metadata view.",
          )}
        </span>
      </div>

      {historyFatalError ? <ErrorState error={historyFatalError} /> : null}
      {!historyFatalError && !historyAuthorized ? (
        <LoadingState label="Revalidating Traffic access" />
      ) : null}
      {historyAuthorized && !integrity.ok ? (
        <div className="traffic-integrity-alert" role="alert">
          <AlertTriangle size={18} />
          <div>
            <strong>
              {t(integrity.scopeMismatch ? "Traffic response scope mismatch" : "Traffic integrity check failed")}
            </strong>
            <span>
              {t("RiftX hid the complete metadata batch instead of mixing unverified Exchange records.")}
            </span>
            <ul>
              {integrity.issues.map((issue) => <li key={issue}><code>{issue}</code></li>)}
            </ul>
          </div>
          <button className="secondary-button" type="button" onClick={restartHistory}>
            <RefreshCw size={15} /> {t("Restart Traffic snapshot")}
          </button>
        </div>
      ) : null}

      {historyAuthorized && integrity.ok ? (
        <>
          {historyRefetchError ? (
            <div className="traffic-stale-alert" role="alert">
              <AlertTriangle size={18} />
              <div>
                <strong>{t("Traffic refresh failed; showing last verified metadata")}</strong>
                <span>{t("Retry to request a newer metadata snapshot.")}</span>
              </div>
              <button
                className="secondary-button"
                type="button"
                disabled={history.isRefetching}
                onClick={() => void history.refetch()}
              >
                <RefreshCw size={15} /> {t("Retry Traffic refresh")}
              </button>
            </div>
          ) : null}

          {partial || truncated || snapshotStale || pagePartialReasons.length ? (
            <div className="traffic-quality-alert" role="alert">
              <AlertTriangle size={18} />
              <div>
                <strong>
                  {t(
                    truncated
                      ? "Traffic metadata is truncated"
                      : snapshotStale
                        ? "Traffic metadata snapshot is stale"
                        : "Traffic metadata is partial",
                  )}
                </strong>
                <span>
                  {t("Unavailable legacy fields stay unavailable; the browser never recovers them from raw Events or Artifacts.")}
                </span>
                {pagePartialReasons.length ? (
                  <ul>
                    {pagePartialReasons.map((reason) => <li key={reason}><code>{reason}</code></li>)}
                  </ul>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="traffic-content-grid">
            <section className="traffic-history-panel" aria-labelledby="traffic-history-heading">
              <div className="traffic-panel-heading">
                <div>
                  <span className="panel-kicker">{t("Stable metadata pagination")}</span>
                  <h4 id="traffic-history-heading">{t("Exchange History")}</h4>
                </div>
                <span>{t("{count} loaded", { count: items.length })}</span>
              </div>

              {!items.length ? (
                <EmptyState icon={Network} title="No Target HTTP exchanges">
                  {t("Metadata appears here after an authorized Target HTTP execution is persisted.")}
                </EmptyState>
              ) : (
                <ol className="traffic-history-list" aria-label={t("Target HTTP Exchange History")}>
                  {items.map((item, index) => {
                    const selected = item.exchange_id === validExchangeId;
                    return (
                      <li key={item.exchange_id}>
                        <button
                          ref={(node) => {
                            if (node) rowRefs.current.set(item.exchange_id, node);
                            else rowRefs.current.delete(item.exchange_id);
                          }}
                          type="button"
                          className={selected ? "selected" : ""}
                          aria-current={selected ? "true" : undefined}
                          aria-label={t("Inspect Exchange {id}", { id: item.exchange_id })}
                          tabIndex={selected || (!validExchangeId && index === 0) ? 0 : -1}
                          onClick={() => onExchangeChange(item.exchange_id)}
                          onKeyDown={(event) => moveRowFocus(event, index)}
                        >
                          <span className="traffic-method">{item.method}</span>
                          <span className="traffic-history-summary">
                            <strong>{trafficUrlLabel(item.url_summary, t)}</strong>
                            <small>
                              {trafficResponseLabel(item, t)} · {formatTimestamp(item.created_at, language)}
                            </small>
                            <code>{item.exchange_id}</code>
                          </span>
                          <span className={`traffic-quality ${item.projection_quality}`}>
                            {t(metadataLabel(item.projection_quality))}
                          </span>
                          <ArrowRight size={15} aria-hidden="true" />
                        </button>
                      </li>
                    );
                  })}
                </ol>
              )}

              {trafficCursorError(history.error) ? (
                <div className="traffic-pagination-alert" role="alert">
                  <AlertTriangle size={17} />
                  <div>
                    <strong>{t("Traffic snapshot changed or its cursor was rejected")}</strong>
                    <span>{t("Restart pagination to avoid mixing metadata snapshots.")}</span>
                  </div>
                  <button className="secondary-button" type="button" onClick={restartHistory}>
                    <RefreshCw size={15} /> {t("Restart Traffic snapshot")}
                  </button>
                </div>
              ) : history.isFetchNextPageError && history.error ? (
                <div className="traffic-pagination-alert"><ErrorState error={history.error} /></div>
              ) : null}

              {hasMore && !trafficCursorError(history.error) ? (
                <button
                  className="secondary-button traffic-load-more"
                  type="button"
                  disabled={history.isFetchingNextPage}
                  onClick={() => void history.fetchNextPage()}
                >
                  {history.isFetchingNextPage ? (
                    <Loader2 className="spin" size={15} />
                  ) : (
                    <ArrowRight size={15} />
                  )}
                  {t("Load more Exchanges")}
                </button>
              ) : null}
            </section>

            <section className="traffic-inspector-panel" aria-labelledby="traffic-inspector-heading">
              <div className="traffic-panel-heading">
                <div>
                  <span className="panel-kicker">{t("Allowlisted metadata")}</span>
                  <h4 id="traffic-inspector-heading">{t("Exchange Inspector")}</h4>
                </div>
                {view === "inspector" && exchangeId ? (
                  <button
                    ref={closeButtonRef}
                    className="icon-button"
                    type="button"
                    aria-label={t("Close Exchange Inspector")}
                    onClick={() => onExchangeChange("")}
                  >
                    <ArrowLeft size={16} />
                  </button>
                ) : null}
              </div>

              {selectionInvalid ? (
                <div className="traffic-detail-alert" role="alert">
                  <AlertTriangle size={18} />
                  <div>
                    <strong>{t("Invalid Exchange identity")}</strong>
                    <span>{t("No detail request was sent for this URL value.")}</span>
                  </div>
                </div>
              ) : view !== "inspector" || !validExchangeId ? (
                <EmptyState icon={FileKey2} title="No Exchange selected">
                  {t("Choose an Exchange from History to inspect only its server-approved metadata.")}
                </EmptyState>
              ) : authorizationError ? (
                <ErrorState error={authorizationError} />
              ) : detailNotFound ? (
                <div className="traffic-detail-alert" role="alert">
                  <AlertTriangle size={18} />
                  <div>
                    <strong>{t("Exchange metadata not found")}</strong>
                    <span>{t("The identity is unavailable in this Run; no substitute record was selected.")}</span>
                  </div>
                </div>
              ) : detailContractError ? (
                <ErrorState error={detailContractError} />
              ) : !detailAuthorized ? (
                detail.error ? <ErrorState error={detail.error} /> : <LoadingState label="Loading Exchange metadata" />
              ) : selectedDetail ? (
                <>
                  {detailRefetchError ? (
                    <div className="traffic-stale-alert" role="alert">
                      <AlertTriangle size={17} />
                      <div>
                        <strong>{t("Exchange refresh failed; showing last verified metadata")}</strong>
                        <span>{t("The Inspector is stale until a metadata refresh succeeds.")}</span>
                      </div>
                    </div>
                  ) : null}
                  <TrafficInspector item={selectedDetail} />
                </>
              ) : (
                <div className="traffic-detail-alert" role="alert">
                  <AlertTriangle size={18} />
                  <div>
                    <strong>{t("Exchange response identity mismatch")}</strong>
                    <span>{t("RiftX hid metadata that does not match this Run and URL selection.")}</span>
                  </div>
                </div>
              )}
            </section>
          </div>
        </>
      ) : null}
    </section>
  );
}

function TrafficInspector({ item }: { item: TrafficItem }) {
  const { language, t } = useI18n();
  const decisionRows = [
    ["Scope decision", item.scope_decision],
    ["Approval reference", item.approval],
    ["Safety Gate reference", item.safety_gate],
  ] as const;
  return (
    <article className="traffic-inspector" aria-label={t("Selected Exchange metadata")}>
      <header>
        <div>
          <span className="traffic-method">{item.method}</span>
          <strong>{trafficUrlLabel(item.url_summary, t)}</strong>
        </div>
        <span className={`traffic-quality ${item.projection_quality}`}>
          {t(metadataLabel(item.projection_quality))}
        </span>
      </header>

      {item.partial_reasons.length ? (
        <div className="traffic-item-partial" role="alert">
          <AlertTriangle size={16} />
          <div>
            <strong>{t("This Exchange projection is partial")}</strong>
            <ul>{item.partial_reasons.map((reason) => <li key={reason}><code>{reason}</code></li>)}</ul>
          </div>
        </div>
      ) : null}

      <section aria-labelledby="traffic-request-metadata">
        <h5 id="traffic-request-metadata">{t("Request metadata")}</h5>
        <dl className="traffic-fact-list">
          <Fact label={t("Exchange ID")} value={item.exchange_id} code />
          <Fact label={t("Request ID")} value={item.request_id} code />
          <Fact label={t("Execution key")} value={item.execution_key} code />
          <Fact label={t("Canonical request digest")} value={item.canonical_request_digest} code />
          <Fact label={t("Digest stability")} value={t(metadataLabel(item.digest_stability))} />
          <Fact label={t("URL availability")} value={t(metadataLabel(item.url_summary.availability))} />
          <Fact label={t("Scheme")} value={item.url_summary.scheme ?? t("Unavailable")} />
          <Fact label={t("Origin summary")} value={item.url_summary.origin ?? t("Unavailable")} code />
          <Fact label={t("Path shape")} value={item.url_summary.path_shape ?? t("Unavailable")} code />
          <Fact
            label={t("Path segments")}
            value={item.url_summary.path_segment_count?.toString() ?? t("Unavailable")}
          />
        </dl>
      </section>

      <section aria-labelledby="traffic-response-metadata">
        <h5 id="traffic-response-metadata">{t("Response metadata")}</h5>
        <dl className="traffic-fact-list">
          <Fact
            label={t("HTTP status")}
            value={item.response.status_code.toString()}
          />
          <Fact label={t("Status class")} value={t(metadataLabel(item.response.status_class))} />
          <Fact
            label={t("Elapsed")}
            value={`${item.response.elapsed_ms} ms`}
          />
          <Fact label={t("Content type")} value={item.response.content_type ?? t("Unavailable")} />
          <Fact
            label={t("Content length")}
            value={item.response.content_length === null ? t("Unavailable") : formatBytes(item.response.content_length)}
          />
          <Fact label={t("Response truncated")} value={t(item.response.truncated ? "Yes" : "No")} />
          <Fact label={t("TLS metadata")} value={t(metadataLabel(item.tls.availability))} />
          <Fact
            label={t("TLS verified")}
            value={item.tls.verified === null ? t("Unavailable") : t(item.tls.verified ? "Yes" : "No")}
          />
          <Fact
            label={t("Client certificate used")}
            value={
              item.tls.client_certificate_used === null
                ? t("Unavailable")
                : t(item.tls.client_certificate_used ? "Yes" : "No")
            }
          />
        </dl>
      </section>

      <section aria-labelledby="traffic-lineage-metadata">
        <h5 id="traffic-lineage-metadata">{t("Execution lineage")}</h5>
        <dl className="traffic-fact-list">
          <Fact label={t("Run")} value={item.lineage.run_id} code />
          <Fact label={t("Session")} value={item.lineage.session_id} code />
          <Fact label={t("Tool Call")} value={item.lineage.tool_call_id} code />
          <Fact label={t("Node")} value={item.lineage.node_id} code />
          <Fact label={t("Runner status")} value={t(metadataLabel(item.lineage.node_status))} />
          <Fact label={t("Created by")} value={decisionLabel(item.created_by, t)} />
          <Fact label={t("Created")} value={formatTimestamp(item.created_at, language)} />
        </dl>
      </section>

      <section aria-labelledby="traffic-redirect-metadata">
        <h5 id="traffic-redirect-metadata">{t("Redirect summary")}</h5>
        <dl className="traffic-fact-list">
          <Fact label={t("Availability")} value={t(metadataLabel(item.redirect.availability))} />
          <Fact
            label={t("Redirect count")}
            value={item.redirect.count === null ? t("Unavailable") : item.redirect.count.toString()}
          />
          <Fact
            label={t("Followed redirects")}
            value={item.redirect.followed === null ? t("Unavailable") : t(item.redirect.followed ? "Yes" : "No")}
          />
          <Fact label={t("Partial redirect lineage")} value={t(item.redirect.partial ? "Yes" : "No")} />
        </dl>
        {item.redirect.origins.length ? (
          <ol className="traffic-redirect-list" aria-label={t("Redacted redirect hops")}>
            {item.redirect.origins.map((origin, index) => (
              <li key={`${index}:${origin}`}>
                <Route size={14} />
                <span>
                  <strong>{t("Hop {count}", { count: index + 1 })}</strong>
                  <code>{origin}</code>
                </span>
              </li>
            ))}
          </ol>
        ) : null}
      </section>

      <section aria-labelledby="traffic-governance-metadata">
        <h5 id="traffic-governance-metadata">{t("Metadata governance")}</h5>
        <dl className="traffic-fact-list">
          <Fact label={t("Sensitivity")} value={t(metadataLabel(item.governance.sensitivity))} />
          <Fact label={t("Access class")} value={t(metadataLabel(item.governance.access))} />
          <Fact label={t("Retention state")} value={t(metadataLabel(item.governance.retention))} />
          <Fact label={t("Reveal capability")} value={t(metadataLabel(item.governance.reveal_capability))} />
          <Fact label={t("Request body")} value={t(metadataLabel(item.body.request.availability))} />
          <Fact label={t("Request body truncated")} value={t(item.body.request.truncated ? "Yes" : "No")} />
          <Fact label={t("Response body")} value={t(metadataLabel(item.body.response.availability))} />
          <Fact label={t("Response body truncated")} value={t(item.body.response.truncated ? "Yes" : "No")} />
          <Fact label={t("Replay lineage")} value={decisionLabel(item.replay_of, t)} />
        </dl>
      </section>

      <section aria-labelledby="traffic-artifact-metadata">
        <h5 id="traffic-artifact-metadata">{t("Opaque Artifact references")}</h5>
        <dl className="traffic-fact-list">
          <Fact label={t("Request Artifact presence")} value={t(metadataLabel(item.artifacts.request.presence))} />
          <Fact label={t("Request Artifact access")} value={t(metadataLabel(item.artifacts.request.access))} />
          <Fact label={t("Request opaque ref")} value={item.artifacts.request.opaque_ref ?? t("Unavailable")} code />
          <Fact label={t("Response Artifact presence")} value={t(metadataLabel(item.artifacts.response.presence))} />
          <Fact label={t("Response Artifact access")} value={t(metadataLabel(item.artifacts.response.access))} />
          <Fact label={t("Response opaque ref")} value={item.artifacts.response.opaque_ref ?? t("Unavailable")} code />
        </dl>
      </section>

      <section aria-labelledby="traffic-decision-metadata">
        <h5 id="traffic-decision-metadata">{t("Server decisions")}</h5>
        <dl className="traffic-fact-list">
          {decisionRows.map(([label, summary]) => (
            <Fact key={label} label={t(label)} value={decisionLabel(summary, t)} />
          ))}
        </dl>
      </section>

      <div className="traffic-no-sensitive-actions" role="note">
        <ShieldCheck size={16} />
        {t("This 04A Inspector has no Reveal, Download, or Replay capability.")}
      </div>
    </article>
  );
}

function Fact({ label, value, code = false }: { label: string; value: string; code?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{code ? <code>{value}</code> : value}</dd>
    </div>
  );
}

function trafficRootKey(runId: string) {
  return [TRAFFIC_QUERY_ROOT, runId] as const;
}

function trafficHistoryKey(runId: string, engagementId: string) {
  return [...trafficRootKey(runId), engagementId, "history"] as const;
}

function trafficDetailKey(runId: string, engagementId: string, exchangeId: string) {
  return [...trafficRootKey(runId), engagementId, "detail", exchangeId] as const;
}

function trafficAuthLatchMap(queryClient: QueryClient): Map<string, TrafficAuthLatch> {
  let latches = trafficAuthLatches.get(queryClient);
  if (!latches) {
    latches = new Map();
    trafficAuthLatches.set(queryClient, latches);
  }
  return latches;
}

function beginTrafficAuthRequest(queryClient: QueryClient, runId: string): number {
  const latches = trafficAuthLatchMap(queryClient);
  const previous = latches.get(runId);
  const epoch = (previous?.latestEpoch ?? 0) + 1;
  latches.set(runId, {
    latestEpoch: epoch,
    rejectionFence: previous?.rejectionFence ?? 0,
    settledEpoch: previous?.settledEpoch ?? 0,
    error: previous?.error ?? null,
  });
  return epoch;
}

function resolveTrafficAuthRequest(queryClient: QueryClient, runId: string, epoch: number) {
  const latches = trafficAuthLatchMap(queryClient);
  const current = latches.get(runId);
  if (
    !current ||
    epoch <= current.rejectionFence ||
    epoch < current.settledEpoch
  ) {
    return false;
  }
  latches.set(runId, { ...current, settledEpoch: epoch, error: null });
  return true;
}

function rejectTrafficAuthRequest(
  queryClient: QueryClient,
  runId: string,
  epoch: number,
  error: RiftXAPIError,
): boolean {
  const latches = trafficAuthLatchMap(queryClient);
  const current = latches.get(runId);
  if (!current || epoch <= current.rejectionFence) return false;
  const rejectionFence = current.latestEpoch;
  latches.set(runId, {
    ...current,
    rejectionFence,
    settledEpoch: Math.max(current.settledEpoch, rejectionFence),
    error,
  });
  return true;
}

function readTrafficAuthError(queryClient: QueryClient, runId: string): RiftXAPIError | null {
  return trafficAuthLatches.get(queryClient)?.get(runId)?.error ?? null;
}

function clearTrafficAuthLatch(queryClient: QueryClient, runId: string) {
  trafficAuthLatches.get(queryClient)?.delete(runId);
}

function purgeRunTrafficCache(queryClient: QueryClient, runId: string) {
  queryClient.setQueriesData<unknown>(
    { queryKey: trafficRootKey(runId) },
    (current: unknown) =>
      current && typeof current === "object" && "pages" in current
        ? ({ pages: [], pageParams: [] } satisfies InfiniteData<TrafficPage>)
        : null,
  );
  queryClient.removeQueries({ queryKey: trafficRootKey(runId), type: "inactive" });
}

function handleTrafficRequestFailure(
  queryClient: QueryClient,
  runId: string,
  epoch: number,
  error: unknown,
) {
  const authorizationError = trafficAuthorizationError(error);
  if (
    authorizationError &&
    rejectTrafficAuthRequest(queryClient, runId, epoch, authorizationError)
  ) {
    purgeRunTrafficCache(queryClient, runId);
  }
}

function retryTrafficQuery(failureCount: number, error: Error): boolean {
  if (error instanceof RiftXAPIError && [401, 403].includes(error.status)) return false;
  if (error instanceof RiftXAPIError && error.status >= 400 && error.status < 500) return false;
  if (trafficContractError(error)) return false;
  return failureCount < TRAFFIC_QUERY_RETRY_LIMIT;
}

function trafficAuthorizationError(error: unknown): RiftXAPIError | null {
  return error instanceof RiftXAPIError && [401, 403].includes(error.status) ? error : null;
}

function trafficContractError(error: unknown): RiftXAPIError | null {
  return error instanceof RiftXAPIError && error.code === "invalid_traffic_contract"
    ? error
    : null;
}

function staleTrafficAuthorizationEpoch(): RiftXAPIError {
  return new RiftXAPIError(
    409,
    "stale_traffic_authorization_epoch",
    "RiftX discarded metadata from an obsolete authorization request",
  );
}

function trafficDetailNotFound(error: unknown): RiftXAPIError | null {
  return error instanceof RiftXAPIError && error.status === 404 ? error : null;
}

function trafficCursorError(error: unknown): RiftXAPIError | null {
  return error instanceof RiftXAPIError &&
    ((error.status === 409 && error.code === "stale_traffic_cursor") ||
      (error.status === 422 && error.code === "invalid_traffic_cursor"))
    ? error
    : null;
}

function validateTrafficPages(
  pages: readonly TrafficPage[],
  runId: string,
  engagementId: string,
): TrafficIntegrity {
  const issues: string[] = [];
  let scopeMismatch = false;
  const exchangeIds = new Set<string>();
  const requestIds = new Set<string>();
  const cursors = new Set<string>();
  const snapshotId = pages[0]?.snapshot.id ?? null;
  const snapshotThrough = pages[0]?.snapshot.created_through ?? null;

  for (const page of pages) {
    if (page.scope.run_id !== runId || page.scope.engagement_id !== engagementId) {
      issues.push("traffic_scope_mismatch");
      scopeMismatch = true;
    }
    if (
      page.snapshot.id !== snapshotId ||
      page.snapshot.created_through !== snapshotThrough
    ) {
      issues.push("traffic_snapshot_mismatch");
    }
    if (page.has_more !== Boolean(page.next_cursor)) {
      issues.push("traffic_cursor_state_invalid");
    }
    if (page.next_cursor) {
      if (cursors.has(page.next_cursor)) issues.push("duplicate_traffic_cursor");
      cursors.add(page.next_cursor);
    }
    for (const item of page.items) {
      if (item.lineage.run_id !== runId) {
        issues.push("traffic_item_run_mismatch");
        scopeMismatch = true;
      }
      if (exchangeIds.has(item.exchange_id)) issues.push("duplicate_traffic_exchange");
      if (requestIds.has(item.request_id)) issues.push("duplicate_traffic_request");
      exchangeIds.add(item.exchange_id);
      requestIds.add(item.request_id);
    }
  }
  return { issues: uniqueStrings(issues), ok: issues.length === 0, scopeMismatch };
}

function validateTrafficPageContract(value: unknown): TrafficPage {
  const page = exactRecord(
    value,
    [
      "scope",
      "snapshot",
      "items",
      "truncated",
      "has_more",
      "next_cursor",
      "partial",
      "partial_reasons",
    ],
    "page",
  );
  const scope = exactRecord(page.scope, ["run_id", "engagement_id"], "scope");
  const snapshot = exactRecord(page.snapshot, ["id", "created_through", "stale"], "snapshot");
  const items = requireArray(page.items, "items").map((item, index) =>
    validateTrafficItemContract(item, `items.${index}`),
  );
  const validated: TrafficPage = {
    scope: {
      run_id: requireTrafficId(scope.run_id, "scope.run_id"),
      engagement_id: requireTrafficId(scope.engagement_id, "scope.engagement_id"),
    },
    snapshot: {
      id: requireDigest(snapshot.id, "snapshot.id"),
      created_through: optionalTimestamp(snapshot.created_through, "snapshot.created_through"),
      stale: requireLiteralFalse(snapshot.stale, "snapshot.stale"),
    },
    items,
    truncated: requireBoolean(page.truncated, "truncated"),
    has_more: requireBoolean(page.has_more, "has_more"),
    next_cursor: optionalOpaque(page.next_cursor, "next_cursor", 4096),
    partial: requireBoolean(page.partial, "partial"),
    partial_reasons: requireTokens(page.partial_reasons, "partial_reasons"),
  };
  const itemReasons = [...new Set(items.flatMap((item) => item.partial_reasons))].sort();
  if (
    validated.truncated ||
    validated.partial !== Boolean(itemReasons.length) ||
    validated.partial_reasons.length !== itemReasons.length ||
    validated.partial_reasons.some((reason, index) => reason !== itemReasons[index]) ||
    validated.has_more !== Boolean(validated.next_cursor) ||
    items.some((item) => item.lineage.run_id !== validated.scope.run_id)
  ) {
    throw invalidTrafficContract("page_invariant_mismatch");
  }
  return validated;
}

function validateTrafficDetailContract(value: unknown): TrafficDetail {
  const detail = exactRecord(value, ["scope", "item"], "detail");
  const scope = exactRecord(detail.scope, ["run_id", "engagement_id"], "detail.scope");
  const validated: TrafficDetail = {
    scope: {
      run_id: requireTrafficId(scope.run_id, "detail.scope.run_id"),
      engagement_id: requireTrafficId(
        scope.engagement_id,
        "detail.scope.engagement_id",
      ),
    },
    item: validateTrafficItemContract(detail.item, "detail.item"),
  };
  if (validated.scope.run_id !== validated.item.lineage.run_id) {
    throw invalidTrafficContract("detail_scope_mismatch");
  }
  return validated;
}

function validateTrafficItemContract(value: unknown, path: string): TrafficItem {
  const item = exactRecord(
    value,
    [
      "exchange_id",
      "request_id",
      "execution_key",
      "canonical_request_digest",
      "digest_stability",
      "lineage",
      "method",
      "url_summary",
      "tls",
      "response",
      "artifacts",
      "body",
      "redirect",
      "replay_of",
      "created_by",
      "created_at",
      "scope_decision",
      "approval",
      "safety_gate",
      "governance",
      "projection_quality",
      "partial_reasons",
    ],
    path,
  );
  const lineage = exactRecord(
    item.lineage,
    ["run_id", "session_id", "tool_call_id", "node_id", "node_status"],
    `${path}.lineage`,
  );
  const url = exactRecord(
    item.url_summary,
    ["availability", "scheme", "origin", "path_shape", "path_segment_count", "redacted"],
    `${path}.url_summary`,
  );
  const response = exactRecord(
    item.response,
    ["status_code", "status_class", "elapsed_ms", "content_type", "content_length", "truncated"],
    `${path}.response`,
  );
  const tls = exactRecord(
    item.tls,
    ["availability", "verified", "client_certificate_used"],
    `${path}.tls`,
  );
  const artifacts = exactRecord(item.artifacts, ["request", "response"], `${path}.artifacts`);
  const body = exactRecord(item.body, ["request", "response"], `${path}.body`);
  const redirect = exactRecord(
    item.redirect,
    ["availability", "count", "followed", "origins", "partial"],
    `${path}.redirect`,
  );
  const replay = exactRecord(
    item.replay_of,
    ["availability", "request_id", "reason"],
    `${path}.replay_of`,
  );
  const createdBy = exactRecord(item.created_by, ["availability", "kind"], `${path}.created_by`);
  const governance = exactRecord(
    item.governance,
    ["sensitivity", "access", "retention", "reveal_capability"],
    `${path}.governance`,
  );

  const validated: TrafficItem = {
    exchange_id: requireTrafficId(item.exchange_id, `${path}.exchange_id`),
    request_id: requireTrafficId(item.request_id, `${path}.request_id`),
    execution_key: requireOpaque(item.execution_key, `${path}.execution_key`, 255),
    canonical_request_digest: requireDigest(
      item.canonical_request_digest,
      `${path}.canonical_request_digest`,
    ),
    digest_stability: requireLiteral(
      item.digest_stability,
      "server_instance",
      `${path}.digest_stability`,
    ),
    lineage: {
      run_id: requireTrafficId(lineage.run_id, `${path}.lineage.run_id`),
      session_id: requireTrafficId(lineage.session_id, `${path}.lineage.session_id`),
      tool_call_id: requireTrafficId(lineage.tool_call_id, `${path}.lineage.tool_call_id`),
      node_id: requireTrafficId(lineage.node_id, `${path}.lineage.node_id`),
      node_status: requireOneOf(
        lineage.node_status,
        ["online", "offline", "degraded", "lost", "unknown"],
        `${path}.lineage.node_status`,
      ),
    },
    method: requireMethod(item.method, `${path}.method`),
    url_summary: {
      availability: requireAvailability(url.availability, `${path}.url_summary.availability`),
      scheme: optionalScheme(url.scheme, `${path}.url_summary.scheme`),
      origin: optionalSafeOrigin(url.origin, `${path}.url_summary.origin`),
      path_shape: optionalPathShape(url.path_shape, `${path}.url_summary.path_shape`),
      path_segment_count: optionalInteger(
        url.path_segment_count,
        `${path}.url_summary.path_segment_count`,
        4096,
      ),
      redacted: requireLiteralTrue(url.redacted, `${path}.url_summary.redacted`),
    },
    tls: validateTlsSummary(tls, `${path}.tls`),
    response: {
      status_code: requireStatus(response.status_code, `${path}.response.status_code`),
      status_class: validateStatusClass(
        response.status_class,
        response.status_code,
        `${path}.response.status_class`,
      ),
      elapsed_ms: requireInteger(response.elapsed_ms, `${path}.response.elapsed_ms`),
      content_type: optionalMediaType(response.content_type, `${path}.response.content_type`),
      content_length: optionalInteger(response.content_length, `${path}.response.content_length`),
      truncated: requireBoolean(response.truncated, `${path}.response.truncated`),
    },
    artifacts: {
      request: validateArtifactSummary(artifacts.request, `${path}.artifacts.request`),
      response: validateArtifactSummary(artifacts.response, `${path}.artifacts.response`),
    },
    body: {
      request: validateBodySummary(body.request, `${path}.body.request`),
      response: validateBodySummary(body.response, `${path}.body.response`),
    },
    redirect: {
      availability: requireAvailability(redirect.availability, `${path}.redirect.availability`),
      count: optionalInteger(redirect.count, `${path}.redirect.count`),
      followed: optionalBoolean(redirect.followed, `${path}.redirect.followed`),
      origins: requireArray(redirect.origins, `${path}.redirect.origins`).map((origin, index) =>
        requireSafeOrigin(origin, `${path}.redirect.origins.${index}`),
      ),
      partial: requireBoolean(redirect.partial, `${path}.redirect.partial`),
    },
    replay_of: {
      availability: requireLiteral(replay.availability, "unavailable", `${path}.replay_of.availability`),
      request_id: requireNull(replay.request_id, `${path}.replay_of.request_id`),
      reason: requireLiteral(replay.reason, "not_persisted", `${path}.replay_of.reason`),
    },
    created_by: {
      availability: requireAvailability(createdBy.availability, `${path}.created_by.availability`),
      kind: requireOneOf(createdBy.kind, ["agent_runtime", "unknown"], `${path}.created_by.kind`),
    },
    created_at: requireTimestamp(item.created_at, `${path}.created_at`),
    scope_decision: validateScopeDecision(item.scope_decision, `${path}.scope_decision`),
    approval: validateApprovalSummary(item.approval, `${path}.approval`),
    safety_gate: validateSafetyGateSummary(item.safety_gate, `${path}.safety_gate`),
    governance: {
      sensitivity: requireLiteral(
        governance.sensitivity,
        "restricted_sensitive",
        `${path}.governance.sensitivity`,
      ),
      access: requireLiteral(
        governance.access,
        "metadata_only",
        `${path}.governance.access`,
      ),
      retention: requireLiteral(
        governance.retention,
        "legacy_unmanaged",
        `${path}.governance.retention`,
      ),
      reveal_capability: requireLiteral(
        governance.reveal_capability,
        "disabled",
        `${path}.governance.reveal_capability`,
      ),
    },
    projection_quality: requireOneOf(
      item.projection_quality,
      ["exact", "partial"],
      `${path}.projection_quality`,
    ),
    partial_reasons: requireTokens(item.partial_reasons, `${path}.partial_reasons`),
  };
  if (validated.exchange_id !== validated.request_id) {
    throw invalidTrafficContract(`${path}_identity_mismatch`);
  }
  if (
    (validated.projection_quality === "partial") !==
    Boolean(validated.partial_reasons.length)
  ) {
    throw invalidTrafficContract(`${path}_quality_mismatch`);
  }
  validateUrlAvailability(validated.url_summary, `${path}.url_summary`);
  validateRedirectAvailability(validated.redirect, `${path}.redirect`);
  validateCreatedByAvailability(validated.created_by, `${path}.created_by`);
  return validated;
}

function validateArtifactSummary(value: unknown, path: string): TrafficArtifactSummary {
  const summary = exactRecord(value, ["opaque_ref", "presence", "access"], path);
  const presence = requireOneOf(
    summary.presence,
    ["recorded_present", "recorded_missing", "not_recorded"],
    `${path}.presence`,
  );
  const opaqueRef = optionalArtifactRef(summary.opaque_ref, `${path}.opaque_ref`);
  if ((presence === "not_recorded") !== (opaqueRef === null)) {
    throw invalidTrafficContract(`${path}_reference_mismatch`);
  }
  return {
    opaque_ref: opaqueRef,
    presence,
    access: requireLiteral(summary.access, "metadata_only", `${path}.access`),
  };
}

function validateBodySummary(value: unknown, path: string): TrafficBodySummary {
  const summary = exactRecord(value, ["availability", "revealable", "truncated"], path);
  return {
    availability: requireOneOf(
      summary.availability,
      ["present", "absent", "unknown"],
      `${path}.availability`,
    ),
    revealable: requireLiteralFalse(summary.revealable, `${path}.revealable`),
    truncated: requireBoolean(summary.truncated, `${path}.truncated`),
  };
}

function validateTlsSummary(
  value: Record<string, unknown>,
  path: string,
): TrafficTlsSummary {
  const availability = requireOneOf(
    value.availability,
    ["available", "not_applicable", "unavailable"],
    `${path}.availability`,
  );
  const verified = optionalBoolean(value.verified, `${path}.verified`);
  const clientCertificateUsed = optionalBoolean(
    value.client_certificate_used,
    `${path}.client_certificate_used`,
  );
  if (
    (availability === "available" &&
      (verified === null || clientCertificateUsed === null)) ||
    (availability !== "available" &&
      (verified !== null || clientCertificateUsed !== null))
  ) {
    throw invalidTrafficContract(`${path}_availability_mismatch`);
  }
  return {
    availability,
    verified,
    client_certificate_used: clientCertificateUsed,
  };
}

function validateScopeDecision(value: unknown, path: string): TrafficScopeDecision {
  const summary = exactRecord(
    value,
    ["availability", "decision", "reference_kind", "reason"],
    path,
  );
  return {
    availability: requireLiteral(summary.availability, "unavailable", `${path}.availability`),
    decision: requireNull(summary.decision, `${path}.decision`),
    reference_kind: requireLiteral(summary.reference_kind, "run_scope", `${path}.reference_kind`),
    reason: requireLiteral(summary.reason, "decision_not_persisted", `${path}.reason`),
  };
}

function validateApprovalSummary(value: unknown, path: string): TrafficApprovalSummary {
  const summary = exactRecord(value, ["availability", "reference_id", "status"], path);
  const availability = requireOneOf(
    summary.availability,
    ["available", "not_required", "unavailable"],
    `${path}.availability`,
  );
  const referenceId = optionalTrafficId(summary.reference_id, `${path}.reference_id`);
  const status = optionalOneOf(
    summary.status,
    ["pending", "approved", "rejected", "cancelled"],
    `${path}.status`,
  );
  if (
    (availability === "available" && (!referenceId || !status)) ||
    (availability !== "available" && (referenceId !== null || status !== null))
  ) {
    throw invalidTrafficContract(`${path}_availability_mismatch`);
  }
  return { availability, reference_id: referenceId, status };
}

function validateSafetyGateSummary(value: unknown, path: string): TrafficSafetyGateSummary {
  const summary = exactRecord(value, ["availability", "reference_id", "reason"], path);
  return {
    availability: requireLiteral(summary.availability, "unavailable", `${path}.availability`),
    reference_id: requireNull(summary.reference_id, `${path}.reference_id`),
    reason: requireLiteral(summary.reason, "not_implemented", `${path}.reason`),
  };
}

function exactRecord(
  value: unknown,
  allowedKeys: readonly string[],
  path: string,
): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw invalidTrafficContract(`${path}_not_object`);
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  if (keys.some((key) => !allowedKeys.includes(key))) {
    throw invalidTrafficContract(`${path}_unknown_field`);
  }
  if (allowedKeys.some((key) => !(key in record))) {
    throw invalidTrafficContract(`${path}_missing_field`);
  }
  return record;
}

function invalidTrafficContract(reason: string): RiftXAPIError {
  return new RiftXAPIError(
    502,
    "invalid_traffic_contract",
    "RiftX rejected an invalid Target HTTP metadata response",
    { reason },
  );
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value) || value.length > 100) throw invalidTrafficContract(`${path}_invalid`);
  return value;
}

function requireTrafficId(value: unknown, path: string): string {
  if (typeof value !== "string" || !isTrafficId(value)) throw invalidTrafficContract(`${path}_invalid`);
  return value;
}

function optionalTrafficId(value: unknown, path: string): string | null {
  return value === null ? null : requireTrafficId(value, path);
}

function isTrafficId(value: string): boolean {
  return (
    Boolean(value) &&
    value.length <= 256 &&
    value === value.trim() &&
    !hasControlCharacters(value)
  );
}

function requireOpaque(value: unknown, path: string, maximum = 512): string {
  if (
    typeof value !== "string" ||
    !value ||
    value.length > maximum ||
    value !== value.trim() ||
    hasControlCharacters(value)
  ) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function optionalOpaque(value: unknown, path: string, maximum = 512): string | null {
  return value === null ? null : requireOpaque(value, path, maximum);
}

function requireDigest(value: unknown, path: string): string {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function optionalArtifactRef(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !/^traffic-artifact:v1:[0-9a-f]{64}$/.test(value)) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function requireToken(value: unknown, path: string): string {
  if (typeof value !== "string" || !SAFE_TOKEN_PATTERN.test(value)) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function requireLiteral<T extends string>(
  value: unknown,
  expected: T,
  path: string,
): T {
  if (value !== expected) throw invalidTrafficContract(`${path}_invalid`);
  return expected;
}

function requireOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  path: string,
): T[number] {
  if (typeof value !== "string" || !allowed.includes(value)) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value as T[number];
}

function optionalOneOf<const T extends readonly string[]>(
  value: unknown,
  allowed: T,
  path: string,
): T[number] | null {
  return value === null ? null : requireOneOf(value, allowed, path);
}

function requireNull(value: unknown, path: string): null {
  if (value !== null) throw invalidTrafficContract(`${path}_must_be_null`);
  return null;
}

function requireTokens(value: unknown, path: string): string[] {
  const tokens = requireArray(value, path).map((item, index) =>
    requireToken(item, `${path}.${index}`),
  );
  if (
    new Set(tokens).size !== tokens.length ||
    tokens.some((token, index) => token !== [...tokens].sort()[index])
  ) {
    throw invalidTrafficContract(`${path}_order_or_duplicate`);
  }
  return tokens;
}

function requireAvailability(value: unknown, path: string): TrafficAvailability {
  if (value !== "available" && value !== "unavailable") {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function requireMethod(value: unknown, path: string): string {
  if (
    typeof value !== "string" ||
    value.length > 32 ||
    !/^[A-Z][A-Z-]*$/.test(value)
  ) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function requireBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw invalidTrafficContract(`${path}_invalid`);
  return value;
}

function optionalBoolean(value: unknown, path: string): boolean | null {
  return value === null ? null : requireBoolean(value, path);
}

function requireLiteralTrue(value: unknown, path: string): true {
  if (value !== true) throw invalidTrafficContract(`${path}_must_be_true`);
  return true;
}

function requireLiteralFalse(value: unknown, path: string): false {
  if (value !== false) throw invalidTrafficContract(`${path}_must_be_false`);
  return false;
}

function requireInteger(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value as number;
}

function optionalInteger(
  value: unknown,
  path: string,
  maximum = Number.MAX_SAFE_INTEGER,
): number | null {
  if (value === null) return null;
  const result = requireInteger(value, path);
  if (result > maximum) throw invalidTrafficContract(`${path}_invalid`);
  return result;
}

function requireStatus(value: unknown, path: string): number {
  const status = requireInteger(value, path);
  if (status < 100 || status > 599) throw invalidTrafficContract(`${path}_invalid`);
  return status;
}

function validateStatusClass(
  value: unknown,
  statusValue: unknown,
  path: string,
): string {
  const status = requireStatus(statusValue, path.replace("status_class", "status_code"));
  const expected = [
    "",
    "informational",
    "success",
    "redirect",
    "client_error",
    "server_error",
  ][Math.floor(status / 100)];
  if (!expected || value !== expected) throw invalidTrafficContract(`${path}_mismatch`);
  return expected;
}

function requireTimestamp(value: unknown, path: string): string {
  if (
    typeof value !== "string" ||
    value.length > 64 ||
    !/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ||
    !Number.isFinite(Date.parse(value)) ||
    hasControlCharacters(value)
  ) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function optionalTimestamp(value: unknown, path: string): string | null {
  return value === null ? null : requireTimestamp(value, path);
}

function optionalScheme(value: unknown, path: string): "http" | "https" | null {
  if (value === null) return null;
  if (value !== "http" && value !== "https") throw invalidTrafficContract(`${path}_invalid`);
  return value;
}

function requireSafeOrigin(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length > 512 || hasControlCharacters(value)) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  try {
    const parsed = new URL(value);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      (parsed.pathname !== "" && parsed.pathname !== "/") ||
      parsed.origin !== value
    ) {
      throw invalidTrafficContract(`${path}_unsafe`);
    }
  } catch (error) {
    if (error instanceof RiftXAPIError) throw error;
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function optionalSafeOrigin(value: unknown, path: string): string | null {
  return value === null ? null : requireSafeOrigin(value, path);
}

function optionalPathShape(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length > 256 ||
    hasControlCharacters(value) ||
    /[?#@=&%\\]/.test(value)
  ) {
    throw invalidTrafficContract(`${path}_unsafe`);
  }
  return value;
}

function validateUrlAvailability(summary: TrafficUrlSummary, path: string) {
  if (summary.availability === "unavailable") {
    if (
      summary.scheme !== null ||
      summary.origin !== null ||
      summary.path_shape !== null ||
      summary.path_segment_count !== null
    ) {
      throw invalidTrafficContract(`${path}_unavailable_values`);
    }
    return;
  }
  if (
    !summary.scheme ||
    !summary.origin ||
    (summary.path_shape !== "/" && summary.path_shape !== "/…") ||
    summary.path_segment_count === null ||
    new URL(summary.origin).protocol !== `${summary.scheme}:`
  ) {
    throw invalidTrafficContract(`${path}_available_values`);
  }
}

function validateRedirectAvailability(summary: TrafficRedirectSummary, path: string) {
  if (summary.availability === "unavailable") {
    if (
      summary.count !== null ||
      summary.followed !== null ||
      summary.origins.length ||
      !summary.partial
    ) {
      throw invalidTrafficContract(`${path}_unavailable_values`);
    }
    return;
  }
  if (
    summary.count === null ||
    summary.count > 10 ||
    summary.followed !== (summary.count > 0) ||
    summary.origins.length !== summary.count ||
    summary.partial
  ) {
    throw invalidTrafficContract(`${path}_available_values`);
  }
}

function validateCreatedByAvailability(summary: TrafficCreatedBy, path: string) {
  if (
    (summary.availability === "available" && summary.kind !== "agent_runtime") ||
    (summary.availability === "unavailable" && summary.kind !== "unknown")
  ) {
    throw invalidTrafficContract(`${path}_mismatch`);
  }
}

function optionalMediaType(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    !SAFE_TRAFFIC_MEDIA_TYPES.has(value)
  ) {
    throw invalidTrafficContract(`${path}_invalid`);
  }
  return value;
}

function hasControlCharacters(value: string): boolean {
  return /\p{C}/u.test(value);
}

function trafficUrlLabel(
  summary: TrafficUrlSummary,
  t: (message: string, values?: Record<string, string | number>) => string,
): string {
  if (summary.availability !== "available") return t("URL summary unavailable");
  return [summary.origin, summary.path_shape].filter(Boolean).join(" ") || t("URL summary unavailable");
}

function trafficResponseLabel(
  item: TrafficItem,
  t: (message: string, values?: Record<string, string | number>) => string,
): string {
  const base = `${item.response.status_code} · ${item.response.elapsed_ms} ms`;
  return item.lineage.node_status === "lost" ? `${base} · ${t("Runner LOST")}` : base;
}

function metadataLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function decisionLabel(
  value:
    | TrafficScopeDecision
    | TrafficApprovalSummary
    | TrafficSafetyGateSummary
    | TrafficCreatedBy
    | TrafficReplaySummary,
  t: (message: string, values?: Record<string, string | number>) => string,
): string {
  const parts = Object.entries(value)
    .filter(([key, item]) => key !== "availability" && item !== null)
    .map(([, item]) => metadataLabel(String(item)));
  return parts.length
    ? `${t(metadataLabel(value.availability))} · ${parts.join(" · ")}`
    : t(metadataLabel(value.availability));
}

function formatTimestamp(value: string, language: string) {
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
  let unit = units[0]!;
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index]!;
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${unit}`;
}

function uniqueStrings(values: readonly string[]): string[] {
  return [...new Set(values)];
}

export default RunTrafficWorkspace;
