import { useState, type Dispatch } from "react";

import { PixelIcon } from "../components/PixelIcon";
import { DemoStamp, PanelHeading } from "../components/Ui";
import { getDemoData } from "../data/demo";
import type { DemoAction, DemoState } from "../data/demoMachine";
import { useLocale } from "../i18n";

const capturedItems = [
  { id: "cap-01", source: "Chrome", method: "GET", target: "staging.example.test/", state: "attached" },
  { id: "cap-02", source: "Burp", method: "OPTIONS", target: "api.example.test/v1/session", state: "attached" },
  { id: "cap-03", source: "Chrome", method: "GET", target: "staging.example.test/login", state: "queued" },
];

export function ConnectorsView({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const { locale, t, label } = useLocale();
  const { connectorRecords } = getDemoData(locale);
  const [observation, setObservation] = useState("login-form");
  const [selectedConnector, setSelectedConnector] = useState("browser");
  const connector = connectorRecords.find((item) => item.id === selectedConnector) ?? connectorRecords[0];

  return (
    <div className="screen-stack connectors-screen">
      <header className="screen-heading">
        <div>
          <DemoStamp />
          <h1>{t("Bring browsers and external capture into one evidence chain.", "把浏览器和外部捕获接入同一条证据链。")}</h1>
          <p>{t("Managed Browser is owned by the Runner. Chrome and Burp are capture/control clients only; they contain no Agent runtime.", "Managed Browser 由 Runner 拥有。Chrome 与 Burp 只是 capture/control client，不包含 Agent runtime。")}</p>
        </div>
        <div className="connector-counter">
          <span>{t("CAPTURED ARTIFACTS", "已捕获 ARTIFACT")}</span>
          <strong>03</strong>
          <small>{t("synthetic / current Run", "合成数据 / 当前 Run")}</small>
        </div>
      </header>

      <section className="connector-flow pixel-panel" aria-label={t("Connector data flow", "连接器数据流")}>
        <div className="flow-endpoint"><PixelIcon name="traffic" /><strong>Chrome / Burp</strong><span>{t("capture client", "捕获客户端")}</span></div>
        <span className="flow-link"><b>{t("HTTP Artifact", "HTTP 工件")}</b></span>
        <div className="flow-endpoint flow-control-plane"><PixelIcon name="shield" /><strong>Control Plane</strong><span>{t("identity + policy", "身份 + 策略")}</span></div>
        <span className="flow-link"><b>{t("Run event", "Run 事件")}</b></span>
        <div className="flow-endpoint"><PixelIcon name="evidence" /><strong>{t("Run + Evidence", "Run + 证据")}</strong><span>{t("durable owner", "持久所有者")}</span></div>
      </section>

      <section className="connector-grid">
        <article className="pixel-panel managed-browser-panel">
          <PanelHeading
            icon="server"
            title={t("Managed Browser session", "Managed Browser 会话")}
            detail={t("Stable element references, sanitized visible text, network summaries, and Artifact IDs.", "稳定元素引用、脱敏可见文本、网络摘要与 Artifact ID。")}
            action={<span className={`owner-badge owner-${state.browserOwner}`}>OWNER / {label(state.browserOwner).toUpperCase()}</span>}
          />
          <div className="browser-session">
            <div className="browser-address">
              <PixelIcon name="lock" />
              <code>https://staging.example.test/login</code>
              <span className="state-label state-running">{t("active", "活动")}</span>
            </div>
            <div className="browser-observation">
              <div className="observation-map">
                <span className="page-zone zone-header">header / public</span>
                <button type="button" className={observation === "login-form" ? "is-selected" : ""} onClick={() => setObservation("login-form")}>
                  <strong>e12 / form</strong><span>2 inputs + 1 submit</span>
                </button>
                <button type="button" className={observation === "support-link" ? "is-selected" : ""} onClick={() => setObservation("support-link")}>
                  <strong>e18 / link</strong><span>Support policy</span>
                </button>
                <span className="page-zone zone-footer">footer / build hidden</span>
              </div>
              <aside>
                <strong>{t("Sanitized observation", "脱敏观察")}</strong>
                <dl>
                  <div><dt>Ref</dt><dd>{observation === "login-form" ? "e12" : "e18"}</dd></div>
                  <div><dt>{t("Role", "角色")}</dt><dd>{observation === "login-form" ? "form" : "link"}</dd></div>
                  <div><dt>{t("Visible text", "可见文本")}</dt><dd>{observation === "login-form" ? "Sign in" : "Support"}</dd></div>
                  <div><dt>DOM</dt><dd>{t("not returned", "不返回")}</dd></div>
                  <div><dt>{t("Profile path", "Profile 路径")}</dt><dd>{t("hidden", "已隐藏")}</dd></div>
                  <div><dt>CDP endpoint</dt><dd>{t("hidden", "已隐藏")}</dd></div>
                </dl>
              </aside>
            </div>
          </div>
          <div className="browser-actions">
            <button className="primary-button" type="button" onClick={() => dispatch({ type: "browser-owner" })}>
              <PixelIcon name={state.browserOwner === "agent" ? "terminal" : "run"} />
              {state.browserOwner === "agent" ? t("Operator takeover", "Operator 接管") : t("Release to Agent", "归还 Agent")}
            </button>
            <button className="secondary-button" type="button" disabled={state.browserOwner === "operator"}>{t("Agent click e12", "Agent 点击 e12")}</button>
            <button className="secondary-button" type="button">{t("Observe", "观察")}</button>
          </div>
          {state.browserOwner === "operator" ? (
            <div className="takeover-note"><PixelIcon name="shield" />{t("Agent writes are rejected while sanitized observation remains available.", "Agent 写操作已拒绝，脱敏观察仍然可用。")}</div>
          ) : null}
        </article>

        <aside className="connector-catalog pixel-panel">
          <PanelHeading icon="traffic" title={t("Connector catalog", "连接器目录")} detail={t("Select a channel to inspect its product boundary.", "选择一个通道查看其产品边界。")} />
          <div className="connector-list">
            {connectorRecords.map((item) => (
              <button key={item.id} type="button" className={connector.id === item.id ? "is-selected" : ""} onClick={() => setSelectedConnector(item.id)}>
                <PixelIcon name={item.id === "browser" ? "server" : "traffic"} />
                <span><strong>{item.name}</strong><small>{item.channel}</small></span>
                <span className={`state-label state-${item.status === "connected" ? "completed" : "running"}`}>{label(item.status)}</span>
              </button>
            ))}
          </div>
          <article className="connector-detail">
            <strong>{connector.name}</strong>
            <p>{connector.detail}</p>
            <dl>
              <div><dt>{t("Runtime", "运行时")}</dt><dd>{connector.id === "browser" ? t("Runner owned", "Runner 所有") : t("none in connector", "连接器内无 runtime")}</dd></div>
              <div><dt>{t("Auth", "认证")}</dt><dd>{t("local operator policy", "本地 Operator 策略")}</dd></div>
              <div><dt>{t("Payload", "载荷")}</dt><dd>{t("immutable Artifact", "不可变 Artifact")}</dd></div>
              <div><dt>{t("Updates", "更新")}</dt><dd>{t("SSE Run events", "SSE Run 事件")}</dd></div>
            </dl>
          </article>
        </aside>
      </section>

      <section className="pixel-panel capture-queue">
        <PanelHeading
          icon="evidence"
          title={t("Capture-to-Artifact queue", "Capture 到 Artifact 队列")}
          detail={t("Full exchanges become Artifacts; the Traffic workspace reads sanitized metadata only.", "完整交换进入 Artifact，Traffic 工作区只读取脱敏元数据。")}
          action={<span className="readonly-badge"><PixelIcon name="lock" />{t("NO REPLAY", "禁止重放")}</span>}
        />
        <div className="capture-rows">
          {capturedItems.map((item) => (
            <div key={item.id}>
              <code>{item.id}</code><span>{item.source}</span><strong>{item.method}</strong><span>{item.target}</span>
              <span className={`state-label state-${item.state === "attached" ? "completed" : "pending"}`}>{label(item.state)}</span>
              <code>artifact://runs/demo/http/{item.id}</code>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
