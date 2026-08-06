# RiftX 正式版开发优化文档

> 文档状态：权威优化、开发与收尾指南
>
> 面向对象：Codex 与 RiftX 核心开发者
>
> 校准日期：2026-08-06（Asia/Shanghai）
>
> 当前实现分支：`ch1nfo/riftx-3-code-audit`
>
> 当前进度基线：`86aaecdf`
>
> 实际进度与提交证据：[正式版 Agent 开发实施账本](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 总体安全边界：[ADR-0012](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest workload 决策：[ADR-0013](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 1. 最终产品决策

RiftX 正式版只聚焦一个产品结果：

> **成为专业人士手中真正好用、可持续成长的授权渗透测试 Agent。**

它需要同时具备两个属性：

1. **开箱即用**：完成 Onboard 后，用户可以在隔离授权目标上启动、观察、恢复和停止一条基础 Pentest 工作流。
2. **高能力上限**：专业用户可以持续添加 Tool、Skill、Technique、Playbook 和目标知识，并通过 Replay、人工批准、版本化与回滚逐步形成个人方法论。

“超过 Codex、Claude Code、OpenCode”是长期追求，不是正式版的量化发布条件。RiftX 的长期优势来自可持久化的专业状态、证据链、安全边界、工具组合和操作者经验，而不是依赖某个模型短期领先。

正式版不再同时建设通用 Agent 平台、代码审计完全体、Marketplace、多租户、远程集群和大规模排行榜。

---

## 2. 当前真实状态

### 2.1 已经完成

当前代码已经具备较完整的平台底座：

- Durable Run、Temporal Worker、Runner、Execution、Terminal 与 Stop Proof；
- Scope、Approval、Credential Reference、Redaction 与 RunKind Effect Policy；
- Browser、Target HTTP、HTTP Traffic、Web Research、MCP 与原生 Code Tool；
- Task Graph、Evidence Ledger、Reasoning Graph、Observer 与 Closure；
- Progressive Skill、Capability Version、Digest、Provenance、Pack 与 Selection；
- Official Pentest/Code Audit Packs；
- Onboard、Doctor、配置迁移、SQLite migration、Backup/Restore 与 Pack repair；
- 本地离线 Pentest Demo 和真实内置 Detector Code Audit Demo。

`PACK-302` 已完成。最近全仓验证证据为：

```text
5275 passed, 5 skipped, 17 warnings
Full Ruff passed
Database Maintenance/Doctor 目标回归 27 passed
```

这些结果证明底座稳定，不代表真实 Pentest 产品已经完成。

### 2.2 正在进行

当前唯一主线是：

```text
Stage: P1 — 真实 Pentest Run
Task: PEN-500 — Pentest Admission 与 Attack Surface
Status: in_progress
```

ADR-0013 已完成并明确决定：

- 新增持久 `RunKind.PENTEST`；
- Pentest 使用专用 Application/API/CLI 创建入口；
- Admission 必须持久化预算、禁止行为与硬停止条件；
- Objective、Scope、Entry Point、Approval 继续复用现有 Run 字段；
- Workflow、Runner、Effect Policy 必须显式识别 Pentest；
- Attack Surface 是现有 Run、Artifact/Evidence、Reasoning 与 Traffic 的投影，不建立第二套事实数据库。

首个实现切片也已完成：

- `RunKind.PENTEST`、`PentestAdmission` 和有界预算已进入 Domain；
- Pentest Run 必须具有具体正向网络 Scope、网络 Entry Point 和 Admission；
- 非 Pentest Run 不得携带 Pentest Admission；
- ORM、Mapper、Repository 与通用只读 API 已支持 Pentest 往返；
- Alembic head 已更新为 `6f2a9c4d8e17`；
- 未审计的 Pentest Web/MCP/Workflow/Runner 副作用仍保持失败关闭。

### 2.3 尚未完成的核心产品结果

当前还没有生产级的：

- `riftx pentest start/status/resume/stop`；
- 持久 Pentest Run 和不可绕过的 Admission；
- 真实授权目标上的 Pentest E2E；
- 状态化 Web 身份/授权验证闭环；
- 从 Hypothesis 到 Evidence、Negative Result、Finding 的最小验证闭环；
- Attack Chain、专业报告与 Pentest Stop Proof；
- 从真实运行复盘到 Operator Capability 生效、禁用和回滚的完整成长闭环。

因此当前判断是：

> **RiftX 已经有强底座，但还不是一个完成的渗透测试 Agent。后续不应继续横向扩平台，必须把已有能力压到一条真实 Pentest 热路径上。**

---

## 3. 过度开发判断与处理原则

### 3.1 已存在的过度开发

相对当前目标，过度主要来自：

1. 代码审计、通用 Agent、Pack 生态和企业能力曾被同时纳入首发；
2. Domain、Repository、Graph、API 和恢复设施先于真实 Pentest 闭环大量建设；
3. 小切片过于频繁运行全仓测试和更新长账本，降低了产品交付速度；
4. 默认 CLI/API 暴露面大于 Pentest 用户实际需要理解的范围。

### 3.2 不应删除的部分

以下内容即使复杂，也属于授权安全测试的必要边界：

- Scope、Approval、Credential、Redaction；
- RunKind Effect Policy 与 fail-closed 分支；
- Execution、Artifact、Evidence、Negative Result；
- Runner ownership、恢复、取消与 Stop Proof；
- migration、Backup/Restore 与数据兼容；
- Capability Version、Digest、Provenance、人工批准和回滚。

### 3.3 当前处理方式

现在不做大规模删代码，顺序固定为：

```text
冻结非主线功能
→ 完成真实 Pentest 热路径
→ 记录生产调用与默认加载范围
→ 收缩默认产品面
→ 隔离可选模块
→ 删除无消费者代码
→ 全量迁移和回归验证
```

在完成 Pentest 报告闭环前，只允许两种清理：

- 当前改动触及模块中的局部重复和错误命名；
- 能证明没有兼容、安全或生产消费者的低风险死代码。

---

## 4. 正式版范围

### 4.1 V1 必须交付

- 新用户可通过 `riftx onboard` 和 `riftx doctor` 完成可用环境初始化；
- 用户可创建具有授权引用、具体 Scope、Entry Point、预算、Approval、禁止行为和停止条件的 Pentest Run；
- Agent 可执行基础被动侦察、服务枚举、状态化 Web 测试和最小漏洞验证；
- Browser、Target HTTP、Runner Tool 和 Scanner 产生统一 Execution、Artifact 与 Evidence；
- Agent 可维护 Attack Surface、Hypothesis、Attempt、Negative Result、Finding 与 Attack Chain；
- 失败、取消、重启与人工接管后可以恢复或证明已经停止；
- 至少两个真实隔离场景形成可审查报告；
- 专业用户可以添加 Operator Tool/Skill/Technique；
- 至少一个 Operator Capability 完成 Review、Replay、批准、生效、禁用和回滚。

### 4.2 V1 不阻塞项

- CVE/PoC 自动研究；
- 更多 Scanner、协议与商业工具 Adapter；
- 多 Agent 并行探索；
- 自动生成复杂验证脚本；
- 高级 Attack Graph UI；
- Organization/Engagement Profile 完整导入导出；
- 远程 Runner 能力同步；
- Code Audit 新功能。

### 4.3 Post-V1

- Pack Marketplace 与在线 install/update/publish；
- 第三方 Pack 签名、撤销和供应链服务；
- 多租户控制面；
- 大规模远程 Runner 集群；
- 默认深层 Agent Team；
- 代码审计完全体；
- 对标通用 Agent 的排行榜。

---

## 5. 最小用户闭环

### 5.1 开箱即用路径

```text
riftx onboard
riftx doctor
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest report
riftx pentest stop
```

最小启动示例：

```text
riftx pentest start \
  --target https://app.example.test \
  --scope app.example.test \
  --objective "验证身份、授权和输入处理风险" \
  --approval balanced
```

启动前必须显示并持久化：

- Engagement 与 `authorization_reference`；
- 允许的 Domain/IP/CIDR/URL prefix 和 exclusions；
- Entry Point、Objective 与 Success Criteria；
- 时间、Model/Token、Tool、并发和目标交互预算；
- Approval Mode、禁止行为和硬停止条件；
- 最终选中的 Model、Tool、Skill、Technique 和 Pack 版本。

### 5.2 专业执行闭环

```text
Admission
→ Recon / Enumeration
→ Attack Surface
→ Hypothesis
→ Minimal Verification Plan
→ Tool Execution
→ Evidence / Negative Result
→ Finding
→ Variant / Chain Analysis
→ Report
→ Stop Proof / Review
```

每一步必须可持久化、可恢复、可审查，不能只存在于聊天历史。

### 5.3 V1 真实场景

首批只做三类，并至少完成其中两类：

1. **网络服务**：解析、端口/服务发现、版本线索、可达性与最小验证；
2. **Web 身份**：登录状态、角色差异、对象访问、会话和授权边界；
3. **请求差异**：参数、Header、Method、身份或状态变化引起的响应差异。

所有场景必须运行在可复位、隔离、明确授权的目标中，不能以固定 transcript 代替生产 E2E。

---

## 6. 架构收敛规则

### 6.1 只复用一套核心事实

Pentest 必须复用：

- `Run`、`Engagement`、`Scope` 与 Approval；
- Temporal Workflow 与 Runner；
- Browser、Target HTTP、Traffic、Execution 与 Artifact；
- Task、Evidence、Reasoning、Observer 与 Closure；
- Capability、Pack、Selection 与 Progressive Skill；
- SQLite migration、Backup/Restore 与 Doctor。

不得为 Pentest 新建第二套 Run、Execution、Artifact、Evidence、Skill、Pack、Graph 或健康状态系统。

### 6.2 Pentest 身份必须贯穿边界

Pentest 固定使用：

```text
run_kind: pentest
owner_kind: pentest_run
workflow_protocol_version: riftx.pentest-run-workflow/v1
workflow_id: riftx-pentest-<run_id>
```

数据库约束、Domain validator、Workflow signal、Runner binding、Artifact ownership、API projection 和 Effect Policy 都必须显式支持该身份。未知 RunKind 不得 fallback 为 General。

### 6.3 Attack Surface 只做投影

PEN-500 初始投影只从 Run Scope 与 Entry Point 生成 declared 节点：

- `asset`；
- `service`；
- `endpoint`；
- `parameter`。

节点记录规范化值、`declared/observed/verified` 来源等级、Scope decision 和来源对象。后续 observed/verified 节点只能来自现有 Artifact/Evidence、Reasoning 或 Traffic，不新建主事实表。

### 6.4 Skill 只表达可复用方法

适合成为 Skill/Technique 的内容：

- 可重复的验证步骤；
- 特定框架、设备或协议的方法；
- 工具参数与输出解释纪律；
- 常见失败后的替代路径；
- 证据与报告要求。

不应成为 Skill 的内容：目标秘密、凭据、未经验证的猜测、一次偶然成功、大段原始聊天、确定性解析逻辑，以及任何扩大 Scope 或降低 Approval 的提示文本。

---

## 7. 唯一关键路径

```text
O0 计划迁移（completed）
→ O1 Onboard/Doctor（completed）
→ P1 PEN-500 真实 Pentest Run（in_progress）
→ P2 PEN-501/502 状态化验证闭环
→ P3 PEN-504 Report + Stop Proof
→ P4 LEARN-600~604 Operator 能力成长
→ R1 EVAL-701~703 Pentest 发布门
→ O2 默认产品面收缩与安全删减
→ V1 Release
```

执行约束：

- P1 完成前不启动 PEN-501 之后的生产实现；
- P3 完成前不开展目录级删除或大规模重构；
- P4 完成前不建设 Marketplace；
- R1 完成前不宣称正式版完成；
- AUD、ECO 和与 Pentest 无关的 UI/API 只修复安全、兼容和阻断问题。

---

## 8. 当前任务 PEN-500 施工方案

### 8.1 已完成切片

- ADR-0013 已接受；
- 已决定新增 `RunKind.PENTEST`；
- 已决定 Pentest admission 的持久边界；
- 已决定独立 Workflow identity 与 Runner ownership；
- 已决定 Attack Surface 采用可重建投影。
- 已新增持久 `RunKind.PENTEST`；
- 已新增结构化 Admission、预算、禁止行为与停止条件；
- 已完成 ORM、Mapper、Repository、API 只读投影和 migration；
- 已完成 upgrade/downgrade、跨级降级保护与全仓回归。

### 8.2 已完成：切片 A — Domain 与持久化

最小实现：

- 在 `src/riftx/domain/enums.py` 新增 `RunKind.PENTEST`；
- 在 `src/riftx/domain/run.py` 增加结构化 `PentestAdmission`；
- Admission 只保存预算、禁止行为和停止条件，不复制 Objective、Scope、Entry Point；
- Pentest Run 必须携带 Admission，非 Pentest Run 不得携带；
- Pentest 必须存在具体正向网络 Scope 和至少一个网络 Entry Point；
- 在 `runs` 增加 nullable JSON 字段并建立 kind/admission 一致性约束；
- mapper、API schema 与 repository 完整往返；
- Alembic 已从 `3c6e8a1f2b40` 迁移到新 head `6f2a9c4d8e17`；
- downgrade 在存在 Pentest 权威数据时拒绝，不改写为 General。

建议最小预算字段：

```text
max_duration_seconds
max_model_calls
max_tokens
max_tool_calls
max_target_interactions
max_concurrent_target_interactions
```

Domain 只验证结构不变量。Entry Point 与 Scope 的精确匹配由 Application Service 调用现有 `ScopeGuard` 完成，避免 `domain.run` 与 `riftx.scope` 循环依赖。

### 8.3 当前下一步：切片 B — 安全边界贯通

必须显式更新：

- `src/riftx/domain/workflow_signal.py`；
- `src/riftx/domain/runner.py`；
- `src/riftx/persistence/workflow_signals.py`；
- `src/riftx/application/run_kind_effects.py`；
- Workflow transport、router、worker runtime 与 reconciliation；
- Artifact、Memory、Execution、Terminal、Browser、Target HTTP、Finding 与 Report 的 RunKind guard。

将只接受 General 的共享交互 helper 改成语义明确的 General+Pentest helper，例如 `require_interactive_run_operation`。不要让名称为 `require_general_run_operation` 的函数暗中接受 Pentest。

Effect Policy 至少区分：

```text
_INTERACTIVE_RUNS = {general, pentest}
_PENTEST_ONLY = {pentest}
_AUDIT_ONLY = {code_audit}
```

新增 enum 后，逐项审计 `_ALL_RUN_KINDS`；不能因为集合自动包含 Pentest 就默认获得权限。

### 8.4 切片 C：专用 Application/API

实现一个权威 Pentest 创建服务，职责仅包括：

- 验证 Engagement 授权引用；
- 拒绝空 Objective、空正向 Scope 和无效 Entry Point；
- 对每个 Entry Point 执行 `ScopeGuard`；
- 验证预算、禁止行为、停止条件和 Approval；
- 解析 Official Pentest Pack 与 Capability Selection；
- 原子创建 Pentest Run 并启动独立 workflow identity。

不要允许普通 `POST /runs` 通过任意 `kind=pentest` 绕过该服务。

### 8.5 切片 D：CLI 与 Attack Surface

实现：

```text
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
```

CLI 只调用 Application/API，不复制 Admission 逻辑。`status` 至少展示 Run、Admission、Selection、预算状态、Stop 状态和 declared Attack Surface。

PEN-500 不创建完整 Attack Surface 数据库，只实现从 Run 可确定性重建的初始投影。

### 8.6 切片 E：真实 E2E 与完成条件

在隔离授权目标中证明：

- 无授权引用、无具体 Scope、无 Entry Point 或越界 Entry Point 时拒绝创建；
- Pentest Run 可以启动、查询、停止和跨进程重读；
- Workflow、Runner binding 与 Effect Policy 保持 `pentest` 身份；
- Scope 外请求在 DNS/HTTP/Browser/Runner 副作用前失败关闭；
- 取消和停止产生可验证状态；
- declared Attack Surface 可从持久 Run 重建。

PEN-500 完成前不得提前建设复杂 Planner、Attack Graph UI 或新事实表。

---

## 9. 后续阶段施工指南

### 9.1 P2：状态化验证闭环

PEN-501 先完成一个 Web 身份/授权靶场：

- Browser、Target HTTP 与 Traffic 使用统一 Request/Session identity；
- Cookie/Token 只通过 Secret Reference 使用；
- 登录、角色、会话和请求状态可恢复；
- 支持请求/响应 Diff、重放和最小化；
- 人工接管后生成 Takeover Summary。

PEN-502 再把 Hypothesis 转成最小验证计划：

- 前置条件、动作、正向/负向判据；
- 风险、Approval、Evidence capture；
- Stop condition 与 Retry relation；
- 失败产生 Negative Result；
- 扫描信号、搜索结果和模型猜测不得直接成为 Confirmed Finding。

### 9.2 P3：收口与报告

PEN-504 必须交付：

- Finding 与 Exploit/Proof 分离；
- Attack Chain 区分已确认段、假设段、前置条件、阻断点和权限变化；
- Coverage、Negative Result、限制和未完成项进入报告；
- `pentest report`；
- 失败、取消、超时、重启和人工停止后的 Stop Proof；
- 至少两个真实场景从 Admission 走到报告。

### 9.3 P4：越用越好用

最小成长闭环：

```text
Sanitized Trajectory
→ Post-run Review
→ Failure/Success Classification
→ Capability Candidate
→ Original + Variant + Negative + Regression Replay
→ Human Approve/Reject
→ Activate/Disable/Rollback
```

约束：

- 后台 Review 不得调用目标交互工具；
- Candidate 不得直接变成 Active；
- 不引入第二套向量数据库，先用现有数据库与 FTS；
- V1 只需证明一个 Operator Skill 的真实晋升闭环；
- 不建设 Organization Marketplace。

### 9.4 R1：发布门

固定可复位的 Pentest 场景，覆盖：

- 授权 Scope 与越界拒绝；
- 真实工具路径与 Evidence；
- 状态恢复、取消和 Stop Proof；
- Operator Capability 启用前后差异；
- migration、Backup/Restore 和已知限制。

评测用于 RiftX 自身回归和诊断，不用于证明超过通用 Agent。

### 9.5 O2：代码优化

只有在真实热路径稳定后才执行：

1. 记录默认 CLI/API/UI/Worker 实际消费者；
2. 默认隐藏 Advanced、Legacy、Audit 与非 Pentest 命令；
3. 将 Code Audit Runtime、routes 和 worker 初始化改为按需；
4. 延迟加载可选 Scanner、Connector 和 Adapter；
5. 对无生产消费者模块做删除准入审查；
6. 每次删除提供数据兼容、升级和回滚说明；
7. 用迁移、目标回归和全仓门禁证明没有削弱安全边界。

删除代码不是目标；降低默认认知、初始化和维护负担才是目标。

---

## 10. 删除与隔离准入门

一段生产代码只有同时满足以下条件才允许删除：

1. 没有 Pentest 热路径消费者；
2. 没有默认 CLI/API/UI 消费者；
3. 不是 migration 或旧数据兼容读取所需；
4. 不是安全、审计、恢复、Evidence 或 Provenance 所需；
5. 没有仍受支持的用户数据依赖；
6. 可选功能已有明确禁用、导出或升级路径；
7. 目标测试、关联回归和 milestone gate 通过。

优先优化热点：

- 默认 CLI 命令面；
- 默认 API route 面；
- `runtime/control_tools.py` 的 Pentest 子集；
- `application/run_kind_effects.py` 的规则组织与命名；
- Worker Runtime eager 初始化；
- Code Audit 专属 Runtime/Repository/API；
- 无生产消费者的 Connector、Model、UI 或 Adapter。

禁止以“文件大”“测试只引用”或“当前没用到”作为单独删除理由。

---

## 11. 开发与验证纪律

### 11.1 每个切片必须形成结果

每个实现切片必须说明：

- 用户输入和可见输出；
- 生产调用路径；
- 持久状态；
- 工具副作用；
- Scope/Approval 影响；
- Evidence；
- 失败、停止与恢复；
- 显式非目标。

只新增 Model、Repository、API Schema、Graph 节点或空 CLI 命令不算完成。

### 11.2 YAGNI 门

新增抽象前回答：

1. 哪个当前 Pentest 场景无法使用现有组件实现？
2. 第一个生产消费者是谁？
3. 不实现会造成什么当前用户失败？
4. 能否复用标准库、现有 Repository 或 Tool？
5. 是否扩大 migration、权限或恢复面积？

不能明确回答时不实现。

### 11.3 分层测试

| Gate | 触发 | 要求 |
| --- | --- | --- |
| Slice | 每个实现提交 | 目标测试、受影响模块回归、Ruff/mypy/typecheck、`git diff --check` |
| Task | 一个 Task 完成 | 用户工作流 E2E、持久化、权限、失败与恢复测试 |
| Milestone | P1/P2/P3/P4 或高风险边界完成 | 全仓 Python、相关前端/桌面 build、release checks |
| Release | 发布候选 | 真实靶场、升级/恢复、安全评审和已知限制 |

数据库 migration、Scope/Approval、Credential、Artifact ACL、Stop Proof 和恢复原语修改必须执行 Milestone gate。普通文档或低风险局部修改不重复运行 5000+ 全仓测试。

所有 Agent 相关运行与测试使用：

```bash
conda run --no-capture-output -n agent ...
```

### 11.4 Git 纪律

- 一个提交只表达一个可解释结果；
- 实现与 Task 级账本更新分开提交；
- Task 完成时更新账本，不为每个内部小步骤重复写长记录；
- 不提交无关用户改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前运行 staged `git diff --check`。

---

## 12. 任务目录

本节保留历史 Task ID 与依赖关系，用于与提交、ADR、migration 和实施账本对账。状态以实施账本为最终权威。

### SEC-000：正式版 ADR 与实施账本

**依赖**：无。

状态：completed。只维护边界与账本一致性。

### SEC-001：Security Capability Evaluation 骨架

**依赖**：SEC-000。

状态：completed。只为真实 Pentest 场景增加 Fixture/Replay。

### CAP-001：Capability Domain 与持久化

**依赖**：SEC-000。

状态：completed。保留 Version、Digest、Provenance、Candidate 与 Lock。

### CAP-100：接通生产 Progressive Skill

**依赖**：CAP-001。

状态：completed。后续由 Operator 成长闭环证明价值。

### CAP-101：原生代码工具

**依赖**：CAP-001。

状态：completed。只支持 Pentest 所需脚本/PoC 审查，不扩建通用 IDE。

### CAP-102：Browser/Web/Traffic Tool 闭环

**依赖**：CAP-001。

状态：completed。PEN-500/501 的主要执行面。

### CAP-103：MCP 生产接入

**依赖**：CAP-001。

状态：completed。只接入真实使用的专业工具。

### CAP-104：持久化 Tool/Skill Selection

**依赖**：CAP-100、CAP-103。

状态：completed。Pentest Run 必须记录最终选择与版本。

### COG-200：Task Graph

**依赖**：CAP-104。

状态：completed。复用，不建立第二套 Pentest Planner 状态。

### COG-201：Evidence Ledger

**依赖**：COG-200。

状态：completed。所有 Target Interaction 必须引用 Evidence。

### COG-202：Reasoning Graph

**依赖**：COG-201。

状态：completed。优先复用现有节点语义。

### COG-203：Primary Agent Proposal Tools

**依赖**：COG-202。

状态：completed。PEN-502 直接复用。

### COG-204：Observer Supervisor 与 Projector

**依赖**：COG-203。

状态：completed。Pentest 重点验证 Scope、预算、重复尝试和证据门。

### COG-205：Closure Verifier

**依赖**：COG-204。

状态：completed。报告必须经过 Closure。

### PACK-300：基础渗透 Packs

**依赖**：CAP-102、CAP-104、COG-205。

状态：completed。后续证明 Pack 真正影响生产计划与执行。

### PACK-301：基础代码审计 Packs

**依赖**：CAP-101、CAP-104、COG-205。

状态：completed/frozen。保留兼容，不阻塞 Pentest V1。

### PACK-302：Onboard 和 Doctor

**依赖**：PACK-300、PACK-301。

状态：completed。真实 Backup/Restore readiness 已接通；Pack Marketplace 后置。

### PEN-500：Pentest Admission 与 Attack Surface

**依赖**：CAP-102、COG-202。

状态：in_progress。Domain/持久化已完成，当前进入 Workflow/Runner/Effect Policy 安全边界贯通。

### PEN-501：状态化 Web 测试

**依赖**：CAP-102、PEN-500。

状态：pending。完成 Web 身份/授权靶场和统一 Session/Request 状态。

### PEN-502：验证规划器

**依赖**：COG-203、PEN-500、PEN-501。

状态：pending。交付最小验证动作、证据判据和 Negative Result。

### PEN-503：CVE/PoC Research

**依赖**：CAP-102、PEN-502。

状态：deferred enhancement。不得阻塞 V1。

### PEN-504：Attack Chain、Report 与 Stop Proof

**依赖**：COG-201、PEN-500、PEN-502。

状态：pending。完成至少两个真实场景的专业收口。

### LEARN-600：Trajectory Store 与 Session Search

**依赖**：COG-205。

状态：pending。只保存脱敏结构化 Trajectory，优先使用现有数据库与 FTS。

### LEARN-601：Post-run Review

**依赖**：LEARN-600。

状态：pending。只能产出 Candidate，不能调用目标工具或直接激活能力。

### LEARN-602：Failure Taxonomy

**依赖**：LEARN-601。

状态：pending。先覆盖工具、Skill、规划、重复、证据、Scope、误报和环境失败。

### LEARN-603：Replay Lab

**依赖**：SEC-001、LEARN-601、LEARN-602。

状态：pending。最小 Replay 包含原始、变体、负向和旧版本回归案例。

### LEARN-604：Capability Curator

**依赖**：CAP-001、LEARN-603。

状态：pending。交付人工批准、激活、禁用和回滚。

### LEARN-605：Profile、导入和迁移

**依赖**：LEARN-604、PACK-302。

状态：deferred enhancement。V1 不建设 Organization/远程同步。

### EVAL-701：渗透测试靶场

**依赖**：SEC-001、PEN-504。

状态：pending。固化可复位、隔离、授权并有 Ground Truth 的场景。

### EVAL-702：版本、配置与能力包回归 Harness

**依赖**：EVAL-701、LEARN-603。

状态：pending。只比较 RiftX 自身版本和 Operator Capability 差异。

### EVAL-703：质量与安全发布检查

**依赖**：EVAL-702、PACK-302。

状态：pending。覆盖 Pentest 功能、安全、恢复、能力污染与已知限制。

### AUD-400：Repository Intelligence

**依赖**：CAP-101、COG-202。

状态：frozen。只修复安全、兼容和现有用户阻断。

### AUD-401：Scanner Adapter

**依赖**：AUD-400。

状态：frozen。Pentest Scanner 通过 Tool/MCP 接入。

### AUD-402：专业角色工作流

**依赖**：AUD-400、AUD-401、COG-205、PACK-301。

状态：frozen。不实现七个常驻审计 Agent。

### AUD-403：代码证据模型

**依赖**：COG-201、AUD-400、AUD-401。

状态：frozen。只保留现有数据兼容。

### AUD-404：Diff Audit 与 Variant Analysis

**依赖**：AUD-400、AUD-403。

状态：frozen。Pentest 脚本审查复用 CAP-101。

### AUD-405：受控动态验证

**依赖**：CAP-101、AUD-403。

状态：frozen。不得成为默认执行目标代码的后门。

### EVAL-700：代码审计语料

**依赖**：SEC-001、AUD-403、AUD-404。

状态：frozen。保留 Fixture，不阻塞 Pentest 发布。

### ECO-800：Pack SDK

**依赖**：CAP-001、LEARN-604。

状态：post-V1。出现真实第三方/组织 Pack 来源后再启动。

### ECO-801：信任与供应链

**依赖**：ECO-800。

状态：post-V1。与真实分发渠道一起设计。

### ECO-802：Gateway 与持续运行

**依赖**：LEARN-605、ECO-801。

状态：post-V1。单机 Pentest 体验稳定后再建设。

---

## 13. 正式版完成定义

只有同时满足以下条件，RiftX Pentest-first V1 才算完成：

1. 新用户通过 Onboard 和 Doctor 可以启动真实授权 Pentest Run；
2. 所有目标交互都有非空 Scope、预算、Approval 和 Stop condition；
3. Browser、Target HTTP、Runner Tool 与 Scanner 走生产 Runtime；
4. 至少两个隔离场景从 Admission 推进到 Evidence/Negative Result/Finding/Report；
5. 扫描信号、外部搜索和模型猜测不能直接成为 Confirmed Finding；
6. Task、Hypothesis、Attempt、Evidence、Finding 与 Selection 可跨重启恢复；
7. 取消、失败和超时具有可验证 Stop Proof；
8. 专业人士可以添加自己的 Tool、Skill 或 Technique；
9. 至少一个 Operator Capability 完成 Review、Replay、批准、生效、禁用和回滚；
10. 默认产品面不再要求用户理解与 Pentest 无关的大量命令和模块；
11. Code Audit、Marketplace、多租户与远程集群不阻塞发布；
12. 发布检查覆盖功能、安全、恢复、真实任务复盘和已知限制。

---

## 14. Codex 每轮施工模板

```text
Current milestone/task:
Pentest user outcome:
Authoritative code/ADR/ledger evidence:
Existing components to reuse:
Smallest production slice:
Allowed files/modules:
Explicit non-goals:
Scope/Approval impact:
Persistence/migration impact:
Evidence and recovery requirements:
Target tests:
Task/Milestone gate:
Rollback strategy:
Implementation commit boundary:
Ledger update commit:
```

开始前检查：

- 是否直接推进当前里程碑；
- 是否正在为 frozen/post-V1 范围新增功能；
- 是否复用了已有生产组件；
- 是否把未来需求误当成当前 blocker；
- 是否新增了没有首个生产消费者的抽象；
- 是否可以通过更小的端到端结果完成同一目标。

不能直接推进当前里程碑时，默认停止扩张，回到本文件重新判断。

---

## 15. 最终定位

RiftX 不是“工具更多、Prompt 更长”的通用 Agent。

它应成为专业人士的渗透测试工作台：知道授权边界，能够持续运行和恢复，会记录证据、反证与失败路径，能把工具信号转化为最小验证，能形成 Attack Surface、Finding、Attack Chain 与报告，并能把操作者的方法论沉淀为有版本、有 Replay、可批准、可禁用和可回滚的生产能力。

这就是项目后续唯一需要兑现的核心能力。
