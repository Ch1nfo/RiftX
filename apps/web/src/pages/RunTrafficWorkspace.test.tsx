import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import trafficMetadataDetailFixture from "../../../../tests/fixtures/traffic_metadata_detail.json";
import trafficMetadataListFixture from "../../../../tests/fixtures/traffic_metadata_list.json";
import { api, RiftXAPIError } from "../api/client";
import { LanguageProvider, languageStorageKey } from "../i18n";
import { RunTrafficWorkspace } from "./RunTrafficWorkspace";

const PARTIAL_REASONS = [
  "replay_lineage_not_persisted",
  "retention_unmanaged",
  "safety_gate_not_implemented",
  "scope_decision_not_persisted",
];

function trafficItem(exchangeId = "exchange-1", overrides: Record<string, unknown> = {}) {
  return {
    exchange_id: exchangeId,
    request_id: exchangeId,
    execution_key: `execution:${exchangeId}`,
    canonical_request_digest: "a".repeat(64),
    digest_stability: "server_instance",
    lineage: {
      run_id: "run-1",
      session_id: "session-1",
      tool_call_id: "tool-call-1",
      node_id: "local",
      node_status: "online",
    },
    method: "GET",
    url_summary: {
      availability: "available",
      scheme: "https",
      origin: "https://target.example.test",
      path_shape: "/…",
      path_segment_count: 2,
      redacted: true,
    },
    tls: {
      availability: "available",
      verified: true,
      client_certificate_used: false,
    },
    response: {
      status_code: 201,
      status_class: "success",
      elapsed_ms: 42,
      content_type: "application/json",
      content_length: 512,
      truncated: false,
    },
    artifacts: {
      request: {
        opaque_ref: `traffic-artifact:v1:${"b".repeat(64)}`,
        presence: "recorded_present",
        access: "metadata_only",
      },
      response: {
        opaque_ref: `traffic-artifact:v1:${"c".repeat(64)}`,
        presence: "recorded_present",
        access: "metadata_only",
      },
    },
    body: {
      request: { availability: "present", revealable: false, truncated: false },
      response: { availability: "present", revealable: false, truncated: false },
    },
    redirect: {
      availability: "available",
      count: 2,
      followed: true,
      origins: ["https://redirect-one.example.test", "https://target.example.test"],
      partial: false,
    },
    replay_of: {
      availability: "unavailable",
      request_id: null,
      reason: "not_persisted",
    },
    created_by: { availability: "available", kind: "agent_runtime" },
    created_at: "2026-08-01T00:00:00+00:00",
    scope_decision: {
      availability: "unavailable",
      decision: null,
      reference_kind: "run_scope",
      reason: "decision_not_persisted",
    },
    approval: {
      availability: "available",
      reference_id: "approval-1",
      status: "approved",
    },
    safety_gate: {
      availability: "unavailable",
      reference_id: null,
      reason: "not_implemented",
    },
    governance: {
      sensitivity: "restricted_sensitive",
      access: "metadata_only",
      retention: "legacy_unmanaged",
      reveal_capability: "disabled",
    },
    projection_quality: "partial",
    partial_reasons: PARTIAL_REASONS,
    ...overrides,
  };
}

function trafficPage(
  items = [trafficItem()],
  overrides: Record<string, unknown> = {},
) {
  const reasons = [...new Set(items.flatMap((item) => item.partial_reasons as string[]))].sort();
  return {
    scope: { run_id: "run-1", engagement_id: "engagement-1" },
    snapshot: {
      id: "d".repeat(64),
      created_through: "2026-08-01T00:00:00+00:00",
      stale: false,
    },
    items,
    truncated: false,
    has_more: false,
    next_cursor: null,
    partial: Boolean(reasons.length),
    partial_reasons: reasons,
    ...overrides,
  };
}

function trafficDetail(item = trafficItem(), overrides: Record<string, unknown> = {}) {
  return {
    scope: { run_id: "run-1", engagement_id: "engagement-1" },
    item,
    ...overrides,
  };
}

function legacyLostItem() {
  const partialReasons = [...PARTIAL_REASONS, ...[
    "approval_metadata_unavailable",
    "redirect_metadata_unavailable",
    "request_body_availability_unknown",
    "response_body_availability_unknown",
    "tls_metadata_unavailable",
    "url_metadata_unavailable",
  ]].sort();
  return trafficItem("exchange-legacy", {
    lineage: {
      run_id: "run-1",
      session_id: "session-legacy",
      tool_call_id: "tool-call-legacy",
      node_id: "runner-legacy",
      node_status: "lost",
    },
    url_summary: {
      availability: "unavailable",
      scheme: null,
      origin: null,
      path_shape: null,
      path_segment_count: null,
      redacted: true,
    },
    tls: {
      availability: "unavailable",
      verified: null,
      client_certificate_used: null,
    },
    response: {
      status_code: 502,
      status_class: "server_error",
      elapsed_ms: 901,
      content_type: null,
      content_length: null,
      truncated: true,
    },
    artifacts: {
      request: {
        opaque_ref: `traffic-artifact:v1:${"e".repeat(64)}`,
        presence: "recorded_missing",
        access: "metadata_only",
      },
      response: {
        opaque_ref: null,
        presence: "not_recorded",
        access: "metadata_only",
      },
    },
    body: {
      request: { availability: "unknown", revealable: false, truncated: false },
      response: { availability: "unknown", revealable: false, truncated: true },
    },
    redirect: {
      availability: "unavailable",
      count: null,
      followed: null,
      origins: [],
      partial: true,
    },
    created_by: { availability: "unavailable", kind: "unknown" },
    approval: { availability: "unavailable", reference_id: null, status: null },
    partial_reasons: partialReasons,
  });
}

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

function testQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  });
}

function renderWorkspace(
  props: Partial<React.ComponentProps<typeof RunTrafficWorkspace>> = {},
  queryClient = testQueryClient(),
  language = false,
) {
  const resolved = {
    runId: "run-1",
    expectedEngagementId: "engagement-1",
    view: "history" as const,
    exchangeId: "",
    onViewChange: vi.fn(),
    onExchangeChange: vi.fn(),
    ...props,
  };
  const workspace = (
    <QueryClientProvider client={queryClient}>
      <RunTrafficWorkspace {...resolved} />
    </QueryClientProvider>
  );
  return {
    ...render(language ? <LanguageProvider>{workspace}</LanguageProvider> : workspace),
    props: resolved,
    queryClient,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe("RunTrafficWorkspace", () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
    installLocalStorage();
  });

  it("accepts the backend-generated list and detail fixtures without schema adaptation", async () => {
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValue(structuredClone(trafficMetadataListFixture));
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockResolvedValue(structuredClone(trafficMetadataDetailFixture));

    renderWorkspace({
      runId: "run-traffic",
      expectedEngagementId: "engagement-traffic",
      view: "inspector",
      exchangeId: "exchange-traffic",
    });

    const inspector = await screen.findByRole("article", {
      name: "Selected Exchange metadata",
    });
    expect(within(inspector).getByText("https://target.example /…")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(
      "run-traffic",
      { cursor: undefined, limit: 50 },
      expect.any(AbortSignal),
    );
    expect(get).toHaveBeenCalledWith(
      "run-traffic",
      "exchange-traffic",
      expect.any(AbortSignal),
    );
  });

  it.each(["extra", "missing", "invariant"] as const)(
    "rejects a backend-generated fixture after an adversarial %s mutation",
    async (attack) => {
      const raw = structuredClone(trafficMetadataListFixture);
      const item = raw.items[0]!;
      if (attack === "extra") {
        (item as unknown as Record<string, unknown>).raw_headers = {
          Authorization: "Bearer FIXTURE_SECRET_CANARY",
        };
      } else if (attack === "missing") {
        delete (item.body.request as unknown as Record<string, unknown>).truncated;
      } else {
        item.request_id = "foreign-request";
      }
      vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(raw);
      const get = vi
        .spyOn(api, "getRunTargetHttpExchange")
        .mockResolvedValue(structuredClone(trafficMetadataDetailFixture));

      renderWorkspace({
        runId: "run-traffic",
        expectedEngagementId: "engagement-traffic",
      });

      expect(
        await screen.findByText("RiftX rejected an invalid Target HTTP metadata response"),
      ).toBeInTheDocument();
      expect(screen.queryByText(/FIXTURE_SECRET_CANARY/)).not.toBeInTheDocument();
      expect(get).not.toHaveBeenCalled();
    },
  );

  it("renders only production-shaped allowlisted metadata and has no sensitive action", async () => {
    const item = trafficItem();
    const list = vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));
    const get = vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(item));

    renderWorkspace({ view: "inspector", exchangeId: "exchange-1" });

    const inspector = await screen.findByRole("article", { name: "Selected Exchange metadata" });
    expect(within(inspector).getByText("https://target.example.test /…")).toBeInTheDocument();
    expect(within(inspector).getByText("https://redirect-one.example.test")).toBeInTheDocument();
    expect(within(inspector).getByText(`traffic-artifact:v1:${"b".repeat(64)}`)).toBeInTheDocument();
    expect(within(inspector).getAllByText("metadata only").length).toBeGreaterThan(0);
    expect(within(inspector).getByText("disabled")).toBeInTheDocument();
    expect(within(inspector).getByText("Client certificate used")).toBeInTheDocument();
    expect(screen.getByText(/Headers, cookies, authorization/)).toBeInTheDocument();
    expect(screen.queryByText("Authorization: Bearer BODY_SECRET_CANARY")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /reveal|replay|download/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /artifact|body|download/i })).not.toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(
      "run-1",
      { cursor: undefined, limit: 50 },
      expect.any(AbortSignal),
    );
    expect(get).toHaveBeenCalledWith("run-1", "exchange-1", expect.any(AbortSignal));
  });

  it("keeps a safe Unicode legacy execution key readable", async () => {
    const item = trafficItem("exchange-legacy-key", {
      execution_key: "执行键-旧",
    });
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(item));

    renderWorkspace({ view: "inspector", exchangeId: "exchange-legacy-key" });

    const inspector = await screen.findByRole("article", {
      name: "Selected Exchange metadata",
    });
    expect(within(inspector).getByText("执行键-旧")).toBeInTheDocument();
  });

  it.each([
    ["line break", "执行键\n旧"],
    ["Unicode format control", "执行键\u200B旧"],
  ])("rejects an execution key containing %s", async (_label, executionKey) => {
    const item = trafficItem("exchange-bad-key", { execution_key: executionKey });
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));

    renderWorkspace();

    expect(
      await screen.findByText("RiftX rejected an invalid Target HTTP metadata response"),
    ).toBeInTheDocument();
    expect(screen.queryByText(executionKey)).not.toBeInTheDocument();
  });

  it("keeps legacy URL/TLS/redirect metadata unavailable and shows LOST separately from HTTP status", async () => {
    const item = legacyLostItem();
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(item));

    renderWorkspace({ view: "inspector", exchangeId: "exchange-legacy" });

    const row = await screen.findByRole("button", {
      name: "Inspect Exchange exchange-legacy",
    });
    expect(row).toHaveTextContent(/502 · 901 ms · Runner LOST/);
    const inspector = await screen.findByRole("article", { name: "Selected Exchange metadata" });
    expect(within(inspector).getAllByText("Unavailable").length).toBeGreaterThan(3);
    expect(within(inspector).getByText("Response truncated").nextSibling).toHaveTextContent("Yes");
    expect(screen.queryByText("https://legacy.example.test/?signed=SECRET_CANARY")).not.toBeInTheDocument();
    expect(screen.queryByText("OLD_ARTIFACT_BODY_CANARY")).not.toBeInTheDocument();
  });

  it("loads stable pages without duplicate or omitted Exchange rows", async () => {
    const first = trafficItem("exchange-1");
    const second = trafficItem("exchange-2");
    const third = trafficItem("exchange-3");
    const list = vi.spyOn(api, "listRunTargetHttpExchanges").mockImplementation(
      (_runId, options) =>
        Promise.resolve(
          options?.cursor
            ? trafficPage([third])
            : trafficPage([first, second], { has_more: true, next_cursor: "cursor-2" }),
        ),
    );
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(first));

    renderWorkspace();
    const history = await screen.findByRole("list", { name: "Target HTTP Exchange History" });
    expect(within(history).getAllByRole("button")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Load more Exchanges" }));

    await waitFor(() => expect(within(history).getAllByRole("button")).toHaveLength(3));
    expect(list).toHaveBeenLastCalledWith(
      "run-1",
      { cursor: "cursor-2", limit: 50 },
      expect.any(AbortSignal),
    );
  });

  it("retains last verified metadata with an explicit stale warning after an ordinary refetch failure", async () => {
    const item = trafficItem();
    vi.spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValueOnce(trafficPage([item]))
      .mockRejectedValue(new RiftXAPIError(503, "traffic_unavailable", "Traffic unavailable"));
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(item));

    renderWorkspace();
    expect(await screen.findByText("https://target.example.test /…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh Traffic metadata" }));

    expect(
      await screen.findByText("Traffic refresh failed; showing last verified metadata"),
    ).toBeInTheDocument();
    expect(screen.getByText("https://target.example.test /…")).toBeInTheDocument();
  });

  it("shows a same-shape detail-not-found state without guessing another Exchange", async () => {
    const item = trafficItem();
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));
    const get = vi.spyOn(api, "getRunTargetHttpExchange").mockRejectedValue(
      new RiftXAPIError(404, "resource_not_accessible", "Resource is not accessible"),
    );

    renderWorkspace({ view: "inspector", exchangeId: "exchange-1" });

    expect(await screen.findByText("Exchange metadata not found")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("article", { name: "Selected Exchange metadata" })).not.toBeInTheDocument();
  });

  it("rejects invalid URL identities without issuing a detail request and accepts safe Unicode legacy identity", async () => {
    const unicodeItem = trafficItem("交换-一");
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([unicodeItem]));
    const get = vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(unicodeItem));

    const invalid = renderWorkspace({ view: "inspector", exchangeId: " bad\nidentity " });
    expect(await screen.findByText("Invalid Exchange identity")).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    invalid.unmount();

    renderWorkspace({ view: "inspector", exchangeId: "交换-一" });
    expect(await screen.findByRole("article", { name: "Selected Exchange metadata" })).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("run-1", "交换-一", expect.any(AbortSignal));
  });

  it.each([
    "raw_headers",
    "signed_query",
    "scheme_origin_mismatch",
    "path_segment_overflow",
    "body_reveal",
    "governance_upgrade",
    "raw_artifact_id",
    "custom_content_type",
    "identity_mismatch",
    "status_mismatch",
    "missing_body_truncated",
    "wrong_detail_envelope",
  ])("fails closed for adversarial %s response contract", async (attack) => {
    const item = trafficItem();
    let raw: unknown = trafficPage([item]);
    if (attack === "raw_headers") {
      raw = trafficPage([{
        ...item,
        response_headers: { Authorization: "Bearer SECRET_CANARY" },
      } as unknown as ReturnType<typeof trafficItem>]);
    } else if (attack === "signed_query") {
      raw = trafficPage([{
        ...item,
        url_summary: { ...item.url_summary, origin: "https://target.example.test?sig=SECRET_CANARY" },
      }]);
    } else if (attack === "scheme_origin_mismatch") {
      raw = trafficPage([{
        ...item,
        url_summary: { ...item.url_summary, scheme: "http" },
      }]);
    } else if (attack === "path_segment_overflow") {
      raw = trafficPage([{
        ...item,
        url_summary: { ...item.url_summary, path_segment_count: 4097 },
      }]);
    } else if (attack === "body_reveal") {
      raw = trafficPage([{
        ...item,
        body: { ...item.body, request: { ...item.body.request, revealable: true } },
      }]);
    } else if (attack === "governance_upgrade") {
      raw = trafficPage([{
        ...item,
        governance: { ...item.governance, reveal_capability: "enabled" },
      }]);
    } else if (attack === "raw_artifact_id") {
      raw = trafficPage([{
        ...item,
        artifacts: {
          ...item.artifacts,
          request: { ...item.artifacts.request, opaque_ref: "artifact-SECRET_CANARY" },
        },
      }]);
    } else if (attack === "custom_content_type") {
      raw = trafficPage([{
        ...item,
        response: {
          ...item.response,
          content_type: "application/x-custom-canary",
        },
      }]);
    } else if (attack === "identity_mismatch") {
      raw = trafficPage([{ ...item, request_id: "foreign-request" }]);
    } else if (attack === "status_mismatch") {
      raw = trafficPage([{
        ...item,
        response: { ...item.response, status_code: 500, status_class: "success" },
      }]);
    } else if (attack === "missing_body_truncated") {
      raw = trafficPage([{
        ...item,
        body: {
          ...item.body,
          request: {
            availability: "present",
            revealable: false,
          } as unknown as typeof item.body.request,
        },
      }]);
    }
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(
      attack === "wrong_detail_envelope" ? trafficPage([item]) : raw,
    );
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(
      attack === "wrong_detail_envelope" ? item : trafficDetail(item),
    );

    renderWorkspace(
      attack === "wrong_detail_envelope"
        ? { view: "inspector", exchangeId: "exchange-1" }
        : {},
    );

    expect(
      await screen.findByText("RiftX rejected an invalid Target HTTP metadata response"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/SECRET_CANARY/)).not.toBeInTheDocument();
    expect(screen.queryByText("application/x-custom-canary")).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: "Selected Exchange metadata" })).not.toBeInTheDocument();
  });

  it("zero-retries 403 and masks both active History and detail caches for the Run", async () => {
    const item = trafficItem(undefined, {
      url_summary: {
        ...trafficItem().url_summary,
        origin: "https://cached-secret.example.test",
      },
    });
    const queryClient = testQueryClient();
    const historyKey = ["run-target-http-exchanges", "run-1", "engagement-1", "history"];
    const detailKey = [
      "run-target-http-exchanges",
      "run-1",
      "engagement-1",
      "detail",
      "exchange-1",
    ];
    queryClient.setQueryData(historyKey, { pages: [trafficPage([item])], pageParams: [null] });
    queryClient.setQueryData(detailKey, trafficDetail(item));
    const forbidden = new RiftXAPIError(403, "traffic_forbidden", "Traffic forbidden");
    const list = vi.spyOn(api, "listRunTargetHttpExchanges").mockRejectedValue(forbidden);
    const get = vi.spyOn(api, "getRunTargetHttpExchange").mockRejectedValue(forbidden);

    renderWorkspace(
      { view: "inspector", exchangeId: "exchange-1" },
      queryClient,
    );

    expect(await screen.findByText("Traffic forbidden")).toBeInTheDocument();
    expect(screen.queryByText("https://cached-secret.example.test /…")).not.toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledTimes(1);
    expect(queryClient.getQueryData(detailKey)).toBeNull();
    const cachedHistory = queryClient.getQueryData<{ pages?: unknown[] }>(historyKey);
    expect(cachedHistory?.pages ?? []).toHaveLength(0);
  });

  it("fences every request started before an earlier list 403 from restoring authorization", async () => {
    const historyItem = trafficItem("exchange-1", {
      url_summary: {
        ...trafficItem().url_summary,
        origin: "https://history-after-revalidation.example.test",
      },
    });
    const preRevocationDetail = trafficItem("exchange-1", {
      url_summary: {
        ...trafficItem().url_summary,
        origin: "https://prefence-detail.example.test",
      },
    });
    const postRevocationDetail = trafficItem("exchange-1", {
      url_summary: {
        ...trafficItem().url_summary,
        origin: "https://postfence-detail.example.test",
      },
    });
    const forbidden = new RiftXAPIError(403, "traffic_forbidden", "Traffic forbidden");
    const firstList = deferred<unknown>();
    const preRevocation = deferred<unknown>();
    const postRevocation = deferred<unknown>();
    const list = vi
      .spyOn(api, "listRunTargetHttpExchanges")
      .mockImplementationOnce(() => firstList.promise)
      .mockResolvedValue(trafficPage([historyItem]));
    const get = vi
      .spyOn(api, "getRunTargetHttpExchange")
      .mockImplementationOnce(() => preRevocation.promise)
      .mockImplementation(() => postRevocation.promise);

    renderWorkspace({ view: "inspector", exchangeId: "exchange-1" });
    await waitFor(() => {
      expect(list).toHaveBeenCalledTimes(1);
      expect(get).toHaveBeenCalledTimes(1);
    });

    firstList.reject(forbidden);
    expect(await screen.findByText("Traffic forbidden")).toBeInTheDocument();

    preRevocation.resolve(trafficDetail(preRevocationDetail));
    await waitFor(() =>
      expect(
        screen.queryByText("https://prefence-detail.example.test /…"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Traffic forbidden")).toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Refresh Traffic metadata" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(
      screen.queryByText("https://prefence-detail.example.test /…"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("https://postfence-detail.example.test /…"),
    ).not.toBeInTheDocument();

    postRevocation.resolve(trafficDetail(postRevocationDetail));
    expect(
      await screen.findByText("https://postfence-detail.example.test /…"),
    ).toBeInTheDocument();
  });

  it("discards an ignored late detail success across revocation until detail itself revalidates", async () => {
    const historyItem = trafficItem("exchange-1", {
      url_summary: { ...trafficItem().url_summary, origin: "https://history.example.test" },
    });
    const oldItem = trafficItem("exchange-1", {
      url_summary: { ...trafficItem().url_summary, origin: "https://old-detail.example.test" },
    });
    const freshItem = trafficItem("exchange-1", {
      url_summary: { ...trafficItem().url_summary, origin: "https://fresh-detail.example.test" },
    });
    const forbidden = new RiftXAPIError(403, "traffic_forbidden", "Traffic forbidden");
    const list = vi.spyOn(api, "listRunTargetHttpExchanges")
      .mockResolvedValueOnce(trafficPage([historyItem]))
      .mockRejectedValueOnce(forbidden)
      .mockResolvedValue(trafficPage([historyItem]));
    const oldDetail = deferred<unknown>();
    const freshDetail = deferred<unknown>();
    const get = vi.spyOn(api, "getRunTargetHttpExchange")
      .mockImplementationOnce(() => oldDetail.promise)
      .mockImplementation(() => freshDetail.promise);

    renderWorkspace({ view: "inspector", exchangeId: "exchange-1" });
    expect(await screen.findByText("https://history.example.test /…")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Refresh Traffic metadata" }));
    expect(await screen.findByText("Traffic forbidden")).toBeInTheDocument();
    oldDetail.resolve(trafficDetail(oldItem));
    await waitFor(() => expect(screen.queryByText("https://old-detail.example.test /…")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Refresh Traffic metadata" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("https://old-detail.example.test /…")).not.toBeInTheDocument();
    expect(screen.queryByText("https://fresh-detail.example.test /…")).not.toBeInTheDocument();

    freshDetail.resolve(trafficDetail(freshItem));
    expect(await screen.findByText("https://fresh-detail.example.test /…")).toBeInTheDocument();
  });

  it("isolates visible selection and data immediately when the Run changes", async () => {
    const run1Item = trafficItem("exchange-run-1");
    const run2Item = trafficItem("exchange-run-2", {
      lineage: { ...trafficItem().lineage, run_id: "run-2" },
      url_summary: { ...trafficItem().url_summary, origin: "https://run-two.example.test" },
    });
    const run2Page = trafficPage([run2Item], {
      scope: { run_id: "run-2", engagement_id: "engagement-2" },
    });
    const run2 = deferred<unknown>();
    vi.spyOn(api, "listRunTargetHttpExchanges").mockImplementation((runId) =>
      runId === "run-1" ? Promise.resolve(trafficPage([run1Item])) : run2.promise,
    );
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(run1Item));
    const queryClient = testQueryClient();
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <RunTrafficWorkspace
          runId="run-1"
          expectedEngagementId="engagement-1"
          view="history"
          exchangeId=""
          onViewChange={vi.fn()}
          onExchangeChange={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("https://target.example.test /…")).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <RunTrafficWorkspace
          runId="run-2"
          expectedEngagementId="engagement-2"
          view="history"
          exchangeId=""
          onViewChange={vi.fn()}
          onExchangeChange={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.queryByText("https://target.example.test /…")).not.toBeInTheDocument();
    run2.resolve(run2Page);
    expect(await screen.findByText("https://run-two.example.test /…")).toBeInTheDocument();
  });

  it("supports roving keyboard rows and restores focus after closing Inspector", async () => {
    const first = trafficItem("exchange-1");
    const second = trafficItem("exchange-2");
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([first, second]));
    vi.spyOn(api, "getRunTargetHttpExchange").mockImplementation((_runId, exchangeId) =>
      Promise.resolve(trafficDetail(exchangeId === "exchange-2" ? second : first)),
    );
    const onExchangeChange = vi.fn();
    const initial = renderWorkspace({ onExchangeChange });
    const firstButton = await screen.findByRole("button", { name: "Inspect Exchange exchange-1" });
    const secondButton = screen.getByRole("button", { name: "Inspect Exchange exchange-2" });
    firstButton.focus();
    fireEvent.keyDown(firstButton, { key: "ArrowDown" });
    expect(secondButton).toHaveFocus();
    fireEvent.click(secondButton);
    expect(onExchangeChange).toHaveBeenCalledWith("exchange-2");
    initial.unmount();

    function Controlled() {
      const [selection, setSelection] = useState("exchange-2");
      const [view, setView] = useState<"history" | "inspector">("inspector");
      return (
        <RunTrafficWorkspace
          runId="run-1"
          expectedEngagementId="engagement-1"
          view={view}
          exchangeId={selection}
          onViewChange={setView}
          onExchangeChange={(next) => {
            setSelection(next);
            setView(next ? "inspector" : "history");
          }}
        />
      );
    }
    render(
      <QueryClientProvider client={testQueryClient()}>
        <Controlled />
      </QueryClientProvider>,
    );
    const close = await screen.findByRole("button", { name: "Close Exchange Inspector" });
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Inspect Exchange exchange-2" })).toHaveFocus(),
    );
  });

  it("localizes the Chinese History and Inspector main path", async () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");
    const item = trafficItem();
    vi.spyOn(api, "listRunTargetHttpExchanges").mockResolvedValue(trafficPage([item]));
    vi.spyOn(api, "getRunTargetHttpExchange").mockResolvedValue(trafficDetail(item));

    renderWorkspace({ view: "inspector", exchangeId: "exchange-1" }, undefined, true);

    expect(await screen.findByRole("heading", { name: "目标 HTTP 流量" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "历史" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "检查器" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("article", { name: "已选 Exchange 元数据" })).toBeInTheDocument();
    expect(screen.getByText(/绝不会加载 Header/)).toBeInTheDocument();
    expect(screen.getByText("显示正文能力")).toBeInTheDocument();
  });
});
