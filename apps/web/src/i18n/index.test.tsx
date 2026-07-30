import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  LanguageProvider,
  languageStorageKey,
  translate,
  useI18n,
} from "./index";

function Probe() {
  const { language, toggleLanguage, t } = useI18n();
  return (
    <div>
      <span data-testid="language">{language}</span>
      <span>{t("{count} events", { count: 3 })}</span>
      <button type="button" onClick={toggleLanguage}>
        {t("Switch language")}
      </button>
    </div>
  );
}

const originalNavigatorLanguage = navigator.language;

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

describe("LanguageProvider", () => {
  beforeEach(() => {
    installLocalStorage();
    Object.defineProperty(navigator, "language", {
      configurable: true,
      value: "en-US",
    });
  });

  afterEach(() => {
    cleanup();
    Object.defineProperty(navigator, "language", {
      configurable: true,
      value: originalNavigatorLanguage,
    });
    document.documentElement.lang = "";
  });

  it("defaults to English and interpolates values", () => {
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );

    expect(screen.getByTestId("language")).toHaveTextContent("en");
    expect(screen.getByText("3 events")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
  });

  it("detects a Chinese browser locale", () => {
    Object.defineProperty(navigator, "language", {
      configurable: true,
      value: "zh-CN",
    });

    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );

    expect(screen.getByTestId("language")).toHaveTextContent("zh-CN");
    expect(screen.getByText("3 个事件")).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("zh-CN");
  });

  it("toggles languages and persists the selection", () => {
    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Switch language" }));
    expect(screen.getByTestId("language")).toHaveTextContent("zh-CN");
    expect(window.localStorage.getItem(languageStorageKey)).toBe("zh-CN");
    expect(document.documentElement.lang).toBe("zh-CN");

    fireEvent.click(screen.getByRole("button", { name: "切换语言" }));
    expect(screen.getByTestId("language")).toHaveTextContent("en");
    expect(window.localStorage.getItem(languageStorageKey)).toBe("en");
  });

  it("restores a stored language before browser detection", () => {
    window.localStorage.setItem(languageStorageKey, "zh-CN");

    render(
      <LanguageProvider>
        <Probe />
      </LanguageProvider>,
    );

    expect(screen.getByTestId("language")).toHaveTextContent("zh-CN");
  });
});

describe("translate", () => {
  it("falls back to the English source string when no translation exists", () => {
    expect(translate("zh-CN", "Untranslated source")).toBe("Untranslated source");
  });
});
