import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

let fetchMock: ReturnType<typeof vi.fn>;

function createMemoryStorage(): Storage {
  const values = new Map<string, string>();

  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key) {
      return values.get(key) ?? null;
    },
    key(index) {
      return [...values.keys()][index] ?? null;
    },
    removeItem(key) {
      values.delete(key);
    },
    setItem(key, value) {
      values.set(key, value);
    },
  };
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  vi.stubGlobal("localStorage", createMemoryStorage());
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
  vi.stubGlobal("scrollTo", vi.fn());
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  expect(fetchMock).not.toHaveBeenCalled();
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function primaryNavigation() {
  return within(screen.getByRole("complementary", { name: "Primary navigation" }));
}

describe("App", () => {
  it("renders the complete local-only overview without making a request", () => {
    render(<App />);

    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute(
      "href",
      "#demo-main",
    );
    expect(
      screen.getByRole("heading", { name: /Turn every red-team operation.*into a recoverable control protocol/ }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Current demo operation")).toBeInTheDocument();
    expect(screen.getByLabelText("Operation progress")).toBeInTheDocument();
    expect(screen.getByText("LOCAL")).toBeInTheDocument();
    expect(screen.getByText("SAFE")).toBeInTheDocument();
    expect(primaryNavigation().getByRole("button", { name: "Operations Overview" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("navigates through every primary product area", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(primaryNavigation().getByRole("button", { name: "New Operation" }));
    expect(
      screen.getByRole("heading", { name: "Lock the authorization boundary before creating a Run." }),
    ).toBeInTheDocument();

    await user.click(primaryNavigation().getByRole("button", { name: "Operation Workspace" }));
    expect(screen.getByRole("heading", { name: "Q3 STAGING VALIDATION" })).toBeInTheDocument();

    await user.click(primaryNavigation().getByRole("button", { name: "Runtime Registry" }));
    expect(
      screen.getByRole("heading", { name: "Confirm runtime resources before the Agent acts." }),
    ).toBeInTheDocument();

    await user.click(primaryNavigation().getByRole("button", { name: "Browsers and Connectors" }));
    expect(
      screen.getByRole("heading", { name: "Bring browsers and external capture into one evidence chain." }),
    ).toBeInTheDocument();

    await user.click(primaryNavigation().getByRole("button", { name: "RiftX" }));
    expect(
      screen.getByRole("heading", { name: /Turn every red-team operation.*into a recoverable control protocol/ }),
    ).toBeInTheDocument();
  });

  it("persists the theme choice locally without remote work", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(window.localStorage.getItem("riftx-demo-theme")).toBe("dark");

    await user.click(screen.getByRole("button", { name: "Switch to light theme" }));

    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(window.localStorage.getItem("riftx-demo-theme")).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
  });

  it("uses English by default and rebuilds the complete demo in Chinese", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(document.documentElement).toHaveAttribute("lang", "en");
    expect(window.localStorage.getItem("riftx-demo-locale")).toBe("en");
    expect(new URLSearchParams(window.location.search).get("lang")).toBe("en");

    await user.click(screen.getByRole("button", { name: "Switch to Chinese" }));

    expect(document.documentElement).toHaveAttribute("lang", "zh-CN");
    expect(window.localStorage.getItem("riftx-demo-locale")).toBe("zh-CN");
    expect(new URLSearchParams(window.location.search).get("lang")).toBe("zh-CN");
    expect(screen.getByRole("heading", { name: /把每一次红队行动.*变成可恢复的控制协议/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "切换到英文" })).toBeInTheDocument();
    expect(screen.queryByText(/Turn every red-team operation/)).not.toBeInTheDocument();
  });

  it("lets the lang query override and persist a stored locale", () => {
    window.localStorage.setItem("riftx-demo-locale", "en");
    window.history.replaceState({}, "", "/?lang=zh-CN");

    render(<App />);

    expect(document.documentElement).toHaveAttribute("lang", "zh-CN");
    expect(window.localStorage.getItem("riftx-demo-locale")).toBe("zh-CN");
    expect(screen.getByRole("heading", { name: /把每一次红队行动.*变成可恢复的控制协议/ })).toBeInTheDocument();
  });

  it("runs the synthetic conversation, approval, pause, stop, and reset flow", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(primaryNavigation().getByRole("button", { name: "Operation Workspace" }));

    const composer = screen.getByRole("textbox", { name: "Send an instruction to the demo Run" });
    await user.type(composer, "Validate only two read-only endpoints");
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.getByText("Validate only two read-only endpoints")).toBeInTheDocument();
    expect(
      screen.getByText(/This Demo updates local synthetic state only; it never connects to a model, Runner, or target system/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Approve This Action Only" }));
    expect(screen.getByText("Decision: Approved")).toBeInTheDocument();
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Pause Run" }));
    expect(screen.getByRole("button", { name: "Resume Run" })).toBeInTheDocument();
    expect(screen.getAllByText("Paused").length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Resume Run" }));
    expect(screen.getByRole("button", { name: "Pause Run" })).toBeInTheDocument();

    const stopButton = screen.getByRole("button", { name: "Emergency Stop" });
    await user.click(stopButton);
    expect(stopButton).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Confirm emergency stop")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Confirm Stop" }));
    expect(
      screen.getByText(/Stop complete. Every known effect has affirmative proof/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Emergency Stop" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Reset demo state" }));
    expect(
      screen.getByRole("heading", { name: /Turn every red-team operation.*into a recoverable control protocol/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Stop complete. Every known effect has affirmative proof/)).not.toBeInTheDocument();
    expect(screen.getAllByText("Awaiting approval").length).toBeGreaterThan(0);
  });
});
