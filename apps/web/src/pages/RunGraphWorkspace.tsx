import {
  type InfiniteData,
  type QueryClient,
  useInfiniteQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  AlertTriangle,
  Boxes,
  ExternalLink,
  GitBranch,
  Loader2,
  LocateFixed,
  RefreshCw,
  Search,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import {
  type CSSProperties,
  type FormEvent,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, RiftXAPIError } from "../api/client";
import type {
  GraphEdge,
  GraphNode,
  GraphTypeMetadata,
  GraphViewKind,
  GraphViewPage,
} from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useI18n } from "../i18n";

const GRAPH_PAGE_SIZE = 100;
const GRAPH_CANVAS_NODE_LIMIT = 120;
const GRAPH_QUERY_RETRY_LIMIT = 1;
const GRAPH_QUERY_ROOT = "run-graph";
const GRAPH_NODE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@+~-]{0,511}$/;
const GRAPH_ACTION_COMPONENT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._@+~-]{0,127}$/;
const EMPTY_GRAPH_PAGES: readonly GraphViewPage[] = [];

const GRAPH_VIEWS: ReadonlyArray<{ label: string; value: GraphViewKind }> = [
  { value: "task", label: "Task" },
  { value: "evidence", label: "Evidence" },
  { value: "operation", label: "Operation" },
];

export interface RunGraphWorkspaceProps {
  runId: string;
  expectedEngagementId: string;
  view: GraphViewKind;
  focusId: string;
  onViewChange: (view: GraphViewKind) => void;
  onFocusChange: (focusId: string) => void;
  onOpenAction: (actionId: string) => void;
}

type ScopedFilters = {
  edgeType: string;
  nodeType: string;
  scopeKey: string;
  search: string;
  searchDraft: string;
};

type GraphPosition = { x: number; y: number };
type GraphViewport = { height: number; width: number; x: number; y: number };
type GraphLayout = {
  positions: Map<string, GraphPosition>;
  revision: number;
  scopeKey: string;
  topology: string;
  viewport: GraphViewport;
};

type GraphValidationGate = {
  identity: string;
  validated: boolean;
};

type GraphIntegrity = {
  issues: string[];
  ok: boolean;
  scopeMismatch: boolean;
};

type GraphAuthLatch = {
  latestEpoch: number;
  settledEpoch: number;
  error: RiftXAPIError | null;
};

const graphAuthLatches = new WeakMap<QueryClient, Map<string, GraphAuthLatch>>();

export function RunGraphWorkspace({
  runId,
  expectedEngagementId,
  view,
  focusId,
  onViewChange,
  onFocusChange,
  onOpenAction,
}: RunGraphWorkspaceProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const scopeKey = `${runId}:${expectedEngagementId}:${view}`;
  const [filters, setFilters] = useState<ScopedFilters>(() => emptyFilters(scopeKey));
  const activeFilters = filters.scopeKey === scopeKey ? filters : emptyFilters(scopeKey);
  const [selection, setSelection] = useState({ id: focusId, scopeKey });
  const [focusRequest, setFocusRequest] = useState({ id: focusId, scopeKey });
  const requestedFocus =
    focusRequest.scopeKey === scopeKey ? focusRequest.id : focusId;
  const [validationGate, setValidationGate] = useState<GraphValidationGate>({
    identity: scopeKey,
    validated: false,
  });
  const securityValidated =
    validationGate.identity === scopeKey && validationGate.validated;
  const [metadataState, setMetadataState] = useState<{
    items: GraphTypeMetadata[];
    scopeKey: string;
  }>({ items: [], scopeKey });
  const activeSelection =
    selection.scopeKey === scopeKey
      ? selection.id
      : focusId;
  const [layout, setLayout] = useState<GraphLayout | null>(null);
  const previousRunIdRef = useRef(runId);
  const viewTabRefs = useRef(new Map<GraphViewKind, HTMLButtonElement>());

  useEffect(() => {
    if (filters.scopeKey !== scopeKey) setFilters(emptyFilters(scopeKey));
  }, [filters.scopeKey, scopeKey]);

  useEffect(() => {
    if (validationGate.identity !== scopeKey) {
      setValidationGate({ identity: scopeKey, validated: false });
    }
  }, [scopeKey, validationGate.identity]);

  useEffect(() => {
    const previousRunId = previousRunIdRef.current;
    if (previousRunId !== runId) {
      queryClient.removeQueries({ queryKey: graphRootKey(previousRunId) });
      clearGraphAuthLatch(queryClient, previousRunId);
      previousRunIdRef.current = runId;
    }
  }, [queryClient, runId]);

  const queryKey = graphQueryKey(
    runId,
    expectedEngagementId,
    view,
    activeFilters,
    requestedFocus,
  );
  const graph = useInfiniteQuery({
    queryKey,
    queryFn: async ({ pageParam, signal }) => {
      const requestEpoch = beginGraphAuthRequest(queryClient, runId);
      try {
        const response = await api.listRunGraph(
          runId,
          {
            view,
            nodeType: activeFilters.nodeType || undefined,
            edgeType: activeFilters.edgeType || undefined,
            focus: requestedFocus || undefined,
            search: activeFilters.search || undefined,
            limit: GRAPH_PAGE_SIZE,
            cursor: pageParam ?? undefined,
          },
          signal,
        );
        resolveGraphAuthRequest(queryClient, runId, requestEpoch);
        return response;
      } catch (error) {
        const authorizationError = graphAuthorizationError(
          error instanceof Error ? error : null,
        );
        if (authorizationError) {
          if (
            rejectGraphAuthRequest(
              queryClient,
              runId,
              requestEpoch,
              authorizationError,
            )
          ) {
            purgeRunGraphCache(queryClient, runId);
          }
        }
        throw error;
      }
    },
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage: GraphViewPage) =>
      lastPage.has_more ? lastPage.next_cursor : null,
    enabled: Boolean(runId),
    placeholderData: (previousData) => previousData,
    refetchOnMount: "always",
    retry: retryGraphQuery,
  });

  const authorizationError =
    graphAuthorizationError(graph.error) ??
    readGraphAuthError(queryClient, runId);
  const staleCursorError = graphStaleCursorError(graph.error);
  useEffect(() => {
    if (
      !authorizationError &&
      graph.isSuccess &&
      graph.isFetchedAfterMount &&
      !graph.isFetching &&
      !graph.isPlaceholderData
    ) {
      setValidationGate({ identity: scopeKey, validated: true });
    }
  }, [
    authorizationError,
    graph.isFetchedAfterMount,
    graph.isFetching,
    graph.isPlaceholderData,
    graph.isSuccess,
    scopeKey,
  ]);

  const revealAuthorizedData = securityValidated && !authorizationError;
  const allPages = graph.data?.pages ?? EMPTY_GRAPH_PAGES;
  const integrity = useMemo(
    () =>
      validateGraphPages(
        allPages,
        runId,
        expectedEngagementId,
        view,
      ),
    [allPages, expectedEngagementId, runId, view],
  );
  const scopeMismatch = revealAuthorizedData && integrity.scopeMismatch;
  const integrityFailure = revealAuthorizedData && !integrity.ok;
  const pages =
    revealAuthorizedData && integrity.ok ? allPages : EMPTY_GRAPH_PAGES;
  const nodes = useMemo(() => mergeGraphNodes(pages), [pages]);
  const edges = useMemo(() => mergeGraphEdges(pages), [pages]);
  const responseMetadata = useMemo(() => mergeTypeMetadata(pages), [pages]);
  const metadata =
    !revealAuthorizedData || !integrity.ok
      ? []
      : responseMetadata.length
        ? responseMetadata
        : metadataState.scopeKey === scopeKey
          ? metadataState.items
          : [];

  useEffect(() => {
    if (revealAuthorizedData && integrity.ok && responseMetadata.length) {
      setMetadataState({ items: responseMetadata, scopeKey });
    }
  }, [integrity.ok, responseMetadata, revealAuthorizedData, scopeKey]);
  const metadataByKey = useMemo(
    () => new Map(metadata.map((item) => [metadataKey(item.kind, item.type), item])),
    [metadata],
  );
  const pagePartialReasons = useMemo(
    () => uniqueStrings(pages.flatMap((candidate) => candidate.partial_reasons)),
    [pages],
  );
  const truncated = pages.some((candidate) => candidate.truncated);
  const snapshotStale = pages.some((candidate) => snapshotIsStale(candidate));
  const topology = topologySignature(nodes, edges);
  const activeLayout = useMemo(() => {
    if (
      layout &&
      layout.scopeKey === scopeKey &&
      layout.topology === topology
    ) {
      return layout;
    }
    return createGraphLayout(nodes, edges, scopeKey, topology, 0);
  }, [edges, layout, nodes, scopeKey, topology]);

  useEffect(() => {
    setLayout((current) => {
      if (
        current &&
        current.scopeKey === scopeKey &&
        current.topology === topology
      ) {
        return current;
      }
      return createGraphLayout(nodes, edges, scopeKey, topology, 0);
    });
  }, [edges, nodes, scopeKey, topology]);

  const nodeById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  useEffect(() => {
    setSelection((current) =>
      current.id === focusId && current.scopeKey === scopeKey
        ? current
        : { id: focusId, scopeKey },
    );
  }, [focusId, scopeKey]);
  useEffect(() => {
    setFocusRequest((current) => {
      if (current.scopeKey !== scopeKey) return { id: focusId, scopeKey };
      if (!focusId) {
        return current.id ? { id: "", scopeKey } : current;
      }
      if (nodeById.has(focusId) || current.id === focusId) return current;
      return { id: focusId, scopeKey };
    });
  }, [focusId, nodeById, scopeKey]);
  const selectedNode = activeSelection ? nodeById.get(activeSelection) : undefined;
  const hasMore = revealAuthorizedData && integrity.ok && Boolean(graph.hasNextPage);
  const canvasNodes = nodes.slice(0, GRAPH_CANVAS_NODE_LIMIT);
  const canvasNodeIds = new Set(canvasNodes.map((node) => node.id));
  const canvasEdges = edges.filter(
    (edge) => canvasNodeIds.has(edge.source) && canvasNodeIds.has(edge.target),
  );
  const fatalError =
    authorizationError ??
    (!revealAuthorizedData && graph.error && !staleCursorError ? graph.error : null);
  const rootRefetchError =
    revealAuthorizedData &&
    graph.isRefetchError &&
    !graph.isFetchNextPageError &&
    !staleCursorError
      ? graph.error
      : null;

  function selectNode(nodeId: string) {
    setSelection({ id: nodeId, scopeKey });
    onFocusChange(nodeId);
  }

  function updateViewport(scale: number) {
    setLayout((current) => {
      const source =
        current?.scopeKey === scopeKey && current.topology === topology
          ? current
          : activeLayout;
      const width = clamp(source.viewport.width * scale, 320, 2200);
      const height = clamp(source.viewport.height * scale, 220, 1600);
      return {
        ...source,
        viewport: {
          width,
          height,
          x: source.viewport.x + (source.viewport.width - width) / 2,
          y: source.viewport.y + (source.viewport.height - height) / 2,
        },
      };
    });
  }

  function relayout() {
    setLayout((current) => {
      const revision = (current?.revision ?? 0) + 1;
      return createGraphLayout(nodes, edges, scopeKey, topology, revision);
    });
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const search = activeFilters.searchDraft.trim();
    setFilters((current) => ({
      ...(current.scopeKey === scopeKey ? current : emptyFilters(scopeKey)),
      scopeKey,
      search,
    }));
  }

  function moveViewFocus(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % GRAPH_VIEWS.length;
    if (event.key === "ArrowLeft") {
      nextIndex = (index - 1 + GRAPH_VIEWS.length) % GRAPH_VIEWS.length;
    }
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = GRAPH_VIEWS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    const nextView = GRAPH_VIEWS[nextIndex]!.value;
    onViewChange(nextView);
    viewTabRefs.current.get(nextView)?.focus();
  }

  function restartSnapshot() {
    setValidationGate({ identity: scopeKey, validated: false });
    void queryClient.resetQueries({ queryKey, exact: true });
  }

  return (
    <section className="run-graph-workspace" aria-labelledby="run-graph-heading">
      <div className="graph-workspace-heading">
        <div>
          <span className="panel-kicker">{t("Read-only projections")}</span>
          <h3 id="run-graph-heading">{t("Run Graph")}</h3>
        </div>
        <button
          className="secondary-button graph-refresh-button"
          type="button"
          disabled={graph.isRefetching}
          aria-label={t("Refresh Graph")}
          onClick={() => void graph.refetch()}
        >
          {graph.isRefetching ? (
            <Loader2 className="spin" size={15} />
          ) : (
            <RefreshCw size={15} />
          )}
          {t("Refresh")}
        </button>
      </div>

      <div className="graph-view-tabs" role="tablist" aria-label={t("Graph views")}>
        {GRAPH_VIEWS.map((candidate, index) => (
          <button
            key={candidate.value}
            ref={(node) => {
              if (node) viewTabRefs.current.set(candidate.value, node);
              else viewTabRefs.current.delete(candidate.value);
            }}
            type="button"
            role="tab"
            aria-selected={candidate.value === view}
            tabIndex={candidate.value === view ? 0 : -1}
            className={candidate.value === view ? "active" : ""}
            onClick={() => onViewChange(candidate.value)}
            onKeyDown={(event) => moveViewFocus(event, index)}
          >
            {t(candidate.label)}
          </button>
        ))}
      </div>

      <form className="graph-controls" role="search" onSubmit={submitSearch}>
        <label>
          <span>{t("Node type")}</span>
          <select
            value={activeFilters.nodeType}
            onChange={(event) =>
              setFilters((current) => ({
                ...(current.scopeKey === scopeKey ? current : emptyFilters(scopeKey)),
                nodeType: event.target.value,
                scopeKey,
              }))
            }
          >
            <option value="">{t("All node types")}</option>
            {metadata
              .filter((item) => item.kind === "node")
              .map((item) => (
                <option key={metadataKey(item.kind, item.type)} value={item.type}>
                  {graphDisplayLabel(item.label, t)}
                </option>
              ))}
          </select>
        </label>
        <label>
          <span>{t("Edge type")}</span>
          <select
            value={activeFilters.edgeType}
            onChange={(event) =>
              setFilters((current) => ({
                ...(current.scopeKey === scopeKey ? current : emptyFilters(scopeKey)),
                edgeType: event.target.value,
                scopeKey,
              }))
            }
          >
            <option value="">{t("All edge types")}</option>
            {metadata
              .filter((item) => item.kind === "edge")
              .map((item) => (
                <option key={metadataKey(item.kind, item.type)} value={item.type}>
                  {graphDisplayLabel(item.label, t)}
                </option>
              ))}
          </select>
        </label>
        <label className="graph-search-field">
          <span>{t("Search Graph")}</span>
          <span className="graph-search-input">
            <Search size={15} aria-hidden="true" />
            <input
              type="search"
              value={activeFilters.searchDraft}
              onChange={(event) =>
                setFilters((current) => ({
                  ...(current.scopeKey === scopeKey ? current : emptyFilters(scopeKey)),
                  scopeKey,
                  searchDraft: event.target.value,
                }))
              }
              placeholder={t("Search labels and identifiers")}
              aria-label={t("Search Graph")}
            />
          </span>
        </label>
        <button className="secondary-button" type="submit">
          <Search size={15} /> {t("Apply")}
        </button>
      </form>

      {metadata.length ? (
        <ul className="graph-legend" aria-label={t("Graph legend")}>
          {metadata.map((item) => (
            <li key={metadataKey(item.kind, item.type)}>
              <span
                className={`graph-legend-swatch ${item.kind}`}
                style={{ "--graph-type-color": safeGraphColor(item.color) } as CSSProperties}
                aria-hidden="true"
              />
              <span>{graphDisplayLabel(item.label, t)}</span>
              <small>{t(item.kind === "node" ? "node" : "relation")}</small>
            </li>
          ))}
        </ul>
      ) : null}

      {fatalError ? <ErrorState error={fatalError} /> : null}
      {integrityFailure ? (
        <div className="graph-integrity-alert" role="alert">
          <AlertTriangle size={18} />
          <div>
            <strong>
              {t(
                scopeMismatch
                  ? "Graph response scope mismatch"
                  : "Graph integrity check failed",
              )}
            </strong>
            <span>
              {t(
                scopeMismatch
                  ? "RiftX hid records that do not belong to this Run and view."
                  : "RiftX hid the complete page batch to avoid mixing inconsistent Graph records.",
              )}
            </span>
            <ul>
              {integrity.issues.map((issue) => <li key={issue}><code>{issue}</code></li>)}
            </ul>
          </div>
          <button className="secondary-button" type="button" onClick={restartSnapshot}>
            <RefreshCw size={15} /> {t("Restart Graph snapshot")}
          </button>
        </div>
      ) : null}
      {!fatalError && !revealAuthorizedData ? (
        <LoadingState label="Revalidating Graph access" />
      ) : null}

      {!fatalError && revealAuthorizedData && integrity.ok ? (
        <>
          {rootRefetchError ? (
            <div className="graph-refetch-error" role="alert">
              <AlertTriangle size={18} />
              <div>
                <strong>{t("Graph refresh failed; showing last verified snapshot")}</strong>
                <span>
                  {t("The last verified Graph data remains visible. Retry refresh to request a newer snapshot.")}
                </span>
                <code>
                  {rootRefetchError instanceof RiftXAPIError
                    ? `${rootRefetchError.code}: ${rootRefetchError.message}`
                    : rootRefetchError.message}
                </code>
              </div>
              <button
                className="secondary-button"
                type="button"
                disabled={graph.isRefetching}
                onClick={() => void graph.refetch()}
              >
                <RefreshCw size={15} /> {t("Retry Graph refresh")}
              </button>
            </div>
          ) : null}

          {truncated || pagePartialReasons.length || snapshotStale ? (
            <div className="graph-quality-alert" role="alert">
              <AlertTriangle size={18} />
              <div>
                <strong>
                  {t(
                    truncated
                      ? "Graph response is truncated"
                      : snapshotStale
                        ? "Graph snapshot reports stale data"
                        : "Graph projection is partial",
                  )}
                </strong>
                <span>
                  {t("Only explicit server-projected lineage is shown; RiftX does not infer missing links.")}
                </span>
                {pagePartialReasons.length ? (
                  <ul>
                    {pagePartialReasons.map((reason) => (
                      <li key={reason}><code>{reason}</code></li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </div>
          ) : null}

          {activeSelection && !selectedNode ? (
            <div
              className="graph-focus-status"
              role={hasMore ? "status" : "alert"}
            >
              <LocateFixed size={17} />
              <div>
                <strong>
                  {t(
                    hasMore
                      ? "Focus is not in the loaded Graph pages"
                      : "Graph focus is missing or belongs to another scope",
                  )}
                </strong>
                <code>{activeSelection}</code>
                <span>
                  {t(
                    hasMore
                      ? "Load more records to resolve this focus without guessing."
                      : "The server did not return this focus; no substitute node was selected.",
                  )}
                </span>
              </div>
            </div>
          ) : null}

          {!nodes.length && !edges.length ? (
            <EmptyState icon={GitBranch} title="Graph is empty">
              {t("No server-projected nodes or relations match this view and filter.")}
            </EmptyState>
          ) : (
            <div className="graph-content-grid">
              <section className="graph-canvas-panel" aria-label={t("Graph canvas")}>
                <div className="graph-canvas-toolbar">
                  <span>
                    {t("{nodes} nodes · {edges} relations loaded", {
                      nodes: nodes.length,
                      edges: edges.length,
                    })}
                  </span>
                  <div>
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={t("Zoom in")}
                      onClick={() => updateViewport(0.8)}
                    >
                      <ZoomIn size={15} />
                    </button>
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={t("Zoom out")}
                      onClick={() => updateViewport(1.25)}
                    >
                      <ZoomOut size={15} />
                    </button>
                    <button
                      className="secondary-button graph-relayout-button"
                      type="button"
                      onClick={relayout}
                    >
                      <Boxes size={14} /> {t("Relayout")}
                    </button>
                  </div>
                </div>
                {nodes.length > GRAPH_CANVAS_NODE_LIMIT ? (
                  <div className="graph-canvas-limit" role="alert">
                    <AlertTriangle size={16} />
                    {t("Canvas shows {visible} of {total} loaded nodes; the complete list remains below.", {
                      visible: GRAPH_CANVAS_NODE_LIMIT,
                      total: nodes.length,
                    })}
                  </div>
                ) : null}
                <GraphCanvas
                  view={view}
                  nodes={canvasNodes}
                  edges={canvasEdges}
                  metadataByKey={metadataByKey}
                  layout={activeLayout}
                  selectedNodeId={activeSelection}
                  onSelect={selectNode}
                  t={t}
                />
              </section>

              <GraphListFallback
                runId={runId}
                nodes={nodes}
                edges={edges}
                metadataByKey={metadataByKey}
                selectedNodeId={activeSelection}
                onSelect={selectNode}
                onOpenAction={onOpenAction}
                t={t}
              />
            </div>
          )}

          {staleCursorError ? (
            <div className="graph-pagination-error" role="alert">
              <AlertTriangle size={18} />
              <div>
                <strong>{t("Graph snapshot changed")}</strong>
                <span>{t("Restart pagination to avoid mixing records from different topologies.")}</span>
              </div>
              <button className="secondary-button" type="button" onClick={restartSnapshot}>
                <RefreshCw size={15} /> {t("Restart Graph snapshot")}
              </button>
            </div>
          ) : graph.isFetchNextPageError && graph.error ? (
            <div className="graph-pagination-error"><ErrorState error={graph.error} /></div>
          ) : null}

          {hasMore && !staleCursorError ? (
            <button
              className="secondary-button graph-load-more"
              type="button"
              disabled={graph.isFetchingNextPage}
              onClick={() => void graph.fetchNextPage()}
            >
              {graph.isFetchingNextPage ? (
                <Loader2 className="spin" size={15} />
              ) : (
                <GitBranch size={15} />
              )}
              {t("Load more Graph records")}
            </button>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function GraphCanvas({
  view,
  nodes,
  edges,
  metadataByKey,
  layout,
  selectedNodeId,
  onSelect,
  t,
}: {
  view: GraphViewKind;
  nodes: GraphNode[];
  edges: GraphEdge[];
  metadataByKey: Map<string, GraphTypeMetadata>;
  layout: GraphLayout;
  selectedNodeId: string;
  onSelect: (nodeId: string) => void;
  t: ReturnType<typeof useI18n>["t"];
}) {
  const viewLabel = GRAPH_VIEWS.find((candidate) => candidate.value === view)?.label ?? view;
  return (
    <svg
      className="graph-canvas"
      role="img"
      aria-label={t("{view} Graph visualization", { view: t(viewLabel) })}
      viewBox={viewportValue(layout.viewport)}
    >
      <title>{t("{view} Graph visualization", { view: t(viewLabel) })}</title>
      <desc>{t("Use Tab to reach nodes, then Enter or Space to inspect one. A complete list follows the canvas.")}</desc>
      <g className="graph-edge-layer" aria-hidden="true">
        {edges.map((edge) => {
          const source = layout.positions.get(edge.source);
          const target = layout.positions.get(edge.target);
          if (!source || !target) return null;
          const metadata = metadataByKey.get(metadataKey("edge", edge.type));
          return (
            <line
              key={edge.id}
              x1={source.x}
              y1={source.y}
              x2={target.x}
              y2={target.y}
              style={{ "--graph-type-color": safeGraphColor(metadata?.color) } as CSSProperties}
              className={edge.projection_quality === "exact" ? "" : "partial"}
            />
          );
        })}
      </g>
      <g className="graph-node-layer">
        {nodes.map((node) => {
          const position = layout.positions.get(node.id);
          if (!position) return null;
          const metadata = metadataByKey.get(metadataKey("node", node.type));
          const displayLabel = graphDisplayLabel(node.label, t);
          const displayType = graphDisplayLabel(metadata?.label ?? node.type, t);
          const selected = selectedNodeId === node.id;
          return (
            <g
              key={node.id}
              data-testid={`graph-node-${node.id}`}
              className={`graph-node${selected ? " selected" : ""}${node.projection_quality === "exact" ? "" : " partial"}`}
              transform={`translate(${position.x} ${position.y})`}
              role="button"
              tabIndex={0}
              aria-label={t("Inspect {label}", { label: displayLabel })}
              onClick={() => onSelect(node.id)}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                onSelect(node.id);
              }}
            >
              <circle
                r="24"
                style={{ "--graph-type-color": safeGraphColor(metadata?.color) } as CSSProperties}
              />
              <text y="38" textAnchor="middle">{boundedSvgLabel(displayLabel)}</text>
              <title>{`${displayType}: ${displayLabel}`}</title>
            </g>
          );
        })}
      </g>
    </svg>
  );
}

function GraphListFallback({
  runId,
  nodes,
  edges,
  metadataByKey,
  selectedNodeId,
  onSelect,
  onOpenAction,
  t,
}: {
  runId: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  metadataByKey: Map<string, GraphTypeMetadata>;
  selectedNodeId: string;
  onSelect: (nodeId: string) => void;
  onOpenAction: (actionId: string) => void;
  t: ReturnType<typeof useI18n>["t"];
}) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const selectedNode = nodeById.get(selectedNodeId);
  const actionId = selectedNode ? graphActionId(selectedNode, runId) : null;
  const selectedNodeLabel = selectedNode
    ? graphDisplayLabel(selectedNode.label, t)
    : "";
  const selectedNodeType = selectedNode
    ? graphDisplayLabel(
        metadataByKey.get(metadataKey("node", selectedNode.type))?.label ??
          selectedNode.type,
        t,
      )
    : "";
  return (
    <section className="graph-list-fallback" role="region" aria-label={t("Complete Graph list")}>
      <div className="graph-list-heading">
        <div>
          <span className="panel-kicker">{t("Accessible fallback")}</span>
          <h4>{t("Complete Graph list")}</h4>
        </div>
        <span>{t("All loaded records")}</span>
      </div>
      <h5>{t("Nodes")}</h5>
      <ul className="graph-node-list">
        {nodes.map((node) => {
          const metadata = metadataByKey.get(metadataKey("node", node.type));
          const displayLabel = graphDisplayLabel(node.label, t);
          const displayType = graphDisplayLabel(metadata?.label ?? node.type, t);
          const selected = node.id === selectedNodeId;
          return (
            <li key={node.id}>
              <button
                type="button"
                className={selected ? "selected" : ""}
                aria-current={selected ? "true" : undefined}
                onClick={() => onSelect(node.id)}
              >
                <span
                  className="graph-list-marker"
                  style={{ "--graph-type-color": safeGraphColor(metadata?.color) } as CSSProperties}
                aria-hidden="true"
              />
              <span>
                  <strong>{t("Inspect {label}", { label: displayLabel })}</strong>
                  <small>{displayType}</small>
                </span>
                <code>{node.id}</code>
              </button>
            </li>
          );
        })}
      </ul>

      {selectedNode ? (
        <article className="graph-node-inspector" aria-label={t("Selected Graph node")}>
          <div>
            <span className="panel-kicker">{t("Selected node")}</span>
            <h5>{selectedNodeLabel}</h5>
            <code>{selectedNode.id}</code>
          </div>
          <dl>
            <div>
              <dt>{t("Type")}</dt>
              <dd>{selectedNodeType}</dd>
            </div>
            <div>
              <dt>{t("Status")}</dt>
              <dd>{selectedNode.status ? t(selectedNode.status) : t("Unknown")}</dd>
            </div>
            <div>
              <dt>{t("Projection quality")}</dt>
              <dd>{t(selectedNode.projection_quality)}</dd>
            </div>
            <div>
              <dt>{t("Provenance")}</dt>
              <dd>{selectedNode.provenance_refs.length ? selectedNode.provenance_refs.join(", ") : t("None")}</dd>
            </div>
          </dl>
          {selectedNode.partial_reasons.length ? (
            <ul className="partial-reason-list">
              {selectedNode.partial_reasons.map((reason) => <li key={reason}><code>{reason}</code></li>)}
            </ul>
          ) : null}
          {actionId ? (
            <button
              className="secondary-button"
              type="button"
              aria-label={t("Open Action {id}", { id: actionId })}
              onClick={() => onOpenAction(actionId)}
            >
              <ExternalLink size={15} /> {t("Open in Actions")}
            </button>
          ) : null}
        </article>
      ) : null}

      <h5>{t("Relations")}</h5>
      <ol className="graph-edge-list">
        {edges.map((edge) => {
          const metadata = metadataByKey.get(metadataKey("edge", edge.type));
          const displayType = graphDisplayLabel(metadata?.label ?? edge.type, t);
          const sourceLabel = nodeById.get(edge.source)?.label;
          const targetLabel = nodeById.get(edge.target)?.label;
          return (
            <li key={edge.id}>
              <span
                className="graph-edge-marker"
                style={{ "--graph-type-color": safeGraphColor(metadata?.color) } as CSSProperties}
                aria-hidden="true"
              />
              <span>
                <strong>{displayType}</strong>
                <small>
                  {sourceLabel
                    ? graphDisplayLabel(sourceLabel, t)
                    : t("Unresolved endpoint")}
                  {" → "}
                  {targetLabel
                    ? graphDisplayLabel(targetLabel, t)
                    : t("Unresolved endpoint")}
                </small>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function emptyFilters(scopeKey: string): ScopedFilters {
  return { edgeType: "", nodeType: "", scopeKey, search: "", searchDraft: "" };
}

function graphRootKey(runId: string) {
  return [GRAPH_QUERY_ROOT, runId] as const;
}

function graphQueryKey(
  runId: string,
  expectedEngagementId: string,
  view: GraphViewKind,
  filters: ScopedFilters,
  focusId: string,
) {
  return [
    ...graphRootKey(runId),
    expectedEngagementId,
    view,
    filters.nodeType,
    filters.edgeType,
    filters.search,
    focusId,
  ] as const;
}

function graphAuthLatchMap(queryClient: QueryClient): Map<string, GraphAuthLatch> {
  let latches = graphAuthLatches.get(queryClient);
  if (!latches) {
    latches = new Map();
    graphAuthLatches.set(queryClient, latches);
  }
  return latches;
}

function beginGraphAuthRequest(queryClient: QueryClient, runId: string): number {
  const latches = graphAuthLatchMap(queryClient);
  const previous = latches.get(runId);
  const epoch = (previous?.latestEpoch ?? 0) + 1;
  latches.set(runId, {
    latestEpoch: epoch,
    settledEpoch: previous?.settledEpoch ?? 0,
    error: previous?.error ?? null,
  });
  return epoch;
}

function resolveGraphAuthRequest(
  queryClient: QueryClient,
  runId: string,
  epoch: number,
) {
  const latches = graphAuthLatchMap(queryClient);
  const current = latches.get(runId);
  if (!current || epoch < current.settledEpoch) return;
  latches.set(runId, { ...current, settledEpoch: epoch, error: null });
}

function rejectGraphAuthRequest(
  queryClient: QueryClient,
  runId: string,
  epoch: number,
  error: RiftXAPIError,
): boolean {
  const latches = graphAuthLatchMap(queryClient);
  const current = latches.get(runId);
  if (!current || epoch < current.settledEpoch) return false;
  latches.set(runId, { ...current, settledEpoch: epoch, error });
  return true;
}

function readGraphAuthError(
  queryClient: QueryClient,
  runId: string,
): RiftXAPIError | null {
  return graphAuthLatches.get(queryClient)?.get(runId)?.error ?? null;
}

function clearGraphAuthLatch(queryClient: QueryClient, runId: string) {
  graphAuthLatches.get(queryClient)?.delete(runId);
}

function purgeRunGraphCache(queryClient: QueryClient, runId: string) {
  queryClient.setQueriesData<InfiniteData<GraphViewPage>>(
    { queryKey: graphRootKey(runId) },
    { pages: [], pageParams: [] },
  );
  queryClient.removeQueries({
    queryKey: graphRootKey(runId),
    type: "inactive",
  });
}

function retryGraphQuery(failureCount: number, error: Error): boolean {
  if (error instanceof RiftXAPIError && [401, 403].includes(error.status)) return false;
  return failureCount < GRAPH_QUERY_RETRY_LIMIT;
}

function graphAuthorizationError(error: Error | null): RiftXAPIError | null {
  return error instanceof RiftXAPIError && [401, 403].includes(error.status)
    ? error
    : null;
}

function graphStaleCursorError(error: Error | null): RiftXAPIError | null {
  return error instanceof RiftXAPIError &&
    error.status === 409 &&
    ["stale_graph_cursor", "graph_cursor_stale"].includes(error.code)
    ? error
    : null;
}

function validateGraphPages(
  pages: readonly GraphViewPage[],
  runId: string,
  expectedEngagementId: string,
  view: GraphViewKind,
): GraphIntegrity {
  const issues: string[] = [];
  let scopeMismatch = false;
  const nodesById = new Map<string, GraphNode>();
  const edgeIds = new Set<string>();
  const edges: GraphEdge[] = [];
  const firstSnapshot = pages.length ? graphSnapshotIdentity(pages[0]!) : null;

  if (pages.length && !firstSnapshot) issues.push("snapshot_identity_missing");
  for (const page of pages) {
    const pageNodeIds = new Set<string>();
    if (
      page.scope.run_id !== runId ||
      page.scope.engagement_id !== expectedEngagementId ||
      page.view !== view
    ) {
      scopeMismatch = true;
      issues.push("scope_or_view_mismatch");
    }
    const snapshot = graphSnapshotIdentity(page);
    if (!snapshot) {
      issues.push("snapshot_identity_missing");
    } else if (firstSnapshot && snapshot !== firstSnapshot) {
      issues.push("snapshot_identity_drift");
    }
    const hasCursor =
      typeof page.next_cursor === "string" && page.next_cursor.trim().length > 0;
    if (
      page.has_more !== hasCursor ||
      (!page.has_more && page.next_cursor !== null)
    ) {
      issues.push("pagination_cursor_contract_invalid");
    }
    for (const node of page.nodes) {
      if (pageNodeIds.has(node.id)) issues.push("duplicate_graph_node");
      pageNodeIds.add(node.id);
      const existing = nodesById.get(node.id);
      if (existing && !sameGraphNodeSemantics(existing, node)) {
        issues.push("duplicate_graph_node_semantic_conflict");
      } else if (!existing) {
        nodesById.set(node.id, node);
      }
    }
    for (const edge of page.edges) {
      if (edgeIds.has(edge.id)) issues.push("duplicate_graph_edge");
      edgeIds.add(edge.id);
      edges.push(edge);
    }
  }
  if (
    edges.some(
      (edge) => !nodesById.has(edge.source) || !nodesById.has(edge.target),
    )
  ) {
    issues.push("orphan_graph_edge_endpoint");
  }
  const uniqueIssues = uniqueStrings(issues);
  return { issues: uniqueIssues, ok: uniqueIssues.length === 0, scopeMismatch };
}

function sameGraphNodeSemantics(left: GraphNode, right: GraphNode): boolean {
  return (
    left.id === right.id &&
    left.type === right.type &&
    left.domain_id === right.domain_id &&
    left.label === right.label &&
    left.status === right.status &&
    sameNormalizedStrings(left.provenance_refs, right.provenance_refs) &&
    left.projection_quality === right.projection_quality &&
    sameNormalizedStrings(left.partial_reasons, right.partial_reasons)
  );
}

function sameNormalizedStrings(
  left: readonly string[],
  right: readonly string[],
): boolean {
  if (left.length !== right.length) return false;
  const normalizedLeft = [...left].sort();
  const normalizedRight = [...right].sort();
  return normalizedLeft.every((value, index) => value === normalizedRight[index]);
}

function graphSnapshotIdentity(page: GraphViewPage): string | null {
  if (typeof page.snapshot_id === "string" && page.snapshot_id.trim()) {
    return page.snapshot_id;
  }
  if (typeof page.snapshot === "string" && page.snapshot.trim()) return page.snapshot;
  if (
    typeof page.snapshot === "object" &&
    page.snapshot !== null &&
    typeof page.snapshot.id === "string" &&
    page.snapshot.id.trim()
  ) {
    return page.snapshot.id;
  }
  return null;
}

function mergeGraphNodes(pages: readonly GraphViewPage[]): GraphNode[] {
  const byId = new Map<string, GraphNode>();
  for (const page of pages) {
    for (const node of page.nodes) {
      if (!byId.has(node.id)) byId.set(node.id, node);
    }
  }
  return [...byId.values()];
}

function mergeGraphEdges(pages: readonly GraphViewPage[]): GraphEdge[] {
  const byId = new Map<string, GraphEdge>();
  for (const page of pages) {
    for (const edge of page.edges) byId.set(edge.id, edge);
  }
  return [...byId.values()];
}

function mergeTypeMetadata(pages: readonly GraphViewPage[]): GraphTypeMetadata[] {
  const byKey = new Map<string, GraphTypeMetadata>();
  for (const page of pages) {
    for (const item of page.type_metadata) {
      byKey.set(metadataKey(item.kind, item.type), item);
    }
  }
  return [...byKey.values()];
}

function metadataKey(kind: GraphTypeMetadata["kind"], type: string): string {
  return `${kind}:${type}`;
}

function topologySignature(nodes: GraphNode[], edges: GraphEdge[]): string {
  const nodeTopology = nodes
    .map((node) => `${node.id}\u001f${node.type}`)
    .sort()
    .join("\u001e");
  const edgeTopology = edges
    .map((edge) => `${edge.id}\u001f${edge.type}\u001f${edge.source}\u001f${edge.target}`)
    .sort()
    .join("\u001e");
  return `${nodeTopology}\u001d${edgeTopology}`;
}

function createGraphLayout(
  nodes: GraphNode[],
  _edges: GraphEdge[],
  scopeKey: string,
  topology: string,
  revision: number,
): GraphLayout {
  const sorted = [...nodes].sort((left, right) => left.id.localeCompare(right.id));
  const columns = Math.max(1, Math.min(5, Math.ceil(Math.sqrt(sorted.length || 1))));
  const rows = Math.max(1, Math.ceil(sorted.length / columns));
  const width = Math.max(720, columns * 170 + 100);
  const height = Math.max(420, rows * 130 + 100);
  const positions = new Map<string, GraphPosition>();
  sorted.forEach((node, index) => {
    const shifted = (index + revision) % Math.max(sorted.length, 1);
    const column = shifted % columns;
    const row = Math.floor(shifted / columns);
    positions.set(node.id, {
      x: 85 + column * ((width - 170) / Math.max(columns - 1, 1)),
      y: 75 + row * ((height - 150) / Math.max(rows - 1, 1)),
    });
  });
  return {
    positions,
    revision,
    scopeKey,
    topology,
    viewport: { height, width, x: 0, y: 0 },
  };
}

function viewportValue(viewport: GraphViewport): string {
  return [viewport.x, viewport.y, viewport.width, viewport.height].join(" ");
}

function graphActionId(node: GraphNode, runId: string): string | null {
  // This is the only semantic bridge in the UI. It accepts only the explicit
  // server action node contract and its domain ID; no PlanItem/time/focus
  // correlation is attempted.
  if (
    node.type !== "action" ||
    node.projection_quality !== "exact" ||
    typeof node.id !== "string" ||
    typeof node.domain_id !== "string" ||
    !GRAPH_ACTION_COMPONENT_PATTERN.test(runId) ||
    !GRAPH_ACTION_COMPONENT_PATTERN.test(node.domain_id)
  ) {
    return null;
  }
  const expectedNodeId = `action:${runId}:${node.domain_id}`;
  return GRAPH_NODE_ID_PATTERN.test(node.id) && node.id === expectedNodeId
    ? node.domain_id
    : null;
}

function snapshotIsStale(page: GraphViewPage): boolean {
  return typeof page.snapshot === "object" && page.snapshot !== null
    ? page.snapshot.stale
    : false;
}

function safeGraphColor(value: string | undefined): string {
  if (value && /^#[0-9a-f]{3,8}$/i.test(value)) return value;
  if (value && /^var\(--[a-z0-9-]+\)$/i.test(value)) return value;
  return "var(--mint)";
}

function boundedSvgLabel(value: string): string {
  return value.length <= 26 ? value : `${value.slice(0, 23)}…`;
}

function graphDisplayLabel(
  label: string,
  t: ReturnType<typeof useI18n>["t"],
): string {
  const planItem = /^Plan item ([0-9]+)$/.exec(label);
  return planItem
    ? t("Plan item {sequence}", { sequence: planItem[1]! })
    : t(label);
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)];
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export default RunGraphWorkspace;
