import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, RiftXAPIError } from "../api/client";
import type { GraphViewPage } from "../api/types";
import { LanguageProvider, languageStorageKey } from "../i18n";
import { RunGraphWorkspace } from "./RunGraphWorkspace";

const page = (overrides: Partial<GraphViewPage> = {}): GraphViewPage => ({
  scope: { engagement_id: "engagement-1", run_id: "run-1" },
  view: "task",
  snapshot: { id: "snapshot-1", stale: false },
  nodes: [
    {
      id: "plan_item:run-1:plan-1",
      type: "plan_item",
      domain_id: "plan-1",
      label: "Enumerate target",
      status: "pending",
      provenance_refs: ["working_memory.run_plan"],
      projection_quality: "exact",
      partial_reasons: [],
    },
    {
      id: "action:run-1:action-1",
      type: "action",
      domain_id: "action-1",
      label: "Inspect service",
      status: "succeeded",
      provenance_refs: ["tool_call_intents"],
      projection_quality: "exact",
      partial_reasons: [],
    },
    {
      id: "unassigned_actions:run-1",
      type: "unassigned_actions",
      domain_id: "run-1",
      label: "Unassigned actions",
      status: "partial",
      provenance_refs: ["tool_call_intents"],
      projection_quality: "partial",
      partial_reasons: ["action_plan_lineage_unavailable"],
    },
  ],
  edges: [
    {
      id: "edge-1",
      type: "unassigned",
      source: "action:run-1:action-1",
      target: "unassigned_actions:run-1",
      provenance_refs: ["tool_call_intents"],
      projection_quality: "partial",
    },
  ],
  type_metadata: [
    {
      kind: "node",
      type: "plan_item",
      label: "Server plan item",
      color: "#2563eb",
    },
    {
      kind: "node",
      type: "action",
      label: "Server action",
      color: "#16a34a",
    },
    {
      kind: "edge",
      type: "unassigned",
      label: "Server unassigned relation",
      color: "#f97316",
    },
    {
      kind: "node",
      type: "unassigned_actions",
      label: "Server unassigned bucket",
      color: "#f59e0b",
    },
  ],
  partial_reasons: [],
  truncated: false,
  has_more: false,
  next_cursor: null,
  ...overrides,
});

function installLocalStorage() {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      get length() { return values.size; },
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      key: (index: number) => [...values.keys()][index] ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, String(value)),
    } satisfies Storage,
  });
}

function renderWorkspace(
  props: Partial<React.ComponentProps<typeof RunGraphWorkspace>> = {},
  queryClient = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  }),
  { language = false } = {},
) {
  const resolved = {
    runId: "run-1",
    expectedEngagementId: "engagement-1",
    view: "task" as const,
    focusId: "",
    onViewChange: vi.fn(),
    onFocusChange: vi.fn(),
    onOpenAction: vi.fn(),
    ...props,
  };
  const workspace = (
    <QueryClientProvider client={queryClient}>
      <RunGraphWorkspace {...resolved} />
    </QueryClientProvider>
  );
  const rendered = render(
    language ? <LanguageProvider>{workspace}</LanguageProvider> : workspace,
  );
  return { ...rendered, props: resolved, queryClient };
}

function expectRunGraphCacheMasked(queryClient: QueryClient, runId: string) {
  const entries = queryClient.getQueriesData<{ pages?: unknown[] }>({
    queryKey: ["run-graph", runId],
  });
  expect(
    entries.every(([, data]) => data === undefined || data.pages?.length === 0),
  ).toBe(true);
}

function ControlledGraphSelection() {
  const [focusId, setFocusId] = useState("");
  return (
    <>
      <output aria-label="Graph URL focus">{focusId}</output>
      <RunGraphWorkspace
        runId="run-1"
        expectedEngagementId="engagement-1"
        view="task"
        focusId={focusId}
        onViewChange={vi.fn()}
        onFocusChange={setFocusId}
        onOpenAction={vi.fn()}
      />
    </>
  );
}

describe("RunGraphWorkspace", () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
    installLocalStorage();
    document.documentElement.lang = "";
  });

  it("uses server metadata as the single legend and filter vocabulary", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockResolvedValue(page());
    renderWorkspace();

    expect(await screen.findByRole("heading", { name: "Run Graph" })).toBeInTheDocument();
    const legend = await screen.findByRole("list", { name: "Graph legend" });
    expect(within(legend).getByText("Server plan item")).toBeInTheDocument();
    expect(within(legend).getByText("Server action")).toBeInTheDocument();
    expect(within(legend).getByText("Server unassigned relation")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Server plan item" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Server unassigned relation" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Node type"), {
      target: { value: "plan_item" },
    });
    fireEvent.change(screen.getByLabelText("Edge type"), {
      target: { value: "unassigned" },
    });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search Graph" }), {
      target: { value: "target:443" },
    });
    fireEvent.submit(screen.getByRole("search"));

    await waitFor(() =>
      expect(listRunGraph).toHaveBeenLastCalledWith(
        "run-1",
        expect.objectContaining({
          edgeType: "unassigned",
          nodeType: "plan_item",
          search: "target:443",
          view: "task",
        }),
        expect.anything(),
      ),
    );
  });

  it("localizes known server Graph labels without changing metadata-driven filter values", async () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    const localizedPage = page({
      nodes: [
        { ...page().nodes[0]!, label: "Plan item 12" },
        { ...page().nodes[1]!, label: "Action" },
        { ...page().nodes[2]!, label: "Unassigned actions" },
      ],
      type_metadata: [
        { kind: "node", type: "plan_item", label: "Plan item", color: "#2563eb" },
        { kind: "node", type: "action", label: "Action", color: "#16a34a" },
        {
          kind: "node",
          type: "unassigned_actions",
          label: "Unassigned actions",
          color: "#f59e0b",
        },
        { kind: "edge", type: "unassigned", label: "Unassigned", color: "#f97316" },
      ],
    });
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockResolvedValue(localizedPage);

    renderWorkspace(
      { focusId: "plan_item:run-1:plan-1" },
      undefined,
      { language: true },
    );

    const legend = await screen.findByRole("list", { name: "图谱图例" });
    expect(within(legend).getByText("计划项")).toBeInTheDocument();
    expect(within(legend).getByText("任务行动")).toBeInTheDocument();
    expect(within(legend).getByText("未分配的任务行动")).toBeInTheDocument();
    expect(within(legend).getByText("未分配")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "计划项" })).toHaveValue("plan_item");
    expect(screen.getByRole("option", { name: "未分配" })).toHaveValue("unassigned");

    const fallback = screen.getByRole("region", { name: "完整图谱列表" });
    expect(
      within(fallback).getByRole("button", { name: /^查看 计划项 12/ }),
    ).toHaveAttribute("aria-current", "true");
    const inspector = within(fallback).getByRole("article", { name: "已选图谱节点" });
    expect(within(inspector).getByRole("heading", { name: "计划项 12" })).toBeInTheDocument();
    expect(within(inspector).getByText("计划项")).toBeInTheDocument();
    expect(within(fallback).getByText("任务行动 → 未分配的任务行动")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("节点类型"), {
      target: { value: "plan_item" },
    });
    fireEvent.change(screen.getByLabelText("关系类型"), {
      target: { value: "unassigned" },
    });
    await waitFor(() =>
      expect(listRunGraph).toHaveBeenLastCalledWith(
        "run-1",
        expect.objectContaining({
          edgeType: "unassigned",
          nodeType: "plan_item",
        }),
        expect.anything(),
      ),
    );
  });

  it("preserves unknown custom Graph labels in Chinese mode", async () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      page({
        nodes: [
          {
            ...page().nodes[0]!,
            id: "custom:run-1:future-1",
            type: "future_node",
            domain_id: "future-1",
            label: "Plan item custom",
          },
        ],
        edges: [],
        type_metadata: [
          {
            kind: "node",
            type: "future_node",
            label: "Future custom category",
            color: "#2563eb",
          },
        ],
      }),
    );

    renderWorkspace(
      { focusId: "custom:run-1:future-1" },
      undefined,
      { language: true },
    );

    const legend = await screen.findByRole("list", { name: "图谱图例" });
    expect(within(legend).getByText("Future custom category")).toBeInTheDocument();
    const fallback = screen.getByRole("region", { name: "完整图谱列表" });
    expect(
      within(fallback).getByRole("button", { name: /^查看 Plan item custom/ }),
    ).toBeInTheDocument();
    const inspector = within(fallback).getByRole("article", { name: "已选图谱节点" });
    expect(
      within(inspector).getByRole("heading", { name: "Plan item custom" }),
    ).toBeInTheDocument();
    expect(within(inspector).getByText("Future custom category")).toBeInTheDocument();
  });

  it("provides an accessible SVG and complete list fallback with explicit focus lineage", async () => {
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      page({
        nodes: page().nodes.map((node) =>
          node.id === "action:run-1:action-1"
            ? ({ ...node, internal_secret: "GRAPH_SECRET_CANARY" } as typeof node)
            : node,
        ),
      }),
    );
    const { props } = renderWorkspace({ focusId: "action:run-1:action-1" });

    expect(await screen.findByRole("img", { name: "Task Graph visualization" })).toBeInTheDocument();
    const fallback = screen.getByRole("region", { name: "Complete Graph list" });
    expect(await within(fallback).findByRole("button", { name: /Inspect service/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "Open Action action-1" }));
    expect(props.onOpenAction).toHaveBeenCalledWith("action-1");
    expect(document.body).not.toHaveTextContent("GRAPH_SECRET_CANARY");
    expect(JSON.stringify(window.sessionStorage) ?? "").not.toContain("GRAPH_SECRET_CANARY");
    expect(JSON.stringify(window.localStorage) ?? "").not.toContain("GRAPH_SECRET_CANARY");
  });

  it.each([
    {
      name: "foreign-Run node ID",
      node: { ...page().nodes[1]!, id: "action:run-2:action-1" },
    },
    {
      name: "same-Run wrong-Action node ID",
      node: { ...page().nodes[1]!, id: "action:run-1:action-2" },
    },
    {
      name: "partial Action projection",
      node: { ...page().nodes[1]!, projection_quality: "partial" },
    },
    {
      name: "legacy colon Action component",
      node: {
        ...page().nodes[1]!,
        id: "action:run-1:action:legacy",
        domain_id: "action:legacy",
      },
    },
    {
      name: "Unicode Action component",
      node: {
        ...page().nodes[1]!,
        id: "action:run-1:行动-1",
        domain_id: "行动-1",
      },
    },
  ])("fails closed when Graph-to-Action receives a $name", async ({ node }) => {
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      page({ nodes: [node], edges: [] }),
    );
    const { props } = renderWorkspace({ focusId: node.id });

    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    expect(within(fallback).getByRole("article", { name: "Selected Graph node" })).toBeInTheDocument();
    expect(within(fallback).queryByRole("button", { name: /Open Action/ })).not.toBeInTheDocument();
    expect(props.onOpenAction).not.toHaveBeenCalled();
  });

  it("marks missing versus unloaded focus and exposes bounded pagination", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [page().nodes[0]!, page().nodes[2]!],
          edges: [],
          has_more: true,
          next_cursor: "cursor-2",
          truncated: true,
          partial_reasons: ["bounded_page"],
        }),
      )
      .mockResolvedValueOnce(
        page({
          nodes: [page().nodes[1]!],
          edges: [],
          has_more: false,
          next_cursor: null,
        }),
      );
    renderWorkspace({ focusId: "action:run-1:action-1" });

    expect(await screen.findByText("Focus is not in the loaded Graph pages")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Graph response is truncated");
    fireEvent.click(screen.getByRole("button", { name: "Load more Graph records" }));

    const fallback = screen.getByRole("region", { name: "Complete Graph list" });
    expect(await within(fallback).findByRole("button", { name: /Inspect service/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    expect(listRunGraph).toHaveBeenLastCalledWith(
      "run-1",
      expect.objectContaining({ cursor: "cursor-2" }),
      expect.anything(),
    );
  });

  it("masks cached Graph data immediately after a forbidden refresh and never retries", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [{ ...page().nodes[0]!, label: "CACHED_GRAPH_SECRET_CANARY" }],
          edges: [],
        }),
      )
      .mockRejectedValueOnce(
        new RiftXAPIError(403, "graph_forbidden", "Graph access forbidden"),
      );
    renderWorkspace();
    expect(await screen.findByText("CACHED_GRAPH_SECRET_CANARY")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh Graph" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Graph access forbidden");
    expect(screen.queryByText("CACHED_GRAPH_SECRET_CANARY")).not.toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(listRunGraph).toHaveBeenCalledTimes(2);
  });

  it("revalidates before revealing a cached remount and purges the Run on revoked access", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0 } },
    });
    const listRunGraph = vi
      .spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [{ ...page().nodes[0]!, label: "REMOUNT_GRAPH_SECRET_CANARY" }],
          edges: [],
        }),
      )
      .mockRejectedValueOnce(
        new RiftXAPIError(403, "graph_forbidden", "Graph access revoked on remount"),
      );
    const first = renderWorkspace({}, queryClient);
    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    expect(
      within(fallback).getByRole("button", { name: /REMOUNT_GRAPH_SECRET_CANARY/ }),
    ).toBeInTheDocument();

    first.unmount();
    renderWorkspace({}, queryClient);

    expect(screen.queryByText("REMOUNT_GRAPH_SECRET_CANARY")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Graph access revoked on remount",
    );
    expect(listRunGraph).toHaveBeenCalledTimes(2);
    expectRunGraphCacheMasked(queryClient, "run-1");
  });

  it("revalidates cached views before reveal and a denial masks every view for the Run", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0 } },
    });
    const listRunGraph = vi
      .spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [{ ...page().nodes[0]!, label: "TASK_CACHE_SECRET_CANARY" }],
          edges: [],
        }),
      )
      .mockResolvedValueOnce(
        page({
          view: "evidence",
          nodes: [{ ...page().nodes[0]!, label: "Evidence view node" }],
          edges: [],
        }),
      )
      .mockRejectedValueOnce(
        new RiftXAPIError(401, "graph_unauthorized", "Graph authorization expired"),
      );
    const rendered = renderWorkspace({}, queryClient);
    expect(await screen.findByText("TASK_CACHE_SECRET_CANARY")).toBeInTheDocument();

    rendered.rerender(
      <QueryClientProvider client={queryClient}>
        <RunGraphWorkspace {...rendered.props} view="evidence" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Evidence view node")).toBeInTheDocument();

    rendered.rerender(
      <QueryClientProvider client={queryClient}>
        <RunGraphWorkspace {...rendered.props} view="task" />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("TASK_CACHE_SECRET_CANARY")).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Graph authorization expired",
    );
    expect(listRunGraph).toHaveBeenCalledTimes(3);
    expectRunGraphCacheMasked(queryClient, "run-1");
  });

  it("ignores a superseded authorization failure after a newer view succeeds", async () => {
    let rejectSuperseded!: (reason?: unknown) => void;
    const superseded = new Promise<GraphViewPage>((_resolve, reject) => {
      rejectSuperseded = reject;
    });
    const listRunGraph = vi
      .spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(page())
      .mockReturnValueOnce(superseded)
      .mockResolvedValueOnce(
        page({
          view: "evidence",
          nodes: [{ ...page().nodes[0]!, label: "Newer authorized view" }],
          edges: [],
        }),
      );
    const rendered = renderWorkspace();
    expect(await screen.findByRole("region", { name: "Complete Graph list" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh Graph" }));
    rendered.rerender(
      <QueryClientProvider client={rendered.queryClient}>
        <RunGraphWorkspace {...rendered.props} view="evidence" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Newer authorized view")).toBeInTheDocument();

    rejectSuperseded(
      new RiftXAPIError(403, "graph_forbidden", "Superseded Graph denial"),
    );
    await new Promise((resolve) => window.setTimeout(resolve, 0));

    expect(screen.getByText("Newer authorized view")).toBeInTheDocument();
    expect(screen.queryByText("Superseded Graph denial")).not.toBeInTheDocument();
    expect(listRunGraph).toHaveBeenCalledTimes(3);
  });

  it.each([
    {
      name: "500 response",
      error: new RiftXAPIError(500, "graph_unavailable", "Graph backend unavailable"),
    },
    {
      name: "network failure",
      error: new Error("Graph network unavailable"),
    },
  ])("keeps verified cached data but surfaces a stale warning after a $name", async ({ error }) => {
    const listRunGraph = vi
      .spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [{ ...page().nodes[0]!, label: "VERIFIED_GRAPH_CACHE" }],
          edges: [],
        }),
      )
      .mockRejectedValueOnce(error)
      .mockRejectedValueOnce(error)
      .mockResolvedValueOnce(
        page({
          snapshot: { id: "snapshot-2", stale: false },
          nodes: [{ ...page().nodes[0]!, label: "RECOVERED_GRAPH_SNAPSHOT" }],
          edges: [],
        }),
      );
    renderWorkspace();
    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    expect(
      within(fallback).getByRole("button", { name: /VERIFIED_GRAPH_CACHE/ }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh Graph" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Graph refresh failed; showing last verified snapshot");
    expect(alert).toHaveTextContent(error.message);
    expect(
      within(fallback).getByRole("button", { name: /VERIFIED_GRAPH_CACHE/ }),
    ).toBeInTheDocument();
    expect(listRunGraph).toHaveBeenCalledTimes(3);

    fireEvent.click(screen.getByRole("button", { name: "Retry Graph refresh" }));

    expect(
      await within(fallback).findByRole("button", { name: /RECOVERED_GRAPH_SNAPSHOT/ }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByText("Graph refresh failed; showing last verified snapshot"),
      ).not.toBeInTheDocument(),
    );
    expect(listRunGraph).toHaveBeenCalledTimes(4);
  });

  it("patches attributes in place while preserving selection, positions, and viewport", async () => {
    vi.spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(page())
      .mockResolvedValueOnce(
        page({
          snapshot: { id: "snapshot-2", stale: false },
          nodes: page().nodes.map((node) =>
            node.id === "plan_item:run-1:plan-1"
              ? { ...node, label: "Enumerate target (updated)", status: "running" }
              : node,
          ),
        }),
      );
    renderWorkspace();
    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    const planButton = within(fallback).getByRole("button", { name: /Enumerate target/ });
    fireEvent.click(planButton);
    const planShape = screen.getByTestId("graph-node-plan_item:run-1:plan-1");
    const position = planShape.getAttribute("transform");
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const graph = screen.getByRole("img", { name: "Task Graph visualization" });
    const viewport = graph.getAttribute("viewBox");

    fireEvent.click(screen.getByRole("button", { name: "Refresh Graph" }));

    expect(await screen.findAllByText("Enumerate target (updated)")).not.toHaveLength(0);
    expect(screen.getByTestId("graph-node-plan_item:run-1:plan-1")).toHaveAttribute(
      "transform",
      position,
    );
    expect(graph).toHaveAttribute("viewBox", viewport);
    expect(within(fallback).getByRole("button", { name: /Enumerate target \(updated\)/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it("keeps the same query, layout, and viewport when URL focus follows a loaded selection", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockResolvedValue(page());
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retryDelay: 0 } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ControlledGraphSelection />
      </QueryClientProvider>,
    );
    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    const graph = screen.getByRole("img", { name: "Task Graph visualization" });
    const actionShape = screen.getByTestId("graph-node-action:run-1:action-1");
    const position = actionShape.getAttribute("transform");
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const viewport = graph.getAttribute("viewBox");

    fireEvent.click(within(fallback).getByRole("button", { name: /Inspect service/ }));

    expect(screen.getByLabelText("Graph URL focus")).toHaveTextContent(
      "action:run-1:action-1",
    );
    expect(screen.getByTestId("graph-node-action:run-1:action-1")).toHaveAttribute(
      "transform",
      position,
    );
    expect(graph).toHaveAttribute("viewBox", viewport);
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(listRunGraph).toHaveBeenCalledTimes(1);
  });

  it("clears a deep-link focus back to the full Graph and reuses it on forward navigation", async () => {
    const listRunGraph = vi.spyOn(api, "listRunGraph").mockResolvedValue(page());
    const rendered = renderWorkspace({ focusId: "action:run-1:action-1" });
    const fallback = await screen.findByRole("region", { name: "Complete Graph list" });
    const actionButton = within(fallback).getByRole("button", { name: /Inspect service/ });
    expect(actionButton).toHaveAttribute("aria-current", "true");
    const actionShape = screen.getByTestId("graph-node-action:run-1:action-1");
    const position = actionShape.getAttribute("transform");
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const graph = screen.getByRole("img", { name: "Task Graph visualization" });
    const viewport = graph.getAttribute("viewBox");

    rendered.rerender(
      <QueryClientProvider client={rendered.queryClient}>
        <RunGraphWorkspace {...rendered.props} focusId="" />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(listRunGraph).toHaveBeenCalledTimes(2));
    expect(listRunGraph).toHaveBeenLastCalledWith(
      "run-1",
      expect.objectContaining({ focus: undefined }),
      expect.anything(),
    );
    expect(within(fallback).getByRole("button", { name: /Inspect service/ })).not.toHaveAttribute(
      "aria-current",
    );
    expect(screen.getByTestId("graph-node-action:run-1:action-1")).toHaveAttribute(
      "transform",
      position,
    );
    expect(graph).toHaveAttribute("viewBox", viewport);

    rendered.rerender(
      <QueryClientProvider client={rendered.queryClient}>
        <RunGraphWorkspace
          {...rendered.props}
          focusId="action:run-1:action-1"
        />
      </QueryClientProvider>,
    );

    expect(within(fallback).getByRole("button", { name: /Inspect service/ })).toHaveAttribute(
      "aria-current",
      "true",
    );
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(listRunGraph).toHaveBeenCalledTimes(2);
  });

  it("drops a superseded Run response and calls out a stale pagination cursor", async () => {
    let resolveOld!: (value: GraphViewPage) => void;
    const oldResponse = new Promise<GraphViewPage>((resolve) => {
      resolveOld = resolve;
    });
    vi.spyOn(api, "listRunGraph").mockImplementation((runId, options) => {
      if (runId === "run-1") return oldResponse;
      if (options.cursor) {
        return Promise.reject(
          new RiftXAPIError(409, "stale_graph_cursor", "Cursor belongs to an older Graph snapshot"),
        );
      }
      return Promise.resolve(
        page({
          scope: { engagement_id: "engagement-2", run_id: "run-2" },
          nodes: [{ ...page().nodes[0]!, id: "plan_item:run-2:plan-1", label: "Run 2 node" }],
          edges: [],
          has_more: true,
          next_cursor: "stale-cursor",
        }),
      );
    });
    const rendered = renderWorkspace();
    rendered.rerender(
      <QueryClientProvider client={rendered.queryClient}>
        <RunGraphWorkspace
          {...rendered.props}
          runId="run-2"
          expectedEngagementId="engagement-2"
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Run 2 node")).toBeInTheDocument();
    resolveOld(page({ nodes: [{ ...page().nodes[0]!, label: "OLD_RUN_GRAPH_SECRET" }] }));
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(screen.queryByText("OLD_RUN_GRAPH_SECRET")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Load more Graph records" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Graph snapshot changed");
    expect(screen.getByRole("button", { name: "Restart Graph snapshot" })).toBeInTheDocument();
  });

  it("fails closed when any successful page belongs to another engagement", async () => {
    vi.spyOn(api, "listRunGraph").mockResolvedValue(
      page({
        scope: { engagement_id: "foreign-engagement", run_id: "run-1" },
        nodes: [{ ...page().nodes[0]!, label: "FOREIGN_ENGAGEMENT_SECRET" }],
        edges: [],
      }),
    );
    renderWorkspace();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Graph response scope mismatch",
    );
    expect(screen.queryByText("FOREIGN_ENGAGEMENT_SECRET")).not.toBeInTheDocument();
  });

  it.each([
    {
      name: "has_more without a cursor",
      response: page({
        has_more: true,
        next_cursor: null,
        nodes: [{ ...page().nodes[0]!, label: "INVALID_CURSOR_SECRET" }],
        edges: [],
      }),
      hidden: "INVALID_CURSOR_SECRET",
    },
    {
      name: "duplicate primary nodes",
      response: page({
        nodes: [
          { ...page().nodes[0]!, label: "DUPLICATE_NODE_SECRET" },
          { ...page().nodes[0]!, label: "DUPLICATE_NODE_SECRET" },
        ],
        edges: [],
      }),
      hidden: "DUPLICATE_NODE_SECRET",
    },
    {
      name: "duplicate relations",
      response: page({
        edges: [page().edges[0]!, page().edges[0]!],
      }),
      hidden: "edge-1",
    },
    {
      name: "an orphan relation endpoint",
      response: page({
        nodes: [{ ...page().nodes[0]!, label: "ORPHAN_BATCH_SECRET" }],
        edges: [
          {
            ...page().edges[0]!,
            source: "plan_item:run-1:plan-1",
            target: "foreign:raw-orphan-endpoint",
          },
        ],
      }),
      hidden: "foreign:raw-orphan-endpoint",
    },
  ])("fails closed for a 200 page with $name", async ({ response, hidden }) => {
    vi.spyOn(api, "listRunGraph").mockResolvedValue(response);
    renderWorkspace();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Graph integrity check failed",
    );
    expect(screen.queryByText(hidden)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart Graph snapshot" })).toBeInTheDocument();
  });

  it.each(["snapshot", "node", "edge"] as const)(
    "fails the complete batch for cross-page %s drift or duplication",
    async (caseName) => {
      const first = page({
        nodes: page().nodes,
        edges: page().edges,
        has_more: true,
        next_cursor: "cursor-2",
      });
      const second = page({
        snapshot:
          caseName === "snapshot"
            ? { id: "snapshot-drift", stale: false }
            : page().snapshot,
        nodes:
          caseName === "node"
            ? [{ ...page().nodes[0]!, domain_id: "conflicting-plan" }]
            : [
                {
                  ...page().nodes[0]!,
                  id: "plan_item:run-1:plan-2",
                  domain_id: "plan-2",
                  label: "Second page node",
                },
              ],
        edges: caseName === "edge" ? [page().edges[0]!] : [],
        has_more: false,
        next_cursor: null,
      });
      vi.spyOn(api, "listRunGraph")
        .mockResolvedValueOnce(first)
        .mockResolvedValueOnce(second);
      renderWorkspace();
      expect(await screen.findByRole("button", { name: "Load more Graph records" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Load more Graph records" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Graph integrity check failed",
      );
      expect(screen.queryByText("Enumerate target")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Restart Graph snapshot" })).toBeInTheDocument();
    },
  );

  it.each([
    {
      name: "status",
      patch: { status: "running" },
    },
    {
      name: "provenance",
      patch: { provenance_refs: ["foreign.provenance"] },
    },
    {
      name: "projection quality",
      patch: { projection_quality: "exact" },
    },
  ])("fails closed when a repeated context endpoint drifts in $name", async ({ patch }) => {
    const sharedEndpoint = page().nodes[2]!;
    vi.spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(
        page({
          nodes: [page().nodes[1]!, sharedEndpoint],
          edges: [page().edges[0]!],
          has_more: true,
          next_cursor: "cursor-2",
        }),
      )
      .mockResolvedValueOnce(
        page({
          nodes: [{ ...sharedEndpoint, ...patch }],
          edges: [],
          has_more: false,
          next_cursor: null,
        }),
      );
    renderWorkspace();

    fireEvent.click(await screen.findByRole("button", { name: "Load more Graph records" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Graph integrity check failed",
    );
    expect(screen.queryByText("Unassigned actions")).not.toBeInTheDocument();
  });

  it("accepts and deduplicates consistent context endpoints repeated across edge pages", async () => {
    const sharedEndpoint = {
      ...page().nodes[2]!,
      provenance_refs: ["tool_call_intents", "working_memory.run_plan"],
      partial_reasons: ["action_plan_lineage_unavailable", "bounded_page"],
    };
    const first = page({
      nodes: [page().nodes[1]!, sharedEndpoint],
      edges: [page().edges[0]!],
      has_more: true,
      next_cursor: "cursor-2",
    });
    const second = page({
      nodes: [
        {
          ...sharedEndpoint,
          provenance_refs: [...sharedEndpoint.provenance_refs].reverse(),
          partial_reasons: [...sharedEndpoint.partial_reasons].reverse(),
        },
        page().nodes[0]!,
      ],
      edges: [
        {
          ...page().edges[0]!,
          id: "edge-2",
          source: "plan_item:run-1:plan-1",
        },
      ],
      has_more: false,
      next_cursor: null,
    });
    vi.spyOn(api, "listRunGraph")
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    renderWorkspace();

    fireEvent.click(await screen.findByRole("button", { name: "Load more Graph records" }));

    const fallback = screen.getByRole("region", { name: "Complete Graph list" });
    expect(await within(fallback).findByRole("button", { name: /Enumerate target/ })).toBeInTheDocument();
    expect(within(fallback).getAllByRole("button", { name: /Unassigned actions/ })).toHaveLength(1);
    expect(screen.queryByText("Graph integrity check failed")).not.toBeInTheDocument();
  });

  it("never encodes an Action to PlanItem relation in the safety fixture", () => {
    const fixture = page();
    const actionIds = new Set(
      fixture.nodes.filter((node) => node.type === "action").map((node) => node.id),
    );
    const planIds = new Set(
      fixture.nodes.filter((node) => node.type === "plan_item").map((node) => node.id),
    );
    expect(
      fixture.edges.some(
        (edge) => actionIds.has(edge.source) && planIds.has(edge.target),
      ),
    ).toBe(false);
  });
});
