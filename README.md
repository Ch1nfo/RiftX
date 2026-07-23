# RiftX

RiftX is a local-first, conversation-driven AI red-team workbench for explicitly
authorized security assessments.

RiftX 是面向明确授权安全测试的本机 AI 红队工作台。操作者通过对话定义目标、入口、
授权范围和成功条件；主 Agent 可以在 Scope 内持续发现资产、验证攻击路径、保存证据并
生成报告，而不是被限制在单一 IP 或域名。

> RiftX 仅用于具有明确书面授权、有效时间窗和清晰 Scope 的测试。Auto Mode 仅允许
> 用于 Lab 环境，不得用于生产环境。

## 产品方向

RiftX 采用三种显式模式：

- **Native Mode**：直接使用本机 shell、系统 PATH 和 Tools Directory，面向专业红队。
- **Hardened Mode**：仍在本机执行，由各平台 `riftx-guard` 强制边界；Guard 不可用时拒绝启动。
- **Auto Mode**：在 Lab 授权、有效期、Scope、Guard、审计、快照和 Kill Switch 全部满足后，无人监督地持续推进目标。

首发形态为 macOS Apple Silicon DMG、Windows x86_64 EXE，以及 Linux x86_64 CLI/TUI。
桌面端采用对话优先的三栏工作台；后台 `riftxd` 通过 Unix Domain Socket 或 Windows
Named Pipe 提供本地服务。

## 当前实现

当前代码是 v0.7 的早期开发基线，已经具备：

- API-Key-only 的内嵌 Agent Runtime，不读取账号登录状态。
- 本机 engagement workspace 与单一持续主 Agent。
- Objective、Scope、Policy Revision、Approval 和三模式领域约束。
- 多资产 Target State、Evidence 链、SQLite 状态、JSONL 审计和 Markdown/JSON 报告。
- `riftxd` 本地 IPC API 和 Operator CLI。

尚未完成 Desktop、Linux TUI、Tools/Skills Directory、加密案件存储、三平台 Guard、
Auto planner loop，以及完整的 typed IPC 业务消息。

项目不包含容器执行后端、固定渗透工具、固定 Recon/Exploit/Report Agent，也不预装任何
安全工具。

## 本机工具

正式实现将扫描用户配置的 Tools Directory，并把其中可执行文件加入任务 PATH。一个工具
只要能由当前用户在本机运行，Agent 原则上就能通过 shell 使用它。可选元数据用于补充名称、
平台、风险等级和健康检查，但不会要求为每个工具开发适配器。

## LLM 配置

RiftX 不提供账号登录、浏览器授权或设备码登录。模型配置位于
[riftx.toml](./riftx.toml)，API Key 只从指定环境变量读取：

```toml
[llm]
model = "gpt-5.2"
base_url = "https://api.openai.com/v1"
api_key_env = "RIFTX_LLM_API_KEY"
```

```bash
export RIFTX_LLM_API_KEY="<your-api-key>"
```

API Key 不写入 TOML、SQLite、审计、命令行或普通日志。

## 开发运行

```bash
conda run -n agent sh -lc \
  'cd codex-rs && cargo build -p codex-riftx-gateway -p codex-riftx-cli'

export RIFTX_LLM_API_KEY="<your-api-key>"
./codex-rs/target/debug/riftxd --config riftx.toml
```

另一个终端通过同一 `riftx.toml` 中的本地 IPC 端点连接：

```bash
./codex-rs/target/debug/riftx create \
  --name "Authorized Lab" \
  --objective "验证授权范围内是否存在到达域控的攻击路径" \
  --success-criterion "到达域控的每一跳都有可复现证据" \
  --structured-criterion '{"id":"reach-dc","description":"验证到达域控的攻击路径","predicate":{"type":"attackPath","destinationRole":"domainController","accessLevel":"domainAdminEquivalent","minimumConfidenceBasisPoints":9000,"reproducibleEvidence":true}}' \
  --entry-point "10.10.20.15" \
  --cidr "10.10.0.0/16" \
  --mode native \
  --environment lab \
  --capability network.discovery \
  --capability attack_path.analysis \
  --identity-selector '{"domain":"lab.example"}'
```

`create` 返回 engagement ID。随后运行：

```bash
./codex-rs/target/debug/riftx activate <engagement-id>
./codex-rs/target/debug/riftx turn <engagement-id> "分析目标并执行下一步授权测试"
./codex-rs/target/debug/riftx events <engagement-id>
```

## 文档

- [产品与技术实施计划](./RiftX-技术实现方案.md)
- [当前项目说明](./RiftX-项目说明.md)
- [v0.7 架构决策](./architecture/adr/0001-v0.7-local-native-execution.md)
- [上游版本锁](./codex-upstream.lock)
- [上游同步约定](./UPSTREAM.md)

## Source Attribution

RiftX includes Apache-2.0 licensed source from
[openai/codex](https://github.com/openai/codex), pinned by `codex-upstream.lock`. The upstream
name is retained only for source compatibility, licensing, and attribution. RiftX exposes its own
product identity and API-key-only model configuration, and is not an official OpenAI product.
