import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { LanguageProvider } from "../i18n";
import { ThemeProvider, themeStorageKey } from "../theme";
import { Layout } from "./Layout";

function installLocalStorage() {
  const values = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => values.delete(key),
    setItem: (key, value) => values.set(key, String(value)),
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

describe("Layout theme control", () => {
  beforeEach(() => {
    installLocalStorage();
  });

  it("switches between dark and light modes from the top bar", () => {
    render(
      <ThemeProvider initialTheme="dark">
        <LanguageProvider>
          <MemoryRouter>
            <Routes>
              <Route element={<Layout />}>
                <Route index element={<div>Dashboard content</div>} />
              </Route>
            </Routes>
          </MemoryRouter>
        </LanguageProvider>
      </ThemeProvider>,
    );

    const switchToLight = screen.getByRole("button", { name: "Switch to light mode" });
    fireEvent.click(switchToLight);

    expect(document.documentElement.dataset.theme).toBe("light");
    expect(window.localStorage.getItem(themeStorageKey)).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to dark mode" })).toBeInTheDocument();
  });
});
