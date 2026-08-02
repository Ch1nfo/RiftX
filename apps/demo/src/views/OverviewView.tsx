import type { Dispatch } from "react";

import { PixelIcon } from "../components/PixelIcon";
import { DemoStamp, PanelHeading, StatusPill } from "../components/Ui";
import { getDemoData } from "../data/demo";
import type { DemoAction, DemoState, PrimaryView } from "../data/demoMachine";
import { useLocale } from "../i18n";

export function OverviewView({
  state,
  dispatch,
  navigate,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
  navigate: (view: PrimaryView) => void;
}) {
  const { locale, t, label } = useLocale();
  const { actionRecords, activeRun, runQueue, tools } = getDemoData(locale);
  const availableTools = tools.filter((tool) => tool.availability === "available").length;
  const pendingActions = actionRecords.filter((action) => action.status === "pending").length;
  const missionStages = [
    { label: t("Authorization boundary", "授权边界"), state: "complete" },
    { label: t("Low-impact discovery", "低影响发现"), state: "complete" },
    { label: t("Active validation", "主动验证"), state: "active" },
    { label: t("Evidence preservation", "证据固化"), state: "queued" },
    { label: t("Report delivery", "报告交付"), state: "queued" },
  ] as const;
  const tourStops: Array<{
    title: string;
    detail: string;
    view: PrimaryView;
    icon: "target" | "message" | "shield" | "evidence" | "stop";
  }> = [
    {
      title: t("Define an authorized operation", "定义授权行动"),
      detail: t(
        "Objective, authorization, scope, exclusions, Node, Model, and approval mode.",
        "目标、授权编号、Scope、排除项、Node、Model 与审批模式。",
      ),
      view: "mission",
      icon: "target",
    },
    {
      title: t("Start from conversation", "从会话启动"),
      detail: t(
        "Creating a Run is not execution; the first explicit instruction starts an Agent Cycle.",
        "创建 Run 不等于执行，第一条明确指令才启动 Agent Cycle。",
      ),
      view: "operation",
      icon: "message",
    },
    {
      title: t("Approve every effect", "逐项人工批准"),
      detail: t(
        "Every Action keeps its own identity, exact target, and recoverable decision.",
        "每个 Action 保留独立身份、精确目标和可恢复决定。",
      ),
      view: "operation",
      icon: "shield",
    },
    {
      title: t("Trace the evidence chain", "追踪证据链"),
      detail: t(
        "Artifacts, Findings, Graph, Traffic, and Reports remain attributable.",
        "Artifact、Finding、Graph、Traffic 和 Report 保持可归因。",
      ),
      view: "operation",
      icon: "evidence",
    },
    {
      title: t("Verify a real stop", "验证真实停止"),
      detail: t(
        "Fence new effects, then wait for affirmative proof from every effect owner.",
        "先围栏新效果，再等待每个效果所有者给出停止证明。",
      ),
      view: "operation",
      icon: "stop",
    },
  ];

  return (
    <div className="screen-stack overview-screen">
      <section className="command-deck pixel-panel">
        <div className="command-deck-copy">
          <DemoStamp />
          <h1>
            {t("Turn every red-team operation", "把每一次红队行动")}
            <br />
            {t("into a recoverable control protocol.", "变成可恢复的控制协议。")}
          </h1>
          <p>
            {t(
              "This is not an attack simulator. Local synthetic state demonstrates how RiftX makes boundaries, approvals, execution, evidence, and stop proof durable.",
              "这不是攻击模拟器。它用本地合成状态完整展示 RiftX 如何持久化边界、批准、执行、证据和停止证明。",
            )}
          </p>
          <div className="command-actions">
            <button className="primary-button" type="button" onClick={() => navigate("operation")}>
              <PixelIcon name="run" />
              {t("Enter operation workspace", "进入行动空间")}
            </button>
            <button className="secondary-button" type="button" onClick={() => navigate("mission")}>
              <PixelIcon name="target" />
              {t("Create demo Run", "新建演示 Run")}
            </button>
          </div>
        </div>

        <div className="live-cartridge" aria-label={t("Current demo operation", "当前演示行动")}>
          <header>
            <div>
              <span>{t("ACTIVE CARTRIDGE", "活动任务匣")}</span>
              <strong>{activeRun.engagement}</strong>
            </div>
            <StatusPill status={state.runStatus} />
          </header>
          <div className="scope-lock">
            <PixelIcon name="lock" />
            <div>
              <span>{t("Authorized target", "授权目标")}</span>
              <strong>{activeRun.target}</strong>
            </div>
            <code>AUTH-DEMO-2408</code>
          </div>
          <ol className="mission-stage-map" aria-label={t("Operation progress", "行动进度")}>
            {missionStages.map((stage, index) => (
              <li key={stage.label} className={`stage-${stage.state}`}>
                <span className="stage-node">{index + 1}</span>
                <span>{stage.label}</span>
              </li>
            ))}
          </ol>
          <div className="effect-proof-grid">
            <span><PixelIcon name="terminal" />{t("Execution", "执行")} <strong>{t("confirmed", "已确认")}</strong></span>
            <span><PixelIcon name="traffic" />{t("Browser", "浏览器")} <strong>{t("observed", "已观察")}</strong></span>
            <span><PixelIcon name="shield" />HTTP <strong>{t("awaiting", "等待中")}</strong></span>
          </div>
        </div>
      </section>

      <section className="telemetry-strip" aria-label={t("Synthetic demo overview", "合成演示概览")}>
        <div><span>{t("ACTIVE RUNS", "活动 RUN")}</span><strong>{runQueue.filter((run) => run.status !== "completed").length}</strong><small>{t("Local synthetic state", "本地合成状态")}</small></div>
        <div><span>{t("ACTIONS AWAITING APPROVAL", "待审批 ACTION")}</span><strong>{pendingActions}</strong><small>{t("Individually attributable", "逐项可归因")}</small></div>
        <div><span>{t("AVAILABLE TOOLS", "可用工具")}</span><strong>{availableTools}/{tools.length}</strong><small>{t("registry generation 42", "注册表世代 42")}</small></div>
        <div><span>{t("RECOVERY METRIC", "恢复指标")}</span><strong>N/A</strong><small>{t("No sample is never shown as zero", "无样本不显示为 0")}</small></div>
      </section>

      <section className="overview-grid">
        <article className="pixel-panel queue-panel">
          <PanelHeading
            icon="run"
            title={t("Durable operation queue", "持久行动队列")}
            detail={t("The browser is only a projection; closing it does not stop the workflow.", "浏览器只是投影，关闭页面不会停止工作流。")}
            action={
              <button className="text-button" type="button" onClick={() => navigate("mission")}>
                {t("Configure operation", "配置行动")} <PixelIcon name="chevron" />
              </button>
            }
          />
          <div className="run-queue">
            {runQueue.map((run) => (
              <button key={run.id} className="run-queue-row" type="button" onClick={() => navigate("operation")}>
                <span className="run-glyph"><PixelIcon name="terminal" /></span>
                <span className="run-copy"><strong>{run.objective}</strong><small>{run.id.slice(-6)} / {run.target} / {run.updated}</small></span>
                <StatusPill status={run.id === activeRun.id ? state.runStatus : run.status} />
                <PixelIcon name="chevron" />
              </button>
            ))}
          </div>
        </article>

        <article className="pixel-panel approval-console">
          <PanelHeading
            icon="shield"
            title={t("Current human boundary", "当前人工边界")}
            detail={t("Target HTTP Action awaits an independent decision.", "Target HTTP Action 等待独立决定。")}
          />
          <div className="approval-command">
            <div><span>{t("ACTION", "动作")}</span><code>act-http</code></div>
            <div><span>{t("TOOL", "工具")}</span><code>target_http</code></div>
            <div><span>{t("TARGET", "目标")}</span><code>staging.example.test</code></div>
          </div>
          <p className="approval-reason">
            {t("Request two bounded GETs to ", "请求对 ")}
            <code>/health</code>
            {t(" and ", " 与 ")}
            <code>/version</code>
            {t(". Exclusions and request count are locked.", " 发起两次有界 GET。排除项和请求数量已锁定。")}
          </p>
          {state.approvalStatus === "pending" ? (
            <div className="approval-actions">
              <button className="primary-button" type="button" onClick={() => dispatch({ type: "approval", decision: "approved" })}>
                <PixelIcon name="check" />{t("Approve this Action only", "仅批准此 Action")}
              </button>
              <button className="danger-button" type="button" onClick={() => dispatch({ type: "approval", decision: "rejected" })}>
                <PixelIcon name="stop" />{t("Reject", "拒绝")}
              </button>
            </div>
          ) : (
            <div className={`decision-result decision-${state.approvalStatus}`}>
              <PixelIcon name={state.approvalStatus === "approved" ? "check" : "stop"} />
              <span>{t("Local decision: ", "本地决定：")}{label(state.approvalStatus)}</span>
              <button type="button" className="text-button" onClick={() => navigate("operation")}>
                {t("View audit trail", "查看审计轨迹")}
              </button>
            </div>
          )}
        </article>
      </section>

      <section className="guided-tour pixel-panel">
        <PanelHeading
          icon="graph"
          title={t("A complete product demo route", "一条完整的产品演示路线")}
          detail={t("Follow it in about five minutes, or jump directly to any feature.", "按顺序走完约 5 分钟，也可以直接进入任意功能。")}
        />
        <div className="tour-track" role="list">
          {tourStops.map((stop, index) => (
            <button
              key={stop.title}
              type="button"
              role="listitem"
              className={state.tourStep === index ? "is-active" : ""}
              onClick={() => {
                dispatch({ type: "tour", step: index });
                if (stop.view === "operation") {
                  const tabs = ["conversation", "actions", "evidence", "timeline"] as const;
                  dispatch({ type: "workspace", tab: tabs[Math.min(index - 1, tabs.length - 1)] ?? "conversation" });
                }
                navigate(stop.view);
              }}
            >
              <span className="tour-index">{String(index + 1).padStart(2, "0")}</span>
              <PixelIcon name={stop.icon} />
              <strong>{stop.title}</strong>
              <small>{stop.detail}</small>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
