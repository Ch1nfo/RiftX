import {
  AlertCircle,
  Command,
  OctagonX,
  PanelRight,
  Pause,
  Play,
  Send,
  Settings,
  Square,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  activateEngagement,
  bridgeError,
  createEngagement,
  conversationHistory,
  daemonInfo,
  decideApproval,
  engagementReport,
  engagementStreamStatus,
  interruptEngagement,
  killRuntime,
  listApprovals,
  listEngagements,
  onEngagementEvent,
  onEngagementStream,
  onRuntimeError,
  onRuntimeStatus,
  pauseRuntime,
  resumeRuntime,
  startTurn,
} from "./bridge";
import riftxIcon from "./assets/riftx-icon.png";
import { ActivityTimeline } from "./components/ActivityTimeline";
import { EngagementInspector } from "./components/EngagementInspector";
import { NewEngagementDialog } from "./components/NewEngagementDialog";
import { SettingsDialog } from "./components/SettingsDialog";
import { TaskSidebar } from "./components/TaskSidebar";
import type {
  ApprovalDecision,
  ConversationEntry,
  CreateEngagementInput,
  DaemonControlStatus,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementEvent,
  EngagementReport,
  EngagementStreamStatus,
  PendingApproval,
} from "./models";

export default function App() {
  const [daemon, setDaemon] = useState<DesktopDaemonInfo | null>(null);
  const [engagements, setEngagements] = useState<Engagement[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState<EngagementReport | null>(null);
  const [events, setEvents] = useState<EngagementEvent[]>([]);
  const [history, setHistory] = useState<ConversationEntry[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [streamState, setStreamState] =
    useState<EngagementStreamStatus["state"]>("disconnected");
  const [turnRunning, setTurnRunning] = useState(false);
  const [decidingApprovalId, setDecidingApprovalId] = useState<string | null>(
    null,
  );
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
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

  const replaceEngagements = useCallback((taskList: Engagement[]) => {
    setEngagements(taskList);
    setSelectedId((current) => {
      if (current && taskList.some((task) => task.id === current)) {
        return current;
      }
      return taskList[0]?.id ?? null;
    });
  }, []);

  const loadReport = useCallback(
    async (engagementId: string) => {
      try {
        const nextReport = await engagementReport(engagementId);
        setReport(nextReport);
        updateEngagement(nextReport.engagement);
      } catch (cause) {
        setError(bridgeError(cause));
      }
    },
    [updateEngagement],
  );

  const loadApprovals = useCallback(async (engagementId: string) => {
    try {
      setApprovals(await listApprovals(engagementId));
    } catch (cause) {
      setError(bridgeError(cause));
    }
  }, []);

  const loadConversation = useCallback(
    async (engagementId: string, replace = false) => {
      try {
        const page = await conversationHistory(engagementId);
        setHistory((current) =>
          replace ? page.data : mergeConversationEntries(current, page.data),
        );
        setHistoryCursor((current) =>
          replace || current === null ? page.nextCursor : current,
        );
      } catch (cause) {
        setError(bridgeError(cause));
      }
    },
    [],
  );

  const refresh = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setLoading(true);
    }
    try {
      const daemonState = await daemonInfo();
      const taskList = await listEngagements();
      setDaemon(daemonState);
      replaceEngagements(taskList);
      setError(null);
    } catch (cause) {
      setDaemon(null);
      setError(bridgeError(cause));
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  }, [replaceEngagements]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let disposed = false;
    let stopStatus: () => void = () => undefined;
    let stopError: () => void = () => undefined;
    void Promise.all([
      onRuntimeStatus((runtime) => {
        if (disposed) {
          return;
        }
        setDaemon((current) => (current ? { ...current, runtime } : current));
        setControlBusy(false);
        void listEngagements()
          .then((taskList) => {
            if (!disposed) {
              replaceEngagements(taskList);
            }
          })
          .catch((cause) => {
            if (!disposed) {
              setError(bridgeError(cause));
            }
          });
      }),
      onRuntimeError((runtimeError) => {
        if (!disposed) {
          setControlBusy(false);
          setError(runtimeError);
        }
      }),
    ]).then(([statusListener, errorListener]) => {
      if (disposed) {
        statusListener();
        errorListener();
        return;
      }
      stopStatus = statusListener;
      stopError = errorListener;
    });
    return () => {
      disposed = true;
      stopStatus();
      stopError();
    };
  }, [replaceEngagements]);

  useEffect(() => {
    if (!selectedId) {
      setReport(null);
      setEvents([]);
      setHistory([]);
      setHistoryCursor(null);
      setApprovals([]);
      setTurnRunning(false);
      setStreamState("disconnected");
      return;
    }
    setReport(null);
    setEvents([]);
    setHistory([]);
    setHistoryCursor(null);
    setApprovals([]);
    setTurnRunning(false);
    setStreamState("connecting");
    void Promise.all([
      loadReport(selectedId),
      loadApprovals(selectedId),
      loadConversation(selectedId, true),
    ]);

    let disposed = false;
    let stopEvents: () => void = () => undefined;
    let stopStream: () => void = () => undefined;
    void Promise.all([
      onEngagementEvent((event) => {
        if (disposed || event.engagementId !== selectedId) {
          return;
        }
        setEvents((current) => [...current, event].slice(-300));
        if (event.kind === "turnStarted") {
          setTurnRunning(true);
        }
        if (
          event.kind === "turn/completed" ||
          event.kind === "engagementInterrupted" ||
          event.kind === "appServer/closed"
        ) {
          setTurnRunning(false);
        }
        if (
          event.kind.startsWith("approval/") ||
          event.kind === "approvalDecided" ||
          event.kind === "appServer/closed"
        ) {
          void loadApprovals(selectedId);
        }
        if (
          event.kind === "turn/completed" ||
          event.kind === "engagementInterrupted" ||
          event.kind === "appServer/closed" ||
          event.kind === "approvalDecided" ||
          event.kind === "item/completed" ||
          event.kind.startsWith("execution/") ||
          event.kind.startsWith("artifact/")
        ) {
          void Promise.all([loadReport(selectedId), refresh(false)]);
        }
        if (event.kind === "item/completed") {
          void loadConversation(selectedId);
        }
      }),
      onEngagementStream((status) => {
        if (!disposed && status.engagementId === selectedId) {
          setStreamState(status.state);
        }
      }),
    ])
      .then(([eventListener, streamListener]) => {
        if (disposed) {
          eventListener();
          streamListener();
          return;
        }
        stopEvents = eventListener;
        stopStream = streamListener;
        return engagementStreamStatus(selectedId).then((status) => {
          if (!disposed) {
            setStreamState(status.state);
          }
        });
      })
      .catch((cause) => {
        if (!disposed) {
          setStreamState("disconnected");
          setError(bridgeError(cause));
        }
      });

    return () => {
      disposed = true;
      stopEvents();
      stopStream();
    };
  }, [loadApprovals, loadConversation, loadReport, refresh, selectedId]);

  useEffect(() => {
    if (!selected || selected.status !== "active") {
      return;
    }
    const timer = window.setInterval(() => {
      void Promise.all([
        loadReport(selected.id),
        loadApprovals(selected.id),
        loadConversation(selected.id),
        refresh(false),
      ]);
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadApprovals, loadConversation, loadReport, refresh, selected]);

  const reportHasRunningTask =
    report?.tasks.some(
      (task) => task.status === "pending" || task.status === "running",
    ) ?? false;
  const isRunning = turnRunning || reportHasRunningTask;
  const runtimePaused = daemon?.runtime.state === "paused";
  const killSwitchActive = daemon?.runtime.reason === "killSwitch";

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
    if (
      !selected ||
      !instruction ||
      submitting ||
      isRunning ||
      runtimePaused
    ) {
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
      setTurnRunning(true);
      setInput("");
      setError(null);
      window.setTimeout(() => void loadReport(active.id), 300);
    } catch (cause) {
      setTurnRunning(false);
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
      setTurnRunning(false);
      await loadReport(selected.id);
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setSubmitting(false);
    }
  };

  const decide = async (approvalId: string, decision: ApprovalDecision) => {
    setDecidingApprovalId(approvalId);
    try {
      await decideApproval(approvalId, decision);
      setApprovals((current) =>
        current.filter((approval) => approval.id !== approvalId),
      );
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
      if (selectedId) {
        await loadApprovals(selectedId);
      }
    } finally {
      setDecidingApprovalId(null);
    }
  };

  const changeRuntime = async (
    command: () => Promise<DaemonControlStatus>,
  ) => {
    if (!daemon || controlBusy) {
      return;
    }
    setControlBusy(true);
    try {
      const runtime = await command();
      setDaemon((current) => (current ? { ...current, runtime } : current));
      replaceEngagements(await listEngagements());
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setControlBusy(false);
    }
  };

  const loadOlder = async () => {
    if (!selectedId || !historyCursor || loadingOlder) {
      return;
    }
    setLoadingOlder(true);
    try {
      const page = await conversationHistory(
        selectedId,
        Number(historyCursor),
      );
      setHistory((current) =>
        mergeConversationEntries(page.data, current),
      );
      setHistoryCursor(page.nextCursor);
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setLoadingOlder(false);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <img src={riftxIcon} alt="" />
          <strong>RiftX</strong>
        </div>
        <div className="topbar-actions">
          <div className="runtime-controls">
            <button
              type="button"
              className="icon-button"
              aria-label={runtimePaused ? "Resume runtime" : "Pause runtime"}
              title={runtimePaused ? "Resume runtime" : "Pause runtime"}
              disabled={!daemon || controlBusy}
              onClick={() =>
                void changeRuntime(runtimePaused ? resumeRuntime : pauseRuntime)
              }
            >
              {runtimePaused ? <Play size={15} /> : <Pause size={15} />}
            </button>
            <button
              type="button"
              className={`icon-button kill-switch ${killSwitchActive ? "active" : ""}`}
              aria-label="Activate Kill Switch"
              title="Activate Kill Switch"
              disabled={!daemon || controlBusy || killSwitchActive}
              onClick={() => void changeRuntime(killRuntime)}
            >
              <OctagonX size={15} />
            </button>
          </div>
          <div
            className={`daemon-state ${
              daemon
                ? killSwitchActive
                  ? "kill-switch-active"
                  : runtimePaused
                    ? "paused"
                    : "connected"
                : "disconnected"
            }`}
            title={
              daemon
                ? `${daemon.configPath} · daemon ${daemon.daemonVersion}`
                : undefined
            }
          >
            <span />
            {daemon
              ? killSwitchActive
                ? "Kill Switch"
                : runtimePaused
                  ? "Paused"
                  : "Running"
              : "Daemon offline"}
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Open settings"
            title="Settings"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings size={16} />
          </button>
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
                  <span className={`stream-state ${streamState}`}>
                    {streamState === "connected" ? "live" : streamState}
                  </span>
                  <span>{selected.status}</span>
                </div>
              </header>

              <ActivityTimeline
                engagement={selected}
                report={report}
                history={history}
                events={events}
                approvals={approvals}
                loading={loading}
                decidingApprovalId={decidingApprovalId}
                canLoadOlder={historyCursor !== null}
                loadingOlder={loadingOlder}
                onLoadOlder={() => void loadOlder()}
                onApproval={(approvalId, decision) =>
                  void decide(approvalId, decision)
                }
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
                  disabled={
                    selected.status === "completed" ||
                    submitting ||
                    isRunning ||
                    runtimePaused
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
                {isRunning ? (
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
                    disabled={
                      !input.trim() || submitting || isRunning || runtimePaused
                    }
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
        onError={setError}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onError={setError}
        onRuntimeChanged={(available) => {
          if (available) {
            void refresh(false);
          } else {
            setDaemon(null);
            setError(null);
          }
        }}
      />
    </div>
  );
}

function mergeConversationEntries(
  first: ConversationEntry[],
  second: ConversationEntry[],
): ConversationEntry[] {
  const entries = new Map<string, ConversationEntry>();
  [...first, ...second].forEach((entry) => entries.set(entry.id, entry));
  return [...entries.values()].sort(
    (left, right) => left.sequence - right.sequence,
  );
}
