# RiftX 3.0 — RiftX Code Audit 开发目标与实施规格

> 状态：Proposed / 3.0 开发权威目标
>
> 文档日期：2026-08-02（Asia/Shanghai）
>
> RiftX 基线：496e260f3cb1f18ce485c5f706d20d8352d6a398
>
> 目标版本：3.0.0
>
> 产品主功能名称：RiftX Code Audit

## 0. 文档用途

本文档是 RiftX 3.0 Code Audit 的产品、架构和工程实施目标。它应当能够直接作为 Codex 的长周期开发任务输入，并用于判断每一阶段是否真正完成。

Codex 执行本计划时必须遵守以下规则：

1. 先完成当前阶段的领域契约、失败语义和测试，再进入下一阶段。
2. 每项任务必须同时交付实现、迁移、API/CLI 或 UI 接线、自动化测试和必要文档；只创建空壳不算完成。
3. 不修改现有 RiftXRunWorkflow 的历史确定性路径。Code Audit 使用独立 Workflow，避免破坏 2.x Temporal replay。
4. 数据库与不可变 Artifact 是权威状态；Temporal History、模型上下文、SARIF 和报告都不是唯一事实源。
5. Agent 输出只是不可信提案。稳定 ID、Coverage、严重性、扫描终态和历史比较必须由 RiftX 代码裁决。
6. 所有 Agent 相关测试与运行都必须使用 Conda 的 agent 环境。
7. 每完成一个里程碑，在 docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md 记录任务、变更文件、测试命令、结果和剩余风险。
8. 如果实现中发现本文档与现有安全不变量冲突，优先保持 fail-closed，并先更新 ADR 和本文档，不得静默降低安全标准。
9. 不引入 Codex Security 的包、CLI、运行时、插件、MCP 服务、账号依赖、代码、Prompt、Skill、Schema、测试、示例或品牌资产。
10. 不以“模型说完成了”“没有发现漏洞”或“进程已经退出”作为 Coverage、扫描成功或安全停止证明。

## 1. 最终产品结论

RiftX 3.0 的主功能是一个完全由 RiftX 掌握的、持久化的混合式代码安全审计平台：

- 确定性分析器负责可复现的事实、规则信号、代码结构、依赖和配置检查。
- 多 Agent 负责威胁建模、跨文件推理、候选发现、反证、验证计划和攻击路径分析。
- Temporal 负责长任务、并行工作、恢复、暂停、取消和阶段门禁。
- Runner 与审批系统负责扫描器、构建、测试、PoC 和修复验证的受控执行。
- RiftX 数据库负责 Snapshot、Coverage、Signal、Evidence、Decision、Finding、Baseline 和历史。
- WebUI 与 CLI 只投影权威状态，不拥有扫描状态。

RiftX Code Audit 不是：

- 一个普通 Skill；
- 一次开放式“让模型阅读仓库并找漏洞”的聊天；
- Codex Security 的替代外壳或行为兼容克隆；
- 单纯的 Semgrep/CodeQL/SARIF 查看器；
- 对“仓库安全”或“无漏洞”的保证；
- 在宿主环境直接构建和运行不可信仓库的功能。

## 2. 3.0 范围

### 2.1 3.0 GA 必须交付

1. 本地 Git 仓库、指定 revision、工作区快照和 base/head diff 的不可变审计目标。
2. Standard、Deep、Diff 三种扫描模式。
3. Git-aware 文件清单、语言/依赖/入口面清单和显式 Scope Ledger。
4. RiftX 原生 Detector 接口、确定性基础检测器和通用 SARIF 导入适配器。
5. 结构化威胁模型、分区发现、独立反证、静态验证和攻击路径分析。
6. 可恢复的独立 RiftXCodeAuditWorkflow、持久 Work Item 和 Deep Child Workflow。
7. 内容寻址 Evidence、Decision Ledger、Coverage Closure 和封存清单。
8. 跨扫描稳定 Finding 身份、Occurrence、Baseline 和 new/persisting/mitigated/resolved/regressed/reintroduced/unknown 比较。
9. 静态验证默认可用；动态验证必须通过独立隔离能力和审批。
10. Markdown、HTML、JSON、SARIF 2.1.0 输出。
11. Code Audit 一级导航、创建页、审计总览、审计详情、Finding 详情、Coverage、Threat Model、Evidence、Timeline、报告和历史比较。
12. 完整 CLI、SSE 实时事件、观测指标、预算和发布门禁。
13. 针对 Python 与 TypeScript/JavaScript 的 Tier A 质量门禁；其他语言按明确能力等级报告。
14. 修复建议和隔离 Retest。自动改动必须在临时 worktree 中、显式触发且可审阅；不得直接修改用户工作区。

### 2.2 明确不属于 3.0 GA

- Codex Security 的任何 Provider 或兼容层。
- 多租户、远程多用户 RBAC 或把当前 Control Plane 暴露到公网。
- 自动提交、自动推送、自动创建或合并 PR。
- 在没有隔离证明的 macOS/Windows 宿主上执行不可信构建或 PoC。
- 自动联网安装任意依赖。
- 对所有语言宣称完整 AST/CFG/污点分析能力。
- 二进制逆向、移动应用、固件或大型单体二进制审计。
- 将模型推理过程或 chain-of-thought 暴露到 UI、Event 或 Report。

### 2.3 语言能力分级

| 等级 | 3.0 定义 | GA 目标 |
| --- | --- | --- |
| Tier A | 原生结构解析、代码位置、规则、有限 source-to-sink、Agent 与完整评测 | Python、TypeScript、JavaScript |
| Tier B | 文件/依赖/配置清单、SARIF/外部检测器、Agent 审阅；不宣称完整原生数据流 | Go、Rust、Java |
| Tier C | 安全文本读取、清单与通用检测器；Coverage 明确标记能力缺口 | 其他文本语言 |
| Unsupported | 二进制、超限、无法解码或被策略排除 | 显式记录，不得静默跳过 |

### 2.4 从公开 Code Security 方法中吸收什么

本项目只吸收通用安全审计思想，并用 RiftX 自有架构重新实现：

| 可取思想 | RiftX 3.0 的独立实现 |
| --- | --- |
| 威胁模型先于漏洞搜索 | 结构化 ThreatModelPacket，所有边界和假设绑定 Scope/Evidence |
| Discovery、Validation、Attack Path 分离 | 独立 Phase、角色和 Decision Ledger，发现者不能验证自己 |
| 文件、工作与候选账本 | 数据库中的 ScopeUnit、WorkItem、Signal、Evidence、Decision、Closure |
| Deep 的独立重复发现与集中归并 | Temporal Child Workflow、worker 隔离、确定性 novelty reducer、可恢复 Epoch |
| 连续无新增后的饱和停止 | 最小审阅次数、连续无新 Cluster、高风险 gap 和预算共同裁决 |
| Finding 与一次扫描位置分离 | CodeFindingIdentity 与 CodeFindingOccurrence |
| 结果契约、封存和 SARIF | RiftX 自有版本化 Schema、Artifact digest、Sealed Manifest、SARIF 2.1.0 |
| Diff 聚焦变更但理解全仓边界 | base/head Snapshot、change-impact graph、hunk/edge Closure |
| 扫描历史、误报与比较 | Baseline、Triage、new/persisting/mitigated/resolved/regressed/reintroduced/unknown |
| 动态 PoC 与静态 Proof 互补 | Approval + AuditSandbox；不可执行时保留明确 proof_gap |

明确不吸收：

| 不采用的做法 | RiftX 处理 |
| --- | --- |
| 主要依赖 LLM 自由阅读来代替 SAST | 原生/外部确定性 Detector 与 Agent 混合 |
| 用模型自报“读完”作为 Coverage | Snapshot reader receipt 与 Closure Validator |
| 硬编码或静默排除目录 | 每个对象有 included/excluded/deferred 决议 |
| 在继承宿主环境的进程中运行目标代码 | clean、只读、默认禁网的独立 Sandbox |
| 不可恢复的内存型 Deep coordinator | Temporal + SQL WorkItem + Continue-As-New |
| 完全由模型去重和跨扫描匹配 | versioned fingerprint、确定性聚类、可审计 alias |
| 复制 CLI、Workbench、Prompt、Skill 或 Runtime | 全部由 RiftX 自有 API、CLI、WebUI 与 Agent 契约实现 |

本文的工程定位是 **independent reimplementation（独立重新实现）**，不对外宣称法律意义的 strict clean-room：参与设计的人员/Agent 已分析公开实现思想，单靠包名扫描不能证明访问隔离。若未来确需作 clean-room 声明，必须另立经法律确认的流程：reference/requirements 与未接触上游的 implementation 两组人员、访问 ACL/日志、去表达化 requirements-only handoff、每个 contract/fixture/commit provenance attestation，以及自动相似性和人工版权复核。在该流程完成前，产品只使用 RiftX Code Audit 品牌，不声称隶属、兼容或获得上游背书；任何实际代码复用都走单独许可证/NOTICE/商标/专利审核，不属于本文方案。

## 3. 不可变产品原则

### CA-INV-001：零上游运行依赖

源代码、SBOM、前端 bundle、Python/Node 依赖和网络调用中不得出现 Codex Security 的运行时依赖或专用端点。允许在设计历史中说明曾研究公开项目，但其实现不得进入产品。

### CA-INV-002：扫描对象不可变

Audit 一旦开始，Snapshot、目标 revision、base/head、Scope、规则包、Detector 配置、模型配置、验证策略和预算摘要均不可修改。需要变更时创建新 Audit。

### CA-INV-003：原仓库只读

Snapshot 创建后，所有读取必须通过 Snapshot ID。扫描、验证、修复和报告都不得写入原仓库。修复使用独立临时 worktree/overlay。

### CA-INV-004：模型无裁决权

模型不能直接：

- 宣布扫描完成；
- 设置 Coverage 百分比；
- 生成稳定 Finding ID；
- 把 Candidate 直接变成 Confirmed Finding；
- 自由决定最终严重性；
- 批准命令、扩大 Scope 或开启网络；
- 把旧 Finding 标记为 Resolved。

### CA-INV-005：Coverage 必须可证明

每个纳入、排除、延期或失败的文件、symbol、diff hunk、依赖、配置和 Detector 都必须有持久状态与原因。模型自报“已阅读”只能在存在真实读取 receipt 时计入。

### CA-INV-006：停止必须有证明

Audit 的取消沿用 RiftX 现有停止协议：先围栏新效果，再等待所有 Scanner、Execution、Browser、Target HTTP 和验证沙箱返回物理停止证明。无法证明时不得显示为已安全取消。

### CA-INV-007：完成与质量分离

扫描进程正常退出不代表扫描完整；没有 Confirmed Finding 不代表安全。最终 Closure 只能是：

- complete_under_declared_scope
- complete_with_policy_exclusions
- partial_capability
- partial_budget
- failed
- cancelled

`AuditClosurePolicy/v1` 按固定优先级聚合所有 Scope/Detector/WorkItem/Candidate/Approval/Execution 状态，不能因为某对象“已终态”就把 failed/deferred 当成功：

| 优先级 | 条件 | Closure / 生命周期 |
| --- | --- | --- |
| 0 | 任一效果停止或 Capsule destroy 尚无肯定证明 | 不产生 terminal Closure；保持 cleaning/cancelling/failing |
| 1 | Operator 已请求取消，且全资源 stop proof 完成 | cancelled / cancelled；保留已产生 Evidence 与缺口 |
| 2 | 合同/ownership/digest/integrity/security 违规、存储损坏、required Detector 执行或 parser fatal | failed / failed |
| 3 | 任一 required 工作因 wall/token/call/worker/Epoch/Artifact 等硬预算未完成 | partial_budget / completed_partial |
| 4 | required 能力 unsupported/unavailable、Sandbox/模型不可用、模型 `outcome_unknown`、required Candidate/receipt/validation deferred | partial_capability / completed_partial |
| 5 | 只有冻结前显式批准的 include/exclude/waiver 导致未分析；所有 included required 工作完成 | complete_with_policy_exclusions / completed |
| 6 | 所有 declared required 工作与 Evidence contract 完成，无上述缺口 | complete_under_declared_scope / completed |

`not_applicable` 只有冻结 profile 本来不要求该阶段时为中性；运行中临时跳过不能使用它。`waived/excluded` 只有合同冻结前的版本化 Policy/Operator reason 才能进入优先级 5；超限、工具缺失或失败不能伪装成 exclusion。Candidate `deferred` 若属于 required 永远阻止 complete。多个原因并存时取最高优先级结果并保留全部 reason codes。

Closure 与发布状态分开：core seal 后报告/打包失败不改写 Closure，但 Audit 为 `completed_partial/report_failed`，CLI 返回 partial exit；CLI 只有 Closure complete 且策略门禁通过返回 0，partial 返回 3，failed/API error 返回 1，cancelled 返回 5。所有 API/UI/CLI 映射由同一 Policy 投影，不各自推断。

### CA-INV-008：失败不丢证据

失败、超预算和取消必须保留已产生的 Snapshot、Receipt、Signal、Evidence 和 Execution provenance；报告必须清楚标记不完整，不能输出“零漏洞”。

### CA-INV-009：大对象不进入 Temporal History

Workflow 与 Activity 之间只传 ID、digest、小型状态和计数。源代码、SARIF、原始 Scanner 输出、候选集合和报告必须先持久化为数据库记录或 Artifact。

### CA-INV-010：报告可重建

最终 JSON、SARIF、Markdown、HTML 和封存清单必须能仅根据数据库与不可变 Artifact 重新生成，不依赖模型会话仍然存在。

## 4. 与 RiftX 2.x 的衔接

### 4.1 必须复用的现有能力

| 现有能力 | 当前位置 | 3.0 用法 |
| --- | --- | --- |
| Run 聚合与状态 | src/riftx/domain/run.py | 增加 RunKind；Audit 仍使用 Run 作为执行与控制信封 |
| Run 服务与 workspace | src/riftx/application/services/runs.py | 原子创建 Run 与 AuditScan；通用 Run 行为保持不变 |
| Temporal 持久流程 | src/riftx/temporal/workflow.py | 保持旧 Workflow；新增独立 Audit Workflow |
| Agent Engine | src/riftx/runtime/engine/ | 扩展 model-adapter-neutral 结构化输出，不把审计绑死到 SDK |
| Subagent 模型 | src/riftx/subagents/ | 复用 typed packet、范围和预算思想；Deep 并行改用 Child Workflow |
| Tool/Approval/Execution | src/riftx/tools/、src/riftx/application/services/ | Detector 与动态验证统一经过可归因效果边界 |
| Runner/cgroup 停止证明 | src/riftx/runner/、src/riftx/executors/ | 继续负责进程所有权；新增文件系统与网络沙箱层 |
| Artifact | src/riftx/domain/artifact.py | 保存 Manifest、原始输出、Evidence、SARIF 和报告 |
| 通用 Finding | src/riftx/domain/finding.py | 保持通用 Run Finding；Code Finding 使用独立稳定身份和 Occurrence |
| Report | src/riftx/application/services/reports.py | 新增 Audit 专用 Source/Composer，不削弱现有报告 |
| Run Event/SSE | src/riftx/domain/event.py、src/riftx/api/routes/events.py | Audit 事件写入关联 Run 的事件流 |
| API Policy | src/riftx/api/policy.py | 新路由逐条登记能力并 fail-closed |
| Web 设计系统 | apps/web/DESIGN.md、apps/web/src/pixel-theme.css | 保持 Blue Team Cartridge 视觉与安全状态语义 |
| CLI | src/riftx/cli/ | 新增 audit 子命令，CLI 只调用 Control Plane API |

### 4.2 不能直接复用的部分

1. 现有 Finding 以 run_id 为中心，无法单独表达跨扫描稳定身份。
2. 现有 SubagentOrchestrator 在一次 Activity 内使用 asyncio.gather，不适合 Deep 长任务恢复。
3. 现有 cgroup 只证明进程树所有权，不提供源码只读、网络、凭据、syscall 或容器边界。
4. 现有 ScopeGuard 面向 IP/CIDR/domain/URL，不等同于代码路径 Scope。
5. 现有 AgentEngineRequest 没有审计所需的强类型输出契约。
6. 现有报告模型缺少 Snapshot、Coverage、CWE、代码流、Proof 和 Baseline。
7. 现有 RunDetailPage 不应被继续膨胀成 Code Audit 工作台；审计使用独立页面。

### 4.3 关键架构决策

- 增加 RunKind：general、code_audit。
- 一个 AuditScan 必须且只能关联一个 Run；Run 负责控制和效果，AuditScan 负责审计领域。
- Code Audit 不沿用通用 Run 的 waiting_user 首消息规则，但“持久化草稿”和“授权宿主执行”必须分成两个 API 效果：`POST /audits` 只创建 draft，Review 页再显式调用 `POST /audits/{id}/start`。WebUI 可以在一次确认交互中顺序完成两步，但不得让 durable-write 权限绕过 host-execution 授权。
- 旧 RiftXRunWorkflow 不做分支改造；新增 RiftXCodeAuditWorkflow。
- Audit 暂时继续使用关联 Run 的 Approval、Execution、Artifact 和 Event；API 对外使用 audit_id，不要求用户理解内部 run_id。
- 关联 Run 的 workspace_path 必须是 RiftX 管理的 Audit 输出工作区，绝不能指向被审计仓库；所有源码读取都通过 Snapshot ID 和只读映射。
- 新增 RunWorkflowControlRouter，按 RunKind 把 pause/resume/cancel/stop 信号发送到通用或 Audit Workflow；不能让现有 TemporalRunClient 错误寻找 riftx-run-{id}。
- code_audit Run 拒绝 message、compact、switch-model 和通用 terminal 等不适用操作；返回结构化 run_kind_operation_unsupported。
- 通用 Dashboard 默认只展示 general Run；Audit Run 通过 Code Audit 页面展示。Run API 返回 kind 并支持显式 kind filter。
- CodeFindingIdentity 表示跨 Snapshot 的逻辑问题；CodeFindingOccurrence 表示一次 Audit 的观察。
- 通用 Finding 作为兼容投影：每个已确认 Occurrence 可选创建一个同 Run 的通用 Finding，并保存双向 ID；Code Audit 真相仍以 Code Finding 域为准。

### 4.4 RunKindEffectPolicy

新增服务端 `RunKindEffectPolicy`，所有接受/间接解析 `run_id` 的 mutation 都在 API 依赖和 Application Service 内层各检查一次；未知 operation/kind/origin 默认拒绝。不能只维护 message/model/compact 三个特例。

| mutation family | general Run | code_audit Run |
| --- | --- | --- |
| generic message / compact / switch-model | 现有行为 | 禁止；`run_kind_operation_unsupported` |
| generic `/runs/{id}/pause|resume|cancel` | 现有 Workflow policy | 禁止；Audit 只能走 `/audits/{id}`，其中 cancel 为 HOST_CONTROL |
| generic cancel-current/cancel-execution | 现有行为 | 禁止 Operator 旁路；Audit Workflow/Validation action 按 audit ownership 处理 |
| terminal / browser / target HTTP capture / connector | 现有行为 | 禁止创建、操作与 capture |
| generic memory create/update/delete/pin | 现有行为 | 禁止；3.0 不把审计事实写入通用 Memory |
| generic Finding create/update | 现有行为 | 禁止；只允许内部 Occurrence projection 与 `/code-findings/.../triage` |
| generic Artifact register | 现有行为 | 禁止任意 path 注册；只允许 Audit/Runner bounded ingest |
| generic Run report generate | 现有行为 | 禁止；只允许 Audit distribution revision API |
| Approval approve/reject | 现有行为 | 仅 audit_id/plan_digest ownership 校验后的 Audit approval；禁止 Run grant/approve_for_run |
| runner Approval/Execution completion callback | 通用 Workflow router | 按 RunKind 路由 Audit Workflow，并校验 Audit/plan/execution ownership |
| read-only Run/Event/Execution/Artifact | 现有行为 | 可读安全投影；restricted access class、Audit ownership 与 UI redirect 仍生效 |

实现时建立 machine-readable operation catalog，覆盖 policy.py 中所有 DURABLE_WRITE/WORKFLOW_CONTROL/HOST_EXECUTION/HOST_CONTROL route、内部 Service mutation 和 Runner callback。测试枚举现有 route names 与 Service commands：每项必须声明 allowed kinds、origin、required RouteEffect 和 Audit alternative；新增未登记 mutation 使 CI 失败。特别证明 generic cancel 的 WORKFLOW_CONTROL 权限不能触发需要 HOST_CONTROL 的 Audit cancel。

## 5. 总体架构

~~~mermaid
flowchart LR
    UI["WebUI / CLI"] --> API["FastAPI Control Plane"]
    API --> AuditService["Audit Application Service"]
    AuditService --> DB["SQLite + Alembic"]
    AuditService --> Temporal["RiftXCodeAuditWorkflow"]
    Temporal --> Snapshot["Snapshot + Inventory"]
    Temporal --> Detectors["Deterministic Detectors"]
    Temporal --> Agents["Typed Audit Agents"]
    Temporal --> Validator["Validation Coordinator"]
    Detectors --> Runner["Audit Runner / Sandbox"]
    Validator --> Runner
    Snapshot --> Store["Immutable Artifact Store"]
    Detectors --> Ledger["Signal / Evidence Ledgers"]
    Agents --> Ledger
    Validator --> Ledger
    Ledger --> Findings["Finding Identity + Occurrence"]
    Findings --> Closure["Coverage + Closure Validator"]
    Closure --> Outputs["JSON / SARIF / MD / HTML"]
    Outputs --> Store
    DB --> UI
~~~

### 5.1 新增模块建议

~~~text
src/riftx/domain/audit.py
src/riftx/domain/code_finding.py
src/riftx/application/ports/audits.py
src/riftx/application/services/audits.py
src/riftx/application/services/audit_findings.py
src/riftx/persistence/audit_repositories.py
src/riftx/persistence/audit_mappers.py
src/riftx/audit/
  contracts.py
  snapshot.py
  inventory.py
  scope.py
  fingerprints.py
  reducer.py
  closure.py
  risk.py
  sarif.py
  reports.py
  detectors/
    base.py
    registry.py
    native/
    command.py
    sarif.py
  agents/
    contracts.py
    instructions/
    tools.py
    runner.py
  validation/
    policy.py
    sandbox.py
    capsule.py
src/riftx/temporal/audit_models.py
src/riftx/temporal/audit_workflow.py
src/riftx/temporal/audit_activities.py
src/riftx/temporal/audit_child_workflow.py
src/riftx/api/schemas/audits.py
src/riftx/api/routes/audits.py
src/riftx/cli/audit.py
apps/web/src/pages/AuditsPage.tsx
apps/web/src/pages/NewAuditPage.tsx
apps/web/src/pages/AuditDetailPage.tsx
apps/web/src/pages/CodeFindingPage.tsx
apps/web/src/components/audit/
tests/audit/
tests/fixtures/audit_repositories/
~~~

不创建任何 codex_security、codex-security、openai_security 或兼容命名的生产模块。

## 6. 权威状态与阶段

### 6.1 Audit 生命周期

~~~text
draft
  -> queued
  -> preflighting
  -> snapshotting
  -> running
  -> finalizing
  -> cleaning
  -> sealing_core
  -> reporting
  -> packaging
  -> completed | completed_partial

running <-> waiting_approval
running -> pausing -> paused -> running

任意非终态：
  -> cancelling -> cleaning -> sealing_core -> reporting -> packaging -> cancelled
  -> failing -> cleaning -> sealing_core -> reporting -> packaging -> failed
~~~

Audit 生命周期是产品投影；Run 仍是底层控制状态。映射必须由一个服务集中维护，不能由 UI 推断。即使错误发生在早期，进入 `failed/cancelled` 前也必须执行第 14.6 节 cleanup/stop audit，并对已产生事实走 partial terminal core seal/报告/manifest；若停止证明不完整则保持 `failing/cancelling/cleaning` 可控制非终态，不能直达失败终态。

`AuditScan.terminal_outcome` 是独立于 lifecycle/publication 的持久判定，固定为
`complete | partial | failed | cancelled`。正常分析只能在 `validate_closure ->
finalizing` 时选择 complete/partial；进入 cancelling/failing 分别原子冻结
cancelled/failed。该值在 cleaning、core seal、报告失败与发布重试期间保持不变，恢复时不得
依据当前 lifecycle 重新猜测。Closure 只能在 `cleaning/cleanup` 中、所有效果停止证明肯定后
记录；一经记录不可改写。停止证明前发生的 integrity/security 失败可把尚未记录 Closure 的
complete/partial 单调升级为 failed，cancelled 不得降级成 failed。

### 6.2 Audit Phase

~~~text
authorize_and_freeze
  -> map_scope
  -> deterministic_probe
  -> threat_model
  -> agent_hunt
  -> reconcile
  -> prove
  -> compose_risk
  -> compare_baseline
  -> validate_closure
  -> cleanup
  -> seal_core
  -> generate_reports
  -> package_and_publish
~~~

每个 Phase 都有独立 PhaseRun、输入 digest、配置 digest、状态、attempt、输出 Artifact、计数、开始/结束时间和错误分类。

PhaseRun 状态包括 queued、running、completed、failed、deferred、cancelled、not_applicable。not_applicable 只能由冻结的 analysis profile 与服务端策略产生，并必须记录 reason；模型不能跳过阶段。

### 6.3 Candidate 状态

~~~text
new -> normalized -> validating
    -> confirmed
    -> rejected
    -> deferred
    -> merged
~~~

任何状态变化都产生不可变 Decision 记录。扫描结论与人工 Triage 分离：

- confirmed/rejected/deferred/merged 属于扫描结论；
- open/false_positive/accepted_risk/fixed/reopened 属于后续 Triage；
- Triage 不得改写原 Decision 和 Evidence。

### 6.4 阶段门禁

| 阶段 | 必须持久化的输出 | 进入下一阶段的条件 |
| --- | --- | --- |
| Authorize & Freeze | AuditContract、Snapshot 目标、策略与预算 digest | 所有输入规范化且不可变 |
| Map Scope | Manifest、ScopeUnit、语言/依赖/入口面、排除决议 | 每个输入对象均有 included/excluded/deferred |
| Deterministic Probe | DetectorRun、原始 Artifact、Signal | 每个适用 Detector 有终态 |
| Threat Model | 结构化攻击者、边界、入口、资产、sink、假设 | Schema 有效且引用真实 Scope/Evidence |
| Agent Hunt | WorkReceipt、SignalPacket、读取 receipts | 所有 required WorkItem 有终态 |
| Reconcile | Cluster、Lineage、Decision | 每个 Signal 被接受、合并、拒绝或延期 |
| Prove | 静态 trace、反证、测试或 PoC Evidence | 每个 Candidate 进入终态 |
| Compose Risk | 事实化风险字段、AttackPath、Occurrence | High/Critical 达到证据门槛 |
| Compare Baseline | new/persisting/mitigated/resolved/regressed/reintroduced/unknown | Coverage 足以支持每个历史结论 |
| Validate Closure | 待封存 Coverage、未完成工作、效果清单 | 领域 Closure 通过且没有新效果可调度 |
| Cleanup | Execution/Capsule stop proof、Run 终态 | 所有效果确认停止，Run 安全进入终态 |
| Seal Core | canonical ledger roots、core seal、全部权威 Artifact digest | 事实层不可变且 core_seal_root 已持久化 |
| Generate Reports | JSON/SARIF/Markdown/HTML | 仅从 core-sealed 事实生成并引用 core root |
| Package & Publish | distribution manifest 与 digest | 报告/Artifact 校验完成，manifest 排除自身且最终封存 |

### 6.5 AuditCapabilityMatrix

Preflight 生成、Start 冻结版本化 capability matrix，避免各 Phase 按当前环境自由降级。
v1 schema 为 `riftx.audit-capability-matrix/v1`，最多 512 entries：

~~~text
phase
capability_id
requirement: required | optional | not_applicable
scope/language_tier
provider/node/backend
min_version_and_digest
proof_kind
missing_outcome.start: reject_start | continue_without_claim | not_applicable
missing_outcome.runtime: partial_capability | failed | continue_without_claim | not_applicable
reason_code
matrix_schema_version
~~~

规范规则：

- SourceIngest/AnalysisBackend/SnapshotStore/Closure/CoreSeal 是所有 Audit required；
- applicable deterministic Detector/parser 由语言 tier、Scope 和 rulepack 冻结，unsupported 文件按声明的 Tier C/Unsupported 处理，不能运行中假装 not_applicable；
- hybrid 的 model adapter、typed output、ModelDataEgress/local transport、Threat/Hunt/Skeptic/Proof required；deterministic profile 中这些才是 not_applicable；
- Deep 还要求 hybrid、Child Workflow、minimum visits/Epoch/budget；缺一项直接拒绝 Start，不降 Standard；
- Diff 要求 sealed base/head、Diff mapper/Comparator 和成对 Closure；缺一项拒绝 Start；
- dynamic policy 选中的 Sandbox/Approval/Egress capability required；`static_only` 时 Build/Test/PoC not_applicable；
- required 在 Start 缺失必须 reject_start，运行中缺失只能按合同选择 partial_capability 或 failed；optional 的两个时点都只能 continue_without_claim，not_applicable 的两个时点都必须 not_applicable；
- SourceIngest row 必须绑定冻结的 source node、ingest backend digest 和 prepare proof；AnalysisBackend row 必须绑定 selected analysis node、backend ID、backend digest 和 analysis prepare proof；Detector/parser、hybrid execution 与选中的 dynamic validation row 必须绑定同一 selected analysis node/backend 及各自组件 version/digest；
- mode/profile/policy 声明为 not_applicable 的 capability 不能被 scoped row 重新启用；全局 required 的 scoped row 不能降级，也不能替换 provider/node/backend、version/digest、proof 或 missing outcome；
- 每个已知 capability（包括 Detector/parser scoped row）只能出现在规定 Phase，不能依赖 specificity resolver 改换执行身份；
- `optional` 只能表示合同明确不作能力声明的增强项，不能用于隐藏 GA required；所有 Policy exclusion 在 Review 可见。

Start 重新验证 proof/digest；模式定义能力已缺失时返回 `audit_capability_unavailable`。运行中 required capability 暂时丢失按 Closure Policy 为 partial_capability，identity/integrity mismatch 为 failed；不得选择另一个 parser/model/backend 静默继续。Matrix、实际 proof、deviation Decision 和 missing reason 进入 core seal、Coverage、API/UI 报告。

### 6.6 Audit ↔ Run 状态映射

`AuditRunStateProjector` 是唯一可写双状态的服务；同一事务/CAS 更新 Audit、Run 和 Event，Workflow/UI 不自行推断：

| Audit lifecycle | Run status | 规则 |
| --- | --- | --- |
| draft | created | 没有 StartIntent/执行效果 |
| queued / preflighting / snapshotting | preparing | StartIntent 已持久化；尚未进入分析 |
| running | running | 当前 phase 由 Audit 单独表示 |
| waiting_approval | waiting_approval | 只等待同 Audit 的 plan decision |
| pausing / paused | pausing / paused | Resume 回到保存的 Audit phase 与 Run running |
| cancelling / cleaning(outcome=cancelled) | cancelling | admission 已围栏；stop proof 未完成 |
| failing / cleaning(outcome=failed) | completing | 不接受新效果；准备 Run failed |
| finalizing / cleaning(outcome=complete/partial) | completing | Closure/stop audit 进行中 |
| sealing_core / reporting / packaging，outcome=complete/partial | completed | Run 已安全终态，报告服务可运行 |
| sealing_core / reporting / packaging，outcome=failed | failed | partial terminal package 仍可生成 |
| sealing_core / reporting / packaging，outcome=cancelled | cancelled | partial terminal package 仍可生成 |
| completed / completed_partial | completed | report_failed 也不重开 Run |
| failed | failed | 仅可重试 seal/report/package |
| cancelled | cancelled | 仅可读/导出或重试发布链 |

进入 terminal lifecycle 前，publication_status 必须已是 `published` 或明确的
`seal_failed/report_failed/package_failed`；活跃 sealing/reporting/packaging 不能提前投影终态。
终态发布重试不重开 Run 或分析 lifecycle：failure status 只切回对应 publication phase，复用同一
Closure/core-sealed facts。complete outcome 的发布失败投影为 completed_partial，重试成功后才可
投影回 completed；partial/failed/cancelled outcome 即使发布成功也保持各自原 terminal lifecycle。
首次 distribution revision 必须与 published 状态、initial/latest revision 和
publication_finished_at 原子出现，不能在活跃 publication state 预填。

code_audit 不使用 Run `waiting_user/ready/compacting`。Run 进入 terminal 后永不为重建报告而重开；Audit 的 `analysis_finished_at`、`publication_finished_at` 与 Run `finished_at` 分开。任何映射不在表内返回 `audit_run_state_conflict` 并由 reconciler 保持较安全状态，不能单边推进。

## 7. 扫描模式

### 7.1 Standard

默认模式。确定性 Detector 完整执行一次；hybrid profile 的每个 required ScopeUnit 至少一次 Agent 审阅；随后统一 Reconcile、Proof 和风险组合。

deterministic profile 也可以使用 Standard，但 Agent 阶段必须由策略标记为 not_applicable，Closure 和报告必须明确声明该 profile 不包含 Agent review。它可以达到 complete_under_declared_scope，但不能被 UI 或报告描述为 hybrid/full-agent 审计。

完成条件：

- 所有 required ScopeUnit 为终态；
- 所有 applicable Detector 为终态；
- 所有 Candidate 为终态；
- 没有未完成 Approval 或 Execution；
- Closure Validator 通过。

### 7.2 Deep

Deep 只重复 Agent Hunt，不重复输入与配置未变化的确定性 Detector。

Deep 必须使用 hybrid profile；Deep + deterministic 的 Create 请求直接返回 audit_target_invalid，不做隐式降级。

规则：

1. 按模块、边界、入口、sink 和高风险 symbol 划分 WorkItem。
2. 每个 Epoch 的 Hunter 默认看不到其他 Hunter 的候选，减少锚定。
3. 观察策略轮换：entry-first、sink-first、boundary-first、state-machine-first。
4. 普通单元至少 2 次独立审阅，高风险单元至少 3 次。
5. 每轮结束后由确定性 Reducer 计算真正的新 Cluster。
6. 新颖性只包括新 Cluster，或已有 Cluster 的新独立入口、sink、缺失控制或实例；改写描述和提高自报 confidence 不算。
7. 至少完成 2 个全局 Epoch。
8. 连续 2 个完整 Epoch 无 accepted novel cluster、所有 tier 的 required Coverage 均无 gap，且高风险单元已满足额外 visit 要求，才允许按饱和停止；风险只会增加工作，不能让低风险 required 项被跳过。
9. 达到 token、模型调用、累计 worker job、Epoch、wall-clock 或总预算上限时，终态为 partial_budget。`workers.max_parallel` 只是并发节流阀：达到它时新 job 排队，不得因此进入 `partial_budget`；只有 `budget.max_worker_jobs` 等累计硬上限耗尽才算预算终止。
10. Deep worker 使用 Temporal Child Workflow；每个 Epoch 或大批次后 Continue-As-New。

### 7.3 Diff

Diff 输入必须冻结：

~~~text
base_snapshot_id
head_snapshot_id
merge_base
dirty_snapshot_digest（可选）
diff_policy_digest
~~~

审阅范围包括：

- changed hunks；
- enclosing symbols；
- 被修改的 guard、wrapper、parser 和 sink；
- 配置、权限和依赖变化；
- change-impact graph 的直接 caller/callee；
- 同一共享改动影响的 sibling instances。

Diff 支持 deterministic 与 hybrid；只有 hybrid 执行 Agent Hunt/Skeptic。两种 profile 的 Coverage 和报告名称必须明确区分。

规则：

- 威胁模型只允许复用完全相同的 `project_stable_id + snapshot_digest + scope_policy_digest + config_digest + policy_digest + threat_model_schema_version + model_profile_digest` 缓存，并记录来源 Audit、生成器和 Evidence。仅项目与配置相同、但 Snapshot 或 Scope 不同的结果不得命中；跨 Snapshot 只能复用带独立 Evidence 的代码无关组织策略事实，不能复用入口、边界、sink 或可达性结论。
- pre-existing Finding 可以作为上下文显示，不能标记为本次引入。
- 只有 newly_introduced、newly_reachable、regressed、reintroduced 才能作为 Diff failure。
- Coverage 不足时，历史问题只能是 unknown，不能误判 resolved。
- 每个 changed hunk 和 impact edge 必须有 Closure receipt。

#### 7.3.1 Diff 可比性与成对归因

Diff 归因不是模型标签，而是 `DiffComparator` 对 base/head 成对证据执行的确定性裁决。`mode=diff` 必须同时具有已封存的 base 与 head Snapshot；只有 head、只有自然语言基线或只查历史 Finding 的请求均为 `audit_target_invalid`。

Comparator 先生成并持久化 `DiffComparabilityRecord`：

~~~text
comparison_id
audit_id
base_snapshot_id
head_snapshot_id
diff_map_digest
scope_policy_digest
detector_set_digest
rulepack_digest
analysis_contract_digest
comparator_version
comparable_scope_pairs
unmapped_scope_units
incomparable_reasons
created_at
~~~

可比的 scope pair 必须满足：

1. base/head 使用同一规范化 include/exclude、submodule/LFS、generated/vendor 和文件上限策略；
2. 对该 weakness family 有相同的 Detector 版本、rulepack、配置、parser 与声明能力，或存在版本化的显式兼容规则；
3. base 与 head 对应 ScopeUnit、required receipt 和 applicable Detector 均通过 Closure；
4. 通过 Git diff、blob digest、canonical symbol anchor 和确定性 rename/alias 规则建立可审计映射；mapping kind 为 `added | deleted | modified | renamed | unchanged`，映射不唯一时不得猜测；
5. 动态 Evidence 若参与归因，sandbox policy 和环境 digest 必须可比；否则只能使用静态成对证据；
6. profile 不同、base 读取缺口、Detector 失败、规则能力缩小或 identity 映射不确定时，相应对象为 `unknown`，不能用“base 未发现”代替“base 不存在”。

每个 head Candidate 必须生成 `DiffEvidencePair`：

~~~text
head_subject_id
base_comparison_ref
head_evidence_ids
base_evidence_ids
changed_hunk_scope_ids
impact_edge_scope_ids
identity_mapping_kind
comparability_status
attribution
reason_codes
comparator_version
~~~

`base_comparison_ref` 必填：通常引用 base subject；对真正新增的 path/symbol 则引用服务端生成的 `BaseAbsenceEvidence`。后者只有在 base Manifest 对相同 capture policy 完整封存、Git diff 明确为 added、base 中不存在该 blob/path/canonical alias，且相关 parent scope/Detector Closure 完成时才成立，并保存 base manifest root、diff entry、absence anchor 与 policy/comparator version。它是可比的 absence/tombstone sentinel，不是“搜索没找到”。deleted 目标同理生成 head absence 供 resolved 比较；无法唯一映射仍是 unmapped/unknown，不能伪造成 absence。

`attribution` 与跨扫描 lifecycle 是两个字段，不能混用。其规范定义为：

- `newly_introduced`：head 已满足 Finding 证据门槛；base 对应范围可比且成对证据证明该危险 source、sink、缺失控制或危险配置由本次 diff 创建，base 中不存在等价可利用路径。
- `newly_reachable`：危险 primitive 或 Finding identity 在 base 已存在，但 base 证据证明路径被 guard、权限、配置或调用关系阻断；changed hunk/impact edge 使 head 路径可达。
- `pre_existing`：base/head 均有等价、可达且证据质量可比的同一逻辑问题，本次 diff 没有造成需要升级的 reachability/impact 变化。
- `regressed`：问题在 base/head 都可达，但版本化 Risk Comparator 从成对事实证明 changed guard/config/privilege/impact/blast-radius 使利用前置条件降低或安全影响上升；必须保存具体 fact delta，单纯模型 severity 提升不算。
- `reintroduced`：除成对 base/head 证据外，还存在指定 `baseline_audit_id` 中同一 logical Finding 的 `fixed`、`resolved` 或 `mitigated` 可比记录，而 head changed hunk 重新打开该问题或恢复已移除危险行为；没有历史 baseline 时不得使用此标签。
- `unknown`：不满足以上任一证明条件。base 无 Signal、搜索无结果或模型声称“新引入”都不足以升级此状态。

只有 `comparability_status=comparable`、相关 hunk/edge Closure 完成且 Evidence Pair 通过 schema/ownership 校验时，`newly_introduced`、`newly_reachable`、`regressed`、`reintroduced` 才能触发门禁。Comparator 版本、输入 digest 和 Decision 必须进入 core seal。

## 8. Snapshot、Scope 与代码事实

### 8.1 支持的目标

3.0 GA 只从本地可访问 Git 仓库创建目标：

- commit/revision；
- 当前工作区，包括 staged、unstaged 和可选 untracked；
- base/head diff；
- include/exclude 的仓库内相对路径。

远程仓库应由操作员先克隆。3.0 不在 Control Plane 保存 Git 凭据，也不自动联网 fetch。

### 8.2 Preflight

Preflight 是纯读取、无模型、无凭据变更的操作，必须返回：

- 是否为 Git 仓库与真实根目录；
- HEAD、base、head、merge-base；
- dirty、staged、unstaged、untracked 状态；
- 文件数、总字节、最大文件和语言摘要；
- submodule、symlink、LFS pointer、generated/vendor/ignored 摘要；
- 适用 Detector 与缺失能力；
- 预计 WorkItem、预算区间和阻塞错误；
- Snapshot 将采用的 include/exclude 策略摘要。

Preflight 不创建 Audit，不执行 Scanner，不访问模型。

本地 Git 仓库、object pack、index 和 config 本身也不可信。Control Plane 可信层只做 source-root/realpath/dirfd 授权、大小上限和 opaque job 调度；不得在带数据库、Temporal、模型或云凭据的进程里 import Git parser、解压 object 或启动 `git`。

Preflight 与 Snapshot 在 M2 的 `SourceIngestCapsule` 中执行：只读挂载目标 repo，独立 Snapshot staging/out，无网络/凭据/socket，非 root，限制 CPU/memory/pids/time/input/output；经过安全评审的 object/index reader 以 raw blob 方式读取，不解释 repository/system/global Git config，不运行 hook、fsmonitor、textconv、diff driver、clean/smudge filter、credential helper、URL rewrite、submodule helper或 LFS 命令。必须验证 `.git` file、gitdir/commondir、object alternates 和 worktree admin path；逃出允许 root 的 external gitdir/alternate 默认拒绝。Capsule 只返回 bounded metadata、digest、CAS ingest handle 和 stop proof。

若某个只读操作必须调用 `git` CLI，集中 `SafeGitAdapter` 也只能存在于 SourceIngestCapsule 内，使用固定 argv allowlist、clean env、超时与输出上限，并显式禁用 optional locks、prompt、replace refs、hooks、fsmonitor、external diff/textconv、filters、网络协议与 system/global config；不能调用 checkout、submodule update、LFS materialize 或任何会解释目标 repo driver 的命令。无法证明安全的路径标记 capability unavailable。恶意 object/index/config、压缩炸弹、include、hooks、fsmonitor、attributes filter/textconv、credential helper、alternate 与 replace-ref fixtures必须证明宿主零解析、零额外进程、零网络、零仓库写入，并受资源上限终止。

### 8.3 不可变 Snapshot

SourceSnapshot 至少包含：

~~~text
snapshot_id
project_id
source_kind
parent_snapshot_id（Retest 可选）
base_tree_digest（Retest 可选）
patch_digest（Retest 可选）
commit_sha
base_commit_sha（可选）
working_tree_digest（可选）
tree_digest
capture_policy_digest
materializer_schema_version
snapshot_digest
snapshot_store_version
content_storage_key
manifest_storage_key
manifest_digest
file_count
total_bytes
created_at
~~~

其中：

~~~text
snapshot_digest = H(
  tree_digest
  || capture_policy_digest
  || materializer_schema_version
)
~~~

`tree_digest` 只表示源树内容；`capture_policy_digest` 冻结 include/exclude、untracked、submodule、LFS、generated/vendor、symlink、解码和大小上限等所有会改变 Manifest 决议的输入。Snapshot 只能按 `project_id + snapshot_digest` 去重，绝不能只按 `project_id + tree_digest` 复用不同策略的 Manifest。

实现要求：

1. commit 目标优先使用 Git object database 读取，不 checkout 到原工作区。
2. dirty tree 创建内容寻址副本，不在扫描期间继续读取变化中的原文件。
3. Manifest 按规范化 POSIX 相对路径排序，记录 mode、size、SHA-256、Git blob ID、language、classification 和 capture decision；每条 decision 必须能追溯到冻结的 `capture_policy_digest`。Audit 内进一步的分析优先级与 required/optional 选择属于 Scope Ledger，不得回写共享 Snapshot Manifest。
4. 不包含 .git 内容。
5. 不跟随逃出仓库的 symlink；symlink 本身作为对象记录。
6. special file、socket、device、超限文件和无法解码文件必须显式 deferred/excluded。
7. submodule 默认显式排除；选择纳入时必须独立冻结其 commit。
8. LFS pointer 未 materialize 时标为 deferred，不允许联网拉取。
9. Snapshot 根目录权限只读；输出目录必须在其外部。
10. Audit 完成前后都计算原仓库 digest 抽样或完整校验，测试中必须证明未修改。

#### 8.3.1 SnapshotStore 与生命周期

Snapshot 内容不能存成首个 Run 所属 Artifact，也不能只保存 Manifest 后假设能从原 dirty tree 重建。新增独立 `SnapshotStore` Port，后端为 RiftX 管理、位于所有 source root 之外的内容寻址只读 CAS：

~~~text
put_staged_tree(temp_root, expected_snapshot_digest) -> content_storage_key
open_blob(content_storage_key, relative_path, expected_blob_digest) -> bounded_reader
verify(content_storage_key, manifest_digest) -> integrity_result
add_reference(snapshot_id, audit_id, role)
release_reference(snapshot_id, audit_id, role)
garbage_collect(unreferenced_before, dry_run) -> gc_plan/receipt
~~~

`content_storage_key` 是后端 opaque locator，不返回 UI/普通 API；数据库另存 `snapshot_references(audit_id, snapshot_id, role)`。commit 与 dirty Snapshot 都必须在 sealed 前证明所有 included blob 可从 SnapshotStore 独立读取。写入使用同一文件系统 staging、fsync、digest 校验和原子 rename；CAS 对象封存后只读。删除仅由引用计数/保留策略生成可审阅 GC plan，运行中的 Audit、Baseline、Finding Evidence 或 Retest 引用均阻止回收。

Run-scoped Artifact 只保存 Audit 对该 Snapshot 的 Manifest/报告投影；它可以引用 snapshot_id/digest，但不拥有或删除共享 Snapshot 内容。

跨 Node 水合使用短期 `SnapshotHydrationLease`，而不是“知道 digest 即可下载”：

~~~text
lease_id/nonce
audit_id
snapshot_id/manifest_digest
target_runner_principal
target_node_id
allowed_blob_digests
max_bytes
expires_at
hydration_policy_digest
status
~~~

每个 manifest/chunk 请求都验证认证 Runner principal、Audit/Node/lease/expiry/剩余字节并防 replay；服务不提供跨 Audit digest 查询/list oracle。传输强制 TLS/mTLS 或等价双向认证加密，每块与最终 node-local CAS 重新验证 digest/manifest root，文件权限只授予关联 Audit worker。Lease/session 持久化、可撤销且绑定一个 Runner Execution；Run admission fence/cancel 先撤销 Lease，再由 Execution stopper证明无活跃传输。没有加密、object authorization 或 stop ownership 的跨 Node backend 必须标记 capability unavailable，不能回退明文共享目录。

node-local bytes 也有独立生命周期。Backend 二选一且写入 capability proof：

1. 每 Execution 私有 materialization，目录只属于该 worker/mount namespace，cleanup 在进程停止后卸载、撤权并删除；或
2. daemon-only 共享 CAS：底层 root 对普通 worker 不可遍历，`snapshot_hydration_objects` 只存 node/blob/local-key/integrity，`snapshot_hydration_pins` 为每个 Audit/Execution 创建不可转让的只读 mount token/ref。

任一方案都要求：其他 Audit/同 UID 进程不能凭 path/digest 读取；stop proof 包含活跃 fd/process 已停、mount namespace 卸载、lease/pin revoked、worker path 不可访问。Runner 重启 reconciler 重建 object/pin/session 状态，无法证明撤权时 Cancel 保持 unconfirmed。Eviction/GC 只删除无 active pin 且超过保留期的对象，使用原子 tombstone，生成 node、object digests、bytes、reason、deleted_at 的 `HydrationGCReceipt`；Audit/Baseline/Retest pin 阻止删除。node-local cache 命中仍需新 Lease/Pin/ownership 校验，不能跨 Audit 沿用旧 mount。

### 8.4 Code Scope Ledger

ScopeUnit 类型：

- file
- symbol
- diff_hunk
- dependency
- endpoint
- configuration
- trust_boundary

字段至少包括：

~~~text
id
audit_id
snapshot_id
kind
relative_path
blob_digest
symbol_anchor
risk_tier
required_analyses
status
closure_code
closure_reason
receipt_count
created_at
updated_at
~~~

风险排序只决定调度顺序，不能让低风险对象静默消失。

`risk_tier` 由服务端版本化 `ScopeRiskPolicy` 持有，不取 Agent 自报值。Planner 在首次派发前用冻结的 deterministic facts 计算 floor：语言/文件类别、入口与权限配置、确定性 sink/source/secret/依赖信号、Operator 冻结的 asset criticality 和 Scope 关系；保存 policy version、输入 digest 与 reason codes。System Mapper/Hunter 只能提交带 Evidence 的 monotonic elevation proposal，Policy 验证后可以把 tier 调高并追加 visit/WorkItem，永远不能调低 floor 或减少已要求的 Coverage。任何 risk 变化均产生 Decision 并进入 core seal。

所有 ScopeUnit 无论 tier 都有基础 required visit/analysis；高风险只增加审阅次数、Proof 强度和优先级。Saturation/Closure 先检查全体基础要求，再检查 elevation 产生的附加要求，避免模型通过少报 high-risk sink 间接提前停止。

### 8.5 Agent 安全读取工具

审计 Agent 不直接获得通用 Shell 或任意文件系统读取。新增以下 Snapshot-aware 工具：

- list_snapshot_files
- read_snapshot_lines
- search_snapshot_text
- get_symbol
- find_references
- find_callers
- find_callees
- get_scope_unit
- list_detector_signals
- register_audit_signal
- request_scope_expansion
- close_audit_work_item

所有工具必须：

- 绑定 audit_id、snapshot_id、worker_id 和 ScopeUnit；
- 只接受规范化相对路径；
- 拒绝路径穿越、symlink 逃逸和跨 Audit 引用；
- 记录实际读取范围和 digest receipt；
- 限制单次与累计字节数；
- 不允许模型直接提供稳定 ID、最终 Finding 或 Coverage。

### 8.6 WorkItem 与 Coverage Receipt

Planner 在派发 WorkItem 前必须生成不可变的 `RequiredCoveragePlan`，由服务端保存为 Artifact 并把 digest 写入 WorkItem。Agent 无权新增、删除或缩小要求：

~~~text
coverage_plan_id
audit_id
work_item_id
primary_scope_unit_id
support_scope_unit_ids
snapshot_id
required_blob_digests
required_byte_ranges
required_symbol_anchors
required_graph_edges
required_detector_signal_ids
coverage_predicate_version
plan_digest
~~~

范围规则必须是确定性的：`file/full` 覆盖该 blob 的全部允许字节；`symbol` 覆盖规范化 symbol span 与 Planner 指定的 guard/context span；`diff_hunk` 覆盖 changed range、enclosing symbol 和指定 impact edge；过大的对象必须先拆成多个 WorkItem，不能用“抽样读取”冒充完整覆盖。

每次 Snapshot reader 调用由服务端追加只读 `ReadReceipt`，记录工具、实际 blob digest、实际字节/行范围、返回字节数、worker/lease、时间和响应 digest。Agent 返回的 `WorkReceiptPacket` 只是完成申请，不是权威 Coverage 记录。

`close_audit_work_item` 的服务端语义为：

1. 校验调用 worker 持有有效 lease，Packet 引用均属于同一 Audit/Snapshot/Scope；
2. 从数据库重新读取 `RequiredCoveragePlan` 与服务端生成的 ReadReceipt，不接受 Packet 自报的读取范围；
3. 对实际范围做区间合并，按 `coverage_predicate_version` 验证 required byte、symbol、edge、Signal 和 Detector 条件；
4. 有缺口时返回结构化 `coverage_gap`，WorkItem 保持非终态并列出尚缺的服务端范围；
5. 通过后由服务端生成唯一 `AuditWorkReceipt` 并原子地关闭 WorkItem；重试返回同一 Receipt；
6. `waived`、`deferred`、`unsupported`、`not_applicable` 只能由版本化 Policy、Detector capability 或 Operator reason 产生，Agent 不能借此关闭 required 工作。

`AuditWorkReceipt` 至少保存 `coverage_plan_digest`、参与判定的 `read_receipt_ids`、合并后范围、predicate version、closure code 和 server signature/digest。Finalizer 必须重新计算 Receipt 与当前 immutable plan 的对应关系；只读一行、伪造 Packet、复用另一 WorkItem receipt 或删除任一 required ReadReceipt 都必须阻止 `complete`。

跨文件分析使用有界 `WorkScopeSet`，而不是让 Hunter 获得整个 Snapshot。每个 WorkItem 有一个 primary ScopeUnit，并通过 `audit_work_item_scopes(work_item_id, scope_unit_id, role, reason_edge_id)` 关联 Planner 预先批准的 support/context ScopeUnit；`RequiredCoveragePlan` 覆盖整个集合。工具只能读取该集合。

若阅读中发现新的 caller/callee、shared guard 或配置依赖，Agent 只能调用 `request_scope_expansion` 提交确定性 symbol/edge 引用。服务端验证边确实存在、预算和去重规则后，创建新的 support ScopeUnit/子 WorkItem 或扩展后重新排队原 WorkItem，并生成新版本 Coverage Plan；在新 plan 生效前 Agent 不能读取目标。已关闭 Receipt 永远不被原地扩写。拒绝或超预算的 expansion 必须成为显式 Coverage gap，保证跨文件发现能力不以越权读取或虚假完成为代价。

## 9. 确定性 Detector 层

### 9.1 统一契约

每个 Detector 实现 AuditDetector：

~~~text
describe() -> DetectorDescriptor
applicability(snapshot, inventory) -> applicable | unsupported | waived
plan(audit, scope) -> DetectorJob[]
execute(job, sandbox) -> DetectorExecutionResult
parse(result_artifact) -> Signal[]
~~~

DetectorDescriptor 至少包含：

- detector_id、display_name、version；
- capability 与支持语言；
- rulepack/config digest；
- 是否内置或外部命令；
- 所需 Runner capability；
- 输出格式和 parser 版本；
- 默认超时、资源和网络策略。

任何 Detector 只能产生 Signal，不能直接写 Confirmed Finding。

### 9.2 3.0 原生最低能力

1. Git-aware Inventory Detector。
2. Secret Detector：规则、熵、上下文 allowlist、精确 span；测试值必须使用明显假凭据。
3. Manifest/SBOM Detector：解析锁文件与依赖来源；advisory 数据库必须带 snapshot digest。
4. Structural Rule Detector：基于语法树，不用纯正则承担语言语义。
5. Configuration/IaC Detector：Docker、CI、权限和危险默认值。
6. Diff Impact Detector：hunk、symbol、依赖和直接影响边。
7. 有限 Source-to-Sink Detector：只为明确支持的语言和 sink 声明能力。
8. SARIF Import Detector：接受已注册的外部工具输出并归一化。

Semgrep、CodeQL、SCA、Secret 或 IaC 工具可以作为可选普通适配器，但：

- 不作为 RiftX 3.0 唯一检测能力；
- 不随 RiftX 自动下载；
- 不直接决定 Finding；
- 必须记录工具版本、规则包、命令参数摘要、输入和输出 digest；
- 打包或分发前单独完成许可证审核。

### 9.3 Detector Ledger

~~~text
detector_run_id
audit_id
detector_id
detector_version
rulepack_digest
configuration_digest
input_selector_digest
sandbox_policy_digest
execution_id
status
raw_artifact_ids
parsed_signal_count
parse_errors
started_at
finished_at
~~~

Scanner 退出码为 0 但输出无法解析时必须标记 failed，不得当作零发现。

## 10. Agent 审计方法

### 10.1 角色

| 角色 | 职责 | 禁止事项 |
| --- | --- | --- |
| System Mapper | 结构化攻击者、信任边界、入口、资产、权限转换和高风险 sink | 不产生最终 Finding |
| Partition Hunter | 在分配的有界 WorkScopeSet 内生成漏洞假设与跨文件代码流声明，必要时申请 scope expansion | 不读取未获批范围，不验证自己 |
| Skeptic | 寻找 guard、不可达条件、安全 wrapper、部署限制和反例 | 不因措辞相似合并候选 |
| Proof Analyst | 形成静态证明或受控动态验证计划 | 不自行批准执行或网络 |
| Chain Analyst | 仅用 Confirmed facts 组合跨组件攻击路径 | 不使用未验证假设 |
| Fix Advisor | 生成修复策略、受影响面和回归测试建议 | 不直接写用户工作区 |

Coordinator 必须是确定性 Workflow，不是“总控 Agent”。

### 10.2 Typed Packet

新增严格 Pydantic 契约，model_config 使用 extra=forbid：

- ThreatModelPacket
- HuntSignalPacket
- SkepticReviewPacket
- ValidationPlanPacket
- StaticProofPacket
- AttackPathPacket
- FixAdvicePacket
- WorkReceiptPacket

每个 Packet：

- 只能引用本 Audit 已存在的 Snapshot、ScopeUnit、Signal、Evidence 和 Location；
- 有严格长度、数量和枚举上限；
- 不能携带任意工具命令；
- Schema 无效时整体拒绝，记录失败，可按策略最多修复一次；
- 模型自报 confidence 只保存为 hint；
- 原始模型输出只作为受限 Artifact 保存，不进入报告。

现有 AgentEngineRequest 应增加 model-adapter-neutral 的 output_contract，而不是在审计模块直接调用某个模型 SDK。若具体 adapter 原生支持结构化输出则使用；否则执行 JSON 提取与 Pydantic 校验。所有 adapter 必须通过同一组契约测试。

### 10.3 威胁模型契约

Threat Model 不能只是一段自由文本，至少包含：

- attacker_capabilities
- trust_boundaries
- external_entry_points
- privilege_transitions
- sensitive_assets
- high_risk_sinks
- deployment_assumptions
- unresolved_assumptions
- evidence_refs
- scope_unit_refs

没有 Evidence 的部署假设必须标记 unresolved，不能作为排除漏洞的事实。

### 10.4 Prompt Injection 防线

仓库代码、注释、README、Issue 模板、Scanner 输出和测试数据全部视为 untrusted source data：

- 通过工具数据通道传入，不拼接为系统指令；
- 明确标记来源与 digest；
- 只暴露审计工具 allowlist；
- Scope、审批和网络由服务端执行，Prompt 无权改变；
- 对“忽略规则、执行命令、泄露密钥、扩大范围”等 fixture 做强制测试；
- UI 不展示内部推理，只展示结构化进度、Evidence 与裁决。

### 10.5 Model Data Egress Policy

Hybrid 不等于默认允许把源码发送到远程模型。`ModelDataEgressPolicy` 是 AuditContract 的冻结部分：

~~~text
mode: local_only | remote_redacted
model_profile_digest
endpoint_origin_digest
provider_display_name
execution_locality
retention/training_disclosure
allowed_scope_classes
allowed_remote_origins
max_bytes_per_call
max_bytes_per_audit
redaction_policy_version
redaction_policy_digest
operator_consent_requirement_digest
operator_consent_at
policy_digest
~~~

`retention/training_disclosure` 不是自由文本或任意 canonical document；v1 使用专用 strict
schema `riftx.model-retention-training-disclosure/v1`：

~~~text
data_residency_regions: 1..32 个 canonical region token，禁止 unknown/undisclosed/unspecified
retention_days: 0..3650
training_usage: not_used_for_training | may_be_used_for_training
provider_terms_version
provider_terms_digest
~~~

Model egress v1 wire schema 为 `riftx.model-data-egress/v1`。endpoint origin 集使用
canonical HTTPS origin 并计算独立 domain digest；policy_digest 覆盖上述全部冻结字段。
`operator_consent_requirement_digest` 由除 consent 时间、该 digest 自身和 policy_digest 以外的
所有 egress 风险字段，以 `riftx.model-egress-consent-requirement/v1` domain-separated SHA-256
确定性计算；调用方提供的值必须恒等。`operator_consent_at` 记录 Review 披露确认；AUD-103
仍需把实际 `start_request_id + reviewed_contract_digest` 同意保存为独立 Start 事实，不能在
Start 后回写合同形成循环 digest。任意 schema、未知驻留/保留/训练披露或 consent digest
mismatch 都必须 fail-closed。

默认 profile 为 deterministic；`local_only` 只允许服务端标记并验证为本地受控 origin 的模型。远程 profile 必须在 Review 明示 provider、base origin、数据驻留/保留/训练披露、最大外发字节和风险，由 Operator 对该 contract digest 显式同意；未知披露、origin 变化或 proxy 重定向一律阻止 Start。RiftX 不因 Provider 宣称“零保留”而跳过技术控制。

所有给远程模型的 Snapshot 内容经过服务端 `OutboundCodeView`：Secret Detector 和高熵/私钥/credential/path 规则先行，把 literal 替换为保留语法形状的稳定占位符；`.env`、key material、credential config 与 Policy 禁止类永不外发。工具返回给 Agent 的是 redacted view，模型不能请求 raw secret。每次调用持久 `ModelEgressReceipt`，只含 Audit/Call、provider/profile/policy digest、Snapshot/blob/range refs、原始/外发字节数、redaction count 与 outbound digest，不保存第二份明文。

远程模型请求使用固定 origin allowlist、TLS 验证、无自动跨 origin redirect，并与第 15 节网络 broker/connection receipt 接线。测试必须向仓库放入明显 fake secret、private-key canary、credential URL 和 prompt injection，证明 Provider fixture 收不到原 literal、超范围字节或绝对路径。Operator 未同意时 hybrid Start 返回 `audit_model_egress_not_approved`，不得静默降级或外发。

## 11. Signal、Evidence 与 Decision

### 11.1 Signal

Signal 是尚未确认的线索：

~~~text
signal_id
audit_id
producer_type: native_detector | external_detector | agent
producer_id
producer_version
weakness_family
rule_ids
locations
trace_claims
evidence_ids
confidence_hint
raw_payload_digest
cluster_key
disposition
~~~

### 11.2 CodeLocation

~~~text
snapshot_id
relative_path
blob_digest
start_line
start_column
end_line
end_column
byte_start
byte_end
enclosing_symbol
role: source | control | sanitizer | sink | trigger | definition | evidence
snippet_hash
~~~

行号不能单独作为证据；Location 必须绑定 Snapshot 与 blob digest。

### 11.3 Evidence

Evidence ID 是规范化内容的 SHA-256。类型：

- source_span
- static_trace
- detector_output
- dependency_fact
- configuration_fact
- test_result
- crash
- poc
- counterexample
- operator_attestation

动态 Evidence 的 SandboxCapsule 必须记录：

- sandbox backend 与镜像/环境 digest；
- 参数化命令摘要与 redaction；
- environment allowlist；
- network policy；
- CPU、内存、进程、磁盘和超时；
- exit code；
- stdout/stderr Artifact；
- 文件系统变更摘要；
- Execution ID 与 Approval ID。

### 11.4 Decision Ledger

每次 Candidate/Cluster 状态变化新增 Decision：

~~~text
decision_id
audit_id
subject_id
previous_state
new_state
reason_code
evidence_ids
actor_type: policy | detector | agent | operator
actor_id
created_at
~~~

不允许 UPDATE 覆盖历史 Decision。

`actor_type` 是 provenance，不是授权。所有写入必须经过 `AuditDecisionService` 的版本化 transition ACL，Repository 禁止向 Agent tool 暴露通用 `append_decision`：

- `agent` 只能提交 `proposed`、`challenged`、`needs_evidence` 等建议性状态，并且必须引用自己的 typed Packet 与实际 Evidence；
- `detector` 只能记录 Signal 的产生、撤回或 parser/capability 结果，不能确认 Finding；
- `policy` 可在 Evidence 门槛、Closure、ownership 与 comparability 全部通过后执行 `confirmed`、`rejected`、`severity_assigned` 和历史比较；
- `operator` 可执行显式 Triage/attestation 路径，但必须提供 reason、身份与审计记录，不能伪造 Detector、Coverage 或动态执行事实；
- `confirmed`、最终 severity、`resolved`/`regressed`、Coverage complete 和 final seal 永远禁止 `actor_type=agent`；非法组合返回 `audit_transition_forbidden`，不能仅靠 UI 隐藏。

数据库约束、Application Service 单元测试和 API/tool contract 测试必须覆盖这张 ACL。Agent Packet 中出现 stable ID、最终 disposition、最终 severity 或历史状态时，Schema 应拒绝或忽略该越权字段，绝不能落成 Decision。

### 11.5 证据质量等级

| 等级 | 定义 | 可报告性 |
| --- | --- | --- |
| Q0 | 只有模型假设 | 不可成为 Confirmed Finding |
| Q1 | 精确静态 trace 或确定性规则证据 | Medium/Low 可继续裁决 |
| Q2 | 独立 Skeptic 通过且有第二类证据 | High/Critical 最低门槛之一 |
| Q3 | 受控测试、崩溃或 PoC 动态复现 | 最强证据 |

门槛：

- Critical/High：至少 Q2，或直接 Q3。
- Medium：至少 Q1 且完成 Skeptic。
- Low/Info：必须有精确 Evidence；纯建议不能成为漏洞。
- 同一个模型对同一 Evidence 的复述不构成独立证据。

Q2 必须由版本化 `EvidenceIndependencePolicy` 计算，不能按“Evidence 数量 >= 2”机械升级。至少满足一项技术组合：`detector_output + 独立 static_trace`、`static_trace + counterexample review`、两个不同实现/规则谱系的确定性 Detector，或 Q3 动态结果；各项必须绑定可验证的代码/配置事实。Hunter 与 Skeptic 使用同一模型、同一 prompt lineage 或同一上游 Signal 时不独立，换角色名称也不能提高等级。`operator_attestation` 只能证明部署、资产、权限或配置环境事实，不能单独或与模型陈述组合证明代码漏洞存在。Policy 保存 provenance lineage、independence reason 和 policy version；不在允许矩阵中的组合最高保持 Q1。

## 12. Finding、严重性与历史

### 12.1 身份分层

- signal_id：一次 Audit 内的原始线索。
- candidate/cluster_id：一次 Audit 内的归一化候选。
- logical_finding_id：跨 Snapshot 的逻辑问题。
- occurrence_id：某个 Snapshot/Audit 中的一次观察。

稳定身份不得包含标题、严重性、模型、自然语言描述或绝对行号。

建议指纹：

~~~text
anchor_key_v1 =
  H(language
    || canonical_symbol_path
    || sink_kind
    || normalized_sink_fingerprint
    || trust_boundary_kind)

logical_finding_id_v1 =
  H(project_stable_id
    || weakness_family
    || anchor_key_v1
    || instance_discriminator)

occurrence_id_v1 =
  H(logical_finding_id_v1
    || audit_id)

snapshot_observation_key_v1 =
  H(logical_finding_id_v1
    || snapshot_digest
    || audit_profile_digest)
~~~

`occurrence_id` 表示一次 Audit 的观察，所以必须包含不可复用的 Audit ID；相同 Snapshot/profile 的两次重跑仍是两个 Occurrence。需要跨重跑聚合时使用非主键 `snapshot_observation_key`，不能让它替代 Occurrence 身份。

上述字段必须来自服务端 `FindingIdentityCanonicalizer`，不能直接取 Agent Packet 文本。其规则为：

- `weakness_family` 只能是 RiftX 版本化 taxonomy 中的 canonical ID，由受支持 Detector rule、已验证的结构化 trace/Proof 和 Policy 映射；Agent 提议只作为 hint；
- `canonical_symbol_path`、sink fingerprint 和 instance discriminator 从 Snapshot parser、blob、symbol graph 与精确 Location 计算；
- `trust_boundary_kind` 只能引用 Inventory/Configuration Evidence 证明的服务端枚举边界；只有模型 Threat Model 的未解决边界不得进入指纹，缺少可信边界时使用稳定的 `boundary_unknown`，而不是自然语言；
- 每个 canonical fact 保存 `value + canonicalizer_version + evidence/provenance refs`；未通过 ownership、Evidence 和枚举校验的 Candidate 不得创建 logical identity；
- Agent 永远不能提交 `anchor_key`、fingerprint、logical ID、occurrence ID 或 identity alias。服务端忽略或拒绝这些字段并重新计算。

文件移动或格式变化不应改变 logical ID。大规模重构只能产生 possible_identity_alias，由确定性规则和人工确认，不得静默合并。

### 12.2 严重性

模型只能提出事实字段，RiskPolicy 根据以下信息计算 RiftX Severity：

- attacker proximity；
- authentication 与所需 privilege；
- user interaction；
- reachability；
- confidentiality/integrity/availability impact；
- asset criticality；
- exploit preconditions；
- blast radius；
- exploit maturity；
- Evidence quality；
- 未解决假设。

Severity 与 Confidence 必须分开显示。CVSS 可以导出，但只从结构化事实计算，并允许操作员复核。

### 12.3 历史状态

跨扫描比较：

- new
- persisting
- mitigated
- resolved
- regressed
- reintroduced
- unknown

这些是 `history_lifecycle`，不是第 7.3 节 `diff_attribution`。`BaselineComparator` 按以下互斥优先级写一个状态，并保存 previous/current occurrence、risk-fact delta、CoverageVector 和 reason code：

1. 当前 confirmed identity 在项目历史中从未出现：`new`，含义仅是“RiftX 首次观察”，绝不等同 `newly_introduced`，不能单独触发 Diff failure。
2. 历史 identity 存在但 identity mapping、Scope、能力或 Evidence 不可比：`unknown`；不得跳到以下状态。
3. 当前 confirmed，而最近一次可比状态为 `resolved`，或有带 Retest/技术 Evidence 的 `fixed`：`reintroduced`。仅 operator 文本声称 fixed 不够；从 `mitigated` 再变严重属于 `regressed`。
4. 当前没有 confirmed occurrence，且当前 CoverageVector 支配历史向量并有 targeted negative/absence proof：`resolved`；否则 `unknown`。
5. 当前和 baseline 都 confirmed，版本化 Risk Comparator 证明 exploit precondition 下降、guard/privilege 变弱或 impact/blast radius 上升：`regressed`。
6. 当前问题仍存在，但成对事实证明 reachability、impact 或可利用性降低且未达到 resolved 证明：`mitigated`。
7. 当前和 baseline 都 confirmed，risk facts 在 Policy 容差内等价且没有上述变化：`persisting`。

`accepted_risk`、`false_positive` 等 Triage 不自动改变扫描 lifecycle；Triage 与技术比较分别显示。任何模型只能提议 risk facts，不能选择 history state。

只有当前 Coverage 足以覆盖旧 Occurrence 的相关 Scope、策略与 Detector，才能判定 mitigated/resolved/regressed/reintroduced/persisting。否则必须是 unknown。

具体实现不得只比较“文件已扫、Detector 已跑”。每个 Occurrence 在确认时冻结 `DetectionCoverageVector`：

~~~text
analysis_profile
scope_policy_digest
relevant_scope_anchors
required_coverage_predicate_versions
detector_capability/version/rulepack/config tuples
language_parser_and_graph_versions
agent_role_strategy_and_min_visit_counts
proof_and_validation_requirements
evidence_quality
vector_schema_version
~~~

`BaselineComparator` 只有在当前向量对该 weakness/location **支配或等价于** 历史向量，相关 Scope 映射唯一且全部 Closure，并且当前有确定性 absence/negative proof 时，才可写 `resolved`。因此 hybrid/Agent 发现的历史问题不能被 deterministic-only 扫描判为 resolved；减少 Hunter visits、缺失原有 strategy、Detector/rulepack/language 能力下降、parser 不兼容或验证环境不可比时必须为 `unknown`。若专门的反例测试或静态证明直接证明旧攻击路径已被删除/阻断，可按版本化 Policy 作为 targeted negative proof，但仍须覆盖该 Finding 的 source/control/sink 与变更边。Comparator 输入、dominance 结果和 reason code 必须持久化。

### 12.4 Triage

Triage 状态：

- open
- accepted_risk
- false_positive
- fixed
- reopened

每次 Triage 保存 actor、reason、created_at、expires_at（适用时）和 scope。Suppressions 必须可过期、可审计，不能删除原 Finding。

## 13. 领域模型与数据库

### 13.1 Run 扩展

在 Run 增加：

~~~text
kind: general | code_audit
~~~

迁移要求：

- runs.kind 为 NOT NULL，旧记录使用 server-side default general；
- 迁移后移除不必要的 database default，避免新代码忘记显式设置；
- 所有旧 API 响应向后兼容；
- 通用 Run 创建始终写 general；
- AuditApplicationService 在一个数据库事务中创建 code_audit Run 和 AuditScan；
- 不允许给已有 general Run 追加 AuditScan。

### 13.2 核心聚合

#### CodeProject

表示稳定的代码项目身份，不等同于某个路径：

~~~text
id
engagement_id
display_name
vcs_kind
repository_identity_digest
default_branch（可选）
created_at
updated_at
~~~

repository_identity_digest 由规范化 Git identity 与操作员确认信息生成。每个 CodeProject 关联一个 Engagement，以满足 Run 强制 FK 与授权事实；创建 Project 时在同一 UoW 创建或验证 Operator 指定的 Engagement，authorization_reference 保存 source authorization/policy digest 而非绝对路径。原始本地绝对路径是敏感服务端配置，不进入指纹、不进入 Event、不默认返回 UI。

#### SourceSnapshot

字段见第 8 节。Snapshot 一经 sealed 不允许修改。SnapshotStore 中的 Manifest 必须与 `manifest_digest`、`tree_digest`、`capture_policy_digest` 和 `snapshot_digest` 相互校验；Repository 复用前必须同时命中 `project_id + snapshot_digest`。Run-scoped Manifest Artifact 只是投影，不能作为 SourceSnapshot FK 或生命周期 owner。

#### AuditContractRecord

Audit 开始后不能只剩几个 digest。`audit_contracts` 必须保存有大小上限、版本化 canonical JSON 及其 digest，至少冻结规范化 source target（绝对路径作为敏感字段，不进 Event/API）、base/head、Scope/capture policy、analysis profile、Detector/rulepack/parser 集、模型 profile 与 ModelDataEgressPolicy、ValidationPolicy、预算、选定 node/backend、环境能力要求和所有 schema version：

v1 使用 `riftx.audit-contract/v1`，canonical UTF-8 JSON 上限 256 KiB；内嵌
versioned policy document 使用 `riftx.versioned-policy-document/v1`、单文档上限 64 KiB，
ValidationPolicy document 还必须使用 `riftx.validation-policy/v1` 并显式携带与枚举一致的
`validation_policy`。canonical parser 拒绝 duplicate key、非 canonical encoding、未知字段，
并限制 depth 64、总节点 10,000、单 key 1 KiB UTF-8、单 string 64 KiB UTF-8。
Contract、SourceTarget、Budget、CapabilityMatrix、Policy document、ModelDataEgressPolicy
分别使用带 schema/domain separator 的 SHA-256，禁止跨对象类型复用裸 payload digest。

~~~text
contract_id
audit_id
schema_version
canonical_contract_json
contract_digest
source_target_digest
source_node_id
source_ingest_backend_digest
source_prepare_proof_digest
selected_node_id
required_backend_id
snapshot_hydration_policy_digest
created_at
sealed_at
~~~

canonical contract 中的 `AuditExecutionSelection` 至少冻结：

~~~text
source_node_id
source_ingest_backend_id/digest
source_prepare_proof_digest
selected_node_id
required_backend_id
analysis_backend_digest
analysis_prepare_proof_digest
analysis_image_digest
analysis_policy_digest
snapshot_hydration_policy_digest
selection_policy_version
eligible_candidates_digest
~~~

CapabilityMatrix 的 SourceIngest/AnalysisBackend rows 是这些字段的交叉证明，不允许形成两套
互相冲突的 backend 身份。AuditContractRecord 的冗余查询列只是索引/快速校验；canonical
contract 始终是完整恢复源，每个冗余值都必须与其重新解析结果恒等。

Worker 重启只从该记录恢复，不读取当前配置来“补全”旧合同。AuditScan 保存 `contract_id + contract_digest` FK；两者不匹配必须 fail-closed。Preflight plan 的 token hash、expiry、reserved/consumed audit、content digest 与 `client_request_id` 使用独立列和唯一约束，不得只存在内存。

#### AuditScan

~~~text
id
run_id
project_id
contract_id
snapshot_id（Snapshot sealed 前可空）
base_snapshot_id（Diff 可选）
baseline_audit_id（可选）
purpose: primary | validation_followup | retest
parent_audit_id（follow-up 可选）
mode
analysis_profile: deterministic | hybrid
lifecycle_status
current_phase
terminal_outcome: complete | partial | failed | cancelled（分析期间可空）
cleanup_proof_digest（cleanup convergence 后可填）
run_terminal_status: completed | failed | cancelled（与 cleanup proof 原子记录）
closure_status（可选）
publication_status: not_started | sealing_core | report_pending | reporting | packaging | published | seal_failed | report_failed | package_failed
core_seal_root（可选）
initial_distribution_revision_id（可选）
latest_distribution_revision_id（可选）
model_profile（可选）
selected_node_id
required_backend_id
policy_digest
budget_digest
config_digest
contract_digest
temporal_workflow_id
created_at
started_at
analysis_finished_at
publication_finished_at
sealed_at
~~~

约束：

- run_id 唯一；
- run.kind 必须为 code_audit；
- `draft/queued/preflighting/snapshotting` 允许 snapshot_id 为空；Snapshot seal 与 FK 更新原子提交，running/finalizing 以及 outcome=complete/partial 要求 snapshot_id 非空；Start 前或 Snapshot 失败后的 failed/cancelled partial-facts seal 是明确例外，可以没有 Snapshot；
- Diff 只由 `AuditMode.DIFF` 表示，head/base Snapshot 必须原子绑定、同时存在且不同；非 Diff 禁止 base_snapshot_id；
- `current_phase=map_scope..validate_closure` 时，即使 lifecycle 已进入 failing/cancelling，也必须有 started_at 和 sealed Snapshot，Diff 还必须有 base Snapshot；只有 authorize_and_freeze/Snapshot 失败等真正早期路径可无 Snapshot；
- cleanup_proof_digest 与 run_terminal_status 必须原子出现，并按 terminal_outcome 映射为 complete/partial→Run completed、failed→Run failed、cancelled→Run cancelled；两者未记录时禁止产生 Closure 或进入 sealing_core，一经记录不可替换；
- publication_status 只能由 AuditRunStateProjector/Publisher 更新；reporting 以后要求 core_seal_root，published 要求同 Audit 的 latest revision FK，revision 只能前进不能覆盖；
- project、snapshot、base 和 baseline 必须同一对象授权域；
- sealed 后只允许追加 Triage、Alias、Validation Supplement 和 Retest 关系，不允许改写扫描结论。

#### AuditPhaseRun

~~~text
id
audit_id
phase
attempt
idempotency_key
input_digest
config_digest
status
output_artifact_ids
summary_counts
error_code
error_summary
started_at
finished_at
~~~

唯一约束：audit_id、phase、idempotency_key。错误摘要必须脱敏，完整诊断作为受限 Artifact。

#### AuditWorkItem

~~~text
id
audit_id
phase
epoch
primary_scope_unit_id
strategy
stable_key
risk_tier
status
lease_owner
lease_expires_at
attempt
input_digest
required_coverage_plan_artifact_id
required_coverage_plan_digest
receipt_id
created_at
updated_at
~~~

唯一约束：audit_id、phase、epoch、stable_key。Activity 重试先查询已有 terminal WorkItem；模型/外部效果按第 14.3 节的可判定幂等或 `outcome_unknown` 规则处理，不虚假承诺网络模型调用 exactly-once。

### 13.3 建议新增表

| 表 | 作用 | 关键约束/索引 |
| --- | --- | --- |
| audit_projects | 稳定项目身份与 Engagement FK | repository_identity_digest 唯一；engagement_id FK |
| audit_preflight_plans | 短期冻结创建计划 | token_hash 唯一；expires/status 索引 |
| audit_contracts | 可恢复的 canonical 冻结合同 | audit_id 唯一；contract_digest 校验 |
| audit_start_intents | DB→Temporal 可靠启动投递 | audit_id 唯一；start_request_id 唯一；status/next_attempt 索引 |
| source_snapshots | 不可变源码目标 | project_id + snapshot_digest 唯一；tree/policy/schema 可校验 |
| snapshot_references | Audit/Baseline/Evidence 对 CAS Snapshot 的生命周期引用 | audit/snapshot/role 唯一；GC 外键保护 |
| snapshot_hydration_leases | 跨 Node CAS 对象授权与活跃传输 | lease nonce 唯一；audit/snapshot/target/expiry/status 索引 |
| snapshot_hydration_objects | Node-local daemon CAS 对象（共享 backend） | node/blob digest 唯一；integrity/GC 状态 |
| snapshot_hydration_pins | Audit/Execution 私有 mount/ref 授权 | node/object/audit/execution 唯一；revoked/expiry 索引 |
| audit_scans | 审计聚合 | run_id 唯一；project/status/created 索引 |
| audit_phase_runs | 阶段执行 | audit/phase/idempotency 唯一 |
| audit_scope_units | Coverage 工作单元 | audit/kind/status/risk 索引 |
| audit_work_items | 可恢复 Agent/Detector 工作 | audit/phase/epoch/stable_key 唯一 |
| audit_work_item_scopes | WorkItem 的 primary/support 有界范围 | work_item/scope 唯一；role/reason edge 可追溯 |
| audit_read_receipts | 每次 Snapshot reader 实际返回范围 | audit/work_item/sequence 唯一；tool_call_id 唯一；append-only |
| audit_work_receipts | 实际读取与完成凭证 | work_item_id 唯一；digest 校验 |
| audit_detector_runs | Scanner provenance | audit/detector/status 索引 |
| audit_capsules | Content/Validation/Fix 沙箱资源账本 | run/audit/status 索引；backend/node/stop proof 持久化 |
| audit_network_egress_receipts | Broker 连接/DNS/TLS/redirect 凭证 | audit/approval/connection 索引；无 payload |
| audit_egress_sessions | Broker 活跃 client/upstream/transfer 资源 | run/audit/status 索引；admission fence 与 stop proof |
| audit_signals | 未确认线索 | audit/disposition/cluster 索引 |
| audit_signal_locations | Signal 精确位置 | signal/path/symbol 索引 |
| audit_evidence | Audit-local 内容寻址证据引用 | audit_id + evidence_digest 唯一；跨 Audit 禁止直接引用 |
| audit_evidence_locations | Evidence 与代码位置 | evidence/location 复合唯一 |
| audit_decisions | append-only 裁决 | subject/created 索引；禁止覆盖 |
| code_finding_identities | 跨扫描逻辑 Finding | project/fingerprint_version/fingerprint 唯一 |
| code_finding_occurrences | 单次观察 | audit/identity 唯一 |
| code_finding_locations | Finding 代码位置 | occurrence/role/path 索引 |
| code_finding_aliases | 重构/rename 身份关系 | from/to/version 唯一 |
| code_finding_triage | append-only Triage | identity/created 索引 |
| code_finding_validation_supplements | 原 Occurrence 与独立 Validation Audit 关系 | source occurrence/validation audit 唯一；seal root |
| audit_comparisons | Baseline 对比 | audit/baseline/identity 唯一 |
| audit_closures | 封存 Coverage | audit_id 唯一 |
| audit_distribution_revisions | append-only 报告/manifest 发布版本 | audit/revision 唯一；manifest digest 唯一；parent 可追溯 |
| audit_usage_records | token/调用/时长/worker | audit/kind/created 索引 |
| audit_model_calls | 模型请求意图、幂等能力与不确定结果 | request_id 唯一；audit/work_item/attempt 索引 |
| audit_model_egress_receipts | 远程模型脱敏外发范围与字节凭证 | audit/call/sequence 唯一；outbound digest；不存明文 |

所有多行子记录必须带 audit_id 或 project_id 并在 Repository 层验证对象归属，不能只依赖调用方传入正确 ID。

### 13.4 JSON 使用边界

允许 JSON 保存：

- 冻结的小型策略；
- 有上限的枚举列表；
- 报表 summary counts；
- 安全、版本化的结构化 Metadata。

不允许把以下内容作为大 JSON 行保存：

- 源代码；
- 完整 SARIF；
- Scanner stdout/stderr；
- 大型 Signal/Candidate 集合；
- 模型原始输出；
- 报告正文。

这些内容使用 Artifact，数据库只存 ID、digest、计数与可查询索引。

### 13.5 Repository 与映射

新增独立 Ports：

- AuditRepository
- AuditProjectRepository
- SnapshotRepository
- AuditPhaseRepository
- AuditScopeRepository
- AuditWorkRepository
- AuditSignalRepository
- AuditEvidenceRepository
- CodeFindingRepository
- AuditComparisonRepository
- AuditClosureRepository

实现放在 persistence/audit_repositories.py，映射放在 persistence/audit_mappers.py。不要继续扩大现有 repositories.py 和 mappers.py 的单文件体积。

当前 Repository 通常各自管理 session。Run + Audit 的创建不能通过两个独立 auto-commit Repository 顺序调用；必须新增 aggregate create Port 与 `AuditCreationUnitOfWork`（或等价单 session 持久化实现），禁止从 Service 顺序调用现有 auto-commit Engagement/Run Repository 和新 AuditRepository。draft 事务一次创建/验证 Engagement、创建/复用 Project、Run、RunEvent、AuditScan、AuditContract、AuditEvent、client-request/preflight reservation；start 事务一次更新 Run/Audit、消费 token、追加 Event 与 AuditStartIntent。任何一步失败都完整回滚。

每个 Repository 必须具备：

- 同 Scope 对象校验；
- 明确分页与稳定排序；
- compare-and-set 状态更新；
- idempotent create；
- terminal 状态保护；
- SQLite 并发冲突测试；
- 重启后重建投影测试。

### 13.6 通用 Finding 投影

当 CodeFindingOccurrence 变为 confirmed 时，可以创建现有 Finding：

- run_id 使用 Audit 关联 Run；
- evidence 只引用同 Run Artifact/Execution；
- status 映射为 confirmed；
- affected_assets 使用项目与相对路径摘要；
- 新增 code_occurrence_id 外键或映射表；
- 通用 Finding 更新不得反向覆盖 Code Audit 的 Decision Ledger。

这样现有 Report、Graph 和通用 Run 视图可以继续看到审计结果，同时避免把 run-scoped Finding 误当成跨版本真相。

### 13.7 迁移策略

1. 第一条迁移只增加 runs.kind 和最小 Audit 表。
2. 后续表按里程碑拆分，避免一条不可审阅的大迁移。
3. 每条迁移必须有 upgrade、downgrade、SQLite foreign key、旧数据保留和全链升级测试。
4. tests/integration/persistence/test_migrations.py 必须从最早支持版本升级到 head。
5. 迁移中不读取源码、不创建 Snapshot、不回填虚假的 Finding 身份。
6. 3.0 Alpha 前允许调整尚未发布的 Audit 表；发布后只能通过新迁移演进。

## 14. Temporal 编排

### 14.1 可靠启动与独立 Workflow

数据库提交与 Temporal Start 之间必须使用持久 `AuditStartIntent`，不能在 HTTP handler 中执行“先 commit，再 best-effort start”：

~~~text
intent_id
audit_id
run_id
start_request_id
contract_digest
workflow_id
task_queue
status: pending | claimed | started | retryable | outcome_unknown | cancelled
attempt
lease_owner
lease_expires_at
next_attempt_at
last_error_code
created_at
started_at
~~~

Start Application Service 在一个 `AuditCreationUnitOfWork`/aggregate transaction 中校验 draft 与合同、转为 queued、消费 Preflight token、写 Run/Audit Event 与 StartIntent。提交后由 API background dispatcher 或独立 reconciler claim intent，并使用确定性 `workflow_id = riftx-code-audit-{audit_id}` 启动 Workflow；Temporal 的 Workflow ID reuse/conflict policy 必须使重复投递返回同一个执行而不是第二个扫描。成功后 CAS 标记 started。

Control Plane 在启动及周期性 reconciliation 时扫描 pending、租约过期和 queued-without-started-intent 状态，并同时查询 Temporal/数据库修复投影。进程在 DB commit 后、Temporal RPC 前后任意崩溃都必须通过集成故障注入证明：最终只存在一个 Workflow，或保留明确可重试/`outcome_unknown` 状态；绝不能永久停在 queued。Cancel 若先于投递到达，原子取消 Intent，dispatcher 不得再启动。

新增 RiftXCodeAuditWorkflow。输入只包含：

~~~text
audit_id
run_id
workflow_schema_version
~~~

Workflow 不直接访问数据库或文件系统。每一步通过 Activity 获取/提交 ID 与小型状态。

### 14.2 主流程

~~~text
prepare_audit_activity
materialize_snapshot_activity
build_inventory_activity
plan_detector_jobs_activity
run_detector_child_workflows
normalize_detector_results_activity
build_threat_model_activity
plan_hunt_epoch_activity
run_hunt_child_workflows
reduce_signals_activity
plan_validation_activity
run_validation_child_workflows
compose_risk_activity
compare_baseline_activity
validate_audit_closure_activity
cleanup_audit_activity
seal_audit_core_activity
generate_audit_reports_activity
seal_audit_manifest_activity
~~~

Standard 完成一个 Hunt Epoch；Deep 执行多个 Epoch；Diff 使用独立 Scope planner，但最终共用 Reconcile、Proof、Risk 和 Closure。领域 Closure 通过后先围栏并清理所有效果，使关联 Run 达到可报告终态；随后封存权威事实 core root、生成引用该 root 的报告，最后生成 distribution manifest。

这一顺序保留现有 ReportApplicationService 的“Run 终态后才可报告”安全约束。若报告生成失败，Run 和 core seal 可以保持已完成/不可变，但 Audit 必须是 completed_partial/report_failed，并允许只重试报告与 distribution manifest；不得重新启动扫描或改写 Finding。

### 14.3 Activity 规则

- Activity 开始前创建或 claim AuditPhaseRun/AuditWorkItem。
- 外部副作用前持久化 execution key。
- 成功结果先写数据库/Artifact，再返回 Temporal。
- 重试先查 idempotency key，已有 terminal 结果直接返回。
- 长任务每 30 秒或更短 heartbeat，并在 heartbeat detail 中只保存安全小对象。
- 超时、取消、解析失败、能力缺失和预算耗尽使用不同 error code。
- 不把 Python exception repr 原样返回 UI。

模型调用遵守 at-least-once 现实：调用前先持久 `ModelCallIntent/Attempt`（request_id、WorkItem、payload/schema/model digest、adapter、idempotency capability、预算 reservation、status）。Adapter 若支持服务端 idempotency/retrieve，重试必须复用同一 request_id 并先 reconcile；若不支持，Worker 在“请求可能已被 Provider 接收、结果尚未持久化”边界崩溃时，把 attempt 标为 `outcome_unknown`，不得自动再次调用。Policy 可将 WorkItem deferred/partial，或由 Operator 显式创建新 attempt；所有已知/可能被接收的 attempt 都计入最坏情况调用与 token 预算，并显示 usage uncertainty。故障注入必须覆盖 send 前、Provider accept 后、typed Packet 持久化前后三个边界。

因此 exactly-once 门禁只适用于 RiftX 自己拥有、可按 execution/workflow ID reconcile 的副作用；不支持幂等协议的外部模型只能保证“不静默重复不确定调用”，不能声称零重复计费。

推荐幂等键：

~~~text
SHA256(
  audit_id
  + phase
  + scope_unit_or_job_key
  + operation_kind
  + input_digest
  + config_digest
)
~~~

### 14.4 Child Workflow

以下工作使用 Child Workflow，不使用一个 Activity 内的 asyncio.gather：

- Detector shard；
- Hunt worker；
- Skeptic/Proof worker；
- Deep Epoch；
- 大型报告导出（必要时）。

Child Workflow ID 必须由 audit、phase、epoch 和 stable work key 确定生成。Parent 重放时不得生成不同 ID。

### 14.5 Signal 与 Query

Signals：

- pause
- resume
- cancel
- approval_decided
- execution_completed
- retry_deferred_work

Queries：

- status
- current_phase
- progress_counts
- active_work_items
- pending_approvals
- budget_usage

Query 只返回 Workflow 已知的小型状态；完整详情由 API 查询数据库。

### 14.6 Pause、Cancel 与失败

- Pause 先停止调度新 WorkItem，再等待当前安全点；已经运行的静态只读 job 可按策略完成或中断。
- Cancel 立即围栏新效果，并进入现有 RunSafetyStopService 的全资源停止流程。
- 只有所有效果均有停止证明，Audit 才进入 cancelled。
- Activity fatal error 进入 failed 前也必须执行清理。
- 清理无法证明完成时，Workflow 保持可控制状态，不把错误关闭成终态。
- 清理与 Run 终态证明完成后，failed/cancelled 也继续执行 partial core seal、incomplete report 与 distribution package；seal/report 失败只允许重试发布链，不重新调度审计工作。

`audit_capsules` 与 `audit_egress_sessions` 都是 Run effect family，不得只藏在 Activity/Broker 内存。M2 起扩展 `RunSafetyStopService` required resource types，同时注册 `AuditCapsuleStopper` 与 ledger-backed `AuditEgressStopper`（Broker 尚未启用时也要证明 active session 为零）；M4 把 EgressStopper 接上真实 Broker controller。前者按 run_id 枚举 content/validation/fix Capsule 并调用 backend stop/destroy；后者围栏新网络并终止 client/upstream/transfer；两者返回 observed/confirmed status、node/session、stop proof 与 failures。API、Worker、Broker runtime 的所有 SafetyStop 组合根必须注入对应 stopper；缺失 controller/proof 一律使 Cancel 未确认。这些 stopper、Runner callback 和 reconciliation 在 `audit.enabled=false` 时也必须存在。

### 14.7 Continue-As-New

在以下边界评估 Continue-As-New：

- 每个 Deep Epoch 后；
- WorkItem/Signal 数达到配置阈值；
- Temporal History 达到安全阈值；
- 长时间等待人工审批后恢复。

传入下一次 Workflow 的只有 audit_id、run_id、epoch、phase 和必要控制序列；其余状态从数据库恢复。

### 14.8 Worker 接线

修改：

- temporal/runtime.py：注册 Audit Workflow、Child Workflow 和 Activities。
- temporal/worker_runtime.py：组装 Audit repositories、services、detectors、agent runner 和 sandbox。
- api/runtime.py：ControlPlane 增加 AuditApplicationService，但不要在 Control Plane 本进程执行 Scanner。
- config.py：增加 AuditConfig 与 AuditSandboxConfig。

Temporal task queue 可以继续使用现有队列开发；生产建议允许单独配置 audit_task_queue 与 audit_sandbox_task_queue，便于资源和权限隔离。

## 15. 扫描与验证安全边界

### 15.1 硬阻塞结论

现有 Linux cgroup 解决进程树所有权和停止证明，不等于文件系统、网络或凭据沙箱。“只读静态分析”也会把敌对字节送入 Git、SARIF、AST、CFG、正则和解码库，并非天然安全。在以下能力完成前，不允许在带 Control Plane、Temporal、模型或云凭据的宿主进程解析真实不可信仓库，也不允许启用 Scanner、构建、测试或 PoC：

- 源码只读挂载；
- 独立输出可写目录；
- root filesystem 只读或等价隔离；
- 默认禁网；
- clean environment；
- 无宿主 socket 与凭据；
- 资源限制；
- 路径逃逸防护；
- 可证明停止。

### 15.2 最小 Content Processing Sandbox

M2/M3 必须先交付最小 `AuditContentSandbox`，M7 再在同一 Backend contract 上增加 Build/Test/PoC/Fix。SourceIngest/Git object/index/config、Inventory 内容解析、language parser、Secret/Structural/Config Detector、dependency/SBOM、SARIF parser 与外部 Scanner均在对应低权限 profile 执行；SafeGitAdapter 不是宿主例外：

- Snapshot/CAS 以只读、单 Snapshot 挂载；独立限额 out/tmp；
- 非 root、只读 rootfs、无宿主 home、Docker/SSH/cloud socket；
- clean env，不注入模型、Temporal、数据库、Runner bootstrap 或 Registry 凭据；
- 默认无网络、CPU/memory/pids/time/file/output 限制；
- 输入/output digest、image/backend proof、process identity 和 stop receipt 可追溯；
- parser 只输出 bounded typed result/Artifact handle，不能直接连接数据库。

Control Plane/普通 Temporal Worker 只处理 source-root dirfd 授权、有上限的 schema、digest 和 ID；不得 import 后直接调用敌对 Git/SARIF/AST parser。macOS 开发机可用可信自建 fixture 和 fake sandbox 测试 contract；没有合格 source-ingest/content sandbox proof 时，真实仓库的 Preflight/Snapshot/native/static parsing 必须显示 `audit_sandbox_unavailable`，不能只把动态验证标为 unavailable。

### 15.3 ValidationPolicy

~~~text
static_only
isolated_build
isolated_test
isolated_poc
isolated_fix_and_retest
~~~

默认 static_only。更高等级不能由模型自行提升。

| 操作 | 默认策略 | 是否审批 | 网络 |
| --- | --- | --- | --- |
| Snapshot 读取 | 允许 | 否 | 无 |
| 内置只读 Detector | 允许，但必须在 Content Sandbox | 否 | 无 |
| local_only 模型 | 允许，固定本地 transport | AuditContract consent | 无 broker/Internet |
| remote_redacted 模型 | 仅冻结 provider/origin/redaction/总预算 | Audit-level egress consent | Broker-only |
| 已注册外部 Scanner | 允许，需受控 Runner | 按策略 | 无 |
| Build/Test | 禁止，除非选择 isolated_build/test | 是 | 默认无 |
| PoC | 禁止，除非 isolated_poc | 每个计划审批 | 默认无 |
| 依赖下载 | 禁止 | 单独审批 | 仅 allowlist |
| Fix worktree 写入 | 显式触发 | 是 | 无 |
| 原仓库写入 | 永久禁止 | 不可批准 | 不适用 |

### 15.4 AuditSandboxBackend

定义 backend-neutral AuditSandboxBackend：

~~~text
prepare(capsule_spec) -> capsule
execute(capsule, execution_spec) -> execution_id
inspect(capsule) -> filesystem_delta
stop(capsule) -> stop_proof
destroy(capsule) -> destruction_receipt
~~~

首个生产 backend 目标为隔离 Linux 容器或 VM。最小 Content Sandbox 在 M3 前可用；M7 扩展动态执行能力。没有对应 backend proof 时相关静态或动态 capability 分别显示 unavailable，禁止宿主降级。

Capsule 约束：

- /workspace/src 只读；
- /workspace/out 独立可写；
- /tmp 为限额 tmpfs；
- 非 root 用户；
- no-new-privileges；
- 删除 Linux capabilities；
- seccomp/AppArmor 或等价策略；
- 禁止挂载 Docker socket、SSH agent、云配置和宿主 home；
- EnvironmentMode.CLEAN，加最小 PATH/locale；
- 网络 none；需要目标网络时按 Scope allowlist 创建新 Capsule；
- CPU、memory、pids、wall time、disk、file count、output bytes 限制；
- 镜像、工具、规则和策略均记录 digest。

### 15.5 AuditEgressBroker

任何 Capsule 都没有直接 DNS/Internet 路由；获批网络必须经 RiftX `AuditEgressBroker`。Approval 冻结 scheme、host、port、path scope、DNS answer/IP policy digest、TLS 要求、最大连接/字节和 expiry：

- broker 使用受信 resolver，每次连接/TTL 过期重解析并校验所有 A/AAAA；DNS rebinding 或 answer 超出批准 policy 时拒绝；
- 默认拒绝 loopback、private、link-local、multicast、reserved、Unix socket、宿主网关与 cloud metadata（含 `169.254.169.254`/IPv6 等价）；metadata 永不可批准，private target 需要独立 exact CIDR/port plan；
- TLS SNI/Host/certificate 必须匹配批准 origin，禁止明文 downgrade、user-info URL 和未批准 IP literal；
- redirect 每一 hop 重新执行 origin/IP/TLS 校验，跨 origin 默认拒绝并要求新 Approval；
- Capsule 只能连 broker，不能自带 resolver/proxy 绕过；broker 防止 CONNECT/协议走私和响应/下载洪泛；
- 每个连接保存 origin、resolved IP、TLS peer digest、redirect chain、字节数、时间、Approval/plan digest 的 `NetworkEgressReceipt`，不保存 secret payload。

Broker 在打开 socket 前持久化 `audit_egress_sessions` 并检查 Run-scoped admission fence；记录 client/upstream/transfer 状态，完成后才转 Receipt。新增 `AuditEgressStopper` 作为 RunSafetyStop required resource：Cancel 先关闭新 session admission，再关闭本地 client/upstream socket、终止 DNS/下载/model stream、丢弃 bounded buffer，循环确认活跃 session 为零并返回 stop proof。API/Worker/Broker 组合根和 feature-disabled cleanup 都必须注入该 stopper，不能只停止 Capsule。

远程 Provider 已接受的模型请求可能无法证明其服务器停止；本地 stopper 必须证明不再发送/接收，并把 ModelCall 标记 `provider_outcome_unknown`、拒绝任何迟到结果落库/触发 Decision，同时按最坏情况计预算。UI 显示 provider outcome unknown，不能把“本地 socket 已关”描述为远端推理已取消。

远程模型 egress 与依赖/registry 下载都使用该 broker 的不同 policy class。`local_only` 使用验证过的本地 IPC/in-process transport，不进入 Internet broker，也不把 loopback/Unix socket 普遍开放给 Capsule。批准一个域名或 registry tag 不等于批准其重定向、私网 IP 或任意端口。

### 15.6 动态验证审批内容

审批卡必须显示：

- Finding/Candidate；
- 验证目的；
- 参数化命令；
- Snapshot 与临时 worktree；
- 需要的文件写入；
- 网络策略与目标 allowlist；
- 资源和时间上限；
- 环境变量差异；
- 预期 Evidence；
- 风险与回滚方式。

审批后任何命令、Scope、网络或镜像变化都使原审批失效。

为 Code Audit 新增 `mandatory_one_plan` 审批语义。Build、Test、PoC、依赖/registry 下载、目标网络、Fix 写入和 Retest 执行永远不能被 Run 的 `ApprovalMode.AUTO`、已有 `ApprovalGrant` 或 `approve_for_run` 绕过；Audit API 对这些 decision 返回 `approve_once` 或 reject，提交 `approve_for_run` 必须是 `audit_approval_scope_forbidden`。

远程模型是单独的 Audit-level `ModelEgressConsent`：Start 时对固定 contract/provider/origin/redaction policy/max calls/max bytes/expiry 一次确认，每个 ModelCall 仍做 admission、budget、broker 与 EgressReceipt，但不复用一次性动态 Approval，也不要求每 call 人工点击。任何合同字段变化需要重新 Review/Start consent。该 consent 不能授权 dependency、PoC、target 或其他网络；`local_only` 不产生远程 consent。

每个 `AuditExecutionPlan` 以 canonical schema 冻结并计算 `plan_digest`，至少包含 Audit/Snapshot/Finding、operation、argv、node/backend、只读 mount、唯一 writable roots、egress broker origin/resolved-IP/TLS/redirect policy digest、clean env diff、CPU/memory/pids/time/disk/output、输入/预期输出 digest 和 policy version。所有供应链对象必须绑定 bytes 而非名字：OCI manifest/rootfs digest（tag 仅显示）、executable SHA-256、签名 Tool Registry revision、interpreter/compiler/runtime digest、rulepack/parser/config/lock digest、registry metadata snapshot 与签名/allowlist 验证结果。mutable tag、PATH lookup 或普通文件路径不能作为执行身份。

这些 digest 必须在 Review 前解析；Approval 保存完整 plan digest，admission、sandbox prepare 与 Runner 真正 execute 前都从实际 bytes 重新计算并常量时间比较。解析结果、签名状态、tool/image/rulepack 任一变化都使 Approval 失效并回到 Review。一个 Approval ID 只能原子消费一次且只能用于该 plan；任何命令、backend、网络或资源上限变化也创建新 Approval。Operator 对部署事实的确认不能替代技术执行 Evidence。

这要求扩展现有 Approval domain/schema/UI，不能只复用当前 `tool_call_id + command` 字段。`AuditApprovalCard` 必须是可复用组件，并显示 mandatory-one-plan 状态；通用 Run 页面现有私有审批渲染函数不得被复制。

### 15.7 修复与 Retest

Fix Advisor 只产生结构化建议。操作员选择生成补丁后：

1. 从 Snapshot 创建临时 Git worktree/overlay。
2. 记录 base tree digest。
3. Agent 仅在该临时副本写入。
4. 生成 patch Artifact 与文件变更摘要。
5. 运行批准的回归测试与目标验证。
6. 把 patched overlay 物化并封存为新的 SourceSnapshot，保存 `parent_snapshot_id + base_tree_digest + patch_digest`；绝不修改或复用原 Snapshot bytes。
7. 创建 Retest Audit，关联原 Occurrence、新 Snapshot 和 patch digest。只可复用 scope policy 与稳定 target anchors；ScopeUnit、Receipt、Evidence、Occurrence 和 Closure 必须针对新 blob 重新生成。
8. UI 提供补丁下载或人工复制；3.0 不自动提交、推送或合并。

## 16. API 契约

### 16.1 路由

~~~text
POST /api/v1/audits/preflight
POST /api/v1/audits
POST /api/v1/audits/{audit_id}/start
GET  /api/v1/audits
GET  /api/v1/audits/{audit_id}
POST /api/v1/audits/{audit_id}/pause
POST /api/v1/audits/{audit_id}/resume
POST /api/v1/audits/{audit_id}/cancel
GET  /api/v1/audits/{audit_id}/phases
GET  /api/v1/audits/{audit_id}/coverage
GET  /api/v1/audits/{audit_id}/threat-model
GET  /api/v1/audits/{audit_id}/signals
GET  /api/v1/audits/{audit_id}/findings
GET  /api/v1/audits/{audit_id}/evidence/{evidence_id}
GET  /api/v1/audits/{audit_id}/compare
POST /api/v1/audits/{audit_id}/reports
GET  /api/v1/audits/{audit_id}/reports
GET  /api/v1/code-findings/{finding_id}
POST /api/v1/code-findings/{finding_id}/triage
POST /api/v1/code-findings/{finding_id}/occurrences/{occurrence_id}/validate
POST /api/v1/code-findings/{finding_id}/occurrences/{occurrence_id}/fix
POST /api/v1/code-findings/{finding_id}/occurrences/{occurrence_id}/retest
~~~

Artifact 下载继续使用现有认证下载能力。Audit 事件继续使用关联 run_id 的现有 SSE；AuditResponse 必须返回 run_id 供客户端接线。

Validate/Fix/Retest 不能只绑定长期 logical finding。请求体还必须带 `source_audit_id + source_snapshot_id + expected_snapshot_digest + expected_occurrence_decision_digest + client_request_id`；Retest 再带 patch/base digest。Service 验证 path 中 finding/occurrence、Audit、Snapshot、Evidence 和当前 Decision 全部同域且未过期，任何 mismatch 返回 `audit_occurrence_stale`。不提供 occurrence 的逻辑 Finding action 不实现“自动选最新”；UI/CLI 必须让 Operator 明确选择要验证/修复的 Observation。

Occurrence validate 是 sealed 后的 follow-up，不改写原 Audit：source Audit/Occurrence/core seal 必须已封存；endpoint 原子创建 `purpose=validation_followup` 的新 Audit/Run，引用同一 immutable Snapshot，但重新生成有限 ScopeUnit、Receipt、Approval、Execution、Evidence、Decision 与自己的 core seal。结果通过 `code_finding_validation_supplements(source_occurrence_id, validation_audit_id, result_occurrence_id, seal_root)` 追加关联，UI 分栏显示“original scan”与“follow-up validation”。原 Occurrence disposition/Evidence/core 不变。source Audit 尚未 sealed 时返回 `audit_occurrence_not_sealed`；运行中验证只由原 Workflow 的 Candidate plan 完成，不通过该 endpoint。

Fix 同样只产生新 plan/patch Artifact；Retest 对 patched bytes 创建第 15.7 节的新 Snapshot/Audit。任何 follow-up 失败都不能回写 source Decision。

### 16.2 PreflightRequest

~~~json
{
  "repository_path": "/absolute/local/path",
  "source_execution_target": {
    "node_id": "source-node-id",
    "source_ingest_backend": "linux_container"
  },
  "target": {
    "kind": "working_tree",
    "revision": "HEAD",
    "base_revision": null,
    "include_untracked": false
  },
  "include_paths": [],
  "exclude_paths": [],
  "mode": "standard"
}
~~~

`SourceTargetKind` v1 只包含 `revision | working_tree`；Diff 不增加第三种 target kind，
只由 `AuditMode.DIFF` 表示。Diff 必须同时冻结不同的 base/head revision，非 Diff 禁止
base_revision；revision target 禁止 include_untracked。repository_path 必须是 node-local
绝对 canonical path：POSIX 禁止 dot segment、重复 separator 和非 root 尾 `/`；Windows v1
只接受大写 drive + `/`，或 lowercase server 的 `//server/share/...` UNC 形式，不接受反斜线、
重复 separator、尾 separator 或 home expansion。原路径仍是敏感合同字段，不进入 Event/API。

绝对路径是 node-local，不能在未选 Node 时解释。Operator 必须指定 `source_execution_target`，或先调用服务端确定性 eligible-node selection 并把结果放入请求。Control Plane 根据 operator-approved Node/source-root inventory 授权；Source Runner 在打开前再次 realpath/dirfd 校验自己的 allowed roots，双方任一拒绝即失败。禁止仅信 Runner 自报 capability。

Preflight 在该 Node 的 SourceIngestCapsule 中运行，Response/token 绑定 `source_node_id + source_root_identity_digest + repository_identity_digest + backend/image/policy digest + capsule_prepare_proof_digest + content digest`。Start/Snapshot 必须在同一 source Node 重新证明路径 identity 与内容；同名路径在另一 Node 不可替代。若 analysis `execution_target.node_id` 不同，只把已封存、逐 blob 校验的 CAS Snapshot 经认证 content-addressed channel 传输/水合到目标 Node，绝不传原绝对路径或让目标 Node重读源仓库。

### 16.3 CreateAuditRequest

~~~json
{
  "client_request_id": "uuid",
  "preflight_token": "opaque-signed-token",
  "project_name": "RiftX",
  "engagement_id": null,
  "mode": "standard",
  "analysis_profile": "hybrid",
  "model_profile": "primary",
  "model_data_egress": {
    "mode": "local_only"
  },
  "validation_policy": "static_only",
  "baseline_audit_id": null,
  "execution_target": {
    "node_id": "node-id",
    "required_sandbox_backend": "linux_container"
  },
  "budget": {
    "max_wall_seconds": 7200,
    "max_model_calls": 100,
    "max_input_tokens": 2000000,
    "max_output_tokens": 200000,
    "max_worker_jobs": 64,
    "max_epochs": 8,
    "max_candidates": 1000
  }
}
~~~

preflight_token 是高熵 opaque value，服务端只持久化 hash，并绑定规范化目标、Scope、预检时间、expiry 和内容摘要。创建 draft 时 token 原子地 `reserved` 给 audit_id；同一 token 不能创建第二个 Audit，同一 token/client_request_id 的重试返回原 Audit。Start 时重新检查目标；若工作树内容已经变化，返回 snapshot_changed，并把 draft 标为需要重新 Preflight，不得扫描与预检不同的输入。只有 Start 事务成功写入 `AuditStartIntent` 时 token 才进入 `consumed`。

client_request_id 是幂等键。HTTP 响应丢失后用相同 ID 重试必须返回同一 Audit。

`engagement_id` 可引用已授权的现有 Engagement；为空时 UoW 为新 CodeProject 创建 Engagement。复用时必须验证 authorization_reference/source policy 与 Project 对象域，不允许借任意 Engagement 绕过 source authorization。

`POST /audits` 只创建 `draft`，不再次接触 Git、不物化 Snapshot、不启动 Temporal。关联 Run 的 `node_id` 来自 analysis `execution_target`；source node/backend/proof 来自绑定的 Preflight plan。Node 自报 capability 只作提示：Preflight 已获得 source-ingest prepare proof；Start 重新校验 source/analysis Node，后续每次 content/dynamic `sandbox.prepare` 再验证实际隔离能力。自动选择必须在 Review 前完成，选择算法、候选集合、source/analysis node/backend/image policy 均冻结到 AuditContract。

`POST /audits/{audit_id}/start` 接受独立 `start_request_id + reviewed_contract_digest`，重新验证 preflight plan、目标 digest、冻结合同、ModelDataEgress consent、Node/backend 与 Feature Flag，并在同一数据库事务中把 Audit 转为 queued、消费 token、写 `AuditStartIntent`。任何自动选择、origin、image/policy 或 capability 摘要变化都返回 `audit_contract_review_required`，由 UI 展示新合同后重新确认。它只返回已持久化的启动意图；Temporal 启动由第 14.1 节可靠投递协议完成。

### 16.4 AuditResponse 最低字段

~~~text
id
run_id
project
snapshot
base_snapshot
mode
analysis_profile
lifecycle_status
current_phase
closure_status
publication_status
core_seal_root
initial_distribution_revision
latest_distribution_revision
distribution_revision_count
progress_counts
budget
usage
baseline
pending_approval_count
execution_target_summary
model_data_egress_summary
created_at
started_at
analysis_finished_at
publication_finished_at
~~~

`execution_target_summary` 必须分别显示 source/analysis node、backend、CAS handoff、immutable image/policy digest 与 capability/prepare proof 状态；`model_data_egress_summary` 显示 local/remote、provider/origin 安全摘要、redaction/retention disclosure 和批准状态。绝对 Runner 路径、源代码、模型原始输出和凭据不得进入摘要响应。

Response 使用 lifecycle discriminated union：`draft/queued/preflighting/snapshotting` 的 `snapshot` 可以为 null，并有 `snapshot_status`；Snapshot sealed 的原子事务填入 snapshot_id 后，`running` 及后续状态 DB CHECK 要求非空。Diff 的 base/head 采用同样规则。前端不能对 draft 强制解引用 Snapshot。

UI/CLI 必须直接显示 `publication_status`：仍在 sealing/reporting/packaging、seal_failed、report_failed、package_failed 与 published 不得从 lifecycle/Artifact 数量猜测。Revision 摘要含 revision id、manifest digest、composer/schema digest、created_at 和是否 latest；retry 后通过 SSE/GET 更新权威投影。

### 16.5 列表与分页

- Audits、Signals、Findings、Evidence 和 Phase 列表必须使用稳定 cursor 或明确稳定排序。
- Audit filters 包括 run_id（唯一映射）、project、status、mode 和 created range；Finding/Signal filters 包括 severity、confidence、CWE、validation、history_state、path。
- Cursor 必须签名并绑定 Scope、filter、sort 与 snapshot version；拓扑变化时返回 stale cursor。
- 默认 page size 50，最大 200；不允许无界查询。

### 16.6 错误码

至少定义：

- audit_source_not_allowed
- audit_repository_invalid
- audit_target_invalid
- audit_snapshot_changed
- audit_snapshot_too_large
- audit_capability_unavailable
- audit_model_profile_unavailable
- audit_model_egress_not_approved
- audit_contract_review_required
- audit_budget_invalid
- audit_budget_infeasible
- audit_already_sealed
- audit_not_controllable
- audit_workflow_unavailable
- audit_run_state_conflict
- audit_sandbox_unavailable
- audit_validation_not_approved
- audit_approval_scope_forbidden
- audit_cross_scope_reference
- audit_occurrence_stale
- audit_occurrence_not_sealed
- audit_contract_invalid
- audit_closure_incomplete
- audit_seal_failed
- audit_cursor_invalid
- audit_cursor_stale

错误 Envelope 继续沿用 RiftX 现有统一 APIError。敏感路径和命令输出不得进入 message。

### 16.7 API Policy

所有路由必须在 api/policy.py 逐路由显式登记 `RouteEffect`，不能用“Audit 路由”通配：

- 普通 GET：`READ_ONLY`；
- `POST /audits` 草稿、Triage 和纯数据库报告重建：`DURABLE_WRITE`；
- Preflight 因读取宿主路径并执行受限 Git/object 操作：`HOST_EXECUTION`；
- `POST /audits/{id}/start`、validate、fix、retest 以及任何启动 Detector/Build/PoC 的端点：`HOST_EXECUTION`；
- pause/resume：`WORKFLOW_CONTROL`；Audit cancel、强制停止和 Capsule 清理：`HOST_CONTROL`；
- Runner/worker 回调继续使用现有 runner 身份，不允许 local operator route 冒充。

policy inventory test 必须枚举每个新 route name，并证明未知路由 fail-closed；不能把 `create_audit` 登记成普通 `DURABLE_WRITE` 后在 handler 内顺手启动 Workflow。

LocalObjectAuthorizer 必须验证 Audit、Run、Project、Finding、Artifact、Execution 属于同一授权对象；跨 Scope 统一返回不可区分的 404。

## 17. Event、Artifact 与输出契约

### 17.1 Event

使用关联 Run 的 RunEventRepository，事件命名：

~~~text
audit.created
audit.preflight_completed
audit.snapshot_started
audit.snapshot_sealed
audit.phase_started
audit.phase_completed
audit.phase_failed
audit.inventory_completed
audit.detector_started
audit.detector_completed
audit.detector_failed
audit.work_item_started
audit.work_item_completed
audit.work_item_deferred
audit.signal_discovered
audit.signal_reconciled
audit.validation_requested
audit.validation_completed
audit.finding_confirmed
audit.finding_rejected
audit.coverage_updated
audit.baseline_compared
audit.contract_sealed
audit.report_generated
audit.completed
audit.completed_partial
~~~

Event payload 只允许 ID、枚举、计数、digest 摘要和安全状态。不得写源代码、完整路径、Scanner 输出、Prompt 或模型回复。

### 17.2 Artifact 分类

必须产生或允许产生：

- audit-contract.json
- snapshot-manifest.json
- inventory.json
- threat-model.json
- detector 原始输出
- normalized-signals.json
- work-receipts.json
- evidence objects
- decision-ledger.json
- findings.json
- coverage.json
- comparison.json
- results.sarif
- report.md
- report.html
- report.json
- audit-core-seal.json
- audit-manifest.json
- fix.patch
- retest.json

扩展 Artifact 领域字段：`audit_id`（可选 FK）、`access_class: public_export | audit_internal | restricted_sensitive`、`content_trust: generated | untrusted_source | untrusted_tool_output`、ingest provenance 与 immutable storage key。大型中间 Artifact 默认 `restricted_sensitive`；通用 list/download 必须按 access class 和 Audit ownership 服务端过滤，restricted 只能从显式 Audit 对象授权路由访问，不能仅在 UI 隐藏。401/403 后客户端全局清除所有 Audit snippet、Evidence 与 Artifact 缓存。

敌对 Scanner/parser 输出不得沿用“先 resolve path、稍后再次 open/copy”的注册方式。Runner 优先通过认证的 bounded chunk/fd stream 上传：服务端写私有 staging、边读边执行总量/单文件限制与 SHA-256、完成后 fsync + 原子 rename。若本地 backend 必须摄取文件，使用受信 output-dir 的 dirfd/openat2 或等价 `O_NOFOLLOW` 打开并始终持有同一 fd，fstat 校验 regular file、dev/inode/link/size，复制后再 fstat；路径替换、symlink/hardlink、增长文件或超限立即失败并清理 staging。任何来源都不能让 Control Plane 按 Runner 提供的任意绝对路径重新打开文件。

### 17.3 Core Seal 与 Distribution Manifest

封存使用无哈希环的两层结构：

1. `audit-core-seal.json` 在正常 Closure 通过、或 failed/cancelled 的 partial terminal Closure 已确定，并且所有效果停止、Run 终态后生成。它对每个权威投影的 canonical ledger root 做承诺：Snapshot/Scope、Read/Work Receipt、DetectorRun/Signal、Evidence/Location、Decision、Finding/Occurrence、Comparison、Closure、Usage、ModelCall/Egress、Approval/Execution/Capsule/Network receipt provenance；并列出所有被引用的 public/restricted Evidence 与中间 Artifact 的名称、access class、size、SHA-256。它排除报告、distribution manifest 和 core-seal 容器自身。其 `core_seal_root` 写入 Audit DB 与 append-only Event。
2. Markdown/HTML/JSON/SARIF 报告只嵌入 `core_seal_root`，从该已封存事实生成，不能嵌入尚不存在的 distribution manifest digest。
3. `audit-manifest.json` 是外层 distribution manifest，hash `audit-core-seal.json`、所有报告和发布 Artifact，但明确排除自身 bytes。其 digest 写入 Audit DB/Seal Event，不回写报告或自身内容。

Canonical projection 必须固定 schema version、排序、分页拼接和 null/时间编码；Finalizer 从数据库重算 ledger roots，并重新读取 Artifact metadata/digest，不能信任 Agent 或旧报告。Access class 只是授权 metadata，绝不是免除 seal 的理由。

`audit-manifest.json` 使用 RiftX 自有版本化 Schema，例如 riftx.code-audit.manifest.v1，至少包含：

- Audit/Run/Project/Snapshot ID；
- target、Scope、mode、profile 的安全摘要；
- config/policy/budget/rulepack/model profile digest；
- `core_seal_root`、每个 Phase 的 terminal state 与 output digest；
- Coverage Closure；
- Finding/Occurrence ID；
- 所有 distribution Artifact 的名称、access class、MIME、size、SHA-256；
- 生成器版本；
- sealed_at。

报告重建必须先验证 core seal；数据库或 restricted Artifact 被改写时，即使公开报告未变，也必须产生 seal mismatch 并拒绝声称是原封存 Audit。

失败/取消不是跳过封存：cleanup + stop proof 后生成 `terminal_outcome=failed|cancelled` 的 partial core seal，包含已产 Evidence、所有未完成/不可用范围、错误 taxonomy 和 cleanup proof；报告标题与每页状态明确标为 incomplete/cancelled。若 integrity/storage 损坏使 core seal 本身无法生成，保存 `seal_failed` publication status 和可重试诊断，不发布“sealed”报告；原始数据库/Artifact 保留，修复后只允许重试 seal/report/package，不得重跑或改写扫描事实。

Post-seal 报告永不覆盖旧 bytes。首次发布创建 `AuditDistributionRevision(revision=1, core_seal_root, composer/schema digest, artifact digests, distribution_manifest_digest)`；`POST /audits/{id}/reports` 在验证同一 core root 后：

`sealed_at` 表示 core seal 时间；`publication_finished_at` 表示最新成功 distribution revision 完成时间，二者不能复用。`publication_status`、core root 和 initial/latest revision FK 是 AuditScan 权威投影，并通过 CAS 与 revision transaction 更新。

- 同 composer/schema/input digest 的幂等重试返回原 revision，报告时间取 core seal 时间，保证可复现；
- composer/schema/locale 或选择的 post-seal Triage/Alias/Validation Supplement/Retest 投影变化时创建 revision N+1，保存 parent manifest digest 与 supplemental state root；
- 每个 revision 的 reports 与 distribution manifest 都是 immutable Artifact，manifest 排除自身；API 的 `latest` 只是数据库指针，旧 revision 始终可列出、下载和验证；
- post-seal Triage/Retest 不回写 scan core seal，报告必须把 `core-sealed scan facts` 与 `supplemental state as of ...` 分栏并分别给 digest；
- rebuild/retry 不改变 AuditClosure、Finding Decision、Run status/finished_at 或原 `sealed_at`。

若 revision 生成中断，持久 revision intent/status 并只重试该发布副作用；不能留下一个 Artifact 名称被新 bytes 替换，也不能把 N+1 digest 回写 N 的 manifest。

### 17.4 报告

报告由确定性 Composer 生成，模型可以提供经过验证的修复建议，但不能自由生成事实章节。

报告顺序：

1. Executive Summary。
2. Target 与不可变 Snapshot。
3. Scope、模式、配置与限制。
4. Coverage Closure。
5. Findings Summary。
6. 每个 Finding 的代码位置、CWE、Severity、Confidence、Evidence 等级。
7. Source/Control/Sanitizer/Sink 路径。
8. Attack Path 与前置条件。
9. Proof、反证与 Proof Gap。
10. 修复建议与 Retest。
11. Baseline 比较。
12. Detector/Agent provenance。
13. 排除项、延期项和失败能力。
14. Artifact Index 与封存 digest。

禁止使用“仓库安全”“没有漏洞”“审计完整无遗漏”。零 Finding 时必须写：

> 在本文档记录的 Scope、能力和 Coverage 下，没有确认新的 Finding；这不代表不存在漏洞。

仓库文件名、代码、Scanner/model 文本和 Evidence 全部是不可信展示数据。Composer 对 HTML 做 context-aware escaping，禁止把任何输入当模板/HTML；Markdown 禁 raw HTML、javascript/data URL 与不受控图片/链接。`report.html` 无 script/form/network，带严格 CSP，下载使用安全 RFC 5987 filename 与 `Content-Disposition: attachment`。Web Report tab 默认从结构化 report.json 用 React 文本节点渲染，不在同源 DOM inline HTML；如必须预览 HTML，只能使用 opaque-origin sandbox iframe，不能同时授予 `allow-scripts` 或 `allow-same-origin`。恶意 `<script>`、SVG、事件属性、Markdown link、双向控制符、路径/Content-Disposition 注入 fixture 必须通过 stored-XSS 测试。

### 17.5 SARIF

- 输出 SARIF 2.1.0。
- rule.id 使用 RiftX weakness/rule identity，不使用模型标题。
- locations 使用相对路径与精确 region。
- codeFlows 只包含已验证 trace。
- partialFingerprints 包含版本化 logical/occurrence fingerprint。
- properties 保存 confidence、evidence_quality、validation_state、history_state 和 audit_id。
- suppressions 只导出有有效 Triage 记录的项。
- SARIF 必须通过本地 Schema 与 golden 测试。

## 18. WebUI 产品规格

### 18.1 视觉原则

Code Audit 必须继续使用 apps/web/DESIGN.md 定义的 Blue Team Cartridge / 蓝队战术卡带设计语言：

- 继续使用现有深蓝/钴蓝/青色、金色审批、绿色确认和红色危险语义；
- 继续使用 Silkscreen 作为短标签，系统 monospace 用于正文、代码和数据；
- 继续使用 2px 方形边框、硬像素阴影和 stepped cut；
- 继续支持 light/dark、English/中文、键盘、focus-visible 和 reduced-motion；
- 不引入圆角 SaaS 卡片、紫色霓虹、玻璃拟态、黑客雨、风险分数游戏化或新的独立设计系统；
- Severity、Confidence、Validation、Coverage 和 Stop 状态必须同时使用文本/图标，不能只靠颜色。

所有新增样式优先复用 pixel-theme.css 的 token 和现有 class。审计专用 CSS 使用 audit- 前缀，避免继续堆叠 RunDetailPage 的全局样式。

### 18.2 一级导航

当前导航为 Dashboard、New run、Nodes、Tools、Models。3.0 将 New run 的一级导航位置替换为 Code Audit：

1. Dashboard
2. Code Audit
3. Nodes
4. Tools
5. Models

通用 New Run 仍通过 Dashboard 的 New run 主按钮和 /runs/new 路由访问。这样移动端继续保持五个主导航目标，同时把 3.0 主功能提升为一级入口。

修改 apps/web/src/components/Layout.tsx：

- 增加 /audits 导航和 shield/target PixelIcon；
- 为 /audits、/audits/new、/audits/:id、/code-findings/:id 提供标题解析；
- 动态路由不能错误回退到 Dashboard 标题；
- 保持现有 sidebar/bottom-nav 响应式结构。

### 18.3 路由

修改 apps/web/src/App.tsx，使用 lazy import：

~~~text
/audits
/audits/new
/audits/:auditId
/code-findings/:findingId
~~~

详情筛选和 Inspector 状态使用可分享 query 参数：

~~~text
/audits/:id?tab=summary&view=threat-model
/audits/:id?tab=findings&view=signals
/audits/:id?tab=coverage&scope=...
/audits/:id?tab=evidence&evidence=...
/audits/:id?tab=activity&view=timeline
/code-findings/:id?occurrence=...
~~~

关闭 Inspector 后必须恢复触发按钮 focus；Escape、浏览器前进后退和直接 deep link 均需测试。

### 18.4 AuditsPage

页面目标：以代码项目和审计状态为中心，而不是复用通用 Run 列表。

首屏：

- Hero：RiftX Code Audit、简短能力说明、New audit 主按钮；
- 明确声明 Snapshot 固定、Coverage 可审计、动态验证默认隔离；
- 不展示虚构安全评分。

Metrics：

- Active audits；
- Awaiting approval；
- New High/Critical findings；
- Partial/failed coverage；
- Recently completed。

主体：

- Recent Audits 列表：项目、revision 短 SHA、mode、phase、closure、Finding 数、时间；
- Finding change：new、persisting、resolved、regressed；
- Coverage health：complete、policy exclusions、partial capability、partial budget；
- Detector capability strip：available/degraded/unavailable；
- 空、加载、错误、过期状态。

筛选：

- project；
- lifecycle/closure；
- mode；
- created range；
- severity；
- only audits requiring attention。

文件：

~~~text
apps/web/src/pages/AuditsPage.tsx
apps/web/src/pages/AuditsPage.test.tsx
apps/web/src/components/audit/AuditStatusBadge.tsx
apps/web/src/components/audit/AuditPhaseRail.tsx
apps/web/src/components/audit/CoverageStatus.tsx
~~~

### 18.5 NewAuditPage

使用现有 NewRunPage 的分段表单语法与 panel 视觉，但实现独立表单：

#### Step 01 — Repository

- 先选择/解析 source node 与 SourceIngest backend，再解释 node-local 路径；
- 本地 Git 路径；
- Preflight 按钮；
- 显示规范化项目名、HEAD、dirty 状态、文件/字节/语言摘要；
- 不在页面离开后保存绝对路径到浏览器 storage。

#### Step 02 — Target

- revision 或 working tree；
- Diff 的 base/head；
- include/exclude 相对路径；
- include untracked 明确开关；
- submodule/LFS/symlink 策略只显示服务端允许选项。

#### Step 03 — Analysis

- Standard、Deep、Diff；
- deterministic 或 hybrid；
- Model Profile（hybrid 必填）；
- local/remote execution、provider/base origin、retention/training disclosure 与 ModelDataEgressPolicy；远程必须显式同意 redacted source egress；
- applicable Detectors 与 unavailable 原因；
- Baseline Audit（可选）。

#### Step 04 — Safety and Budget

- ValidationPolicy；
- 动态验证不可用时解释具体缺失能力；
- max wall time、workers、model calls/tokens、candidates；
- Source 不可变与默认禁网说明。

#### Step 05 — Review

- Snapshot 目标；
- Scope 与排除；
- 模式、模型、Detector；
- 预算与验证权限；
- 最终 source node/ingest backend/prepare proof、analysis node/backend、CAS handoff、immutable image/policy digest 与 capability proof 摘要；
- 模型数据是否离机、provider/origin、脱敏与最大外发字节；
- “Create draft and start audit”的明确执行确认；客户端先创建 draft，再调用需要 host-execution 权限的 Start，任一步失败都显示真实状态，不能把两次 API 合成一个越权后端效果。

创建前必须重新使用 preflight_token。若内容变化，表单停留并提示重新 Preflight，不能静默继续。

### 18.6 AuditDetailPage

不要基于现有超大的 RunDetailPage 复制全部功能。创建独立页面，复用小组件、StatusBadge、ErrorState、LoadingState、Artifact 下载和 Approval 控制。

页头：

- project / revision / snapshot digest；
- mode、analysis profile、model、baseline；
- lifecycle、current phase、closure；
- Pause、Resume、Cancel；
- pending approval banner；
- Partial/Failed Coverage banner；
- 返回 Audits。

阶段轨：

~~~text
Freeze -> Scope -> Probe -> Threat -> Hunt -> Reconcile
       -> Prove -> Risk -> Baseline -> Publish
~~~

当前、完成、等待、失败、跳过均有明确文本。

Tabs：

1. Summary
2. Coverage
3. Findings
4. Evidence
5. Activity
6. Report

避免 10 个同级 Tabs 重现 RunDetailPage 膨胀：Threat Model 是 Summary 的 secondary view/Inspector；Signals 与 Baseline Comparison 是 Findings 的 secondary views；Timeline 与 Artifacts 属于 Activity；报告格式切换属于 Report。deep link 仍通过 `view=` 保留，桌面和移动端不新增第二排主 Tabs。

Summary：

- Snapshot；
- Phase progress；
- Finding summary；
- Coverage dimensions；
- Budget/usage；
- active WorkItem；
- Detector status；
- pending approvals；
- limitations/open questions。

Threat Model secondary view：

- attackers、boundaries、entry points、assets、privilege transitions、high-risk sinks；
- 每个项目可跳转 Evidence/ScopeUnit；
- unresolved assumptions 使用金色等待语义；
- 不能显示模型内部推理。

Coverage：

- Inventory、Engine、Review、Diff、Candidate、Validation 六维；
- 文件/模块/语言/Detector 分组；
- included、analyzed、excluded、deferred、failed 分开；
- 百分比仅是辅助，必须同时显示分子/分母和原因；
- 能跳转 ScopeUnit receipts；
- partial 的原因始终可见。

Signals secondary view：

- 默认只读，显示 producer、rule/CWE、location、disposition、cluster；
- Signals 不是漏洞，视觉上不得与 Confirmed Finding 相同；
- 大量数据使用服务端分页。

Findings：

- severity、confidence、validation、history、CWE、path、title；
- new/persisting/mitigated/resolved/regressed/reintroduced/unknown filter；
- Finding 行可进入 CodeFindingPage；
- false positive、accepted risk 等 Triage 需要 reason；
- High/Critical Evidence 不达门槛时 UI 标记 contract error，不应正常出现。

Evidence：

- Evidence type、quality、producer、digest、locations、limitations；
- 动态 Evidence 显示 SandboxCapsule、Execution、Approval 和 stop proof；
- 受限 Artifact 明确提示，不把原始输出内联到页面。

Baseline secondary view：

- 当前与 baseline 的 Snapshot、Coverage 可比性；
- new/persisting/mitigated/resolved/regressed/reintroduced/unknown；
- Coverage 不可比时禁止“resolved”绿色结论。

Activity / Timeline：

- 复用 Run Event SSE 和批量 reducer；
- 只显示阶段、Detector、WorkItem、Validation、Finding、Approval、Closure 等高层事件；
- 原始 Audit Event 可在开发/高级模式查看，但仍经过字段 allowlist。

### 18.7 CodeFindingPage

Finding 是长期对象，页面不依赖某一个 Audit：

- 标题、CWE、Severity、Confidence、Triage；
- stable fingerprint version 和安全短摘要；
- first seen、last seen、occurrence timeline；
- 当前与历史代码位置；
- verified source/control/sanitizer/sink code flow；
- Evidence 与 Proof Gap；
- Attack Path；
- 修复建议；
- Patch/Retest；
- Triage 历史；
- identity alias；
- open/new/reintroduced 等状态。

代码查看器 3.0 首版使用只读、带行号和高亮范围的轻量组件，不为此引入大型编辑器。只读取 Snapshot 中允许的代码片段，并限制上下文行数。

### 18.8 组件

~~~text
AuditStatusBadge
AuditPhaseRail
AuditProgressSummary
AuditBudgetMeter
CoverageMatrix
CoverageReasonList
ThreatBoundaryPanel
DetectorRunTable
SignalTable
FindingTable
FindingHistoryBadge
EvidenceQualityBadge
CodeLocationViewer
CodeFlowPanel
SandboxCapsulePanel
BaselineComparison
AuditApprovalCard
AuditLimitationsPanel
~~~

组件必须接收 typed props，不在组件内部拼接 API 路径或推断领域状态。

### 18.9 API Client 与 Queries

扩展：

~~~text
apps/web/src/api/types.ts
apps/web/src/api/client.ts
apps/web/src/hooks/queries.ts
apps/web/src/hooks/useEventStream.ts
~~~

要求：

- 独立 queryKeys.auditRoot/audits/audit/auditFindings 等；
- Mutation 成功后只失效相关 Audit/Run key；
- SSE 事件批量合并，不能每个 token 或 WorkItem 触发全页重绘；
- 切换 auditId 时不得短暂显示上一 Audit 的数据；
- 401/403 后清除敏感缓存投影；
- 所有列表支持 AbortSignal；
- 服务器 cursor 原样传回，不在客户端重建。

当前 `LocalOperatorGate` 不能读取 profile 后丢弃 `capabilities/features`。新增 typed `LocalOperatorSecurityContext`，由 Layout、导航、NewAudit、Approval 和 action guards 共用；后端仍是最终授权，前端 feature 只控制可发现性。需要同步扩展固定 security feature map 与 contract tests。

所有 request/query/SSE/download 捕获递增的 `authSessionEpoch`。401 时按顺序：提升 epoch 使迟到响应不可 commit，abort/cancel 全部请求与 SSE/download，revoke object URL，清除 token，把 SecurityContext 设为 unauthenticated 并返回 LocalOperatorGate，最后删除全部 Audit/snippet/Evidence/restricted Artifact/Inspector cache；旧 epoch 的 Promise 即使随后成功也必须丢弃。403 只清对应授权 scope 并刷新 SecurityProfile，不能误当 401，但同样禁止该 scope 的迟到响应回填。切换 operator/session 执行完整 401 级清理。

该逻辑必须由 API client/query boundary 全局触发，不能只让 SSE hook 清一个 action key。`AuditApprovalCard` 从现有 Run detail 私有逻辑抽成共享、typed 组件，并单独支持 mandatory-one-plan；禁止复制两套审批语义。

### 18.10 i18n、可访问性与响应式

- 所有新文本加入 apps/web/src/i18n/index.tsx 的 English 与 zh-CN；
- 不允许以英文 key 缺失回退作为完成；
- Tabs 使用正确 aria-selected、role=tablist 和键盘方向键；
- Drawer/Inspector 管理 focus trap 与返回 focus；
- 表格在窄屏转换为结构化 cards，但不丢状态标签；
- 44px 最小触控目标；
- 720px 底部五项导航继续可用；
- 代码区可横向滚动，页面本身不产生水平溢出；
- light/dark 均通过对比度与截图审查；
- reduced-motion 时阶段进度无动画。

### 18.11 Dashboard 与 Demo

DashboardPage 增加一个 Code Audit 状态入口，但不替换现有 Run 核心指标。建议在 Hero 下方增加紧凑 Audit strip：

- active audits；
- findings requiring attention；
- partial coverage；
- Open Code Audit。

更新 apps/demo 时使用完全脱敏、明确 DEMO / SANITIZED 的虚构 Audit 数据。不得使用真实仓库路径、真实密钥、真实客户或虚构性能基准。

## 19. CLI

新增 audit Typer 子应用，放在 src/riftx/cli/audit.py，并在 cli/app.py 注册。CLI 只调用 APIClient。

~~~text
riftx audit preflight PATH [--revision HEAD] [--base REF] [--include PATH] [--exclude PATH]
riftx audit start PATH [--mode standard|deep|diff] [--profile deterministic|hybrid]
                        [--model PROFILE] [--validation static_only]
                        [--baseline AUDIT_ID] [budget options]
riftx audit list [filters]
riftx audit show AUDIT_ID
riftx audit watch AUDIT_ID
riftx audit pause AUDIT_ID
riftx audit resume AUDIT_ID
riftx audit cancel AUDIT_ID
riftx audit coverage AUDIT_ID
riftx audit threat-model AUDIT_ID
riftx audit signals AUDIT_ID [filters]
riftx audit findings AUDIT_ID [filters]
riftx audit compare AUDIT_ID
riftx audit triage FINDING_ID --status ... --reason ...
riftx audit validate FINDING_ID
riftx audit fix FINDING_ID
riftx audit retest FINDING_ID
riftx audit report AUDIT_ID --format markdown|html|json|sarif
~~~

CLI 规则：

- start 先执行 Preflight，并显示冻结摘要；非交互模式使用 --yes 明确确认。
- 输出默认 Rich table，--json 返回版本化 API payload。
- watch 复用 SSE cursor 并支持断线恢复。
- 不打印绝对 Snapshot 存储路径、源代码、凭据或完整命令环境。
- Cancel 结果区分 accepted、fencing、confirmed、unconfirmed。
- Exit code：0 完成且策略门禁通过；1 工具/API/failed；2 使用错误；3 completed_partial；4 Finding policy failure；5 cancelled。

APIClient 增加对应方法，所有 HTTP 错误继续通过 RiftXAPIError 统一渲染。

`audit start PATH` 是客户端编排的 preflight → draft → reviewed start，不是单一越权 API。交互模式先打印最终 contract digest、node/backend/image policy 与 model egress 摘要再确认；非交互必须提供服务端返回的 `--confirm-contract-digest`。选择 remote_redacted 还需显式 `--allow-remote-model-egress`，显示 provider/origin/retention/字节上限；不能用通用 `--yes` 隐藏该披露。

## 20. 配置

### 20.1 RiftXConfig

新增 AuditConfig：

~~~yaml
audit:
  # 开发期保持 false；M10 GA 门禁通过后默认改为 true。
  enabled: false
  # 部署者必须显式配置授权源码根目录。
  source_roots: []
  # 示例必须是 source_roots 之外的绝对持久路径。
  snapshot_root: /var/lib/riftx/audit/snapshots
  temp_root: /var/lib/riftx/audit/tmp
  fix_root: /var/lib/riftx/audit/fixes
  default_mode: standard
  # 安全默认不外发代码；Operator 显式选择 hybrid 与 egress policy。
  default_analysis_profile: deterministic
  model_egress:
    default_mode: local_only
    max_bytes_per_call: 131072
    max_bytes_per_audit: 16777216
    allow_remote_origins: []
  max_repository_bytes: 2147483648
  max_file_bytes: 5242880
  max_files: 200000
  max_artifact_bytes: 67108864
  max_total_artifact_bytes: 268435456
  workers:
    max_parallel: 4
    max_epochs: 8
    saturation_epochs: 2
  budget:
    max_wall_seconds: 7200
    max_model_calls: 100
    max_input_tokens: 2000000
    max_output_tokens: 200000
    max_worker_jobs: 64
    max_candidates: 1000
  validation:
    default_policy: static_only
    require_sandbox: true
    default_network: none
    max_wall_seconds: 900
    max_memory_mib: 2048
    max_pids: 128
~~~

数值是安全默认上限，部署者可以进一步降低。具体默认值在实现阶段通过 fixtures 和性能测试校准，但任何配置都必须有明确范围验证。单 Artifact 的 64 MiB 默认上限与当前 Web 认证下载边界一致；更大的内部数据必须分片/分页且保持 restricted，不能生成 UI 无法安全下载的单文件。

`source_roots: []` 明确表示 deny-all，不得回退到当前目录。启动与每次 Preflight 都必须 realpath 校验：任一 source root 与 SnapshotStore、Run workspace、Artifact store、数据库/state、audit temp、fix worktree 根目录之间，只要存在任一方向的 ancestor/descendant 或同目录关系就拒绝配置/请求；不存在的输出目录按最近已存在父目录校验，创建后再校验一次。相对 storage root、CWD 推导 root 和 symlink 交叠均 fail-closed。这样 dogfood RiftX 自身时，`.riftx/...` 也不会落入被审计仓库。

### 20.2 Detector Registry

新增 configs/audit-detectors.example.yaml，生产配置默认 configs/audit-detectors.yaml：

~~~yaml
detectors:
  - id: riftx_inventory
    kind: native
    enabled: true
  - id: sarif_external_example
    kind: registered_command
    enabled: false
    tool_id: example_sarif_scanner
    output_format: sarif
    timeout_seconds: 600
    approval_level: sensitive
~~~

外部 Detector 必须引用 Tool Registry 中已注册 tool_id，不能在 YAML 接受任意 shell string。

### 20.3 环境变量

增加 RIFTX_AUDIT_* 映射，但：

- Source roots 与路径可以配置；
- Secret、token、镜像 Registry credential 不得写 YAML；
- 不允许 CLI 直接传密钥；
- Audit 子进程不继承 RIFTX_*、OPENAI_*、AWS_*、SSH_* 等私有环境；
- 所有配置参与 config_digest，但敏感值只参与不可逆 keyed digest，不写报告。

### 20.4 Feature Flag

开发期 audit.enabled 默认 false；完成 Snapshot、基础 Detector、API 权限和测试后在 Alpha 配置示例中显式开启；GA 默认 true。

当关闭时：

- Preflight、创建和所有会启动新执行的 Audit 端点返回 feature_disabled；
- GET 列表、详情、Finding、Artifact 与报告仍可只读访问已有 Audit；
- UI 隐藏 Code Audit 一级导航；
- 不再注册可创建新 Capsule/Execution 的 Activity，但 stop、destroy、cancel callback、StartIntent reconciler 和安全清理 Activity 必须始终注册；Feature Flag 不能移除停止能力；
- 已存在非终态 Audit 先持久围栏新效果并进入 `paused`（`pause_reason=feature_disabled`）或安全 cancel/cleanup；cancel 与 stop endpoint 始终可用，完成物理停止后才只读；
- 关闭是 admission fence，不得把运行中的效果留成不可控制状态，也不得把 pending StartIntent 继续投递。

## 21. 预算与可观测性

### 21.1 Budget

AuditBudget 至少控制：

- wall-clock；
- Detector jobs；
- 累计 Agent worker jobs（`workers.max_parallel` 仅限制同时运行数）；
- Epoch；
- 模型调用；
- input/output token；
-读取字节；
- Candidate/Signal 数；
-动态验证次数；
- Artifact 输出字节。

`riftx.audit-budget/v1` 的 AUD-101 hard caps 是 wire contract，不是可由部署配置放大的软默认：

| 字段 | v1 范围 |
| --- | --- |
| max_wall_seconds | 1..7,200 |
| max_detector_jobs | 1..4,096 |
| max_worker_jobs | 1..64 |
| max_epochs | 1..8 |
| max_model_calls | 0..100 |
| max_input_tokens | 0..2,000,000 |
| max_output_tokens | 0..200,000 |
| max_read_bytes | 1..2,147,483,648 |
| max_candidates | 1..1,000 |
| max_signals | 1..16,000 |
| max_dynamic_validations | 0..1,000 |
| max_artifact_output_bytes | 1..268,435,456 |

deterministic profile 的 model calls/input/output token 必须为 0；hybrid 三者都必须非 0。
static_only 的 dynamic validations 必须为 0，任一 isolated validation policy 必须非 0。
改变任一预算维度都必须改变 budget digest。

Preflight/Scope Planner 必须先计算 `MinimumFeasibleBudget`，不能接受结构上不可能满足 Coverage 的默认值：

- 一个模型 call 可以处理多个 ScopeUnit，但只能组成受 `max_scope_units/max_source_bytes/max_input_tokens` 限制的 WorkScopeSet，每个成员仍有独立 required range/receipt；
- Standard hybrid 下界包含 System Mapper、每个 required ScopeUnit 的基础 visit、Skeptic/Proof 保留量和已知 candidate；Deep 按 2/3 visits、最小 Epoch 与策略轮换计算；
- lower bound 同时覆盖 model calls/tokens、worker jobs、read/egress bytes、wall time 与 Artifact；`workers.max_parallel` 不计入总量；
- 若请求 budget 小于已知下界，或系统 hard max 无法容纳最小计划，Preflight/Start 返回 `audit_budget_infeasible`，建议缩 Scope、改 deterministic 或调整部署上限；不能启动一个注定虚假 complete 的 Audit；
- 运行中新增 Candidate/scope expansion 或实际 token 超出预留时才允许按真实 `partial_budget` 结束，且不得丢弃线索以适配预算。

默认 100 model calls/64 worker jobs 只是部署示例，不是任意仓库承诺；UI 显示下界、余量、分区数和为什么不可行，Create/Start 再用冻结 Snapshot/Matrix 重算。

达到上限：

- 先停止调度新工作；
- 完成或取消在途工作；
- 保存 Usage 与未完成 WorkItem；
- 进入 partial_budget；
- 报告准确列出未完成范围；
- 不尝试通过丢弃 Signal 伪装完成。

费用可以作为可选估计值显示，但不能替代 token/call 硬上限，也不能声称绝对计费准确。

### 21.2 指标

新增 Audit Metrics：

- audit_completion_rate；
- audit_partial_rate；
- phase_recovery_success_rate；
- detector_success_rate；
- detector_parse_failure_rate；
- required_scope_closure_rate；
- candidate_confirmation_rate；
- candidate_rejection_rate；
- evidence_contract_pass_rate；
- finding_deduplication_rate；
- finding_identity_stability；
- diff_introduction_precision；
- deep_novelty_per_epoch；
- deep_saturation_rate；
- dynamic_validation_success_rate；
- sandbox_stop_confirmation_rate；
- model_schema_rejection_rate；
- prompt_injection_policy_violation_count；
- token_per_confirmed_finding；
- time_to_first_signal；
- time_to_sealed_report。

指标基于数据库事实计算，不能信任模型 telemetry。分母为 0 时沿用现有 unavailable 语义。

### 21.3 日志与 Trace

- 日志使用 audit_id、run_id、phase、work_item_id、execution_id correlation；
- 不记录源代码、Prompt、模型输出、完整命令、绝对源路径或凭据；
- Error 使用分类码与受限 Artifact；
- OpenTelemetry/结构化日志接入属于实现选择，但字段 allowlist 是硬要求；
- Worker 重启、retry、duplicate suppression、Continue-As-New 和 cleanup 都有指标。

## 22. Codex 分阶段实施计划

以下任务顺序是依赖顺序。除明确标记可以并行的任务外，不得跨越前置里程碑。任务完成是指代码、测试、接线、失败语义和文档全部完成。

### M0 — 契约与开发护栏

目标：冻结 3.0 的名词、边界、配置和进度记录，不产生扫描副作用。

#### AUD-000：创建实施进度账本

新增 docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md，包含：

- 本文档版本与基线 Commit；
- M0–M10 任务表；
- 每项状态：pending/in_progress/blocked/completed；
- 实际变更文件；
- 测试命令与结果；
- 设计偏差及 ADR；
- 当前风险和下一项任务。

不得在进度文档记录容易过期的总测试数量，只记录具体命令和候选 Commit。

#### AUD-001：建立独立实现与命名边界

新增 ADR，声明：

- 产品名只有 RiftX Code Audit；
- 不使用 Codex Security 运行依赖；
- 不复制上游表达性材料；
- 对外术语使用 independent reimplementation，不作未经隔离证明的 strict clean-room 声明；
- 使用公开标准 CWE、CVSS、SARIF、Git 与自建 fixtures；
- 模型/外部扫描器通过 RiftX 自有契约接入。

为 requirements、contracts、Agent instructions、fixtures 和 commits 记录 RiftX provenance/author/review source；增加 CI 检查，扫描生产依赖和 bundle 中被禁止的包名/路径/端点。检查规则本身不得误伤普通文档引用。若任何人提议复用第三方代码，停止独立路径并进入单独法律与许可证决策，不在本任务内顺手复制。

#### AUD-002：配置与 Feature Flag

在 config.py 增加 AuditConfig、默认关闭的 feature flag、环境变量映射和严格校验；更新 configs/riftx.example.yaml；补 unit tests。

M0 Exit：

- 配置向后兼容；
- Audit 关闭时现有全部行为不变；
- 禁止依赖检查可运行；
- 进度账本存在。

### M1 — Run Kind、领域模型与持久化

目标：能原子创建一个尚不执行的 Audit，并在重启后读取。

#### AUD-100：RunKind

修改：

- src/riftx/domain/enums.py
- src/riftx/domain/run.py
- src/riftx/persistence/orm.py
- src/riftx/persistence/mappers.py
- Run API schemas/types

新增 general/code_audit，所有旧构造点显式写 general。创建 Alembic 迁移并测试旧数据库升级。

Run API/TypeScript type 返回 kind；list 增加显式 kind filter。现有 Web Dashboard 查询 general，避免把 Audit Run 打开到通用 Conversation 页面。

#### AUD-101：Audit Domain

新增 domain/audit.py、domain/code_finding.py，先实现：

- AuditScan、AuditContract；
- SourceTarget、AuditMode、AnalysisProfile；
- AuditLifecycleStatus、AuditPhase；
- AuditBudget、ValidationPolicy；
- AuditCapabilityMatrix 与 phase requirement/missing outcome；
- bounded/versioned AuditContractRecord、执行 node/backend 选择；
- 状态转换和 frozen 字段验证。

模型使用严格 Pydantic、extra=forbid、时区时间和稳定枚举。

#### AUD-102：ORM 与 Repository

实现最小表：

- audit_projects；
- source_snapshots；
- audit_scans；
- audit_contracts；
- audit_start_intents；
- audit_phase_runs；
- audit_scope_units；
- audit_work_items。

实现 Ports、mappers 和 SQLAlchemy Repository。增加 unique/index/CAS/跨 Scope 测试。

#### AUD-103：AuditApplicationService

实现：

- create_draft；
- get；
- list；
- pause/resume/cancel 的状态门禁接口；
- feature flag；
- aggregate create Port/AuditCreationUnitOfWork，一次提交/验证 Engagement、Project、Run、RunEvent、Audit、Contract、AuditEvent 与 client-request；M2 再把 preflight reservation 纳入同一事务；
- client_request_id 幂等。

使用 AuditCreationUnitOfWork；不得用两个独立提交的现有 Repository 模拟原子性。

此阶段不启动 Temporal，不读取 Git。

#### AUD-104：API Skeleton 与 Policy

添加 schemas/audits.py、routes/audits.py、dependencies 和 app router：

- POST /audits（draft-only test path）；
- GET /audits；
- GET /audits/{id}。

在 api/policy.py 登记权限；补 Control Plane 和跨对象授权测试。

#### AUD-105：Artifact Access Foundation

在任何 Scanner/模型原始输出进入系统前，扩展 Artifact 的 `audit_id/access_class/content_trust/ingest provenance`，让通用 list/download 服务端过滤 restricted Artifact；实现 bounded stream/fd ingest，消除 path reopen TOCTOU、symlink/hardlink 与输出增长问题。

#### AUD-106：RunKind Workflow Router

实现 machine-readable `RunKindEffectPolicy` 全 mutation inventory 与 `RunWorkflowControlRouter`，先保持 general Run 行为完全一致；按 RunKind 路由 pause/resume/cancel、Approval decision、Execution completion 与 stop callback。code_audit 的 generic Run mutation 按第 4.4 节逐项拒绝或走 Audit-owned alternative。为当前 `TemporalRunClient`、ApprovalService、Artifact/Report/Finding/Memory/Terminal/Browser/HTTP/Connector Service 和组合根增加 regression tests；旧 Workflow history 不变。

M1 Exit：

- 旧 Run API/CLI/Web tests 全部通过；
- 数据库升级无数据丢失；
- 同一 client_request_id 返回同一 Audit；
- 重启后 Audit 可读取；
- Restricted Artifact 不会从通用路由泄露；
- general Run 控制/Approval/Execution callback 回归通过，code_audit 不适用操作 fail-closed；
- Audit 尚不能接触仓库或模型。

### M2 — Preflight、Snapshot 与 Scope Ledger

目标：从允许的本地 Git 仓库产生不可变、可复现 Snapshot，不运行模型或 Scanner。

#### AUD-200：Source Root 与 Git Preflight

先实现第 8.2 节最小 SourceIngestCapsule、source node/backend 选择与双端 source-root 校验，再实现 audit/snapshot.py 的宿主路径授权与 Capsule 内 Git 元数据读取：

- allowed_source_roots；
- realpath 二次校验；
- Git root；
- commit/base/head/merge-base；
- dirty/staged/unstaged/untracked；
- file/byte/language 估计；
- capability warnings。
- versioned capability matrix、proof digest 与 mode/profile feasibility。

不得把“argv、不使用 shell”当成 Git 安全边界。`SafeGitAdapter`/object reader 只在无凭据 SourceIngestCapsule，禁用 repository-controlled config、hook、fsmonitor、textconv/filter/helper/alternate 逃逸；stderr 作为受限诊断。增加恶意 object/index/`.git/config`、压缩炸弹与外部程序 canary 测试。

#### AUD-201：Signed Preflight Token

实现高熵 opaque token 与持久 audit_preflight_plans：数据库只保存 token hash，计划绑定 source node/root identity、ingest backend/image/policy/prepare proof、目标、Scope、内容摘要和短时 expiry。这样 Control Plane 重启后仍可安全创建。Start 时在同一 source Node 重验，变化返回 audit_snapshot_changed；token reservation/consume 与 audit_id 幂等。

#### AUD-202：Snapshot Materializer

实现独立 SnapshotStore/CAS Port，以及 commit 与 dirty working tree 的内容寻址 Snapshot、Manifest、只读权限、临时目录原子 rename、并发去重、引用生命周期和失败清理。定义 source Node → SnapshotStore → analysis Node 的 mTLS/等价加密、SnapshotHydrationLease object authorization、认证分块上传/下载、水合、逐 blob digest、断点/重试、replay/cross-Audit 拒绝和删除协议；活跃传输绑定 Runner Execution/stop proof。实现每 Execution 私有 materialization 或 daemon CAS object/pin、卸载撤权、reconcile、eviction/GC receipt tests。Snapshot 内容不归首个 Run Artifact 或某一 Node 所有。

必须覆盖：

- symlink；
- hardlink；
- submodule；
- LFS pointer；
- special file；
- invalid UTF-8；
- 超大文件；
- ignored/untracked；
- TOCTOU。

#### AUD-203：Inventory 与 Scope

实现路径分类、language、dependency manifest、configuration、generated/vendor、include/exclude，并为每个对象创建 ScopeUnit。

#### AUD-204：Snapshot Reader

实现服务端 Snapshot-aware list/read/search API，路径规范化、字节配额和读取 receipt。先写 application service，不暴露给 Agent。

#### AUD-205：Snapshot Artifact 与 API

只把 Audit-local Manifest 投影接入已加固 ArtifactApplicationService；源码 blob 留在 SnapshotStore。开放 Preflight 与 Snapshot 摘要；绝对存储路径/content locator 不返回 API。

#### AUD-206：Content Sandbox 与 Safety Stop

在 AUD-200 SourceIngestCapsule 基础上交付第 15.2 节通用无网/无凭据 content-processing sandbox，持久 `audit_capsules/audit_egress_sessions`；扩展 RunSafetyStopService required resource，同时注入 CapsuleStopper 与零网络也可证明的 EgressStopper、API/Worker 组合根和 Feature Flag 关闭路径。M3 所有 AST/SARIF/Detector 解析均依赖它，不允许宿主降级。

#### AUD-207：评测 Schema 与 Harness

在编写 Detector 规则前冻结 Corpus/Truth/Matcher/Scorer schema、holdout 分配规则和 golden matcher tests；建立 vulnerable/fixed fixture generator 与评测 runner。此阶段不要求凑满 GA 200 组，但任何新增样本创建时就固定 train/dev/holdout，M10 只补足与执行最终冻结评测，不能事后挑 holdout。

#### AUD-208：Start Intent 投递骨架

实现 `POST /audits/{id}/start` 的 HOST_EXECUTION policy、start UoW、AuditStartIntent、确定性 Workflow ID、dispatcher/reconciler 和 fake Temporal 故障注入。Feature Flag 仍关闭真实扫描；M3 接入真实 Audit Workflow。

M2 Exit：

- 同一目标生成逐字节稳定 Manifest/tree digest；
- 工作树变化使旧 token 失效；
- fixture 中所有文件均有 included/excluded/deferred 决议；
- 原仓库前后 digest 不变；
- symlink/path traversal/TOCTOU 逃逸测试为零；
- storage/source roots 无任一方向重叠，空 source_roots 为 deny-all；
- 敌对 Git config/hook/filter/helper 不产生外部进程或网络；
- parser/Detector 只能进入 Content Sandbox，Cancel 可证明停止 Capsule；
- DB commit/Temporal start 边界崩溃后 StartIntent 可恢复且不产生两个 Workflow；
- Preflight 全程无模型、无 Scanner、无网络。

### M3 — Detector 框架与 Deterministic-only Vertical Slice

目标：完成第一个不依赖模型的完整、可封存审计。

#### AUD-300：Detector Registry

实现 AuditDetector、Descriptor、Job、ExecutionResult、Signal 契约；新增 audit-detectors 配置、Tool Registry 引用与 doctor。

#### AUD-301：Detector Runner

通过 Runner 执行 registered_command Detector：

- EnvironmentMode.CLEAN；
- Snapshot 只读；
- 输出目录独立；
- timeout/output limit；
- execution provenance；
- 当前没有合格 Sandbox 时外部 Detector 标记 unavailable，不在宿主降级运行。

#### AUD-302：原生基础 Detector

按顺序实现：

1. Inventory；
2. Secret；
3. Manifest/SBOM；
4. Configuration/IaC；
5. Tier A Structural Rules；
6. 有限 source-to-sink。

每个规则需要 vulnerable/fixed 成对 fixture、rule identity、CWE、支持范围和 false-positive 控制。

#### AUD-303：SARIF Import

实现带尺寸、层级、Location 和 result 数量限制的 SARIF 2.1.0 parser。第三方字段全部视为 untrusted data。Parser 失败使 DetectorRun failed。

#### AUD-304：Signal Normalization

实现 canonical locations、weakness family、rule IDs、producer provenance、初步 cluster key。不得生成最终 Finding。

#### AUD-305：Deterministic Evidence-to-Closure Slice

在第一次允许 `complete/sealed` 前，实现生产级 schema 的最小纵切：Detector Evidence/Location、Decision ACL、canonical weakness/risk facts、deterministic Finding identity/Occurrence、Scope/Detector/Candidate Closure。只支持确定性 producer 也必须遵守 Q1/Q2、stable ID、append-only 与 core-seal 规则；不允许用 Signal 直接冒充 Finding。`analysis_profile=deterministic` 可以生成 complete_under_declared_scope 或明确 partial。

#### AUD-306：Deterministic Audit Workflow

新增独立 RiftXCodeAuditWorkflow/Activities 与 concrete `TemporalAuditClient`，先串起 freeze、Snapshot、Inventory、Detector、normalize、deterministic adjudication、Closure、Safety cleanup 与 Run 终态；接入 M2 StartIntent 和 M1 RunWorkflowControlRouter。Agent phases 由冻结 profile 标记 not_applicable。

#### AUD-307：Core Seal 与最小报告

实现 canonical ledger roots、audit-core-seal、JSON/SARIF/Markdown/HTML deterministic composer 和外层 distribution manifest。报告只引用 core_seal_root；故障重试不得重跑扫描。

M3 Exit：

- deterministic-only Audit 从 Preflight 到 sealed report 全流程通过；
- 禁用网络和所有模型仍可运行；
- Scanner 重试不产生重复 Execution；
- 规则输出、Signal ID、Coverage 与报告可复现；
- 外部 Scanner 缺失不会被当作零 Finding；
- 所有 parser/Detector 在 Content Sandbox，原始输出经 bounded fd/stream ingest；
- 无 Evidence→Decision→Identity→Closure 的路径不能进入 completed/sealed；
- 产生 JSON、SARIF、Markdown 和 HTML 最小报告。

### M4 — Model-adapter-neutral Typed Agent 与 Standard Workflow

目标：在确定性底座上增加结构化威胁建模、发现和反证。

#### AUD-400：Agent Engine 结构化输出

扩展 runtime/engine/types.py：

- output_contract；
- schema/version digest；
- max output bytes；
- validation result。
- model profile locality/origin/retention metadata 与 broker route。

更新所有 Engine adapter 和 fake engine 测试。不能在 audit/agents 直接绕过 AgentEngine。

#### AUD-401：Audit Agent Contracts

实现第 10 节 Packet、交叉引用校验、长度/数量上限、无效输出拒绝、最多一次修复策略。

#### AUD-402：安全代码工具

把第 8.5 节工具接入 AuditAgentContext，并实现 ModelDataEgressPolicy、OutboundCodeView、secret placeholder 和 ModelEgressReceipt。Agent 不能看到 run_shell、通用 Process、任意 Workspace、raw secret 或凭据工具。

#### AUD-403：System Mapper

从 Inventory、Detector Signal 和安全代码读取构建 ThreatModelPacket。无 Evidence 的 assumption 标记 unresolved。

#### AUD-404：Hunter 与 Skeptic

实现 Scope planner、Partition Hunter 和独立 Skeptic：

- Hunter 只读分配 Scope；
- Skeptic 不读取 Hunter 自由文本以外的未验证结论；
- 两者产生 typed packet 与 receipts；
- 不能直接 Confirm Finding。

#### AUD-405：Proof 与 Chain

先只实现静态 Proof；Chain Analyst 只接受 confirmed facts。Fix Advisor 只产生建议。

#### AUD-406：Agent-aware Reconcile、Risk 与 Closure

在 M3 生产契约上扩展确定性 Reducer、Evidence independence、Skeptic/Proof Decision、RiskPolicy 与 Agent Coverage vector。Agent 仍只能提出事实；Policy 在门槛通过后确认 Finding。只有此任务完成后 Chain Analyst 才能读取 confirmed facts。

#### AUD-407：Model Egress Broker

交付第 15.5 节 broker 的 remote-model policy class：固定 origin/TLS、DNS/IP/redirect 校验、无直连、active egress session/Run fence、AuditEgressStopper、connection receipt 和字节预算。local_only 不经过网络；remote_redacted 必须同时有合同 consent、OutboundCodeView 和 broker proof。

#### AUD-408：Standard Workflow

扩展 M3 的 audit_models.py、audit_workflow.py、audit_activities.py，把 Threat、Hunt、Reconcile、Proof、Risk、Closure 串入独立 Workflow。

扩展 M3 TemporalAuditClient 与 M1 RunWorkflowControlRouter；pause/resume/cancel 能按 RunKind 正确路由，其他通用 Run 操作继续对 code_audit fail-closed。

M4 Exit：

- hybrid Standard 可完成；
- fake model 完整集成测试不需要外网；
- invalid schema、超限输出、prompt injection 和模型中断均有正确 partial/failure；
- hybrid complete 必须经过 Evidence→Decision→Risk→Identity→Closure，不存在 Agent 直接 confirmation；
- 未显式批准的 remote model egress 为 0，fake secret/provider canary 不泄漏原 literal；
- Worker 在每个 Phase 前后崩溃都能恢复；
- 可幂等模型 adapter 使用同一 request ID reconcile；不可幂等 adapter 的 ambiguous outcome 不自动重试，且 owned external Execution 不重复；
- 旧 RiftXRunWorkflow replay 测试继续通过。

### M5 — Evidence、Finding、Baseline、Closure 与完整报告

目标：形成可用于 Triage 和跨版本追踪的审计事实闭环。

#### AUD-500：Evidence 与 Decision Ledger

加固 M3/M4 的内容寻址 Evidence、Location、append-only Decision、Evidence independence/quality、transition ACL、restricted storage 和同 Audit 引用校验；补全人工 Triage/attestation 边界。

#### AUD-501：Reducer

把 M4 的确定性规范化与聚类扩展为完整 lineage：

- 精确规则与 anchor 优先；
- model similarity 只能产生 merge suggestion；
- 合并必须记录 lineage；
- 不同独立入口不得错误折叠。

#### AUD-502：Risk Policy

扩展事实字段、Severity、Confidence、Risk delta 和 Evidence 门槛。High/Critical 不满足 Q2/Q3 时 Finalizer 必须失败或降为 Candidate。

#### AUD-503：Finding Identity

扩展 versioned canonical fingerprint、Identity、Occurrence、Alias、CoverageVector 与通用 Finding 投影；M3 已有 ID 不得迁移漂移。

#### AUD-504：Baseline Comparison

实现 new/persisting/mitigated/resolved/regressed/reintroduced/unknown。Coverage 不可比时强制 unknown。

#### AUD-505：Closure Validator

在 M3/M4 Closure 基础上逐项校验并预留后续模式 capability：

- Inventory；
- Detector；
- Review；
- Diff；
- Candidate；
- Validation；
- Approval/Execution；
- Budget；
- Artifact digest；
- terminal Run safety。

#### AUD-506：Audit Report 与 SARIF

实现第 17 节完整输出、CoreSeal、append-only DistributionRevision、Schema、golden tests、幂等/升级重建测试和受限 Artifact。

M5 Exit：

- 每个 UI/API Finding 可追溯到 Snapshot、Location、Signal、Decision 与 Evidence；
- 报告完全由持久事实重建；
- formatting/line shift 不改变 logical ID；
- Coverage 不足不会误判 resolved；
- 所有报告通过版本化 Schema。

### M6 — 完整 API、CLI 与 WebUI

目标：在不改变主体风格的情况下交付完整操作体验。

#### AUD-600：API 完整化

实现第 16 节端点、cursor、filters、错误码、Policy 和对象授权；添加 OpenAPI schema tests。

#### AUD-601：CLI

实现第 19 节命令、Rich render、JSON mode、SSE resume 和 exit code。

#### AUD-602：Web Types/Client/Queries

增加 typed contracts、API 方法、React Query keys、mutation invalidation 和 Audit SSE reducer。

#### AUD-603：Layout 与路由

一级导航替换、lazy routes、动态标题、mobile 五项导航和 404。

若用户直接访问 /runs/{audit_run_id}，在取得 Run.kind 后通过 Audits 的唯一 run_id filter 解析 audit_id，再 replace 跳转到 /audits/{audit_id}；不得渲染通用 Conversation，也不得短暂启用不适用控制。

#### AUD-604：AuditsPage 与 NewAuditPage

实现总览、Preflight、创建表单、预算/策略和所有 loading/empty/error/changed states。

#### AUD-605：AuditDetailPage

实现阶段轨、6 个主 Tabs、secondary views/Inspector、实时状态、控制、共享 mandatory Approval、Coverage、Threat、Signals、Findings、Evidence、Baseline、Timeline、Artifacts、Reports；接入 SecurityProfile feature context 与全局敏感 query 清理。

#### AUD-606：CodeFindingPage

实现长期 Finding、Occurrence、代码流、Triage、修复建议和 Retest 投影。

#### AUD-607：i18n/A11y/Responsive

补齐中英文、键盘、focus、ARIA、light/dark、reduced motion 和断点测试。

#### AUD-608：Demo 与 README

更新独立 Demo、PRODUCT/DESIGN 必要说明和脱敏截图；不写未经测量的质量或性能宣传。

M6 Exit：

- Web production typecheck/test/build 通过；
- 所有新页面英文/中文、light/dark、desktop/mobile 可用；
- 直接 deep link 和浏览器导航可恢复；
- 不展示 chain-of-thought、绝对路径或敏感 Artifact；
- WebUI 与 CLI 对同一 Audit 显示一致权威状态。

### M7 — 生产隔离与动态验证

目标：安全地执行 Build/Test/PoC，并继承 RiftX 的停止证明。

#### AUD-700：Sandbox Backend

在 M2 Content Sandbox contract 上扩展 Build/Test/PoC/Fix 能力并实现 Linux 生产 backend；把 M4 EgressBroker 扩展到 dependency/registry/target policy class。Fake backend 只能用于测试，静态解析也不得退回宿主。

#### AUD-701：Runner Capability

Runner 注册 audit.readonly、audit.sandbox、audit.network-scoped 等能力，但自报字符串只用于候选筛选。Control Plane 还要校验 operator-approved backend/image policy，Worker 在调度前验证，`sandbox.prepare` 返回的 mount/network/credential/resource proof 在执行前再次验证；proof 不符即 fail-closed 并隔离 Node。

#### AUD-702：Validation Plan 与 Approval

把 ValidationPlanPacket 转为 canonical AuditExecutionPlan 与 `mandatory_one_plan` Approval；扩展现有领域字段，拒绝 AUTO/grant/approve_for_run 绕过，admission 与 Runner 执行前重算 plan digest。实现 sealed Occurrence → 独立 validation_followup Audit/Supplement 关系，禁止追加原 core seal。

#### AUD-703：Sandbox Capsule Evidence

保存镜像、策略、资源、网络、Execution、输出、filesystem delta 和 stop proof。

#### AUD-704：失败与取消

加固 M2 的 AuditCapsuleStopper，实现 Build/Test/PoC/Fix Capsule cleanup/reconciliation、Runner 重启恢复、cgroup/容器双重停止证明、Feature Flag 关闭后的 stop 与 unconfirmed UI。

M7 Exit：

- 源码只读、默认禁网、环境无凭据；
- path/symlink/hardlink/mount/socket/credential escape 测试零容忍；
- Cancel 后无残留进程/Capsule；
- 没有支持能力的平台明确 unavailable；
- Dynamic Finding 满足 Q3 contract。

### M8 — Diff 与 Deep

目标：在 Standard 闭环稳定后交付增量和高覆盖模式。

#### AUD-800：Diff Scope Planner

实现 base/head、hunk、symbol、dependency/config 与 change-impact edge。

#### AUD-801：Diff Classification

实现 base subject/absence tombstone 成对 Evidence，区分 newly_introduced、newly_reachable、pre_existing、regressed、reintroduced、unknown；完成 changed hunk/impact edge receipts 与 risk-fact delta。

#### AUD-802：Deep Child Workflow

实现 epoch/shard、worker 隔离、策略轮换、deterministic novelty reducer 和 Continue-As-New。

#### AUD-803：Saturation 与 Budget

实现至少/最多 Epoch、连续无新 Cluster、风险 Coverage gap 和在途 job 上限。

#### AUD-804：Deep/Diff UI 与 CLI

在 M6 API/Web/CLI contract 完成后展示 Epoch、novelty、impact scope、baseline 可比性与 partial budget；后端 AUD-800～803 可与 M6 并行，此任务显式依赖 M6。

M8 Exit：

- Diff 不把不受改动影响的旧问题算作新增；
- formatting-only diff 通过误报门禁；
- Deep coordinator/worker/reducer 崩溃后恢复；
- 重复描述不算 novelty；
- 达预算时正确 partial_budget；
- feature-specific tests 和当前冻结 dev corpus 无回归；第 24.7 节 GA 统计门槛只在 M10 完整 holdout Corpus 上最终裁决。

### M9 — Fix、Retest 与生命周期闭环

目标：从 Finding 安全进入修复建议、补丁和复测，而不修改原工作区。

#### AUD-900：Fix Advisor

结构化修复策略、受影响 symbol、建议测试和预期风险降低。

#### AUD-901：Isolated Fix Worktree

创建临时副本、限制可写范围、生成 patch 与 delta Artifact。

#### AUD-902：Retest

从 patched overlay 封存新的 content-addressed SourceSnapshot，记录 parent/base/patch digest；新建 Retest Audit。只复用 scope policy/稳定 anchors，重新生成 ScopeUnit、Receipt、Evidence、Occurrence 与 Closure，并与原 Finding 比较。

#### AUD-903：Lifecycle Projection

根据 Retest 与 Triage 投影 fixed/reopened/reintroduced，保留历史。

M9 Exit：

- 原仓库零修改；
- Patch 可审阅、可下载、带 base digest；
- Retest 结果可追溯；
- 自动 push/merge 永远不存在；
- UI/CLI 完成 Finding 到 Retest 闭环。

### M10 — 评测、加固与 3.0 发布

目标：证明功能、安全、恢复、质量和独立性达到 GA。

#### AUD-1000：评测 Corpus

在 M2 已冻结的 schema/partition/harness 上补足至少 200 组 vulnerable/fixed 成对样本，至少 30% holdout，不得从外部项目复制表达性测试。覆盖 Tier A 和至少 10 个 CWE 家族；不能因产品结果调整既有 holdout truth/matcher。

#### AUD-1001：Fault Injection

在每个 Phase commit 前后、每个副作用前后、每个 Deep Epoch/Reducer、Approval 和 Cancel 注入故障。

#### AUD-1002：安全测试

覆盖恶意路径、压缩炸弹/深层 SARIF、Prompt Injection、恶意构建、网络、凭据、输出洪泛、fork/daemon 和 cleanup。

#### AUD-1003：独立性/SBOM/许可证

扫描 Python/Node/Rust/native 实际打包物；验证无禁止依赖；外部 Detector 仅作为用户安装项；复核 NOTICE 与许可证。

#### AUD-1004：发布门禁

新增 scripts/qa/code-audit-release-gate.py，并接入总 release-gate.py。门禁读取评测 Artifact，不使用手工声称。

#### AUD-1005：版本与文档

在所有门禁通过后才：

- 版本更新为 3.0.0；
- 更新 README/README_ZH；
- 更新 deployment.md；
- 新增 Code Audit 操作、安全与故障排查文档；
- 更新 v3 completion audit；
- 生成脱敏截图。

M10 Exit = 第 25 节 Definition of Done 全部满足。

## 23. 任务依赖与并行边界

~~~mermaid
flowchart LR
    M0["M0 契约"] --> M1["M1 领域/数据库"]
    M1 --> M2["M2 Snapshot/Scope"]
    M2 --> M3["M3 Deterministic"]
    M3 --> M4["M4 Standard Agent"]
    M4 --> M5["M5 Finding/Closure"]
    M5 --> M6["M6 API/CLI/UI"]
    M5 --> M7["M7 Sandbox/Validation"]
    M5 --> M8B["M8 AUD-800..803 Backend"]
    M6 --> M8UI["M8 AUD-804 UI/CLI"]
    M8B --> M8UI
    M6 --> M9["M9 Fix/Retest"]
    M7 --> M9
    M8UI --> M9
    M9 --> M10["M10 GA"]
~~~

允许的并行：

- M2 的 Snapshot tests 与 UI Preflight mock 可以并行，但 UI 不得先冻结错误 API。
- M3 的各原生 Detector 在统一契约稳定后并行。
- M5 的 Report/SARIF 与 Fingerprint tests 可以并行。
- M6 的 CLI 与 WebUI 在 API Schema 稳定后并行。
- M7 的容器 backend 与 Approval UI 可以并行。
- M8 的 Diff/Deep 后端 AUD-800～803 可与 M6 并行，但依赖 Standard Closure；AUD-804 明确等待 M6 Web/CLI contract。

禁止的捷径：

- 在 Snapshot 完成前运行真实仓库 Scanner。
- 在 Signal/Evidence/Decision 契约完成前创建最终 Finding。
- 在 Standard 可恢复前实现 Deep。
- 在 Coverage/Baseline 完成前标记 resolved。
- 在生产 Sandbox 完成前启用动态验证。
- 在后端状态和 API 固定前制作“演示先行”的假 UI。

## 24. 测试与质量门禁

### 24.1 测试目录

~~~text
tests/audit/unit/
tests/audit/integration/
tests/audit/temporal/
tests/audit/security/
tests/audit/evaluation/
tests/fixtures/audit_repositories/
apps/web/src/pages/*.audit.test.tsx
apps/web/src/components/audit/*.test.tsx
~~~

测试遵循现有目录约定时可以合并到 tests/unit、tests/integration，但必须可通过 marker 或路径独立运行。

### 24.2 Unit

必须覆盖：

- 所有状态转换与非法转换；
- canonical JSON/digest；
- path normalization；
- Manifest 排序；
- Snapshot target；
- Scope decisions；
- Detector applicability；
- SARIF bounds/parser；
- Location 与 code flow；
- cluster/fingerprint；
- Evidence/Decision；
- Severity/Confidence；
- Baseline comparison；
- Closure Validator；
- API schemas；
- Event payload allowlist；
- Report/SARIF rendering；
- i18n key 完整性。

### 24.3 Integration

- ORM/mappers/repositories；
- Alembic 全链升级；
- Run + Audit 原子创建；
- client_request_id；
- Preflight token race；
- Artifact digest；
- API auth/policy/cross-scope；
- Runner/Execution/Approval；
- fake model typed contracts；
- fake/real Detector adapters；
- report rebuild；
- restart/reconciliation。

### 24.4 Temporal

- Workflow replay；
- Activity retry；
- duplicate suppression；
- heartbeat timeout；
- Worker kill/restart；
- Parent/Child failure；
- Continue-As-New；
- pause/resume/cancel；
- Approval race；
- late execution completion；
- cleanup unavailable；
- Temporal temporarily unavailable；
- old RiftXRunWorkflow history compatibility。

### 24.5 Security

- ../、absolute path、Unicode normalization、NUL；
- symlink/hardlink/submodule/LFS；
- file replacement TOCTOU；
- special files；
- source root escape；
- Artifact cross-run；
- oversized/deep SARIF；
- output flood；
- malicious filenames；
- Prompt Injection；
- Scanner config injection；
- shell metacharacters；
- host environment/credential leak；
- Docker/SSH/cloud socket exposure；
- network egress；
- fork/daemon escape；
- cancel/stop proof；
- sensitive Event/Log/API fields。
- report/Markdown/filename stored XSS 与 unsafe Content-Disposition；
- model source egress secret/redaction canary；
- DNS rebinding、redirect、private/link-local/metadata egress；

### 24.6 Frontend

- route/lazy load/404；
- loading/empty/error/stale/partial；
- Preflight changed state；
- Audit controls；
- SSE resume/batching；
- filters/cursor；
- direct deep links；
- keyboard/focus/Escape；
- English/中文；
- light/dark；
- responsive overflow；
- non-color status；
- sensitive data absence。
- authSessionEpoch late-response/401 relock/403 scoped cache；

### 24.7 评测门槛

#### 统一计分契约

M2 `AUD-207` 必须先冻结 `AuditEvaluationScorer/v1` 及其 JSON Schema、实现、golden tests 和 scorer digest；M10 只能在版本升级和重新计算全部基线的显式流程下修改。发布门禁只读取由该版本 scorer 生成并签名/摘要的 Artifact，禁止各任务自行解释指标。Corpus 的每个真值实例至少包含：

~~~text
truth_id
repository_fixture_id
vulnerable_snapshot_id
fixed_snapshot_id
weakness_family
ground_truth_severity
source/control/sink anchors
expected_diff_attribution
language_tier
holdout_partition
match_policy_version
provenance
~~~

规范计分如下：

1. 计分单位是版本化的独立漏洞实例 `truth_id` 与 confirmed `CodeFindingOccurrence`；标题或自然语言相似度不参与匹配。
2. Matcher 先要求 weakness family 兼容，再按 canonical symbol/location、source-control-sink overlap 和 fixture 的允许 alias 建图，执行确定性的最大权重一对一匹配；一条预测不能覆盖多个 truth，一个 truth 也不能被多个预测重复计中。
3. 同一真实实例的重复 Finding，第一条最多成为 TP，其余均计 FP；同文件的多个真实实例必须有不同 truth_id 并分别匹配。被 Policy 合并的多入口问题只有在 Corpus 明确标为一个 logical instance 时才算一个。
4. Critical/High 分层使用 Corpus 的 `ground_truth_severity`，不能使用产品预测 severity。Critical/High recall 与总体 recall 使用 truth-instance micro average；同时报告按 CWE、语言和仓库 macro average，任何样本量不足分层不得冒充总体结论。
5. precision = matched confirmed occurrences / 全部 confirmed occurrences。fixed-counterpart FPR = 在 fixed Snapshot 上仍匹配到对应 vulnerable truth anchor 的实例数 / 全部可评 fixed truth 实例数；重复错误仍额外降低 precision。
6. deterministic profile 每个 case 使用一次可复现运行。hybrid/Deep 的独立运行次数、模型/adapter 版本、参数和随机性控制写入 Evaluation Manifest；每次运行分别计分，再按 repository/case cluster bootstrap 汇总，不能把多次随机运行当成互相独立的新 Corpus。
7. 每个门槛同时输出 point estimate、分子/分母与 95% cluster-bootstrap confidence interval。GA 要求 point estimate 达标，且预先声明的 one-sided 95% lower bound 不低于门槛减去冻结的统计容差；容差、最小样本数和 bootstrap seed 必须在看 holdout 结果前写入 scorer contract，不能发布时临时调整。
8. 无法映射、执行失败和 incomplete Audit 不能从分母静默删除：按预注册 missing-result policy 计为 recall miss，并单独报告 availability；无 ground truth 的额外 confirmed Finding 仍计 precision FP。

Scorer 的 candidate graph、最终 matching、排除原因、每个指标分子/分母、bootstrap 输入和 Evaluation Manifest 全部保存为可重算 Artifact。修改匹配或统计定义必须升级 scorer version，并用同一版本重算基线和候选版本。

#### 独立性

- 生产源码、SBOM 和 bundle 中禁止依赖为 0。
- 完全阻断相关外部服务后，deterministic scan、fake-model hybrid scan 和报告全部通过。
- 所有审计 instructions/contracts/fixtures 有 RiftX provenance。

#### 可复现性

- 对 `analysis_profile=deterministic`，同一 Snapshot/config/rulepack 连续 20 次：`snapshot-manifest.json` 逐字节一致；deterministic Signals、cluster keys、logical Finding IDs、snapshot observation keys、Coverage 和 ledger roots 在 `reproducibility_projection/v1` 下逐字节一致。该投影明确移除/稳定重标 audit/run/workflow/occurrence 等 audit-scoped ID、created/sealed time、attempt/lease 和 distribution envelope；原始 `audit-manifest.json` 因包含 Run/Audit ID 与时间，按设计不跨 Audit 字节相同，只要求各自 digest/引用验证通过。
- 对 hybrid/Deep，不要求随机模型输出或最终 Finding 集逐字节一致；要求给定同一组已校验 typed Packets 时 Reducer、ACL、stable logical ID、Coverage、Risk 和 canonical semantic ledger projection 逐字节一致，并满足下述 Deep Jaccard 质量门槛。core/distribution manifest 只做单 Audit 内完整性验证，不参加跨 Audit 字节比较。任何测试不得同时要求 hybrid/Deep 输出字节一致与允许统计波动。
- Activity 故障注入：重复外部 Execution 为 0。
- 插入空行/格式化后的 logical ID 保持率 100%。
- 无语义文件移动后的 logical ID 保持率至少 95%。

#### Coverage 真实性

- hidden、ignored、untracked、symlink、submodule、generated、vendor fixture 的输入决议率 100%。
- 删除任意 required receipt 后 Finalizer 必须拒绝完成。
- 报告计数与数据库逐项一致。
- unresolved required Candidate 阻止 complete。

#### 检测质量

- Critical/High recall 不低于 90%。
- 总体 recall 不低于 80%。
- Confirmed Finding precision 不低于 75%。
- fixed counterpart 的 confirmed false-positive rate 不高于 5%。
- Confirmed Finding Evidence contract 通过率 100%。
- Critical/High 满足 Q2 或 Q3 的比例 100%。

所有指标必须在版本化 Corpus、固定策略和记录硬件/模型配置下计算。不能把训练/调试样本计入 holdout。

#### Diff

- 引入漏洞的 paired diff recall 不低于 90%。
- formatting-only diff 的新增漏洞误报率不高于 2%。
- changed hunk closure 100%。
- 未受影响旧问题标记 newly introduced 的数量为 0。
- shared guard 变化影响的 sibling instance recall 不低于 90%。

#### Deep

- Hard benchmark 上 Deep 不得低于 Standard；当 Standard 尚有 miss 时，Deep 的 median recall 至少回收剩余 miss 的 20%，即 `deep_recall - standard_recall >= 0.20 * (100% - standard_recall)`；当 Standard 为 100% 时提升项自动通过，但零回归、Jaccard、novelty 与预算门槛仍必须通过。
- 十次重复扫描以 `logical_finding_id` 集合（Corpus 评测时也报告 scorer-matched truth_id 集合）计算 Confirmed Finding Jaccard；先对同一 repository 的所有 run pair 计算，再按冻结 scorer 聚合，门槛不低于 0.80。不能使用包含 audit_id 的 occurrence_id；empty/empty 定义为 1，empty/non-empty 为 0，并单独报告全空扫描比例以防虚假稳定。
- 重复描述计为 novelty 的数量为 0。
- Epoch/Reducer 故障恢复率 100%。
- 预算超出最多允许一个已在途 job。

#### 安全与恢复

- 每个 Phase 边界故障恢复率 100%。
- 审计前后原仓库内容 digest 不变。
- 凭据泄漏、默认 egress、路径逃逸、symlink 穿越为 0。
- Prompt Injection 不得扩大 Scope、绕过审批、启网或写原仓库。
- confirmed cancel 后存活的审计进程/Capsule 为 0。

#### 输出

- JSON/SARIF Schema 通过率 100%。
- 每个 Finding 到 Snapshot/Location/Signal/Decision/Evidence 的可追溯率 100%。
- 报告从数据库/Artifact 重建一致率 100%。
- 历史比较 fixtures 准确率 100%。

### 24.8 权威命令

开发中所有 Agent 相关命令使用：

~~~bash
conda run --no-capture-output -n agent ruff check src/riftx tests migrations
conda run --no-capture-output -n agent python -m pytest tests/audit
conda run --no-capture-output -n agent python -m pytest
conda run --no-capture-output -n agent python scripts/qa/code-audit-release-gate.py
conda run --no-capture-output -n agent python scripts/qa/release-gate.py
conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
conda run --no-capture-output -n agent pnpm --filter @riftx/web test
conda run --no-capture-output -n agent pnpm --filter @riftx/web build
conda run --no-capture-output -n agent pnpm --filter @riftx/demo typecheck
conda run --no-capture-output -n agent pnpm --filter @riftx/demo test
conda run --no-capture-output -n agent pnpm --filter @riftx/demo build
~~~

Targeted tests 每项任务执行；每个 Milestone 执行完整后端/Web gate；M10 执行全部命令和无害部署检查。

## 25. 3.0 Definition of Done

RiftX Code Audit 只有在以下全部成立时才算完成：

### 产品

- Code Audit 是一级导航和默认可发现的 3.0 主功能。
- Standard、Deep、Diff 均是真实后端能力，不是 UI 占位。
- 创建、控制、Coverage、Finding、Evidence、Baseline、报告、Fix 与 Retest 可用。
- WebUI/CLI 双语状态一致。

### 独立掌控

- 无 Codex Security 运行依赖、代码、账号或专用网络依赖。
- 领域模型、Workflow、Prompt/Instructions、Schema、测试、UI 和报告均为 RiftX 自有实现。
- 模型和外部 Scanner 可替换；deterministic profile 可独立运行。

### 正确性

- Snapshot 不可变。
- Coverage 可证明。
- Agent 无最终裁决权。
- Finding 身份稳定。
- Baseline 不误判 resolved。
- 输出可重建并通过 Schema。

### 安全

- 原仓库只读。
- 动态验证使用生产隔离 backend。
- 默认禁网和 clean environment。
- 审批绑定不可变计划。
- Cancel 有完整停止证明。
- 敏感数据不进入 Event、Log 或非受限 UI。

### 持久性

- Control Plane、Worker、Runner、Temporal 任一受测重启后可恢复。
- RiftX-owned 副作用可幂等/reconcile；无法提供幂等协议的外部调用在 ambiguous outcome 时不自动重试并明确标记 partial。
- Deep 可 Continue-As-New。
- Partial/Failure 保留证据。

### 质量

- 第 24.7 节全部 GA 门槛通过。
- 当前完整 release gate 通过。
- 无开放 P0/P1 安全问题。
- P2 问题有明确接受理由、Owner 与到期时间。

### 文档与发布

- README、部署、安全、操作、故障排查、Completion Audit 和 Demo 已更新。
- 实际安装物 SBOM/许可证审核完成。
- 版本只在候选 Commit 达标后更新为 3.0.0。

## 26. 风险与处理

| 风险 | 表现 | 必须的处理 |
| --- | --- | --- |
| 模型方差 | 漏报、重复、描述漂移 | 确定性 seeds、独立 Hunter、Skeptic、typed packet、质量评测 |
| 虚假 Coverage | 模型声称读完 | Snapshot reader receipts、Scope Ledger、Closure Validator |
| Finding 身份漂移 | 行号/标题改变导致新 Finding | symbol/sink/边界指纹、alias、成对 fixtures |
| Deep 成本失控 | worker/epoch 爆炸 | 固定深度、Child Workflow、预算、饱和停止、partial_budget |
| Temporal History 膨胀 | 大对象和过多事件 | DB/Artifact 真相、ID-only Activity、Continue-As-New |
| 恶意仓库 | Prompt、parser、build 攻击 | untrusted data、tool allowlist、limits、Sandbox、默认禁网 |
| 宿主凭据泄露 | build/Scanner 继承环境 | CLEAN env、mount allowlist、无 sockets、泄漏测试 |
| 误报过多 | Operator 无法使用 | Skeptic、Evidence 门槛、Triage、paired fixed fixtures |
| 错误“已修复” | Coverage 不足仍 resolved | Baseline 可比性门禁、unknown 状态 |
| SQLite 规模 | Signal/Evidence 数量大 | 分页/索引、Artifact 大对象、基准；必要时后续数据库抽象 |
| 外部工具许可证 | 打包限制 | 用户安装、普通 adapter、SBOM/许可证复核 |
| UI 膨胀 | RunDetail 失控 | 独立 Audit 页面、组件拆分、服务端分页 |
| 自动修复破坏代码 | 写原仓库或错误补丁 | 临时 worktree、显式审批、patch Artifact、Retest |

## 27. 建议发布节奏

### 3.0 Internal

- M0–M3；
- deterministic-only；
- 只扫描自建 fixtures 与 RiftX 自身；
- feature flag 默认关闭。

### 3.0 Alpha

- M4–M5；
- Standard hybrid；
- Snapshot、Finding、Coverage、Report 完整；
- 仅允许可信本地仓库；
- 动态验证关闭。

### 3.0 Beta

- M6–M8；
- 完整 WebUI/CLI；
- 生产 Linux Sandbox；
- Standard/Deep/Diff；
- 外部小范围 dogfood。

### 3.0 GA

- M9–M10；
- Fix/Retest；
- 质量、安全、恢复和独立性门禁全部通过；
- audit.enabled 默认 true；
- 仍保持 local_single_operator 可信边界。

## 28. Codex 每次执行任务的交付模板

Codex 每个任务结束时必须报告：

~~~text
Task ID:
Outcome:
Files changed:
Schema/migration impact:
Security boundary impact:
Tests run:
Test results:
Manual verification:
Known limitations:
Progress document updated:
Next unblocked task:
~~~

如果某个任务无法满足门禁，必须保持任务未完成，给出可复现阻塞证据；不得用 TODO、空实现、跳过测试、降低断言或把 partial 伪装为 complete。

---

本规格的最终目标不是复刻任何外部产品，而是把 RiftX 已有的持久 Run、Temporal、Agent Runtime、审批、Runner、Artifact、Finding、Report、SSE 和证据关系能力，收敛为一个由 RiftX 完整控制、可验证、可恢复、可安全停止的 Code Audit 产品。
