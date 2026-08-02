import {
  useState,
  type CSSProperties,
  type Dispatch,
  type FormEvent,
} from "react";

import { PixelIcon, type PixelIconName } from "../components/PixelIcon";
import { DemoStamp, PanelHeading, StatusPill } from "../components/Ui";
import { getDemoData, type GraphNodeRecord } from "../data/demo";
import type {
  DemoAction,
  DemoState,
  WorkspaceTab,
} from "../data/demoMachine";
import { useLocale } from "../i18n";

const workspaceTabs: Array<{
  id: WorkspaceTab;
  label: readonly [english: string, chinese: string];
  icon: PixelIconName;
}> = [
  { id: "conversation", label: ["Conversation", "会话"], icon: "message" },
  { id: "actions", label: ["Actions and Approvals", "Action 与审批"], icon: "shield" },
  { id: "graph", label: ["Relationship Graph", "关系图谱"], icon: "graph" },
  { id: "traffic", label: ["Traffic Metadata", "流量元数据"], icon: "traffic" },
  { id: "terminal", label: ["Terminal", "终端"], icon: "terminal" },
  { id: "evidence", label: ["Evidence and Reports", "证据与报告"], icon: "evidence" },
  { id: "timeline", label: ["Audit Events", "审计事件"], icon: "file" },
];

export function OperationView({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const [confirmStop, setConfirmStop] = useState(false);
  const { locale, t, label } = useLocale();
  const { activeRun } = getDemoData(locale);

  return (
    <div className="screen-stack operation-screen">
      <header className="operation-heading">
        <div className="operation-identity">
          <DemoStamp />
          <div className="operation-title-line">
            <h1>{activeRun.engagement}</h1>
            <StatusPill status={state.runStatus} />
          </div>
          <p>{activeRun.objective}</p>
          <div className="operation-facts">
            <span><strong>{t("RUN", "RUN")}</strong> {activeRun.id}</span>
            <span><strong>{t("NODE", "节点")}</strong> {activeRun.node}</span>
            <span><strong>{t("MODEL", "模型")}</strong> primary</span>
            <span><strong>{t("MODE", "模式")}</strong> {label("balanced")}</span>
          </div>
        </div>
        <div className="run-controls">
          <button
            className="secondary-button"
            type="button"
            onClick={() => dispatch({ type: "pause" })}
            disabled={state.runStatus !== "running" && state.runStatus !== "paused"}
          >
            <PixelIcon name={state.runStatus === "paused" ? "run" : "pause"} />
            {state.runStatus === "paused" ? t("Resume Run", "恢复 Run") : t("Pause Run", "暂停 Run")}
          </button>
          <button
            className="danger-button"
            type="button"
            onClick={() => setConfirmStop((value) => !value)}
            disabled={state.runStatus === "cancelled"}
            aria-expanded={confirmStop}
          >
            <PixelIcon name="stop" />
            {t("Emergency Stop", "紧急停止")}
          </button>
        </div>
      </header>

      {confirmStop && state.runStatus !== "cancelled" ? (
        <section className="stop-confirmation" aria-label={t("Confirm emergency stop", "确认紧急停止")}>
          <PixelIcon name="warning" />
          <div>
            <strong>{t(
              "Fence first, then request a stop from every effect owner.",
              "先围栏，再向每个效果所有者请求停止。",
            )}</strong>
            <p>{t(
              "The Demo shows independent confirmations from Execution, Browser, and Target HTTP without running real stop commands.",
              "Demo 将显示 Execution、Browser 与 Target HTTP 的独立确认，不会执行真实停止命令。",
            )}</p>
          </div>
          <div className="stop-confirmation-actions">
            <button className="danger-button" type="button" onClick={() => {
              dispatch({ type: "stop" });
              setConfirmStop(false);
            }}>
              {t("Confirm Stop", "确认停止")}
            </button>
            <button className="secondary-button" type="button" onClick={() => setConfirmStop(false)}>
              {t("Go Back", "返回")}
            </button>
          </div>
        </section>
      ) : null}

      {state.stopProof ? <StopProofPanel /> : null}

      <nav className="workspace-tabs" aria-label={t("Run workspace", "Run 工作区")}>
        {workspaceTabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={state.workspaceTab === tab.id ? "is-active" : ""}
            aria-label={t(tab.label[0], tab.label[1])}
            aria-current={state.workspaceTab === tab.id ? "page" : undefined}
            onClick={() => dispatch({ type: "workspace", tab: tab.id })}
          >
            <PixelIcon name={tab.icon} />
            <span>{t(tab.label[0], tab.label[1])}</span>
            {tab.id === "actions" && state.approvalStatus === "pending" ? (
              <b aria-label={t("1 pending item", "1 个待处理项")}>1</b>
            ) : null}
          </button>
        ))}
      </nav>

      <section className="workspace-surface pixel-panel">
        {state.workspaceTab === "conversation" ? (
          <ConversationWorkspace state={state} dispatch={dispatch} />
        ) : null}
        {state.workspaceTab === "actions" ? (
          <ActionsWorkspace state={state} dispatch={dispatch} />
        ) : null}
        {state.workspaceTab === "graph" ? (
          <GraphWorkspace state={state} dispatch={dispatch} />
        ) : null}
        {state.workspaceTab === "traffic" ? (
          <TrafficWorkspace state={state} dispatch={dispatch} />
        ) : null}
        {state.workspaceTab === "terminal" ? (
          <TerminalWorkspace state={state} dispatch={dispatch} />
        ) : null}
        {state.workspaceTab === "evidence" ? <EvidenceWorkspace /> : null}
        {state.workspaceTab === "timeline" ? <TimelineWorkspace state={state} /> : null}
      </section>
    </div>
  );
}

function StopProofPanel() {
  const { locale, t, label } = useLocale();

  return (
    <section
      className="stop-proof-panel"
      lang={locale}
      aria-label={t(`Stop proof: Run ${label("cancelled")}`, `停止证明：Run ${label("cancelled")}`)}
    >
      <header>
        <PixelIcon name="check" />
        <div>
          <strong>{t(
            "Stop complete. Every known effect has affirmative proof.",
            "停止完成，所有已知效果均获得肯定性证明。",
          )}</strong>
          <p>{t(
            "The Run is not shown as cancelled until every confirmation is collected.",
            "Run 在收齐确认之前不会被显示为已取消。",
          )}</p>
        </div>
      </header>
      <div className="stop-proof-grid">
        <div><span>{t("Execution", "执行")}</span><strong>{t("confirmed stopped", "已确认停止")}</strong><code>exec-demo-01</code></div>
        <div><span>{t("Browser", "浏览器")}</span><strong>{t("confirmed closed", "已确认关闭")}</strong><code>browser-demo-01</code></div>
        <div><span>{t("Target HTTP", "目标 HTTP")}</span><strong>{t("not admitted", "未准入")}</strong><code>act-http</code></div>
        <div><span>{t("Temporal sync", "Temporal 同步")}</span><strong>{t("best effort sent", "已尽力发送")}</strong><code>{t("after local proof", "本地证明之后")}</code></div>
      </div>
    </section>
  );
}

function ConversationWorkspace({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const [draft, setDraft] = useState("");
  const { locale, t, label } = useLocale();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    dispatch({ type: "message", text: draft });
    setDraft("");
  }

  return (
    <div className="conversation-workspace" lang={locale}>
      <div className="conversation-main">
        <PanelHeading
          icon="message"
          title={t("Conversation-first Run", "会话优先 Run")}
          detail={t(
            "Every message is persisted before it triggers an Agent Cycle.",
            "所有消息先持久化，再触发 Agent Cycle。",
          )}
        />
        <div className="scope-banner">
          <PixelIcon name="lock" />
          <div>
            <strong>{t("Authorization boundary stays visible", "授权边界持续可见")}</strong>
            <span>IN: staging.example.test, 10.10.10.0/24</span>
            <span>OUT: 10.10.10.1, /production</span>
          </div>
          <code>AUTH-DEMO-2408</code>
        </div>
        <div className="message-feed" aria-label={t("Run conversation", "Run 对话")}>
          {state.messages.map((message) => (
            <article key={message.id} className={`message-row message-${message.role}`}>
              <div className="message-avatar">
                <PixelIcon
                  name={message.role === "operator" ? "terminal" : message.role === "agent" ? "run" : "shield"}
                />
              </div>
              <div className="message-bubble">
                <header><strong>{message.author}</strong><time>{message.at}</time></header>
                <p>{message.text}</p>
              </div>
            </article>
          ))}
        </div>
        <form className="message-composer" onSubmit={submit}>
          <label htmlFor="demo-message">{t("Send an instruction to the demo Run", "向演示 Run 发送指令")}</label>
          <div>
            <textarea
              id="demo-message"
              rows={2}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={t(
                "For example: after approval, validate only the two read-only endpoints and report the result first.",
                "例如：批准后仅验证两个只读端点，并先汇报结果。",
              )}
              disabled={state.runStatus === "cancelled"}
            />
            <button className="primary-button" type="submit" disabled={!draft.trim() || state.runStatus === "cancelled"}>
              <PixelIcon name="message" />
              {t("Send", "发送")}
            </button>
          </div>
          <small>{t(
            "This input triggers a deterministic local response only. It never connects to a model, Runner, or target.",
            "本输入只触发确定性本地响应，不会连接模型、Runner 或目标。",
          )}</small>
        </form>
      </div>

      <aside className="conversation-side">
        <article className={`inline-approval approval-${state.approvalStatus}`}>
          <header>
            <PixelIcon name={state.approvalStatus === "pending" ? "warning" : "check"} />
            <div>
              <strong>{t("Target HTTP Action", "Target HTTP 动作")}</strong>
              <span>{state.approvalStatus === "pending"
                ? t("Awaiting exact approval", "等待精确批准")
                : `${t("Decision", "决定")}: ${label(state.approvalStatus)}`}</span>
            </div>
          </header>
          <dl>
            <div><dt>{t("Tool", "工具")}</dt><dd>target_http</dd></div>
            <div><dt>{t("Method", "方法")}</dt><dd>{t("GET only", "仅 GET")}</dd></div>
            <div><dt>{t("Target", "目标")}</dt><dd>staging.example.test</dd></div>
            <div><dt>{t("Paths", "路径")}</dt><dd>/health, /version</dd></div>
            <div><dt>{t("Reason", "原因")}</dt><dd>{t("Confirm the service version boundary", "确认服务版本边界")}</dd></div>
          </dl>
          {state.approvalStatus === "pending" ? (
            <div className="stacked-actions">
              <button className="primary-button" type="button" onClick={() => dispatch({ type: "approval", decision: "approved" })}>
                <PixelIcon name="check" /> {t("Approve This Action Only", "仅批准此 Action")}
              </button>
              <button className="danger-button" type="button" onClick={() => dispatch({ type: "approval", decision: "rejected" })}>
                <PixelIcon name="stop" /> {t("Reject and Respond", "拒绝并反馈")}
              </button>
            </div>
          ) : (
            <button className="secondary-button full-width" type="button" onClick={() => dispatch({ type: "workspace", tab: "timeline" })}>
              {t("View Persisted Decision", "查看持久化决定")}
            </button>
          )}
        </article>

        <article className="working-memory">
          <h3><PixelIcon name="graph" /> {t("Working Memory", "工作记忆")}</h3>
          <dl>
            <div><dt>{t("Current focus", "当前重点")}</dt><dd>{t("Low-impact HTTP exposure validation", "低影响 HTTP 暴露验证")}</dd></div>
            <div><dt>{t("Confirmed fact", "已确认事实")}</dt><dd>{t("Ports 443 and 8443 serve HTTPS", "443 与 8443 提供 HTTPS")}</dd></div>
            <div><dt>{t("Hypothesis", "假设")}</dt><dd>{t("/version may expose component identifiers", "/version 可能泄露组件标识")}</dd></div>
            <div><dt>{t("Open question", "待解问题")}</dt><dd>{t("Is this endpoint part of the public contract?", "该端点是否属于公开契约")}</dd></div>
            <div><dt>{t("Next action", "下一步")}</dt><dd>{t("Await the act-http decision", "等待 act-http 决定")}</dd></div>
          </dl>
        </article>
      </aside>
    </div>
  );
}

function ActionsWorkspace({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const { locale, t, label } = useLocale();
  const { actionRecords } = getDemoData(locale);

  return (
    <div className="actions-workspace" lang={locale}>
      <div className="actions-main">
        <PanelHeading
          icon="shield"
          title={t("Actions, Approvals, and Executions", "Action、Approval 与 Execution")}
          detail={t(
            "Intent, human decisions, and execution attempts are three separate durable objects.",
            "意图、人工决定和执行尝试是三个独立的持久对象。",
          )}
        />
        <div className="table-wrap">
          <table className="data-table action-table">
            <thead>
              <tr>
                <th>{t("Action", "动作")}</th>
                <th>{t("Tool / Target", "工具 / 目标")}</th>
                <th>{t("Risk", "风险")}</th>
                <th>{t("Status", "状态")}</th>
                <th>{t("Duration", "耗时")}</th>
              </tr>
            </thead>
            <tbody>
              {actionRecords.map((record) => {
                const status = record.id === "act-http"
                  ? state.approvalStatus === "pending"
                    ? "pending"
                    : state.approvalStatus === "approved"
                      ? "running"
                      : "cancelled"
                  : record.status;
                return (
                  <tr key={record.id}>
                    <td><code>{record.id}</code><small>{record.command}</small></td>
                    <td><strong>{record.tool}</strong><small>{record.target}</small></td>
                    <td><span className={`risk-label risk-${record.risk}`}>{label(record.risk)}</span></td>
                    <td><span className={`state-label state-${status}`}>{label(status)}</span></td>
                    <td>{record.duration}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <section className="execution-attempt">
          <header>
            <div><PixelIcon name="terminal" /><strong>{t("Execution attempt", "执行尝试")}</strong></div>
            <span className="state-label state-completed">{label("completed")}</span>
          </header>
          <div className="execution-flow" aria-label={t("Execution lifecycle", "Execution 生命周期")}>
            {[
              [t("intent", "意图"), t("persisted", "已持久化")],
              [label("queued"), "14:31:23"],
              [t("starting", "启动中"), "14:31:24"],
              [label("running"), "8.7s"],
              [label("completed"), t("proof stored", "证明已保存")],
            ].map(([label, detail], index) => (
              <div key={label} className="is-complete">
                <span>{index + 1}</span>
                <strong>{label}</strong>
                <small>{detail}</small>
              </div>
            ))}
          </div>
          <div className="execution-output">
            <code>artifact://runs/demo/executions/exec-demo-01/stdout</code>
            <p>{t(
              "Large outputs expose only a bounded summary to the model; full content remains in an immutable Artifact.",
              "大输出只向模型提供有界摘要，完整内容保留为不可变 Artifact。",
            )}</p>
          </div>
        </section>
      </div>

      <aside className="approval-inspector">
        <h3><PixelIcon name="lock" /> {t("Immutable approval snapshot", "不可变审批快照")}</h3>
        <dl>
          <div><dt>{t("Approval ID", "审批 ID")}</dt><dd>approval-demo-03</dd></div>
          <div><dt>{t("Action ID", "动作 ID")}</dt><dd>act-http</dd></div>
          <div><dt>{t("Command", "命令")}</dt><dd>target_http GET</dd></div>
          <div><dt>CWD</dt><dd>workspace://run/demo</dd></div>
          <div><dt>{t("Target", "目标")}</dt><dd>staging.example.test</dd></div>
          <div><dt>{t("Environment diff", "环境差异")}</dt><dd>{t("none", "无")}</dd></div>
          <div><dt>{t("Policy", "策略")}</dt><dd>{t("sensitive", "敏感")} / {label("balanced")}</dd></div>
        </dl>
        <p>{t(
          "Approval resumes by Approval ID. Duplicate or out-of-order decisions never start a second execution.",
          "批准按 Approval ID 恢复。重复或乱序的决定不会启动第二次执行。",
        )}</p>
        {state.approvalStatus === "pending" ? (
          <div className="stacked-actions">
            <button className="primary-button" type="button" onClick={() => dispatch({ type: "approval", decision: "approved" })}>
              <PixelIcon name="check" /> {t("Approve Once", "批准一次")}
            </button>
            <button className="danger-button" type="button" onClick={() => dispatch({ type: "approval", decision: "rejected" })}>
              <PixelIcon name="stop" /> {t("Reject", "拒绝")}
            </button>
          </div>
        ) : (
          <div className={`decision-result decision-${state.approvalStatus}`}>
            <PixelIcon name={state.approvalStatus === "approved" ? "check" : "stop"} />
            {t("Decision persisted", "决定已持久化")}: {label(state.approvalStatus)}
          </div>
        )}
      </aside>
    </div>
  );
}

const graphEdges = [
  ["g-objective", "g-task-1"],
  ["g-objective", "g-task-2"],
  ["g-task-1", "g-asset-1"],
  ["g-task-2", "g-asset-2"],
  ["g-asset-1", "g-evidence-1"],
  ["g-asset-2", "g-evidence-2"],
  ["g-evidence-1", "g-finding"],
  ["g-evidence-2", "g-finding"],
] as const;

function edgeStyle(from: GraphNodeRecord, to: GraphNodeRecord): CSSProperties {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  return {
    left: `${from.x}%`,
    top: `${from.y}%`,
    width: `${Math.hypot(dx, dy)}%`,
    transform: `rotate(${Math.atan2(dy, dx)}rad)`,
  };
}

function GraphWorkspace({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const [view, setView] = useState<"task" | "evidence" | "operation">("evidence");
  const { locale, t, label } = useLocale();
  const { graphNodes } = getDemoData(locale);
  const selected = graphNodes.find((node) => node.id === state.selectedGraphId) ?? graphNodes[0];
  const viewLabel = (item: "task" | "evidence" | "operation") => (
    item === "operation" ? t("Operation", "行动") : label(item)
  );

  return (
    <div className="graph-workspace" lang={locale}>
      <div className="graph-main">
        <PanelHeading
          icon="graph"
          title={t("Deterministic relationship projection", "确定性关系投影")}
          detail={t(
            "The graph shows only relationships with explicit lineage in durable state; it never infers hidden links.",
            "图谱只显示持久状态中有明确 lineage 的关系，不推断隐藏关联。",
          )}
          action={
            <div className="segmented-control" aria-label={t("Graph view", "图谱视图")}>
              {(["task", "evidence", "operation"] as const).map((item) => (
                <button key={item} type="button" className={view === item ? "is-active" : ""} onClick={() => setView(item)}>
                  {viewLabel(item)}
                </button>
              ))}
            </div>
          }
        />
        <div className={`graph-canvas graph-view-${view}`}>
          <div className="graph-grid-label">
            {viewLabel(view).toUpperCase()} {t("PROJECTION / SNAPSHOT", "投影 / 快照")} DEMO-42
          </div>
          {graphEdges.map(([fromId, toId]) => {
            const from = graphNodes.find((node) => node.id === fromId)!;
            const to = graphNodes.find((node) => node.id === toId)!;
            return <span key={`${fromId}-${toId}`} className="graph-edge" style={edgeStyle(from, to)} aria-hidden="true" />;
          })}
          {graphNodes.map((node) => (
            <button
              key={node.id}
              type="button"
              className={`graph-node graph-kind-${node.kind} graph-status-${node.status}${selected.id === node.id ? " is-selected" : ""}`}
              style={{ left: `${node.x}%`, top: `${node.y}%` }}
              onClick={() => dispatch({ type: "graph", id: node.id })}
            >
              <span>{label(node.kind)}</span>
              <strong>{node.label}</strong>
            </button>
          ))}
        </div>
        <div className="graph-legend" aria-label={t("Graph legend", "图谱图例")}>
          {(["objective", "task", "asset", "evidence", "finding"] as const).map((kind) => (
            <span key={kind} className={`legend-${kind}`}><i aria-hidden="true" />{label(kind)}</span>
          ))}
        </div>
      </div>
      <aside className="graph-inspector">
        <h3><PixelIcon name="target" /> {t("Node inspector", "节点检查器")}</h3>
        <dl>
          <div><dt>ID</dt><dd>{selected.id}</dd></div>
          <div><dt>{t("Type", "类型")}</dt><dd>{label(selected.kind)}</dd></div>
          <div><dt>{t("Status", "状态")}</dt><dd>{label(selected.status)}</dd></div>
          <div><dt>{t("Label", "标签")}</dt><dd>{selected.label}</dd></div>
          <div><dt>{t("Quality", "质量")}</dt><dd>{t("deterministic", "确定性")}</dd></div>
          <div><dt>{t("Provenance", "来源")}</dt><dd>{t("2 persisted references", "2 个持久化引用")}</dd></div>
        </dl>
        <div className="integrity-note">
          <PixelIcon name="shield" />
          {t(
            "A scope, integrity, or authorization mismatch fails the entire projection closed.",
            "Scope、integrity 或 authorization 不匹配时，整批投影会失败关闭。",
          )}
        </div>
      </aside>
    </div>
  );
}

function TrafficWorkspace({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const { locale, t, label } = useLocale();
  const { trafficExchanges } = getDemoData(locale);
  const selected = trafficExchanges.find((item) => item.id === state.selectedTrafficId) ?? trafficExchanges[0];

  return (
    <div className="traffic-workspace" lang={locale}>
      <div className="traffic-main">
        <PanelHeading
          icon="traffic"
          title={t("Target HTTP — metadata only", "Target HTTP — 仅元数据")}
          detail={t(
            "Shows only the minimum metadata needed for execution decisions. Bodies, headers, cookies, and authentication material are never loaded.",
            "仅展示执行决策所需的最小元数据，不加载正文、Header、Cookie 或认证材料。",
          )}
          action={<span className="readonly-badge"><PixelIcon name="lock" /> {t("READ ONLY", "只读")}</span>}
        />
        <div className="traffic-policy-strip">
          {[
            [t("Body", "正文"), t("hidden", "已隐藏")],
            [t("Headers", "请求头"), t("hidden", "已隐藏")],
            [t("Cookies", "Cookie"), t("hidden", "已隐藏")],
            [t("Replay", "重放"), t("disabled", "已禁用")],
            [t("Download", "下载"), t("disabled", "已禁用")],
          ].map(([label, value]) => <span key={label}><strong>{label}</strong>{value}</span>)}
        </div>
        <div className="table-wrap">
          <table className="data-table traffic-table">
            <thead><tr><th>{t("Time", "时间")}</th><th>{t("Source", "来源")}</th><th>{t("Method", "方法")}</th><th>{t("Target", "目标")}</th><th>{t("Status", "状态")}</th><th>{t("Latency", "延迟")}</th></tr></thead>
            <tbody>
              {trafficExchanges.map((item) => (
                <tr key={item.id} className={selected.id === item.id ? "is-selected" : ""}>
                  <td><button type="button" onClick={() => dispatch({ type: "traffic", id: item.id })}>{item.at}</button></td>
                  <td>{item.source}</td>
                  <td><code>{item.method}</code></td>
                  <td><strong>{item.host}</strong><small>{item.path}</small></td>
                  <td><span className={`http-status http-${Math.floor(item.status / 100)}xx`}>{item.status}</span></td>
                  <td>{item.duration}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <aside className="traffic-inspector">
        <h3><PixelIcon name="file" /> {t("Exchange inspector", "交换检查器")}</h3>
        <dl>
          <div><dt>ID</dt><dd>{selected.id}</dd></div>
          <div><dt>{t("Captured by", "捕获来源")}</dt><dd>{selected.source}</dd></div>
          <div><dt>{t("Method", "方法")}</dt><dd>{selected.method}</dd></div>
          <div><dt>{t("Origin", "来源站点")}</dt><dd>https://{selected.host}</dd></div>
          <div><dt>{t("Path shape", "路径形式")}</dt><dd>{selected.path}</dd></div>
          <div><dt>{t("Status", "状态")}</dt><dd>{selected.status}</dd></div>
          <div><dt>{t("Transfer", "传输量")}</dt><dd>{selected.size}</dd></div>
          <div><dt>{t("Artifact", "工件")}</dt><dd>{selected.artifact}</dd></div>
          <div><dt>TLS</dt><dd>{label("verified")} / {t("synthetic", "合成")}</dd></div>
        </dl>
        <div className="integrity-note"><PixelIcon name="lock" /> {t(
          "The full exchange is stored only as an immutable Artifact.",
          "完整交换只作为不可变 Artifact 保存。",
        )}</div>
      </aside>
    </div>
  );
}

function TerminalWorkspace({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const { locale, t, label } = useLocale();
  const { terminalSeed } = getDemoData(locale);
  const [lines, setLines] = useState(terminalSeed);
  const [command, setCommand] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = command.trim();
    if (!value) return;
    setLines((current) => [
      ...current,
      `$ ${value}`,
      t(
        "[demo] command recorded locally; no shell or process was started",
        "[demo] 命令已在本地记录；未启动 Shell 或进程",
      ),
    ]);
    setCommand("");
  }

  return (
    <div className="terminal-workspace" lang={locale}>
      <div className="terminal-main">
        <PanelHeading
          icon="terminal"
          title={t("Synthetic PTY transcript", "合成 PTY 记录")}
          detail={t(
            "Demonstrates input, resize, Ctrl+C, takeover, release, and close semantics without ever executing a real Shell.",
            "展示输入、Resize、Ctrl+C、Takeover、Release 与关闭语义，但绝不执行真实 Shell。",
          )}
          action={<span className={`owner-badge owner-${state.terminalOwner}`}>{t("OWNER", "所有者")} / {label(state.terminalOwner).toUpperCase()}</span>}
        />
        <div className="terminal-frame" role="log" aria-label={t("Synthetic terminal output", "合成终端输出")}>
          <div className="terminal-toolbar"><span>80 x 24</span><span>UTF-8</span><span>exec-demo-pty-01</span></div>
          <pre>{lines.join("\n")}</pre>
          <form onSubmit={submit}>
            <label htmlFor="demo-command">{t("Synthetic command input", "合成命令输入")}</label>
            <span aria-hidden="true">$</span>
            <input
              id="demo-command"
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              disabled={state.terminalOwner !== "operator" || state.runStatus === "cancelled"}
              placeholder={state.terminalOwner === "operator"
                ? t("Enter any demo text", "输入任意演示文本")
                : t("Operator takeover required", "先由 Operator 接管")}
              autoComplete="off"
            />
            <button className="primary-button" type="submit" disabled={!command.trim() || state.terminalOwner !== "operator"}>
              {t("Send", "发送")}
            </button>
          </form>
        </div>
      </div>
      <aside className="terminal-control">
        <h3><PixelIcon name="shield" /> {t("PTY ownership", "PTY 所有权")}</h3>
        <p>{t(
          "Takeover blocks Agent writes while preserving read-only observation. Release creates a durable summary and returns ownership.",
          "Takeover 会拒绝 Agent 写入，同时保留只读观察。Release 生成持久摘要并归还所有权。",
        )}</p>
        <div className="ownership-diagram">
          <div className={state.terminalOwner === "agent" ? "is-owner" : ""}><PixelIcon name="run" /><strong>Agent</strong><span>{t("write + observe", "写入 + 观察")}</span></div>
          <span className="ownership-link" aria-hidden="true" />
          <div className={state.terminalOwner === "operator" ? "is-owner" : ""}><PixelIcon name="terminal" /><strong>Operator</strong><span>{t("interactive owner", "交互所有者")}</span></div>
        </div>
        <button className="primary-button full-width" type="button" onClick={() => dispatch({ type: "terminal-owner" })} disabled={state.runStatus === "cancelled"}>
          <PixelIcon name={state.terminalOwner === "agent" ? "terminal" : "run"} />
          {state.terminalOwner === "agent" ? t("Operator Takeover", "Operator 接管") : t("Release to Agent", "归还 Agent")}
        </button>
        <button className="secondary-button full-width" type="button" disabled>
          {t("Ctrl+C / no active process", "Ctrl+C / 无活动进程")}
        </button>
      </aside>
    </div>
  );
}

function EvidenceWorkspace() {
  const [section, setSection] = useState<"artifacts" | "findings" | "reports">("artifacts");
  const [selected, setSelected] = useState("art-01");
  const { locale, t, label } = useLocale();
  const { activeRun, artifacts, findings, reports } = getDemoData(locale);
  const sectionLabels = {
    artifacts: t("Artifacts", "工件"),
    findings: t("Findings", "发现"),
    reports: t("Reports", "报告"),
  } as const;
  const artifactTypeLabel = (type: "stdout" | "http" | "screenshot" | "note") => ({
    stdout: t("stdout", "标准输出"),
    http: "HTTP",
    screenshot: t("screenshot", "截图"),
    note: t("note", "记录"),
  })[type];

  return (
    <div className="evidence-workspace" lang={locale}>
      <PanelHeading
        icon="evidence"
        title={t("Evidence chain and delivery", "证据链与交付")}
        detail={t(
          "Artifacts are immutable, Findings reference evidence, and Reports are generated only from durable state.",
          "Artifact 不可变，Finding 引用证据，Report 只从持久状态生成。",
        )}
        action={
          <div className="segmented-control" aria-label={t("Evidence workspace", "证据工作区")}>
            {(["artifacts", "findings", "reports"] as const).map((item) => (
              <button
                key={item}
                type="button"
                className={section === item ? "is-active" : ""}
                aria-label={sectionLabels[item]}
                onClick={() => setSection(item)}
              >
                {sectionLabels[item]}
              </button>
            ))}
          </div>
        }
      />

      {section === "artifacts" ? (
        <div className="evidence-split">
          <div className="evidence-list">
            {artifacts.map((artifact) => (
              <button key={artifact.id} type="button" className={selected === artifact.id ? "is-selected" : ""} onClick={() => setSelected(artifact.id)}>
                <PixelIcon name={artifact.type === "http" ? "traffic" : artifact.type === "screenshot" ? "evidence" : "file"} />
                <span><strong>{artifact.name}</strong><small>{artifactTypeLabel(artifact.type)} / {artifact.size}</small></span>
                <span className="state-label state-completed">{label(artifact.integrity)}</span>
              </button>
            ))}
          </div>
          <article className="artifact-inspector">
            <h3>{t("Artifact integrity", "Artifact 完整性")}</h3>
            <dl>
              <div><dt>{t("Logical ID", "逻辑 ID")}</dt><dd>artifact://runs/demo/{selected}</dd></div>
              <div><dt>SHA-256</dt><dd>{label("verified")} / {t("synthetic fixture", "合成固定数据")}</dd></div>
              <div><dt>{t("Owner", "所有者")}</dt><dd>{activeRun.id}</dd></div>
              <div><dt>{t("Created by", "创建者")}</dt><dd>exec-demo-01</dd></div>
              <div><dt>{t("Model exposure", "模型可见范围")}</dt><dd>{t("bounded summary only", "仅限有界摘要")}</dd></div>
            </dl>
            <p>{t(
              "The Demo provides no real downloads. The production WebUI downloads Run-owned Artifacts according to authorization.",
              "Demo 不提供真实下载。生产 WebUI 根据权限下载 Run-owned Artifact。",
            )}</p>
          </article>
        </div>
      ) : null}

      {section === "findings" ? (
        <div className="finding-board">
          {findings.map((finding) => (
            <article key={finding.id}>
              <header><code>{finding.id}</code><span className={`state-label state-${finding.state}`}>{label(finding.state)}</span></header>
              <h3>{finding.title}</h3>
              <p>{finding.confidence}</p>
              <div>{finding.evidence.map((id) => <code key={id}>{id}</code>)}</div>
              <button className="secondary-button" type="button">{t("Open Evidence Links", "打开证据关联")}</button>
            </article>
          ))}
          <article className="finding-empty">
            <PixelIcon name="shield" />
            <h3>{t("Scan output is never auto-confirmed", "不凭扫描结果自动确认")}</h3>
            <p>{t(
              "An Operator must review the evidence, impact, reproduction steps, and recommendation for every Finding.",
              "Finding 必须由 Operator 复核证据、影响、复现步骤和建议。",
            )}</p>
          </article>
        </div>
      ) : null}

      {section === "reports" ? (
        <div className="report-list">
          {reports.map((report) => (
            <article key={report.id}>
              <PixelIcon name="file" />
              <div><strong>{report.name}</strong><span>{report.format} / {t(
                `${report.sections} sections`,
                `${report.sections} 个章节`,
              )}</span></div>
              <span className={`state-label state-${report.state}`}>{label(report.state)}</span>
              <button className="secondary-button" type="button">{t("Preview", "预览")}</button>
            </article>
          ))}
          <div className="report-boundary">
            <PixelIcon name="alert" />
            <p>{t(
              "V2 delivers Markdown, HTML, and JSON. This Demo does not claim PDF or DOCX export.",
              "V2 提供 Markdown、HTML 与 JSON。此 Demo 不宣传 PDF 或 DOCX 导出。",
            )}</p>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function TimelineWorkspace({ state }: { state: DemoState }) {
  const [mode, setMode] = useState<"timeline" | "raw">("timeline");
  const [selected, setSelected] = useState(state.timeline[0]?.id ?? "");
  const { locale, t, label } = useLocale();
  const { activeRun } = getDemoData(locale);
  const record = state.timeline.find((item) => item.id === selected) ?? state.timeline[0];

  return (
    <div className="timeline-workspace" lang={locale}>
      <div className="timeline-main">
        <PanelHeading
          icon="file"
          title={t("Durable audit trail", "持久审计轨迹")}
          detail={t(
            "Timeline is operator-facing; Raw events retains the latest 200 debugging projections.",
            "Timeline 面向操作员，Raw events 保留最近 200 条调试投影。",
          )}
          action={
            <div className="segmented-control" aria-label={t("Audit event view", "审计事件视图")}>
              <button
                type="button"
                className={mode === "timeline" ? "is-active" : ""}
                aria-label={t("Timeline", "时间线")}
                onClick={() => setMode("timeline")}
              >
                {t("Timeline", "时间线")}
              </button>
              <button
                type="button"
                className={mode === "raw" ? "is-active" : ""}
                aria-label={t("Raw events", "原始事件")}
                onClick={() => setMode("raw")}
              >
                {t("Raw events", "原始事件")}
              </button>
            </div>
          }
        />
        {mode === "timeline" ? (
          <ol className="timeline-list" aria-label={t("Audit timeline", "审计时间线")}>
            {state.timeline.map((item) => (
              <li key={item.id} className={`timeline-${item.tone}${selected === item.id ? " is-selected" : ""}`}>
                <button type="button" onClick={() => setSelected(item.id)}>
                  <time>{item.at}</time>
                  <span className="timeline-marker" aria-hidden="true" />
                  <span><code>{item.type}</code><strong>{item.title}</strong><small>{item.detail}</small></span>
                </button>
              </li>
            ))}
          </ol>
        ) : (
          <div className="raw-events" role="log" aria-label={t("Raw audit events", "原始审计事件")}>
            {state.timeline.map((item, index) => (
              <button key={item.id} type="button" onClick={() => setSelected(item.id)}>
                <span>{String(index + 1).padStart(3, "0")}</span>
                <code>{item.at}</code>
                <strong>{item.type}</strong>
                <small>{item.id}</small>
              </button>
            ))}
          </div>
        )}
      </div>
      <aside
        className="event-inspector"
        aria-label={t(`Event payload — ${label("active")}`, `事件载荷 — ${label("active")}`)}
      >
        <h3><PixelIcon name="file" /> {t("Event payload", "事件载荷")}</h3>
        {record ? (
          <pre>{JSON.stringify({
            id: record.id,
            run_id: activeRun.id,
            event_type: record.type,
            created_at: record.at,
            projection: "DEMO / SANITIZED",
            detail: record.detail,
          }, null, 2)}</pre>
        ) : null}
        <p>{t(
          "Displayed content is sanitized for the Demo and contains no credentials, absolute paths, or secret URLs.",
          "显示内容经过演示脱敏，不包含凭据、绝对路径或秘密 URL。",
        )}</p>
      </aside>
    </div>
  );
}
