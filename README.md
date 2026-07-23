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
- 跨平台 Tools Directory 扫描、可选元数据、SHA-256 快照、PATH 注入和
  `riftx tools doctor`。
- 单一 Skills Directory、独占 Runtime 根目录、内容快照和 `riftx skills doctor`。

尚未完成 Desktop、Linux TUI、工具执行审计闭环、加密案件存储、
三平台 Guard、Auto planner loop，以及完整的 typed IPC 业务消息。

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
