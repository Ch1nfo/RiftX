# RiftX

RiftX is a Win/Mac desktop app that wraps a fixed open-source agent runtime with
a security-tool directory, your own LLM profiles, and three engagement modes.

RiftX 以开源 Agent Runtime 为底座：指定文件夹放安全工具即可调用，自配大模型，用
三种模式整合红队 / 渗透工作流。边界主要靠提示词与分档人工审批，不做重型 OS 隔离平台。

> 仅用于你有权测试的目标。展示面使用 RiftX 品牌；不提供上游账号登录。

## 产品方向（v0.8 → 1.0）

三种模式从紧到松：

- **RedTeam**：护网等场景的 AI 攻击队；危险命令 + 高风险工具要批。
- **Pentest**：企业内指定目标巡检；多数可跑，危险命令要批。
- **Auto**：靶场全自动推进；启动前一次风险确认，运行中少打断，可 Kill Switch。

形态：macOS / Windows 桌面应用；Linux 以 CLI 为正式入口（见 1.0 计划）。自有 UI，不照抄上游 Agent 产品界面。
后台 `riftxd` 经本机 IPC（UDS / Named Pipe）通信。API Key 进系统钥匙串。

详细说明见 [RiftX-项目说明.md](./RiftX-项目说明.md)、
[RiftX-技术实现方案.md](./RiftX-技术实现方案.md) 与
[1.0 计划.md](./1.0%20计划.md)。

## 当前实现

v0.8 产品主路径（模式、分档审批、设置、Auto 启动确认、Guard 旁路）已落地；正在按
[1.0 计划.md](./1.0%20计划.md) 补齐双协议 LLM、Auto 多 turn、三平台发布与验收。

- 内嵌 Agent Runtime（仅 API Key，无上游账号登录）。
- `riftxd`、本地 IPC、Operator CLI。
- Tauri 桌面：任务列表、对话、中断、命令审批、报告、托盘 Pause/Kill Switch。
- Tools / Skills 目录扫描、doctor、PATH 注入。
- LLM Profile + 钥匙串；RedTeam / Pentest / Auto 与分档审批。
- Desktop 可编辑 Tools Directory 与 LLM Profile（写回 `riftx.toml`）。
- Auto：启动确认短语、Lab + 到期校验、五分钟无进展提示。
- Markdown / JSON 报告；macOS 调试 `.app` 验证过主流程。

仓库中仍可能存在 Guard、全库加密等**遗留**实现；**不作为**产品主路径或 1.0 发布门槛。

Linux 正式 CLI 命令同时提供人类可读输出和 `--json`；`events --json` 是持续输出的
newline-delimited JSON 事件流。稳定 JSON 字段和退出码由
`codex-rs/riftx-cli/fixtures/cli-json-v1.schema.json` 固定。

当前剩余发布门槛（详见 1.0 计划）主要是远端 required CI 结果、受保护 provider live
smoke、正式签名安装包和干净系统真人安装验收。不预装任何安全工具。

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
schema_version = 1
capabilities = ["network.discovery"]
risk = "low"
version_args = ["--version"]
health_check_args = ["--help"]
```

`schema_version` 为必填字段；当前仅支持 `1`。缺失或不受支持的版本会被拒绝并在
Tools Doctor 中报告诊断，不会作为受管理工具元数据使用。

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

采集拒绝绝对路径、路径穿越、符号链接和目录。文件按明文 SHA-256 内容寻址，并使用
Engagement 数据密钥以 64 KiB 分块认证加密保存，受 `[artifacts].max_bytes_per_engagement`
限制。导出会先完整解密并重新校验大小和哈希，再从最小权限、关闭即删除的临时文件
流式返回；认证或完整性失败时不会返回部分内容。

## LLM 配置

RiftX 不提供账号登录、浏览器授权或设备码登录。模型配置位于
[riftx.toml](./riftx.toml)，默认从操作系统安全存储读取 API Key：

```toml
[llm]
default_profile = "openai"

[llm.profiles.openai]
model = "gpt-5.2"
base_url = "https://api.openai.com/v1"
api_key = { source = "keyring", credential = "openai" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000
```

确定性测试和本地 mock 可显式使用环境变量来源：

```toml
api_key = { source = "environment", variable = "RIFTX_LLM_API_KEY" }
```

最多可配置 16 个 LLM Profile。`riftxd` 为每个 Profile 创建独立的 App Server 和
`runtime/profiles/<name>` runtime home。Desktop 从系统安全存储读取所有 Keyring
Profile 的 API Key，并通过继承的 stdin 发送一次性、长度前缀的 Profile-Key 内存帧；
独立启动的 `riftxd` 仍可直接读取系统安全存储。环境变量来源会被强制从 Agent 工具
进程环境中排除；每个 Runtime 都排除所有 Profile 的密钥变量，防止跨 Profile 泄漏。
Desktop 设置页可逐 Profile 管理 Keyring 凭据，新建 Engagement 时可选择配置中的任意
Profile。Keyring 来源的 API Key 不写入 TOML、SQLite、审计、命令行、环境变量或普通
日志。

Linux headless 环境没有可用 Secret Service 时，可让受信任的密码管理器或秘密提供程序
向 stdin 输出一次性的 JSON 对象；不要把真实 Key 写进命令行或 shell 历史：

```bash
secure-profile-key-json | riftxd --config riftx.toml --llm-api-key-stdin-json
```

stdin 格式为 `{"profile-name":"api-key"}`，总大小上限为 2 MiB，并且只能引用配置中
使用 Keyring 来源的 Profile。daemon 读取后会清零输入缓冲；未提供 Key 的 Profile 保持
`unconfigured`，Runtime 仍在首次使用时 lazy 初始化。

评估目标使用的密码、Token、私钥等凭据与 LLM API Key 分开管理。CLI/Desktop 只通过
本机 IPC 向 `riftxd` 提交秘密，`riftxd` 负责写入和读取 Keychain、Credential Manager
或 Secret Service；凭据元数据、SQLite、审计、报告和 Agent 上下文只保留引用及
`configured` 状态。未成功写入系统安全存储的引用不能创建 Credential Grant。

## 开发运行

```bash
conda run -n agent sh -lc \
  'cd codex-rs && cargo build -p codex-riftx-gateway -p codex-riftx-cli'
```

启动桌面端时会自动构建并运行随应用携带的 `riftxd` sidecar。若尚未配置 Key，右上角
设置可以将其保存到系统安全存储；保存后 sidecar 自动启动，替换 Key 时自动重启，删除
Key 时自动停止：

```bash
conda run -n agent pnpm install
RIFTX_CONFIG="$PWD/riftx.toml" \
  conda run --no-capture-output -n agent \
  pnpm --filter @riftx/desktop tauri dev
```

仅在无桌面的 CLI/TUI 开发或显式测试外部 daemon 时，才需要单独启动：

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
`apps/desktop/src-tauri/target/debug/bundle/macos/RiftX.app`，其中已经包含当前平台的
`riftxd` sidecar。正式 DMG、签名和 notarization 在发布阶段实现。

另一个终端通过同一 `riftx.toml` 中的本地 IPC 端点连接：

```bash
./codex-rs/target/debug/riftx create \
  --name "Authorized Lab" \
  --objective "验证授权范围内是否存在到达域控的攻击路径" \
  --success-criterion "到达域控的每一跳都有可复现证据" \
  --structured-criterion '{"id":"reach-dc","description":"验证到达域控的攻击路径","predicate":{"type":"attackPath","destinationRole":"domainController","accessLevel":"domainAdminEquivalent","minimumConfidenceBasisPoints":9000,"reproducibleEvidence":true}}' \
  --entry-point "10.10.20.15" \
  --cidr "10.10.0.0/16" \
  --mode pentest \
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

- [1.0 执行计划（正式发布合同）](./1.0%20计划.md)
- [项目说明（v0.8 产品基线）](./RiftX-项目说明.md)
- [产品与技术实施计划（v0.8）](./RiftX-技术实现方案.md)
- [1.0 M0 基线记录](./docs/1.0-baseline.md)
- [Changelog](./CHANGELOG.md)
- [v0.7 架构决策（历史，已被 v0.8 收敛）](./architecture/adr/0001-v0.7-local-native-execution.md)
- [上游版本锁](./codex-upstream.lock)
- [上游同步约定](./UPSTREAM.md)

## Source Attribution

RiftX includes Apache-2.0 licensed source from
[openai/codex](https://github.com/openai/codex), pinned by `codex-upstream.lock`. The upstream
name is retained only for source compatibility, licensing, and attribution. RiftX exposes its own
product identity and API-key-only model configuration, and is not an official OpenAI product.
