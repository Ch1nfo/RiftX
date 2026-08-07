# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前唯一的产品收敛、代码优化与 Pentest-first 正式版完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 当前代码基线：`013d1e3a`；本次优化计划起点：`d63e6f84`
>
> 当前状态：Pentest-first R1 开发与发布门已完成；后续按真实用户需求进入 R2+
>
> 实施与测试事实：[`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 消费者与成本事实：[`docs/pentest-r1-consumer-audit.md`](docs/pentest-r1-consumer-audit.md)
>
> R1 发布事实：[`docs/pentest-r1-release-check.md`](docs/pentest-r1-release-check.md)
>
> 平台边界：[`ADR-0012`](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest Admission：[`ADR-0013`](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 0. 一页执行结论

RiftX 已经有足够厚的 Agent、Runtime、安全、持久化和专业事实底座。当前问题不是“能力太少”，而是已有能力没有被收敛成一条专业用户愿意持续使用的 Pentest 主路径。

项目确实存在过度开发，主要表现为：

- 生产代码约 18 万行、424 个 Python 文件、111 个 API 路由和 51 个 migration；
- Capability、Code Audit、Evaluation、Web、Browser、MCP、Connector 等横向能力同时存在；
- CLI 默认展示通用 Run、Memory、Terminal、Node、Audit 等大量平台概念；
- 历史账本保留了 Candidate、Promotion、Replay、Marketplace 等远期任务，容易被误当成当前待办；
- 能力成长曾被 Candidate、Promotion、Evaluation 等远期设想包围，最小 Operator Skill 闭环直到阶段 D 才完成。

但现在不应进行大规模删库或重写。安全边界、migration、恢复路径和旧数据兼容都使“先删再说”风险很高，而且删代码本身不会让 Pentest 更好用。

正式版路径只包含三个交付包：

1. Operator Skill 的最小生命周期和新 Pentest 门禁（已完成）；
2. 复用现有 Report 完成一次人工复盘与版本迭代（已完成）；
3. 收缩默认产品面、验证干净环境和启动成本，并完成最终发布回归（已完成）。

Pentest-first R1 已完成，不再存在必须继续扩建的平台任务。后续默认进入维护模式：只修安全、数据兼容和真实用户阻断；Operator Tool、Technique、团队共享和 Code Audit 恢复必须满足第 9 节触发条件。

---

## 1. 产品目标与完成层级

### 1.1 唯一产品目标

RiftX 的目标是：

> 成为一个在专业人士手中好用、可控、可恢复，并且能通过持续加入 Skill、Tool、Technique 和实战方法而越来越顺手的授权渗透测试 Agent。

“超过直接使用 Codex、Claude Code、OpenCode”是长期追求，不是发布门槛，也不要求构造单一排行榜证明。RiftX 的优势应来自领域复利：

- 通用 Agent 提供模型、代码操作和通用推理；
- RiftX 提供 Scope、Approval、预算、持久任务、Evidence、Finding、Report 和停止证明；
- 专业用户把自己的方法固化为有版本、有来源、可禁用的能力；
- 新 Run 能安全复用，旧 Run 始终能解释当时使用了什么。

### 1.2 两个基础属性

**开箱即用的即战力**：

- 新用户完成 Onboard、Doctor 和模型配置后能启动基础 Pentest；
- 不安装额外 Scanner、Browser 或 Connector 也能完成基础场景；
- Scope、Approval、预算、运行状态、Evidence、Finding 和 Report 可见；
- 配置缺失时给出具体错误对象和修复动作；
- 网络服务与状态化 Web 至少各有一条可重复的专业闭环。

**专业用户可持续提高的上限**：

- 用户可以加入自己的 Skill，并在后续版本扩展 Operator Tool 与 Technique；
- 每项能力有 ID、Version、Digest、Source、Provenance 和最小权限；
- 新 Run 显式固定 Selection，文件漂移不能静默替换；
- 用户能启用、禁用和回滚，且旧 Run 不受新状态影响；
- Skill 不能扩大 Tool allowlist、绕过 Scope 或降低 Tool 自身 Approval；
- 用户根据真实 Run 的证据和失败修改方法，而不是让模型自动批准自己生成的能力。

### 1.3 两个“完成”定义

**Pentest-first R1** 是当前开发终点：产品可安装、可完成两类场景、可报告、可恢复，并完成一次 Operator Skill 成长闭环。

**完全体方向** 不是一次性里程碑：在 R1 稳定后，按真实需求逐步开放 Operator Tool、Technique、团队共享和更多专业场景。没有真实用户阻断时，不预建生态平台。

### 1.4 R1 明确非目标

以下内容不阻塞当前正式版：

- Code Audit 新功能、Detector、Pack 或新里程碑；
- Marketplace、在线 Registry、组织同步、多租户和远程集群；
- 常驻多 Agent 团队、新 Planner、新 Graph 或第二套 Runtime；
- Candidate/Promotion 自动流水线、自动生成、自动批准或自动启用 Skill；
- Trajectory Store、Replay Lab、向量记忆和自动评分平台；
- 更多 Official Pack、Scanner、Crawler、Fuzzer 和 Connector；
- 为证明超过通用 Agent 而建设综合评分系统；
- 与 Pentest 主路径无关的 UI 重做。

---

## 2. 当前实际进度

### 2.1 规模只说明复杂度，不说明产品成熟度

截至本次校准：

| 项目 | 当前规模 | 结论 |
| --- | ---: | --- |
| 生产 Python 文件 | 424 | 禁止继续随意加层和抽象 |
| 生产 Python 代码 | 约 18 万行 | 优先复用和收缩入口 |
| Python 测试文件 | 298 | 测试多不等于用户闭环完成 |
| API 路由 | 111 | 需要默认产品面和消费者审计 |
| Alembic migration | 51 | 历史不可重写，新增表必须极谨慎 |
| Official Pack | 22 | 不再以 Pack 数量作为进度 |

### 2.2 已经完成且不再扩建的能力

| 能力 | 当前事实 | 后续处置 |
| --- | --- | --- |
| Onboard / Doctor | 初始化、migration、配置诊断、Pack repair、Backup/Restore 已存在 | 只修可复现阻断 |
| Durable Runtime | Run、Temporal、Runner、Execution、暂停、恢复、取消和 Stop Proof 已存在 | 不建第二套 Runtime |
| Pentest Admission | 专用创建入口、Scope、Approval、预算、Capability Selection 和 Pack Lock 已存在 | 普通 Run 不得绕过 |
| 专业事实 | Task、Artifact、Traffic、Evidence、Reasoning、Negative Result、Finding、Closure、Report 已存在 | 作为唯一事实链 |
| 网络服务闭环 | 枚举、Artifact、Evidence、Hypothesis、验证、结论、Closure、JSON Report 已完成 | 阶段 B completed |
| 状态化 Web 闭环 | Alice/Bob、Credential Reference、跨对象 Attempt、Finding、Closure、JSON/Markdown Report 已完成 | 阶段 C completed |
| 能力底座 | Capability Version、Selection、Progressive Skill 与 Operator 生命周期已存在 | 阶段 D completed；只做回归 |
| 报告投影 | JSON/Markdown 已显示 Selection、Allowlist、Execution、Evidence、Attempt、Finding 和停止事实 | 阶段 D completed；只做回归 |

阶段 C3 的实现提交为 `a8b29a4c`，已验证状态化 Web E2E、Report、Temporal、Target HTTP、Runtime Control、Worker、Control Plane、文档合同、Ruff、scoped mypy 和 `git diff --check`。

### 2.3 当前真正缺口

Pentest-first R1 当前没有未完成的代码缺口。E4 已在同一候选代码上完成 wheel、干净 Onboard/Doctor、Control Plane、两个 Pentest 场景、Operator Skill、安全失败关闭、恢复、migration、Backup/Restore、全仓 Python、Web 和文档 Gate。

仓库外仍可由产品所有者决定版本号、Git tag、远程推送和制品发布；这些发布动作不属于本地开发完成条件。当前包仍明确标记为 `2.0.0-alpha.0`，避免在未获授权时擅自宣告公开 GA。

### 2.4 对“过度开发”的准确判断

**已经过度的部分**：产品面、历史计划范围、Capability 远期模型、默认暴露概念和文档数量。

**尚不能直接判定为应删除的部分**：Runtime、安全边界、Code Audit 持久化、Capability 表、Browser、MCP、Connector 和前端页面。它们可能有迁移、兼容、安全或高级用户消费者。

因此优化顺序固定为：

```text
[已完成] 冻结新增
→ [已完成] 收缩默认入口
→ [已完成] 测量真实成本与审计消费者
→ [已完成] 对非当前命令运行时做局部惰性导入
→ [已完成] 执行 R1 发布门
→ [未来按证据] 删除同时满足三项准入条件的代码
```

---

## 3. 保留、冻结、隐藏与删除

### 3.1 必须保留

- Scope、Approval、Credential Reference、Redaction 和 Effect Policy；
- Run、Execution、Runner ownership、暂停、恢复、取消和 Stop Proof；
- Artifact、Traffic、Evidence、Reasoning、Finding、Closure 和 Report；
- Capability Version、Digest、Provenance、Selection 和 Pack Lock；
- migration 历史、Backup/Restore 和旧数据兼容读取；
- 当前 Pentest CLI/API/Worker/E2E 直接使用的 Tool、Target HTTP 和模型路径。

这些能力看起来重，但承担安全、恢复或审计责任，不能以 YAGNI 为由删除。

### 3.2 立即冻结

R1 前不再开发：

- Code Audit 新能力；
- Capability Candidate、Promotion、Evaluation 自动流程；
- Marketplace、Gateway、多租户、组织同步和远程 Registry；
- 新 Agent 角色、Planner、Graph、Memory 类型或 Runtime；
- 新资产数据库、向量数据库、Budget Service 或后台 Worker；
- 新 Official Pack、通用 Scanner、Crawler、Fuzzer 和 Connector；
- 没有当前生产消费者的 Domain、Repository、Adapter 和兼容层。

冻结表示不继续完成，不表示立刻删除历史表或兼容代码。

### 3.3 从默认产品面隐藏，但先保留代码

| 模块 | R1 处置 | 原因 |
| --- | --- | --- |
| `audit` 与 Code Audit 页面 | 标记 frozen/experimental，不进入 Quickstart | 当前主线是 Pentest |
| General Run、Memory、Terminal、Node、Execution | 放入 Advanced 文档 | 高级能力不是新手路径 |
| Capability Candidate/Promotion/Evaluation | 无默认入口，不继续接线 | 当前 Skill 生命周期不需要 |
| Browser、MCP、Connector、外部 Scanner | 可选能力，缺失时降级 | 不应阻塞基础 Pentest |
| Evaluation Harness | 仅开发与回归使用 | 不是用户产品面 |
| `demo code-audit` | 明确离线/冻结属性 | 避免误解为当前主产品 |

### 3.4 删除准入门

生产代码只有同时满足以下条件才允许删除：

1. 没有 Pentest 主路径消费者；
2. 没有默认或 Advanced CLI/API/Web/Worker 消费者；
3. 不是 migration、Backup/Restore 或旧数据读取所需；
4. 不承担 Scope、Approval、Credential、Evidence、Provenance 或 Stop Proof；
5. 没有受支持用户数据依赖；
6. 禁用或替代路径已有说明；
7. 引用审计、目标测试、migration 回归和 Pentest E2E 全部通过。

Migration 历史永不删除或重写。优先删除不可达 wrapper、重复装配、废弃入口和无消费者适配层，最后才考虑 Domain 或持久化模型。

---

## 4. Pentest-first R1 完成定义

R1 只要求以下用户结果连续成立：

1. 干净环境可以完成 Onboard、Doctor、模型配置和第一个授权 Pentest；
2. 配置错误能指出对象、候选和修复动作，无法复现的外部错误不进入 RiftX 开发计划；
3. 所有目标副作用都在 Scope、Approval、预算和 Run 状态检查之后发生；
4. 网络服务和状态化 Web 两个本地场景都能生成可审查报告；
5. Evidence、Negative Result、Finding、执行失败和未验证假设不会混淆；
6. 暂停、恢复、取消、失败和重启后，Run 身份、专业事实与 Stop Proof 可重建；
7. 专业用户能加入一项 Operator Skill，并完成验证、注册、启用、使用、禁用和回滚；
8. 默认文档与 CLI 只要求用户先理解 Pentest 主线；
9. migration、Backup/Restore、安全边界和受影响回归通过；
10. 已知限制明确记录，不把模型文本当作已执行或已验证事实。

最小用户路径：

```text
riftx onboard
→ riftx doctor
→ riftx model configure/default
→ riftx pentest start/status
→ riftx approvals / approve / reject
→ riftx pentest resume/stop
→ riftx report generate/show
→ riftx skills validate/register/activate/list/disable/rollback
```

R1 不以“功能数量最多”或“量化超过通用 Agent”为完成条件。

---

## 5. 必须复用的权威事实

| 需求 | 唯一权威事实 | 禁止的平行实现 |
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

任何副作用绕过这条链，应修正共享入口，不能用 Prompt 约定补洞。

---

## 6. 唯一开发路线

| 阶段 | 状态 | 用户结果 |
| --- | --- | --- |
| A. Pentest 预算与停止 | completed | Admission 预算有明确执行语义和硬停止 |
| B. 网络服务专业闭环 | completed | 真实服务从枚举走到证据化结论、Closure 和 Report |
| C. 状态化 Web 与报告 | completed | 身份/授权场景走到证据化结论、Closure 和 Report |
| D. 用户驱动能力成长 | completed | Operator Skill 可验证、启用、使用、复盘、禁用和回滚 |
| E. 默认产品面收缩与发布 | completed | Pentest-first 产品可安装、可理解、可回归、可发布 |

阶段 A、B、C、D 只保留回归合同，不再扩建。除安全修复、数据兼容和真实用户阻断外，不得跳过阶段。

### 6.1 阶段 A-C 的回归合同

- Model、Token、Tool、Duration 和 Target Interaction 在真实副作用前检查；
- Proposal、等待批准和 Scope 拒绝不计成真实执行；
- 预算耗尽统一产生 Pause、Safety Stop 和 Stop Proof；
- scanner guess、banner 和模型猜测不能直接成为 Confirmed Finding；
- Tool failure、Scope rejection、Approval rejection 和 Negative Result 语义分离；
- Secret 和受保护 HTTP 原始体不进入通用 Event、Artifact 标题和 Report；
- Report 从持久事实重建，重放不重复创建 Finding；
- Control Plane 重建后仍能解释 Selection、Traffic、Evidence、Reasoning 和结果。

---

## 7. 阶段 D：最小能力成长闭环

### 7.1 正确的 R1 用户流程

当前架构不支持“未启用 Skill 直接进入测试 Pentest”，也不需要为此增加 Candidate/Promotion 流水线。R1 流程固定为：

```text
用户创建或修改本地 Operator Skill
→ validate 静态验证
→ register 为 approved Capability Version
→ 用户显式 activate
→ 在授权本地/测试目标运行 Pentest
→ 从现有 Report 查看 Selection、Attempt、Evidence、Finding 和失败
→ 保持 active，或 disable
→ 修改内容并提升版本后重新注册
→ 需要回退时先恢复旧源码，再 rollback 状态
```

这是人工可控的“越用越好用”。R1 不自动生成、不自动改写、不自动批准 Skill。

### 7.2 D1 的边界（completed）

D1 只关闭一个真实漏洞：显式 Operator Skill 当前可从 Progressive Skill Registry 直接进入 Pentest，而不要求 active Capability Version。

必须复用：

- `ProgressiveSkillRegistry.validate()` 和现有 Skill digest；
- `CapabilityVersion`、`register_version()`、`list_versions()`、`list_active_versions()` 与 `set_version_status()`；
- `PentestCapabilityResolver`；
- Session Capability Selection 与完整 Skill Document/Reference 快照；
- 现有本地 SQLite、Doctor 和 CLI 装配。

D1 禁止新增：

- 表、migration 和新的包存储；
- Candidate、Promotion、Evaluation 或 Review 工作流；
- Marketplace、在线 Registry、签名服务和 UI；
- 新 Skill Parser、Skill SDK 或第二套权限模型；
- 自动批准、自动启用、自动回滚源码。

### 7.3 Operator Skill Capability Version 的最小映射

Capability Version 只承担“这个文件包是否允许被新 Run 选择”的身份与安全门禁，不保存 Skill 全文。

最小映射：

- `capability_id`：目录 Skill ID；
- `version`：Skill front matter version，必须是语义版本；
- `kind`：`skill`；
- `title` / `description`：Skill 文档元数据；
- `domains`：固定为 `pentest`、`operator-skill`；
- `dependencies`：留空，不能从 `preferred_tools` 推导 Tool 依赖；
- `provenance.source`：`operator`；
- `provenance.source_reference`：稳定的 `operator://skills/<id>/<version>`，不写绝对本地路径；
- `provenance.source_digest`：Progressive Skill 包 digest；
- `trust_tier`：`local`；
- `permission`：固定为 `target_interaction + always approval + requires_scope=true + credential_references=[]`；
- `evidence_contract`：固定为人工复核合同，不创造新的 Evidence 类型；
- `input_schema` / `output_schema`：存在 Skill schema 时复用，否则使用空对象；
- 完整 `SkillDocument`、`Reference` 和可选 schema 只进入新 Run 的 Session Selection 快照。

不得把 Skill 全文塞进 Capability Manifest。否则会重复持久化内容、污染 Capability 语义，并扩大 Official Pack digest 与迁移影响面。

固定权限是保守的准入元数据，不表示 Skill 可以执行目标副作用。Operator Skill 的 `preferred_tools` 和 `required_capabilities` 只用于选择提示，不能把 Tool 加入 Run allowlist。Tool 是否可用、是否需审批和是否允许目标交互，继续由 Tool Definition、Selection、ScopeGuard 和 Effect Policy 决定。

### 7.4 CLI 最小命令

只增加一个薄 `skills` 命令组：

```text
riftx skills validate [skill-id]
riftx skills register <skill-id>
riftx skills activate <skill-id> <version>
riftx skills list [skill-id]
riftx skills disable <skill-id> [version]
riftx skills rollback <skill-id> <version>
```

行为要求：

- `validate` 只读本地文件，不写数据库；
- `register` 创建 `approved` Version，同 ID/version/digest 幂等；
- 同 ID/version 但 digest 不同必须失败并要求提升版本号；
- `activate` 只接受当前本地源与注册 Version 的 ID/version/digest/source 完全匹配；
- 同一 Skill 已有另一个 active Version 时，普通 `activate` 失败并要求用户先显式 disable；
- `disable` 只阻止新 Run，旧 Run 继续读取已持久化 Selection 快照；
- `list` 同时显示本地文件状态与数据库状态，明确 missing、drifted、approved、active、disabled；
- 所有错误给出下一条可执行修复命令。

### 7.5 回滚的真实语义

R1 不保存 Operator Skill 历史源码包，因此 rollback 不是包管理器：

1. 用户先从 Git、备份或自己的 Skill 仓库恢复目标版本源码；
2. `skills rollback` 验证当前文件的 ID/version/digest 与已注册旧 Version 一致；
3. 在全部校验通过后禁用当前 active Version，再重新激活目标 Version；
4. 新 Run 使用恢复后的旧版本；旧 Run 保留各自原 Selection 快照。

若本地源码未恢复，rollback 必须失败并说明期望 digest。若状态切换中途失败，保持无 active Version 的安全状态并输出修复命令，不静默回到不确定状态。不得从历史 Run 快照自动覆盖用户文件，也不得为了 R1 新建包缓存、制品仓库或下载服务。

### 7.6 Pentest Admission 门禁

`PentestCapabilityResolver` 对显式 Operator Skill 必须：

1. 从 Progressive Skill Registry 读取当前包；
2. 在 active Skill Capability Versions 中查找同一 ID；
3. 校验 Version、`source=operator` 和 `provenance.source_digest`；
4. 任一不匹配在创建 Run 和网络副作用前失败关闭；
5. 将完整 Skill 快照与 Capability Selection 一起持久化；
6. 不把 Skill 的 preferred tool 自动加入 Tool allowlist。

未注册、仅 approved、disabled、源文件删除、同版本漂移、版本不匹配或持久状态损坏都必须拒绝新 Run。Official Pack Skill 保持现有 Pack Lock 与版本匹配逻辑。

### 7.7 D1 最小验收

至少覆盖：

- 注册幂等；
- 同版本内容漂移冲突；
- 未 activate 不能进入 Pentest；
- activate 后新 Run 固定 ID/version/digest/source 与完整文档快照；
- disable 后新 Run 拒绝；
- 恢复旧源码并 rollback 后，新 Run 使用旧版本；
- 已存在 Run 在 disable、升级、源码删除后仍可读取原 Selection；
- 本地源删除后新 Run 拒绝；
- Skill 不能扩展 Tool allowlist；
- Skill 不能降低 Tool 自身 Approval、绕过 Scope 或 Credential Reference 权限。

D1 已由实现提交 `6f59e278` 完成：新增本地 `skills` 生命周期命令、Operator 来源校验、保守 Capability Version、Pentest active/source digest 门禁、完整 Selection 快照回归，以及源码删除后的旧 Run 可解释性；未新增表、migration、包缓存、Candidate/Promotion、UI 或自动学习流程。

### 7.8 D2：复盘与人工改进（completed）

现有 Report 已包含 Capability Selection、Allowlist、Attempt、Evidence、Finding、Closure 和停止信息。D2 首先验证这些事实是否足以回答：

- 使用了哪个 Skill、版本、Digest 和来源；
- 调用了哪些 Tool；
- 产生了什么 Evidence、Finding、Negative Result 或执行失败；
- 是否被 Scope、Approval、预算、Credential 或环境阻断；
- 哪一步方法需要修改。

若现有 JSON/Markdown Report 已能回答，不新增 Review Domain、表或命令，只补文档和 E2E。若确有字段缺失，只向 `ReportApplicationService` 增加确定性、脱敏投影。

D2 的完成结果是：用户根据一次真实 Run 修改 Skill、提升版本、重新注册并在下一次 Run 使用新版本。自动评分、自动改写、Replay Lab 和 Trajectory Store 全部延期。

D2 已由实现提交 `a929fdb4` 完成：JSON 保持原有完整结构化事实，Markdown 增加确定性、脱敏的 Capability Selection、Allowlist、Stop、Execution 和 Evidence 投影；纵向 E2E 证明旧 Run 与旧 Report 在 v2 启用后仍固定 v1，下一 Run 和 Report 固定 v2；专业用户流程见 [`docs/operator-skill-lifecycle.md`](docs/operator-skill-lifecycle.md)。未新增 Review Domain、表、migration、命令、自动评分或自动改写。

## 8. 阶段 E：产品面收缩与发布

### 8.1 默认路径收缩

README、CLI help 和示例只把以下概念放在第一层：

```text
onboard / doctor / model
pentest / approvals / report
skills
```

以下命令继续保留，但进入 Advanced：

```text
run / execution / node / tools / terminal / artifact / memory
capabilities / packs / attach
```

`audit` 和 Code Audit 页面明确标记 frozen/experimental。不要为了隐藏命令删除其实现，也不要在 R1 前重做整个 WebUI。

任何类似 `Configured model not found` 的问题必须先在 RiftX CLI/API 内复现并定位。若字符串和失败来自 Codex、编辑器或外部 Provider，不得把它当作 RiftX 功能缺口继续开发。

E1 已由实现提交 `fdfa06e5` 完成：README 中英版把 Onboard、Doctor、Pentest、Approval、Report 和 Operator Skill 放到首条真实 Quickstart；顶级 CLI help 使用现有 Typer 分组区分 Getting started、Service operation、Pentest workflow、Advanced 与 Experimental；通用 Run 和平台命令仍可调用，Code Audit 明确 frozen/experimental，Demo 明确 simulated/sanitized。未删除命令、修改 API、重做 WebUI 或新增导航框架。

### 8.2 安装和降级体验

- 干净 XDG 环境完成一次真实 Onboard；
- Doctor 区分 ready、degraded、failed，并给出修复命令；
- 缺少 Nmap、Nuclei、Browser、MCP 或 Connector 时，基础 Pentest 仍可启动；
- 模型配置错误显示 profile/provider/model 和实际候选；
- 离线 Demo 明确标记 simulated/transcript，不冒充真实 Pentest；
- Quickstart 从安装到第一份报告只有一条路径。

E2 已由实现提交 `8ea90890` 完成：新增隔离 XDG、空可选工具 PATH 的纵向 E2E，真实运行 Onboard、Doctor、生产 Control Plane 装配和 Pentest Admission，并验证重复 Onboard 不覆盖配置。审计同时发现并修复 Onboard 未写入必需 `security.trust_profile` 的真实启动阻断；当前生成配置固定为唯一支持的 `local_single_operator`。缺少 Nmap、Nuclei、Browser、MCP、LSP 和 Scanner 时 Doctor 明确 degraded 而不失败；Temporal 缺失仍返回显式 `temporal_unavailable`，已持久 Admission 可按原请求重试。

### 8.3 消费者与成本审计

E3 已由实现提交 `2975e818` 完成，完整证据见 [`docs/pentest-r1-consumer-audit.md`](docs/pentest-r1-consumer-audit.md)。

| 模块 | 事实结论 | R1 处置 |
| --- | --- | --- |
| Code Audit runtime / snapshot / materializer | API、Worker、Snapshot、migration 和旧数据消费者存在 | 冻结兼容 |
| Browser / MCP / Connector | 状态化 Web、Worker Tool、API、停止与持久化消费者存在 | 按需初始化 |
| Candidate / Promotion / Evaluation | 无 R1 在线入口，但有 Repository、Schema、migration 和回归消费者 | 冻结兼容 |
| General Agent / Memory / Terminal | Agent Runtime 或 Advanced 用户与恢复路径直接使用 | Advanced |
| Demo / 前端非主线路由 | 非主路径；Demo 可离线演示，前端条件挂载 | 按需初始化 |
| 重复 wrapper / 旧入口 | 均存在部署或 CLI/API 兼容责任 | 保留 |

本轮测得 `riftx --help` 7 次冷启动中位数为 2.3360 秒。把 API、Runner、Temporal Worker、Demo 和 Capability Management 从 CLI 顶层移到实际命令后，中位数降至 1.7278 秒，约降低 26%；防回退测试验证导入 CLI 时这些运行时不在 `sys.modules`。

E3 没有发现满足“零消费者、无兼容责任、有测量收益”的删除候选，因此没有删除模块、表或 migration，也没有建设通用 lazy-import 框架。Control Plane 首次装配中位数约 0.4821 秒，当前不足以支持重写装配器或拆包。

### 8.4 R1 发布门

E4 是 R1 前最后阶段，只做现有能力的同版本发布证明。不得借发布门新增产品能力。

E4 已完成，完整环境、命令、结果、阻断修复和已知限制见 [`docs/pentest-r1-release-check.md`](docs/pentest-r1-release-check.md)。首次全仓运行发现 Artifact Service 两项受保护 Evidence 读取方法未进入 RunKind 公开异步方法分类；`013d1e3a` 将其登记为只读入口，107 项聚焦回归通过。最终全仓结果为 `5383 passed, 5 skipped`，Ruff、scoped mypy、Web 270 项测试与生产 build、wheel 隔离安装和文档合同全部通过。

#### E4.1 分发与干净启动

- 从当前提交构建 wheel，不依赖工作树源码隐式可见；
- 在临时目录安装并运行 `riftx --help`、`riftx onboard` 和 `riftx doctor`；
- 验证重复 Onboard 不覆盖用户文件，缺少可选 Tool 时保持 degraded；
- 启动 Control Plane 后检查 `/healthz`，并证明 Temporal 缺失错误仍可恢复。

#### E4.2 三条纵向用户闭环

必须在同一候选提交上连续通过：

1. 网络服务：Admission → Scope/Approval/Budget → Execution → Artifact/Evidence → Draft Finding/Negative Result → Closure → Report；
2. 状态化 Web：Alice/Bob 基线 → Credential Reference → 跨对象验证 → Evidence → Draft Finding → Report → 重建读取；
3. Operator Skill：validate → register → activate → Pentest Selection → Report 复盘 → v2 → disable → 恢复源码 → rollback。

只复用现有 E2E。除非回归暴露真实缺口，不新增第三个靶场、Browser 场景、Scanner、评分器或学习流水线。

#### E4.3 安全、恢复与数据门

- Scope、Approval、Credential、Redaction、Effect Policy 和预算在副作用前失败关闭；
- 暂停、恢复、取消、超时、重启和 Stop Proof 可解释；
- migration 从受支持旧版本升级成功，受保护 downgrade 行为符合现有合同；
- Backup/Restore 后 Run、Selection、Evidence、Finding、Report 和停止事实仍可读取；
- Secret、Credential 值、受保护 HTTP 原始体和本地敏感路径不进入 Event、Report 或失败输出。

#### E4.4 工程质量与发布记录

- 全仓 Python 测试、Ruff 和必要 scoped mypy 通过；
- Web test/build 通过；Demo 和 Browser Extension 只在其当前发布范围要求时验证，不因非 R1 表面阻塞 Pentest；
- 文档链接、README Quickstart、CLI help 和 `git diff --check` 通过；
- 新增 `docs/pentest-r1-release-check.md`，只记录环境、提交、命令、结果、修复提交和已知限制，不建设发布平台。

发布前最终结果必须同时满足：

- 全新环境 Onboard、Doctor 和模型配置成功；
- 网络服务场景从 Admission 到 Report；
- 状态化 Web 场景从双身份基线到 Draft Finding 与 Report；
- Operator Skill 从 validate/register/activate 到使用、disable、源码恢复和 rollback；
- Scope、Approval、Credential、Redaction 和预算失败关闭；
- 暂停、恢复、取消、超时、重启和 Stop Proof 可解释；
- migration upgrade、受保护 downgrade 和 Backup/Restore 通过；
- 默认 CLI/API、可选工具降级和已知限制已记录；
- 全仓 Python 测试、Ruff、必要 mypy、相关前端 build、文档链接和 `git diff --check` 通过。

某一项失败时，只修复该失败的共享根因并重跑受影响回归。外部 Provider、Codex 或编辑器无法在 RiftX 内复现的错误，记录为环境限制，不扩建 RiftX。

评测只用于 RiftX 自身回归、复现、发布和能力演进，不承担量化证明超过通用 Agent 的义务。

---

## 9. R1 之后如何兑现更高上限

R1 完成后按真实用户阻断逐层开放，不并行建设。

### 9.1 R2：Operator Tool

只有当用户方法无法由现有 Tool 完成时，才增加 Operator Tool 生命周期。复用 Capability Version、Tool Registry、Runner、Effect Policy、Scope 和 Approval；不得允许 Skill 直接拼接未注册 shell 命令。

完成结果：专业用户能加入一个本地 Tool，声明输入、输出、权限和可执行环境，并像 Skill 一样启用、禁用和回滚。

### 9.2 R3：Technique 与方法组合

只有多项 Skill/Tool 已稳定复用后，才开放 Operator Technique 或 Playbook。优先使用现有 Task Graph、Reasoning Graph 和 Capability Selection 组合，不建设第二套 Planner/Graph。

完成结果：用户能把多个已注册能力组合成一套可审查方法，并保留每一步 Evidence 与停止条件。

### 9.3 R4：团队共享

只有出现多个真实操作者、跨机器同步和来源验证需求后，才考虑签名包、组织源或 Registry。Marketplace、多租户和在线分发不是单用户产品的前置条件。

### 9.4 Code Audit 恢复条件

Code Audit 只有在 Pentest R1 稳定，且有明确用户任务、维护预算和消费者后才恢复。恢复时先复用现有 Audit 基础，不能重启一套新的 Agent 平台计划。

---

## 10. 开发准入与停止规则

新增抽象、表、依赖、后台服务或兼容层前，必须回答：

1. 哪个当前 Pentest 用户结果无法由已有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会造成什么当前失败？
4. 能否改成现有 Service 的一个方法、确定性投影或薄 CLI？
5. 是否扩大权限、migration、恢复和测试面积？

答案不具体时，不实现。

有效进度必须至少产生一个结果：

- 用户完成一个新的 Pentest 操作；
- 一个真实副作用进入受控生产路径；
- 一个持久状态可跨进程恢复；
- 一条 Evidence/Negative Result/Finding 链可审查；
- 一个失败、停止或恢复场景被证明；
- 一项用户能力可安全复用或回滚；
- 默认产品路径通过实测变简单。

只增加 Domain、Repository、Adapter、Graph、空 CLI、空服务或设计文档，不算开发完成。

出现以下情况立即停止扩张：

- 正在实现 Frozen 或 Post-R1 范围；
- 新抽象没有当前生产消费者；
- 新表复制已有权威事实；
- 一个小功能要求修改大量无关模块；
- 只有单元测试，没有 CLI/API/Worker 生产路径；
- 为未来可能性阻塞当前 Pentest 闭环；
- 用 Prompt 修补确定性安全边界。

---

## 11. Codex 执行协议

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

所有 Agent 相关测试和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

分层验证：

| Gate | 最小要求 |
| --- | --- |
| Slice | 目标测试、受影响回归、Ruff、必要 mypy/typecheck、`git diff --check` |
| User result | CLI/API、持久化、权限、失败、恢复和跨进程读取 |
| Milestone | 全仓 Python、相关前端 build、migration 和 release checks |
| Release | 两个靶场、能力成长、升级恢复、安全评审和已知限制 |

提交纪律：

- 一个实现提交只表达一个用户结果或安全结果；
- 实现提交与实施账本提交分开；
- 不提交用户无关改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前检查 staged diff、`git diff --cached --check` 和实际测试命令；
- 每个切片保持可回滚。

---

## 12. R1 完成后的唯一准入规则

R1 已完成，没有默认的下一项平台开发任务。后续只允许三类工作：

1. 可复现的安全、数据损坏、恢复或受支持兼容问题；
2. 真实专业 Pentest 用户被现有 Tool、Skill 或工作流明确阻断；
3. 产品所有者明确授权的版本号、tag、远程推送或制品发布动作。

遇到第 2 类需求时，从第 9 节最靠前且满足触发条件的阶段开始，每次仍只实现一个最小纵向用户结果。不得因为 R1 完成就自动启动 Operator Tool、Technique、团队共享、Code Audit、Marketplace 或大规模删除。

每次维护仍执行最小 Gate：

```bash
conda run --no-capture-output -n agent pytest -q <affected tests>
conda run --no-capture-output -n agent ruff check <affected paths>
conda run --no-capture-output -n agent mypy <affected source paths>
git diff --check
```

涉及 Runtime、安全、持久化、CLI 分发或 Web 时，必须补跑相应 E4 Gate；完整发布证据以 [`docs/pentest-r1-release-check.md`](docs/pentest-r1-release-check.md) 为基线。

---

## 13. 最终完成顺序

```text
[已完成] A：预算与停止语义
→ [已完成] B：网络服务专业闭环
→ [已完成] C：状态化 Web、Closure 与 Report
→ [已完成] D1：Operator Skill 生命周期与 Admission 门禁
→ [已完成] D2：复用现有 Report 完成人工复盘和版本迭代
→ [已完成] E1：默认产品入口与 Quickstart 单路径
→ [已完成] E2：干净 XDG Onboard 与可选工具降级
→ [已完成] E3：消费者、启动成本审计与局部惰性导入
→ [已完成] E4：Pentest R1 发布门
→ [按需] R2+：Operator Tool、Technique 与团队共享
```

RiftX 当前不需要更多架构，也不应立刻大规模删代码。Pentest-first R1 已完成；后续高上限来自专业用户持续沉淀方法，并在真实阻断出现时按需增加 Operator Tool 和 Technique，不来自一次性把所有平台设想全部开发完。
