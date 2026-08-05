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
- 评测用于回归、复现和能力演进，不以量化证明超过通用 Agent 为完成条件。

## 2. Current wave

- Stage：`S1 — 生产 Capability Plane`
- Current task：`CAP-101 — 原生代码工具`
- Status：`in_progress`
- Completed predecessor：SEC-000，implementation commit `a15e8e94`。
- Completed predecessor：SEC-001，implementation commit `53161141`。
- Completed predecessor：CAP-001，domain/API commit `0fd20fda`，persistence commit `84481149`。
- Completed predecessor：CAP-100，implementation commit `bb1b3b03`。
- Product behavior：当前建立 Code Workspace/Snapshot 内的原生只读工具，不用通用 Shell 替代代码导航。
- Next task after completion：`CAP-102 — Browser/Web/Traffic Tool 闭环`。

## 3. 研究与实现基线

| 输入 | 基线 | 用途 |
| --- | --- | --- |
| RiftX | `e40af267` | 正式版计划开始前的产品代码基线 |
| 正式版计划 | `84c657e1` | S0-S8 权威开发计划及评测定位修订 |
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
- 评测只服务于 RiftX 自身的质量、安全和能力演进，不用于量化证明超过通用 Agent；
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
| S7 专业能力评测与回归保障 | pending | 专业案例、对照 Harness 与质量安全发布检查可用 |
| S8 Pack 生态与正式版运维 | pending | SDK、签名供应链、Gateway 和持续运维可用 |

## 7. Task status

| Task | Dependency | Status | Implementation commit |
| --- | --- | --- | --- |
| SEC-000 | none | completed | `a15e8e94` |
| SEC-001 | SEC-000 | completed | `53161141` |
| CAP-001 | SEC-000 | completed | `0fd20fda`, `84481149` |
| CAP-100 | CAP-001 | completed | `bb1b3b03` |
| CAP-101 | CAP-001 | in_progress | `73ba9900`, `80276a08` |
| CAP-102 | CAP-001 | pending | — |
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
- Third delivery implementation commit：pending。
- Later slices：符号/引用/调用层级/LSP，以及显式批准的 Patch/Worktree/Revert。

## 9. Known pre-existing worktree state

- 当前没有已知的任务外工作树改动；每个切片仍须以当次 `git status` 为权威证据。
