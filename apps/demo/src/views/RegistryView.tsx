import { useMemo, useState, type Dispatch } from "react";

import { PixelIcon } from "../components/PixelIcon";
import { DemoStamp, PanelHeading } from "../components/Ui";
import { getDemoData } from "../data/demo";
import type { DemoAction, DemoState, RegistryTab } from "../data/demoMachine";
import { useLocale } from "../i18n";

export function RegistryView({
  state,
  dispatch,
}: {
  state: DemoState;
  dispatch: Dispatch<DemoAction>;
}) {
  const { t } = useLocale();
  const registryTabs: Array<{
    id: RegistryTab;
    label: string;
    icon: "node" | "terminal" | "server";
    detail: string;
  }> = [
    { id: "nodes", label: t("Nodes", "节点"), icon: "node", detail: t("Effect owners and capability boundaries", "效果所有者与能力边界") },
    { id: "tools", label: t("Tools", "工具"), icon: "terminal", detail: t("Discoverable tools and approval levels", "可发现工具与审批级别") },
    { id: "models", label: t("Models", "模型"), icon: "server", detail: t("Reasoning channels and write-only credentials", "推理通道与写入式凭据") },
  ];

  return (
    <div className="screen-stack registry-screen">
      <header className="screen-heading">
        <div>
          <DemoStamp />
          <h1>{t("Confirm runtime resources before the Agent acts.", "先确认运行资源，再让 Agent 行动。")}</h1>
          <p>{t("Nodes, Tools, and Models are managed product objects. The Demo shows metadata only and never reads a real host, binary, or credential.", "Node、Tool 与 Model 都是可管理产品对象。Demo 只展示元数据，不读取真实主机、二进制或凭据。")}</p>
        </div>
        <div className="registry-generation">
          <span>{t("REGISTRY GENERATION", "注册表世代")}</span><strong>42</strong><small>{t("source digest / demo fixture", "来源摘要 / 演示样例")}</small>
        </div>
      </header>

      <nav className="registry-tabs" aria-label={t("Runtime resource categories", "运行资源类别")}>
        {registryTabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={state.registryTab === tab.id ? "is-active" : ""}
            aria-label={tab.label}
            aria-current={state.registryTab === tab.id ? "page" : undefined}
            onClick={() => dispatch({ type: "registry", tab: tab.id })}
          >
            <PixelIcon name={tab.icon} />
            <span><strong>{tab.label}</strong><small>{tab.detail}</small></span>
          </button>
        ))}
      </nav>

      <section className="registry-surface pixel-panel">
        {state.registryTab === "nodes" ? <NodesRegistry /> : null}
        {state.registryTab === "tools" ? <ToolsRegistry /> : null}
        {state.registryTab === "models" ? <ModelsRegistry /> : null}
      </section>
    </div>
  );
}

function NodesRegistry() {
  const { locale, t, label } = useLocale();
  const { nodes } = getDemoData(locale);
  const [selectedId, setSelectedId] = useState(nodes[0].id);
  const selected = nodes.find((node) => node.id === selectedId) ?? nodes[0];

  return (
    <div className="node-registry">
      <div className="registry-main">
        <PanelHeading
          icon="node"
          title={t("Runner node inventory", "Runner 节点清单")}
          detail={t("Platform, capability, heartbeat, and containment determine which effects may be admitted safely.", "Platform、能力、心跳与 containment 共同决定哪些效果可以被安全接纳。")}
          action={<span className="readonly-badge"><PixelIcon name="lock" />{t("DEMO READ ONLY", "演示只读")}</span>}
        />
        <div className="node-grid">
          {nodes.map((node) => (
            <button key={node.id} type="button" className={`node-card node-${node.status}${selected.id === node.id ? " is-selected" : ""}`} onClick={() => setSelectedId(node.id)}>
              <header><span className="node-pixel"><PixelIcon name="server" /></span><span className={`state-label state-${node.status}`}>{label(node.status)}</span></header>
              <strong>{node.name}</strong><code>{node.id}</code>
              <dl>
                <div><dt>{t("PLATFORM", "平台")}</dt><dd>{node.platform}</dd></div>
                <div><dt>{t("CONTAINMENT", "隔离边界")}</dt><dd>{node.containment}</dd></div>
                <div><dt>{t("HEARTBEAT", "心跳")}</dt><dd>{node.heartbeat}</dd></div>
              </dl>
            </button>
          ))}
        </div>
      </div>
      <aside className="node-inspector">
        <h3><PixelIcon name="shield" />{t("Capability manifest", "能力清单")}</h3>
        <strong>{selected.name}</strong><code>{selected.id}</code>
        <div className="capability-list">{selected.capabilities.map((capability) => <span key={capability}>{capability}</span>)}</div>
        <dl>
          <div><dt>{t("Status", "状态")}</dt><dd>{label(selected.status)}</dd></div>
          <div><dt>{t("Platform", "平台")}</dt><dd>{selected.platform}</dd></div>
          <div><dt>{t("Containment", "隔离边界")}</dt><dd>{selected.containment}</dd></div>
          <div><dt>{t("Active executions", "活动执行")}</dt><dd>{selected.status === "online" ? t("1 synthetic", "1 个合成执行") : label("unavailable")}</dd></div>
          <div><dt>{t("Runner version", "Runner 版本")}</dt><dd>demo-fixture</dd></div>
        </dl>
        <div className={`integrity-note${selected.containment === "cgroup v2" ? " integrity-ok" : " integrity-warning"}`}>
          <PixelIcon name={selected.containment === "cgroup v2" ? "check" : "warning"} />
          {selected.containment === "cgroup v2"
            ? t("This fixture has a delegated cgroup v2 whole-tree stop proof boundary.", "该样例节点具备 delegated cgroup v2 whole-tree stop 证明边界。")
            : t("This node does not claim stop proof equivalent to Linux cgroup v2.", "此节点不宣传与 Linux cgroup v2 等价的停止证明。")}
        </div>
      </aside>
    </div>
  );
}

function ToolsRegistry() {
  const { locale, t, label } = useLocale();
  const { tools } = getDemoData(locale);
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState(tools[0].name);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return tools;
    return tools.filter((tool) => [tool.name, tool.capability, tool.source, tool.approval].some((value) => value.toLowerCase().includes(needle)));
  }, [query, tools]);
  const selected = tools.find((tool) => tool.name === selectedName) ?? tools[0];

  return (
    <div className="tool-registry">
      <div className="registry-main">
        <PanelHeading
          icon="terminal"
          title={t("Dynamic tool registry", "动态工具注册表")}
          detail={t("The Agent sees a minimal resident set first, then discovers tools progressively by capability and synonym.", "Agent 首先看到最小 resident set，再按能力与同义词逐步发现工具。")}
          action={<span className="generation-badge">GEN / 42</span>}
        />
        <label className="registry-search">
          <span>{t("Search tools, capabilities, or approval levels", "搜索工具、能力或审批级别")}</span>
          <div><PixelIcon name="target" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("For example: http, browser, manual", "例如：http、browser、manual")} /></div>
        </label>
        <div className="table-wrap">
          <table className="data-table tool-table">
            <thead><tr><th>{t("Tool", "工具")}</th><th>{t("Capability", "能力")}</th><th>{t("Source", "来源")}</th><th>{t("Approval", "审批")}</th><th>{t("Availability", "可用性")}</th></tr></thead>
            <tbody>
              {filtered.map((tool) => (
                <tr key={tool.name} className={selected.name === tool.name ? "is-selected" : ""}>
                  <td><button type="button" onClick={() => setSelectedName(tool.name)}>{tool.name}</button></td>
                  <td>{tool.capability}</td><td>{tool.source}</td>
                  <td><span className={`risk-label risk-${tool.approval === "manual" ? "approval" : tool.approval === "read" ? "read" : "blocked"}`}>{label(tool.approval)}</span></td>
                  <td><span className={`state-label state-${tool.availability}`}>{label(tool.availability)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length ? <div className="registry-empty"><PixelIcon name="target" /><strong>{t("No matching tools", "没有匹配工具")}</strong><span>{t("Change the query or browse the full Registry generation 42 catalog.", "修改搜索词，或查看 Registry generation 42 的完整目录。")}</span></div> : null}
      </div>
      <aside className="tool-inspector">
        <h3><PixelIcon name="terminal" />{t("Tool detail", "工具详情")}</h3>
        <strong>{selected.name}</strong><span className={`state-label state-${selected.availability}`}>{label(selected.availability)}</span>
        <dl>
          <div><dt>{t("Capability", "能力")}</dt><dd>{selected.capability}</dd></div>
          <div><dt>{t("Source", "来源")}</dt><dd>{selected.source}</dd></div>
          <div><dt>{t("Approval level", "审批级别")}</dt><dd>{label(selected.approval)}</dd></div>
          <div><dt>{t("Executor", "执行器")}</dt><dd>{t("registered process", "已注册进程")}</dd></div>
          <div><dt>{t("Timeout", "超时")}</dt><dd>{t("bounded / fixture", "有界 / 样例")}</dd></div>
          <div><dt>{t("Output", "输出")}</dt><dd>{t("summary + Artifact", "摘要 + Artifact")}</dd></div>
        </dl>
        <p>{t("RiftX manages registration metadata but never installs penetration-testing tools automatically.", "RiftX 管理注册信息，但不会自动安装渗透测试工具。")}</p>
        <button className="secondary-button full-width" type="button" disabled>{t("Demo mode is read only", "演示模式不可编辑")}</button>
      </aside>
    </div>
  );
}

function ModelsRegistry() {
  const { locale, t, label } = useLocale();
  const { modelProfiles } = getDemoData(locale);
  const [defaultName, setDefaultName] = useState(modelProfiles.find((profile) => profile.isDefault)?.name ?? modelProfiles[0].name);
  const [selectedName, setSelectedName] = useState(defaultName);
  const selected = modelProfiles.find((profile) => profile.name === selectedName) ?? modelProfiles[0];

  return (
    <div className="model-registry">
      <div className="registry-main">
        <PanelHeading
          icon="server"
          title={t("Model profiles", "模型 Profiles")}
          detail={t("Provider, request mode, timeout, and retry are metadata; credentials remain write-only.", "Provider、请求模式、超时与重试是元数据，凭据始终 write-only。")}
          action={<span className="readonly-badge"><PixelIcon name="lock" />{t("NO KEYS LOADED", "未加载密钥")}</span>}
        />
        <div className="model-list">
          {modelProfiles.map((profile) => (
            <button key={profile.name} type="button" className={selected.name === profile.name ? "is-selected" : ""} onClick={() => setSelectedName(profile.name)}>
              <span className="model-icon"><PixelIcon name="server" /></span>
              <span className="model-copy"><strong>{profile.name}</strong><small>{profile.provider} / {profile.mode}</small><code>{profile.model}</code></span>
              <span className={`credential-state ${profile.credential === "configured" ? "is-ready" : ""}`}>{label(profile.credential)}</span>
              {defaultName === profile.name ? <span className="default-flag">{t("DEFAULT", "默认")}</span> : null}
            </button>
          ))}
        </div>
      </div>
      <aside className="model-inspector">
        <h3><PixelIcon name="lock" />{t("Profile contract", "Profile 契约")}</h3>
        <strong>{selected.name}</strong>
        <dl>
          <div><dt>{t("Provider", "提供商")}</dt><dd>{selected.provider}</dd></div><div><dt>{t("Model", "模型")}</dt><dd>{selected.model}</dd></div>
          <div><dt>{t("Request mode", "请求模式")}</dt><dd>{selected.mode}</dd></div>
          <div><dt>{t("Credential", "凭据")}</dt><dd>{label(selected.credential)}</dd></div>
          <div><dt>{t("Timeout", "超时")}</dt><dd>{t("120 seconds / synthetic", "120 秒 / 合成")}</dd></div>
          <div><dt>{t("Max retries", "最大重试")}</dt><dd>{t("2 / synthetic", "2 / 合成")}</dd></div>
        </dl>
        <div className="secret-boundary"><PixelIcon name="shield" /><p>{t("The API Key is never returned by the Control Plane. Changing Provider or Base URL never reuses an old stored key.", "API Key 永不从 Control Plane 返回。改变 Provider 或 Base URL 时不会沿用旧的 stored key。")}</p></div>
        <button className="primary-button full-width" type="button" onClick={() => setDefaultName(selected.name)} disabled={defaultName === selected.name}>
          <PixelIcon name="check" />{defaultName === selected.name ? t("Current default Profile", "当前默认 Profile") : t("Set as demo default", "设为演示默认")}
        </button>
      </aside>
    </div>
  );
}
