# RiftX V2 完全设计计划书

> 产品定位：面向明确授权场景的 Host-Native 智能渗透测试 Agent
> 文档版本：V2.0
> 文档日期：2026-07-29

---

## 1. 执行摘要

RiftX V2 定位为一个面向专业安全人员的智能渗透测试 Runtime。

RiftX 不提供、安装或托管 Nmap、Nuclei、Metasploit、Impacket 等渗透测试工具，也不强制用户使用 Docker 或其他 Sandbox。RiftX 直接调用执行节点上已经安装并配置好的 CLI、脚本、交互式终端工具和本地服务。

最终架构职责如下：

| 模块             | 核心职责                            |
| -------------- | ------------------------------- |
| Agent Harness  | 理解目标、制定计划、选择工具、分析结果、决定下一步       |
| Temporal       | 持久化 Run 生命周期，处理暂停、恢复、重试、取消和审批等待 |
| RiftX Runner   | 在本机启动、监控和终止进程，管理 Shell 和 PTY 会话 |
| Tool Registry  | 描述当前节点有哪些工具、命令路径和使用能力           |
| Skill Registry | 将高层安全能力映射为具体工具调用                |
| Approval Mode  | 控制哪些动作自动执行、哪些动作需要用户确认           |
| WebUI          | 提供 Run、时间线、终端、审批、发现和报告可视化       |
| RiftX CLI      | 提供类似 Claude Code 的交互式操作体验       |
| Persistence    | 保存任务、消息、工具调用、执行状态、证据和发现         |

一句话概括：

> Agent Harness 负责思考，Temporal 负责记住和推进，Runner 负责调用本机，Tool Registry 负责告诉 Agent 有什么，Skill Registry 负责告诉 Agent 怎么用。

---

# 2. 产品定位

## 2.1 目标用户

RiftX 的主要用户是：

* 红队成员；
* 渗透测试工程师；
* 安全研究人员；
* 攻防演练人员；
* 已经拥有 Kali、Ubuntu、macOS 或 Windows 攻击环境的专业用户；
* 需要把现有安全工具组合成目标导向自动化流程的团队。

默认假设用户：

* 理解渗透测试工具的作用和风险；
* 已获得测试目标的明确授权；
* 能自行安装和维护本机工具；
* 能理解 Shell 命令和终端输出；
* 接受 RiftX 使用启动 Runner 的当前系统用户权限执行命令。

---

## 2.2 RiftX 要解决的问题

RiftX 解决：

1. 用户不需要手动决定每一步使用哪个工具。
2. 不同工具的结果可以被 Agent 连续分析和利用。
3. 长任务不会因为 WebUI 关闭或 Worker 重启而完全丢失。
4. 用户可以随时暂停、继续、接管或终止 Agent。
5. WebUI 和 CLI 使用同一套 Runtime。
6. 本机已有的工具、网络、VPN、代理、凭据和脚本可以直接复用。
7. 测试过程能够整理为 Finding、Evidence 和最终报告。

---

## 2.3 RiftX 不解决的问题

RiftX V2 不负责：

* 安装渗透测试工具；
* 更新渗透测试工具；
* 解决工具依赖；
* 创建统一攻击镜像；
* 保证不同用户环境完全一致；
* 为所有工具编写结构化解析器；
* 建设复杂零信任策略平台；
* 对每一个 Shell 命令进行完整静态安全分析；
* 保证 Host 模式下的命令无法影响用户系统；
* 代替专业用户判断授权是否合法。

---

# 3. 核心设计原则

## 3.1 Host-Native First

工具直接运行在用户现有环境中：

```text
RiftX Agent
    ↓
RiftX Runner
    ↓
bash / zsh / PowerShell / cmd
    ↓
本机 Nmap、Nuclei、Metasploit、自定义 PoC
```

Host 模式不是 Docker 模式的降级方案，而是 RiftX 的主要执行方式。

---

## 3.2 WebUI 与 CLI 共享同一后端

WebUI 和 CLI 都只是客户端：

```text
React WebUI ─┐
             ├── RiftX API ── Temporal ── Runner
RiftX CLI ───┘
```

任何客户端关闭后，Run 都继续由后端运行。

---

## 3.3 直接进程执行优先，Shell 次之，PTY 按需使用

执行优先级：

1. `Direct Process Executor`
2. `Shell Executor`
3. `PTY Executor`

例如 Nmap 应优先执行：

```python
["nmap", "-sV", "-p", "80,443", "10.0.0.8"]
```

而不是：

```bash
nmap -sV -p 80,443 10.0.0.8
```

Python 的 `asyncio.create_subprocess_exec()` 可直接以程序和参数列表创建异步子进程；只有需要管道、重定向、变量展开和 Shell 控制结构时才使用 Shell 执行。

---

## 3.4 工具和 Skill 分离

Tool 表示实际安装的软件：

```text
nmap
nuclei
impacket
msfconsole
sqlmap
```

Skill 表示 Agent 能够完成的能力：

```text
port_scan
service_detection
smb_enumeration
credential_dump
web_vulnerability_scan
```

一个 Tool 可以支持多个 Skill，一个 Skill也可以使用多个 Tool。

---

## 3.5 工具配置是能力清单，不默认作为强制白名单

默认专业模式下：

```yaml
execution_policy: open
```

Tool Registry 用于告诉 Agent当前有哪些工具，而不是阻止 Agent 调用其他命令。

需要限制时可以选择：

```yaml
execution_policy: registered_only
```

此时 Agent 只能调用已注册 Tool 和 Skill。

---

## 3.6 状态持久化与原始文件分离

Temporal 和数据库只保存：

* 状态；
* 摘要；
* ID；
* 路径；
* 哈希；
* 结构化结果。

完整 stdout、stderr、扫描文件、截图、PCAP、报告和附件保存到 Run 工作目录。

---

# 4. 总体架构

```text
┌──────────────────────────────────────────────────────┐
│                    Client Layer                      │
│                                                      │
│     React WebUI                  RiftX CLI            │
│     浏览器操作                    交互式终端           │
└───────────────┬──────────────────────┬───────────────┘
                │ REST / SSE           │ REST / SSE
                └──────────────┬───────┘
                               ▼
┌──────────────────────────────────────────────────────┐
│                  RiftX Control Plane                 │
│                                                      │
│ FastAPI                                              │
│ Run API / Tool API / Approval API / Artifact API     │
│ Event Stream / Node Management / Configuration       │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                   Temporal Runtime                   │
│                                                      │
│ RiftXRunWorkflow                                     │
│ AgentCycleActivity                                   │
│ ReportActivity                                       │
│ Run pause / resume / retry / cancel                  │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                    Agent Harness                     │
│                                                      │
│ OpenAI Agents SDK                                    │
│ Primary Agent / Specialist Agents / Sessions / HITL  │
│ Tool selection / planning / result interpretation    │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                  Tool & Skill Layer                  │
│                                                      │
│ Tool Registry             Skill Registry             │
│ 本机工具描述               高层能力与调用适配          │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                    RiftX Runner                      │
│                                                      │
│ Direct Process / Shell / PTY / Process Supervisor    │
│ stdout / stderr / input / signal / resize / cancel   │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                    Host System                       │
│                                                      │
│ bash / zsh / pwsh / cmd                              │
│ Nmap / Nuclei / Metasploit / Scripts / PoC           │
└──────────────────────────────────────────────────────┘
```

---

# 5. 最终技术栈

## 5.1 后端

| 用途            | 技术                    |
| ------------- | --------------------- |
| 主语言           | Python 3.12           |
| API           | FastAPI               |
| 数据模型          | Pydantic v2           |
| ORM           | SQLAlchemy 2          |
| 数据库迁移         | Alembic               |
| Agent Harness | OpenAI Agents SDK     |
| 持久任务          | Temporal Python SDK   |
| HTTP Client   | httpx                 |
| 异步执行          | asyncio               |
| CLI 命令        | Typer                 |
| 交互式 CLI       | prompt_toolkit        |
| CLI 显示        | Rich                  |
| 日志            | structlog             |
| 测试            | pytest、pytest-asyncio |

OpenAI Agents SDK 提供 Agent、工具调用、Agent-as-tool、handoff、session、tracing 和 HITL 等基础能力，适合作为 RiftX 的内层 Agent Harness。

Temporal Python SDK 提供 Client、Workflow、Activity、Worker、测试环境和重放等接口，负责 Run 的持久生命周期。

---

## 5.2 前端

| 用途       | 技术             |
| -------- | -------------- |
| Web 框架   | React          |
| 语言       | TypeScript     |
| 构建       | Vite           |
| 样式       | Tailwind CSS   |
| UI 组件    | shadcn/ui      |
| 服务端状态    | TanStack Query |
| 本地状态     | Zustand        |
| 终端       | xterm.js       |
| Markdown | react-markdown |
| 图表       | Recharts       |

---

## 5.3 存储

### 单用户本地模式

```text
SQLite
+ 本地 Run 工作目录
+ 本地 Temporal 开发服务
```

### 长期或团队模式

```text
PostgreSQL
+ 本地目录或对象存储
+ 独立 Temporal Service
```

业务数据库通过 SQLAlchemy 抽象，保证 SQLite 和 PostgreSQL 使用同一套 Repository。

---

# 6. 模块一：Domain Core

## 6.1 职责

Domain Core 定义整个 RiftX 的稳定业务对象，不依赖 FastAPI、Temporal 或 OpenAI SDK。

核心对象：

```text
Engagement
Run
Objective
SuccessCriteria
EntryPoint
Scope
Node
Tool
Skill
AgentStep
ToolCall
Execution
TerminalSession
Approval
Artifact
Finding
Report
RunEvent
```

---

## 6.2 推荐目录

```text
src/riftx/domain/
├── engagement.py
├── run.py
├── node.py
├── tool.py
├── skill.py
├── execution.py
├── approval.py
├── artifact.py
├── finding.py
├── report.py
├── event.py
└── enums.py
```

---

## 6.3 开发指导

Domain 对象使用 Pydantic Model 或 dataclass。

不要在 Domain Model 中：

* 查询数据库；
* 调用 API；
* 启动进程；
* 调用模型；
* 导入 FastAPI；
* 导入 Temporal SDK。

推荐状态枚举：

```python
class RunStatus(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

```python
class ExecutionStatus(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
```

---

## 6.4 验收标准

* Domain 包不依赖基础设施模块。
* 所有对象可 JSON 序列化。
* 状态迁移有单元测试。
* 非法状态迁移会明确报错。

---

# 7. 模块二：RiftX Control Plane

## 7.1 职责

Control Plane 是 WebUI 和 CLI 的统一后端，负责：

* 创建和管理 Run；
* 管理节点；
* 查看工具状态；
* 处理审批；
* 提供实时事件；
* 管理 Artifact 和 Finding；
* 调用 Temporal Client；
* 代理终端 WebSocket。

---

## 7.2 推荐目录

```text
src/riftx/api/
├── app.py
├── dependencies.py
├── errors.py
├── routes/
│   ├── runs.py
│   ├── nodes.py
│   ├── tools.py
│   ├── approvals.py
│   ├── executions.py
│   ├── terminals.py
│   ├── artifacts.py
│   ├── findings.py
│   └── events.py
└── schemas/
```

---

## 7.3 核心 API

### Run

```text
POST   /api/v1/runs
GET    /api/v1/runs
GET    /api/v1/runs/{run_id}
POST   /api/v1/runs/{run_id}/pause
POST   /api/v1/runs/{run_id}/resume
POST   /api/v1/runs/{run_id}/cancel
POST   /api/v1/runs/{run_id}/message
```

### Node 和 Tool

```text
GET    /api/v1/nodes
GET    /api/v1/nodes/{node_id}
POST   /api/v1/nodes/{node_id}/refresh-tools
GET    /api/v1/nodes/{node_id}/tools
PUT    /api/v1/nodes/{node_id}/tools/{tool_id}
```

### Approval

```text
GET    /api/v1/runs/{run_id}/approvals
POST   /api/v1/approvals/{approval_id}/approve
POST   /api/v1/approvals/{approval_id}/reject
```

### Execution

```text
GET    /api/v1/executions/{execution_id}
POST   /api/v1/executions/{execution_id}/cancel
GET    /api/v1/executions/{execution_id}/output
```

### Terminal

```text
POST   /api/v1/runs/{run_id}/terminals
GET    /api/v1/terminals/{session_id}
DELETE /api/v1/terminals/{session_id}

WS     /api/v1/terminals/{session_id}/ws
```

### Events

```text
GET    /api/v1/runs/{run_id}/events
```

Run 时间线使用 SSE；终端双向输入输出使用 WebSocket。当前 FastAPI 已提供原生 SSE 支持，并支持事件 ID 和 `Last-Event-ID` 恢复，适合 Agent 输出、工具状态和日志时间线。

---

## 7.4 开发指导

API 路由层只处理：

* 参数解析；
* 权限检查；
* 调用 Application Service；
* 返回 Schema。

不要在路由函数里：

* 写复杂业务逻辑；
* 直接操作 SQLAlchemy Session；
* 直接调用 Runner；
* 直接启动 Temporal Workflow 以外的后台任务。

推荐：

```python
@router.post("/runs")
async def create_run(
    request: CreateRunRequest,
    service: RunApplicationService = Depends(get_run_service),
) -> RunResponse:
    return await service.create_run(request)
```

---

## 7.5 验收标准

* WebUI 和 CLI 全部通过相同 API 工作。
* API 重启后已有 Run 不丢失。
* SSE 断线后可按事件序号续传。
* 所有错误返回统一错误结构。

---

# 8. 模块三：Temporal Runtime

## 8.1 职责

Temporal 负责：

* Run 持久状态；
* Agent Cycle 调度；
* 审批等待；
* 暂停与继续；
* Activity 重试；
* 取消；
* 报告阶段；
* Worker 崩溃恢复。

Temporal 不保存：

* 完整终端输出；
* 完整模型上下文；
* 大型 Artifact；
* 原始扫描结果。

---

## 8.2 Workflow 设计

V2 初期只定义一个主要 Workflow：

```text
RiftXRunWorkflow
```

伪代码：

```python
@workflow.defn
class RiftXRunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        while not self.finished:
            if self.paused:
                await workflow.wait_condition(lambda: not self.paused)

            result = await workflow.execute_activity(
                agent_cycle_activity,
                AgentCycleInput(run_id=run_id),
                start_to_close_timeout=timedelta(minutes=30),
            )

            if result.status == "waiting_approval":
                await workflow.wait_condition(
                    lambda: self.approval_decision is not None
                )

            if result.status == "completed":
                self.finished = True

        await workflow.execute_activity(
            generate_report_activity,
            run_id,
        )
```

---

## 8.3 Workflow Signal

```text
pause
resume
approve
reject
cancel_current_execution
append_user_message
```

---

## 8.4 Workflow Query

```text
get_status
get_current_phase
get_pending_approval
get_active_execution
```

---

## 8.5 Activity 划分

第一阶段保持 Activity 数量少：

| Activity                   | 职责                   |
| -------------------------- | -------------------- |
| `prepare_run_activity`     | 创建工作目录、检查节点和工具       |
| `agent_cycle_activity`     | 执行一个有边界的 Agent Cycle |
| `compact_context_activity` | 压缩历史上下文              |
| `generate_report_activity` | 生成最终报告               |
| `cleanup_run_activity`     | 关闭会话和更新状态            |

不要把每个微小数据库写入都做成 Activity。

---

## 8.6 Agents SDK 与 Temporal 的边界

推荐设计：

```text
Temporal：外层 Run 生命周期
Agents SDK：内层 Agent Loop
Runner：真实命令执行
数据库：Agent 和工具状态检查点
```

`agent_cycle_activity` 运行一个有限 Agent Cycle，例如：

* 最多若干次模型调用；
* 遇到审批中断时返回；
* 达到阶段性结果时返回；
* 达到上下文阈值时返回；
* 达到完成条件时返回。

不要让一个 Activity 永远运行。

---

## 8.7 幂等设计

每次工具调用生成：

```text
execution_key =
run_id
+ agent_step_id
+ tool_call_id
```

Runner 收到重复 `execution_key` 时：

* 已完成：返回原结果；
* 正在运行：返回当前 Execution；
* 失败：根据调用参数决定是否重试；
* 不存在：创建新 Execution。

这样 Agent Cycle Activity 重试时不会无条件重复启动工具。

---

## 8.8 验收标准

* API、Worker 或 WebUI 重启后 Run 可以继续。
* 等待审批期间不占用执行线程。
* Activity 重试不会重复启动同一个命令。
* Temporal Workflow History 不存储大型 stdout。
* 新版本 Workflow 可以通过 Replay Test。

---

# 9. 模块四：Agent Harness

## 9.1 职责

Agent Harness 负责：

* 读取 objective；
* 读取 success criteria；
* 读取 entry points 和 scope；
* 查看节点工具能力；
* 制定计划；
* 调用 Skill；
* 解释工具输出；
* 创建 Finding；
* 判断任务是否完成；
* 请求用户补充信息。

---

## 9.2 Agent 结构

V2 初期不建议创建太多 Agent。

推荐：

```text
Primary Security Agent
├── Recon Specialist
├── Web Specialist
├── AD Specialist
└── Report Specialist
```

第一阶段只实现：

```text
Primary Security Agent
Report Agent
```

其他 Specialist 在场景成熟后再增加。

---

## 9.3 Agent Context

```python
class RiftXAgentContext(BaseModel):
    run_id: str
    node_id: str
    objective: str
    success_criteria: list[str]
    entry_points: list[str]
    scope: list[str]
    approval_mode: ApprovalMode
    workspace: str
    available_tools: list[ToolSnapshot]
```

Agent Context 不直接保存数据库连接或 Runner 对象。

运行时服务通过依赖注入提供。

---

## 9.4 Session 和 Checkpoint

Agents SDK Session 用于保存多轮 Agent 历史；HITL 中断可以通过可序列化 `RunState` 暂停和恢复。审批机制覆盖当前 Agent、handoff Agent 和 Agent-as-tool 调用。

RiftX 应实现：

```text
RiftXDatabaseSession
```

并分别存储：

```text
agent_messages
agent_checkpoints
```

其中：

* `agent_messages` 保存长期会话；
* `agent_checkpoints` 保存待审批或待恢复的 SDK RunState；
* Workflow 只保存 checkpoint ID。

---

## 9.5 Agent 可见工具

不要无条件把所有 Tool 暴露给模型。

根据节点状态动态生成：

```text
当前节点已启用工具
∩
当前 Run 需要的能力
∩
当前 Agent 角色
=
Agent 可见工具
```

始终可见的基础能力：

```text
list_available_tools
run_registered_tool
run_shell
open_terminal
read_terminal
send_terminal_input
close_terminal
create_finding
add_artifact
update_plan
complete_run
```

---

## 9.6 输出约束

Agent 输出分为：

```text
assistant_message
plan_summary
tool_request
finding
run_summary
final_report
```

WebUI 展示简要计划和动作理由，不依赖或展示模型隐藏推理链。

---

## 9.7 验收标准

* Agent 只能看到选定节点实际可用的工具。
* Agent 可以在工具缺失时重新规划。
* Agent Cycle 可以在审批后恢复。
* 模型切换不影响 Tool、Runner 和 Temporal 接口。
* 上下文过大时可以自动压缩。

---

# 10. 模块五：Model Provider

## 10.1 职责

Model Provider 隔离 Agent SDK 与实际模型接口。

配置模型角色：

```yaml
models:
  primary:
    provider: openai_compatible
    model: gpt-5.6
    base_url: "${RIFTX_MODEL_BASE_URL}"

  fast:
    provider: openai_compatible
    model: fast-model

  report:
    provider: openai_compatible
    model: report-model
```

---

## 10.2 接口

```python
class ModelProvider(Protocol):
    def get_model(self, profile: str) -> Model:
        ...
```

不要让业务代码直接读取 API Key 或 `base_url`。

---

## 10.3 开发指导

支持：

* OpenAI Responses；
* OpenAI-compatible API；
* 本地 vLLM；
* 企业模型网关。

不要在第一阶段引入复杂模型路由平台。

模型失败分类：

```text
retryable:
- timeout
- 429
- temporary 5xx

non-retryable:
- invalid model
- invalid request
- context permanently exceeded
- unsupported tool schema
```

---

# 11. 模块六：Tool Registry

## 11.1 职责

Tool Registry 描述某个执行节点上：

* 当前有哪些工具；
* 工具如何启动；
* 使用哪种 Executor；
* 具有什么能力；
* 是否需要 PTY；
* 如何获取版本；
* 默认超时；
* 是否启用。

RiftX 可以内置常见工具的元数据模板，但不附带工具二进制。

---

## 11.2 配置文件

推荐路径：

```text
Linux/macOS:
~/.config/riftx/tools.yaml

Windows:
%APPDATA%\RiftX\tools.yaml
```

示例：

```yaml
version: 1

execution_policy: open

shells:
  default:
    linux: /bin/bash
    macos: /bin/zsh
    windows: pwsh.exe

tools:
  nmap:
    enabled: true
    command:
      - nmap

    executor: process

    capabilities:
      - port_scan
      - service_detection
      - os_detection

    version_probe:
      command:
        - nmap
        - --version

    approval: never
    timeout: 1800

    output:
      preferred: xml

  nuclei:
    enabled: true
    command:
      - nuclei

    executor: process

    capabilities:
      - vulnerability_scan
      - template_scan

    version_probe:
      command:
        - nuclei
        - -version

    approval: never
    timeout: 3600

  msfconsole:
    enabled: true
    command:
      - msfconsole
      - -q

    executor: pty

    capabilities:
      - exploit_search
      - exploit_execute
      - session_management

    approval: sensitive

  custom_poc:
    enabled: true
    command:
      - python3
      - /opt/redteam/pocs/custom.py

    executor: process

    capabilities:
      - custom_verification

    approval: sensitive

    environment:
      PYTHONUNBUFFERED: "1"
```

---

## 11.3 Tool 状态

```python
class ToolAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    DISABLED = "disabled"
    UNKNOWN = "unknown"
```

Runner 启动时执行 `version_probe`。

状态快照同步到 Control Plane：

```json
{
  "tool_id": "nmap",
  "status": "available",
  "resolved_command": "/usr/bin/nmap",
  "version": "7.x",
  "checked_at": "..."
}
```

---

## 11.4 命令解析

`command` 允许使用命令前缀：

```yaml
command:
  - python3
  - /opt/sqlmap/sqlmap.py
```

Agent 传入：

```json
{
  "tool_id": "sqlmap",
  "args": ["-u", "https://target/path", "--batch"]
}
```

最终 argv：

```python
[
    "python3",
    "/opt/sqlmap/sqlmap.py",
    "-u",
    "https://target/path",
    "--batch",
]
```

---

## 11.5 开发指导

Tool Registry 只描述软件，不包含复杂 Agent Prompt。

提供：

```python
class ToolRegistry:
    async def refresh(self) -> ToolSnapshot:
        ...

    def get(self, tool_id: str) -> ToolDefinition:
        ...

    def find_by_capability(
        self,
        capability: str,
    ) -> list[ToolDefinition]:
        ...
```

支持热重载：

```bash
riftx tools reload
```

---

## 11.6 验收标准

* 用户只修改一个配置文件即可注册工具。
* 支持 PATH 命令、绝对路径和脚本前缀。
* 工具不可用时不会暴露给 Agent。
* Tool Registry 不包含安装逻辑。
* WebUI 可以编辑并重新加载配置。

---

# 12. 模块七：Skill Registry

## 12.1 职责

Skill 将 Agent 的高层意图转成具体 Tool 调用。

例如：

```text
port_scan
    ↓
选择 nmap 或 masscan
    ↓
构造 argv
    ↓
调用 Runner
    ↓
解析 XML/JSON
```

---

## 12.2 Skill 接口

```python
class Skill(ABC):
    id: str
    description: str
    required_capabilities: set[str]
    approval_level: ApprovalLevel

    @abstractmethod
    async def execute(
        self,
        context: SkillContext,
        arguments: BaseModel,
    ) -> SkillResult:
        ...
```

---

## 12.3 Skill 类型

### 通用 Skill

```text
run_registered_tool
run_shell
open_terminal
send_terminal_input
read_terminal
close_terminal
```

### 结构化 Skill

```text
port_scan
service_scan
web_scan
smb_enumerate
ad_collect
create_finding
```

### 用户自定义 Skill

通过 Python Entry Point：

```toml
[project.entry-points."riftx.skills"]
company_ad_scan = "company_skills.ad:CompanyADScanSkill"
```

---

## 12.4 Tool 选择

Skill 可以声明：

```python
required_capabilities = {"port_scan"}
preferred_tools = ["nmap", "masscan"]
```

运行时选择：

1. 用户明确指定的 Tool；
2. Skill 推荐工具；
3. 当前节点第一个可用工具；
4. 找不到时返回 `ToolUnavailable`。

---

## 12.5 输出处理

完整输出保存为文件：

```text
runs/{run_id}/executions/{execution_id}/stdout.log
runs/{run_id}/executions/{execution_id}/stderr.log
```

返回给 Agent 的结果应包含：

```json
{
  "summary": "发现 3 个开放端口",
  "structured": {
    "ports": [22, 80, 443]
  },
  "stdout_excerpt": "...",
  "artifact_ids": ["artifact-1"],
  "execution_id": "exec-123"
}
```

不要把数十 MB 输出直接送入模型。

---

## 12.6 验收标准

* 没有专用 Adapter 的工具也可通过通用 Tool Skill 执行。
* 常用工具优先使用 XML、JSON、JSONL 等机器格式。
* 完整结果可追溯。
* 解析器失败时自动回退到原始文本摘要。

---

# 13. 模块八：RiftX Runner

## 13.1 职责

Runner 是 Host 执行层，负责：

* 启动进程；
* 管理工作目录；
* 注入环境变量；
* 捕获 stdout 和 stderr；
* 管理 PID 和进程组；
* 发送 Signal；
* 处理超时；
* 管理 PTY；
* 报告执行状态；
* 保存本地日志。

---

## 13.2 部署方式

### P0：Local Runner

Runner 和 Temporal Worker 位于同一台机器，但使用独立模块。

### 后续：Runner Daemon

```text
riftx-runner
```

独立运行并通过本机 HTTP、Unix Socket 或远程控制连接接受任务。

---

## 13.3 Runner API

```text
POST /executions
GET  /executions/{id}
POST /executions/{id}/cancel
GET  /executions/{id}/output

POST /terminals
WS   /terminals/{id}
DELETE /terminals/{id}
```

---

## 13.4 工作目录

默认：

```text
~/.local/share/riftx/runs/{run_id}/
├── workspace/
├── executions/
├── terminals/
├── artifacts/
├── findings/
└── reports/
```

用户可以将 Run 绑定到现有目录：

```yaml
workspace_mode: existing
workspace_path: /home/kali/projects/customer-a
```

---

## 13.5 环境变量

环境变量合并顺序：

```text
Host Environment
→ Node Environment
→ Tool Environment
→ Run Environment
→ Execution Environment
```

后面的值覆盖前面的值。

支持：

```yaml
environment_mode: inherit
```

或：

```yaml
environment_mode: clean
```

专业本机模式默认 `inherit`。

---

## 13.6 验收标准

* Runner 可独立测试，不依赖 LLM。
* 每次执行都有唯一 ID。
* 进程结束后 exit code 和输出不会丢失。
* 取消操作可以终止整个进程组。
* API 重试不会重复启动同一 execution key。

---

# 14. 模块九：Executor

## 14.1 Direct Process Executor

默认执行器。

接口：

```python
class ProcessExecutionRequest(BaseModel):
    execution_key: str
    argv: list[str]
    cwd: str
    env: dict[str, str]
    timeout_seconds: int | None
```

实现：

```python
process = await asyncio.create_subprocess_exec(
    *request.argv,
    cwd=request.cwd,
    env=environment,
    stdin=asyncio.subprocess.PIPE,
    stdout=stdout_file,
    stderr=stderr_file,
    start_new_session=True,
)
```

Unix 使用进程组处理取消：

```python
os.killpg(process.pid, signal.SIGTERM)
```

超时后：

```text
SIGTERM
→ 等待宽限期
→ SIGKILL
```

---

## 14.2 Shell Executor

Shell Executor 用于：

* 管道；
* 重定向；
* Shell 变量；
* 循环；
* `source`；
* 多命令组合。

不要依赖 `shell=True` 自动选择 Shell。

显式执行：

### Bash

```python
["/bin/bash", "-lc", script]
```

### Zsh

```python
["/bin/zsh", "-lc", script]
```

### PowerShell

```python
[
    "pwsh.exe",
    "-NoLogo",
    "-NoProfile",
    "-Command",
    script,
]
```

### cmd

```python
["cmd.exe", "/d", "/s", "/c", script]
```

---

## 14.3 PTY Executor

用于：

* msfconsole；
* ssh；
* evil-winrm；
* smbclient；
* mssqlclient；
* 远程 Shell；
* REPL；
* 需要 TTY 检测的程序。

Unix：

```text
pty.openpty
或 pexpect
```

Windows：

```text
ConPTY
```

ConPTY 是 Windows 官方提供的伪控制台机制，用于让外部终端宿主字符模式控制台程序。

PTY 接口：

```python
class TerminalSession:
    async def start(...)
    async def write(data: bytes)
    async def read(cursor: int)
    async def resize(cols: int, rows: int)
    async def interrupt()
    async def close()
```

---

## 14.4 PTY 所有权

```python
class TerminalOwner(str, Enum):
    AGENT = "agent"
    USER = "user"
    SHARED = "shared"
```

用户点击“接管”后：

```text
AGENT → USER
```

Agent 不再写入终端，只读取输出。

用户释放后：

```text
USER → AGENT
```

任何时刻只允许一个主动写入方。

---

## 14.5 PTY 持久性限制

原生 PTY Session 在 Runner 崩溃后通常无法可靠重连。

V2 处理方式：

* 普通进程输出重定向到文件，可恢复监控；
* 原生 PTY Runner 崩溃后标记为 `LOST`；
* Unix 可选支持 `tmux` Backend；
* 不将 PTY 完整持久化作为首版目标。

---

# 15. 模块十：Process Supervisor

## 15.1 职责

Process Supervisor 管理所有本机执行实例：

```text
execution_id
execution_key
pid
process_group_id
status
argv
cwd
start_time
end_time
exit_code
stdout_path
stderr_path
```

---

## 15.2 状态恢复

Runner 启动时读取本地状态：

```text
RUNNING + PID 存在
→ 检查进程是否仍存活

RUNNING + PID 不存在
→ 标记 LOST

EXITED
→ 保持结果
```

不能仅凭 PID 判断身份，应额外记录：

* 启动时间；
* 可执行文件；
* 命令摘要；
* 平台可用时记录进程创建时间。

---

## 15.3 输出游标

每个输出流使用字节游标：

```json
{
  "stdout_cursor": 4096,
  "stderr_cursor": 512
}
```

客户端请求：

```text
从 cursor=4096 继续读取
```

这样 SSE、WebUI 和 Agent 都不需要重复读取完整日志。

---

# 16. 模块十一：Approval Mode

## 16.1 模式

```python
class ApprovalMode(str, Enum):
    AUTO = "auto"
    BALANCED = "balanced"
    MANUAL = "manual"
```

### AUTO

所有工具自动执行。

### BALANCED

根据 Tool/Skill 的审批等级决定：

```python
class ApprovalLevel(str, Enum):
    NEVER = "never"
    SENSITIVE = "sensitive"
    ALWAYS = "always"
```

### MANUAL

所有具有外部执行效果的工具都暂停等待用户确认。

---

## 16.2 审批内容

WebUI 和 CLI 应显示：

```text
工具
完整命令
工作目录
目标摘要
环境变量变更
Agent 给出的简短理由
```

用户可以：

```text
批准一次
本次 Run 始终批准该 Tool
拒绝
拒绝并提供原因
```

---

## 16.3 实现

优先使用 Agents SDK HITL：

```text
Tool declares needs_approval
→ Agent Run 返回 interruption
→ 保存 RunState
→ Temporal Workflow 等待
→ 用户批准
→ 恢复 RunState
```

首版不实现独立 Auto-review Agent。

后续可新增：

```text
approval_mode: auto_review
```

但不得改变现有 Tool、Runner 和 Workflow 接口。

---

# 17. 模块十二：轻量 Scope 管理

## 17.1 定位

Scope 是 RiftX 的任务约束和上下文，不建设复杂策略服务。

Scope 保存：

```text
CIDR
IP
Domain
URL Prefix
资产标签
排除项
时间范围
```

---

## 17.2 执行方式

对于结构化 Skill：

```text
目标参数可明确提取
→ ScopeGuard 检查
```

对于原始 Shell：

```text
无法可靠解析全部语义
→ 在 BALANCED/MANUAL 模式中展示命令并由用户确认
```

ScopeGuard 不是 Host Sandbox，也不宣称可以阻止所有绕过。

---

# 18. 模块十三：Persistence

## 18.1 主要表

### `engagements`

```text
id
name
description
authorization_reference
created_at
updated_at
```

### `runs`

```text
id
engagement_id
node_id
objective
success_criteria_json
entry_points_json
scope_json
status
approval_mode
workspace_path
temporal_workflow_id
created_at
started_at
finished_at
```

### `agent_messages`

```text
id
run_id
role
message_type
content
sequence
created_at
```

### `agent_checkpoints`

```text
id
run_id
sdk_state
status
created_at
resolved_at
```

### `tool_calls`

```text
id
run_id
agent_step_id
tool_id
skill_id
arguments_json
approval_status
execution_id
created_at
```

### `executions`

```text
id
execution_key
run_id
node_id
executor_type
argv_json
command_text
cwd
env_diff_json
status
pid
exit_code
stdout_path
stderr_path
started_at
finished_at
```

### `terminal_sessions`

```text
id
run_id
execution_id
status
owner
cols
rows
created_at
closed_at
```

### `approvals`

```text
id
run_id
tool_call_id
status
reason
decided_by
created_at
decided_at
```

### `run_events`

```text
id
run_id
sequence
event_type
payload_json
created_at
```

### `artifacts`

```text
id
run_id
execution_id
name
path
mime_type
sha256
size
description
created_at
```

### `findings`

```text
id
run_id
title
severity
status
description
evidence_json
recommendation
created_at
updated_at
```

---

## 18.2 Repository 模式

定义接口：

```python
class RunRepository(Protocol):
    async def create(...)
    async def get(...)
    async def update_status(...)
```

Infrastructure 实现：

```text
SQLAlchemyRunRepository
```

禁止 Agent、API Route 和 Workflow 直接编写 SQL。

---

# 19. 模块十四：Event System

## 19.1 统一事件

建议事件名：

```text
run.created
run.started
run.status_changed
run.paused
run.resumed
run.completed
run.failed

agent.message
agent.plan_updated
agent.cycle_started
agent.cycle_completed

tool.requested
tool.approval_required
tool.approved
tool.rejected

execution.started
execution.output
execution.completed
execution.failed

terminal.opened
terminal.owner_changed
terminal.closed

finding.created
artifact.created
report.generated
```

---

## 19.2 Event 写入原则

任何对 UI 有意义的状态变化：

1. 先写业务状态；
2. 再写 `run_events`；
3. 再推送 SSE。

SSE 断线不会导致事件丢失，因为数据库是事实来源。

---

# 20. 模块十五：WebUI

## 20.1 页面

### Dashboard

展示：

* 活跃 Run；
* 等待审批；
* 最近完成；
* 节点在线状态。

### New Run

配置：

* Objective；
* Success Criteria；
* Entry Points；
* Scope；
* Execution Node；
* Approval Mode；
* Workspace。

### Run Detail

建议布局：

```text
左侧：任务阶段和计划
中间：Agent 对话与事件时间线
右侧：工具、审批、Finding、Artifact
底部：可展开终端
```

Tab：

```text
Overview
Timeline
Agent
Tool Calls
Terminal
Findings
Artifacts
Report
```

### Tools

展示：

* 当前节点；
* 工具配置；
* 实际路径；
* 版本；
* 可用状态；
* Capability；
* 刷新按钮。

### Nodes

展示：

* OS；
* 架构；
* Runner 状态；
* Shell；
* 工作目录；
* Tool 数量；
* 当前运行任务。

---

## 20.2 Terminal

使用 xterm.js：

* WebSocket 双向通信；
* 支持 resize；
* 支持 Ctrl+C；
* 支持用户接管；
* 显示当前 Owner；
* 支持只读查看 Agent 会话。

---

## 20.3 WebUI 开发指导

* 服务端状态全部通过 TanStack Query；
* SSE 事件只更新缓存，不重复维护完整业务状态；
* Terminal 独立使用 WebSocket；
* 不把 Run 状态只保存在浏览器内；
* 刷新页面后从 API 恢复；
* 不依赖浏览器维持任务执行。

---

# 21. 模块十六：RiftX CLI

## 21.1 CLI 形态

### 普通命令

```bash
riftx serve
riftx worker
riftx runner

riftx run create
riftx run list
riftx run show RUN_ID
riftx run watch RUN_ID
riftx run pause RUN_ID
riftx run resume RUN_ID
riftx run cancel RUN_ID

riftx tools list
riftx tools show nmap
riftx tools reload
riftx tools doctor

riftx approve APPROVAL_ID
riftx reject APPROVAL_ID

riftx attach SESSION_ID
riftx web
```

### 交互模式

直接运行：

```bash
riftx
```

进入类似 Claude Code 的会话：

```text
RiftX > 验证 10.10.10.20 上的 Web 服务是否存在可利用漏洞
```

---

## 21.2 Slash Commands

```text
/new
/resume
/runs
/status
/tools
/node
/model
/mode
/plan
/approve
/reject
/terminal
/takeover
/release
/pause
/continue
/cancel
/compact
/web
/exit
```

---

## 21.3 CLI 开发指导

使用：

```text
Typer：普通子命令
prompt_toolkit：交互输入、历史、补全
Rich：Markdown、事件、状态和表格
```

CLI 不直接访问数据库和 Runner，全部调用 RiftX API。

这样 CLI 和 WebUI 行为完全一致。

---

# 22. 模块十七：Artifact、Finding 和 Report

## 22.1 Artifact

任何工具输出都可被注册为 Artifact：

```text
扫描 XML
JSONL
截图
PoC 文件
日志
导出数据
报告附件
```

Artifact 保存哈希，避免内容变化后无法追溯。

---

## 22.2 Finding

Finding 字段：

```text
标题
严重等级
受影响资产
描述
证据
复现步骤
影响
修复建议
状态
```

Agent 可以创建草稿，但用户可在 WebUI 修改。

---

## 22.3 Report

Report Agent 只读取：

* Objective；
* Scope；
* Run Summary；
* Findings；
* Artifact 摘要；
* 关键执行记录。

不应把所有原始终端输出直接输入报告模型。

输出格式：

```text
Markdown
HTML
JSON
```

PDF 和 DOCX 放在后续导出阶段。

---

# 23. 配置体系

## 23.1 主配置

```yaml
server:
  host: 127.0.0.1
  port: 8787

database:
  url: sqlite+aiosqlite:///~/.local/share/riftx/riftx.db

temporal:
  target: 127.0.0.1:7233
  namespace: default
  task_queue: riftx-runtime

runner:
  mode: local
  endpoint: http://127.0.0.1:8790

execution:
  policy: open
  default_timeout: 1800
  environment_mode: inherit

workspace:
  root: ~/.local/share/riftx/runs

approval:
  default_mode: balanced
```

---

## 23.2 配置优先级

```text
默认配置
< 系统配置
< 用户配置
< 环境变量
< CLI 参数
< Run 配置
```

---

## 23.3 Secret

API Key 不写入普通 YAML。

支持：

* 环境变量；
* OS Keyring；
* 外部 Secret Provider。

---

# 24. 项目目录

```text
riftx/
├── pyproject.toml
├── README.md
├── alembic.ini
├── migrations/
├── configs/
│   ├── riftx.example.yaml
│   └── tools.example.yaml
├── src/riftx/
│   ├── domain/
│   ├── application/
│   ├── api/
│   ├── agent/
│   ├── models/
│   ├── runtime/
│   ├── runner/
│   ├── executors/
│   ├── terminal/
│   ├── tools/
│   ├── skills/
│   ├── approvals/
│   ├── scope/
│   ├── events/
│   ├── artifacts/
│   ├── findings/
│   ├── reports/
│   ├── persistence/
│   ├── config/
│   ├── cli/
│   └── telemetry/
├── web/
│   ├── src/
│   └── package.json
└── tests/
    ├── unit/
    ├── integration/
    ├── temporal/
    ├── runner/
    ├── terminal/
    └── e2e/
```

---

# 25. 部署模式

## 25.1 Standalone Local

适合个人红队成员：

```text
RiftX API
Temporal Worker
Local Runner
SQLite
本地 Temporal 开发服务
React 静态资源
```

绑定：

```text
127.0.0.1
```

不要求 Docker。

---

## 25.2 Persistent Local

适合长期使用：

```text
RiftX API
RiftX Worker
RiftX Runner
PostgreSQL
独立 Temporal Service
```

仍可全部运行在一台物理机上。

---

## 25.3 Team Server + Remote Runner

```text
中心 RiftX Server
        │
        ├── Kali Runner A
        ├── Windows Runner B
        └── 内网测试节点 C
```

远程 Runner 后续采用出站连接注册到 Server，避免要求用户设备开放入站端口。

---

# 26. 平台支持路线

## 第一优先级

```text
Linux x86_64
Kali
Ubuntu
Debian
bash / zsh
```

## 第二优先级

```text
macOS ARM64/x86_64
zsh
```

## 第三优先级

```text
Windows
PowerShell 7
Windows PowerShell
ConPTY
cmd 兼容
```

接口从第一天跨平台，但实现和测试按顺序推进。

---

# 27. 开发里程碑

## V2-M1：Domain 和 Persistence

交付：

* Domain Model；
* SQLAlchemy Repository；
* SQLite；
* Alembic；
* Run/Event 数据模型。

验收：

* 可创建 Run；
* 可写入事件；
* 重启后数据存在。

---

## V2-M2：Host Runner

交付：

* Direct Process Executor；
* Shell Executor；
* Process Supervisor；
* stdout/stderr 文件；
* 取消和超时。

验收：

* 可执行 Nmap；
* 可实时读取输出；
* 可取消进程组；
* 重复 execution key 不重复启动。

---

## V2-M3：Tool 和 Skill Registry

交付：

* `tools.yaml`；
* Tool 检测；
* Capability；
* Generic Tool Skill；
* Generic Shell Skill。

验收：

* 修改配置后可热重载；
* 不可用工具不会暴露给 Agent；
* 自定义 Python 脚本可以注册。

---

## V2-M4：Agent Harness

交付：

* Primary Agent；
* Model Provider；
* Session；
* 动态工具；
* Agent Cycle；
* Finding Tool。

验收：

* Agent 可以根据 Objective 选择已配置工具；
* 工具缺失时可以换方案；
* 输出进入事件时间线。

---

## V2-M5：Temporal 集成

交付：

* `RiftXRunWorkflow`；
* Agent Cycle Activity；
* Signal/Query；
* Activity 重试；
* Run 暂停恢复。

验收：

* Worker 重启后 Run 能恢复；
* WebUI 关闭不影响 Run；
* Activity 重试不重复启动命令。

---

## V2-M6：WebUI 和 CLI

交付：

* Dashboard；
* New Run；
* Run Timeline；
* Tool 页面；
* CLI 交互模式；
* SSE。

验收：

* WebUI 和 CLI 可同时观察同一 Run；
* CLI 发出的消息会出现在 WebUI；
* 浏览器刷新后状态恢复。

---

## V2-M7：Approval 和 PTY

交付：

* 三种 Approval Mode；
* HITL 恢复；
* Unix PTY；
* Web Terminal；
* 用户接管。

验收：

* msfconsole 可以运行；
* 用户可以接管和释放；
* 审批后 Agent 从原状态继续。

---

## V2-M8：Finding、Artifact 和 Report

交付：

* Artifact 管理；
* Finding 编辑；
* Report Agent；
* Markdown/HTML 报告。

验收：

* 从 Run 自动生成结构化 Findings；
* 报告中的证据可以跳转到原始 Artifact。

---

## V2-M9：Remote Runner 和 Windows

交付：

* Runner 注册；
* Node 管理；
* 远程控制通道；
* PowerShell；
* ConPTY。

---

# 28. 测试计划

## 28.1 Unit Test

覆盖：

* 状态迁移；
* Tool 配置解析；
* Skill 选择；
* argv 构造；
* Scope 匹配；
* Approval 决策；
* 输出截断。

---

## 28.2 Runner Integration Test

不要依赖真实渗透工具，创建测试脚本：

```text
fake_success
fake_failure
fake_timeout
fake_large_output
fake_interactive
fake_child_process
```

验证：

* exit code；
* 超时；
* 取消；
* 子进程组；
* stdout/stderr；
* PTY；
* Unicode。

---

## 28.3 Temporal Test

覆盖：

* Workflow 暂停恢复；
* Signal；
* Activity Retry；
* Cancel；
* Replay；
* 审批等待；
* 重复 execution key。

---

## 28.4 Tool Adapter Golden Test

保存固定输出样本：

```text
nmap XML
nuclei JSONL
masscan JSON
```

解析结果与 Golden File 比较。

---

## 28.5 E2E Test

完整链路：

```text
创建 Run
→ Agent 选择测试 Tool
→ Runner 执行
→ 事件进入 SSE
→ 创建 Finding
→ 完成 Run
→ 生成报告
```

---

# 29. 主要风险和处理方式

## 29.1 Host 权限过大

这是产品选择，不是实现缺陷。

处理方式：

* UI 显示当前用户；
* 显示 Host Execution；
* 提供 AUTO/BALANCED/MANUAL；
* 不宣称命令处于隔离环境。

---

## 29.2 环境不可复现

处理方式：

每次执行记录：

* executable path；
* tool version；
* argv；
* cwd；
* env diff；
* OS；
* exit code；
* stdout/stderr。

---

## 29.3 Raw Shell 绕过 Tool Registry

在 `execution_policy: open` 下这是预期行为。

需要严格限制时切换：

```yaml
execution_policy: registered_only
```

---

## 29.4 Agent Activity 粒度过大

处理方式：

* Agent Cycle 设置边界；
* 每次 Cycle 持久化消息和 Tool Call；
* Runner 使用 execution key 幂等；
* 不把整个 Run 放在单个 Activity 中。

---

## 29.5 PTY 无法完全持久恢复

处理方式：

* 首版明确限制；
* Runner 崩溃后标记 LOST；
* Unix 后续提供可选 tmux Backend；
* 普通长任务优先使用非 PTY Executor。

---

## 29.6 大型输出撑爆模型上下文

处理方式：

* 完整输出写文件；
* Agent 只接收摘要和片段；
* 支持按 cursor 继续读取；
* 优先使用结构化输出；
* 自动 Context Compaction。

---

## 29.7 Temporal 增加部署复杂度

处理方式：

* 开发和单用户模式提供本地启动方式；
* 团队模式连接外部 Temporal；
* Temporal 只用于 Run 生命周期，不滥用 Workflow。

---

# 30. 当前版本明确不做

以下能力不进入 RiftX V2 首版：

```text
自动安装安全工具
统一工具版本管理
Docker Sandbox
MicroVM
Kubernetes
复杂 Policy Engine
OPA
独立 Auto-review Agent
Kafka
Redis/Celery
多租户计费
完整 Windows 支持
PTY 崩溃后无损恢复
所有工具的结构化解析
```

---

# 31. 最终产品形态

RiftX 最终对用户呈现三个入口：

## WebUI

```bash
riftx serve
```

浏览器访问：

```text
http://127.0.0.1:8787
```

## CLI

```bash
riftx
```

进入交互式 Agent。

## 自动化 API

```bash
curl -X POST http://127.0.0.1:8787/api/v1/runs
```

三种入口使用同一个：

```text
Run
Agent Harness
Temporal Workflow
Tool Registry
Runner
Database
```

最终定位：

> RiftX 不是渗透测试工具发行版，而是运行在专业人员现有攻击环境之上的智能编排层。

其最小闭环为：

```text
用户定义目标
→ Agent 查看当前工具
→ Agent 选择并调用本机 CLI
→ Runner 管理进程和终端
→ Temporal 保证 Run 可恢复
→ WebUI/CLI 展示和控制
→ Agent 整理 Finding 和报告
```

最先落地的关键不是 Temporal 或多 Agent，而是 **Runner、Tool Registry 和统一 Execution 数据模型**；这三部分确定后，其余模块才能稳定地接在上面。
