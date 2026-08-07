# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前唯一的产品收敛、代码优化与 Pentest-first V1 完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 已提交基线：`85decf8d`；C1 实现基线：`d73a0c86`
>
> 当前施工：C3 状态化 Web 结论、Closure 与 Report；C2 已完成并独立提交
>
> 实施事实与测试账本：[`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 平台边界：[`ADR-0012`](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest Admission 与 Attack Surface：[`ADR-0013`](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 0. 执行结论

RiftX 当前已经存在过度平台化，但还没有证据支持大规模删代码。

真正的问题不是“底座不足”，而是：

- 底座、模块、路由、Pack 和测试很多；
- 专业用户可连续完成的 Pentest 结果仍少；
- “越用越好用”的能力添加闭环尚未兑现；
- 默认产品面仍更像通用 Agent 平台，而不是专注的渗透测试 Agent。

因此，后续只做四件事：

1. 完成状态化 Web 专业闭环；
2. 完成一次用户 Skill 添加、选择、复盘、禁用和回滚；
3. 收缩默认入口、初始化和文档，不先重写架构；
4. 只删除已证明无生产消费者、无兼容价值且不承担安全责任的代码。

当前禁止继续建设新的通用平台能力。任何新工作都必须直接改善下列至少一项：

- 专业用户可以完成的 Pentest 操作；
- 目标副作用的安全约束；
- Evidence、Negative Result、Finding 或 Report 的可信度；
- 暂停、恢复、取消和重启后的连续性；
- 用户方法在下一次 Run 中的安全复用；
- 安装、配置和默认操作路径的可理解性。

---

## 1. 产品北极星

RiftX 的唯一正式版目标是：

> 成为一个专业人士手中好用、可控、可恢复，并且能通过持续加入 Tool、Skill、Technique 和实战方法而越来越顺手的授权渗透测试 Agent。

### 1.1 两个基础属性

**开箱即用的即战力**：

- Onboard、Doctor 和模型配置后可以启动一个基础 Pentest；
- 用户能看见 Scope、Approval、预算、运行状态和停止结果；
- 基础枚举、最小验证、证据登记和报告不依赖用户先开发插件；
- 配置或外部工具缺失时给出可执行修复方式。

**极高的专业上限**：

- 专业用户能加入自己的工具和方法；
- 每项能力具有 Version、Digest、Provenance 和 Permission；
- Run 显式固定 Selection，旧 Run 始终可解释；
- 能力可以试用、复盘、批准、禁用和回滚；
- 新能力不能扩大 Scope、降低 Approval 或绕过证据要求。

“超过直接使用 Codex、Claude Code、OpenCode”是追求方向，不是 V1 的量化发布条件。优势应来自长期领域复利，而不是一次排行榜：

- 通用 Agent 提供模型与推理能力；
- RiftX 提供授权边界、持久任务、专业事实、工具方法和操作者经验；
- 用户每次认可的方法都能成为下一次可控、可追溯的能力。

### 1.2 V1 非目标

以下内容不阻塞 Pentest-first V1：

- Code Audit 完全体及新的代码审计里程碑；
- Marketplace、在线 Registry、组织同步、多租户和远程集群；
- 常驻多 Agent 团队、新 Planner、新 Graph 或第二套 Runtime；
- 自动生成、自动批准和自动启用 Skill；
- CVE/PoC 自动研究平台、Trajectory Store、Replay Lab 和向量记忆系统；
- 更多 Official Pack、Scanner、Crawler、Fuzzer 和 Connector；
- 为量化证明超过通用 Agent 而建设综合评分；
- 与 Pentest 主路径无关的大规模 UI 重做。

---

## 2. 当前实际进度

### 2.1 项目规模信号

截至本次校准，仓库约有：

| 项目 | 数量 | 解释 |
| --- | ---: | --- |
| 生产 Python 文件 | 424 | 已达到必须严格控制新增抽象的规模 |
| Python 测试文件 | 298 | 测试面大，不代表专业用户闭环已完成 |
| API 路由 | 111 | 默认暴露面需要消费者审计 |
| Alembic migration | 51 | 历史不得重写，新增表必须极慎重 |
| Official Pack | 22 | V1 不再以增加 Pack 数量作为进度 |

这些数字只用于判断复杂度，不作为删除依据，也不作为产品成熟度指标。

### 2.2 已完成能力

| 能力 | 当前事实 | 处置 |
| --- | --- | --- |
| Onboard 与诊断 | 配置初始化、Doctor、migration、Backup/Restore、Pack repair 已存在 | 只修真实用户阻断 |
| Durable Runtime | Run、Temporal、Runner、Execution、取消、恢复和 Stop Proof 已存在 | 不建第二套 Runtime |
| Pentest Admission | 专用创建入口、Capability Selection、Pack Lock、Scope 和预算已存在 | 普通 Run 不得绕过 |
| Pentest 控制 | `pentest start/status/resume/stop` 已存在 | 收敛为默认主入口 |
| 安全边界 | Scope、Approval、Credential Reference、Redaction、Effect Policy 已存在 | 不得以 YAGNI 为由删除 |
| 执行预算 | Model、Tool、Token、Duration、Target Interaction 已在副作用前持久检查 | 只维持回归合同 |
| Attack Surface | declared/observed/verified 可从权威事实重建 | 不建第二套资产库 |
| 专业事实 | Task、Evidence、Reasoning、Negative Result、Finding、Closure、Report 已存在 | 继续复用 |
| 网络服务闭环 | 枚举、Artifact、Evidence、Hypothesis、最小验证、Draft Finding、Negative Result、Closure、JSON Report 已完成 | 阶段 B completed |
| 状态化 Web 靶场 | Alice/Bob、两个对象、登录、所有权基线、跨对象分支和预算暂停已完成 | 阶段 C1 completed |
| 状态化 Web 身份证据 | Credential Reference、两身份基线、跨对象 Attempt、Evidence、Reasoning 和重启重建已完成 | 阶段 C2 completed |
| 能力底座 | Capability、Version、Digest、Provenance、Selection、Pack、Progressive Skill 已存在 | 缺少用户闭环 |

已完成主线提交：

```text
54c2489b  feat(pentest): register artifact evidence
c7709790  feat(pentest): close service enumeration loop
5018f071  feat(pentest): verify service hypotheses
b2193139  feat(pentest): close network service reporting
d73a0c86  feat(pentest): establish stateful web target
9c0fe158  docs(plan): advance stateful web verification
85decf8d  feat(pentest): authorize stateful web credentials
```

### 2.3 C2 已完成事实

C2 已完成：

- Target HTTP 支持 `header_secret_refs`、`body_secret_ref` 和 `cookie_secret_refs`；
- Credential Reference 由当前 Session 已固定 Technique 的 Permission 授权；
- 引用在 Runner 发送前才按 Environment → Keyring 解析；
- 明文值不进入 ToolCallIntent、请求 fingerprint、Artifact 快照或 Control Plane；
- 未选择、未激活、越权、缺失或损坏的引用/Selection 在网络副作用前失败关闭；
- 完整生产链完成 Alice/Bob 登录、各自基线和一次 Alice 跨对象验证；
- 正常与跨对象响应分别登记受保护 Evidence，并形成 Observation、Hypothesis 和 Attempt；
- Control Plane 重建后可恢复 Selection、Traffic、Evidence、Reasoning 和 Working Memory；
- 6 次已批准尝试中只有 5 次形成真实 HTTP 交换，未授权引用保持零网络副作用；
- 未创建 Finding、Report、新表、migration、Browser、Scanner、Worker 或 UI；
- 实现提交：`85decf8d`。

### 2.4 当前真正缺口

1. 状态化 Web 尚未形成最终专业结论和报告；
2. 用户添加方法后，尚无一次完整的试用、复盘、启用、禁用和回滚；
3. `Configured model not found` 等错误仍可能只暴露内部状态，没有直接修复路径；
4. README、CLI 和启动路径仍展示大量通用平台能力；
5. 非默认模块是否造成启动、依赖和维护负担尚未测量；
6. 代码审计功能目前冻结，但未经过消费者审计，不应现在删除。

---

## 3. 过度开发处理原则

### 3.1 立即保留

- Scope、Approval、Credential、Redaction 和 Effect Policy；
- Run、Execution、Runner ownership、恢复、取消和 Stop Proof；
- Artifact、Traffic、Evidence、Reasoning、Finding、Closure 和 Report；
- Capability Version、Digest、Provenance、Selection 和 Pack Lock；
- migration 历史、Backup/Restore 和旧数据兼容读取；
- 被当前 Pentest CLI/API/Worker/E2E 直接使用的 Tool、Target HTTP、Browser 和 MCP 路径。

### 3.2 立即冻结

在 V1 发布前，不新增：

- Code Audit 功能、Pack、Detector 或 UI；
- Marketplace、Gateway、多租户、远程集群和组织同步；
- 新 Agent 角色、常驻多 Agent、Planner、Graph 或 Runtime；
- 新 Policy Engine、Budget Service、资产数据库和向量数据库；
- 没有当前生产消费者的 Domain、Repository、Adapter 和后台任务；
- 与剩余 C2、C3、D、E 无关的 Pack、Tool 和 Connector。

### 3.3 先收缩，再删除

优化顺序固定为：

```text
收缩默认文档与入口
→ 对非主线模块按需初始化
→ 测量启动时间、内存、依赖和导入成本
→ 审计真实消费者
→ 隔离冻结功能
→ 删除已证明无消费者的代码
```

不得先做“大清理”。大量删除会同时扩大 migration、恢复、安全和回归风险，却不一定改善专业用户体验。

### 3.4 删除准入门

生产代码只有同时满足以下条件才能删除：

1. 没有 Pentest 主路径消费者；
2. 没有默认 CLI、API、Desktop 或 Worker 消费者；
3. 不是 migration、Backup/Restore 或旧数据读取所需；
4. 不承担 Scope、Approval、Credential、Evidence、Provenance 或 Stop Proof；
5. 没有受支持用户数据依赖；
6. 禁用或替代路径已有说明；
7. 引用审计、目标测试、migration 回归和 Pentest E2E 全部通过。

Migration 历史永不删除或重写。优先删除重复装配、无入口 wrapper 和不可达分支，最后才考虑 Domain 或持久化模型。

---

## 4. Pentest-first V1 完成定义

V1 不要求功能最多，只要求以下用户结果连续成立：

1. 新用户可以完成 Onboard、Doctor、模型配置和第一个授权 Pentest；
2. 模型、Provider、Profile 或 Credential 配置错误会指出错误对象、可用候选和修复命令；
3. 所有目标副作用都在 Scope、Approval、预算和 Run 状态检查之后发生；
4. 网络服务和状态化 Web 两个本地场景都能走到可审查报告；
5. Evidence、Negative Result、Finding 和未验证假设不会混淆；
6. 暂停、恢复、取消、失败和重启后，Run 身份、专业事实和 Stop Proof 可重建；
7. 专业用户能加入一项本地 Skill，并完成试用、选择、复盘、禁用和回滚；
8. 默认文档、CLI 和启动路径只要求用户理解 Pentest 主线；
9. migration、Backup/Restore、安全边界和受影响回归通过；
10. 已知限制明确记录，不把模型文本当作已执行或已验证事实。

V1 的最小用户路径：

```text
onboard
→ doctor
→ model configure/default
→ pentest start/status
→ approval
→ resume/stop
→ report
→ capability verify/select/disable/rollback
```

---

## 5. 必须复用的权威事实

| 需求 | 权威事实 | 禁止的平行实现 |
| --- | --- | --- |
| Run 生命周期 | Run、Run Event、Workflow signal | Pentest Job 状态机 |
| 授权与范围 | Engagement、Admission、ScopeGuard | Prompt 内授权或新 Scope 表 |
| 模型/工具用量 | Session、Cycle、ToolCallIntent | 内存计数器 |
| Token 用量 | ContextCompilation actual usage | 新 Token Ledger |
| 目标交互 | ToolCallIntent claim、Target HTTP Request | Target Interaction 表 |
| 资产面 | Admission、Target HTTP、认可 Evidence 的投影 | Attack Surface 数据库 |
| 专业推理 | Task Graph、Reasoning Graph | Pentest Planner/Graph |
| 结果 | Evidence、Negative Result、Finding、Closure、Report | 从模型最终回答倒推 |
| 工具与方法 | Capability、Version、Selection、Pack Lock、Progressive Skill | 第二套插件中心 |
| 停止 | Run pause/cancel、Safety Stop、Stop Proof | Budget 停止服务 |

最小生产调用链：

```text
CLI/API
→ Application Service
→ RunKind / Scope / Approval / Budget
→ Runtime / Tool execution
→ Artifact / Traffic / Evidence / Reasoning
→ Finding / Closure / Report
```

任何副作用绕过这条链，都应修正共享入口，不得用 Prompt 约定补洞。

---

## 6. 唯一开发路线

| 阶段 | 状态 | 用户结果 |
| --- | --- | --- |
| A. 剩余 Pentest 预算收口 | completed | 所有 Admission 预算具有明确执行语义和硬停止 |
| B. 网络服务专业闭环 | completed | 一个真实服务从枚举走到证据化结论、Closure 和 Report |
| C. 状态化 Web 与报告 | in progress；C3 当前施工 | 一个身份/授权场景走到证据化结论、Closure 和 Report |
| D. 用户驱动能力成长 | pending | 一项专业方法可添加、选择、复盘、禁用和回滚 |
| E. 默认产品面收缩与发布 | pending | Pentest-first 产品可安装、可理解、可回归、可发布 |

除安全修复、数据兼容和当前用户阻断外，不得跳过阶段。

阶段 A 和 B 只保留回归合同，不再重复描述或扩建。其实现和测试事实以实施账本为准。

---

## 7. 阶段 A-B：已完成合同

### 7.1 阶段 A 回归合同

- Model、Token、Duration 在 Provider 副作用前检查；
- Tool、Duration 在共享 execution claim 前检查；
- Tool Proposal、等待批准和 Scope 拒绝不计预算；
- 总量耗尽统一 Pause、Safety Stop 和 Stop Proof；
- 并发容量不足可重试，不错误暂停 Run；
- status、门禁和重启恢复读取同一持久事实。

不得新增 Budget 表、计费系统、Budget Worker 或第二套停止服务。

### 7.2 阶段 B 回归合同

生产路径已经完成：

```text
Pentest Admission
→ service-enumeration
→ 注册工具 Execution
→ Artifact
→ Evidence
→ Observation / Hypothesis
→ Target HTTP 最小验证
→ Draft Finding / Negative Result
→ Closure / JSON Report v2
```

必须继续保证：

- scanner guess、banner 和模型猜测不能直接成为 Confirmed Finding；
- Tool failure、Scope rejection、Approval rejection 和 Negative Result 语义分离；
- Target HTTP 原始体和 Secret 不进入通用 Artifact/Event/Report 读取面；
- Report 从持久事实重建，而不是依赖最终模型文本；
- 重放不重复创建 Finding；
- 暂停、恢复、预算停止和 Control Plane 重建可解释。

---

## 8. 阶段 C：状态化 Web 专业闭环

阶段 C 只增加身份、会话、对象授权差异和报告表达，不建设通用 Web Scanner。

### 8.1 C1：最小身份/对象授权靶场（completed）

C1 已完成：

- 可复位随机 localhost 靶场；
- Alice/Bob、两个对象、登录和所有权正常访问；
- 确定性跨对象分支；
- 未批准和越界请求零网络副作用；
- 第六次目标交互预算耗尽并暂停 Run；
- Event、Traffic、Attack Surface 和 Artifact 标题不泄露测试密码或 Cookie；
- 未创建 Finding、Report 或新生产基础设施。

实现提交：`d73a0c86`。

### 8.2 C2：身份、状态与最小验证（completed）

C2 只允许 Agent 持久化 Credential Reference，真实 Header、Body 和 Cookie 值只在 Runner 发送前解析。

最小权限模型：

- 引用必须由当前 Session 固定的 Technique `CapabilityPermission.credential_references` 声明；
- Selection 不存在、损坏、漂移、未激活或跨 Run 时失败关闭；
- Agent 和 Control Plane 不持久化明文 Secret；
- 不建设动态 Cookie Vault、身份服务或会话数据库；
- 真实服务若必须处理动态浏览器会话，等当前协议级闭环证明不足后再准入。

#### C2 已满足的完成门

C2 必须通过完整生产链：

```text
RuntimeControlToolService
→ DeferredExecutionDispatcher
→ TargetHttpApplicationService
→ RunnerTargetHttpClient
```

已验收：

1. 注册一个仅用于测试的 Technique，并声明 Alice/Bob 登录和 Session 引用；
2. Pentest 显式选择 `web-request-analysis` Pack 和该 Technique；
3. ToolCallIntent、Approval、Event、Traffic、Artifact 标题和错误中只出现引用名，不出现密码或 Cookie 值；
4. 通过 Runtime 完成 Alice/Bob 登录和各自对象基线；
5. Alice 只改变对象标识访问 Bob 对象；
6. 正常响应和跨对象响应分别登记受保护 HTTP Evidence；
7. 创建 Observation、明确前置条件和正负判据的 Hypothesis、一次结构化 Attempt；
8. Control Plane 重建后可恢复 Capability Selection、Traffic、Evidence 和 Reasoning；
9. 未选中、损坏、不存在或未授权引用在网络副作用前拒绝；
10. 不创建 Finding、最终验证链或 Report，不新增表、migration、Browser、Crawler、Fuzzer、Scanner、Worker 或 UI。

### 8.3 C3：结论、Closure 与 Report（当前唯一实现切片）

C3 的目标不是增加一个 Attack Chain Domain，而是关闭现有事实链：

```text
身份前置条件
→ 所有权正常基线 Evidence
→ 单变量跨对象 Attempt
→ 差异 Evidence
→ Vulnerability Candidate
→ Draft Finding 或精确 Negative Result
→ Closure
→ Report
```

如果专业报告需要显示“攻击链”，只能从现有 Reasoning Node/Edge、Evidence 和 Finding 做确定性投影。禁止新增 Attack Chain 表、Repository、Planner 或第二套 Graph。

C3 完成门：

- 漏洞分支最多创建 Draft Finding，不自动 Confirmed；
- 无漏洞、登录失败、Scope 阻断、Approval 拒绝、预算耗尽和工具错误分别表达；
- 每个成立步骤引用 Evidence，每个假设步骤明确未验证；
- JSON/Markdown Report 能重建身份前置、动作、结果、停止原因和未测试范围；
- Secret 不进入事件、URL、日志、Artifact 标题或报告；
- 暂停、恢复、取消和重建保持 Run 身份、事实与 Stop Proof；
- 复用 ReportApplicationService，不复制报告业务逻辑。

---

## 9. 阶段 D：兑现“越用越好用”

### 9.1 最小成长闭环

V1 只证明一种扩展：Operator Skill。

```text
用户放入本地 Skill
→ verify/doctor 静态检查
→ Capability Version / Digest / Provenance
→ 测试 Run 显式 Selection
→ 查看脱敏复盘
→ 用户批准启用
→ 新 Run 固定版本
→ 禁用 / 回滚
```

首个 Skill 应选择一个现有 Tool 就能执行的真实渗透方法，例如“基于服务特征选择最小验证步骤”。不要同时建设 Tool SDK、Technique Builder 和 Marketplace。

### 9.2 必须复用

- Progressive Skill loader；
- Capability Repository、Version、Digest 和 Provenance；
- Capability Selection、Pack Lock 和 Tool allowlist；
- Event、Attempt、Artifact、Evidence、Reasoning、Finding、Closure 和 Stop Proof；
- 已有 Doctor/verify 命令，缺口只补薄入口或文档。

### 9.3 复盘的最小实现

初版复盘只做确定性、脱敏导出，回答：

- 使用了哪个 Skill、版本和 Digest；
- 在什么目标条件和前置条件下使用；
- 调用了哪些 Tool；
- 产生了哪些 Evidence、Finding、Negative Result 或失败；
- 是否被 Scope、Approval、预算或环境阻断；
- 用户决定继续试用、启用、禁用还是回滚。

不建设 Trajectory Store、Replay Lab、向量库、自动评分、自动改写 Skill 或自动批准。

### 9.4 阶段 D 完成门

1. 新 Skill 不扩大 Scope；
2. 新 Skill 不降低 Approval；
3. 新 Skill 不能调用 Selection/allowlist 外 Tool；
4. Digest 或版本漂移失败关闭；
5. 测试 Run 与正式 Run 的 Selection 可追溯；
6. 用户能看到来源、权限和变更；
7. 禁用后新 Run 不再选择；
8. 回滚后新 Run 使用旧版本；
9. 旧 Run 仍能解释当时固定版本；
10. 删除本地源目录不破坏旧 Run 的审计事实。

---

## 10. 阶段 E：默认产品面收缩与发布

### 10.1 先修用户路径

优先级固定为：

1. 修复 `Configured model not found`：显示配置的 model/provider/profile、实际候选和修复命令；
2. README、CLI help 和示例以 Pentest 为主；
3. Code Audit 和生态功能标记 frozen/experimental，不出现在默认新手路径；
4. `demo pentest` 若是静态转录，必须明确标记 simulated；
5. Nmap、Browser 或 Connector 缺失时给出降级路径，不阻塞基础 Pentest；
6. 让用户从 Onboard 到第一份报告只需要一条清晰路径。

### 10.2 消费者与成本审计

候选模块按下表记录一次：

| 模块 | Pentest 消费者 | 默认入口 | 启动/依赖成本 | 数据兼容 | 安全价值 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 待审计 | CLI/API/Worker/E2E | 默认/可选/无 | 实测 | 有/无 | 必需/可选/无 | 保留/按需/隔离/删除 |

优先审计：

- CLI 命令面和 API routes；
- Worker eager 初始化；
- Code Audit 专属 Runtime、snapshot、materializer 和 preflight；
- 未进入 B/C E2E 的 Connector、Pack、Demo 和 UI；
- 只被测试引用、没有生产入口的辅助层；
- 重复 Effect Policy、兼容 wrapper 和旧入口。

没有测到启动、内存、安装或维护问题前，不做通用 lazy-loader 重构。

### 10.3 发布检查

R1 至少覆盖：

- 全新环境 Onboard、Doctor 和模型配置诊断；
- 网络服务和状态化 Web 两个专业闭环；
- Skill 添加、选择、复盘、禁用和回滚；
- Scope、Approval、Credential、Redaction 和预算；
- 取消、失败、超时、重启和 Stop Proof；
- migration upgrade、受保护 downgrade 和 Backup/Restore；
- 默认 CLI/API、可选工具降级和已知限制；
- 全仓 Python 回归、相关前端/桌面 build 和发布检查。

评测只服务于回归、复现、发布和能力演进，不承担量化证明超过通用 Agent 的义务。

---

## 11. 开发准入规则

新增抽象、表、依赖、后台服务或兼容层前，必须回答：

1. 哪个当前 Pentest 用户流程无法由已有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会导致什么当前用户失败？
4. 能否改成现有 Service 的一个方法、一个确定性投影或一个薄 CLI？
5. 是否扩大权限、migration、恢复和测试面积？

答案不具体时，不实现。

有效进度必须至少产生一个结果：

- 用户完成一个新的 Pentest 操作；
- 一个真实副作用进入受控生产路径；
- 一个持久状态可跨进程恢复；
- 一条 Evidence/Negative Result/Finding 链可审查；
- 一个失败、停止或恢复场景被证明；
- 一项用户能力可安全复用或回滚；
- 默认产品面通过实测变简单。

只增加 Domain、Repository、Adapter、Graph、空 CLI、空服务或设计文档，不算开发完成。

---

## 12. 验证与 Git 纪律

所有 Agent 相关测试和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

### 12.1 分层验证

| Gate | 最小要求 |
| --- | --- |
| Slice | 目标测试、受影响回归、Ruff、必要 mypy/typecheck、`git diff --check` |
| User result | CLI/API、持久化、权限、失败、恢复和跨进程读取 |
| Milestone | 全仓 Python、相关前端/桌面 build、migration 和 release checks |
| Release | 两个靶场、能力成长、升级恢复、安全评审和已知限制 |

安全路径、migration、Runner ownership、Effect Policy 和 Stop Proof 不得因测试耗时跳过。

### 12.2 提交规则

- 一个实现提交只表达一个用户结果或一个安全结果；
- 实现提交与实施账本提交分开；
- Task 完成后更新 `FORMAL_AGENT_PROGRESS.md`；
- 不提交用户无关改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前检查 staged diff、`git diff --cached --check` 和实际测试命令；
- 每个切片保持可回滚。

---

## 13. Codex 执行协议

Codex 每轮只处理一个最小纵向切片。开始前写明：

```text
Current phase:
Pentest user outcome:
Existing production path to reuse:
Smallest implementation slice:
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
3. 查找已有事实、Service 和生产装配；
4. 写出一个用户结果和明确非目标；
5. 修改最少文件完成该结果；
6. 使用 conda `agent` 环境运行目标测试和受影响回归；
7. 通过后形成独立实现提交；
8. Task 完成后单独更新账本并提交；
9. 进入下一条未满足验收条件。

遇到以下情况立即停止扩张：

- 正在实现冻结或 Post-V1 范围；
- 新抽象没有当前生产消费者；
- 新表复制已有权威事实；
- 一个小功能要求修改大量无关模块；
- 只有单元测试，没有 CLI/API/Worker 生产路径；
- 为未来可能性阻塞当前 Pentest 闭环；
- 用 Prompt 修补确定性安全边界问题。

---

## 14. 当前唯一施工指令

从实现提交 `85decf8d` 继续，只关闭 C3 状态化 Web 专业结果：

1. 复用 C2 同一生产 E2E、受保护 Evidence、Reasoning Graph、Working Memory、Finding、Closure 和 Report Service；
2. 从跨对象 Observation 创建一个 `VULNERABILITY_CANDIDATE`，再通过现有 `create_finding` 幂等投影为 Draft Finding；不得自动 Confirmed；
3. Finding 必须引用跨对象响应 Artifact/Evidence，并明确 Alice 身份前置、只改变对象 ID 的复现动作、影响与修复建议；
4. 未授权引用、Approval/Scope 阻断、预算耗尽和工具错误继续保持失败语义，不得写成漏洞不存在；
5. 使用现有 Closure Verifier 完成 Run，并由现有 ReportApplicationService 生成 JSON 与 Markdown Report；
6. 报告必须从持久事实重建身份基线、Hypothesis、Attempt、差异 Evidence、Draft Finding、失败分支、未测试范围和 Stop/Closure 状态；
7. 所谓“验证链/攻击链”只允许是现有 Reasoning Node/Edge、Evidence、Attempt 和 Finding 的确定性报告投影；不新增表、Repository、Planner、Graph 或 Attack Chain Domain；
8. 重放不得重复创建 Finding 或 Report；Control Plane 重建后结果保持一致；
9. Secret、原始授权引用值、本地路径和受保护 HTTP 原始体不得进入 Event、Transcript、通用 Artifact 读取面或 Report；
10. 协议级闭环已足够，C3 不启用 Browser，不新增 Crawler、Fuzzer、Scanner、Pack、migration、Worker 或 UI；
11. 运行状态化 Web、Finding/Closure/Report、Target HTTP、完整 Control Plane、全仓 Ruff、scoped mypy 和 `git diff --check`；
12. 形成 C3 独立实现提交，再单独更新实施账本；C3 完成前不进入 D 或 E。

建议验证命令：

```bash
conda run --no-capture-output -n agent pytest -q tests/target_http
conda run --no-capture-output -n agent pytest -q tests/integration/api/test_pentest_stateful_web.py
conda run --no-capture-output -n agent pytest -q tests/integration/api/test_control_plane.py
conda run --no-capture-output -n agent ruff check .
git diff --check
```

---

## 15. 最终完成顺序

```text
[已完成] A：预算与停止语义
→ [已完成] B：网络服务专业闭环
→ [已完成] C2：安全身份引用与状态化 Web Evidence
→ [当前] C3：状态化 Web 结论、Closure 与 Report
→ [下一步] D：一项 Operator Skill 的成长闭环
→ [最后] E：默认产品面收缩、消费者审计与发布
```

RiftX 现在不需要更多架构，也不需要立即删除大量代码。最短路径是让已有架构连续产出专业结果，再把用户认可的方法安全沉淀为下一次可复用能力，最后根据真实消费者和实测成本收缩项目。
