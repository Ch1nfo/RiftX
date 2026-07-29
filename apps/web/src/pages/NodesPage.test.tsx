import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NodesPage } from "./NodesPage";

const mocks = vi.hoisted(() => ({ useNodes: vi.fn() }));

vi.mock("../hooks/queries", () => ({ useNodes: mocks.useNodes }));

describe("NodesPage", () => {
  beforeEach(() => {
    mocks.useNodes.mockReturnValue({
      isLoading: false,
      error: null,
      data: {
        items: [
          {
            id: "windows-a",
            name: "Windows Runner A",
            platform: "windows",
            architecture: "amd64",
            runner_version: "2.0.0",
            status: "online",
            capabilities: ["powershell", "conpty"],
            labels: { zone: "internal" },
            last_seen_at: new Date().toISOString(),
            created_at: "2026-07-29T00:00:00Z",
            updated_at: "2026-07-29T00:00:00Z",
          },
          {
            id: "kali-a",
            name: "Kali Runner A",
            platform: "linux",
            architecture: "x86_64",
            runner_version: "2.0.0",
            status: "degraded",
            capabilities: ["port_scan"],
            labels: {},
            last_seen_at: new Date().toISOString(),
            created_at: "2026-07-29T00:00:00Z",
            updated_at: "2026-07-29T00:00:00Z",
          },
        ],
      },
    });
  });

  it("summarizes runner health and advertised capabilities", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NodesPage />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Windows Runner A")).toBeInTheDocument();
    expect(screen.getByText("Kali Runner A")).toBeInTheDocument();
    expect(screen.getByText("powershell")).toBeInTheDocument();
    expect(screen.getByText("conpty")).toBeInTheDocument();
    expect(screen.getByText("port scan")).toBeInTheDocument();
    expect(screen.getByText("online")).toBeInTheDocument();
    expect(screen.getByText("degraded")).toBeInTheDocument();
  });
});
