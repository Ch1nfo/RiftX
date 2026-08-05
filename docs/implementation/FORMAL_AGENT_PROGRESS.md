# RiftX 正式版 Agent 开发实施账本

> 状态：active
>
> 启动日期：2026-08-05（Asia/Shanghai）
>
> 实现分支：`ch1nfo/riftx-3-code-audit`
>
> 计划输入基线：`e40af267`
>
> 正式版计划提交：`84c657e1`
>
> 权威计划：[RiftX 正式版开发文档](../../RiftX_正式版_开发文档.md)
>
> 总体架构：[ADR-0012](../architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)

## 1. 账本规则

- Task 状态只能是 `pending`、`in_progress`、`blocked` 或 `completed`。
- 只有实现、目标测试、关联回归和 `git diff --check` 全部通过后，Task 才能标记为 `completed`。
- 每个 Task 在下一个 Task 开始前必须形成独立本地 Git 提交。
- 当前提交不能记录自身 hash；下一次账本更新必须回填上一 Task 的实现提交。
- 每条测试证据记录实际命令和结果，不复制容易过期的累计测试数量。
- 设计偏离必须更新权威计划；改变核心边界时必须新增或 supersede ADR。
- 所有 Agent 相关测试与运行使用 conda `agent` 环境。
- 用户无关的工作树改动不得进入任务提交。
- 评测用于 RiftX 自身的回归、复现、发布检查和能力演进。

## 2. Current wave

- Stage：`S1 — 生产 Capability Plane`
- Current task：`CAP-102 — Browser/Web/Traffic Tool 闭环`
- Status：`completed`
- Completed predecessor：SEC-000，implementation commit `a15e8e94`。
- Completed predecessor：SEC-001，implementation commit `53161141`。
- Completed predecessor：CAP-001，domain/API commit `0fd20fda`，persistence commit `84481149`。
- Completed predecessor：CAP-100，implementation commit `bb1b3b03`。
- Active carry-over：CAP-101 保持 `in_progress`；隔离 Worktree 与受控 LSP 在建立对应 ownership/lifecycle 基础后继续。
- Product behavior：Primary Agent 已可通过生产 Runtime 使用 owner-bound managed ephemeral browser，逐次批准后执行匿名 Public Fetch、联合 Web Search、只引用 canonical Source 的 Web Research 和 Scope-bound Target HTTP Request，并可查询脱敏 HTTP Traffic、读取 Exchange 及其原文 Artifact；General 与 Code Audit Run 均可使用公网研究，其中 Code Audit 原文、规范化正文、Search Response 和 Research Packet 精确进入 `AUDIT_INTERNAL` Artifact；Scope/SSRF、Run 状态、Approval、Stop Proof、Artifact、Source 与 Transcript 继续复用既有权威服务。
- Current implementation commits：`69d54ab7`、`e8c047c6`、`c9a6394a`、`27fec108`、`e7fc3461`。
- Next delivery slice：开始 `CAP-103 — MCP 生产接入`。

## 3. 研究与实现基线

| 输入 | 基线 | 用途 |
| --- | --- | --- |
| RiftX | `e40af267` | 正式版计划开始前的产品代码基线 |
| 正式版计划 | `84c657e1` | S0-S8 权威开发计划 |
| LuaN1aoAgent | `51af327c29c2` | Task/Reasoning/Operation Graph、Planner/Executor/Observer |
| CyberStrikeAI | `f7ba7070ca74` | Tool Search、Progressive Skill、验证与负结果 |
| OpenAI Codex | `757c151a0e92` | 原生代码工具、Sandbox、Approval、Skill/Plugin/MCP |
| OpenCode | `4a57013cf8cb` | Provider、插件工具、LSP、Session Revert |
| OpenClaw | `26a58bcd92ba` | Gateway、Onboard、Doctor、插件生态 |
| Hermes Agent | `1be70d635488` | Trajectory、Agent-created Skill、Curator、Profile |
| Claude Code | 2026-08-05 前官方技术文档 | Skill、Hook、Memory、Subagent、Checkpoint、权限 |

研究基线只用于复核设计来源。实现必须遵守 RiftX 自身许可证、Provenance、安全边界和独立表达。

## 4. Evaluation 配置基线

SEC-001 之前不创建新的专业能力评分结论。当前只冻结每个 Evaluation Run 的必要记录：

| 类别 | 必须记录 |
| --- | --- |
| Build | RiftX commit、dirty state、schema version、平台、Python/Node 版本 |
| Model | provider、model、request mode、关键采样配置、credential reference |
| Capability | Tool、Skill、Technique、Pack ID/version/digest、selection source |
| Scenario | scenario/version、target snapshot、reset recipe、授权 scope |
| Budget | 时间、Token、工具调用、并发和目标交互限制 |
| Runtime | Control Plane、Worker、Runner、Browser、MCP/LSP/Scanner 可用性 |
| Output | trajectory、evidence、finding disposition、artifact、stop disposition |
| Review | 已知限制、失败类型、人工判断和可复现性说明 |

默认规则：

- Agent 评测命令通过 `conda run --no-capture-output -n agent ...` 执行；
- 开发集与未公开回归集隔离；
- 不同 Run 默认不共享 Operator/Organization/Engagement Memory；
- 评测只服务于 RiftX 自身的质量、安全、回归检查和能力演进；
- 允许定性复盘与定量指标并存，不强迫所有专业能力压缩成单一分数。

## 5. 数据迁移顺序

| 顺序 | 数据面 | 首个任务 | 开放条件 |
| --- | --- | --- | --- |
| 1 | Capability catalog、Version、Dependency、Permission、Pack、Candidate | CAP-001 | additive migration、旧 Tool/Skill 可读 |
| 2 | 旧 Tool/Skill compatibility import | CAP-001/CAP-100 | Provenance 明确，不自动提升为 Official |
| 3 | Run-scoped Tool/Skill/Pack selection | CAP-104 | 生产 Skill/Tool loader 已接通 |
| 4 | Task、Evidence、Reasoning 和 Projection | COG-200 至 COG-204 | 先影子写/兼容读，再成为 Agent 权威输入 |
| 5 | Professional Tool 和 Official Pack 状态 | CAP-101 至 PACK-302 | Selection、Evidence、Effect Policy 已完成 |
| 6 | Trajectory、Review、Replay、Promotion | LEARN-600 至 LEARN-604 | Candidate 与 Active Capability 物理分离 |
| 7 | Profile、SDK、签名、Registry、持续运行 | LEARN-605、ECO-800 至 ECO-802 | Backup、Restore、Rollback 和供应链检查可用 |

任何任务如果需要越过该顺序，必须先修改 ADR 和计划，不能通过临时 nullable 字段或 Prompt 约定绕过。

## 6. Milestone status

| Stage | Status | Exit condition |
| --- | --- | --- |
| S0 规格、基线与评测骨架 | completed | ADR/账本、Evaluation 骨架和 Capability Domain foundation 完成 |
| S1 生产 Capability Plane | in_progress | Capability 可持久加载；Code/Browser/Web/MCP 接入生产 Runtime |
| S2 认知运行时 | pending | Task/Evidence/Reasoning 持久化；Observer 和 Closure 工作 |
| S3 Official Packs 与开箱即用 | pending | Onboard/Doctor 可完成基础渗透和代码审计流程 |
| S4 代码审计完全体 | pending | 语义导航、Scanner、Evidence、Diff/Variant 和受控验证闭环 |
| S5 渗透测试完全体 | pending | Attack Surface、状态 Web、验证规划、Research、Attack Chain 闭环 |
| S6 学习飞轮 | pending | Trajectory 到 Candidate/Replay/Promotion/Curator 闭环 |
| S7 专业能力评测与回归保障 | pending | 专业案例、回归 Harness 与质量安全发布检查可用 |
| S8 Pack 生态与正式版运维 | pending | SDK、签名供应链、Gateway 和持续运维可用 |

## 7. Task status

| Task | Dependency | Status | Implementation commit |
| --- | --- | --- | --- |
| SEC-000 | none | completed | `a15e8e94` |
| SEC-001 | SEC-000 | completed | `53161141` |
| CAP-001 | SEC-000 | completed | `0fd20fda`, `84481149` |
| CAP-100 | CAP-001 | completed | `bb1b3b03` |
| CAP-101 | CAP-001 | in_progress | `73ba9900`, `80276a08`, `a83875d1`, `c6de9413`, `b7e4b969`, `cbc2a2e5`, `546f1466`, `08d746ec`, `203f6c1e` |
| CAP-102 | CAP-001 | completed | `69d54ab7`, `e8c047c6`, `c9a6394a`, `27fec108`, `e7fc3461` |
| CAP-103 | CAP-001 | pending | — |
| CAP-104 | CAP-100, CAP-103 | pending | — |
| COG-200 | CAP-104 | pending | — |
| COG-201 | COG-200 | pending | — |
| COG-202 | COG-201 | pending | — |
| COG-203 | COG-202 | pending | — |
| COG-204 | COG-203 | pending | — |
| COG-205 | COG-204 | pending | — |
| PACK-300 | CAP-102, CAP-104, COG-205 | pending | — |
| PACK-301 | CAP-101, CAP-104, COG-205 | pending | — |
| PACK-302 | PACK-300, PACK-301 | pending | — |
| AUD-400 | CAP-101, COG-202 | pending | — |
| AUD-401 | AUD-400 | pending | — |
| AUD-402 | AUD-400, AUD-401, COG-205, PACK-301 | pending | — |
| AUD-403 | COG-201, AUD-400, AUD-401 | pending | — |
| AUD-404 | AUD-400, AUD-403 | pending | — |
| AUD-405 | CAP-101, AUD-403 | pending | — |
| PEN-500 | CAP-102, COG-202 | pending | — |
| PEN-501 | CAP-102, PEN-500 | pending | — |
| PEN-502 | COG-203, PEN-500, PEN-501 | pending | — |
| PEN-503 | CAP-102, PEN-502 | pending | — |
| PEN-504 | COG-201, PEN-500, PEN-502 | pending | — |
| LEARN-600 | COG-205 | pending | — |
| LEARN-601 | LEARN-600 | pending | — |
| LEARN-602 | LEARN-601 | pending | — |
| LEARN-603 | SEC-001, LEARN-601, LEARN-602 | pending | — |
| LEARN-604 | CAP-001, LEARN-603 | pending | — |
| LEARN-605 | LEARN-604, PACK-302 | pending | — |
| EVAL-700 | SEC-001, AUD-403, AUD-404 | pending | — |
| EVAL-701 | SEC-001, PEN-504 | pending | — |
| EVAL-702 | EVAL-700, EVAL-701, LEARN-603 | pending | — |
| EVAL-703 | EVAL-702, PACK-302 | pending | — |
| ECO-800 | CAP-001, LEARN-604 | pending | — |
| ECO-801 | ECO-800 | pending | — |
| ECO-802 | LEARN-605, ECO-801 | pending | — |

## 8. Task records

### SEC-000：正式版 ADR 与实施账本

- Status：completed
- Started：2026-08-05
- Inputs：正式版计划提交 `357ed38e`、当前代码和既有 ADR/进度账本。
- Deliverables：
  - ADR-0012；
  - 本实施账本；
  - 正式计划内全部 Task 的显式依赖；
  - 文档链接、任务集合和依赖一致性测试。
- Product changes：无。
- Target checks：
  - `conda run --no-capture-output -n agent ruff check tests/docs/test_formal_agent_docs.py`：passed。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/docs/test_formal_agent_docs.py`：`4 passed`。
  - `git diff --check`：passed。
- Implementation commit：`a15e8e94`。

### SEC-001：Security Capability Evaluation 骨架

- Status：completed
- Started：2026-08-05
- Inputs：SEC-000、ADR-0012、现有 `src/riftx/evaluation/` 契约。
- Deliverables：
  - Scenario、Target、Reset、Budget、Run、Trajectory、Evidence Replay 和 Judge schema；
  - root-bound YAML loader、immutable fixture reset 和 canonical JSON；
  - 代码审计与静态 Web transcript 开发场景；
  - Memory isolation、预算、证据状态和版本/配置对照 Harness；
  - 目标和关联 Evaluation 回归测试。
- Safety boundary：Fixture 只能读取，测试不得导入目标源码、发起网络请求或执行外部工具。
- Checks：
  - `conda run --no-capture-output -n agent ruff check src/riftx/evaluation/__init__.py src/riftx/evaluation/security_agent tests/evaluation/security_agent`：passed。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/evaluation/security_agent`：`9 passed`。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/evaluation tests/docs/test_formal_agent_docs.py`：`99 passed`。
- Implementation commit：`53161141`。

### CAP-001：Capability Domain 与持久化

- Status：completed
- Started：2026-08-05
- Inputs：ADR-0012、SEC-001、现有 Skill/Tool/Memory/Persistence 结构。
- Delivery slices：
  1. 领域模型、Canonical Digest、生命周期和 API Schema；
  2. ORM、Repository、Alembic Migration 和兼容性/幂等测试；
  3. Candidate/Active 物理隔离、Pack Lock 和运行中版本保护验收。
- Product wiring：CAP-100 之前不把新 Registry 接入生产 Worker。
- Delivered：
  - 版本化 Capability、Candidate、Promotion、Evaluation、Pack、Install 和 Lock 领域/API 契约；
  - 12 张 Capability 表、SQLAlchemy Repository 和 Alembic revision `7f2c8a1d4e90`；
  - Candidate/Version 物理隔离、Canonical Digest、Provenance 和不可覆盖版本；
  - Pack 精确版本锁定，安装、禁用、回滚和锁释放幂等；
  - Run Session 活跃锁阻止版本禁用、弃用和归档；
  - Candidate 只能在候选、Promotion 均获批且 Evaluation 全部通过后原子晋升；
  - 跨多级 downgrade 在删除 Capability DDL 前先检查 Capability 及历史权威事实。
- Checks：
  - `conda run --no-capture-output -n agent ruff check ...`：passed。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/capabilities tests/unit/persistence/test_schema.py tests/integration/persistence/test_capability_repository.py tests/integration/persistence/test_capability_migration.py tests/docs/test_formal_agent_docs.py`：`35 passed`。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/persistence tests/integration/persistence`：`504 passed, 10 warnings`；警告为 Python 3.12 SQLite datetime adapter 弃用提示。
  - `git diff --check`：passed。
- Implementation commits：domain/API `0fd20fda`；persistence `84481149`。

### CAP-100：接通生产 Progressive Skill

- Status：completed
- Started：2026-08-05
- Inputs：CAP-001、现有 `ProgressiveSkillRegistry`、`ContextCompiler`、Runtime control tools 和 Subagent Delegation。
- Delivered：
  - 生产 Worker 创建并注入 `ProgressiveSkillContextManager`；
  - Primary Agent 获得 Skill search/list/load/reference/unload 工具；
  - Skill 选择按 Agent Session 持久化，锁定 ID/version/digest/source/reason；
  - Worker 重启恢复原选择，文件变更只标记 stale，不静默替换运行中快照；
  - Subagent 只能搜索和加载 Delegation Packet 显式允许的 Skill。
- Persistence：Alembic revision `9a4d6e2b7c11`，新增 `agent_skill_scopes` 和 `agent_skill_selections`。
- Manifest：记录 Skill ID、version、package digest、source、load reason、reference 状态和 stale 状态。
- Product boundary：Skill 文档只进入 Context；不因加载 Skill 扩大 Tool、Scope、Credential 或 Approval 权限。
- Checks：
  - `conda run --no-capture-output -n agent ruff check ...`：passed。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/persistence tests/integration/persistence`：`509 passed, 10 warnings`。
  - `conda run --no-capture-output -n agent python -m pytest -q tests/unit/skills tests/context tests/runtime tests/subagents tests/integration/agent tests/unit/temporal/test_worker_runtime.py tests/unit/test_runtime_config.py`：`365 passed`。
  - Subagent 恢复修复后关联回归：`340 passed`。
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4940 passed, 5 skipped, 11 warnings`。
  - `git diff --check`：passed。
- Implementation commit：`bb1b3b03`。

### CAP-101：原生代码工具

- Status：in_progress
- Started：2026-08-05
- Inputs：CAP-001、Code Audit Snapshot 边界、Artifact 限额读取、Runtime control tools 和现有 LSP/Scanner 模块。
- First delivery slice：
  - 已建立 Workspace/Snapshot owner-bound 代码读取服务；
  - 已将 `list_files`、`read_file`、`read_many_files`、`grep`、`glob` 接入生产 Worker、Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool；
  - General Run 从绝对 Workspace Root 开始逐级 `O_NOFOLLOW` 打开目录，读取期间用 FD 和文件指纹锚定对象；
  - Code Audit Run 不读取可变输出 Workspace，而是验证 `Run → AuditScan → SourceSnapshot → SnapshotStore` owner binding 后读取不可变源码；
  - 结果具有文件数、读取字节、扫描条目、扫描字节、匹配数、单行长度和最终 Runtime JSON 字节上限；
  - 拒绝绝对路径、非规范路径、`..`、Symlink 读取、FIFO/Socket/设备等特殊文件和跨 Run Root；
  - 二进制内容以 bounded base64 Preview 返回；所有代码工具保持只读，不调用 Shell、项目 Hook、构建、测试或安装脚本。
- First delivery implementation commit：`73ba9900`。
- Second delivery slice：
  - 已将 `git_status`、`git_diff`、`git_log` 接入生产 Worker、Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool；
  - Git 进程使用固定可执行路径、最小环境、`--no-optional-locks`、无 Pager、无凭据、无网络协议、禁用 Hook、fsmonitor、external diff、textconv 和签名验证；
  - Git 管理区必须是 Workspace 内的真实 `.git/` 目录；管理区 Symlink、特殊文件、alternates、grafts 和超限条目失败关闭；
  - Repository local config 出现 include、filter、credential、remote、submodule、external command 等外部行为时拒绝执行；
  - `git_status` 返回有界 Index/Worktree 状态，`git_diff` 支持工作区或暂存区 Preview，`git_log` 返回有界提交摘要并处理 unborn repository；
  - 每次命令前后验证 Workspace FD/path binding 和 Git 管理区元数据 Digest，命令不得刷新 Index 或改变管理区；
  - Code Audit Snapshot 不含可信 Git 管理区，本切片明确拒绝 Git 工具调用，不回退到可变 Audit 输出 Workspace。
- Checks：
  - `conda run --no-capture-output -n agent python -m pytest -q tests/code/test_workspace.py`：`9 passed`；
  - Agent/Runtime/Context/Subagent 关联回归：`337 passed`；
  - Snapshot View/Store/Reference/Mount 关联回归：`36 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4951 passed, 5 skipped, 11 warnings`；
  - 跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件；警告为既有 Python 3.12 SQLite datetime adapter 弃用提示。
- Second delivery checks：
  - `conda run --no-capture-output -n agent python -m pytest -q tests/code/test_git.py`：`10 passed`；
  - Agent/Runtime/Context/Subagent/Worker 关联回归：`357 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4963 passed, 5 skipped, 11 warnings`；
  - 全仓 Ruff、文档测试和 `git diff --check`：passed。
- Second delivery implementation commit：`80276a08`。
- Third delivery slice：
  - `read_file` 与 `read_many_files` 对超过 64 KiB 的文件返回 bounded Preview，并附带完整文件 `artifact_id`；小文件的主动分段读取不产生 Artifact；
  - General Run Artifact 继续使用既有 General-only 注册边界；Code Audit 新增独立的 `register_audit_content()`，不放宽原有 `register*()` 防线；
  - Code Audit Artifact 在读取 `Run → AuditScan` 原始 owner binding 后，以 `audit_id + run_id` 精确绑定并存为 `AUDIT_INTERNAL`；
  - Artifact 内容使用 Snapshot/Workspace 已验证的完整字节，标记为 `UNTRUSTED_SOURCE`，生产 Worker 已接入 Artifact Publisher；
  - `read_artifact` 支持同一 Audit Run 内继续分段读取 `AUDIT_INTERNAL`，并拒绝 `RESTRICTED_SENSITIVE`；
  - 新 Audit Artifact 写入入口已纳入 RunKind Effect Policy，限定为 Code Audit durable write。
- Third delivery checks：
  - Artifact/Code Workspace/Runtime control tool 单元回归：`68 passed`；
  - RunKind Effect Policy：`37 passed`；
  - Artifact API/Persistence/Application 集成回归：`24 passed`；
  - Agent/Temporal Worker 集成回归：`46 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4967 passed, 5 skipped, 11 warnings`；
  - 全仓 Ruff、文档测试和 `git diff --check`：passed。
- Third delivery implementation commit：`a83875d1`。
- Fourth delivery slice：
  - 已将 `symbol_search` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool；
  - General Run 与 Code Audit 均复用既有 owner-bound Workspace/Snapshot 读取链路，不读取 Audit 可变输出目录；
  - 无可信 LSP 时使用明确标记为 `builtin_static` 的安全降级索引：Python 使用标准库 AST，JavaScript/TypeScript、Go、Rust、Java、Kotlin、C/C++、C#、Swift、PHP、Ruby 与 Shell 使用有界声明提取；
  - 结果包含语言、符号类型、限定名、路径、行列、签名，以及扫描字节、跳过二进制/大文件/不支持文件、解析失败和截断状态；
  - 单文件、总扫描字节、目录条目、符号扫描数、返回结果、行长、行数、名称和限定名均有硬上限；
  - 本切片不启动 Language Server，不读取项目外配置，不执行项目 Hook、插件、构建、测试或安装脚本；受控 LSP 仍是后续高精度后端。
- Fourth delivery checks：
  - Code/Runtime/Tool Discovery 与 Tool Policy：`272 passed`；
  - Context/Subagent/Agent/Temporal Worker：`110 passed`；
  - RunKind Effect Policy 与文档约束：`41 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4976 passed, 5 skipped, 11 warnings`；
  - 全仓 Ruff、文档测试和 `git diff --check`：passed。
- Fourth delivery implementation commit：`c6de9413`。
- Fifth delivery slice：
  - 已将 `find_references` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool；
  - General Run 与 Code Audit 继续复用 owner-bound Workspace/Snapshot 读取链路和与 `symbol_search` 共用的有界语义扫描预算；
  - Python 使用标准库 `tokenize` 跳过注释与字符串；其他已支持语言使用不执行目标代码的有界标识符词法扫描，并跳过行注释、块注释及单引号、双引号、反引号字符串；
  - 返回精确名称命中的声明/引用类型、语言、路径、行列和 bounded excerpt，不把裸文本出现次数冒充语义引用；
  - 使用现有声明提取器统计同名定义，明确返回 `unresolved`、`unique`、`ambiguous` 或 `indeterminate`，解析失败、扫描不完整和符号上限不会静默宣称唯一解析；
  - 结果包含定义数、扫描文件/字节、跳过二进制/大文件/不支持文件、解析失败和截断状态；结果数、单文件、总字节、目录条目、符号与词法出现次数均有硬上限；
  - 未闭合字符串和块注释标记为词法解析失败；该后端继续明确标记为 `builtin_static`，只按名称解析，不冒充受控 LSP 的位置绑定高精度引用。
- Fifth delivery checks：
  - Code/Runtime/Tool Discovery 与 Tool Policy 定向回归：`63 passed`；
  - Code/Runtime/Context/Agent 关联回归：`113 passed`；
  - Tool/Policy/Subagent 关联回归：`45 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4985 passed, 5 skipped, 11 warnings`；
  - 全仓 Ruff、文档测试和 `git diff --check`：passed。
- Fifth delivery implementation commit：`b7e4b969`。
- Sixth delivery slice：
  - 已将 `call_hierarchy` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool，支持 `incoming`、`outgoing` 和 `both`；
  - General Run 与 Code Audit 继续复用 owner-bound Workspace/Snapshot 和共享有界语义扫描预算，不读取 Audit 可变输出目录；
  - Python 使用标准库 AST 提取限定 Caller、模块级调用和名称/属性 Callee；默认参数、Decorator、Base 等定义期调用不会错误归入函数体 Caller；
  - 其他已支持语言使用明确标记为 `lexical` 的名称级调用点：共享注释/字符串清洗器，过滤函数声明和控制关键字，并使用已提取的函数/方法声明提供低置信 Caller；
  - JavaScript/TypeScript 声明提取补充常见类/对象方法，使方法声明不会被当作调用点；词法 Caller 仍是近似结果，每条边均携带 `python_ast` 或 `lexical` 置信标记；
  - 返回定义数、`unresolved`/`unique`/`ambiguous`/`indeterminate`、分析模式、调用边、扫描质量和截断状态；文件、字节、符号、调用和结果数均有硬上限；
  - 该后端继续标记为 `builtin_static`，只提供安全的静态降级导航，不冒充受控 LSP 或完整跨语言语义调用图。
- Sixth delivery checks：
  - Code/Runtime/Tool Discovery 与 Tool Policy 定向回归：`67 passed`；
  - Code/Runtime/Context/Agent 关联回归：`119 passed`；
  - Tool/Policy/Subagent 关联回归：`45 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4989 passed, 5 skipped, 12 warnings`；额外警告为既有并发首启测试中的 Pydantic alias schema 提示；
  - 全仓 Ruff、文档测试和 `git diff --check`：passed。
- Sixth delivery implementation commit：`cbc2a2e5`。
- Seventh delivery slice：
  - 已将 `diagnostics` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Subagent Resident Tool；
  - General Run 与 Code Audit 继续复用 owner-bound Workspace/Snapshot 和共享有界语义扫描预算，不读取 Audit 可变输出目录；
  - Python 使用标准库 AST 返回有界语法错误；其他已支持语言使用不执行目标代码的词法结构检查，报告未闭合字符串、未闭合块注释、意外/错误配对/未闭合分隔符和分隔符深度超限；
  - 返回诊断级别、代码、消息、语言、路径、行列、bounded excerpt、扫描文件/字节、跳过原因、解析失败和截断状态；文件、字节、目录条目、诊断扫描数、返回结果和分隔符深度均有硬上限；
  - 结果明确标记为 `backend=builtin_static`，每条诊断携带 `python_ast` 或 `lexical` 置信标记；本切片不启动 Language Server，不执行项目 Hook、插件、构建、测试或安装脚本，也不冒充 LSP diagnostics。
- Seventh delivery checks：
  - Code/Runtime/Tool Discovery、Tool Policy 与工具可见性定向回归：`70 passed`；
  - Code/Runtime/Tool 关联回归：`85 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4992 passed, 5 skipped, 11 warnings`；跳过项为 Windows、PowerShell 或 delegated cgroup 主机条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff 和 `git diff --check`：passed。
- Seventh delivery implementation commit：`546f1466`。
- Eighth delivery slice：
  - 已建立显式批准 control tool bridge，供后续 `apply_patch`、Worktree 和 Revert 等原生写工具复用；本切片只交付批准与恢复基础设施，不向生产 Agent 暴露尚未实现的写工具；
  - OpenAI Agents SDK 的 interruption 会投影为 `TOOL_CALL_READY`，携带 `engine_call_id`、Tool ID、参数和 `approval_policy=explicit`；显式批准不受 AUTO mode 或 Run 级永久授权绕过；
  - Provider control proposal 以无 Runner execution spec 的 durable ToolCallIntent 保存，批准请求绑定 Cycle、Step、Context compilation、Provider State 和原始 Tool Call；批准后恢复 SDK Provider State，不创建 Runner Execution；
  - SDK Resume 只按 `engine_call_id` 应用本次数据库批准或拒绝；`approve_tool_for_run` 不会转成 SDK 永久批准，Hook 或普通上下文注入的伪 `approval_decision` 会在 Resume 前剔除；
  - Runtime control handler 在执行批准型 mutation 前以 CAS 将 intent 从 `READY` claim 为 `EXECUTING`，完成或失败后 durable settle 为 `COMPLETED`/`FAILED`；缺少 approved intent 时失败关闭；
  - 新入口已纳入 RunKind Effect inventory：proposal persistence 与 control execution state mutation 均保持 General-only durable write 边界。
- Eighth delivery checks：
  - Agent Factory、OpenAI Agents Adapter、Deferred Runtime、Control Tool、Approval Recovery、Coordinator、Tool Visibility 与 Tool Policy 定向回归：`119 passed`；
  - RunKind Effect Policy、Deferred Runtime、Control Tool 与 Approval Recovery 关联回归：`94 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`4999 passed, 5 skipped, 11 warnings`；跳过项为 Windows、PowerShell 或 delegated cgroup 主机条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff 和 `git diff --check`：passed。
- Eighth delivery implementation commit：`08d746ec`。
- Ninth delivery slice：
  - 已将 `apply_patch` 和 `revert_patch` 接入生产 Runtime control tool、Tool Policy 与 Primary Agent；两者均要求逐次 `approval_policy=explicit`，不向 Subagent 暴露；
  - 写工具只允许 General Run，Code Audit Run 保持只读；不使用通用 Shell，不执行项目 Hook、构建、测试或安装脚本；
  - `apply_patch` 使用严格的单文件 `*** Begin Patch` 格式，支持 Add、Update 和 Delete；Update/Delete 必须绑定读取所得 SHA-256，Add 必须确认目标不存在；
  - Patch 只接受有界 UTF-8 文本，保留 LF/CRLF 与原 permission mode，拒绝二进制、混合换行、Symlink、特殊文件、超限文件、歧义 Context 和无变化 Patch；
  - 写入前先发布 owner-bound immutable Receipt，保存原内容、原/结果 Digest、原 mode 和原 Patch；Receipt 加载会验证 Run owner、MIME、Artifact 完整性并重新执行 Patch 推导结果，拒绝结构合法但语义伪造的恢复材料；
  - 单文件提交使用同目录临时文件、`fsync`、Digest CAS 与原子 link/replace；删除和撤销要求当前状态精确匹配预期 Digest，外部漂移失败关闭；
  - `revert_patch` 可在 Worker/服务实例重启后通过持久 Receipt 恢复 Add、Update 或 Delete，跨 Run Receipt、错误 MIME、错误 Digest 和内容漂移均失败关闭；
  - Patch/Revert 结果、代码路径与 Receipt Artifact 会进入 Transcript source refs；Receipt Artifact 持久化失败时不会写入目标文件。
- Ninth delivery checks：
  - Code、Artifact、Runtime、Agent Factory、Deferred Approval/Recovery、Tool Visibility 与 Tool Policy 关联回归：`141 passed`；
  - Patch、真实 Artifact 持久化/跨实例恢复、Context Gate 与最终安全回归：`25 passed`；
  - 全量回归除一个既有 POSIX 进程替换 2 秒时序项外：`5007 passed, 5 skipped, 1 deselected, 12 warnings`；该时序项在相同代码状态下独立连续运行 `3 passed`；
  - 跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件；警告为既有 Python 3.12 SQLite datetime adapter 和并发首启 Pydantic alias 提示；
  - 全仓 Ruff 和 `git diff --check`：passed。
- Ninth delivery implementation commit：`203f6c1e`。
- Later slices：隔离 Worktree，以及受控 LSP。

### CAP-102：Browser/Web/Traffic Tool 闭环

- Status：completed
- Started：2026-08-06
- Inputs：CAP-001、既有 `BrowserApplicationService`、Runner managed browser、显式批准 control tool bridge、Scope 与 Artifact/Transcript 边界。
- First delivery slice：
  - 已将 `open_browser`、`observe_browser`、`act_browser` 和 `close_browser` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Temporal Worker；这些浏览器工具不向 Subagent 暴露；
  - `open_browser` 只允许 Runtime 注入 Run/Agent Session owner 的 managed ephemeral、headless browser，不向模型暴露 persistent profile 或 CDP；
  - `open_browser` 与 `act_browser` 要求逐次 `approval_policy=explicit`，并在任何 Browser 调用前 claim durable control intent；缺少批准时零 Browser 副作用；
  - `observe_browser` 与幂等清理型 `close_browser` 不要求批准，但 Browser Session 必须同时匹配 Run 和精确 Agent Session owner；Sibling Agent Session 在观察、操作或关闭前失败关闭；
  - Agent Action 只开放 Navigate、Click、Fill、Type、Select、Press、Scroll、Download、Wait、Go Back 和 Reload；不开放 `evaluate`、`upload`、用户 Profile、CDP 或任意 options；
  - Browser Action 使用稳定 Runtime `call_id` 作为 `action_key`，继续由既有 Browser Service/Runner 拒绝陈旧 Observation、非 Agent owner、越界 URL/Link 和重复键参数漂移；
  - Browser Observation 明确保留 `UNTRUSTED_EXTERNAL_CONTENT`，模型结果压缩到 256 KiB 以下；Screenshot、Network、DOM 与 Download Artifact 自动进入 Transcript `artifact_ids` 和 `source_refs`；
  - 本切片复用既有 Browser Application Service、Runner Router、Scope Guard、Artifact Publisher 和停止证明，不建立第二套浏览器状态或执行协议。
- First delivery checks：
  - Control Tool、Agent visibility、Tool Policy 与 Tool Discovery 定向回归：`46 passed`；
  - Browser、Runtime Engine、Deferred Approval/Recovery、Context Gate 与 Worker Runtime 关联回归：`183 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5013 passed, 5 skipped, 12 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 和并发首启 Pydantic alias 提示；
  - 全仓 Ruff、staged `git diff --check`：passed。
- First delivery implementation commit：`69d54ab7`。
- Second delivery slice：
  - 已将 `web_fetch` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Temporal Worker；不向 Subagent 暴露；
  - 每次 Public Web Fetch 要求 `approval_policy=explicit`，缺少 durable approved intent 时不会进入 Run 查询、DNS 或 HTTP；
  - Fetch 只允许匿名 GET，不向模型开放 Credential/Proxy/Host 路由 Header；URL Credential、非 HTTP(S)、私有/本地/保留 IP 和 DNS 解析到非公网地址继续失败关闭；
  - 生产 Worker 使用 Run-scoped `SQLAlchemyWebSourceRepository` 与 `ApplicationWebArtifactStore`，成功 Fetch 后才生成 canonical `WebDocument`、Chunk 和 `SourceReference`；Search Candidate 仍不能直接成为可引用 Source；
  - 新增 `SERVICE_WEB_FETCH` RunKind effect inventory；当前只允许 General Run，Code Audit、暂停、取消、完成和失败状态均在 DNS/HTTP 前拒绝；每次重定向前重新检查 Run 状态与公网目标，跨域自动跟随仍重新执行 SSRF 检查；
  - 模型参数限制为 URL、缓存策略、重定向策略、最多 10 MB 响应、最多 60 秒、原文保存和 Browser handoff 标志；固定 GET、匿名 Header 与最多 10 次重定向由 Fetcher 持有；
  - 模型结果保留 `UNTRUSTED_EXTERNAL_CONTENT`，只内联最多 6 个 Chunk、每个 6000 字符和有界元数据；完整原文与规范化正文进入 immutable Artifact，Source/Document/Artifact 自动进入 Transcript refs；
  - Browser Fallback、Redirect 和 Binary/Partial 仍返回 typed status，不把未 canonicalize 的页面冒充正式 Source。
- Second delivery checks：
  - Public Fetch、Control Tool、Agent visibility、Tool Policy、Tool Discovery、RunKind Effect 与 Worker Runtime 定向回归：`113 passed`；
  - 完整 Web、Runtime Engine、Deferred Approval/Recovery、Context Gate、RunKind Effect 与 Worker Runtime 关联回归：`188 passed`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5017 passed, 5 skipped, 11 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 提示；
  - 全仓 Ruff、staged `git diff --check`：passed。
- Second delivery implementation commit：`e8c047c6`。
- Third delivery slice：
  - 已将 `web_search` 和 `web_research` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Temporal Worker；两者均要求逐次 `approval_policy=explicit`，不向 Subagent 暴露；
  - 新增 `web.search` 配置，默认使用当前 Run 的官方 OpenAI profile 提供 Hosted Search，可配置 SearXNG，也可联合两者并在单 Provider 失败时返回有界 warning 与可用结果；
  - OpenAI Hosted Search 只接受 `provider=openai` 且未配置自定义 `base_url` 的 profile；OpenAI-compatible、本地 Gateway 或自定义目的地不会被误授 Hosted Search 能力，无可用 Provider 时返回明确 `web_search_unavailable`；
  - 联合 Search 对 Provider 并发调用、跨 Provider 去重并交错保留高排名候选；Search Result 继续是 `DISCOVERY_ONLY_NOT_A_CANONICAL_SOURCE`，不能直接成为 Finding 引用源；
  - `web_research` 使用最多 4 个查询、50 个候选和 6 个 Source；候选必须通过既有 SSRF-safe Public Fetch 成功 canonicalize 后，才能进入 Note、Claim 和 Research Packet；
  - 新增 `SERVICE_WEB_SEARCH` 和 `SERVICE_WEB_RESEARCH` RunKind effect inventory；当前仅允许 General Run，每个实际 Provider 出站前都重新读取 Run 并检查暂停、取消、完成或失败状态；
  - Search Response 和最终 Research Packet 均以 `UNTRUSTED_EXTERNAL_CONTENT` 写入 immutable JSON Artifact；Search Query/Result、Research Note/Packet 同步进入既有持久层，Query、Packet、canonical Source 和所有 Artifact 自动进入 Transcript refs；
  - Hosted Search 与 Agent 主模型复用同一 generation-aware `AsyncOpenAI` Client；无论 Search 还是主模型先初始化，都不会覆盖并泄漏旧 Client。
- Third delivery checks：
  - Web Provider、Research Pipeline/Repository/Service、Model Provider、Runtime Control Tool、Agent visibility、Tool Policy/Discovery、RunKind Effect、Config 与 Worker Runtime 定向回归：`186 passed`；
  - 完整 Web、Runtime、Agent Integration、Model/Tool/Agent Unit、Worker Runtime、Config 与 RunKind Effect 关联回归：`560 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/models/provider.py src/riftx/web/search.py src/riftx/web/service.py`：`Success: no issues found in 3 source files`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5038 passed, 5 skipped, 11 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 提示；
  - 全仓 Ruff、Example YAML 解析和 staged `git diff --check`：passed。
- Third delivery implementation commit：`c9a6394a`。
- Fourth delivery slice：
  - 已将 `query_http_traffic`、`read_http_exchange` 和 `target_http_request` 接入生产 Runtime control tool、Tool Policy、Primary Agent 与 Temporal Worker；三者均不向 Subagent 暴露；
  - `query_http_traffic` 与 `read_http_exchange` 只接受 trusted Runtime Run identity，继续复用现有 metadata-only Traffic 投影、稳定 Snapshot/Cursor、URL/Redirect 脱敏和精确 Run/Engagement owner 约束；Runtime Cursor 使用与本地操作者不同的签名绑定；
  - `read_http_exchange` 通过 `run_id + exchange_id` 精确读取 durable Target HTTP Result，仅向模型返回脱敏元数据、最多 8192 字符的不可信响应预览，以及通过 owner 检查的 Request/Response Artifact ID；完整原文继续使用既有 `read_artifact` 有界读取；
  - `target_http_request` 要求逐次 `approval_policy=explicit`；Runtime Approval claim 返回同一个 durable `ToolCallIntent`，其 ID 同时成为 Target HTTP `tool_call_id` 和 `execution_key` 身份组成部分，不创建第二套临时批准或执行身份；
  - 写工具复用既有 `TargetHttpApplicationService`、ScopeGuard、Node Router、Runner Client、effect guard、幂等 Repository 与 `RunSafetyStopService`；每个实际网络副作用前仍重新检查 Run/Intent，暂停、取消、完成、失败、越界 URL 和 Code Audit Run 均在出站前拒绝；
  - 模型可提供 Method、URL、有限 Header/Query/Cookie、Body/JSON、TLS/Redirect、超时和响应上限；不开放 Proxy、Client Certificate、Artifact 保存开关或 Runner 路由，Request/Response 原文固定写入 immutable `UNTRUSTED_SOURCE` Artifact；
  - Target HTTP 模型结果不暴露 Response Header、Cookie、Authorization 或完整 URL 值，只返回 URL/Redirect 安全摘要、有界 `UNTRUSTED_EXTERNAL_CONTENT` 预览、Exchange ID 和 Artifact ID；Exchange/Artifact 自动进入 Transcript refs；
  - 新增 exact Run Result lookup、Artifact owner fail-closed、Runtime/Operator Cursor 隔离、durable Intent identity、Primary/Subagent visibility 和 Code Audit 拒绝测试；既有 Target HTTP 停止确认、远程 Runner 与安全收敛语义保持不变。
- Fourth delivery checks：
  - `conda run --no-capture-output -n agent pytest -q tests/unit/application/test_traffic.py tests/integration/persistence/test_traffic_repository.py tests/target_http/test_service.py tests/runtime/test_control_tools.py tests/unit/agent/test_tool_policy.py tests/unit/tools/test_discovery.py tests/integration/agent/test_runtime_tool_visibility.py tests/execution/test_deferred_runtime.py tests/unit/temporal/test_worker_runtime.py tests/unit/application/test_run_kind_effect_policy.py tests/unit/application/test_run_kind_effect_bridge.py`：`169 passed`；
  - `conda run --no-capture-output -n agent pytest -q tests/target_http tests/runtime tests/integration/agent tests/integration/api/test_traffic_api.py tests/integration/persistence/test_traffic_repository.py tests/unit/agent tests/unit/tools tests/unit/temporal`：`458 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/execution/deferred.py src/riftx/target_http/service.py`：`Success: no issues found in 2 source files`；
  - `conda run --no-capture-output -n agent pytest -q`：`5045 passed, 5 skipped, 11 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Fourth delivery implementation commit：`27fec108`。
- Fifth delivery slice：
  - `SERVICE_WEB_FETCH`、`SERVICE_WEB_SEARCH` 和 `SERVICE_WEB_RESEARCH` 允许 General 与 Code Audit Run；Browser 与 Target HTTP 的既有 RunKind 边界未放宽；
  - Public Fetch、Search 和 Research 在 Code Audit Run 中继续执行既有逐次批准、Run 状态重读、SSRF、重定向重检、候选 canonicalize、Source trust 和 Transcript 约束；
  - `ApplicationWebArtifactStore` 生产构造必须注入 Run 与 Audit owner Repository；General 内容继续注册为 `PUBLIC_EXPORT`，Code Audit 内容先通过 `get_by_run_authorized` 校验精确 owner，再调用 `register_audit_content` 注册为 `AUDIT_INTERNAL`；
  - 公网原文、规范化正文、Search Response 与 Research Packet 均沿用 `UNTRUSTED_SOURCE` Artifact trust；缺失、foreign、歧义、损坏或不可用的 Audit owner 不会回退到通用 Artifact 路径；
  - Worker 装配、Code Audit Fetch/Search/Research 成功路径、暂停状态、SSRF、Artifact owner、RunKind 策略和既有 `read_audit_content_slice` 路径均有回归覆盖。
- Fifth delivery checks：
  - Web、Artifact、RunKind 与 Worker Runtime 定向回归：`81 passed`；
  - 完整 Web、Runtime Control Tool、Artifact、Agent visibility、Deferred Runtime、Worker Runtime 与 RunKind 关联回归：`196 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/web/fetch.py src/riftx/web/service.py src/riftx/application/run_kind_effects.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 4 source files`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5053 passed, 5 skipped, 11 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Fifth delivery implementation commit：`e7fc3461`。

## 9. Known pre-existing worktree state

- 当前没有已知的任务外工作树改动；每个切片仍须以当次 `git status` 为权威证据。
