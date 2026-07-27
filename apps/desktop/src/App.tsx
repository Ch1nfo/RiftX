import {
  AlertCircle,
  Command,
  FileText,
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
  autoStatus,
  bridgeError,
  changeEngagementMode,
  createEngagement,
  conversationHistory,
  daemonInfo,
  decideApproval,
  engagementReport,
  engagementReportMarkdown,
  engagementStreamStatus,
  interruptEngagement,
  killAuto,
  killRuntime,
  listApprovals,
  listAssessmentCredentials,
  listCredentialGrants,
  listEngagements,
  llmProfiles,
  onEngagementEvent,
  onEngagementStream,
  onRuntimeError,
  onRuntimeStatus,
  pauseAuto,
  pauseRuntime,
  resumeAuto,
  resumeRuntime,
  startTurn,
} from "./bridge";
import riftxIcon from "./assets/riftx-icon.png";
import { AUTO_STALE_ACTIVITY_MS } from "./constants";
import { ActivityTimeline } from "./components/ActivityTimeline";
import { CredentialDialog } from "./components/CredentialDialog";
import { EngagementInspector } from "./components/EngagementInspector";
import { NewEngagementDialog } from "./components/NewEngagementDialog";
import { ReportDialog } from "./components/ReportDialog";
import { SettingsDialog } from "./components/SettingsDialog";
import { TaskSidebar } from "./components/TaskSidebar";
import type {
  ApprovalDecision,
  AutoRun,
  ConversationEntry,
  CreateEngagementInput,
  CredentialGrant,
  CredentialReference,
  DaemonControlStatus,
  DesktopBridgeError,
  DesktopDaemonInfo,
  Engagement,
  EngagementEvent,
  EngagementReport,
  EngagementStreamStatus,
  ExecutionMode,
  PendingApproval,
} from "./models";

export default function App() {
  const [daemon, setDaemon] = useState<DesktopDaemonInfo | null>(null);
  const [profileReady, setProfileReady] = useState<boolean | null>(null);
  const [engagements, setEngagements] = useState<Engagement[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState<EngagementReport | null>(null);
  const [autoRun, setAutoRun] = useState<AutoRun | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);
  const [events, setEvents] = useState<EngagementEvent[]>([]);
  const [history, setHistory] = useState<ConversationEntry[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [credentialReferences, setCredentialReferences] = useState<
    CredentialReference[]
  >([]);
  const [credentialGrants, setCredentialGrants] = useState<CredentialGrant[]>(
    [],
  );
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
  const [modeBusy, setModeBusy] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [reportMarkdown, setReportMarkdown] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [error, setError] = useState<DesktopBridgeError | null>(null);
  const [lastActivityAt, setLastActivityAt] = useState<number | null>(null);
  const [staleAutoHint, setStaleAutoHint] = useState(false);

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

  const loadCredentials = useCallback(async (engagementId: string) => {
    try {
      const [references, grants] = await Promise.all([
        listAssessmentCredentials(engagementId),
        listCredentialGrants(engagementId),
      ]);
      setCredentialReferences(references);
      setCredentialGrants(grants);
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
      const [daemonState, taskList] = await Promise.all([
        daemonInfo(),
        listEngagements(),
      ]);
      setDaemon(daemonState);
      replaceEngagements(taskList);
      try {
        const profiles = await llmProfiles();
        setProfileReady(
          profiles.profiles.some(
            (profile) =>
              profile.state === "ready" || profile.state === "in_use",
          ),
        );
        setError(null);
      } catch (cause) {
        setProfileReady(null);
        setError(bridgeError(cause));
      }
    } catch (cause) {
      setDaemon(null);
      setProfileReady(null);
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
      setAutoRun(null);
      setReportOpen(false);
      setReportMarkdown(null);
      setEvents([]);
      setHistory([]);
      setHistoryCursor(null);
      setApprovals([]);
      setCredentialReferences([]);
      setCredentialGrants([]);
      setCredentialsOpen(false);
      setTurnRunning(false);
      setStreamState("disconnected");
      setLastActivityAt(null);
      setStaleAutoHint(false);
      return;
    }
    setReport(null);
    setAutoRun(null);
    setReportOpen(false);
    setReportMarkdown(null);
    setEvents([]);
    setHistory([]);
    setHistoryCursor(null);
    setApprovals([]);
    setCredentialReferences([]);
    setCredentialGrants([]);
    setCredentialsOpen(false);
    setTurnRunning(false);
    setStreamState("connecting");
    setLastActivityAt(null);
    setStaleAutoHint(false);
    void Promise.all([
      loadReport(selectedId),
      loadApprovals(selectedId),
      loadConversation(selectedId, true),
      loadCredentials(selectedId),
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
        setLastActivityAt(Date.now());
        setStaleAutoHint(false);
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
        if (event.kind.startsWith("credential/")) {
          void Promise.all([loadCredentials(selectedId), refresh(false)]);
        }
        if (
          event.kind === "turn/completed" ||
          event.kind === "engagementInterrupted" ||
          event.kind === "engagement/modeChanged" ||
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
        if (event.kind.startsWith("auto/")) {
          void autoStatus(selectedId).then(setAutoRun).catch((cause) => {
            setError(bridgeError(cause));
          });
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
  }, [
    loadApprovals,
    loadConversation,
    loadCredentials,
    loadReport,
    refresh,
    selectedId,
  ]);

  useEffect(() => {
    if (!selected || selected.mode !== "auto") {
      setAutoRun(null);
      return;
    }
    let disposed = false;
    void autoStatus(selected.id)
      .then((run) => {
        if (!disposed) {
          setAutoRun(run);
        }
      })
      .catch((cause) => {
        if (!disposed) {
          setError(bridgeError(cause));
        }
      });
    return () => {
      disposed = true;
    };
  }, [selected?.id, selected?.mode]);

  useEffect(() => {
    if (
      !selected ||
      selected.mode !== "auto" ||
      !turnRunning ||
      lastActivityAt === null
    ) {
      setStaleAutoHint(false);
      return;
    }
    const tick = () => {
      setStaleAutoHint(Date.now() - lastActivityAt >= AUTO_STALE_ACTIVITY_MS);
    };
    tick();
    const timer = window.setInterval(tick, 15_000);
    return () => window.clearInterval(timer);
  }, [lastActivityAt, selected, turnRunning]);

  useEffect(() => {
    if (!selected || selected.status !== "active") {
      return;
    }
    const timer = window.setInterval(() => {
      void Promise.all([
        loadReport(selected.id),
        loadApprovals(selected.id),
        loadConversation(selected.id),
        loadCredentials(selected.id),
        refresh(false),
      ]);
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [
    loadApprovals,
    loadConversation,
    loadCredentials,
    loadReport,
    refresh,
    selected,
  ]);

  const reportHasRunningTask =
    report?.tasks.some(
      (task) =>
        task.status === "pending" ||
        task.status === "running" ||
        task.status === "expiring",
    ) ?? false;
  const isRunning = turnRunning || reportHasRunningTask;
  const canCreateTask = daemon !== null && profileReady === true;
  const runtimePaused = daemon?.runtime.state === "paused";
  const killSwitchActive = daemon?.runtime.reason === "killSwitch";
  const auditDegraded = daemon?.runtime.audit.state === "degraded";
  const controlledExecutionBlocked = runtimePaused || auditDegraded;

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
      selected.status === "completed" ||
      selected.status === "expired" ||
      !instruction ||
      submitting ||
      isRunning ||
      controlledExecutionBlocked
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
      setLastActivityAt(Date.now());
      setStaleAutoHint(false);
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

  const changeAuto = async (action: "pause" | "resume" | "kill") => {
    if (!selected || autoBusy) {
      return;
    }
    setAutoBusy(true);
    try {
      const command =
        action === "pause" ? pauseAuto : action === "resume" ? resumeAuto : killAuto;
      setAutoRun(await command(selected.id));
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setAutoBusy(false);
    }
  };

  const changeMode = async (
    mode: ExecutionMode,
    confirmation: string | null,
  ) => {
    if (!selected || modeBusy || isRunning || approvals.length > 0) {
      return;
    }
    setModeBusy(true);
    try {
      const updated = await changeEngagementMode(
        selected.id,
        mode,
        confirmation,
      );
      updateEngagement(updated);
      setReport((current) =>
        current ? { ...current, engagement: updated } : current,
      );
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setModeBusy(false);
    }
  };

  const openReport = async () => {
    if (!selected || reportLoading) {
      return;
    }
    setReportOpen(true);
    setReportLoading(true);
    try {
      const [nextReport, markdown] = await Promise.all([
        engagementReport(selected.id),
        engagementReportMarkdown(selected.id),
      ]);
      setReport(nextReport);
      setReportMarkdown(markdown);
      updateEngagement(nextReport.engagement);
      setError(null);
    } catch (cause) {
      setError(bridgeError(cause));
    } finally {
      setReportLoading(false);
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
    <div
      className={`app-shell${daemon && profileReady === false ? " setup-required" : ""}`}
    >
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
                ? auditDegraded
                  ? "audit-degraded"
                  : killSwitchActive
                    ? "kill-switch-active"
                    : runtimePaused
                      ? "paused"
                      : "connected"
                : "disconnected"
            }`}
            title={
              daemon
                ? auditDegraded
                  ? `Security audit unavailable: ${daemon.runtime.audit.message ?? "unknown audit failure"}. Controlled execution is blocked. · ${daemon.configPath} · daemon ${daemon.daemonVersion}`
                  : `${daemon.configPath} · daemon ${daemon.daemonVersion}`
                : undefined
            }
          >
            <span />
            {daemon
              ? auditDegraded
                ? "Audit degraded"
                : killSwitchActive
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

      {daemon && profileReady === false && (
        <section className="setup-banner" aria-labelledby="setup-banner-title">
          <AlertCircle size={18} />
          <div>
            <strong id="setup-banner-title">Finish model setup</strong>
            <span>
              Use RiftX only on systems you are authorized to test. Tools run on
              this computer, and the declared scope is not OS-enforced. Test a
              model Profile before creating a task.
            </span>
          </div>
          <button
            type="button"
            className="secondary-button"
            onClick={() => setSettingsOpen(true)}
          >
            Open settings
          </button>
        </section>
      )}

      <div className="workspace">
        <TaskSidebar
          engagements={engagements}
          selectedId={selectedId}
          loading={loading}
          createDisabled={!canCreateTask}
          createDisabledReason="Test a ready model Profile in Settings before creating a task."
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
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="Open report"
                    title="Report"
                    onClick={() => void openReport()}
                  >
                    <FileText size={15} />
                  </button>
                  <span className={`mode-label ${selected.mode}`}>
                    {selected.mode}
                  </span>
                  <span className={`stream-state ${streamState}`}>
                    {streamState === "connected" ? "live" : streamState}
                  </span>
                  <span>{selected.status}</span>
                </div>
              </header>

              {staleAutoHint && (
                <div className="stale-auto-banner" role="status">
                  <AlertCircle size={16} />
                  <div>
                    <strong>Auto seems stalled</strong>
                    <span>
                      No new activity for 5 minutes. Use Pause or Kill Switch if
                      you want to stop the runtime.
                    </span>
                  </div>
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="Dismiss stale activity hint"
                    title="Dismiss"
                    onClick={() => setStaleAutoHint(false)}
                  >
                    <X size={15} />
                  </button>
                </div>
              )}

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
                    selected.status === "expired" ||
                    submitting ||
                    isRunning ||
                    controlledExecutionBlocked
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
                      selected.status === "completed" ||
                      selected.status === "expired" ||
                      !input.trim() ||
                      submitting ||
                      isRunning ||
                      controlledExecutionBlocked
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
                disabled={!canCreateTask}
                title={
                  canCreateTask
                    ? "Create a new task"
                    : "Test a ready model Profile in Settings first"
                }
              >
                New task
              </button>
            </div>
          )}
        </main>

        <EngagementInspector
          engagement={selected}
          report={report}
          modeBusy={modeBusy}
          modeBlocked={isRunning || approvals.length > 0}
          credentialGrants={credentialGrants}
          autoRun={autoRun}
          autoBusy={autoBusy}
          onAutoAction={(action) => void changeAuto(action)}
          onModeChange={(mode, confirmation) =>
            void changeMode(mode, confirmation)
          }
          onOpenCredentials={() => {
            setCredentialsOpen(true);
            if (selectedId) {
              void loadCredentials(selectedId);
            }
          }}
          onOpenReport={() => void openReport()}
        />
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
            setProfileReady(null);
            setError(null);
          }
        }}
      />
      <ReportDialog
        open={reportOpen}
        report={report}
        markdown={reportMarkdown}
        loading={reportLoading}
        onClose={() => setReportOpen(false)}
      />
      <CredentialDialog
        open={credentialsOpen}
        engagement={selected}
        references={credentialReferences}
        grants={credentialGrants}
        mutable={
          selected !== null &&
          selected.status !== "completed" &&
          selected.status !== "expired" &&
          !(selected.mode === "auto" && selected.status === "active") &&
          !isRunning &&
          approvals.length === 0
        }
        onChanged={() => {
          if (selectedId) {
            void Promise.all([
              loadCredentials(selectedId),
              loadReport(selectedId),
              refresh(false),
            ]);
          }
        }}
        onClose={() => setCredentialsOpen(false)}
        onError={setError}
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
