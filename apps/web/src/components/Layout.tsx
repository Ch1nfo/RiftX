import {
  Activity,
  Boxes,
  ChevronRight,
  CirclePlus,
  Languages,
  Menu,
  Moon,
  Network,
  Radar,
  Server,
  ShieldCheck,
  Sun,
  Wrench,
  X,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useI18n } from "../i18n";
import { useUIStore } from "../store/ui";
import { useTheme } from "../theme";

const navigation = [
  { to: "/", label: "Dashboard", icon: Radar, end: true },
  { to: "/runs/new", label: "New run", icon: CirclePlus },
  { to: "/nodes", label: "Nodes", icon: Server },
  { to: "/tools", label: "Tools", icon: Wrench },
];

const titles: Record<string, { eyebrow: string; title: string }> = {
  "/": { eyebrow: "Control plane", title: "Operations dashboard" },
  "/runs/new": { eyebrow: "Run configuration", title: "Launch a durable run" },
  "/nodes": { eyebrow: "Runner fleet", title: "Execution nodes" },
  "/tools": { eyebrow: "Execution environment", title: "Tool registry" },
};

export function Layout() {
  const { language, t, toggleLanguage } = useI18n();
  const { theme, toggleTheme } = useTheme();
  const location = useLocation();
  const sidebarOpen = useUIStore((state) => state.sidebarOpen);
  const setSidebarOpen = useUIStore((state) => state.setSidebarOpen);
  const current = location.pathname.startsWith("/runs/") && location.pathname !== "/runs/new"
    ? { eyebrow: "Active operation", title: "Run timeline" }
    : (titles[location.pathname] ?? titles["/"]);

  return (
    <div className="app-shell">
      <div className="ambient-grid" aria-hidden="true" />
      <button
        className={`sidebar-scrim ${sidebarOpen ? "is-visible" : ""}`}
        aria-label={t("Close navigation")}
        onClick={() => setSidebarOpen(false)}
      />
      <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark">
            <Network size={22} />
          </div>
          <div>
            <strong>RiftX</strong>
            <span>{t("V2 / local control")}</span>
          </div>
          <button
            className="icon-button sidebar-close"
            onClick={() => setSidebarOpen(false)}
            aria-label={t("Close navigation")}
          >
            <X size={18} />
          </button>
        </div>

        <nav className="primary-nav" aria-label={t("Primary navigation")}>
          <span className="nav-label">{t("Workspace")}</span>
          {navigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{t(label)}</span>
              <ChevronRight className="nav-chevron" size={15} />
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-spacer" />
        <div className="system-card">
          <div className="system-card-head">
            <span className="live-indicator" />
            <span>{t("Control plane online")}</span>
          </div>
          <div className="system-card-row">
            <Activity size={15} />
            <span>{t("SSE timeline ready")}</span>
          </div>
          <div className="system-card-row">
            <Boxes size={15} />
            <span>{t("Temporal durable runtime")}</span>
          </div>
          <div className="system-card-row">
            <ShieldCheck size={15} />
            <span>{t("Local node boundary")}</span>
          </div>
        </div>
        <div className="sidebar-footer">RIFTX // HOST-NATIVE</div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            onClick={() => setSidebarOpen(true)}
            aria-label={t("Open navigation")}
          >
            <Menu size={20} />
          </button>
          <div>
            <span className="page-eyebrow">{t(current.eyebrow)}</span>
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
