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
- Tauri 2 + React 桌面壳，已具备 Engagement 列表、创建、选择、Native 指令提交、
  interrupt、实时对话事件、单次命令审批和状态报告工作台；前端只通过 Rust bridge
  访问本地 IPC。
- IPC v3 协议协商、有大小上限的 SSE 解码、断线重连，以及按 Engagement 查询和决策
  待审批请求。
- 可分页恢复的持久对话历史；只保存操作员消息、最终 Agent 回复和计划，不保存推理、
  token 增量或原始 App Server 事件。
- 跨平台 Tools Directory 扫描、可选元数据、SHA-256 快照、PATH 注入和
  `riftx tools doctor`。
- 单一 Skills Directory、独占 Runtime 根目录、内容快照和 `riftx skills doctor`。
- 本机命令 Execution 审计，包括脱敏 argv、resolved path、工具/快照哈希、
  stdout/stderr/stdin 哈希、退出状态和 interrupt 恢复。
- workspace artifact 的内容寻址采集、容量限制、哈希清单和受控流式导出。
- macOS、Windows、Linux 共用的确定性 Native daemon 端到端验收；macOS 调试
  `.app` 已完成本地实际运行验证。

尚未完成桌面设置、完整审批矩阵、通知、Linux TUI、加密案件存储、三平台 Guard、
Auto planner loop，以及完整的 typed IPC 业务消息。

项目不包含容器执行后端、固定渗透工具、固定 Recon/Exploit/Report Agent，也不预装任何
安全工具。

## 本机工具

`[tools].directories` 为空时使用平台默认目录，也可以配置一个或多个自定义目录：

```toml
[tools]
directories = ["/absolute/path/to/team-tools"]
extra_paths = []
```

RiftX 扫描根目录、一级子目录及其 `bin/`，不递归遍历、不跟随符号链接。发现的可执行文件
按 Tools Directory、`extra_paths`、系统 PATH 的顺序进入 Agent 任务 PATH。初始安装不会
下载或预装任何安全工具。

可执行文件旁可以放置可选的 `<filename>.riftx.toml`：

```toml
capabilities = ["network.discovery"]
risk = "low"
version_args = ["--version"]
health_check_args = ["--help"]
```

检查当前启动时工具快照：

```bash
./codex-rs/target/debug/riftx tools doctor
./codex-rs/target/debug/riftx tools doctor --json
```

## 本机 Skill

RiftX 只加载自己的单一 Skills Directory，不会混入 `~/.agents/skills`、项目 Skill、
插件 Skill 或 Agent Runtime 的内置 Skill。默认目录为：

- macOS：`~/Library/Application Support/RiftX/skills/`
- Windows：`%LOCALAPPDATA%\RiftX\skills\`
- Linux：`~/.local/share/riftx/skills/`

每个 Skill 放在独立子目录中，入口文件为 `SKILL.md`。也可以配置自定义目录：

```toml
[skills]
directory = "/absolute/path/to/team-skills"
```

`riftxd` 启动时固定 Skill 元数据和目录内容哈希快照；RiftX 不预装任何 Skill。检查当前
启动快照：

```bash
./codex-rs/target/debug/riftx skills doctor
./codex-rs/target/debug/riftx skills doctor --json
```

## Artifact

每个 engagement workspace 的 `artifacts/` 目录用于保存需要长期保留的证据文件。turn
结束后 `riftxd` 会自动扫描该目录；操作者也可以显式采集 workspace 内的任意相对路径：

```bash
./codex-rs/target/debug/riftx artifacts capture <engagement-id> artifacts/result.json
./codex-rs/target/debug/riftx artifacts list <engagement-id>
./codex-rs/target/debug/riftx artifacts export \
  <engagement-id> <artifact-id> --output result.json
```

采集拒绝绝对路径、路径穿越、符号链接和目录。文件按 SHA-256 内容寻址保存，并受
`[artifacts].max_bytes_per_engagement` 限制；导出前会重新校验大小和哈希。当前开发基线
尚未实现 artifact 加密。

## LLM 配置

RiftX 不提供账号登录、浏览器授权或设备码登录。模型配置位于
[riftx.toml](./riftx.toml)，默认从操作系统安全存储读取 API Key：

```toml
[llm]
model = "gpt-5.2"
base_url = "https://api.openai.com/v1"
api_key = { source = "keyring", profile = "default" }
```

确定性测试和本地 mock 可显式使用环境变量来源：

```toml
api_key = { source = "environment", variable = "RIFTX_LLM_API_KEY" }
```

守护进程启动时读取 API Key，并只通过内存认证对象交给模型客户端。环境变量来源会被
强制从 Agent 工具进程环境中排除；API Key 不写入 TOML、SQLite、审计、命令行或普通日志。

## 开发运行

```bash
conda run -n agent sh -lc \
  'cd codex-rs && cargo build -p codex-riftx-gateway -p codex-riftx-cli'
```

先启动桌面端。即使 daemon 尚未运行，也可以通过右上角设置按钮将 API Key 保存到
系统安全存储：

```bash
conda run -n agent pnpm install
RIFTX_CONFIG="$PWD/riftx.toml" \
  conda run --no-capture-output -n agent \
  pnpm --filter @riftx/desktop tauri dev
```

另一个终端启动 daemon；保存或删除 API Key 后需要重启 daemon：

```bash
./codex-rs/target/debug/riftxd --config riftx.toml
```

仅当 `llm.api_key.source = "environment"` 时，启动前设置对应变量：

```bash
export RIFTX_LLM_API_KEY="<your-api-key>"
```

macOS 调试 `.app` 构建：

```bash
conda run -n agent pnpm --filter @riftx/desktop \
  tauri build --debug --bundles app
```

产物位于
`apps/desktop/src-tauri/target/debug/bundle/macos/RiftX.app`。正式 DMG、签名和
notarization 在发布阶段实现。

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
