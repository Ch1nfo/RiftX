import {
  Activity,
  Boxes,
  ChevronRight,
  CirclePlus,
  Menu,
  Network,
  Radar,
  Server,
  ShieldCheck,
  Wrench,
  X,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useUIStore } from "../store/ui";

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
        aria-label="Close navigation"
        onClick={() => setSidebarOpen(false)}
      />
      <aside className={`sidebar ${sidebarOpen ? "is-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark">
            <Network size={22} />
          </div>
          <div>
            <strong>RiftX</strong>
            <span>V2 / local control</span>
          </div>
          <button
            className="icon-button sidebar-close"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="primary-nav" aria-label="Primary navigation">
          <span className="nav-label">Workspace</span>
          {navigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setSidebarOpen(false)}
              className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{label}</span>
              <ChevronRight className="nav-chevron" size={15} />
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-spacer" />
        <div className="system-card">
          <div className="system-card-head">
            <span className="live-indicator" />
            <span>Control plane online</span>
          </div>
          <div className="system-card-row">
            <Activity size={15} />
            <span>SSE timeline ready</span>
          </div>
          <div className="system-card-row">
            <Boxes size={15} />
            <span>Temporal durable runtime</span>
          </div>
          <div className="system-card-row">
            <ShieldCheck size={15} />
            <span>Local node boundary</span>
          </div>
        </div>
        <div className="sidebar-footer">RIFTX // HOST-NATIVE</div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <Menu size={20} />
          </button>
          <div>
            <span className="page-eyebrow">{current.eyebrow}</span>
            <h1>{current.title}</h1>
          </div>
          <div className="topbar-meta">
            <span className="live-indicator" />
            <span>Local node</span>
          </div>
        </header>
        <div className="page-content">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
