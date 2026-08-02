import { useEffect, useMemo, useReducer, useState } from "react";

import { DemoStamp, StatusPill } from "./components/Ui";
import { PixelIcon, type PixelIconName } from "./components/PixelIcon";
import {
  createInitialDemoState,
  demoReducer,
  type PrimaryView,
} from "./data/demoMachine";
import {
  LocaleProvider,
  persistLocale,
  resolveInitialLocale,
  translate,
} from "./i18n";
import { ConnectorsView } from "./views/ConnectorsView";
import { MissionView } from "./views/MissionView";
import { OperationView } from "./views/OperationView";
import { OverviewView } from "./views/OverviewView";
import { RegistryView } from "./views/RegistryView";

const navDefinitions: Array<{
  id: PrimaryView;
  label: readonly [string, string];
  shortLabel: readonly [string, string];
  icon: PixelIconName;
}> = [
  { id: "overview", label: ["Operations Overview", "战情总览"], shortLabel: ["Overview", "总览"], icon: "graph" },
  { id: "mission", label: ["New Operation", "新建行动"], shortLabel: ["New", "新建"], icon: "target" },
  { id: "operation", label: ["Operation Workspace", "行动空间"], shortLabel: ["Operate", "行动"], icon: "run" },
  { id: "registry", label: ["Runtime Registry", "运行资源"], shortLabel: ["Registry", "资源"], icon: "node" },
  { id: "connectors", label: ["Browsers and Connectors", "浏览器与连接器"], shortLabel: ["Connect", "连接"], icon: "traffic" },
];

const viewDescriptions: Record<PrimaryView, readonly [string, string]> = {
  overview: ["Local demo control plane", "本地演示控制面"],
  mission: ["Authorized targets and boundaries", "授权目标与边界"],
  operation: ["Conversation-led operation theater", "会话主导行动剧场"],
  registry: ["Nodes, tools, and models", "节点、工具与模型"],
  connectors: ["Browser, Chrome, and Burp", "浏览器、Chrome 与 Burp"],
};

type Theme = "dark" | "light";

function initialTheme(): Theme {
  const saved = window.localStorage.getItem("riftx-demo-theme");
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

export function App() {
  const [state, dispatch] = useReducer(
    demoReducer,
    undefined,
    () => createInitialDemoState(resolveInitialLocale()),
  );
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const locale = state.locale;
  const t = (english: string, chinese: string) => translate(locale, english, chinese);
  const navItems = useMemo(
    () => navDefinitions.map((item) => ({
      ...item,
      label: t(item.label[0], item.label[1]),
      shortLabel: t(item.shortLabel[0], item.shortLabel[1]),
    })),
    [locale],
  );
  const currentNav = useMemo(
    () => navItems.find((item) => item.id === state.view) ?? navItems[0],
    [navItems, state.view],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem("riftx-demo-theme", theme);
    const themeMeta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
    themeMeta?.setAttribute("content", theme === "dark" ? "#060b1c" : "#dce9f7");
  }, [theme]);

  useEffect(() => {
    persistLocale(locale);
    document.title = t("RiftX Demo — Authorized Operations", "RiftX Demo — 授权行动");
    const description = document.querySelector<HTMLMetaElement>('meta[name="description"]');
    description?.setAttribute(
      "content",
      t(
        "A standalone, sanitized RiftX product demo for durable authorized security operations.",
        "独立、脱敏的 RiftX 产品演示，展示可持久化的授权安全行动。",
      ),
    );
  }, [locale]);

  function navigate(view: PrimaryView) {
    dispatch({ type: "navigate", view });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <LocaleProvider locale={locale}>
      <div className="demo-shell" key={locale}>
        <a className="skip-link" href="#demo-main">
          {t("Skip to main content", "跳到主要内容")}
        </a>

        <aside className="command-rail" aria-label={t("Primary navigation", "主要导航")}>
          <button className="brand-cartridge" type="button" onClick={() => navigate("overview")}>
            <span className="brand-mark" aria-hidden="true">RX</span>
            <span>RiftX</span>
          </button>
          <nav className="rail-nav">
            {navItems.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`rail-button${state.view === item.id ? " is-active" : ""}`}
                aria-current={state.view === item.id ? "page" : undefined}
                aria-label={item.label}
                onClick={() => navigate(item.id)}
              >
                <PixelIcon name={item.icon} />
                <span>{item.shortLabel}</span>
              </button>
            ))}
          </nav>
          <div className="rail-footer" aria-label={t("Demo status", "演示状态")}>
            <span>{t("LOCAL", "本地")}</span>
            <strong>{t("SAFE", "安全")}</strong>
          </div>
        </aside>

        <header className="topbar">
          <div className="topbar-location">
            <PixelIcon name={currentNav.icon} />
            <div>
              <strong>{currentNav.label}</strong>
              <span>{t(viewDescriptions[state.view][0], viewDescriptions[state.view][1])}</span>
            </div>
          </div>
          <div className="topbar-actions">
            <DemoStamp compact />
            <StatusPill status={state.runStatus} />
            <button
              className="icon-control language-control"
              type="button"
              onClick={() => dispatch({ type: "set-locale", locale: locale === "en" ? "zh-CN" : "en" })}
              aria-label={locale === "en" ? "Switch to Chinese" : "切换到英文"}
              title={locale === "en" ? "中文" : "English"}
            >
              <span aria-hidden="true">{locale === "en" ? "中文" : "EN"}</span>
            </button>
            <button
              className="icon-control"
              type="button"
              onClick={() => setTheme((value) => (value === "dark" ? "light" : "dark"))}
              aria-label={theme === "dark" ? t("Switch to light theme", "切换到浅色主题") : t("Switch to dark theme", "切换到深色主题")}
              title={theme === "dark" ? t("Light theme", "浅色主题") : t("Dark theme", "深色主题")}
            >
              <span className="theme-glyph" aria-hidden="true">{theme === "dark" ? "L" : "D"}</span>
            </button>
            <button
              className="icon-control"
              type="button"
              onClick={() => dispatch({ type: "reset" })}
              aria-label={t("Reset demo state", "重置演示状态")}
              title={t("Reset demo", "重置演示")}
            >
              <PixelIcon name="run" />
            </button>
          </div>
        </header>

        <main id="demo-main" className="demo-main" tabIndex={-1}>
          {state.view === "overview" ? <OverviewView state={state} dispatch={dispatch} navigate={navigate} /> : null}
          {state.view === "mission" ? <MissionView dispatch={dispatch} /> : null}
          {state.view === "operation" ? <OperationView state={state} dispatch={dispatch} /> : null}
          {state.view === "registry" ? <RegistryView state={state} dispatch={dispatch} /> : null}
          {state.view === "connectors" ? <ConnectorsView state={state} dispatch={dispatch} /> : null}
        </main>

        <nav className="mobile-nav" aria-label={t("Mobile primary navigation", "移动端主要导航")}>
          {navItems.map((item) => (
            <button
              key={item.id}
              type="button"
              className={state.view === item.id ? "is-active" : ""}
              aria-current={state.view === item.id ? "page" : undefined}
              aria-label={item.label}
              onClick={() => navigate(item.id)}
            >
              <PixelIcon name={item.icon} />
              <span>{item.shortLabel}</span>
            </button>
          ))}
        </nav>

        <div className="sr-announcement" aria-live="polite" aria-atomic="true">
          {state.announcement}
        </div>
      </div>
    </LocaleProvider>
  );
}
