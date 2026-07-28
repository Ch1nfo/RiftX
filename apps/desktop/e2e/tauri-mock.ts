import type { Page } from "@playwright/test";

export type DesktopMockScenario = "firstRun" | "providerError" | "reconnect";

export async function installTauriMock(
  page: Page,
  scenario: DesktopMockScenario,
): Promise<void> {
  await page.addInitScript((initialScenario) => {
    type Callback = (payload: unknown) => void;
    type Listener = { eventId: number; handlerId: number };
    type RuntimeMock = {
      state: string;
      reason: string | null;
      updatedAt: number;
      audit: { state: string; message: string | null; updatedAt: number };
    };

    const callbacks = new Map<number, { callback: Callback; once: boolean }>();
    const listeners = new Map<string, Listener[]>();
    let nextCallbackId = 1;
    let nextEventId = 1;

    const runningRuntime: RuntimeMock = {
      state: "running",
      reason: null,
      updatedAt: 1,
      audit: { state: "healthy", message: null, updatedAt: 1 },
    };
    const killedRuntime: RuntimeMock = {
      ...runningRuntime,
      state: "paused",
      reason: "killSwitch",
      updatedAt: 2,
    };
    const engagement = {
      id: "engagement-a",
      name: "Authorized lab",
      status: "active",
      objective: {
        summary: "Assess the authorized lab",
        successCriteria: ["Confirm exposure"],
        structuredCriteria: [],
      },
      entryPoints: ["lab.example.test"],
      mode: "auto",
      llmProfile: "default",
      autoLimits: {
        maxTurns: 10,
        maxToolCalls: 20,
        maxWallClockSeconds: 3600,
        maxSingleCommandSeconds: 60,
        maxConsecutiveFailures: 3,
        noProgressWindow: 3,
        maxModelTokensOrCost: null,
      },
      authorization: {
        network: {
          cidrs: [],
          domains: ["lab.example.test"],
          ports: [443],
        },
        identities: [],
        capabilities: [],
        environment: "lab",
        window: { startsAt: null, expiresAt: 4_000_000_000 },
      },
      policyRevision: "policy-a",
      threadId: "thread-a",
      createdAt: 1,
      updatedAt: 1,
    };
    const report = {
      engagement,
      assets: [],
      services: [],
      observations: [],
      hypotheses: [],
      executions: [],
      findings: [],
      evidence: [],
      attackPaths: [],
      coverage: [],
      tasks: [],
      artifacts: [],
      approvals: [],
      toolSnapshot: { snapshotSha256: "tools", tools: [] },
      skillSnapshot: { snapshotSha256: "skills", skills: [] },
    };
    const state = {
      profileReady: initialScenario === "reconnect",
      keySaved: initialScenario === "reconnect",
      reconnected: false,
      runtime: runningRuntime,
    };

    const profileSettings = () => ({
      defaultProfile: "default",
      profiles: [
        {
          profileName: "default",
          protocol: "responses",
          model: "gpt-test",
          baseUrl: "https://api.example.test/v1",
          timeoutSeconds: 300,
          reasoningLevel: "high",
          contextBudget: 200000,
          credentialSource: "keyring",
          credentialName: "riftx/default",
          configured: state.keySaved,
          enabled: true,
        },
      ],
      daemonRestartRequired: false,
    });
    const profileList = () => ({
      defaultProfile: "default",
      profiles: [
        {
          name: "default",
          protocol: "responses",
          model: "gpt-test",
          baseUrl: "https://api.example.test/v1",
          isDefault: true,
          state: state.profileReady ? "in_use" : "unconfigured",
          stateDetail: state.profileReady
            ? "Profile is active."
            : "Save an API key and pass the connection test.",
          configured: state.keySaved,
          runtimeReady: state.profileReady,
        },
      ],
    });
    const autoRun = () => ({
      engagementId: engagement.id,
      config: {
        objective: engagement.objective,
        expiresAt: 4_000_000_000,
        limits: engagement.autoLimits,
      },
      state: state.reconnected ? "needsInput" : "ready",
      stopReason: null,
      currentSubgoal: state.reconnected
        ? "Confirm the recovered target scope"
        : "Initial subgoal",
      turnsStarted: 1,
      turnsCompleted: 0,
      toolCalls: 0,
      consecutiveFailures: 0,
      noProgressTurns: 0,
      lastGoalAssessment: null,
      lastProgressAssessment: null,
      startedAt: 1,
      updatedAt: state.reconnected ? 2 : 1,
    });

    const emit = (event: string, payload: unknown) => {
      for (const listener of listeners.get(event) ?? []) {
        const registered = callbacks.get(listener.handlerId);
        registered?.callback({ event, id: listener.eventId, payload });
        if (registered?.once) {
          callbacks.delete(listener.handlerId);
        }
      }
    };

    const browserWindow = window as typeof window & {
      __TAURI_INTERNALS__: {
        transformCallback: (callback: Callback, once?: boolean) => number;
        unregisterCallback: (id: number) => void;
        invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
      };
      __TAURI_EVENT_PLUGIN_INTERNALS__: {
        unregisterListener: (event: string, eventId: number) => void;
      };
      __RIFTX_E2E__: { reconnect: () => void };
    };

    browserWindow.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
      unregisterListener(event, eventId) {
        listeners.set(
          event,
          (listeners.get(event) ?? []).filter(
            (listener) => listener.eventId !== eventId,
          ),
        );
      },
    };
    browserWindow.__TAURI_INTERNALS__ = {
      transformCallback(callback, once = false) {
        const id = nextCallbackId++;
        callbacks.set(id, { callback, once });
        return id;
      },
      unregisterCallback(id) {
        callbacks.delete(id);
      },
      async invoke(command, args = {}) {
        if (command === "plugin:event|listen") {
          const event = String(args.event);
          const listener = {
            eventId: nextEventId++,
            handlerId: Number(args.handler),
          };
          listeners.set(event, [...(listeners.get(event) ?? []), listener]);
          return listener.eventId;
        }
        if (command === "plugin:event|unlisten") {
          browserWindow.__TAURI_EVENT_PLUGIN_INTERNALS__.unregisterListener(
            String(args.event),
            Number(args.eventId),
          );
          return null;
        }

        switch (command) {
          case "daemon_info":
            return {
              protocolVersion: 13,
              daemonVersion: "1.0.0",
              configPath: "/tmp/riftx.toml",
              runtime: state.runtime,
            };
          case "active_turns":
            return state.reconnected
              ? [{ engagementId: engagement.id, profileName: "default" }]
              : [];
          case "list_engagements":
            return initialScenario === "reconnect" ? [engagement] : [];
          case "llm_profiles":
            return profileList();
          case "llm_settings":
            return profileSettings();
          case "settings_reload_impact":
            return { activeTurns: [] };
          case "save_llm_api_key":
            state.keySaved = true;
            return profileSettings();
          case "test_llm_profile":
            if (initialScenario === "providerError") {
              throw {
                code: "app_server_error",
                message:
                  '{"error":{"authorization":"Bearer provider-secret"}}',
              };
            }
            state.profileReady = true;
            return {
              profileName: "default",
              protocol: "responses",
              model: "gpt-test",
              ok: true,
              capabilities: {
                config: { status: "passed", detail: "Configuration accepted." },
                streamText: { status: "passed", detail: "Text stream received." },
                functionTools: {
                  status: "passed",
                  detail: "Structured function call received.",
                },
              },
            };
          case "tool_inventory":
          case "tool_doctor":
            return {
              roots: ["/opt/riftx/tools"],
              pathEntries: [],
              tools: [],
              snapshotSha256: "tools",
              diagnostics: [],
            };
          case "get_tools_settings":
            return {
              directories: ["/opt/riftx/tools"],
              daemonRestartRequired: false,
            };
          case "notification_settings":
            return { permission: "prompt" };
          case "engagement_report":
            return report;
          case "list_approvals":
            return state.reconnected
              ? [
                  {
                    id: "approval-a",
                    engagementId: engagement.id,
                    policyRevision: "policy-a",
                    kind: "command",
                    requestedAt: 1,
                    command: "nmap -sV lab.example.test",
                    cwd: "/tmp/riftx",
                    reason: "Recovered approval after daemon restart",
                    executionIntent: null,
                  },
                ]
              : [];
          case "conversation_history":
            return {
              data: state.reconnected
                ? [
                    {
                      sequence: 1,
                      id: "message-a",
                      engagementId: engagement.id,
                      turnId: "turn-a",
                      role: "agent",
                      kind: "message",
                      text: "Recovered conversation after reconnect",
                      createdAt: 2,
                    },
                  ]
                : [],
              nextCursor: null,
            };
          case "list_assessment_credentials":
          case "list_credential_grants":
            return [];
          case "engagement_stream_status":
            return {
              engagementId: engagement.id,
              state: "connected",
              message: null,
            };
          case "auto_status":
            return autoRun();
          default:
            throw {
              code: "mock_command_missing",
              message: `No E2E mock exists for ${command}`,
            };
        }
      },
    };
    browserWindow.__RIFTX_E2E__ = {
      reconnect() {
        state.reconnected = true;
        state.runtime = killedRuntime;
        emit("riftx://runtime-status", killedRuntime);
      },
    };
  }, scenario);
}
