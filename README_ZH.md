<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="public/riftx-logo-dark-mark.png">
  <source media="(prefers-color-scheme: light)" srcset="public/riftx-logo-light-mark.png">
  <img alt="RiftX" src="public/riftx-logo-light-mark.png" width="420">
</picture>

### 面向授权 Web 安全测试的本机多 Agent 工作台

[![版本](https://img.shields.io/badge/版本-0.1.0-blue.svg)](https://github.com/Ch1nfo/RiftX/releases)
[![Node.js](https://img.shields.io/badge/Node.js-20.18.1%2B-339933.svg?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000.svg?logo=nextdotjs&logoColor=white)](https://nextjs.org/)
[![React](https://img.shields.io/badge/React-19-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6.svg?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33.svg?logo=playwright&logoColor=white)](https://playwright.dev/)
[![许可证](https://img.shields.io/badge/许可证-MIT-green.svg)](LICENSE)
[![平台](https://img.shields.io/badge/平台-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#环境要求)

[English](README.md) | 中文

</div>

---

## 为什么选择 RiftX？

Web 安全验证通常散落在终端、浏览器、代理工具、笔记和多个模型会话中。上下文需要反复搬运，子任务难以并行，结论也容易与原始请求、截图和命令输出脱节。

**RiftX** 将 Agent 对话、受控本机工具、Playwright 浏览器、多 Agent 协作和证据记录放进一个本机工作台。它面向已经获得明确授权的 Web 测试场景，强调人工可控、过程可见和结论可复核，而不是替代专业扫描器或自动决定测试边界。

- **统一工作台** — 在同一界面查看流式回复、思考过程、工具调用、审批、上下文用量、子 Agent 和证据。
- **多 Agent 并行协作** — 主 Agent 可把独立任务交给后台子 Agent，并在全部必要任务结束后统一收敛结论。
- **浏览器原生验证** — 通过 Playwright 操作真实页面，读取 DOM 快照、网络请求、控制台、Cookie、Storage 和截图。
- **证据驱动输出** — 发现可关联工具调用、浏览器请求、摘录和截图，并记录影响、置信度与复现步骤。
- **本机优先** — 无需数据库和 RiftX 云账户；配置、会话、Skill 与证据都保存在当前用户目录。
- **开放模型接入** — 支持 OpenAI、Anthropic、Google 及兼容端点，可按会话切换模型配置。

## 界面预览

| Agent 工作台 | 模型与 Agent 设置 |
| :---: | :---: |
| ![RiftX Agent 工作台](docs/images/riftx-workbench.png) | ![RiftX 设置页面](docs/images/riftx-settings.png) |

> 截图使用演示数据生成，不包含真实 API Key、目标或会话历史。

## 功能特性

### Agent 工作台

- **流式会话** — 实时呈现文本、Thinking、工具调用、错误和任务状态；断线重连后恢复会话现场。
- **连续对话** — 可在 Agent 运行期间发送补充指令；对话区自动跟随最新内容，也可回到历史位置查看。
- **会话管理** — 会话按工作目录隔离，支持 AI 自动命名、切换、归档、恢复查看和永久删除。
- **上下文管理** — 显示输入、输出、缓存和剩余 Token；接近限制时自动压缩上下文，并在对话中展示压缩状态。
- **模型切换** — Composer 中可直接切换当前会话模型，不影响其他前台或后台会话。
- **双语界面** — 中英文即时切换，并支持明暗主题。

### 多 Agent 编排

- 主 Agent 可将相互独立的调查任务委派给后台子 Agent，自己继续处理主线任务。
- 每个子 Agent 拥有独立线程、审批门和 BrowserContext，并共享父会话的工作目录。
- 子 Agent 默认继承主 Agent 模型，也可以使用单独的模型配置档案。
- 最大并发数可配置为 `1–8`；超过上限的任务自动排队。
- 调度积极性提供低、默认、高三档，用于平衡并行度与 Token 消耗。
- 支持查看增量日志、取消与重试；重连后恢复任务快照和未处理审批。
- 子 Agent 不能继续创建子 Agent；运行时会在最终回答前等待全部必需子任务结束。

### Browser 工具

RiftX 提供一个统一的 action 型 `browser` 工具，底层由 Playwright/Chromium 驱动：

- **页面交互** — `navigate`、`snapshot`、`click`、`fill`、`press`、`select`、`back`、`reload`
- **运行时检查** — `evaluate`、`console`、`screenshot`
- **网络证据** — `requests`、`request_detail`、`response_body`
- **身份与状态** — `use_identity`、`identities`、`cookies`、`cookies_export`、`cookies_import`、`storage`
- **网络控制** — `set_host_mappings`、`set_user_agent`、`set_extra_headers`
- **页面管理** — `tabs`、`close`

页面快照会生成适合模型读取的文本，并使用 `e1`、`e2` 等稳定元素引用。不同 identity 拥有隔离的 Cookie 与 Storage，可用于并行验证匿名、低权限和高权限状态。启用模型图像输入后，截图可以直接作为视觉上下文发送给模型。

浏览器授权范围支持 CIDR、主机、主机与端口、泛域名以及限定协议的 URL 规则。未配置规则时，首次导航会锁定当前主机；出界导航需要审批。虚拟主机测试可通过 host mapping 保留 Host Header 与 TLS SNI，内网环境也可按设置接受自签或无效证书。

### 审批与操作控制

RiftX 为可能改变本机或目标状态的操作提供三种审批模式：

| 模式 | 行为 | 适用场景 |
| --- | --- | --- |
| 请求审批 | 每个受控操作等待人工允许或拒绝 | 默认模式，需要逐步确认 |
| 帮我审批 | Agent 评估影响；无法确认时拒绝 | 已知范围内的连续验证 |
| 完全访问 | 跳过审批门 | 仅限隔离且完全受控的环境 |

`read`、`grep`、`find`、`ls` 等只读操作默认直接执行；`bash`、`write`、`edit` 和会改变浏览器状态的 action 进入审批流程。审批失败、超时或客户端断开时默认拒绝。

### 发现与证据

- 主 Agent 和子 Agent 都可以向父会话写入结构化发现。
- 每条发现包含受影响资产、置信度、影响、复现说明、来源和时间戳。
- 证据可以关联消息摘录、工具调用、浏览器请求或保留的截图。
- 发现按规范化资产与标题去重，并在后续验证中合并新证据。
- 操作者可调整置信度、忽略或恢复发现，不会破坏底层证据记录。

### Agent Skills

RiftX 从以下目录加载本机 Skill：

```text
~/.riftx/skills/<skill-name>/SKILL.md
```

`SKILL.md` frontmatter 需要包含与目录匹配的小写 `name` 和清晰的 `description`。每次普通用户消息都会尝试匹配适用 Skill，同一个 Skill 在一个活动会话中只自动注入一次；也可以使用 `/skill:<skill-name>` 显式调用。RiftX 只读加载该目录，不会修改 Skill 文件，也不会隐式读取项目或 SDK 的 Skill 目录。

### 模型与 Agent 配置

- 支持 `openai-completions`、`openai-responses`、`anthropic-messages` 和 `google-generative-ai` API 协议。
- 每个配置档案可设置 Provider、模型 ID、API Key、Base URL、传输方式、上下文窗口、最大输出、Thinking level 和图像输入能力。
- 主 Agent 与子 Agent 可分别选择模型；运行中的会话按会话粒度切换并保留自己的配置。
- 可配置主 Agent 自定义系统提示词、子 Agent 并发数与调度积极性。
- API Key 只保存在本机配置中，不需要创建 RiftX 账户。

### 持久化与恢复

- 会话使用本机 JSON/JSONL 持久化，服务重启后仍可读取历史。
- 子 Agent 任务、日志、摘要和审批状态跟随父会话恢复；重启时尚未结束的任务会标记为 `interrupted`，不会自动重放。
- 发现及其截图独立保存，损坏 JSON 会保留原始文件备份，写入采用临时文件加原子替换。
- 停止、归档、切换工作目录和删除会话共享统一清理流程，负责终止 Agent、Bash 与浏览器资源。

## 架构总览

```text
┌──────────────────────────────────────────────────────────────┐
│                 Next.js 15 + React 19 WebUI                  │
│  会话 / 流式对话 / 审批 / 子 Agent / 证据 / 设置 / 双语主题   │
└─────────────────────────────┬────────────────────────────────┘
                              │ REST + SSE
┌─────────────────────────────▼────────────────────────────────┐
│                     RiftX Server Runtime                     │
│  Session Manager ─ Approval Gate ─ Context Compaction        │
│         │                │                  │                 │
│         ├── Main Agent   ├── Local Tools    ├── JSONL Store  │
│         ├── Subagents    └── Browser Scope  └── Evidence     │
│         └── Skills                                              │
└─────────────────────────────┬────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────┐
│            Playwright / Chromium + 本机文件与命令工具         │
│  DOM Snapshot / Network / Identities / Screenshot / Bash     │
└──────────────────────────────────────────────────────────────┘
```

RiftX 采用本机单进程 Web 应用结构：React 工作台通过 Next.js Route Handler 调用 Agent 运行时，SSE 将会话事件持续推送到界面；运行状态落盘到 `~/.riftx/`，无需数据库或远程控制面。

## 技术栈

| 层级 | 技术 | 用途 |
| --- | --- | --- |
| Web 框架 | Next.js 15 | UI、Route Handler、生产服务 |
| 前端 | React 19、TypeScript 5 | 工作台、设置与实时状态管理 |
| Agent 运行时 | pi coding agent | 模型会话、工具与上下文管理 |
| 浏览器 | Playwright、Chromium | 页面交互、网络记录与截图 |
| UI 基础 | Radix Select、Phosphor Icons | 可访问控件与图标 |
| 内容渲染 | react-markdown、remark-gfm | Agent Markdown 输出 |
| 数据校验 | TypeBox、Zod | 工具与运行时结构校验 |
| 持久化 | Node.js 文件系统、JSON/JSONL | 本机配置、会话、任务与证据 |
| 测试 | Node.js Test Runner、tsx | TypeScript 单元与回归测试 |

## 环境要求

- Node.js `20.18.1` 或更高版本，推荐 Node.js 22 LTS。
- npm 10 或与所用 Node.js 版本配套的 npm。
- Git 2.x 或更高版本，用于从 GitHub 安装或 clone 源码。
- 一个可用的模型 API 端点与 API Key。
- Playwright Chromium；安装时会自动下载。

RiftX 不依赖 Conda、Python、数据库或远程 RiftX 账户。Linux 如果缺少 Chromium 系统库，需要额外安装一次 Playwright 系统依赖。

## 安装与启动

### 方式一：直接从 GitHub 安装

无需手动 clone，安装到当前用户目录：

```bash
npm_config_prefix="$HOME/.local" npm install --global git+https://github.com/Ch1nfo/RiftX.git
export PATH="$HOME/.local/bin:$PATH"
rx webui
```

把下面一行加入 `~/.zshrc` 或 `~/.bashrc`，新终端中即可直接使用 `rx`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

#### Windows PowerShell

上面的 `$HOME` 和 `export PATH` 是 Unix shell 语法，不能直接复制到 PowerShell。Windows 的 npm 通常使用当前用户可写的全局目录，因此直接执行：

```powershell
npm install --global git+https://github.com/Ch1nfo/RiftX.git
rx webui
```

如果安装后 PowerShell 找不到 `rx`，先查看 npm 全局目录，并将它加入当前用户的 `PATH`：

```powershell
npm config get prefix
```

默认目录通常是 `$env:APPDATA\npm`。修改 `PATH` 后请重新打开 PowerShell。

这里明确使用 `git+https://`，避免 `github:Ch1nfo/RiftX` 在部分 Git/npm 配置中被改写为 SSH。RiftX 暂未发布到 npm registry，因此目前不能使用 `npm install -g riftx`。

### 方式二：从源码安装

```bash
git clone https://github.com/Ch1nfo/RiftX.git
cd RiftX
npm install
npm_config_prefix="$HOME/.local" npm link --ignore-scripts
export PATH="$HOME/.local/bin:$PATH"
rx webui
```

`npm install` 会安装依赖、下载 Chromium 并生成生产构建；`npm link --ignore-scripts` 只把当前构建注册为 `rx`，不会再次构建。整个流程不需要 root 或 `sudo`。

Windows PowerShell 中，`npm install` 完成构建并下载 Chromium 后，在源码目录执行：

```powershell
npm link --ignore-scripts
rx webui
```

如果使用 nvm、fnm 等用户级 Node.js 管理器，同样使用上面的 PowerShell 命令即可。除非你修改过 npm 全局目录，否则不需要设置 `npm_config_prefix` 或执行 `export PATH`。

### 首次配置

1. 打开 <http://localhost:3000>。
2. 点击工作台顶部的文件夹按钮，选择 Agent 可以访问的工作目录。
3. 进入“设置”，添加模型配置档案并填写 API Key、Base URL、协议和模型 ID。
4. 保存设置，回到工作台新建会话并发送任务。

可以修改端口和监听地址：

```bash
rx webui --port 4000
rx webui --hostname 127.0.0.1
rx webui --port 4000 --hostname 0.0.0.0
```

### Linux 浏览器依赖

如果 Chromium 启动时提示缺少系统库，执行：

```bash
npx playwright install-deps chromium
```

该命令安装操作系统级依赖，可能需要系统管理员权限；RiftX 本身仍可安装在普通用户目录。

## 开发命令

```bash
npm install          # 安装依赖、Chromium，并执行生产构建
npm run dev          # 开发服务器与热更新
npm run typecheck    # TypeScript 类型检查
npm test             # 单元与回归测试
npm run build        # 生成生产构建
npm start            # 直接启动生产构建
```

开发服务器和 `rx webui` 默认使用 <http://localhost:3000>。`rx webui` 始终启动已生成的生产构建。

## 运行时数据

所有 RiftX 数据默认位于 `~/.riftx/`：

| 路径 | 内容 |
| --- | --- |
| `~/.riftx/config.json` | 模型配置、审批模式、浏览器范围与 Agent 设置 |
| `~/.riftx/sessions/` | Agent 会话 JSONL 历史 |
| `~/.riftx/agent/` | RiftX 隔离的模型与认证元数据 |
| `~/.riftx/subagents/<session-id>/` | 子 Agent 状态、日志、摘要与线程信息 |
| `~/.riftx/evidence/<session-id>/` | 发现记录与保留截图 |
| `~/.riftx/skills/` | 用户安装的 Agent Skills |

不要提交 API Key、Authorization Header、Cookie、目标数据、证书、私钥、会话历史或测试产物。提交代码前始终检查 `git status`。

## 项目结构

```text
RiftX/
├── bin/                 # rx CLI 入口
├── docs/images/         # README 界面截图
├── public/              # Logo 与静态资源
├── src/
│   ├── app/             # Next.js 页面与 API Route Handler
│   ├── browser/         # Playwright 工具、范围控制和网络记录
│   ├── components/      # 工作台、设置与共享 UI
│   ├── lib/             # 共享类型、国际化与前端辅助模块
│   └── server/          # Agent 运行时、配置、会话、审批与持久化
├── package.json
├── README.md
└── README_ZH.md
```

## Web API

<details>
<summary><strong>查看主要本机 API</strong></summary>

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/bootstrap` | 加载工作区、会话和设置 |
| `GET/POST` | `/api/sessions` | 列出或创建会话 |
| `DELETE` | `/api/sessions/:id` | 删除归档会话 |
| `POST` | `/api/sessions/:id/archive` | 归档会话 |
| `GET` | `/api/sessions/:id/stream` | 订阅 SSE 会话事件 |
| `GET` | `/api/sessions/:id/messages` | 读取会话消息 |
| `POST` | `/api/sessions/:id/prompt` | 发送任务或补充指令 |
| `POST` | `/api/sessions/:id/abort` | 停止运行中的任务 |
| `POST` | `/api/sessions/:id/approval` | 响应审批请求 |
| `GET` | `/api/sessions/:id/findings` | 读取会话发现 |
| `PATCH` | `/api/sessions/:id/findings/:findingId` | 更新发现状态 |
| `GET` | `/api/sessions/:id/subagents` | 读取子 Agent 状态 |
| `POST` | `/api/sessions/:id/subagents/:taskId/cancel` | 取消子 Agent |
| `POST` | `/api/sessions/:id/subagents/:taskId/retry` | 重试子 Agent |
| `PUT` | `/api/settings/approval-mode` | 更新审批模式 |
| `GET/PUT` | `/api/settings/model-profiles` | 读取或保存模型配置 |
| `POST` | `/api/workspace` | 切换工作目录 |

这些接口面向本机单用户运行，不提供远程用户认证。不要把服务直接暴露到不受信任的网络。

</details>

## 使用边界

RiftX 只应用于操作者已获得明确授权的系统。不得使用它访问授权范围之外的目标、干扰服务、删除数据、窃取凭据或保持未经许可的访问。Agent 输出不是安全保证；执行可能产生影响的操作前，应检查命令、目标和证据。

RiftX 当前不内置 nmap、httpx、subfinder、nuclei、ffuf 等专用扫描器，也不提供多用户账户、RBAC、远程任务编排或浏览器认证态自动导出到任意 CLI 工具。

## 常见问题

<details>
<summary><strong>为什么推荐安装到 <code>~/.local</code>？</strong></summary>

系统级 npm prefix 通常指向 `/usr/local`，普通用户没有写权限。使用 `npm_config_prefix="$HOME/.local"` 可以避免 `EACCES`，也不需要 `sudo`。确认 `$HOME/.local/bin` 已加入 `PATH` 即可。

</details>

<details>
<summary><strong>为什么安装时会下载 Chromium 并执行构建？</strong></summary>

RiftX 的 Browser 工具依赖 Playwright Chromium，`rx webui` 运行的是 Next.js 生产构建。`postinstall` 负责安装浏览器，`prepare` 负责生成 `.next` 构建，因此首次安装耗时会长于普通 CLI 包。

</details>

<details>
<summary><strong>没有 API Key 能打开 WebUI 吗？</strong></summary>

可以打开和配置界面，但 Agent 无法执行模型任务。请在“设置”中创建至少一个有效模型配置档案。

</details>

<details>
<summary><strong>Skill 只在新会话开始时选择吗？</strong></summary>

不是。每条普通用户消息都会尝试匹配 Skill；匹配到的新 Skill 会注入当前活动会话。已经自动注入过的同一 Skill 不会重复注入，也可以随时使用 `/skill:<name>` 明确调用。

</details>

<details>
<summary><strong>数据是否会上传到 RiftX 服务？</strong></summary>

RiftX 没有云端账户或 RiftX 数据服务。运行时状态保存在本机；发送给所配置模型 Provider 的内容仍受该 Provider 的服务与隐私条款约束。

</details>

## 贡献

欢迎通过 [Issues](https://github.com/Ch1nfo/RiftX/issues) 反馈缺陷和建议，也欢迎提交 Pull Request。提交前请运行：

```bash
npm run typecheck
npm test
npm run build
```

新增功能前建议先开 Issue 对齐范围。请勿提交本机 `~/.riftx/` 数据、API Key、目标信息或开发计划文档。

## 许可证

本项目基于 [MIT License](LICENSE) 开源。

## 联系方式

- Email: [ch1nfo@foxmail.com](mailto:ch1nfo@foxmail.com)

---

<div align="center">

**如果 RiftX 对你有帮助，请给项目一个 Star。**

</div>
