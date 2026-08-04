import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewLocalAuditPage } from "./NewLocalAuditPage";

const mocks = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  useCreateLocalAudit: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useCreateLocalAudit: mocks.useCreateLocalAudit,
}));

afterEach(cleanup);

describe("NewLocalAuditPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.mutateAsync.mockResolvedValue({ audit_id: "audit-1", status: "queued" });
    mocks.useCreateLocalAudit.mockReturnValue({
      mutateAsync: mocks.mutateAsync,
      isPending: false,
      error: null,
    });
  });

  it("starts an audit for the trimmed local absolute path and opens its detail", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.type(
      screen.getByLabelText("Local folder path"),
      "  /Users/operator/source  ",
    );
    await user.click(screen.getByRole("button", { name: "Start local audit" }));

    expect(mocks.mutateAsync).toHaveBeenCalledWith({
      source_path: "/Users/operator/source",
    });
    expect(await screen.findByText("Audit destination")).toBeInTheDocument();
  });

  it("keeps submission disabled for an empty path and explains the local boundary", () => {
    renderPage();

    expect(screen.getByRole("button", { name: "Start local audit" })).toBeDisabled();
    expect(
      screen.getByText(/No builds, tests, package managers, Git helpers/),
    ).toBeInTheDocument();
    expect(screen.getByText(/same machine that runs RiftX/)).toBeInTheDocument();
  });

  it("renders mutation errors without navigating", () => {
    mocks.useCreateLocalAudit.mockReturnValue({
      mutateAsync: mocks.mutateAsync,
      isPending: false,
      error: new Error("Folder is outside the allowed source root"),
    });

    renderPage();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Folder is outside the allowed source root",
    );
    expect(screen.queryByText("Audit destination")).not.toBeInTheDocument();
  });
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={["/audits/new"]}>
        <Routes>
          <Route path="/audits/new" element={<NewLocalAuditPage />} />
          <Route path="/audits/:auditId" element={<div>Audit destination</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
