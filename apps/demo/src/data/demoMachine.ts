import type { DemoMessage, DemoRunStatus, TimelineRecord } from "./demo";
import { getDemoData } from "./demo";
import { translate, type Locale } from "../i18n";

export type PrimaryView = "overview" | "mission" | "operation" | "registry" | "connectors";
export type WorkspaceTab =
  | "conversation"
  | "actions"
  | "graph"
  | "traffic"
  | "terminal"
  | "evidence"
  | "timeline";
export type RegistryTab = "nodes" | "tools" | "models";

export interface DemoState {
  locale: Locale;
  view: PrimaryView;
  workspaceTab: WorkspaceTab;
  registryTab: RegistryTab;
  runStatus: DemoRunStatus;
  approvalStatus: "pending" | "approved" | "rejected";
  terminalOwner: "agent" | "operator";
  browserOwner: "agent" | "operator";
  selectedTrafficId: string;
  selectedGraphId: string;
  messages: DemoMessage[];
  timeline: TimelineRecord[];
  stopProof: boolean;
  announcement: string;
  tourStep: number;
}

export type DemoAction =
  | { type: "navigate"; view: PrimaryView }
  | { type: "workspace"; tab: WorkspaceTab }
  | { type: "registry"; tab: RegistryTab }
  | { type: "traffic"; id: string }
  | { type: "graph"; id: string }
  | { type: "approval"; decision: "approved" | "rejected" }
  | { type: "pause" }
  | { type: "stop" }
  | { type: "terminal-owner" }
  | { type: "browser-owner" }
  | { type: "message"; text: string }
  | { type: "launch"; objective: string }
  | { type: "tour"; step: number }
  | { type: "set-locale"; locale: Locale }
  | { type: "reset" };

const viewNames: Record<Locale, Record<PrimaryView, string>> = {
  en: {
    overview: "Operations Overview",
    mission: "New Operation",
    operation: "Operation Workspace",
    registry: "Runtime Registry",
    connectors: "Browsers and Connectors",
  },
  "zh-CN": {
    overview: "战情总览",
    mission: "新建行动",
    operation: "行动空间",
    registry: "运行资源",
    connectors: "浏览器与连接器",
  },
};

const workspaceNames: Record<Locale, Record<WorkspaceTab, string>> = {
  en: {
    conversation: "Conversation",
    actions: "Actions and Approvals",
    graph: "Relationship Graph",
    traffic: "Traffic Metadata",
    terminal: "Terminal",
    evidence: "Evidence and Reports",
    timeline: "Audit Events",
  },
  "zh-CN": {
    conversation: "会话",
    actions: "Action 与审批",
    graph: "关系图谱",
    traffic: "流量元数据",
    terminal: "终端",
    evidence: "证据与报告",
    timeline: "审计事件",
  },
};

const registryNames: Record<Locale, Record<RegistryTab, string>> = {
  en: { nodes: "Nodes", tools: "Tools", models: "Models" },
  "zh-CN": { nodes: "节点", tools: "工具", models: "模型" },
};

export function createInitialDemoState(locale: Locale = "en"): DemoState {
  const { initialMessages, timelineSeed, trafficExchanges } = getDemoData(locale);
  return {
    locale,
    view: "overview",
    workspaceTab: "conversation",
    registryTab: "nodes",
    runStatus: "waiting_approval",
    approvalStatus: "pending",
    terminalOwner: "agent",
    browserOwner: "agent",
    selectedTrafficId: trafficExchanges[0].id,
    selectedGraphId: "g-objective",
    messages: initialMessages.map((message) => ({ ...message })),
    timeline: timelineSeed.map((record) => ({ ...record })),
    stopProof: false,
    announcement: translate(locale, "The demo environment is ready.", "演示环境已就绪。"),
    tourStep: 0,
  };
}

function nowLabel(locale: Locale) {
  return new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date());
}

function withEvent(state: DemoState, record: Omit<TimelineRecord, "id" | "at">) {
  return [
    {
      ...record,
      id: `evt-local-${state.timeline.length + 1}`,
      at: nowLabel(state.locale),
    },
    ...state.timeline,
  ];
}

export function demoReducer(state: DemoState, action: DemoAction): DemoState {
  const locale = state.locale;
  const t = (english: string, chinese: string) => translate(locale, english, chinese);

  switch (action.type) {
    case "navigate":
      return {
        ...state,
        view: action.view,
        announcement: t(
          `${viewNames.en[action.view]} demo area opened.`,
          `已打开 ${viewNames["zh-CN"][action.view]}演示区。`,
        ),
      };
    case "workspace":
      return {
        ...state,
        workspaceTab: action.tab,
        announcement: t(
          `Switched to the ${workspaceNames.en[action.tab]} workspace.`,
          `已切换到${workspaceNames["zh-CN"][action.tab]}工作区。`,
        ),
      };
    case "registry":
      return {
        ...state,
        registryTab: action.tab,
        announcement: t(
          `Switched to the ${registryNames.en[action.tab]} registry.`,
          `已切换到${registryNames["zh-CN"][action.tab]}注册表。`,
        ),
      };
    case "traffic":
      return { ...state, selectedTrafficId: action.id, announcement: t("HTTP metadata inspector updated.", "已更新 HTTP 元数据检查器。") };
    case "graph":
      return { ...state, selectedGraphId: action.id, announcement: t("Graph node focused.", "已聚焦图谱节点。") };
    case "approval": {
      if (state.approvalStatus !== "pending") return state;
      const approved = action.decision === "approved";
      const message: DemoMessage = {
        id: `msg-decision-${state.messages.length + 1}`,
        role: approved ? "agent" : "system",
        author: approved ? "RIFTX AGENT" : "CONTROL PLANE",
        at: nowLabel(locale),
        text: approved
          ? t(
              "Approval was persisted against the Action ID. Bounded Target HTTP validation is now running; other pending actions are unaffected.",
              "批准已按 Action ID 持久化。Target HTTP 验证进入有界执行，其他待审批动作不受影响。",
            )
          : t(
              "The Action was rejected and remains traceable. The Run returned to waiting_user without producing a Target HTTP effect.",
              "该 Action 已拒绝并保持可追溯。Run 返回 waiting_user，未产生目标 HTTP 效果。",
            ),
      };
      return {
        ...state,
        approvalStatus: action.decision,
        runStatus: approved ? "running" : "waiting_user",
        messages: [...state.messages, message],
        timeline: withEvent(state, {
          type: approved ? "approval.approved" : "approval.rejected",
          title: approved
            ? t("Operator approved Target HTTP", "Operator 批准 Target HTTP")
            : t("Operator rejected Target HTTP", "Operator 拒绝 Target HTTP"),
          detail: approved
            ? t("Only act-http received execution permission for this Run.", "仅 act-http 获得本次 Run 的执行许可。")
            : t("The rejection reason and Action identity were persisted.", "拒绝原因与 Action 身份已持久化。"),
          tone: approved ? "success" : "danger",
        }),
        announcement: approved
          ? t("Approval granted. Synthetic execution started.", "审批已通过，合成执行开始。")
          : t("Approval rejected. No external effect was produced.", "审批已拒绝，没有产生外部效果。"),
      };
    }
    case "pause": {
      if (state.runStatus !== "running" && state.runStatus !== "paused") return state;
      const resume = state.runStatus === "paused";
      return {
        ...state,
        runStatus: resume ? "running" : "paused",
        timeline: withEvent(state, {
          type: resume ? "run.resumed" : "run.paused",
          title: resume ? t("Run resumed", "Run 已恢复") : t("Run paused", "Run 已暂停"),
          detail: resume
            ? t("New synthetic Actions may continue.", "新的合成 Action 可以继续推进。")
            : t("New effects are fenced; every known effect shows a confirmed state.", "新效果已被围栏，已知效果均显示确认状态。"),
          tone: resume ? "success" : "approval",
        }),
        announcement: resume
          ? t("Run resumed.", "Run 已恢复。")
          : t("Run paused. Every synthetic effect is confirmed.", "Run 已暂停，所有合成效果均已确认。"),
      };
    }
    case "stop":
      return {
        ...state,
        runStatus: "cancelled",
        stopProof: true,
        approvalStatus: state.approvalStatus === "pending" ? "rejected" : state.approvalStatus,
        timeline: withEvent(state, {
          type: "run.cancelled",
          title: t("Emergency stop completed with confirmation", "紧急停止已完成并获得确认"),
          detail: t(
            "Synthetic dispositions for Execution, Browser, and Target HTTP are all confirmed stopped.",
            "Execution、Browser 和 Target HTTP 的合成 disposition 均为 confirmed stopped。",
          ),
          tone: "danger",
        }),
        announcement: t(
          "Emergency stop completed. All three effect owners confirmed stop.",
          "紧急停止完成。三个效果所有者均已确认停止。",
        ),
      };
    case "terminal-owner":
      return {
        ...state,
        terminalOwner: state.terminalOwner === "agent" ? "operator" : "agent",
        announcement: state.terminalOwner === "agent"
          ? t("Terminal handed to the Operator.", "终端已交给 Operator。")
          : t("Terminal returned to the Agent.", "终端已归还 Agent。"),
      };
    case "browser-owner":
      return {
        ...state,
        browserOwner: state.browserOwner === "agent" ? "operator" : "agent",
        announcement: state.browserOwner === "agent"
          ? t("Browser taken over by the Operator.", "浏览器已由 Operator 接管。")
          : t("Browser returned to the Agent.", "浏览器已归还 Agent。"),
      };
    case "message": {
      const text = action.text.trim();
      if (!text) return state;
      const stamp = nowLabel(locale);
      return {
        ...state,
        runStatus: "running",
        messages: [
          ...state.messages,
          { id: `msg-op-${state.messages.length + 1}`, role: "operator", author: "OPERATOR", at: stamp, text },
          {
            id: `msg-agent-${state.messages.length + 2}`,
            role: "agent",
            author: "RIFTX AGENT",
            at: stamp,
            text: t(
              "Instruction persisted. This Demo updates local synthetic state only; it never connects to a model, Runner, or target system.",
              "指令已持久化。本 Demo 只更新本地合成状态，不会连接模型、Runner 或目标系统。",
            ),
          },
        ],
        timeline: withEvent(state, {
          type: "message.persisted",
          title: t("New Operator instruction persisted", "新的 Operator 指令已持久化"),
          detail: t(
            "The Demo uses a deterministic local response to illustrate the conversation-first flow.",
            "Demo 使用本地确定性响应演示 conversation-first 流程。",
          ),
          tone: "info",
        }),
        announcement: t("Demo instruction added to the conversation.", "演示指令已加入会话。"),
      };
    }
    case "launch":
      return {
        ...state,
        view: "operation",
        workspaceTab: "conversation",
        runStatus: "waiting_user",
        approvalStatus: "pending",
        stopProof: false,
        messages: [
          {
            id: "msg-new-run",
            role: "system",
            author: "CONTROL PLANE",
            at: nowLabel(locale),
            text: t(
              `New demo Run created: ${action.objective} It is waiting_user; no model or tool has been called.`,
              `新的演示 Run 已创建：${action.objective} 当前为 waiting_user，尚未调用模型或工具。`,
            ),
          },
        ],
        timeline: withEvent(state, {
          type: "run.created",
          title: t("New demo Run created", "新的演示 Run 已创建"),
          detail: t(
            "The objective, scope, exclusions, node, model, and approval mode were persisted before execution.",
            "目标、范围、排除项、节点、模型与审批模式已先于执行持久化。",
          ),
          tone: "info",
        }),
        announcement: t(
          "Demo Run created and awaiting its first explicit instruction.",
          "演示 Run 已创建，等待第一条具体指令。",
        ),
      };
    case "tour":
      return {
        ...state,
        tourStep: action.step,
        announcement: t(
          `Demo tour advanced to stop ${action.step + 1}.`,
          `演示导览已前进到第 ${action.step + 1} 站。`,
        ),
      };
    case "set-locale":
      return createInitialDemoState(action.locale);
    case "reset":
      return createInitialDemoState(locale);
    default:
      return state;
  }
}
