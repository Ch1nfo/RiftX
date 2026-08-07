# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前阶段唯一的产品收敛、代码优化与 Pentest-first V1 完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 当前实现基线：`b2193139`（阶段 A-B 已完成）
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
- Evidence、Negative Result、Draft Finding、Closure 和 Report 已在一个真实网络服务场景中形成可重复生产闭环，但状态化 Web/Attack Chain 尚未闭环；
- Capability、Version、Selection、Pack Lock、Progressive Skill 等成长底座已经存在，但“专业人士添加方法并在下一次运行中安全生效”的用户闭环尚未完成；
- Code Audit、Marketplace、多租户、远程集群、更多 Agent 角色和更多 Pack 继续冻结。

**当前不应重写架构，也不应先删除大块代码。阶段 A-B 已完成；下一步只复用现有 Target HTTP、Traffic、Secret Reference、Scope、Approval、Evidence、Reasoning 和 Report，建立一个有两个测试用户与两个对象的最小本地授权差异场景。此前不启用 Browser、Crawler、Fuzzer、新身份系统或新 Web Scanner。**

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
| Artifact → Evidence | Primary Agent 可把当前 Run 的 Artifact 精确 byte span 登记为稳定、可回放 Evidence，并用于 Reasoning | 已完成 |
| 服务假设验证 | 受批准 Target HTTP 可将受保护响应登记为 Evidence，形成 Observation、Vulnerability Candidate、幂等 Draft Finding 和精确 Negative Result | 已完成 |
| 网络服务结案 | JSON Report v2 可从持久事实重建 Engagement、Admission、Capability/Pack Lock、预算、Execution、Evidence、Reasoning、Draft Finding、Negative Result、Closure 和 Stop Proof | 已完成 |
| 专业事实底座 | Task、Evidence、Reasoning、Negative Result、Finding、Closure、Report 已存在 | 已在网络服务场景形成生产闭环，下一步复用到状态化 Web |
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

阶段 B 已完成实现：

```text
54c2489b  register artifact evidence
c7709790  close service enumeration loop
5018f071  verify service hypotheses
b2193139  close network service reporting
```

最近已验证的相关回归包括：

```text
Full Control Plane: 67 passed
Runtime/Execution/Target HTTP/Worker: 467 passed
Budget handling focused regression: 215 passed
B1 Artifact → Evidence focused regression: 151 passed
B2 relevant regression: 235 passed
Full Control Plane after B2: 68 passed
B2 Pack/upgrade regression: 14 passed
B3 affected Runtime/Evidence/Repository regression: 117 passed
Full Control Plane after B3: 68 passed
B3 Pack regression: 13 passed
B4 affected Report/Closure/Worker regression: 78 passed
Full Control Plane after B4: 68 passed
B4 final focused regression: 19 passed
Full Ruff: passed
Changed production files scoped mypy: passed
```

实施账本已回填 Attack Surface、隔离生命周期和全部预算提交；PEN-500 已满足关闭条件。

### 2.2 尚未完成的核心结果

| 缺口 | 当前状态 | V1 必须 |
| --- | --- | --- |
| 网络服务专业闭环 | 已完成枚举、HTTP 正负验证、Draft Finding、Closure 和 JSON Report | 已完成 |
| 状态化 Web 闭环 | Browser、Traffic、Target HTTP 已存在，身份/授权场景未闭环 | 是 |
| 专业报告 | 网络服务 JSON Report 已验收；状态化 Web 与 Attack Chain 尚未验收 | 是 |
| 用户驱动能力成长 | Capability 底座存在，缺少一次完整添加、选择、复盘、禁用和回滚 | 是 |
| 默认产品面收缩 | 未按真实消费者审计 | 是 |
| 大规模代码删除 | 尚无足够消费者证据 | 否，延后到收缩阶段 |

阶段 B 的专业事实链已关闭：

```text
Execution
→ ExecutionArtifactStore（已将 stdout/stderr 注册为不可变 Artifact）
→ ToolResultProcessor（已确定性解析 Nmap XML）
→ Agent Tool Result Context（已完成）
→ register_artifact_evidence（已完成；Primary-only、当前 Run、精确 byte span）
→ Evidence Ledger（生产写入与幂等语义已接通）
→ Reasoning Observation/Hypothesis（已完成）
→ Target HTTP 最小验证（已完成）
→ Vulnerability Candidate/Draft Finding/Negative Result（已完成）
→ Closure/JSON Report v2（已完成）
```

可直接复用的现有生产事实：

- `service-enumeration` Official Pack 已声明 `port_scan`、`run_registered_tool`、`read_artifact`、`record_observation` 和 `record_negative_result`；
- `ToolResultProcessor` 已保留原始输出、解析 Nmap XML、生成结构化结果与有界摘要；
- `EvidenceApplicationService.register_artifact_span` 已由生产 Worker 和 Primary-only Control Tool 装配；
- Reasoning 的 Observation、Fact、Finding Candidate 和 Negative Result 已强制校验 Evidence ID；
- Finding 已能从 Reasoning Vulnerability Candidate 确定性投影为 Draft，并引用同 Run 的 Artifact/Execution；
- Target HTTP 原始请求/响应仍不对通用 Artifact 读取面可见，只能通过同 Run 持久关联在服务端登记 Evidence 或校验 Finding；
- Report v2 已能从权威持久状态重建 B3 事实，保留 Draft、精确否定、未验证假设、执行状态、预算停止原因和 Closure；
- Target HTTP 原始体、授权引用原文和本地路径不进入 Report；受保护 Evidence 只暴露 opaque ID、Digest 和安全 Locator；
- 现有测试已有 Nmap golden fixture、`fake_nmap.py` 和可复用的本地异步 HTTP 目标生命周期。

因此，下一个提交应该是“复用现有 HTTP/Traffic 事实建立一个最小身份与对象授权靶场”，而不是新增 Browser 自动化框架、Crawler、Fuzzer、身份服务或 Web Scanner。

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
| B. 网络服务专业闭环 | completed | 一个真实服务从枚举走到证据化结论、Closure 和 Report |
| C. 状态化 Web 与报告 | in progress；C1 当前施工 | 一个身份/授权场景走到 Attack Chain、Closure 和 Report |
| D. 用户驱动能力成长 | pending | 一项专业方法可添加、选择、复盘、禁用和回滚 |
| E. 默认产品面收缩与发布 | pending | Pentest-first 产品可安装、可理解、可回归、可发布 |

除安全修复、数据兼容和当前用户阻断问题外，不得跳过阶段开启冻结范围。

---

## 7. 阶段 A：已完成合同

阶段 A/PEN-500 已完成并由实施账本记录。后续不得重复实现预算体系，只把以下内容视为必须保持的回归合同：

- Model/Token/Duration 在 Provider `start/resume` 前检查；
- Tool/Duration 在共享 execution claim 前检查；
- Tool Proposal、等待批准、Scope 拒绝不计预算；
- 新 retry 重新计数，完全相同的幂等重放不重复计数；
- 总量耗尽使用 `pentest.budget_exhausted`、Pause、Safety Stop 和 Stop Proof；
- 并发容量使用 `pentest.budget_capacity_reached`，允许重试且不暂停 Run；
- 状态读取、预算门禁和跨重启恢复读取同一持久事实。

已完成实现提交：

```text
73288673  project live run usage
b6b5f739  enforce model token duration budgets
53812397  enforce tool execution budgets
1c379dcc  unify budget exhaustion handling
0b53e43d  close pentest admission budget stage
```

阶段 B-E 只有在破坏上述语义时才修改预算代码。不得新增 Budget 表、计费系统、定时 Budget Worker 或第二套停止服务。

---

## 8. 阶段 B：一个网络服务专业闭环

阶段 B 只交付一个仓库内可复位、明确授权、默认不依赖公网的网络服务场景：

```text
Pentest Admission
→ service-enumeration
→ Nmap/注册工具真实 Execution
→ 原始 Artifact
→ Evidence
→ Observation
→ Hypothesis
→ 最小协议验证
→ Finding 或 Negative Result
→ Closure / Report
```

### 8.1 B0：生产链审计（completed）

审计结论：

- 选择现有 `service-enumeration` Pack，不新建 Pack；
- 选择现有 Nmap XML adapter 和 `ToolResultProcessor`，不新建 Scanner Framework；
- 选择现有 `ExecutionArtifactStore`，不新建扫描结果表；
- 选择现有 `EvidenceApplicationService.register_artifact_span`，不新建 Evidence 模型；
- 选择现有 Reasoning、Finding、Closure 和 Report Service，不新建 Pentest 专用 Graph 或 Report；
- CI 复用 `tests/tools/fixtures/fake_nmap.py` 和 golden XML；人工发布 Smoke 在系统安装 Nmap 时对同一 localhost 靶场执行真实 Nmap，不把外部工具存在作为普通测试前提。

B0 确认的首要断点是 `EvidenceApplicationService` 缺少生产写入入口；B1 已用薄 Control Tool 关闭该断点，Agent 现在可以取得供 `record_observation` 使用的 Evidence ID。

### 8.2 B1：Artifact → Evidence 生产入口（completed）

用户结果：Agent 读取一个已完成 Execution 的原始 Artifact 后，可以把精确、不可变、可回放的片段登记为 Evidence，并立即用于 Observation 或 Negative Result。

已完成实现：

1. 在生产 Worker 中装配现有 `EvidenceApplicationService`；
2. 增加 Primary-only 本地认知 Control Tool `register_artifact_evidence`；
3. 在有界 Tool Result Context 中暴露已存在的 opaque Artifact ID；Tool 输入只接受该 ID、精确 byte span、可选 Task ID 和目标引用；
4. 服务端解析并验证 Artifact owner、Run/Session/Task owner、span 上限、Digest、Redaction 和当前 Run 状态；
5. 返回稳定的 Evidence ID、canonical source URI 和 Digest；
6. `record_observation`、`propose_fact`、`propose_finding` 与 `record_negative_result` 继续只接受已存在且同 Run 的 Evidence ID。

实现复用了现有装配和薄适配层，未新增表、migration、Pack、Scanner、Planner、Graph、后台 Worker 或 UI。提交：

```text
54c2489b  feat(pentest): register artifact evidence
```

禁止：

- 自动把所有 stdout/stderr 晋升为专业结论；
- 让模型提交任意文件路径或伪造 Digest；
- 复制 Artifact 内容到第二张表；
- 让 parser 输出绕过 Evidence Ledger 直接成为 Confirmed Finding；
- 因工具失败自动写“端口关闭”等目标结论。

B1 已满足的回归合同：

- 成功 Execution 的 Artifact span 可登记并跨进程读取；
- 跨 Run Artifact、越界 span、缺失 Artifact、错误 Session/Task 全部失败关闭；
- 完全相同 Tool Call 的幂等重放不重复写入；不为新的显式登记另建去重表；
- Evidence 可被 `record_observation` 消费；
- parser error 和非零退出只能形成有证据的工具失败 Attempt/Observation，不能伪装成目标 Negative Result；
- 目标测试、受影响回归、Ruff、scoped mypy 和 `git diff --check` 通过；
- 单独实现提交，不同时加入靶场或报告改造。

### 8.3 B2：可复位本地服务与枚举 E2E（completed）

用户结果：一个 Pentest Run 在明确 localhost Scope 和预算内，能够发现本地服务并形成证据化 Observation 与 Hypothesis。

靶场保持最小：

- 使用仓库测试内的异步 TCP/HTTP 服务，随机监听 localhost 端口；
- 服务返回确定性、无真实 Secret 的 banner/headers；
- 提供一个正常端口和一个确定性失败分支；
- 每次测试结束关闭 socket，不引入 Docker、外部镜像或常驻进程；
- CI 使用已存在的 fake Nmap 进程验证完整注册工具、Execution、Artifact 和 parser 路径；
- 系统存在真实 Nmap 时，增加非阻塞的手工 Smoke 说明，不能让默认 CI 因缺少 Nmap 失败。

专业语义：

- 开放端口只形成“端点可达”的 Observation；
- 服务名、产品和版本是带来源与置信度的 Observation/Hypothesis；
- 默认端口、banner 或 scanner guess 不能直接变成漏洞；
- 超时、拒绝、解析失败和工具缺失分别记录，不混写为“目标安全”；
- Scope、Approval、Tool/Target Interaction/Duration 预算在真实副作用前继续生效。

B2 已满足的回归合同：

- 从 `riftx pentest start` 或等价生产 API 创建 Pentest；
- 获得真实 Execution、stdout Artifact、Evidence ID 和 Observation；
- 形成一个明确待验证 Hypothesis；
- 暂停后不继续发起目标交互，恢复后从持久状态继续；
- Control Plane/Worker 重建后仍可读取 Run、Execution、Artifact、Evidence、Reasoning 状态；
- 单独提交靶场与 E2E，不在该提交创建 Confirmed Finding。

已完成实现：

- Pentest `port_scan` 能力工具只接受结构化目标，服务端在生成可信 Nmap/Masscan argv 前执行 Scope 校验；
- 服务端生成的目标摘要进入现有 Target Interaction 预算，耗尽后在 Runner 副作用前暂停；
- `service-enumeration` 1.1.0 增加 Artifact Evidence、Reasoning Query 和 Hypothesis 工具；
- fake Nmap 对随机 localhost 端口执行真实 TCP/HTTP 探测并输出 Nmap XML；
- E2E 覆盖 Admission、Pack/Tool 固定、批准前零副作用、Execution、Artifact、Evidence、Observation、Hypothesis、预算暂停、恢复与跨重启读取；
- 未创建 Finding、Negative Result、新表、新 Scanner、Planner、Graph、Worker 或 UI。

提交：

```text
c7709790  feat(pentest): close service enumeration loop
```

### 8.4 B3：最小验证、Finding 与 Negative Result（completed）

用户结果：Agent 对 B2 的一个 Hypothesis 执行最小协议验证，并产生一种可审查结论。

推荐靶场事实：本地 HTTP 服务通过 banner 暴露一个只读诊断路径，最小 GET 验证该路径是否匿名泄露确定性部署元数据。数据只使用测试值，不放入真实 Credential 或 Secret。

必须同时证明：

- 正路径：Evidence 支撑一个低风险信息披露 Finding；
- 负路径：另一个猜测路径返回确定性无匹配，形成 Negative Result；
- 正负结论都引用精确 Artifact/HTTP Evidence；
- Finding 先为 Candidate/Draft；只有满足 reproduction contract、Evidence 和最小验证判据后才能 Confirmed；
- 相同动作不会因模型循环而无限重复；
- 预算耗尽、Scope 拒绝、Approval 拒绝和工具故障保留各自原因，不能被写成漏洞不存在。

若现有 Finding 与 Reasoning Finding 是两套未完全接通的表示，只做一个确定性投影或薄调用；不得新建第三套 Finding 状态。

已完成实现：

- Primary-only `create_finding` 只接受现有 `VULNERABILITY_CANDIDATE/CANDIDATE`，并固定创建 Draft；
- Finding ID 由 Run/Session/Tool Call 确定性生成，同内容重放不重复写入，内容漂移失败关闭；
- Target HTTP 受保护 Artifact 保持对通用读取面隐藏，但可通过同 Run 交换关联登记精确 Evidence 并校验 Finding；
- Target HTTP Control Intent 由服务端生成脱敏 origin `target_summary`，Nmap 与 HTTP 跨工具共享同一 Target Interaction 预算；
- 同一 localhost E2E 已覆盖 Nmap、HTTP 200/404、批准前零副作用、Evidence、Observation、Candidate、Draft Finding、Negative Result、预算暂停和跨重启读取；
- 没有自动创建 Confirmed Finding，也没有新增表、migration、Pack、Scanner、Planner、Graph、Worker 或 UI。

提交：

```text
5018f071  feat(pentest): verify service hypotheses
```

### 8.5 B4：Closure、Report 与阶段关闭（completed）

用户结果：最终状态下的结构化 Report 能解释做了什么、证据在哪里、什么成立、什么不成立、什么没有执行以及为什么停止。

最小报告内容：

- Engagement、Scope、Admission、固定 Capability 与预算；
- 发现的端点、服务 Observation 和仍未验证的 Hypothesis；
- Confirmed/Draft Finding 与精确 Artifact/Execution/Evidence 引用；
- Negative Result、工具错误、被阻断动作和未测试区域；
- Run 最终状态、Closure outcome、Safety Stop/Stop Proof；
- 可复现但不泄露本地路径、Secret 或未脱敏响应。

阶段 B 完成门：

1. B1-B4 每个纵向切片独立提交；
2. 一个生产 CLI/API 路径可重复完成；
3. 成功、Negative Result、工具错误、暂停恢复和重启读取均有测试；
4. 结构化 JSON Report 可由权威持久事实重建；
5. 实施账本单独更新并提交；
6. 完成后才允许开始阶段 C。

已完成实现：

- 现有 `ReportApplicationService` 生成 `riftx.report.v2`，Pentest 投影显式包含 Engagement、Admission、Capability Selection/allowlist、Pack Lock、预算使用、Stop Proof、Execution、Evidence、Reasoning Node/Edge 和 Task；
- Report 保留 Domain Finding 的 Draft/Confirmed 状态与 Reasoning 的 Observation、Hypothesis、Vulnerability Candidate、Negative Result，不自动提升结论；
- `pentest.budget_exhausted`、Pause、Resume、Cleanup 与 Closure 通过安全字段白名单进入报告；
- Target HTTP 受保护 Artifact 继续不进入通用 Artifact/Event/Report 读取面，Evidence 仅输出 opaque ID、Digest、Redaction/Replay 状态和安全 Locator；
- 同一 localhost E2E 已覆盖 Closure、JSON Report、Draft/Negative Result、预算停止原因、重启重建、Report 复用幂等和不泄露原始 HTTP 体/授权引用/本地路径；
- 未新增表、migration、Pack、Scanner、Planner、Graph、Worker、Browser 场景或 UI。

提交：

```text
b2193139  feat(pentest): close network service reporting
```

---

## 9. 阶段 C：状态化 Web 与 Attack Chain

阶段 C 只在阶段 B 完成后开始。它不重做报告系统，而是在 B 的证据链上增加身份、会话、授权差异和多步攻击链。

### 9.1 C1：一个最小身份/对象授权靶场（当前唯一实现切片）

使用仓库内可复位的本地 Web 服务，只保留：

- 两个测试用户、两个对象和一个登录入口；
- 一个正常访问路径；
- 一个可验证的对象授权缺陷或明确无缺陷分支；
- 确定性测试数据，不使用真实账号、Token 或公网服务。

先使用 Target HTTP 和 Traffic 完成协议级验证。只有登录流程确实依赖浏览器行为时才启用 Browser；不得为了“覆盖 Browser”强行增加 UI 自动化。

### 9.2 C2：身份、状态与最小验证

必须复用现有 Run/Session/Request identity、Traffic Ledger、Secret Reference、Scope、Approval、Evidence 和 Reasoning：

- Cookie、Token 和密码只通过 Secret Reference 使用；
- 暂停或重启后能够恢复必要的非敏感会话引用；
- 请求/响应 Diff 形成 Evidence；
- 越界 URL、重定向、子资源和回调逐次重新检查 Scope；
- 人工接管后保留结构化 Takeover Summary；
- Hypothesis 明确 prerequisite、minimal action、正负判据、risk/approval、evidence capture 和 stop condition；
- 只验证一个授权差异，不扩展成通用 Web Scanner、Crawler 或 Fuzzer。

### 9.3 C3：Attack Chain 与报告扩展

Attack Chain 只表达已持久存在的 Reasoning Node/Edge：

- 已确认段、假设段和被否定段分开；
- 每个已确认段引用 Evidence；
- 前置身份、会话和权限条件明确；
- 未执行、失败、超时和被安全策略阻断的分支明确；
- 报告扩展现有 `ReportApplicationService` projection，不复制 Report Service。

阶段 C 完成门：

1. 本地状态化 Web E2E 可重复；
2. Secret 不进入事件、URL、Artifact 标题、日志或报告；
3. 成功与无漏洞/被阻断分支都可解释；
4. 暂停、恢复、取消和跨进程读取保持身份状态与 Stop Proof；
5. JSON/Markdown Report 能重建 Attack Chain；
6. 分切片提交，最后单独更新账本。

---

## 10. 阶段 D：兑现“越用越好用”

V1 的高上限不依赖 Agent 自动修改自己，而依赖一个足够短、可审查、可回滚的用户能力闭环：

```text
专业用户加入一项本地 Skill
→ 静态校验权限与依赖
→ 形成 Version / Digest / Provenance
→ 测试 Run 显式 Selection
→ 查看使用事实与结果
→ 用户批准启用
→ 新 Run 固定使用
→ 禁用 / 回滚
```

### 10.1 D1：先证明一项 Skill，不同时建设三种扩展

首个闭环优先选择 Operator Skill，因为 Tool Registry、Progressive Skill loader、Capability Version、Selection 和 Pack Lock 已存在。只有该 Skill 确实需要新二进制时才同时接入一个注册 Tool。

最小用户结果：

- 用户把一个“服务识别/验证方法”目录放入 Operator Skill root；
- Doctor/verify 能指出 manifest、权限、依赖、Digest 或版本问题；
- 测试 Run 显式选择该 Skill；
- Selection snapshot 固定版本和来源；
- Skill 只能调用当前 Run 已允许的 Tool，不能扩大 Scope 或降低 Approval。

如果现有 CLI 已能完成某一步，只补文档或薄命令；不得新建第二套插件 SDK、Registry 或安装协议。

### 10.2 D2：复盘只做确定性导出加人工判断

初版复盘输入直接读取：

- Event、Task、Attempt；
- Artifact、Evidence、Observation、Hypothesis、Negative Result、Finding；
- Closure、Stop Proof；
- Capability Selection、版本与 Digest。

输出一个脱敏、结构化的 review summary，回答“用了什么、在哪些条件下有效、失败在哪里、是否值得继续启用”。用户作出批准、拒绝或保持测试状态的决定。

不先建设 Trajectory Store、Replay Lab、向量库、自动评分或自动改写 Skill。只有至少两个真实用户流程证明现有持久事实无法支持复盘时，才恢复 LEARN-600 以后相关设计。

### 10.3 D3：禁用与回滚

必须证明：

1. 新能力不扩大 Run Scope；
2. 新能力不能降低 Approval；
3. 新能力不能获得 Selection/allowlist 外的 Tool；
4. Digest 或版本漂移时失败关闭；
5. 测试 Run 与生产 Run 的选择可追溯；
6. 用户能看见变更内容和来源；
7. 禁用后新 Run 不再选择；
8. 回滚后恢复旧版本；
9. 旧 Run 仍能解释当时固定版本；
10. 删除本地源目录不会破坏旧 Run 的审计记录。

阶段 D 完成后，RiftX 已兑现“专业人士能把自己的方法安全沉淀为下一次生产能力”。自动生成 Skill、自动批准、在线 Marketplace、组织同步和自治进化全部属于 Post-V1。

---

## 11. 阶段 E：默认产品面收缩与发布

### 11.1 先收缩用户看到的产品

默认用户旅程只强调：

```text
onboard
→ doctor
→ model configure/default
→ pentest start/status
→ approvals
→ resume/stop
→ report
→ capability verify
```

必须完成：

- `Configured model not found` 类错误显示配置中的 model/provider/profile、实际可用候选和修复命令；
- CLI help、README 和示例以 Pentest 为主，Code Audit 和生态功能标记为 frozen/experimental；
- 当前只播放静态转录的 `demo pentest` 必须明确标记 simulated；阶段 B 完成后优先增加或替换为真实本地靶场 Smoke；
- 可选 Nmap、Browser 或 Connector 缺失时给出降级路径，不阻塞基础 Pentest；
- 没有实测启动瓶颈前，不做通用 lazy-loader 重构。

### 11.2 消费者审计

为候选模块建立一次性清单：

| 模块 | Pentest 消费者 | 默认入口 | 启动成本 | 数据兼容 | 安全价值 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 待审计 | CLI/API/Worker/E2E 引用 | 默认/可选/无 | 实测 | migration/历史数据 | 必需/可选/无 | 保留/按需/隔离/删除 |

优先审计：

- 默认 CLI 命令面和 API routes；
- Worker Runtime eager 初始化；
- Code Audit 专属 Runtime、preflight、snapshot 和 source materialization；
- 未进入阶段 B/C E2E 的 Connector、Adapter、Demo、Pack 和 UI；
- 只被测试引用、没有产品入口的辅助层；
- 语义重复的 Effect Policy 名称、兼容 wrapper 和旧入口。

### 11.3 处理顺序

```text
收缩默认文档和入口
→ 测量启动/内存/依赖
→ 改为按需加载或隔离冻结模块
→ 删除已证明无消费者的代码
→ migration/恢复/全仓回归
```

每次删减独立提交。不得用一次“大清理”同时删除 Domain、migration、API 和测试。Code Audit 代码在消费者审计前只冻结，不删除；Migration 历史永不重写。

### 11.4 发布检查

R1 至少包含：

- 全新环境 Onboard、Doctor 和模型配置错误诊断；
- 阶段 A 的全部预算与停止检查；
- 阶段 B 网络服务场景；
- 阶段 C 状态化 Web 场景与 Attack Chain；
- 阶段 D 用户能力添加、选择、禁用和回滚；
- Scope、Approval、Credential、Redaction 和 Effect Policy；
- 取消、失败、超时、重启和 Stop Proof；
- migration upgrade、受保护 downgrade、Backup/Restore；
- 默认 CLI/API 产品面、安装包资产和可选工具降级；
- 已知限制、未执行能力和不完整 Coverage。

评测只用于自身回归、复现、发布检查和能力演进，不承担“量化证明超过通用 Agent”的发布义务。

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

从实现基线 `b2193139` 继续，只做 C1“一个最小身份/对象授权靶场”：

1. 先审计现有 Target HTTP request schema、Traffic Ledger、Cookie/Header 处理、Secret Reference 和 localhost 测试服务，只修复阻断最小身份流的生产断点；
2. 在仓库测试内建立一个每次随机 localhost 端口、每次测试后关闭的状态化 HTTP 服务，不引入 Docker、外部镜像或公网依赖；
3. 靶场只保留两个测试用户、两个对象、一个登录入口、各自对象的正常访问与一个确定性跨对象授权分支；测试数据不得使用真实账号、Token 或 Secret；
4. C1 只验收靶场可复位性、登录状态、所有权正常访问、跨对象分支和请求前 Scope/Approval/预算边界；不在本切片创建 Finding、Attack Chain 或最终 Report；
5. 协议级登录能用 Target HTTP 完成时不启用 Browser；只有真实前端行为阻断 C2 时才准入 Browser；
6. Cookie、Token 和密码不得进入 URL、Event、Artifact 标题、Graph、Report 或测试失败输出；若现有 Target HTTP 尚不支持安全身份引用，只增加一个复用现有 Credential/Secret Reference 的薄入口；
7. 每个请求及重定向继续逐次执行 Scope 校验；未批准、越界、预算耗尽和登录失败不得产生目标成功结论；
8. C1 不新增身份服务、会话数据库、Crawler、Fuzzer、Web Scanner、表、migration、Pack、Planner、Graph、Worker 或 UI；
9. 所有 Agent 测试和运行使用 `conda run --no-capture-output -n agent ...`；通过目标测试、受影响回归、全仓 Ruff、scoped mypy 和 `git diff --check` 后形成 C1 独立实现提交，C1 完成前不进入 C2-D。

---

## 16. 历史计划的处理规则

原 SEC/CAP/COG/PACK/AUD/PEN/LEARN/EVAL/ECO 任务图只作为历史设计索引，不再复制到当前排期。其实施事实、已完成提交与兼容要求统一以 [`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md) 和 ADR 为准。

处理原则：

- 已被 A→E 路线消费的能力，只维持回归合同，不重复建设；
- Code Audit、Planner、CVE/PoC Research、Trajectory Store、Replay Lab、Marketplace 和 Gateway 继续冻结；
- 历史任务只有在 A→E 当前切片出现明确生产断点时，才可被拆成一个最小实现项重新准入。

---

## 17. 最终定位

RiftX 不需要继续证明自己是一个功能更多的通用 Agent 平台。它需要成为：

> 一个知道授权边界、能持续执行和恢复、会记录证据与失败、能输出专业结果，并允许操作者把自己的方法安全沉淀为下一次生产能力的渗透测试工作台。

完成目标的最短路径是：

```text
[已完成] 接通 Artifact → Evidence
→ [已完成] 完成一个网络服务的专业事实闭环
→ [当前] 完成一个状态化 Web 与 Attack Chain 闭环
→ [待完成] 完成一项用户驱动能力成长
→ [待完成] 收缩默认产品面并发布
```

当前不需要更多架构。当前需要让已有架构产生连续、可重复、可审查的专业结果。
