import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ReportDialog } from "./ReportDialog";

it("keeps a failed report recoverable without changing task state", () => {
  const onRetry = vi.fn();
  render(
    <ReportDialog
      open
      report={null}
      markdown={null}
      loading={false}
      failed
      onRetry={onRetry}
      onClose={vi.fn()}
    />,
  );

  expect(screen.getByRole("status")).toHaveTextContent(
    "Report generation failed. Task state was not changed.",
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry report" }));
  expect(onRetry).toHaveBeenCalledOnce();
});
