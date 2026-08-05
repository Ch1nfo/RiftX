import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { LocalOperatorGate } from "./components/LocalOperatorGate";
import { LoadingState } from "./components/LoadingState";

const DashboardPage = lazy(() =>
  import("./pages/DashboardPage").then((module) => ({ default: module.DashboardPage })),
);
const NewRunPage = lazy(() =>
  import("./pages/NewRunPage").then((module) => ({ default: module.NewRunPage })),
);
const RunDetailPage = lazy(() =>
  import("./pages/RunDetailPage").then((module) => ({ default: module.RunDetailPage })),
);
const NodesPage = lazy(() =>
  import("./pages/NodesPage").then((module) => ({ default: module.NodesPage })),
);
const ToolsPage = lazy(() =>
  import("./pages/ToolsPage").then((module) => ({ default: module.ToolsPage })),
);
const ModelsPage = lazy(() =>
  import("./pages/ModelsPage").then((module) => ({ default: module.ModelsPage })),
);
const NewLocalAuditPage = lazy(() =>
  import("./pages/NewLocalAuditPage").then((module) => ({
    default: module.NewLocalAuditPage,
  })),
);
const LocalAuditDetailPage = lazy(() =>
  import("./pages/LocalAuditDetailPage").then((module) => ({
    default: module.LocalAuditDetailPage,
  })),
);
const NotFoundPage = lazy(() =>
  import("./pages/NotFoundPage").then((module) => ({ default: module.NotFoundPage })),
);

export function App() {
  return (
    <LocalOperatorGate>
      <Suspense fallback={<LoadingState />}>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<DashboardPage />} />
            <Route path="runs/new" element={<NewRunPage />} />
            <Route path="runs/:runId" element={<RunDetailPage />} />
            <Route path="audits/new" element={<NewLocalAuditPage />} />
            <Route path="audits/:auditId" element={<LocalAuditDetailPage />} />
            <Route path="nodes" element={<NodesPage />} />
            <Route path="tools" element={<ToolsPage />} />
            <Route path="settings/models" element={<ModelsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Routes>
      </Suspense>
    </LocalOperatorGate>
  );
}
