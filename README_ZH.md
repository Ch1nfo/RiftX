# RiftX

[English README](README.md)

RiftX 是一个本机单用户 Web UI，用于在已获得明确授权的范围内进行 Web 渗透测试与漏洞验证。它嵌入 Pi coding-agent SDK，提供紧凑的终端指挥台界面，用于受控侦察、分析和基于证据的验证。

RiftX 当前是 MVP，重点实现 Pi 的基础 Agent 能力和本机安全运行，不内置 nmap、httpx、subfinder 等专用扫描器。

## 主要能力

- Pi coding-agent 会话，支持文本、思考、工具执行和错误事件流式显示。
- 默认允许只读工具：`read`、`grep`、`find`、`ls`。
- `bash`、`write`、`edit` 以及会修改页面的浏览器 action 受审批策略保护。
- 三种审批模式：请求审批、帮我审批、完全访问。
- 模型配置档案：Provider、模型 ID、API Key、Base URL、协议、传输方式、上下文大小、输出上限和 Thinking level。
- 配置多个模型时，可直接在工作台输入框右侧点击模型名称切换。
- 支持并发子 Agent，具备排队、重试、取消、独立 Pi 线程、独立审批门，以及继承或覆盖主模型配置的能力。
- 会话历史、归档管理、可折叠工具卡片、Markdown、上下文占用圆环和明暗主题。
- 中英文界面切换。
- AI 自动总结任务标题，并在发送新任务后立即更新。
- SSE 事件流和本机 JSON/JSONL 持久化。
- 单一 action 形式的 `browser` 工具，底层使用 Playwright/Chromium，支持 DOM 引用（`e1`、`e2`）、请求历史、Cookie、Storage、标签页和截图。

## 环境要求

- 与当前 Next.js 版本兼容的 Node.js。
- 使用名为 `agent` 的 conda 环境执行开发和验证命令。
- 在设置页配置可兼容的模型接口和 API Key。
- 安装一次 Playwright Chromium：`conda run -n agent npx playwright install chromium`。

RiftX 不需要数据库，也不需要远程 RiftX 账户。

## 当前 MVP 范围

当前 RiftX 重点覆盖：

- 基于 Pi 的交互式 Agent 会话
- 受控的本机命令与文件操作
- 带审批门的浏览器自动化，用于登录态或交互式 Web 流程
- 单个父会话中的并发子 Agent 委派

当前 RiftX 还不包含：

- 内置 nmap、httpx、subfinder、nuclei、ffuf 等专用扫描器
- 多用户账户、RBAC 或远程鉴权
- 数据库或远程任务编排
- 浏览器认证态自动导出到 CLI 工具

## 快速开始

```bash
conda run -n agent npm install
conda run -n agent npm run dev
```

打开 <http://localhost:3000>，进入“设置”创建或选择模型配置。默认工作目录是启动 RiftX 时所在的目录。

生产构建：

```bash
conda run -n agent npm run build
conda run -n agent npm start
```

## 开发命令

```bash
conda run -n agent npm run typecheck
conda run -n agent npm test
conda run -n agent npm run build
```

安装依赖后，请额外执行一次 Playwright Chromium 安装：

```bash
conda run -n agent npx playwright install chromium
```

## 安全模型

RiftX 只应被用于操作者明确获得授权的目标。

- `read`、`grep`、`find`、`ls` 默认允许。
- `bash`、`write`、`edit` 由审批扩展拦截。
- 浏览器只读 action（`snapshot`、`requests`、`cookies`、`storage`、`screenshot`、`tabs`）可直接使用；导航和会修改页面的 action 使用同一审批门。
- 子 Agent 与主 Agent 使用同一套受控工具能力，但不能继续创建子 Agent。
- 设置 `RIFTX_BROWSER_ALLOWED_ORIGINS`（逗号分隔的授权 Origin）来配置浏览器作用域；未设置时首次导航会锁定当前 Origin。越界请求和重定向会被阻止。
- 请求审批会暂停任务，等待人工明确决定。
- 帮我审批会评估提议的操作；无法判断其是否会影响本机或被测系统时默认阻止。
- 完全访问会绕过审批门，只应在受控环境中使用。
- 审批失败、超时和客户端断开时默认拒绝。

不得使用 Agent 访问授权范围以外的系统、干扰服务、删除数据、窃取凭据或保留访问权限。模型输出不等于安全保证；允许有影响的操作前，应检查命令和证据。

## 配置与敏感数据

运行时数据保存在仓库之外的 `~/.riftx/`：

- `~/.riftx/config.json` 保存模型配置和 RiftX 设置。
- `~/.riftx/sessions/` 保存 Pi 会话 JSONL 历史。
- `~/.riftx/pi-agent/` 保存 RiftX 独立的 Pi 授权和模型元数据。

API Key 会以受限权限保存在本机。不要提交 API Key、会话历史、Authorization Header、Cookie、目标数据、证书、私钥或侦察生成文件。仓库中的 `.gitignore` 已覆盖常见密钥、运行时文件、构建产物和本地缓存，但提交前仍应检查 `git status`。

## 项目结构

```text
src/app/          Next.js 页面和 API 路由
src/components/   工作台、设置页和共享 UI
src/server/pi/    Pi 适配器、会话生命周期、审批和用量
src/server/       本机配置与持久化辅助模块
src/lib/          共享 TypeScript 类型
src/browser/      统一 Playwright 浏览器工具、快照、作用域校验和网络记录
public/           RiftX Logo 资源
```

## Browser 工具

RiftX 暴露的是单一统一的 `browser` 工具，而不是拆成多个独立浏览器工具。

当前支持的 action 包括：

- `navigate`
- `snapshot`
- `click`
- `fill`
- `press`
- `select`
- `back`
- `reload`
- `requests`
- `request_detail`
- `response_body`
- `cookies`
- `storage`
- `screenshot`
- `tabs`
- `close`

快照会生成适合 Agent 消费的文本视图，并返回稳定元素引用，如 `e1`、`e2`、`e3`，因此模型可以按 ref 交互，而不是直接依赖原始 CSS Selector。

## 子 Agent

RiftX 在 Pi session 之上实现了应用层子 Agent 系统。

- 子 Agent 按配置的最大并发数并行运行。
- 超出并发限制的任务会进入父会话队列等待。
- 子 Agent 与主 Agent 共享工作目录。
- 子 Agent 拥有独立的 Pi thread、审批门和浏览器上下文。
- 子 Agent 默认继承主模型，也可以在设置页切换为独立 profile。
- 子 Agent 不能递归继续创建子 Agent。
- 子 Agent 的审批请求显示在主 Composer 审批区域，状态和日志显示在工作台面板中。

## Web API

主要接口包括：

- `GET /api/bootstrap`
- `GET/POST /api/sessions`
- `DELETE /api/sessions/:id`
- `POST /api/sessions/:id/archive`
- `GET /api/sessions/:id/stream`
- `GET /api/sessions/:id/messages`
- `POST /api/sessions/:id/prompt`
- `POST /api/sessions/:id/title`
- `POST /api/sessions/:id/abort`
- `POST /api/sessions/:id/approval`
- `GET /api/sessions/:id/subagents`
- `POST /api/sessions/:id/subagents/:taskId/cancel`
- `POST /api/sessions/:id/subagents/:taskId/retry`
- `PUT /api/settings/approval-mode`
- `GET/PUT /api/settings/model-profiles`

所有接口面向本机单用户运行，不提供远程鉴权。

## 许可证

详见 [LICENSE](LICENSE)。
