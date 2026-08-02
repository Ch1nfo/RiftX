import { translate, type Locale } from "../i18n";

export type DemoRunStatus =
  | "running"
  | "waiting_approval"
  | "waiting_user"
  | "paused"
  | "completed"
  | "cancelled";

export interface RunSummary {
  id: string;
  objective: string;
  engagement: string;
  target: string;
  status: DemoRunStatus;
  node: string;
  updated: string;
}

export interface DemoMessage {
  id: string;
  role: "operator" | "agent" | "system";
  author: string;
  at: string;
  text: string;
}

export interface ActionRecord {
  id: string;
  tool: string;
  command: string;
  target: string;
  risk: "read" | "approval" | "blocked";
  status: "completed" | "running" | "pending" | "cancelled";
  duration: string;
}

export interface TrafficExchange {
  id: string;
  at: string;
  source: "Browser" | "Burp" | "Agent";
  method: string;
  host: string;
  path: string;
  status: number;
  size: string;
  duration: string;
  artifact: string;
}

export interface GraphNodeRecord {
  id: string;
  label: string;
  kind: "objective" | "task" | "asset" | "evidence" | "finding";
  status: "confirmed" | "active" | "queued";
  x: number;
  y: number;
}

export interface TimelineRecord {
  id: string;
  at: string;
  type: string;
  title: string;
  detail: string;
  tone: "info" | "success" | "approval" | "danger";
}

interface ArtifactRecord {
  id: string;
  type: "stdout" | "http" | "screenshot" | "note";
  name: string;
  size: string;
  integrity: string;
}

interface FindingRecord {
  id: string;
  title: string;
  state: "draft" | "closed";
  confidence: string;
  evidence: string[];
}

interface ReportRecord {
  id: string;
  name: string;
  format: "Markdown" | "HTML" | "JSON";
  state: "ready" | "draft";
  sections: number;
}

interface NodeRecord {
  id: string;
  name: string;
  platform: string;
  status: "online" | "offline";
  containment: string;
  capabilities: string[];
  heartbeat: string;
}

interface ToolRecord {
  name: string;
  capability: string;
  source: string;
  approval: "read" | "balanced" | "manual";
  availability: "available" | "unavailable";
}

interface ModelProfileRecord {
  name: string;
  provider: string;
  model: string;
  mode: string;
  credential: "configured" | "not required";
  isDefault: boolean;
}

interface ConnectorRecord {
  id: "browser" | "chrome" | "burp";
  name: string;
  channel: string;
  status: "observing" | "connected";
  detail: string;
}

export interface DemoData {
  activeRun: RunSummary;
  runQueue: RunSummary[];
  initialMessages: DemoMessage[];
  actionRecords: ActionRecord[];
  trafficExchanges: TrafficExchange[];
  graphNodes: GraphNodeRecord[];
  timelineSeed: TimelineRecord[];
  terminalSeed: string[];
  artifacts: ArtifactRecord[];
  findings: FindingRecord[];
  reports: ReportRecord[];
  nodes: NodeRecord[];
  tools: ToolRecord[];
  modelProfiles: ModelProfileRecord[];
  connectorRecords: ConnectorRecord[];
}

function createDemoData(locale: Locale): DemoData {
  const t = (english: string, chinese: string) => translate(locale, english, chinese);
  const activeRun: RunSummary = {
    id: "run_demo_7f2a9c",
    objective: t(
      "Validate the external exposure of an authorized test environment and build a traceable evidence chain.",
      "验证授权测试环境的外部暴露面，并形成可追溯证据链。",
    ),
    engagement: "Q3 STAGING VALIDATION",
    target: "staging.example.test",
    status: "waiting_approval",
    node: "runner-linux-01",
    updated: t("just now", "刚刚"),
  };

  const runQueue: RunSummary[] = [
    activeRun,
    {
      id: "run_demo_19bd41",
      objective: t(
        "Review authentication boundaries and error exposure at the API gateway.",
        "核验 API 网关的认证边界与错误暴露。",
      ),
      engagement: "API BOUNDARY REVIEW",
      target: "api.example.test",
      status: "running",
      node: "runner-linux-01",
      updated: t("4 minutes ago", "4 分钟前"),
    },
    {
      id: "run_demo_84ce03",
      objective: t(
        "Organize browser-captured traffic and produce an evidence summary.",
        "整理浏览器捕获流量并生成证据摘要。",
      ),
      engagement: "BROWSER CAPTURE REVIEW",
      target: "portal.example.test",
      status: "waiting_user",
      node: "local",
      updated: t("21 minutes ago", "21 分钟前"),
    },
    {
      id: "run_demo_0ad638",
      objective: t(
        "Review confirmed findings and export the delivery report.",
        "复核已确认发现并导出交付报告。",
      ),
      engagement: "REPORT ASSEMBLY",
      target: "10.10.10.0/24",
      status: "completed",
      node: "runner-linux-01",
      updated: t("yesterday", "昨天"),
    },
  ];

  const initialMessages: DemoMessage[] = [
    {
      id: "msg-system",
      role: "system",
      author: "CONTROL PLANE",
      at: "14:31:02",
      text: t(
        "The operation context is durable. The current boundary includes staging.example.test and 10.10.10.0/24, with 10.10.10.1 and /production explicitly excluded.",
        "行动上下文已持久化。当前边界为 staging.example.test 与 10.10.10.0/24，明确排除 10.10.10.1 和 /production。",
      ),
    },
    {
      id: "msg-operator",
      role: "operator",
      author: "OPERATOR",
      at: "14:31:18",
      text: t(
        "Start with low-impact service identification. Request approval for every path that needs active validation.",
        "先进行低影响的服务识别。发现需要主动验证的路径时，逐项请求批准。",
      ),
    },
    {
      id: "msg-agent-1",
      role: "agent",
      author: "RIFTX AGENT",
      at: "14:31:22",
      text: t(
        "Authorization scope and exclusions confirmed. I will begin with passive resolution and low-impact discovery, binding every external effect to its own Action.",
        "已确认授权范围和排除项。我会从被动解析与低影响探测开始，并把每个外部效果绑定到独立 Action。",
      ),
    },
    {
      id: "msg-agent-2",
      role: "agent",
      author: "RIFTX AGENT",
      at: "14:32:07",
      text: t(
        "The service fingerprint is stored as an Artifact. Next I propose bounded HTTP validation of /health and /version, pending your approval.",
        "服务指纹已写入 Artifact。下一步拟对 /health 与 /version 执行有界 HTTP 验证，等待你的批准。",
      ),
    },
  ];

  const actionRecords: ActionRecord[] = [
    {
      id: "act-dns",
      tool: "dns_resolve",
      command: t("resolve staging.example.test", "解析 staging.example.test"),
      target: "staging.example.test",
      risk: "read",
      status: "completed",
      duration: "0.4s",
    },
    {
      id: "act-nmap",
      tool: "nmap",
      command: t("service detection on bounded ports", "在限定端口执行服务识别"),
      target: "10.10.10.24",
      risk: "read",
      status: "completed",
      duration: "8.7s",
    },
    {
      id: "act-http",
      tool: "target_http",
      command: "GET /health + /version",
      target: "staging.example.test",
      risk: "approval",
      status: "pending",
      duration: t("waiting", "等待中"),
    },
    {
      id: "act-nuclei",
      tool: "nuclei",
      command: t("safe exposure templates only", "仅限安全暴露类模板"),
      target: "staging.example.test",
      risk: "blocked",
      status: "cancelled",
      duration: t("not started", "未启动"),
    },
  ];

  const trafficExchanges: TrafficExchange[] = [
    { id: "http-01", at: "14:31:40", source: "Browser", method: "GET", host: "staging.example.test", path: "/", status: 200, size: "4.8 KB", duration: "124 ms", artifact: "artifact://runs/demo/http/01" },
    { id: "http-02", at: "14:31:43", source: "Browser", method: "GET", host: "staging.example.test", path: "/assets/app.js", status: 200, size: "82 KB", duration: "91 ms", artifact: "artifact://runs/demo/http/02" },
    { id: "http-03", at: "14:31:49", source: "Burp", method: "OPTIONS", host: "api.example.test", path: "/v1/session", status: 204, size: "0 B", duration: "67 ms", artifact: "artifact://runs/demo/http/03" },
    { id: "http-04", at: "14:31:54", source: "Agent", method: "GET", host: "api.example.test", path: "/v1/openapi.json", status: 403, size: "318 B", duration: "102 ms", artifact: "artifact://runs/demo/http/04" },
    { id: "http-05", at: "14:31:58", source: "Browser", method: "GET", host: "staging.example.test", path: "/favicon.ico", status: 404, size: "196 B", duration: "48 ms", artifact: "artifact://runs/demo/http/05" },
  ];

  const graphNodes: GraphNodeRecord[] = [
    { id: "g-objective", label: t("Authorized objective", "授权目标"), kind: "objective", status: "active", x: 8, y: 42 },
    { id: "g-task-1", label: t("Service identification", "服务识别"), kind: "task", status: "confirmed", x: 28, y: 21 },
    { id: "g-task-2", label: t("HTTP validation", "HTTP 验证"), kind: "task", status: "queued", x: 28, y: 62 },
    { id: "g-asset-1", label: "10.10.10.24", kind: "asset", status: "confirmed", x: 51, y: 12 },
    { id: "g-asset-2", label: "api.example.test", kind: "asset", status: "active", x: 51, y: 46 },
    { id: "g-evidence-1", label: t("Service fingerprint", "服务指纹"), kind: "evidence", status: "confirmed", x: 72, y: 18 },
    { id: "g-evidence-2", label: t("HTTP metadata", "HTTP 元数据"), kind: "evidence", status: "queued", x: 72, y: 60 },
    { id: "g-finding", label: t("Finding to review", "待确认发现"), kind: "finding", status: "queued", x: 89, y: 39 },
  ];

  const timelineSeed: TimelineRecord[] = [
    { id: "evt-07", at: "14:32:07", type: "approval.requested", title: t("Target HTTP awaits independent approval", "Target HTTP 等待独立批准"), detail: t("Action act-http binds the method, target, reason, and environment diff.", "Action act-http 绑定了方法、目标、原因和环境差异。"), tone: "approval" },
    { id: "evt-06", at: "14:31:58", type: "artifact.committed", title: t("Service identification committed to an immutable Artifact", "服务识别结果已写入不可变 Artifact"), detail: t("The model receives only a bounded summary and logical artifact:// reference.", "模型只收到有界摘要和逻辑 artifact:// 引用。"), tone: "success" },
    { id: "evt-05", at: "14:31:49", type: "connector.capture", title: t("Burp Connector imported one HTTP exchange", "Burp Connector 导入一条 HTTP 交换"), detail: t("The full exchange is stored as an Artifact; the workspace projects metadata only.", "完整交换作为 Artifact 保存，工作区只投影元数据。"), tone: "info" },
    { id: "evt-04", at: "14:31:32", type: "execution.completed", title: t("Node execution completed", "节点执行已完成"), detail: t("Execution exec-demo-01 returned completed in 8.7 seconds.", "Execution exec-demo-01 返回 completed，耗时 8.7 秒。"), tone: "success" },
    { id: "evt-03", at: "14:31:22", type: "runtime.cycle.started", title: t("First Agent Cycle started", "首个 Agent Cycle 已启动"), detail: t("The Run left waiting_user only after an explicit instruction.", "Run 在收到明确指令后才离开 waiting_user。"), tone: "info" },
    { id: "evt-02", at: "14:31:18", type: "message.persisted", title: t("Operator instruction persisted", "Operator 指令已持久化"), detail: t("The message, scope, and approval mode were recorded before the model call.", "消息、范围和审批模式在模型调用前完成记录。"), tone: "info" },
    { id: "evt-01", at: "14:31:02", type: "run.created", title: t("Run created in waiting_user", "Run 创建于 waiting_user"), detail: t("Creation did not call a model, prepare tools, or produce an external effect.", "创建动作没有调用模型、准备工具或产生外部效果。"), tone: "info" },
  ];

  const terminalSeed = [
    "$ riftx demo attach run_demo_7f2a9c",
    t("[demo] attached to synthetic PTY transcript", "[demo] 已连接到合成 PTY 记录"),
    t("[node] runner-linux-01 | containment: cgroup v2 confirmed", "[node] runner-linux-01 | containment: cgroup v2 已确认"),
    "$ artifact inspect artifact://runs/demo/executions/01/stdout",
    t("PORT    SERVICE    STATE", "端口    服务       状态"),
    "443/tcp https      open",
    "8443/tcp https-alt open",
    t("[output bounded] 2 rows shown | full content retained as Artifact", "[输出有界] 显示 2 行 | 完整内容保留为 Artifact"),
  ];

  const artifacts: ArtifactRecord[] = [
    { id: "art-01", type: "stdout", name: "service-fingerprint.txt", size: "2.4 KB", integrity: "verified" },
    { id: "art-02", type: "http", name: "browser-capture.har", size: "38 KB", integrity: "verified" },
    { id: "art-03", type: "screenshot", name: "login-boundary.webp", size: "112 KB", integrity: "verified" },
    { id: "art-04", type: "note", name: "operator-takeover.md", size: "1.1 KB", integrity: "verified" },
  ];

  const findings: FindingRecord[] = [
    { id: "find-01", title: t("Version endpoint exposes component identifiers", "版本端点暴露组件标识"), state: "draft", confidence: t("Evidence awaiting review", "证据待复核"), evidence: ["art-01", "http-04"] },
    { id: "find-02", title: t("Administration entry point is outside the authorized scope", "管理入口未进入授权范围"), state: "closed", confidence: t("Boundary confirmed", "边界已确认"), evidence: ["art-04"] },
  ];

  const reports: ReportRecord[] = [
    { id: "report-01", name: t("Operation summary", "行动摘要"), format: "Markdown", state: "ready", sections: 6 },
    { id: "report-02", name: t("Evidence index", "证据索引"), format: "HTML", state: "ready", sections: 4 },
    { id: "report-03", name: t("Machine-readable delivery", "机器可读交付"), format: "JSON", state: "draft", sections: 8 },
  ];

  const nodes: NodeRecord[] = [
    { id: "runner-linux-01", name: t("Isolated Linux Runner", "隔离 Linux Runner"), platform: "linux / amd64", status: "online", containment: "cgroup v2", capabilities: ["process", "pty", "browser", "target_http"], heartbeat: "8s" },
    { id: "local", name: t("Local Operator Node", "本地 Operator 节点"), platform: "macOS / arm64", status: "online", containment: t("development only", "仅限开发"), capabilities: ["read", "artifact", "connector"], heartbeat: "local" },
    { id: "runner-win-lab", name: t("Windows Lab Runner", "Windows 实验室 Runner"), platform: "windows / amd64", status: "offline", containment: t("unproven", "未证明"), capabilities: ["powershell", "conpty"], heartbeat: "2h" },
  ];

  const tools: ToolRecord[] = [
    { name: "dns_resolve", capability: "recon", source: "builtin", approval: "read", availability: "available" },
    { name: "nmap", capability: "network_scan", source: "registry", approval: "balanced", availability: "available" },
    { name: "nuclei", capability: "template_scan", source: "registry", approval: "balanced", availability: "available" },
    { name: "target_http", capability: "http_effect", source: "builtin", approval: "manual", availability: "available" },
    { name: "browser_observe", capability: "browser_read", source: "builtin", approval: "read", availability: "available" },
    { name: "browser_action", capability: "browser_write", source: "builtin", approval: "balanced", availability: "available" },
    { name: "artifact_read", capability: "evidence", source: "builtin", approval: "read", availability: "available" },
    { name: "msfconsole", capability: "framework", source: "registry", approval: "manual", availability: "unavailable" },
    { name: "custom_poc", capability: "custom", source: "registry", approval: "manual", availability: "unavailable" },
  ];

  const modelProfiles: ModelProfileRecord[] = [
    { name: "primary", provider: "openai_compatible", model: "redteam-reasoner", mode: "responses", credential: "configured", isDefault: true },
    { name: "fast-triage", provider: "openai", model: "analysis-mini", mode: "responses", credential: "configured", isDefault: false },
    { name: "local-lab", provider: "openai_compatible", model: "local-model", mode: "chat_completions", credential: "not required", isDefault: false },
  ];

  const connectorRecords: ConnectorRecord[] = [
    { id: "browser", name: "Managed Browser", channel: t("Runner-owned Chromium", "Runner 所有的 Chromium"), status: "observing", detail: t("Sanitized visible text, stable element references, network summaries, and Artifact IDs.", "脱敏可见文本、稳定元素引用、网络摘要与 Artifact ID。") },
    { id: "chrome", name: "Chrome DevTools Connector", channel: t("External capture client", "外部捕获客户端"), status: "connected", detail: t("Capture selected exchanges, append them to a Run, follow SSE state, and open the WebUI.", "捕获选定交换、追加到 Run、跟随 SSE 状态并打开 WebUI。") },
    { id: "burp", name: "Burp Suite Connector", channel: t("Montoya extension", "Montoya 扩展"), status: "connected", detail: t("Import request and response Artifacts without placing an Agent runtime inside Burp.", "导入请求和响应 Artifact，而不在 Burp 内放置 Agent runtime。") },
  ];

  return {
    activeRun,
    runQueue,
    initialMessages,
    actionRecords,
    trafficExchanges,
    graphNodes,
    timelineSeed,
    terminalSeed,
    artifacts,
    findings,
    reports,
    nodes,
    tools,
    modelProfiles,
    connectorRecords,
  };
}

const fixtures: Record<Locale, DemoData> = {
  en: createDemoData("en"),
  "zh-CN": createDemoData("zh-CN"),
};

export function getDemoData(locale: Locale) {
  return fixtures[locale];
}
