# RiftX 项目说明

> 当前版本：v0.8 产品基线  
> 文档日期：2026-07-25  
> 配套实施计划：[RiftX-技术实现方案.md](./RiftX-技术实现方案.md)

## 1. 一句话定位

RiftX 是以开源 Agent Runtime（固定版本、源码内嵌）为底座的 **Win / Mac 桌面应用**：把安全工具放进指定文件夹就能当手脚用，自己配置大模型，用三种模式把红队 / 渗透工作流整合得更好用。边界主要靠提示词和分档人工审批，不做重型 OS 隔离平台。

本质：单靠该 runtime 也能做这些事；RiftX 把它们收成一个独立程序，界面、任务、工具、审批都更顺手。

## 2. 三种模式（从紧到松）

| 模式 | 场景 | 审批节奏 |
| --- | --- | --- |
| **RedTeam** | 护网等对抗演练中的 AI 攻击队 | 最紧：危险命令 + 高风险工具都要人批 |
| **Pentest** | 企业内对指定目标做巡检 / 评估 | 中等：多数操作可跑，危险命令要批 |
| **Auto** | 靶场 / Lab 全自动推进直至目标 | 最松：启动前一次性确认风险，运行中尽量少打断；保留 Kill Switch |

人会在提示词里说明目标与边界；产品不靠堆强制隔离来“替人负责”。敏感操作需要人类批准即可。

## 3. 产品形态

| 平台 | 形态 |
| --- | --- |
| macOS | 桌面应用（Apple Silicon 优先） |
| Windows | 桌面应用 |
| Linux | **暂不考虑** |

- 必须有自己的应用 UI，**不照抄**上游 Agent 产品的界面与交互品牌。
- 所有面向用户的展示（窗口标题、关于页、安装包名、进程名、报告抬头、设置文案等）使用 **RiftX** 品牌。
- 上游产品标识不出现在 UI / 安装包 / 报告等展示面；源码、许可证与技术归属可保留在开发者可见位置。
- 只靠用户自己配置大模型（Responses-compatible Profile + API Key），**不提供**上游账号登录 / 设备码登录。
- 后台为本机 `riftxd`，经本地 IPC（macOS UDS / Windows Named Pipe）与桌面通信，不公开 TCP 控制面。

## 4. 核心能力

### 4.1 Agent 底座

复用固定版本的开源 Agent Runtime：对话、工具调用、Skill、shell、流式事件、中断。RiftX 不重写通用 Agent，而聚焦整合体验。

### 4.2 Tools Directory（手脚）

初期不预装渗透工具。用户配置一个或多个工具目录，例如：

```text
~/.riftx/tools/
├── nmap
├── nuclei
├── custom-scanner
└── team-script
```

放入或安装到该目录、当前用户可执行的文件，即可进入 Agent 任务 PATH。RiftX 负责扫描、兼容性提示、`tools doctor`、可选元数据（风险等级等），**不要求**为每个二进制写死适配器。

### 4.3 Skills Directory

单一用户 Skills 目录，用于工作流、提示与脚本扩展。

### 4.4 LLM 与密钥

本机配置多个 Profile（模型、base URL）。API Key 与目标类凭据存放在**操作系统钥匙串 / 凭据库**，不进入对话明文、报告或普通日志。

### 4.5 桌面工作台

自有三栏（或等价）布局：任务列表、对话与执行时间线、任务信息与审批。底部支持模式切换、输入、暂停与 Kill Switch。视觉与文案按 RiftX 设计，不沿用上游产品壳。

## 5. 安全底线（保留）与明确不做

### 保留

- API Key / 目标凭据进系统安全存储。
- 分档人工审批（见第 2 节）。
- Kill Switch / 暂停。
- 不实现遥测。

### 明确不做（v0.8 非目标；远期若企业强需求再议）

- OS 级 Guard（Landlock / netns / Seatbelt / WFP 等强制隔离）。
- 多维强制 Scope / Policy Revision 作为主安全边界。
- 全库案件加密、加密 `.riftxcase` 容器。
- 独立 Evidence Evaluator 作为成功硬门槛。
- Linux CLI/TUI、三平台签名公证与自动更新工程（可晚于产品好用之后）。

仓库中若仍有上述相关代码，视为**遗留实现**，产品路线不再加功能；后续按需收敛或删除。

## 6. 当前实现对照（摘要）

已有且继续用：

- 内嵌 Agent Runtime、`riftxd`、本地 IPC、Tauri 桌面壳。
- Tools / Skills 目录扫描与 doctor。
- 自配 LLM Profile、钥匙串存 Key。
- 任务创建、对话、中断、单次命令审批、模式切换 UI（命名将收敛为 RedTeam / Pentest / Auto）。
- Markdown / JSON 报告。

遗留或偏离、需按 v0.8 收敛：

- 旧模式名 Native / Hardened / Auto 与 OS Guard 路径。
- 过重的凭据租约 / 全库加密 / 案件包叙事。
- Linux 与发布签名排期。
- 展示面若残留上游产品名，改为 RiftX。

细节与阶段计划见 [RiftX-技术实现方案.md](./RiftX-技术实现方案.md)。本地进度见 [当前项目进度.md](./当前项目进度.md)（不提交）。

## 7. 当前运行方式

构建 Gateway / CLI：

```bash
cd codex-rs && cargo build -p codex-riftx-gateway -p codex-riftx-cli
```

启动 daemon（需自备 API Key）：

```bash
export RIFTX_LLM_API_KEY="<your-api-key>"
./codex-rs/target/debug/riftxd --config riftx.toml
```

启动桌面：

```bash
pnpm install
RIFTX_CONFIG="$PWD/riftx.toml" pnpm --filter @riftx/desktop tauri dev
```

## 8. 源码布局（产品相关）

```text
RiftX/
├── apps/desktop/                 # Tauri + React 桌面（RiftX UI）
├── codex-rs/
│   ├── riftx-gateway/            # riftxd
│   ├── riftx-ipc/
│   ├── riftx-tools/
│   ├── riftx-skills/
│   ├── riftx-cli/
│   ├── riftx-app-server-adapter/ # Agent Runtime 受限 facade
│   └── …                         # 内嵌 runtime 与其它 crate
├── RiftX-项目说明.md
└── RiftX-技术实现方案.md
```

目录名 `codex-rs` 等为历史/上游源码树，属开发者可见结构；**产品展示不以该名称对外品牌化**。

## 9. 上游归属

项目内含固定版本、Apache-2.0 许可的开源 Agent Runtime 源码。上游名称仅出现在源码兼容、许可证、锁文件与技术归属中，**不进入** RiftX 产品品牌、账号体系或用户可见主文案。RiftX 是独立项目。
