# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前阶段唯一的产品收敛、代码优化与 Pentest-first V1 完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 当前实现基线：`1c379dcc`
>
> 实施事实与测试账本：[`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 平台边界：[`ADR-0012`](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest Admission 与 Attack Surface：[`ADR-0013`](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 0. 执行结论

RiftX 当前不是“底座没做完”，而是“底座已经很重，专业用户结果还没有闭环”。

因此，后续开发只遵守四条原则：

1. **先完成一个真实 Pentest 结果，再增加平台能力。**
2. **优先复用现有 Run、Runtime、Tool、Evidence、Graph、Report 和 Capability，不建平行系统。**
3. **冻结横向扩张，不进行无证据的大规模删代码。**
4. **每个提交必须让用户路径、安全边界、恢复能力或专业结果至少前进一步。**

当前最重要的判断：

- Pentest Admission、控制命令、状态聚合、Attack Surface、隔离生命周期和目标交互预算已经具备；
- Model、Tool、Token、Duration 和 Target Interaction 已形成 Run 级持久执行前门禁；
- Evidence、Negative Result、Finding、Report 等底层能力已经存在，但尚未在一个真实 Pentest 场景中形成可重复的生产闭环；
- Capability、Version、Selection、Pack Lock、Progressive Skill 等成长底座已经存在，但“专业人士添加方法并在下一次运行中安全生效”的用户闭环尚未完成；
- Code Audit、Marketplace、多租户、远程集群、更多 Agent 角色和更多 Pack 继续冻结。

**当前不应重写架构，也不应先删除大块代码。阶段 A 已完成；下一步只做一个网络服务专业闭环，然后依次完成状态化 Web 与报告、用户驱动的能力成长、默认产品面收缩。**

---

## 1. 产品目标与非目标

### 1.1 唯一产品目标

RiftX 正式版要成为：

> **专业人士手中好用、可控、可恢复，并且能够通过持续加入 Tool、Skill、Technique、Playbook 和复盘经验而越来越顺手的授权渗透测试 Agent。**

它同时兑现两个属性：

1. **开箱即用的即战力**：用户完成 Onboard 和 Doctor 后，可以在明确授权、明确 Scope 和明确预算下启动基础 Pentest，查看状态、处理批准、恢复、停止并生成可审查结果。
2. **高上限的专业成长**：专业用户可以加入自己的工具和方法；RiftX 能固定版本、限制权限、选择使用、记录效果、人工审查、启用、禁用和回滚。

“超过直接使用 Codex、Claude Code、OpenCode”是长期追求，不是 V1 的量化发布条件。RiftX 的优势应来自领域复利，而不是排行榜：

- 持久、可恢复的专业任务状态；
- 授权、Scope、Approval、预算和停止证明；
- Attack Surface、Hypothesis、Attempt、Evidence、Negative Result、Finding 和 Attack Chain；
- 可组合、可追溯、可禁用、可回滚的专业能力；
- 把操作者认可的方法沉淀为下一次可复用能力，而不是只保存聊天记录。

### 1.2 V1 明确非目标

以下内容不阻塞 Pentest-first V1：

- Code Audit 完全体和新的代码审计里程碑；
- Marketplace、在线 Registry、组织 Profile、远程同步和多租户；
- 常驻多 Agent 团队、新 Planner、新 Graph 或第二套 Runtime；
- 自动生成并自动启用 Skill 的“自我进化”；
- CVE/PoC 自动研究平台；
- 更多 Official Pack、更多 Scanner 和更多 Connector；
- 为证明超过通用 Agent而建立单一综合评分；
- 大规模 UI 重做和与当前 Pentest E2E 无关的桌面功能。

---

## 2. 当前实际进度

### 2.1 已经完成的生产能力

| 能力 | 当前事实 | 结论 |
| --- | --- | --- |
| 安装与诊断 | Onboard、Doctor、配置初始化、migration、Backup/Restore、Pack repair 已存在 | 保留并只修真实阻断 |
| Durable Runtime | Run、Temporal Workflow、Runner、Execution、Terminal、取消、恢复和 Stop Proof 已存在 | 不建第二套 Runtime |
| 安全边界 | Engagement、授权引用、Scope、Approval、Credential Reference、Redaction、RunKind Effect Policy 已存在 | 不得为提速绕过 |
| Pentest 创建 | 专用 Admission、`POST /api/v1/pentests`、Capability Selection 和 Pack Lock 已存在 | 普通 Run 不得绕过 |
| Pentest 控制 | `riftx pentest start/status/resume/stop` 已存在 | 当前基本可用 |
| 状态与资产面 | 权威 status 和 declared/observed/verified Attack Surface 可跨重启重建 | 不新建资产事实库 |
| 真实目标交互 | 隔离授权 HTTP 生命周期已覆盖成功、超时、越界、暂停、恢复、取消和重启 | 可作为后续 E2E 基座 |
| 目标交互预算 | 总量和并发预算在持久事务中原子占用；总量耗尽复用 pause、Safety Stop 和 Stop Proof | 已完成 |
| Admission 全预算 | Model、Tool、Token、Duration 和 Target Interaction 在模型或 Tool 副作用前持久检查；耗尽统一暂停并保留 Stop Proof | 已完成 |
| 专业事实底座 | Task、Evidence、Reasoning、Negative Result、Finding、Closure、Report 已存在 | 需要生产消费者 |
| 能力底座 | Capability、Version、Digest、Provenance、Candidate、Selection、Pack、Progressive Skill 已存在 | 需要用户成长闭环 |

阶段 A 最近实现：

```text
ad91c3f4  enforce target interaction budget
2c0f8004  stop on exhausted target budget
73288673  project live run usage
b6b5f739  enforce model token duration budgets
53812397  enforce tool execution budgets
1c379dcc  unify budget exhaustion handling
```

最近已验证的相关回归包括：

```text
Full Control Plane: 67 passed
Runtime/Execution/Target HTTP/Worker: 467 passed
Budget handling focused regression: 215 passed
Full Ruff: passed
Changed production files scoped mypy: passed
```

实施账本已回填 Attack Surface、隔离生命周期和全部预算提交；PEN-500 已满足关闭条件。

### 2.2 尚未完成的核心结果

| 缺口 | 当前状态 | V1 必须 |
| --- | --- | --- |
| 网络服务专业闭环 | Pack、Runner、Evidence 等组件存在，未形成生产 E2E | 是 |
| 状态化 Web 闭环 | Browser、Traffic、Target HTTP 已存在，身份/授权场景未闭环 | 是 |
| 专业报告 | 通用报告能力已存在，Pentest 事实组合与 E2E 未验收 | 是 |
| 用户驱动能力成长 | Capability 底座存在，缺少一次完整添加、选择、复盘、禁用和回滚 | 是 |
| 默认产品面收缩 | 未按真实消费者审计 | 是 |
| 大规模代码删除 | 尚无足够消费者证据 | 否，延后到收缩阶段 |

### 2.3 对“是否过度开发”的最终判断

项目已经出现明显的平台化倾向：生产 Python 模块和测试面很大、Official Pack 数量较多、CLI 同时暴露多个工作负载，历史计划也包含大量 V1 不需要的生态任务。

但以下内容不是应立即删除的过度设计：

- 授权、Scope、Approval、预算、Redaction 和 Credential；
- 持久 Run、恢复、取消、Runner ownership 和 Stop Proof；
- Evidence、Negative Result、Finding、Report 和 Provenance；
- migration、Backup/Restore 和旧数据兼容读取；
- 已被 Pentest 生产路径使用的 Browser、Target HTTP、MCP、Tool、Graph 和 Pack。

当前问题主要是**开发顺序过度平台化**，不是所有底层代码都无价值。正确策略是冻结、完成纵向结果、测量消费者，再删减。

---

## 3. Pentest-first V1 完成定义

### 3.1 最小用户入口

V1 不要求再造一套命令。优先使用现有入口：

```text
riftx onboard
riftx doctor
riftx model list/show/configure/default
riftx pentest start/status/resume/stop
riftx approvals / approve / reject
riftx report generate/list/show
riftx capabilities list/verify
riftx packs list
```

只有真实用户测试证明 `riftx report generate` 难以发现时，才添加 `riftx pentest report` 薄别名；不得复制报告业务逻辑。

### 3.2 最小专业闭环

```text
Admission
→ Recon / Enumeration
→ Attack Surface
→ Hypothesis
→ Minimal Verification
→ Evidence 或 Negative Result
→ Finding / Attack Chain
→ Closure / Report / Stop Proof
→ Review
→ 用户批准的 Capability 更新
```

### 3.3 发布完成门

只有以下条件全部成立，Pentest-first V1 才算完成：

1. 全新用户环境可以完成 Onboard、Doctor、模型配置和第一个 Pentest；
2. 模型、Provider、Profile 或 Credential 配置错误能指出具体对象和修复动作，不能只返回 `Configured model not found`；
3. 所有目标交互在执行前受到 Scope、Approval、预算和 Run 状态约束；
4. Model、Tool、Token、Duration 和 Target Interaction 预算具有明确、持久、可重启的语义；
5. Run 可查询、暂停、恢复、取消、跨进程重读并始终保持 Pentest 身份；
6. 一个网络服务靶场完整走到 Evidence/Negative Result/Finding；
7. 一个含登录、角色或对象授权的状态化 Web 靶场完整走到报告；
8. 扫描信号、搜索结果和模型猜测不能直接成为 Confirmed Finding；
9. Task、Hypothesis、Attempt、Evidence、Negative Result、Finding 和 Selection 可跨重启恢复；
10. 取消、失败、超时、重启和人工停止具有可验证 Stop Proof；
11. 专业用户能够加入至少一项 Tool、Skill 或 Technique，并在新 Run 中显式选择；
12. 至少一项用户能力经过检查、试运行、批准、生效、禁用和回滚；
13. 默认文档、CLI 和启动路径以 Pentest 为主，不要求用户理解冻结模块；
14. migration、Backup/Restore、受影响回归和安全发布检查通过；
15. 已知限制被明确记录，不以模型文本掩盖未执行、未验证或证据不足。

---

## 4. 必须复用的权威事实

任何新实现都先从下表寻找事实来源。除非现有事实不能表达当前用户结果，否则不新增表。

| 需求 | 权威事实 | 禁止的平行实现 |
| --- | --- | --- |
| Run 生命周期 | Run、Run Event、Workflow signal | 第二套 Pentest Job 状态机 |
| 授权与范围 | Engagement、Pentest Admission、ScopeGuard | Prompt 内授权、独立 Pentest Scope 表 |
| 模型/工具用量 | Agent Session、Agent Cycle、ToolCallIntent | 内存计数器、只读 status 后再执行 |
| Token 用量 | ContextCompilation actual usage | 第二套 Token Ledger |
| 目标交互 | ToolCallIntent claim、Target HTTP Request | 新 Target Interaction 表 |
| 资产面 | Admission、Target HTTP、认可 Evidence 的确定性投影 | Attack Surface 数据库或缓存 Worker |
| 专业推理 | Task Graph、Reasoning Graph | Pentest 专用 Planner/Graph |
| 结果 | Evidence、Negative Result、Finding、Closure、Report | 从最后一段模型回答倒推结果 |
| 工具和方法 | Capability、Version、Selection、Pack Lock、Progressive Skill | 第二套插件注册中心 |
| 停止 | RunApplicationService pause/cancel、Safety Stop、Stop Proof | Budget 专用停止服务 |

### 4.1 最小调用链

```text
CLI/API
→ Application Service
→ RunKind / Scope / Approval / Budget admission
→ Runtime / Tool execution
→ Artifact / Traffic / Evidence / Reasoning
→ Finding / Closure / Report
```

任何副作用如果绕过这条链，必须修正共享入口，而不是在单个调用方补一个 Prompt 约定。

### 4.2 不可简化掉的安全边界

YAGNI 不适用于以下内容：

- 外部输入校验；
- Scope 和重定向后的重新检查；
- Approval 与 Tool 参数精确绑定；
- Secret Reference 和 Redaction；
- Run/Session/Execution ownership；
- 并发预算原子性；
- migration 和恢复；
- Evidence 与 Finding 的来源证明；
- 取消、超时和停止后的资源清理。

---

## 5. 优化策略：保留、冻结、收缩、删除

### 5.1 立即保留

- 被当前 Pentest CLI/API/Worker/E2E 直接消费的模块；
- 安全、权限、审计、恢复和持久化模块；
- Evidence、Reasoning、Finding、Report 和 Capability 核心；
- migration 历史和旧数据兼容读取；
- 已有生产消费者的 Tool、Browser、Traffic、MCP 和 Runner 路径。

### 5.2 立即冻结

在默认产品面收缩前，不新增：

- Code Audit 功能、Pack、Scanner 或 UI；
- Marketplace、Registry、组织同步、多租户和远程运行集群；
- 新 Agent 角色、常驻多 Agent、第二套 Planner 或 Graph；
- 新的通用 Policy Engine、Budget Service、资产数据库或向量数据库；
- 没有当前生产消费者的 Repository、Adapter、Domain 和后台任务；
- 与两个 V1 靶场无关的新 Official Pack 和工具适配器。

### 5.3 先收缩默认暴露面

在删除代码前先完成低风险优化：

1. 默认文档和示例只展示 Pentest 主路径；
2. 冻结功能保持 feature-disabled 或标记 experimental；
3. Worker 对非默认模块按需初始化；
4. 可选 Tool 缺失时明确降级，不阻塞基础 Pentest；
5. CLI 帮助优先显示 Onboard、Doctor、Model、Pentest、Approval 和 Report；
6. 记录启动时间、内存、导入耗时和安装包组成，只处理最大的真实瓶颈。

### 5.4 删除代码的准入门

一个生产模块只有同时满足以下条件才允许删除：

1. 没有 Pentest 主路径消费者；
2. 没有默认 CLI/API/Desktop 消费者；
3. 不是 migration、旧数据读取或 Backup/Restore 所需；
4. 不是安全、权限、Evidence、Provenance 或 Stop Proof 所需；
5. 没有受支持用户数据依赖；
6. 可选功能已有禁用、导出或升级说明；
7. 引用审计、目标测试、migration 回归和 Pentest E2E 通过。

Migration 历史不得删除或重写。优先删除重复入口、不可达分支和无消费者装配；最后才删除 Domain 或持久化模型。

---

## 6. 唯一开发路线

| 阶段 | 状态 | 用户结果 |
| --- | --- | --- |
| A. 剩余 Pentest 预算收口 | completed | 所有 Admission 预算具有明确执行语义和硬停止 |
| B. 网络服务专业闭环 | in progress | 一个真实服务从枚举走到证据化结论 |
| C. 状态化 Web 与报告 | pending | 一个身份/授权场景走到 Attack Chain、Closure 和 Report |
| D. 用户驱动能力成长 | pending | 一项专业方法可添加、选择、复盘、禁用和回滚 |
| E. 默认产品面收缩与发布 | pending | Pentest-first 产品可安装、可理解、可回归、可发布 |

除安全修复、数据兼容和当前用户阻断问题外，不得跳过阶段开启冻结范围。

---

## 7. 阶段 A：完成剩余预算门禁

阶段 A 已于 `1c379dcc` 完成。实现提交为 `73288673`、`b6b5f739`、`53812397` 和 `1c379dcc`；本节保留为预算语义与回归合同，不再是待施工清单。

### 7.1 已完成部分

以下内容不得重复实现：

- `max_target_interactions` 在 Tool Intent execution claim 的持久串行化事务中检查并占用；
- `max_concurrent_target_interactions` 使用同一权威事实检查活动占用；
- 总量耗尽写入 `pentest.budget_exhausted` 并复用 Run pause、Safety Stop、Workflow signal 和 Stop Proof；
- 并发容量满写入 `pentest.budget_capacity_reached`，作为可重试容量错误，不暂停 Run；
- 重启后总量不重置，并发竞态不会超额放行。

### 7.2 先冻结预算语义

实现剩余预算前，先在代码和测试中统一以下语义：

| 预算 | V1 计数语义 | 执行边界 |
| --- | --- | --- |
| `max_model_calls` | Run 内实际启动的 Agent Engine 模型轮次；不把同一轮事件重复计数 | `agent_engine.start/resume` 之前 |
| `max_tool_calls` | Run 内实际获得执行 claim 的 Tool 调用；Proposal 和被拒绝调用不计 | Tool execution claim 之前 |
| `max_tokens` | 已持久化 actual input + output tokens；不完整时禁止下一次模型调用 | 下一次 `start/resume` 之前 |
| `max_duration_seconds` | 从持久 Run 创建时间起的 wall-clock 生命周期；Scope 时间窗更严时优先 | 每次模型调用和 Tool 副作用之前 |

V1 不为“只计算活跃运行时间”增加 pause accounting 表。若未来真实用户需要排除暂停时间，再以兼容 migration 增加明确语义。

单次模型调用的最终 Token 只能在 Provider 返回后确认。因此 V1 必须保证：

- 已用量达到或超过上限后，不再启动下一次模型调用；
- Token 记录不完整时失败关闭；
- Provider 支持输出上限时，使用剩余预算限制输出；
- Provider 无法精确限制单次总 Token 时，在已知限制中明确说明，不伪称绝对不会单次越界。

### 7.3 最小实现切片

#### A1. 运行中用量成为权威事实（completed）

- 修正只读取主 Agent Session 导致运行中 Cycle 计数滞后的问题；
- Run 级用量必须覆盖主 Session、允许存在的子 Session 和尚未 yield 的 Cycle；
- 已经合并到 Session 的 Cycle 不得重复相加；
- 复用 `AgentSessionRecord`、`AgentCycleRecord` 和 `ContextCompilationRecord`；
- 先形成一个持久查询/占用方法，不新建 Budget 表或缓存。

验收：运行中 status、重启后 status 和预算 admission 对同一用量给出一致结果。

#### A2. Model、Token 和 Duration 执行前门禁（completed）

- 在 `agent_engine.start/resume` 之前读取并原子占用本次模型轮次；
- 删除或调整 `RUN_STARTED` 后置重复计数，保持唯一计数语义；
- 模型调用前验证 Run 状态、Duration、Model Call 和 Token 完整性/余量；
- 耗尽或不完整时不接触 Provider，写结构化事件并复用既有暂停和 Stop Proof；
- 不把每 Cycle 的 `CycleLimits` 误当作 Pentest Admission 总预算。

验收：预算外请求不会到达假 Provider；最后一个配额并发竞争只允许一个调用；重启后继续拒绝。

#### A3. Tool 和 Duration 执行前门禁（completed）

- 在所有真实 Tool execution claim 的共享边界检查 Run 级 `max_tool_calls`；
- Proposal、等待批准和被 Scope 拒绝的调用不消耗 Tool 预算；
- claim 成功即消耗一次，执行失败不退款，避免重试绕过；
- retry 的计数语义必须显式测试；
- Tool 副作用前同时检查 Duration；
- Pack、MCP、Runner、Browser 和直接 Service 调用不得绕过共享边界。

验收：最后一个配额并发竞争不超额；无 claim 的调用不误计；执行失败和重启不退回配额。

#### A4. 停止、状态和账本收口（completed）

- 总量、Model、Tool、Token、Duration 耗尽使用统一的 `pentest.budget_exhausted` 事实结构；
- 并发容量满继续使用 `pentest.budget_capacity_reached`，不升级为硬停止；
- status 显示 limit、used、complete/incomplete 和 stop confirmation；
- 覆盖暂停失败、Workflow signal 失败和跨进程 Stop Proof 读取；
- 实现提交完成后，单独更新实施账本并把 PEN-500 标记为 `completed`。

### 7.4 阶段 A 非目标

- 不新增数据库表；
- 不新增通用 Policy Engine；
- 不新增定时 Budget Worker；
- 不修改 Code Audit Budget；
- 不建设计费系统；
- 不为未来分布式数据库设计抽象。

---

## 8. 阶段 B：一个网络服务专业闭环

只选择一个可复位、明确授权、默认不依赖公网的网络服务靶场，贯通：

```text
目标解析
→ 可达性
→ 端口/服务发现
→ 版本与配置线索
→ Hypothesis
→ 最小验证
→ Evidence 或 Negative Result
→ Finding / Closure
```

### 8.1 实现原则

- 优先复用 Runner Tool、MCP、Execution、Artifact、Evidence、Reasoning Graph 和现有 Pack；
- 只接通一个专业工具路径；不存在真实需要时，不新增 Scanner Framework；
- 可选工具缺失时允许降级，但 status/report 必须说明未执行能力；
- 扫描输出只能形成 Observation 或 Hypothesis，不能直接成为 Confirmed Finding；
- 最小验证必须记录前置条件、风险、Approval、正负判据、Evidence capture 和 stop condition；
- 失败、不可达、无匹配和被安全控制阻断必须形成 Negative Result 或明确未完成原因；
- 重复动作受预算和已有 Observer/Closure 约束；
- 至少证明一次工具故障或暂停恢复后的持久继续执行。

### 8.2 最小交付物

1. 一个仓库内可复位靶场配方；
2. 一个生产 CLI/API 启动流程；
3. 一个真实 Tool 输出进入 Artifact/Evidence 的解析路径；
4. 一个成立的 Finding 或一个证据充分的 Negative Result；
5. 跨重启状态读取；
6. 结构化报告可读取该结果；
7. 对应 E2E、失败路径和安全回归。

阶段 B 的完成门是“一条专业路径可重复”，不是工具或 Pack 数量。

---

## 9. 阶段 C：状态化 Web、Attack Chain 与报告

### 9.1 一个状态化 Web 场景

选择一个包含登录、角色或对象授权的本地可复位靶场，完成：

- Browser、Target HTTP 和 Traffic 使用统一 Run/Session/Request identity；
- Cookie、Token 和密码只通过 Secret Reference 使用，不进入事件、URL、Artifact 标题或报告；
- 登录、角色、会话和请求状态可在暂停或重启后恢复；
- 请求/响应 Diff、重放和最小化复用现有 Traffic/Evidence；
- 人工接管后生成结构化 Takeover Summary；
- 身份或状态变化导致的响应差异形成 Evidence；
- 越界 URL、重定向、子资源和回调继续执行 Scope 检查。

### 9.2 最小验证语义

复用现有 Task Graph 和 Reasoning Graph，只补真实场景证明缺少的最小字段或关系：

- Hypothesis；
- prerequisite；
- minimal action；
- positive/negative criterion；
- risk/approval；
- evidence capture；
- stop condition；
- retry/variant relation。

不另建 Pentest Planner、Attack Graph 数据库或常驻多 Agent 团队。

### 9.3 报告收口

优先扩展现有通用 Report projection，不复制 Report Service。报告至少包含：

- Engagement、授权、Scope、Admission 和固定 Capability；
- Attack Surface、Coverage 和未测试区域；
- Finding、影响、Evidence、复现条件和修复建议；
- Negative Result、限制、阻断点和未完成项；
- Attack Chain 的已确认段、假设段和前置条件；
- 预算耗尽、取消、失败、超时、重启和人工停止的 Stop Proof。

报告只能读取权威持久事实，不能从最后一段模型文本生成“看起来完整”的结论。

---

## 10. 阶段 D：兑现“越用越好用”

V1 先完成**用户驱动、人工批准**的一项能力成长，不做自动自我修改。

### 10.1 最小成长闭环

```text
用户加入 Tool / Skill / Technique
→ 静态校验与权限声明
→ Version / Digest / Provenance
→ 在测试 Run 中显式 Selection
→ 使用现有事实进行复盘
→ 用户批准
→ 在新 Run 中生效
→ 禁用或回滚
```

### 10.2 最小实现方式

- Tool 继续使用现有 Tool Registry；
- Skill 继续使用 Operator Skill root 和 Progressive Skill loader；
- Technique 继续使用 Capability catalog；
- 运行选择继续使用 Selection snapshot 和 Pack Lock；
- 复盘输入优先投影现有 Event、Task、Attempt、Evidence、Negative Result、Finding 和 Closure；
- 初版复盘可以是确定性导出加人工判断，不先建 Trajectory Store；
- Capability Candidate 和 Promotion 已有底座时直接接通，不重做生命周期；
- 若 CLI 缺少必要入口，只增加薄命令调用现有 Application/API。

### 10.3 必须证明

1. 新能力不会扩大 Run Scope；
2. 新能力不能降低 Approval；
3. 新能力不能获得未在 Selection/allowlist 中的 Tool；
4. Digest 或版本漂移时失败关闭；
5. 测试 Run 与生产 Run 的选择可追溯；
6. 用户能看见变更内容和来源；
7. 禁用后新 Run 不再选择；
8. 回滚后恢复旧版本；
9. 旧 Run 仍能解释当时固定的版本。

### 10.4 延后项

- Agent 自动写 Skill；
- 自动批准和自动激活；
- 在线 Marketplace；
- 组织级共享 Profile；
- 第二套向量检索或知识图谱；
- 未脱敏原始聊天长期保存。

只要专业人士能够安全地把自己的方法加入 RiftX，并在后续 Run 中稳定复用，高上限的核心承诺就已经成立。

---

## 11. 阶段 E：默认产品面收缩与发布

### 11.1 消费者审计

为候选模块建立一次性清单：

| 模块 | Pentest 消费者 | 默认入口 | 启动成本 | 数据兼容 | 安全价值 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 待审计 | CLI/API/Worker/E2E 引用 | 默认/可选/无 | 实测 | migration/历史数据 | 必需/可选/无 | 保留/按需/隔离/删除 |

优先审计：

- 默认 CLI 命令面和 API routes；
- Worker Runtime eager 初始化；
- Code Audit 专属 Runtime、preflight、snapshot 和 source materialization；
- 未进入两个真实 E2E 的 Connector、Adapter、Demo、Pack 和 UI；
- 只被测试引用、没有产品入口的辅助层；
- 语义重复的 Effect Policy 名称、兼容 wrapper 和旧入口。

### 11.2 处理顺序

```text
收缩默认文档和入口
→ 改为按需加载
→ 测量启动/内存/依赖
→ 隔离冻结模块
→ 删除已证明无消费者的代码
→ migration/恢复/全仓回归
```

每次删减独立提交。不得用一次“大清理”同时删除 Domain、migration、API 和测试。

### 11.3 发布检查

R1 至少包含：

- 全新环境 Onboard、Doctor 和模型配置错误诊断；
- 阶段 A 的全部预算与停止检查；
- 阶段 B 网络服务场景；
- 阶段 C 状态化 Web 场景与报告；
- 阶段 D 用户能力添加、选择、禁用和回滚；
- Scope、Approval、Credential、Redaction 和 Effect Policy 安全检查；
- 取消、失败、超时、重启和 Stop Proof；
- migration upgrade、受保护 downgrade、Backup/Restore；
- 默认 CLI/API 产品面、安装包资产和可选工具降级；
- 已知限制、未执行能力和不完整 Coverage。

评测用于自身回归、复现、发布检查和能力演进，不承担“量化证明超过通用 Agent”的发布义务。

---

## 12. 开发准入与完成判定

任何新增抽象、表、依赖、后台服务或兼容层前必须回答：

1. 哪个当前 Pentest 用户流程无法由已有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会导致什么当前用户失败？
4. 能否改为现有 Service 的一个方法、一个确定性投影或一个薄 CLI？
5. 是否扩大权限、migration、恢复和测试面积？

答案不具体时，不实现。

一个切片只有产生以下至少一个结果才算有效进度：

- 用户可以完成新的 Pentest 操作；
- 一个真实副作用进入受控生产路径；
- 一个持久状态可跨进程恢复；
- 一条 Evidence/Negative Result/Finding 链可审查；
- 一个失败、停止或恢复场景被证明正确；
- 一项用户能力能被安全复用或回滚；
- 默认启动或产品面通过实测变得更简单。

只增加 Domain、Repository、Adapter、Graph、空 CLI、空服务或设计文档，不算完成。

---

## 13. 验证与 Git 纪律

### 13.1 分层验证

| Gate | 触发条件 | 最小要求 |
| --- | --- | --- |
| Slice | 每个实现提交 | 目标测试、受影响回归、Ruff、必要 mypy/typecheck、`git diff --check` |
| User result | 一个纵向结果完成 | CLI/API、持久化、权限、失败、恢复、跨进程读取 |
| Milestone | 阶段 A-E 完成 | 全仓 Python、相关前端/桌面 build、migration/release checks |
| Release | 发布候选 | 两个真实靶场、能力成长、升级恢复、安全评审、已知限制 |

所有 Agent 相关测试和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

安全路径、migration、Runner ownership、Effect Policy 和 Stop Proof 不能因为测试耗时而跳过阶段门。

### 13.2 Git 纪律

- 一个实现提交只表达一个用户可解释或安全可验证的结果；
- 实现提交与实施账本提交分开；
- Task 完成后更新 `FORMAL_AGENT_PROGRESS.md`；
- 不提交无关用户改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前检查 staged diff、`git diff --cached --check` 和实际测试命令；
- 每个切片先保证可回滚，再进入下一个切片。

进度不按代码量、表数量、Pack 数量或累计测试数量判断，只看用户路径、持久恢复、安全边界、证据链、停止证明和能力复用。

---

## 14. Codex 执行协议

Codex 每轮只处理一个最小纵向切片。开始前明确：

```text
Current phase:
Pentest user outcome:
Existing production path to reuse:
Smallest implementation slice:
Files expected to change:
Explicit non-goals:
Scope/Approval impact:
Persistence/migration impact:
Evidence/recovery behavior:
Target tests:
Implementation commit:
Ledger commit:
```

执行顺序：

1. 读取本文件、实施账本、相关 ADR、最近提交和工作树；
2. 从 CLI/API 追踪到持久化和真实副作用；
3. 查找现有事实、Service 和生产装配；
4. 写出一个用户结果和明确非目标；
5. 修改最少文件完成该结果；
6. 使用 conda `agent` 环境运行目标测试和受影响回归；
7. 通过后形成独立实现提交；
8. Task 级结果完成后，单独更新实施账本并提交；
9. 从下一条未满足验收条件继续。

遇到以下情况必须停止扩张并重新审查：

- 正在实现冻结或 Post-V1 范围；
- 新抽象没有当前生产消费者；
- 新表复制已有权威事实；
- 一个小功能要求修改大量无关模块；
- 只有单元测试，没有 CLI/API/Worker 生产路径；
- 为未来可能性阻塞当前两个 Pentest E2E；
- 通过 Prompt 修补本应由确定性安全边界处理的问题。

---

## 15. 当前唯一施工指令

从 `1c379dcc` 继续，只做阶段 B 的一个网络服务专业闭环：

1. 先从现有 Official Pack、Tool Registry 和仓库测试资产中选择一个可复位、明确授权、默认离线的网络服务靶场；
2. 固定唯一用户结果：从目标解析、可达性和服务发现走到一个 Evidence 支撑的 Finding，或一个证据充分的 Negative Result；
3. 复用现有 Runner、Execution、Artifact、Evidence、Reasoning、Finding、Closure 和 Report，不新增 Scanner Framework、Planner、Graph 或事实表；
4. 只接通一个生产 Tool 输出解析路径，扫描结果先成为 Observation/Hypothesis，最小验证后才能形成 Confirmed Finding；
5. 记录前置条件、Approval、正负判据、Evidence capture、stop condition、未执行能力和失败原因；
6. 证明预算、Scope、暂停、工具故障和重启不会丢失专业状态，也不会绕过副作用门禁；
7. 提供一个生产 CLI/API 启动入口和一个可读取该结论的结构化 Report；
8. 使用 conda `agent` 环境运行目标 E2E、失败路径、受影响回归、全仓 Ruff 和 scoped mypy；
9. 每个纵向切片独立提交，阶段 B 完成后再单独更新实施账本；
10. 不启动状态化 Web、Code Audit、学习平台、Marketplace、更多 Pack、更多 Scanner、UI 扩展或大规模代码删除。

---

## 16. 历史任务图兼容附录

本附录仅保持 ADR、实施账本和文档合同的一致性。它不是当前排期；实际施工只遵守第 6 节的 A→E 路线。

### SEC-000：正式版 ADR 与实施账本

**依赖**：无。

### SEC-001：Security Capability Evaluation 骨架

**依赖**：SEC-000。

### CAP-001：Capability Domain 与持久化

**依赖**：SEC-000。

### CAP-100：接通生产 Progressive Skill

**依赖**：CAP-001。

### CAP-101：原生代码工具

**依赖**：CAP-001。

### CAP-102：Browser/Web/Traffic Tool 闭环

**依赖**：CAP-001。

### CAP-103：MCP 生产接入

**依赖**：CAP-001。

### CAP-104：持久化 Tool/Skill Selection

**依赖**：CAP-100、CAP-103。

### COG-200：Task Graph

**依赖**：CAP-104。

### COG-201：Evidence Ledger

**依赖**：COG-200。

### COG-202：Reasoning Graph

**依赖**：COG-201。

### COG-203：Primary Agent Proposal Tools

**依赖**：COG-202。

### COG-204：Observer Supervisor 与 Projector

**依赖**：COG-203。

### COG-205：Closure Verifier

**依赖**：COG-204。

### PACK-300：基础渗透 Packs

**依赖**：CAP-102、CAP-104、COG-205。

### PACK-301：基础代码审计 Packs

**依赖**：CAP-101、CAP-104、COG-205。

### PACK-302：Onboard 和 Doctor

**依赖**：PACK-300、PACK-301。

### AUD-400：Repository Intelligence

**依赖**：CAP-101、COG-202。

### AUD-401：Scanner Adapter

**依赖**：AUD-400。

### AUD-402：专业角色工作流

**依赖**：AUD-400、AUD-401、COG-205、PACK-301。

### AUD-403：代码证据模型

**依赖**：COG-201、AUD-400、AUD-401。

### AUD-404：Diff Audit 与 Variant Analysis

**依赖**：AUD-400、AUD-403。

### AUD-405：受控动态验证

**依赖**：CAP-101、AUD-403。

### PEN-500：Pentest Admission 与 Attack Surface

**依赖**：CAP-102、COG-202。

### PEN-501：状态化 Web 测试

**依赖**：CAP-102、PEN-500。

### PEN-502：验证规划器

**依赖**：COG-203、PEN-500、PEN-501。

### PEN-503：CVE/PoC Research

**依赖**：CAP-102、PEN-502。

### PEN-504：Attack Chain、Report 与 Stop Proof

**依赖**：COG-201、PEN-500、PEN-502。

### LEARN-600：Trajectory Store 与 Session Search

**依赖**：COG-205。

### LEARN-601：Post-run Review

**依赖**：LEARN-600。

### LEARN-602：Failure Taxonomy

**依赖**：LEARN-601。

### LEARN-603：Replay Lab

**依赖**：SEC-001、LEARN-601、LEARN-602。

### LEARN-604：Capability Curator

**依赖**：CAP-001、LEARN-603。

### LEARN-605：Profile、导入和迁移

**依赖**：LEARN-604、PACK-302。

### EVAL-700：代码审计语料

**依赖**：SEC-001、AUD-403、AUD-404。

### EVAL-701：渗透测试靶场

**依赖**：SEC-001、PEN-504。

### EVAL-702：版本、配置与能力包回归 Harness

**依赖**：EVAL-701、LEARN-603。

### EVAL-703：质量与安全发布检查

**依赖**：EVAL-702、PACK-302。

### ECO-800：Pack SDK

**依赖**：CAP-001、LEARN-604。

### ECO-801：信任与供应链

**依赖**：ECO-800。

### ECO-802：Gateway 与持续运行

**依赖**：LEARN-605、ECO-801。

---

## 17. 最终定位

RiftX 不需要继续证明自己是一个功能更多的通用 Agent 平台。它需要成为：

> 一个知道授权边界、能持续执行和恢复、会记录证据与失败、能输出专业结果，并允许操作者把自己的方法安全沉淀为下一次生产能力的渗透测试工作台。

完成目标的最短路径是：

```text
完成剩余预算门禁
→ 完成一个网络服务闭环
→ 完成一个状态化 Web 与报告闭环
→ 完成一项用户驱动能力成长
→ 收缩默认产品面并发布
```

当前不需要更多架构。当前需要让已有架构产生连续、可重复、可审查的专业结果。
