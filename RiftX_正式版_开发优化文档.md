# RiftX 正式版开发优化文档

> 文档状态：正式版开发与项目收敛的权威指南
>
> 面向对象：Codex、RiftX 核心开发者与专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前实现分支：`ch1nfo/riftx-3-code-audit`
>
> 当前进度基线：`8b9ef440`；PEN-500 的 P0 身份、Runner 与 Effect Policy 安全边界已完成
>
> 实施证据：[正式版 Agent 开发实施账本](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 安全边界：[ADR-0012](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest 决策：[ADR-0013](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 1. 唯一产品目标

RiftX 正式版只兑现一个结果：

> **成为专业人士手中真正好用、可以持续养成的授权渗透测试 Agent。**

正式版必须同时满足两个属性：

1. **开箱即用**：新用户完成 Onboard 和 Doctor 后，可以在隔离、授权、明确 Scope 的目标上启动、观察、恢复、停止一条基础 Pentest 工作流，并得到可审查报告。
2. **上限足够高**：专业用户可以持续添加 Tool、Skill、Technique 和 Playbook；系统能保留有效经验，通过 Replay、人工批准、版本化、禁用和回滚逐步形成个人方法论。

“超过 Codex、Claude Code、OpenCode”是长期追求，不是 V1 的量化发布条件。RiftX 的差异化不来自更长 Prompt 或更多工具，而来自：

- 持久的专业任务状态；
- 授权、Scope、Approval 与 Stop Proof；
- Evidence、Negative Result、Finding 与 Attack Chain；
- 可组合、可追溯、可回滚的专业能力；
- 对操作者经验的持续沉淀。

### 1.1 V1 明确不做

- 不建设通用 Agent 平台；
- 不继续扩建代码审计完全体；
- 不建设 Marketplace、在线 Registry、远程集群和多租户；
- 不建设默认多 Agent 团队；
- 不为了未来需求新建第二套 Run、Evidence、Graph、Skill、Pack 或 Attack Surface 数据库；
- 不以排行榜或单一分数证明“超过通用 Agent”；
- 不在 Pentest CLI 和真实 E2E 完成前继续扩 UI。

---

## 2. 当前项目的真实状态

### 2.1 已经完成的底座

当前代码已经具备大量可复用生产能力：

- Durable Run、Temporal Worker、Runner、Execution、Terminal、取消与 Stop Proof；
- Engagement、Scope、Approval、Credential Reference、Redaction 与 fail-closed Effect Policy；
- Browser、Target HTTP、HTTP Traffic、Web Research、MCP 与原生 Code Tool；
- Task Graph、Evidence Ledger、Reasoning Graph、Observer 与 Closure；
- Progressive Skill、Capability Version、Digest、Provenance、Pack 与 Selection；
- Onboard、Doctor、SQLite migration、Backup/Restore 与 Pack repair；
- Official Pentest/Code Audit Packs；
- 本地脱敏 Pentest Demo 与真实内置 Detector Code Audit Demo。

最近完整验证证据：

```text
5321 passed, 5 skipped, 17 warnings
Full Ruff passed
PEN-500 scoped mypy: 23 source files passed
Alembic single head: 7b3d1e5f9a24
```

这些结果证明底座较稳定，不证明真实渗透测试产品已经完成。

### 2.2 PEN-500 当前进度

已经完成：

- `RunKind.PENTEST`；
- `PentestAdmission`、有界预算、禁止行为和硬停止条件；
- Pentest Run 的具体正向网络 Scope 与网络 Entry Point 约束；
- ORM、Mapper、Repository、API 只读投影；
- `runs.pentest_admission_json` 与数据库一致性约束；
- Pentest Workflow signal protocol、owner kind、owner identity 与 workflow ID；
- Pentest Runner effect binding、恢复绑定与 stop reconciliation；
- General/Pentest/Code Audit 三个显式 Workflow validator 分支；
- 109 条 General+Pentest 交互 Effect 规则与 55 条三类 Run 共享安全/所有权规则；
- `require_interactive_run_operation` 已替代 28 个生产模块的 general-only guard；
- Pentest 的 Run 控制响应、Artifact/Memory 持久可见性、Web Artifact 与原生 Code/Git Tool 共享交互路径；
- migrations `6f2a9c4d8e17` 与 `7b3d1e5f9a24`；
- 有 Pentest 权威数据时拒绝有损 downgrade；
- 未审计 Pentest 副作用继续失败关闭。

尚未完成：

- 专用 Pentest Application/API 创建入口；
- `riftx pentest start/status/resume/stop/report`；
- Attack Surface 投影；
- 隔离授权目标上的真实 E2E；
- 状态化 Web、最小验证、Negative Result、Attack Chain 与专业报告；
- Operator Capability 的真实成长闭环。

### 2.3 复杂度快照

截至本次校准，仓库约有：

```text
src/riftx Python: 175193 行
tests Python: 145851 行
src/riftx 文件: 594
tests 文件: 311
Official Packs: 22
Alembic migrations: 51
引用 general-only 交互 guard 的生产模块: 0
已迁移至 interactive guard 的生产模块: 28
```

这个比例说明当前主要风险不是“功能太少”，而是“平台底座、兼容面和测试面已经很大，但第一条真实 Pentest 热路径还没有贯通”。

### 2.4 当前结论

> **RiftX 已经存在阶段性过度开发。现在不需要重写，也不应立即大规模删代码；正确动作是冻结横向扩张、贯通真实 Pentest 热路径，再依据生产消费者证据收缩默认产品面和删除死代码。**

---

## 3. 优化总原则

### 3.1 先交付纵向结果

后续任何改动必须直接推进以下链路之一：

```text
Admission
→ Workflow / Runner
→ Recon / Enumeration
→ Attack Surface
→ Hypothesis
→ Minimal Verification
→ Evidence / Negative Result
→ Finding / Attack Chain
→ Report / Stop Proof
→ Review / Replay / Capability Promotion
```

只增加 Model、Repository、Schema、Graph 节点、Adapter、空 CLI 或设计文档，不算交付结果。

### 3.2 复用现有事实系统

Pentest 必须复用：

- `Run`、`Engagement`、`Scope`、Approval；
- Temporal Workflow 与 Runner；
- Browser、Target HTTP、Traffic、Execution、Artifact；
- Task、Evidence、Reasoning、Observer、Closure；
- Capability、Pack、Selection、Progressive Skill；
- migration、Backup/Restore、Doctor。

没有真实消费者证明前，不新增平行系统。

### 3.3 安全能力不能因“简化”被删除

以下内容不是过度设计：

- Scope、授权引用、Approval、Credential、Redaction；
- RunKind Effect Policy 与未知类型 fail-closed；
- Execution、Artifact、Evidence、Negative Result；
- Runner ownership、恢复、取消与 Stop Proof；
- migration、Backup/Restore 与旧数据兼容；
- Capability Version、Digest、Provenance、人工批准与回滚。

### 3.4 YAGNI 门

新增抽象、表、服务或依赖前必须回答：

1. 哪个当前 Pentest 用户流程无法使用现有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会造成什么当前用户失败？
4. 能否使用标准库、现有 Service、Repository、Tool 或投影？
5. 是否扩大权限、migration、恢复和测试面积？

不能给出具体答案时，不实现。

---

## 4. V1 产品完成定义

### 4.1 最小用户入口

```text
riftx onboard
riftx doctor
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
riftx pentest report
```

最小启动示例：

```text
riftx pentest start \
  --target https://app.example.test \
  --scope app.example.test \
  --objective "验证身份、授权和输入处理风险" \
  --approval balanced
```

`start` 必须显示并持久化：

- Engagement 与 `authorization_reference`；
- 允许的 Domain/IP/CIDR/URL prefix 与 exclusions；
- Entry Point、Objective、Success Criteria；
- 时间、Model/Token、Tool、并发、目标交互预算；
- Approval Mode、禁止行为、硬停止条件；
- 最终 Model、Tool、Skill、Technique、Pack 版本与 digest。

### 4.2 开箱即用标准

- Onboard 后必须存在可解析的模型配置；模型 ID、Provider 或 Profile 不匹配时，Doctor 给出准确原因和可执行修复建议，不能只返回 `Configured model not found`；
- 没有可选 Scanner 时允许降级，但必须说明缺失能力，不能伪装已执行；
- 默认帮助和默认 UI 优先展示 Pentest 主路径，不要求用户理解 Code Audit、Marketplace 或远程控制面；
- Scope、授权、预算或停止条件不完整时拒绝启动；
- 失败、取消、重启和人工接管后状态可恢复或可证明已经停止。

### 4.3 专业结果标准

- 至少两个可复位、隔离、明确授权的真实场景走完整链路；
- 至少覆盖一个网络服务场景和一个状态化 Web 身份/授权场景；
- 工具信号、搜索结果和模型猜测不能直接成为 Confirmed Finding；
- 每个 Finding 可追溯到 Execution、Artifact、Evidence 和验证判据；
- Negative Result、覆盖限制、阻断点和未完成项进入报告；
- 至少一个用户添加的 Operator Capability 完成 Review、Replay、批准、生效、禁用和回滚。

---

## 5. 唯一开发关键路径

```text
P0 贯通 Pentest 身份与控制面（completed）
→ P1 交付可运行 Pentest CLI 与 Admission（当前）
→ P2 完成一个真实网络服务闭环
→ P3 完成一个状态化 Web 验证闭环
→ P4 完成报告、Stop Proof 与 Operator 成长闭环
→ P5 收缩默认产品面并删除已证明无消费者代码
→ R1 发布检查
→ V1 Release
```

执行规则：

- P1 前不扩 Planner、Attack Graph UI、Scanner 数量或 Pack 数量；
- P3 前不新增 Agent 角色；
- P4 前不建设 Marketplace、组织 Profile 或远程同步；
- P5 前不做目录级删除或大规模重构；
- R1 前不宣称正式版完成。

---

## 6. P0：完成 PEN-500 安全边界

### 6.1 Workflow signal identity

**状态：completed（`e2314e9b`）。**

新增并精确绑定：

```text
owner_kind: pentest_run
run_kind: pentest
workflow_protocol_version: riftx.pentest-run-workflow/v1
owner_identity: pentest_run:<run_id>
workflow_id: riftx-pentest-<run_id>
```

实现要求：

- 在 Domain 增加 Pentest protocol、owner kind 和 factory；
- General、Pentest、Code Audit 使用三个显式 validator 分支；
- General 不得使用 `riftx-code-audit-` 或 `riftx-pentest-` 保留前缀；
- Pentest 不得 fallback 为 `general_run` owner；
- Transport 可复用 General 的暂停、恢复、取消、批准、拒绝和完成信号发送逻辑，但持久身份不能复用；
- Repository 创建 Approval decision 与 Execution terminal signal 时必须按 RunKind 选择 factory；
- 数据库约束精确绑定 owner kind、run kind、protocol、owner identity 和 workflow ID。

### 6.2 Runner ownership

**状态：completed（`e2314e9b`）。**

`RunnerEffectBinding` 使用三个显式分支：

```text
GENERAL    -> audit_id/plan_digest 必须为空
PENTEST    -> audit_id/plan_digest 必须为空
CODE_AUDIT -> audit_id/plan_digest 必须存在
unknown    -> fail closed
```

实现要求：

- 数据库 `run_kind` 允许 `general/pentest/code_audit`；
- owner shape 允许 General/Pentest 无 Audit 身份，Code Audit 必须有完整计划身份；
- Runner daemon 显式允许 General 与 Pentest，继续拒绝 Code Audit 走通用交互 Runner；
- `_general_runs_for_node` 改为语义准确的 interactive-run 查询，覆盖 Pentest stop reconciliation；
- Pentest binding 不得被序列化或恢复为 General。

### 6.3 Effect Policy 与交互 guard

**状态：completed（`8b9ef440`）。**

使用清晰集合，不再用含义模糊的 `_ALL_RUN_KINDS` 表达权限：

```text
_READABLE_RUNS    = {general, pentest, code_audit}
_INTERACTIVE_RUNS = {general, pentest}
_PENTEST_ONLY     = {pentest}
_AUDIT_ONLY       = {code_audit}
```

逐项审计：

- Run lifecycle、Workflow control、Approval；
- Execution、Artifact、Finding、Report；
- Memory、Context、Task/Reasoning Graph；
- Terminal、Browser、Target HTTP、Traffic；
- Web Research、MCP、Connector、Runner command；
- Safety stop 与 reconciliation。

将 `require_general_run_operation` 重命名为 `require_interactive_run_operation`，只迁移确实允许 General+Pentest 的调用者。Code Audit 专属路径继续拒绝 Pentest。未知 RunKind 永远失败关闭。

完成证据：加入专用 Pentest Application/API 入口后，策略清单为 67 条非 Run、14 条 Code Audit 专属、55 条三类 Run 共享、109 条 General+Pentest 交互规则；不再存在 General-only Effect 漏洞。

### 6.4 Migration

**状态：completed（`e2314e9b`）。**

已从 `6f2a9c4d8e17` 新增 migration `7b3d1e5f9a24`，同时更新：

- `workflow_signal_intents`；
- `runner_effect_bindings`。

Downgrade 前拒绝存在 Pentest Workflow signal intent 或 Runner binding，并延续现有跨多级 downgrade guard：任何权威数据风险都必须在 DDL 前发现。当前 Alembic 单 head 为 `7b3d1e5f9a24`。

### 6.5 P0 完成门

**状态：completed（最终 gate：`5321 passed, 5 skipped, 17 warnings`）。**

- Domain/ORM/Mapper/Repository round-trip；
- Workflow signal、Runner binding、reconciliation 目标测试；
- migration upgrade/downgrade、SQLite 与 PostgreSQL offline compile；
- Effect inventory 完整性测试；
- 未列入 allowlist 的 Pentest 副作用失败关闭；
- 受影响回归、Ruff、scoped mypy、`git diff --check`；
- 该边界涉及 migration、Runner 和安全策略，完成时运行全仓 Python gate。

---

## 7. P1：交付真实 Pentest Run

### 7.1 专用 Application Service

**状态：in_progress（Admission/Application/API 创建切片 `8f1b2554` 已完成；Capability Selection 原子锁定为下一切片）。**

只实现一个权威创建入口，职责包括：

- 验证 Engagement 存在非空授权引用；
- 验证 Objective、Success Criteria；
- 拒绝空正向 Scope；
- 验证 CIDR/IP/Domain/URL Entry Point；
- 对每个 Entry Point 调用现有 `ScopeGuard`；
- 验证预算、禁止行为、停止条件与 Approval；
- 解析并锁定 Model、Tool、Skill、Technique、Pack Selection；
- 原子创建 Pentest Run，并以 Pentest workflow identity 启动。

普通 `POST /runs` 不得通过任意 `kind=pentest` 绕过该服务。

已交付：

- 专用 `POST /api/v1/pentests`，普通 `POST /runs` 继续拒绝 `kind=pentest`；
- 同一数据库事务写入内联 Engagement（如有）、Pentest Run、`<run_id>:primary` Agent Session、上下文事件和首条指令；
- Application 与持久化双重验证非空授权引用、Pentest Admission、具体正向 Scope 和每个 Entry Point 的 `ScopeGuard` 决策；
- `request_id` 作为可恢复幂等身份；Temporal 失败后保留同一 Run 与消息事件，重试同一请求通过 `riftx-pentest-<run_id>` Signal-With-Start 恢复，不重复创建权威事实；
- 创建入口纳入 Route Policy、Effect Policy 和 Managed Effect Inventory；事务中途失败回滚全部数据库事实。

下一切片只补 Selection 绑定：扩展同一个 Creation UoW，复用现有 `agent_sessions`、`agent_capability_selections` 与 `capability_pack_locks`，在生产创建请求的同一事务内将 Model Profile、Tool、Skill、Technique 和 Pack 的最终版本/摘要绑定到主 Session。不得在 Run 上增加一份不被执行器强制消费的重复 JSON manifest，也不得新建平行 Selection 数据库。

### 7.2 API 与 CLI

**状态：in_progress（专用创建 API 已完成；CLI 与 status 聚合未完成）。**

实现专用创建 API 和以下 CLI：

```text
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
```

CLI 只做输入、展示和调用，不复制 Admission、Scope 或 Selection 业务规则。

`status` 至少展示：Run、Admission、Selection、预算使用、Workflow/Runner 状态、Stop 状态和 declared Attack Surface。

### 7.3 Attack Surface 最小投影

P1 只实现可从 Run 确定性重建的 declared 节点：

- `asset`；
- `service`；
- `endpoint`；
- `parameter`。

节点记录规范化值、`declared/observed/verified` 来源等级、Scope decision 和来源对象。Observed/verified 节点必须先进入现有 Artifact/Evidence/Reasoning/Traffic 权威路径，不新建事实表。

### 7.4 P1 E2E

在隔离授权目标证明：

- 无授权引用、无 Scope、无 Entry Point、越界 Entry Point 时拒绝创建；
- Pentest Run 可启动、查询、恢复、停止和跨进程重读；
- Workflow、Runner、Artifact、Effect Policy 全程保持 `pentest` 身份；
- Scope 外请求在 DNS/HTTP/Browser/Runner 副作用前失败关闭；
- 取消和停止产生可验证结果；
- declared Attack Surface 可从持久 Run 重建。

---

## 8. P2/P3：形成真正好用的专业闭环

### 8.1 网络服务场景

只选一个可复位靶场，贯通：

```text
解析
→ 可达性
→ 端口/服务发现
→ 版本线索
→ Hypothesis
→ 最小验证
→ Evidence 或 Negative Result
→ Finding
```

优先复用已有 Runner Tool、MCP、Pack、Execution 和 Evidence，不为单个工具新建框架。可选工具缺失时明确降级。

### 8.2 状态化 Web 场景

只选一个登录/角色/对象授权靶场，完成：

- Browser、Target HTTP、Traffic 使用统一 Request/Session identity；
- Cookie/Token 只通过 Secret Reference 使用；
- 登录、角色、会话、请求状态可恢复；
- 请求/响应 Diff、重放和最小化；
- 人工接管后生成 Takeover Summary；
- 身份或状态变化导致的响应差异形成 Evidence。

### 8.3 最小验证规划

复用现有 Task/Reasoning Graph，只补齐 Pentest 所需语义：

- Hypothesis；
- 前置条件；
- 最小动作；
- 正向与负向判据；
- 风险与 Approval；
- Evidence capture；
- Stop condition；
- Retry/Variant relation。

失败必须产生 Negative Result，不能只写进聊天。扫描器命中、搜索结果和模型判断只能生成待验证线索。

### 8.4 报告与停止

实现 `riftx pentest report`，至少包含：

- Engagement、Scope、Admission 与 Selection；
- Attack Surface 与 Coverage；
- Finding、证据、影响、复现和修复建议；
- Negative Result、限制、阻断点、未完成项；
- Attack Chain 的已确认段、假设段和前置条件；
- 取消、失败、超时、重启和人工停止后的 Stop Proof。

---

## 9. P4：兑现“越用越好用”

V1 只证明一个真实 Operator Capability 的成长闭环：

```text
Sanitized Trajectory
→ Post-run Review
→ Success/Failure Classification
→ Capability Candidate
→ Original + Variant + Negative + Regression Replay
→ Human Approve/Reject
→ Activate
→ Disable/Rollback
```

### 9.1 最小实现边界

- 复用现有 Capability、Candidate、Version、Digest、Provenance、Pack Lock；
- Trajectory 只保存脱敏、结构化、可检索事实；
- 先用现有数据库和 FTS，不引入第二套向量数据库；
- Review 后台不能调用目标交互工具；
- Candidate 不能直接变成 Active；
- Skill/Technique 不能扩大 Scope、降低 Approval 或获得未授权 Tool；
- 用户可以明确查看、启用、禁用和回滚；
- 不建设 Organization Profile、在线 Marketplace 或自动发布。

### 9.2 可沉淀内容

适合沉淀：

- 可重复验证步骤；
- 特定框架、设备、协议的方法；
- 工具参数、输出解析和证据要求；
- 常见失败后的替代路径；
- 报告与复盘规则。

禁止沉淀为能力：

- 目标秘密或凭据；
- 未验证猜测；
- 一次偶然成功；
- 大段原始聊天；
- Scope 绕过或降低 Approval 的提示；
- 应由确定性代码完成的解析逻辑。

---

## 10. P5：项目级优化与删减

### 10.1 现在立即执行

- 冻结 Code Audit、Marketplace、多租户、远程集群和多 Agent 新功能；
- 禁止增加 Official Pack 数量，先证明现有 Pack 影响生产执行；
- 普通改动使用目标测试和受影响回归，不为每个小切片重复跑 5000+ 全仓测试；
- 只清理当前触及模块中的重复、错误命名、不可达分支；
- 默认初始化改为按 Pentest 热路径需要加载，避免无关模块 eager startup；
- 新 UI 功能暂停，CLI/E2E 先完成。

### 10.2 Pentest 热路径稳定后执行

按以下顺序优化：

```text
记录真实生产消费者
→ 收缩默认 CLI/API/UI 暴露面
→ 将 Code Audit 与高级 Connector 改为按需加载
→ 测量启动、内存、依赖与维护热点
→ 隔离可选模块
→ 删除无消费者代码
→ 迁移与全仓回归
```

优先审计：

- 默认 CLI 命令面；
- 默认 API routes；
- `runtime/control_tools.py` 的 Pentest 实际子集；
- `application/run_kind_effects.py` 的重复规则和模糊集合；
- Worker Runtime eager 初始化；
- Code Audit 专属 Runtime、routes、preflight、snapshot 和 source materialization；
- 没有生产消费者的 Connector、Adapter、Demo 或 UI 页面。

这些是候选项，不是预先批准的删除清单。

### 10.3 删除准入门

一段生产代码只有同时满足以下条件才允许删除：

1. 没有 Pentest 热路径消费者；
2. 没有默认 CLI/API/UI 消费者；
3. 不是 migration 或旧数据兼容读取所需；
4. 不是安全、审计、恢复、Evidence 或 Provenance 所需；
5. 没有受支持用户数据依赖；
6. 可选功能已有禁用、导出或升级路径；
7. 目标测试、迁移回归和 milestone gate 通过。

禁止仅以“文件大”“测试只引用”“现在看起来没用”为理由删除。Migration 历史不得删除或重写。

---

## 11. 验证、提交与施工纪律

### 11.1 分层验证

| Gate | 触发条件 | 要求 |
| --- | --- | --- |
| Slice | 每个实现提交 | 目标测试、受影响回归、Ruff、必要的 mypy/typecheck、`git diff --check` |
| Task | 一个 Task 完成 | 用户流程 E2E、持久化、权限、失败、恢复 |
| Milestone | P0-P5 或高风险边界完成 | 全仓 Python、相关前端/桌面 build、migration/release checks |
| Release | 发布候选 | 两个真实靶场、升级恢复、安全评审、已知限制 |

Agent 相关测试与运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

### 11.2 Git 纪律

- 一个提交只表达一个用户可解释或安全可验证的结果；
- 实现提交与 Task 级账本提交分开；
- Task 完成时更新账本，不为每个内部动作复制长记录；
- 不提交无关用户改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前运行 staged `git diff --check`。

### 11.3 每个切片必须回答

- 用户输入和可见输出是什么？
- 真实生产调用路径是什么？
- 哪些状态被持久化？
- 有哪些目标或主机副作用？
- Scope/Approval 如何执行？
- Evidence 如何产生？
- 失败、停止、重启如何处理？
- 本切片明确不做什么？

---

## 12. 任务目录与处置

本节保留既有 Task ID 和依赖，用于提交、ADR、migration 和实施账本对账。状态与实现提交以实施账本为准。

### SEC-000：正式版 ADR 与实施账本
**依赖**：无。状态：completed；只维护权威边界一致性。

### SEC-001：Security Capability Evaluation 骨架
**依赖**：SEC-000。状态：completed；只为真实 Pentest 增加 Fixture/Replay。

### CAP-001：Capability Domain 与持久化
**依赖**：SEC-000。状态：completed；保留版本、Digest、Provenance、Candidate 与 Lock。

### CAP-100：接通生产 Progressive Skill
**依赖**：CAP-001。状态：completed；由 P4 证明实际价值。

### CAP-101：原生代码工具
**依赖**：CAP-001。状态：completed；只支持 Pentest 脚本/PoC 审查，不扩通用 IDE。

### CAP-102：Browser/Web/Traffic Tool 闭环
**依赖**：CAP-001。状态：completed；P1-P3 主要执行面。

### CAP-103：MCP 生产接入
**依赖**：CAP-001。状态：completed；只接入真实使用的专业工具。

### CAP-104：持久化 Tool/Skill Selection
**依赖**：CAP-100、CAP-103。状态：completed；Pentest Run 必须记录最终选择。

### COG-200：Task Graph
**依赖**：CAP-104。状态：completed；复用，不建立 Pentest 平行 Planner 状态。

### COG-201：Evidence Ledger
**依赖**：COG-200。状态：completed；目标交互必须引用 Evidence。

### COG-202：Reasoning Graph
**依赖**：COG-201。状态：completed；优先复用现有节点语义。

### COG-203：Primary Agent Proposal Tools
**依赖**：COG-202。状态：completed；PEN-502 直接复用。

### COG-204：Observer Supervisor 与 Projector
**依赖**：COG-203。状态：completed；重点验证 Scope、预算、重复和证据门。

### COG-205：Closure Verifier
**依赖**：COG-204。状态：completed；报告必须经过 Closure。

### PACK-300：基础渗透 Packs
**依赖**：CAP-102、CAP-104、COG-205。状态：completed/frozen；禁止继续增量，先证明生产效果。

### PACK-301：基础代码审计 Packs
**依赖**：CAP-101、CAP-104、COG-205。状态：completed/frozen；只保留兼容。

### PACK-302：Onboard 和 Doctor
**依赖**：PACK-300、PACK-301。状态：completed；后续只修复真实 Onboard/Doctor 阻断。

### AUD-400：Repository Intelligence
**依赖**：CAP-101、COG-202。状态：frozen；只修安全、兼容和已有用户阻断。

### AUD-401：Scanner Adapter
**依赖**：AUD-400。状态：frozen；Pentest Scanner 通过现有 Tool/MCP 接入。

### AUD-402：专业角色工作流
**依赖**：AUD-400、AUD-401、COG-205、PACK-301。状态：frozen；不实现常驻审计 Agent 团队。

### AUD-403：代码证据模型
**依赖**：COG-201、AUD-400、AUD-401。状态：frozen；只保留现有兼容。

### AUD-404：Diff Audit 与 Variant Analysis
**依赖**：AUD-400、AUD-403。状态：frozen。

### AUD-405：受控动态验证
**依赖**：CAP-101、AUD-403。状态：frozen；不得成为默认执行未知目标代码的入口。

### PEN-500：Pentest Admission 与 Attack Surface
**依赖**：CAP-102、COG-202。状态：in_progress；P0 安全边界和专用 Pentest Admission/Application/API 创建入口已完成，当前绑定最终 Capability Selection，随后交付 CLI、Attack Surface 与真实 E2E。

### PEN-501：状态化 Web 测试
**依赖**：CAP-102、PEN-500。状态：pending；只交付一个真实身份/授权场景。

### PEN-502：验证规划器
**依赖**：COG-203、PEN-500、PEN-501。状态：pending；复用现有图，只补最小验证语义。

### PEN-503：CVE/PoC Research
**依赖**：CAP-102、PEN-502。状态：deferred；不得阻塞 V1。

### PEN-504：Attack Chain、Report 与 Stop Proof
**依赖**：COG-201、PEN-500、PEN-502。状态：pending；完成两个真实场景的专业收口。

### LEARN-600：Trajectory Store 与 Session Search
**依赖**：COG-205。状态：pending；使用现有数据库与 FTS。

### LEARN-601：Post-run Review
**依赖**：LEARN-600。状态：pending；只产出 Candidate。

### LEARN-602：Failure Taxonomy
**依赖**：LEARN-601。状态：pending；先覆盖真实运行中出现的失败。

### LEARN-603：Replay Lab
**依赖**：SEC-001、LEARN-601、LEARN-602。状态：pending；只实现一个 Operator Capability 的 Replay 门。

### LEARN-604：Capability Curator
**依赖**：CAP-001、LEARN-603。状态：pending；交付人工批准、激活、禁用、回滚。

### LEARN-605：Profile、导入和迁移
**依赖**：LEARN-604、PACK-302。状态：deferred；V1 不建设组织/远程同步。

### EVAL-700：代码审计语料
**依赖**：SEC-001、AUD-403、AUD-404。状态：frozen。

### EVAL-701：渗透测试靶场
**依赖**：SEC-001、PEN-504。状态：pending；固化两个可复位真实场景。

### EVAL-702：版本、配置与能力包回归 Harness
**依赖**：EVAL-701、LEARN-603。状态：pending；只比较 RiftX 自身版本与能力变化。

### EVAL-703：质量与安全发布检查
**依赖**：EVAL-702、PACK-302。状态：pending；作为内部发布门，不用于证明超过通用 Agent。

### ECO-800：Pack SDK
**依赖**：CAP-001、LEARN-604。状态：post-V1。

### ECO-801：信任与供应链
**依赖**：ECO-800。状态：post-V1。

### ECO-802：Gateway 与持续运行
**依赖**：LEARN-605、ECO-801。状态：post-V1。

---

## 13. Codex 每轮执行模板

```text
Current milestone/task:
Pentest user outcome:
Authoritative code/ADR/ledger evidence:
Existing components to reuse:
Smallest production slice:
Files/modules to touch:
Explicit non-goals:
Scope/Approval impact:
Persistence/migration impact:
Evidence and recovery behavior:
Target tests:
Task/Milestone gate:
Implementation commit boundary:
Ledger update commit:
```

开始前必须检查：

- 是否直接推进当前里程碑；
- 是否正在为 frozen/post-V1 范围新增功能；
- 是否复用了已有组件；
- 是否新增了没有生产消费者的抽象；
- 是否把未来需求误当成当前 blocker；
- 是否可以用更小的端到端切片完成同一结果。

若不能直接推进当前里程碑，默认停止扩张并重新审查。

---

## 14. 正式版最终完成门

只有同时满足以下条件，RiftX Pentest-first V1 才算完成：

1. 新用户通过 Onboard 和 Doctor 可启动真实授权 Pentest Run；
2. 模型配置错误具有准确诊断和修复路径；
3. 所有目标交互都有 Scope、预算、Approval 和停止条件；
4. Browser、Target HTTP、Runner Tool 与至少一个 Scanner 走生产 Runtime；
5. 网络服务与状态化 Web 两个隔离场景走到报告；
6. 扫描信号、搜索结果和模型猜测不能直接成为 Confirmed Finding；
7. Task、Hypothesis、Attempt、Evidence、Finding、Selection 可跨重启恢复；
8. 取消、失败、超时和人工停止具有可验证 Stop Proof；
9. 专业用户可以添加自己的 Tool、Skill 或 Technique；
10. 至少一个 Operator Capability 完成 Review、Replay、批准、生效、禁用和回滚；
11. 默认产品面不要求用户理解与 Pentest 无关的大量模块；
12. Code Audit、Marketplace、多租户、远程集群不阻塞发布；
13. 发布检查覆盖功能、安全、迁移、恢复和已知限制。

最终定位不变：RiftX 不是“工具更多、Prompt 更长”的通用 Agent，而是一个知道授权边界、能够持续执行和恢复、会记录证据与失败、能形成专业报告，并能把操作者方法论沉淀成可审查生产能力的渗透测试工作台。
