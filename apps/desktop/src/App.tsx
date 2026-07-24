import {
  AlertCircle,
  Command,
  PanelRight,
  Send,
  Square,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  activateEngagement,
  bridgeError,
  createEngagement,
  daemonInfo,
  engagementReport,
  interruptEngagement,
  listEngagements,
  startTurn,
} from "./bridge";
import riftxIcon from "./assets/riftx-icon.png";
import { ActivityTimeline } from "./components/ActivityTimeline";
import { EngagementInspector } from "./components/EngagementInspector";
import { NewEngagementDialog } from "./components/NewEngagementDialog";
import { TaskSidebar } from "./components/TaskSidebar";
import type {
  CreateEngagementInput,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementReport,
} from "./models";

export default function App() {
  const [daemon, setDaemon] = useState<DesktopDaemonInfo | null>(null);
  const [engagements, setEngagements] = useState<Engagement[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState<EngagementReport | null>(null);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState<DesktopBridgeError | null>(null);

  const selected = useMemo(
    () =>
      engagements.find((engagement) => engagement.id === selectedId) ?? null,
    [engagements, selectedId],
  );

  const updateEngagement = useCallback((updated: Engagement) => {
    setEngagements((current) => {
      const remaining = current.filter(
        (engagement) => engagement.id !== updated.id,
      );
      return [updated, ...remaining];
    });
  }, []);

  const loadReport = useCallback(async (engagementId: string) => {
    try {
      setReport(await engagementReport(engagementId));
    } catch (cause) {
      setError(bridgeError(cause));
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [daemonState, taskList] = await Promise.all([
        daemonInfo(),
        listEngagements(),
      ]);
      setDaemon(daemonState);
      setEngagements(taskList);
      setSelectedId((current) => {
        if (current && taskList.some((task) => task.id === current)) {
          return current;
        }
        return taskList[0]?.id ?? null;
      });
      setError(null);
    } catch (cause) {
      setDaemon(null);
      setError(bridgeError(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!selectedId) {
      setReport(null);
      return;
    }
    void loadReport(selectedId);
  }, [loadReport, selectedId]);

  useEffect(() => {
    if (!selected || selected.status !== "active") {
      return;
    }
    const timer = window.setInterval(() => {
      void Promise.all([loadReport(selected.id), refresh()]);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [loadReport, refresh, selected]);

  const create = async (newEngagement: CreateEngagementInput) => {
    setSubmitting(true);
    try {
      const created = await createEngagement(newEngagement);
      updateEngagement(created);
      setSelectedId(created.id);
      setCreateOpen(false);
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const instruction = input.trim();
    if (!selected || !instruction || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      let active = selected;
      if (selected.status === "draft" || selected.status === "interrupted") {
        active = await activateEngagement(selected.id);
        updateEngagement(active);
      }
      await startTurn(active.id, instruction);
      setInput("");
      setError(null);
      window.setTimeout(() => void loadReport(active.id), 300);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const interrupt = async () => {
    if (!selected || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      updateEngagement(await interruptEngagement(selected.id));
      await loadReport(selected.id);
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <img src={riftxIcon} alt="" />
          <strong>RiftX</strong>
        </div>
        <div
          className={`daemon-state ${daemon ? "connected" : "disconnected"}`}
          title={daemon?.configPath}
        >
          <span />
          {daemon ? `Daemon ${daemon.daemonVersion}` : "Daemon offline"}
        </div>
      </header>

      <div className="workspace">
        <TaskSidebar
          engagements={engagements}
          selectedId={selectedId}
          loading={loading}
          onSelect={setSelectedId}
          onCreate={() => setCreateOpen(true)}
          onRefresh={() => void refresh()}
        />

        <main className="conversation">
          {selected ? (
            <>
              <header className="conversation-heading">
                <div>
                  <span>Conversation</span>
                  <h1>{selected.name}</h1>
                </div>
                <div className="conversation-status">
                  <span className={`mode-label ${selected.mode}`}>
                    {selected.mode}
                  </span>
                  <span>{selected.status}</span>
                </div>
              </header>

              <ActivityTimeline
                engagement={selected}
                report={report}
                loading={loading}
              />

              <form className="composer" onSubmit={submit}>
                <div className="composer-mode">
                  <Command size={15} />
                  <span>{selected.mode}</span>
                </div>
                <textarea
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder="Give the next authorized instruction..."
                  disabled={selected.status === "completed" || submitting}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
                {selected.status === "active" ? (
                  <button
                    className="stop-button"
                    type="button"
                    title="Interrupt execution"
                    aria-label="Interrupt execution"
                    onClick={() => void interrupt()}
                    disabled={submitting}
                  >
                    <Square size={16} fill="currentColor" />
                  </button>
                ) : (
                  <button
                    className="send-button"
                    type="submit"
                    title="Run instruction"
                    aria-label="Run instruction"
                    disabled={!input.trim() || submitting}
                  >
                    <Send size={17} />
                  </button>
                )}
              </form>
            </>
          ) : (
            <div className="conversation-empty">
              <PanelRight size={24} />
              <h1>Select or create a task</h1>
              <button
                type="button"
                className="primary-button"
                onClick={() => setCreateOpen(true)}
              >
                New task
              </button>
            </div>
          )}
        </main>

        <EngagementInspector engagement={selected} report={report} />
      </div>

      {error && (
        <div className="error-banner" role="alert">
          <AlertCircle size={17} />
          <div>
            <strong>{error.code.replace(/_/g, " ")}</strong>
            <span>{error.message}</span>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Dismiss error"
            title="Dismiss"
            onClick={() => setError(null)}
          >
            <X size={16} />
          </button>
        </div>
      )}

      <NewEngagementDialog
        open={createOpen}
        busy={submitting}
        onClose={() => setCreateOpen(false)}
        onCreate={create}
      />
    </div>
  );
}
