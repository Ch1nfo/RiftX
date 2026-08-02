import { useMemo, useState, type Dispatch, type FormEvent } from "react";

import { PixelIcon } from "../components/PixelIcon";
import { DemoStamp, PanelHeading } from "../components/Ui";
import type { DemoAction } from "../data/demoMachine";
import { useLocale } from "../i18n";

type ApprovalMode = "auto" | "balanced" | "manual";

export function MissionView({ dispatch }: { dispatch: Dispatch<DemoAction> }) {
  const { t } = useLocale();
  const [engagement, setEngagement] = useState("Q3 STAGING VALIDATION");
  const [authorization, setAuthorization] = useState("AUTH-DEMO-2408");
  const [objective, setObjective] = useState(
    t(
      "Validate the external exposure of an authorized test environment and build a traceable evidence chain.",
      "验证授权测试环境的外部暴露面，并形成可追溯证据链。",
    ),
  );
  const [success, setSuccess] = useState(
    t(
      "Confirm public service and version boundaries\nProduce evidence-supported findings\nDeliver Markdown, HTML, and JSON reports",
      "确认公开服务与版本边界\n形成证据支持的发现\n输出 Markdown、HTML 与 JSON 报告",
    ),
  );
  const [entryPoints, setEntryPoints] = useState("url=https://staging.example.test\nip=10.10.10.24");
  const [scope, setScope] = useState("10.10.10.0/24\nstaging.example.test\napi.example.test");
  const [exclusions, setExclusions] = useState("10.10.10.1\n/production");
  const [node, setNode] = useState("runner-linux-01");
  const [model, setModel] = useState("primary");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>("balanced");

  const scopePreview = useMemo(
    () => scope.split("\n").map((value) => value.trim()).filter(Boolean),
    [scope],
  );
  const exclusionPreview = useMemo(
    () => exclusions.split("\n").map((value) => value.trim()).filter(Boolean),
    [exclusions],
  );

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    dispatch({ type: "launch", objective: objective.trim() });
  }

  const approvalModes: Array<readonly [ApprovalMode, string, string]> = [
    ["auto", t("Automatic", "自动"), t("Run only low-risk effects explicitly allowed by policy.", "仅执行策略明确允许的低风险效果。")],
    ["balanced", t("Balanced", "平衡"), t("Approve sensitive effects one by one; recommended for this Demo.", "敏感动作逐项批准，适合演示。")],
    ["manual", t("Manual", "手动"), t("Every external effect requires an Operator decision.", "每个外部效果都需要 Operator 决定。")],
  ];

  return (
    <div className="screen-stack mission-screen">
      <header className="screen-heading">
        <div>
          <DemoStamp />
          <h1>{t("Lock the authorization boundary before creating a Run.", "先锁定授权边界，再创建 Run。")}</h1>
          <p>{t("This form updates local demo state only. The Run stops at waiting_user after creation and never calls a model or tool automatically.", "此表单只更新本地演示状态。创建后 Run 停在 waiting_user，不会自动调用模型或工具。")}</p>
        </div>
        <div className="heading-status-block">
          <span>{t("TRUST PROFILE", "信任配置")}</span>
          <strong>local_single_operator</strong>
          <small>{t("loopback only", "仅限 loopback")}</small>
        </div>
      </header>

      <form className="mission-layout" onSubmit={submit}>
        <div className="mission-form pixel-panel">
          <section className="form-cluster">
            <PanelHeading
              icon="target"
              title={t("Operation objective", "行动目标")}
              detail={t("Describe the authorized outcome, not a list of commands.", "写清结果和授权依据，不要从命令清单开始。")}
            />
            <div className="form-grid two-columns">
              <label className="field">
                <span>{t("Engagement name", "Engagement 名称")}</span>
                <input required value={engagement} onChange={(event) => setEngagement(event.target.value)} autoComplete="off" />
                <small>{t("Used to archive and locate the Demo Run.", "用于 Run 归档与演示定位。")}</small>
              </label>
              <label className="field">
                <span>{t("Authorization reference", "授权参考")}</span>
                <input required value={authorization} onChange={(event) => setAuthorization(event.target.value)} autoComplete="off" />
                <small>{t("Synthetic value; never enter a real ticket or contract number.", "演示值，不应输入真实工单或合同编号。")}</small>
              </label>
              <label className="field span-two">
                <span>{t("Objective", "目标")}</span>
                <textarea required rows={3} value={objective} onChange={(event) => setObjective(event.target.value)} />
                <small>{t("Describe the intended outcome; the Agent plans within this boundary.", "描述期望结果，Agent 会在此边界内规划。")}</small>
              </label>
              <label className="field span-two">
                <span>{t("Success criteria", "成功标准")}</span>
                <textarea rows={3} value={success} onChange={(event) => setSuccess(event.target.value)} />
                <small>{t("One criterion per line; reports and the completion fence reference them.", "每行一条，报告和 completion fence 会引用这些标准。")}</small>
              </label>
            </div>
          </section>

          <section className="form-cluster">
            <PanelHeading
              icon="lock"
              title={t("Entry points, scope, and exclusions", "入口、范围与排除项")}
              detail={t("List every asset that may be touched—and every path that must never be touched.", "明确列出可以触达的资产，也明确列出绝不能触达的路径。")}
            />
            <div className="form-grid two-columns">
              <label className="field span-two">
                <span>{t("Entry points", "入口点")}</span>
                <textarea rows={3} value={entryPoints} onChange={(event) => setEntryPoints(event.target.value)} />
                <small>{t("Use kind=value. Supported kinds: cidr, ip, domain, url, file, and text.", "格式为 kind=value，支持 cidr、ip、domain、url、file 与 text。")}</small>
              </label>
              <label className="field">
                <span>{t("Positive scope", "允许范围")}</span>
                <textarea rows={4} value={scope} onChange={(event) => setScope(event.target.value)} />
              </label>
              <label className="field">
                <span>{t("Hard exclusions", "明确排除")}</span>
                <textarea rows={4} value={exclusions} onChange={(event) => setExclusions(event.target.value)} />
              </label>
            </div>
          </section>

          <section className="form-cluster">
            <PanelHeading
              icon="server"
              title={t("Runtime boundary", "运行边界")}
              detail={t("Node determines effect ownership, Model determines the reasoning channel, and approval mode determines human intervention.", "Node 决定效果所有者，Model 决定推理通道，审批模式决定人工介入。")}
            />
            <div className="form-grid two-columns">
              <label className="field">
                <span>{t("Execution node", "执行节点")}</span>
                <select value={node} onChange={(event) => setNode(event.target.value)}>
                  <option value="runner-linux-01">{t("Isolated Linux Runner / online", "隔离 Linux Runner / 在线")}</option>
                  <option value="local">{t("Local Operator Node / read-only demo", "本地 Operator 节点 / 只读演示")}</option>
                  <option value="runner-win-lab" disabled>{t("Windows Lab Runner / offline", "Windows 实验室 Runner / 离线")}</option>
                </select>
                <small>{t("For high-risk demonstrations, select the Linux Runner with a cgroup v2 proof boundary.", "高风险演示选择具有 cgroup v2 证明边界的 Linux Runner。")}</small>
              </label>
              <label className="field">
                <span>{t("Model profile", "模型 Profile")}</span>
                <select value={model} onChange={(event) => setModel(event.target.value)}>
                  <option value="primary">primary / redteam-reasoner / {t("ready", "就绪")}</option>
                  <option value="fast-triage">fast-triage / analysis-mini / {t("ready", "就绪")}</option>
                  <option value="local-lab">local-lab / local-model / {t("keyless", "无需密钥")}</option>
                </select>
                <small>{t("The Demo does not read a Base URL and never accepts or stores an API Key.", "Demo 不读取 Base URL，也不接受或保存 API Key。")}</small>
              </label>
            </div>

            <fieldset className="approval-mode-fieldset">
              <legend>{t("Approval mode", "审批模式")}</legend>
              <div className="approval-mode-grid">
                {approvalModes.map(([value, modeLabel, detail]) => (
                  <label key={value} className={approvalMode === value ? "is-selected" : ""}>
                    <input type="radio" name="approval-mode" value={value} checked={approvalMode === value} onChange={() => setApprovalMode(value)} />
                    <span className="mode-radio" aria-hidden="true" />
                    <strong>{modeLabel}</strong>
                    <small>{detail}</small>
                  </label>
                ))}
              </div>
            </fieldset>
          </section>
        </div>

        <aside className="mission-review">
          <section className="pixel-panel boundary-preview">
            <PanelHeading icon="shield" title={t("Boundary review", "边界预检")} detail={t("Read it one final time before submitting.", "提交前最后一次阅读。")} />
            <dl className="review-facts">
              <div><dt>{t("Engagement", "行动")}</dt><dd>{engagement || t("Not provided", "未填写")}</dd></div>
              <div><dt>{t("Authorization", "授权")}</dt><dd>{authorization || t("Not provided", "未填写")}</dd></div>
              <div><dt>{t("Node", "节点")}</dt><dd>{node}</dd></div>
              <div><dt>{t("Model", "模型")}</dt><dd>{model}</dd></div>
              <div><dt>{t("Approval", "审批")}</dt><dd>{approvalMode}</dd></div>
            </dl>

            <div className="scope-preview">
              <strong>{t("Allowed scope", "允许范围")}</strong>
              <div className="token-list">{scopePreview.map((value) => <code key={value}>{value}</code>)}</div>
            </div>
            <div className="scope-preview exclusion-preview">
              <strong>{t("Explicit exclusions", "明确排除")}</strong>
              <div className="token-list">{exclusionPreview.map((value) => <code key={value}>{value}</code>)}</div>
            </div>
          </section>

          <section className="pixel-panel creation-contract">
            <PixelIcon name="message" />
            <h2>{t("Creation is not execution", "创建不等于执行")}</h2>
            <p>{t("The button below creates durable context only. The Run remains waiting_user until the Operator sends the first explicit instruction.", "点击下方按钮只创建持久上下文。Run 会停在 waiting_user，直到 Operator 发送第一条具体指令。")}</p>
            <ul>
              <li><PixelIcon name="check" />{t("No model call", "不调用模型")}</li>
              <li><PixelIcon name="check" />{t("No tool preparation", "不准备工具")}</li>
              <li><PixelIcon name="check" />{t("No external effect", "不产生外部效果")}</li>
            </ul>
            <button className="primary-button full-width" type="submit">
              <PixelIcon name="run" />{t("Create demo Run", "创建演示 Run")}
            </button>
          </section>
        </aside>
      </form>
    </div>
  );
}
