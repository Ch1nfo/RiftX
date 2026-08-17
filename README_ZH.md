# RiftX

[English README](README.md)

RiftX 是一个本机单用户 Web UI，用于在已获得明确授权的范围内进行 Web 渗透测试与漏洞验证。它嵌入 Pi coding-agent SDK，提供紧凑的终端指挥台界面，用于受控侦察、分析和基于证据的验证。

RiftX 当前是 MVP，重点实现 Pi 的基础 Agent 能力和本机安全运行，不内置 nmap、httpx、subfinder 等专用扫描器。

## 主要能力

- Pi coding-agent 会话，支持文本、思考、工具执行和错误事件流式显示。
- 默认允许只读工具：`read`、`grep`、`find`、`ls`。
- `bash`、`write`、`edit` 受审批策略保护。
- 三种审批模式：请求审批、帮我审批、完全访问。
- 模型配置档案：Provider、模型 ID、API Key、Base URL、协议、传输方式、上下文大小、输出上限和 Thinking level。
- 配置多个模型时，可直接在工作台输入框右侧点击模型名称切换。
- 一次性只读子 Agent，支持继承主 Agent 或使用独立 profile。
- 会话历史、归档管理、可折叠工具卡片、Markdown、上下文占用圆环和明暗主题。
- SSE 事件流和本机 JSON/JSONL 持久化。

## 环境要求

- 与当前 Next.js 版本兼容的 Node.js。
- 使用名为 `agent` 的 conda 环境执行开发和验证命令。
- 在设置页配置可兼容的模型接口和 API Key。

RiftX 不需要数据库，也不需要远程 RiftX 账户。

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

## 安全模型

RiftX 只应被用于操作者明确获得授权的目标。

- `read`、`grep`、`find`、`ls` 默认允许。
- `bash`、`write`、`edit` 由审批扩展拦截。
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
public/           RiftX Logo 资源
```

## Web API

主要接口包括：

- `GET /api/bootstrap`
- `GET/POST /api/sessions`
- `GET /api/sessions/:id/stream`
- `GET /api/sessions/:id/messages`
- `POST /api/sessions/:id/prompt`
- `POST /api/sessions/:id/abort`
- `POST /api/sessions/:id/approval`
- `GET/PUT /api/settings/model-profiles`

所有接口面向本机单用户运行，不提供远程鉴权。

## 许可证

详见 [LICENSE](LICENSE)。
