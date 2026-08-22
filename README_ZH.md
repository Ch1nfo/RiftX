# RiftX

[English README](README.md)

RiftX 是一个本机单用户 Web UI，用于在已获得明确授权的范围内进行 Web 渗透测试与漏洞验证。它提供紧凑的终端指挥台界面，用于受控侦察、分析和基于证据的验证。

RiftX 当前是 MVP，重点实现基础 Agent 能力和本机安全运行，不内置 nmap、httpx、subfinder 等专用扫描器。

## 主要能力

- Agent 会话，支持文本、思考、工具执行和错误事件流式显示。
- 默认允许只读工具：`read`、`grep`、`find`、`ls`。
- `bash`、`write`、`edit` 以及会修改页面的浏览器 action 受审批策略保护。
- 三种审批模式：请求审批、帮我审批、完全访问。
- 模型配置档案：Provider、模型 ID、API Key、Base URL、协议、传输方式、上下文大小、输出上限和 Thinking level。
- 配置多个模型时，可直接在工作台输入框右侧点击模型名称切换。
- 支持并发子 Agent，具备排队、重试、取消、独立线程、独立审批门，以及继承或覆盖主模型配置的能力。
- 主 Agent 和子 Agent 均可记录会话发现，包含置信度、影响、复现说明，以及可复核的摘录、工具调用、浏览器请求或截图证据。
- 支持不中断当前 Agent 任务的中途上下文自动压缩，并根据压缩后的对话更新上下文占用。
- 重新连接会恢复子 Agent 快照与待处理审批；已归档或不属于当前工作目录的会话不能建立事件流。
- 会话历史、归档管理、可折叠工具卡片、Markdown、上下文占用圆环和明暗主题。
- 中英文界面切换。
- 工作台顶部提供系统文件夹选择器；最近会话按当前工作目录隔离显示。
- 设置页只保留“模型与 Agent”和“归档会话”两个页面，各设置区域使用独立保存按钮。
- 主 Agent 支持可选的自定义系统提示词；关闭开关或内容为空时使用内置默认提示词。
- 自定义提示词在新建或重新打开会话时生效；已经加载的会话继续使用当前提示词。
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

- 交互式 Agent 会话
- 受控的本机命令与文件操作
- 带审批门的浏览器自动化，用于登录态或交互式 Web 流程
- 单个父会话中的并发子 Agent 委派
- 与支撑证据关联、可持久化复核的会话发现
- 在预留响应空间耗尽前自动压缩上下文
- 活跃会话和历史会话的真实上下文占用与模型信息持久化

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

打开 <http://localhost:3000>，先点击工作台顶部的工作目录按钮，通过系统文件夹选择器选择目录，再进入“设置”创建或选择模型配置。初始工作目录是启动 RiftX 时所在的目录；切换目录后，左侧只显示新目录下的会话。

输入框中的模型选择器会原地切换当前 Agent 会话，即使会话还没有发送过第一条消息也不会丢失会话 ID 或历史；模型和上下文窗口会同步更新。

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
- `browser` 是统一的 action 工具。只读 action 可直接执行；导航、页面修改、表单提交和关闭浏览器受审批控制。
- 浏览器只读 action（`snapshot`、`requests`、`cookies`、`storage`、`screenshot`、`tabs`）可直接使用；导航和会修改页面的 action 使用同一审批门。
- 子 Agent 与主 Agent 使用同一套受控工具能力，但不能继续创建子 Agent；每个子 Agent 有独立审批门和 BrowserContext。
- 设置 `RIFTX_BROWSER_ALLOWED_ORIGINS`（逗号分隔的授权 Origin）来配置浏览器作用域；未设置时首次导航会锁定当前 Origin。越界请求和重定向会被阻止。
- 请求审批会暂停任务，等待人工明确决定。
- 帮我审批会评估提议的操作；无法判断其是否会影响本机或被测系统时默认阻止。
- 完全访问会绕过审批门，只应在受控环境中使用。
- 审批失败、超时和客户端断开时默认拒绝。
- 当前审批模式会同步作用于主 Agent 和正在运行的子 Agent。

不得使用 Agent 访问授权范围以外的系统、干扰服务、删除数据、窃取凭据或保留访问权限。模型输出不等于安全保证；允许有影响的操作前，应检查命令和证据。

## 配置与敏感数据

运行时数据保存在仓库之外的 `~/.riftx/`：

- `~/.riftx/config.json` 保存模型配置和 RiftX 设置。
- `~/.riftx/sessions/` 保存 Agent 会话 JSONL 历史。
- `~/.riftx/agent/` 保存 RiftX 独立的授权和模型元数据。
- `~/.riftx/subagents/<父会话 ID>/` 保存子 Agent 任务状态、日志、摘要和线程元数据。服务重启后，运行中的任务会标记为 `interrupted`，不会自动重新执行。
- `~/.riftx/evidence/<会话 ID>/` 保存会话发现和为发现保留的截图。
- `~/.riftx/skills/` 保存本机安装的 Agent Skill（`<skill-name>/SKILL.md`）；新建或重新打开的会话会加载其中的 skill。

Skill 使用 Agent Skills 格式：

```text
~/.riftx/skills/<skill-name>/SKILL.md
```

`SKILL.md` 的 frontmatter 必须包含与目录匹配的、仅使用小写字母/数字/连字符的 `name`，以及 `description`。RiftX 会把 skill 目录提供给 Agent，并在专业任务中自动加载最匹配的 skill；也可以通过 `/skill:<skill-name>` 显式调用。RiftX 只读加载 skill，不会改写外部 skill 文件。RiftX 只加载这个明确指定的用户 skill 目录，忽略 SDK 和项目的隐式 skill 目录。
在 Windows 上，该路径会通过系统 home 目录解析为 `%USERPROFILE%\\.riftx\\skills`。

API Key 会以受限权限保存在本机。不要提交 API Key、会话历史、Authorization Header、Cookie、目标数据、证书、私钥或侦察生成文件。仓库中的 `.gitignore` 已覆盖常见密钥、运行时文件、构建产物和本地缓存，但提交前仍应检查 `git status`。

## 项目结构

```text
src/app/          Next.js 页面和 API 路由
src/components/   工作台、设置页和共享 UI
src/server/       Agent 运行时适配、本机配置、会话生命周期、审批、用量和持久化辅助模块
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

## 会话发现

主 Agent 和子 Agent 可以把有证据支撑的结论记录到当前父会话。

- 每条发现包含受影响资产、置信度（`confirmed`、`likely`、`suspected` 或 `not_reproducible`）、影响、复现说明、来源和时间戳。
- 证据可以引用简短摘录、工具调用、已捕获的浏览器请求或保留的截图。
- 工作台证据面板可以跳转到工具与请求详情，截图按需加载。
- 操作者可以调整置信度、忽略发现并恢复已忽略的发现，不会删除底层记录。
- 发现按规范化后的资产和标题去重，并合并新证据。

## 子 Agent

RiftX 在 Agent session 之上实现了应用层子 Agent 系统。

- 子 Agent 按配置的最大并发数并行运行。
- 超出并发限制的任务会进入父会话队列等待。
- 子 Agent 与主 Agent 共享工作目录。
- 子 Agent 拥有独立的 thread、审批门和浏览器上下文。
- 子 Agent 默认继承主模型，也可以在设置页切换为独立 profile。
- 最大并发子 Agent 数量可在设置中配置为 1 到 8，超过上限的任务会进入父会话队列。
- 调度积极性分为“低”“默认”“高”。高档会更积极地委派并提示更高的 token 与并发消耗，但仍会跳过重复任务和有状态依赖任务。
- 子 Agent 不能递归继续创建子 Agent。
- 子 Agent 的审批请求显示在主 Composer 审批区域，状态和日志显示在工作台面板中。
- 子 Agent 结果和增量日志会随选定的父会话持久化和恢复。
- 所有子 Agent 都在后台运行，主 Agent 可以继续执行独立工作；子 Agent 完成后会立即回传结果。所有已启动的子 Agent 都是本次评估的必需任务：如果主 Agent 已经准备结束而仍有子 Agent 未完成，RiftX 才会等待所有子 Agent 到达终止状态，再请求主 Agent 生成最终结论。
- `spawn_subagent` 不再提供可选等待模式。主 Agent 禁止使用 `bash`/`sleep` 轮询子 Agent 日志、`tasks.json` 或文件系统状态，最终 join 由运行时完成。
- 重新连接会话时会重放当前子 Agent 任务快照和未处理的审批请求。

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
- `GET /api/sessions/:id/findings`
- `PATCH /api/sessions/:id/findings/:findingId`
- `GET /api/sessions/:id/findings/screenshot/:screenshotId`
- `GET /api/sessions/:id/subagents`
- `POST /api/sessions/:id/subagents/:taskId/cancel`
- `POST /api/sessions/:id/subagents/:taskId/retry`
- `PUT /api/settings/approval-mode`
- `GET/PUT /api/settings/model-profiles`
- `POST /api/workspace`

所有接口面向本机单用户运行，不提供远程鉴权。

## 许可证

详见 [LICENSE](LICENSE)。
