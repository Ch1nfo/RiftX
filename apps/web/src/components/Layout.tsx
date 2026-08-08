import { Languages, Moon, Sun } from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useI18n } from "../i18n";
import { useTheme } from "../theme";
import { PixelIcon, type PixelIconName } from "./PixelIcon";

const navigation: Array<{
  to: string;
  label: string;
  icon: PixelIconName;
  end?: boolean;
}> = [
  { to: "/", label: "Dashboard", icon: "graph", end: true },
  { to: "/nodes", label: "Nodes", icon: "node" },
  { to: "/tools", label: "Tools", icon: "terminal" },
  { to: "/settings/models", label: "Models", icon: "shield" },
];

const titles: Record<string, { sector: string; title: string }> = {
  "/": { sector: "Control plane", title: "Operations dashboard" },
  "/runs/new": { sector: "Run configuration", title: "Launch a durable run" },
  "/nodes": { sector: "Runner fleet", title: "Execution nodes" },
  "/tools": { sector: "Execution environment", title: "Tool registry" },
  "/settings/models": { sector: "Agent configuration", title: "Model profiles" },
};

export function Layout() {
  const { language, t, toggleLanguage } = useI18n();
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();
  const current = location.pathname.startsWith("/runs/") && location.pathname !== "/runs/new"
    ? { sector: "Active operation", title: "Run conversation" }
    : (titles[location.pathname] ?? titles["/"]);

  return (
    <div className="app-shell">
      <div className="ambient-grid" aria-hidden="true" />
      <aside className="sidebar">
        <div className="brand-row" aria-label="RiftX">
          <div className="brand-mark" aria-hidden="true">
            <span>R</span>
            <span>X</span>
          </div>
          <strong className="brand-name">RIFTX</strong>
        </div>

        <nav className="primary-nav" aria-label={t("Primary navigation")}>
          <span className="nav-label">{t("Workspace")}</span>
          {navigation.map(({ to, label, icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => isActive ? "nav-link active" : "nav-link"}
            >
              <PixelIcon name={icon} />
              <span>{t(label)}</span>
              <PixelIcon name="chevron" className="nav-chevron" />
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-spacer" />
        <div className="system-card" aria-label={t("Control plane online")}>
          <div className="system-card-head">
            <span className="live-indicator" />
            <span>{t("Control plane online")}</span>
          </div>
          <div className="system-card-row">
            <PixelIcon name="node" />
            <span>{t("Local node boundary")}</span>
          </div>
        </div>
        <div className="sidebar-footer">LOCAL // ON</div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <div className="topbar-heading">
            <div className="page-path" aria-label={t("Current workspace")}>
              <span>RIFTX</span>
              <span aria-hidden="true">/</span>
              <span>{t(current.sector)}</span>
            </div>
            <h1>{t(current.title)}</h1>
          </div>
          <div className="topbar-actions">
            <button
              type="button"
              className="language-switch theme-switch"
              aria-label={t(theme === "dark" ? "Switch to light mode" : "Switch to dark mode")}
              title={t(theme === "dark" ? "Switch to light mode" : "Switch to dark mode")}
              onClick={toggleTheme}
            >
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <button
              type="button"
              className="language-switch"
              aria-label={t("Switch language")}
              title={t("Switch language")}
              onClick={toggleLanguage}
            >
              <Languages size={16} />
              <span>{language === "en" ? "中文" : "EN"}</span>
            </button>
            <div className="topbar-meta">
              <span className="live-indicator" />
              <span>{t("Local node")}</span>
            </div>
          </div>
        </header>
        <div className="page-content">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
