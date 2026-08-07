# RiftX 正式版 Agent 开发实施账本

> 状态：active
>
> 启动日期：2026-08-05（Asia/Shanghai）
>
> 实现分支：`ch1nfo/riftx-3-code-audit`
>
> 计划输入基线：`e40af267`
>
> 初始正式版计划提交：`84c657e1`
>
> 当前交付重基线提交：`9424b82b`
>
> Pentest-first 优化计划提交：`84b45640`
>
> 权威计划：[RiftX 正式版开发优化文档](../../RiftX_正式版_开发优化文档.md)
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

- Stage：`Pentest-first R1 — Stage E 默认产品面收缩与发布`
- Current task：`E3 — 消费者与启动成本审计`
- Status：`in_progress`
- Completed predecessor：SEC-000，implementation commit `a15e8e94`。
- Completed predecessor：SEC-001，implementation commit `53161141`。
- Completed predecessor：CAP-001，domain/API commit `0fd20fda`，persistence commit `84481149`。
- Completed predecessor：CAP-100，implementation commit `bb1b3b03`。
- Completed predecessor：CAP-103，implementation commits `2c784d8d`、`94d71f3b`、`483ddb81`。
- Completed predecessor：CAP-104，implementation commits `7fc96d33`、`ab9c2f3c`、`fe5e9a86`、`62627843`。
- Completed predecessor：COG-200，implementation commits `20b8dc92`、`099ae428`、`cc48c32a`。
- Completed predecessor：COG-201，implementation commits `d9c2e530`、`f54b1ed8`、`97cd44d7`；migration-head verification commit `666d055a`。
- Completed predecessor：COG-202，implementation commits `4413c1f3`、`e21a6d7f`、`eb1d30b5`、`317a20f7`。
- Completed predecessor：COG-203，implementation commits `a8dbdf50`、`87c7381d`、`d369b684`。
- Completed predecessor：COG-204，implementation commits `16a1d800`、`a03654e0`、`21e28b3e`、`de863606`、`7a70ef6f`、`465ea1f0`、`65f12b02`；cleanup commit `654a72bd`。
- Completed predecessor：COG-205，implementation commits `7849cb2b`、`f09ace2a`、`dc2099a0`。
- Completed predecessor：PACK-300，implementation commits `5e56682e`、`89d43498`、`128f8ae1`、`b87305d9`、`c095ae7f`。
- Completed predecessor：CAP-101，implementation commits `73ba9900`、`80276a08`、`a83875d1`、`c6de9413`、`b7e4b969`、`cbc2a2e5`、`546f1466`、`08d746ec`、`203f6c1e`、`8ae9161d`、`abed90b4`。
- Completed predecessor：PACK-301，implementation commits `4f74479d`、`81574f56`、`0237a0cb`、`8b1cea9b`。
- Completed predecessor：PACK-302，implementation commits `d4f6e4eb`、`02cde9fe`、`eb41f77d`、`41eb8896`、`36100d47`、`0c70cf2e`、`3a1f0fc8`、`e4281b2f`、`6550f85a`、`faf12c50`、`ab3f50b6`、`4ba069e4`。
- Completed predecessor：PEN-500，implementation commits `315039fc`、`86aaecdf`、`e2314e9b`、`8b9ef440`、`8f1b2554`、`33c863ea`、`70f6f4a0`、`2271bc8e`、`9a4714fc`、`ca12ad9b`、`ad91c3f4`、`2c0f8004`、`73288673`、`b6b5f739`、`53812397`、`1c379dcc`。
- Completed predecessor：Stage B 网络服务专业闭环，implementation commits `54c2489b`、`c7709790`、`5018f071`、`b2193139`。
- Completed predecessor：Stage C/C1 最小身份/对象授权靶场，implementation commit `d73a0c86`。
- Completed predecessor：Stage C/C2 身份、状态与最小验证，implementation commit `85decf8d`。
- Completed predecessor：Stage C/C3 结论、Closure 与 Report，implementation commit `a8b29a4c`。
- Completed predecessor：Stage D/D1 Operator Skill 生命周期门禁，implementation commit `6f59e278`。
- Completed predecessor：Stage D/D2 Operator Skill 人工复盘与版本迭代，implementation commit `a929fdb4`。
- Completed predecessor：Stage E/E1 默认产品入口与 Quickstart 单路径，implementation commit `fdfa06e5`。
- Completed predecessor：Stage E/E2 干净 XDG Onboard 与可选工具降级，implementation commit `8ea90890`。
- Product behavior：PACK-302 已交付可重复运行且零覆盖的 `riftx onboard`、顶级 `riftx doctor`、live overlay、本地操作员只读 `/api/v1/system/diagnostics`、有真实修复语义的 `riftx doctor --fix`、Onboard 后可直接运行的两个安全 Demo，以及本地只读 Capability/Pack 检查命令；Onboard 复用现有 Runtime/Model/Tool/Pack 生产路径初始化用户级配置、完整 Alembic schema、22 个 Official Pack 与 66 个 active lock；Pack 持久化写入具备 SQLite 一致性备份、双端 inode identity、恢复后完整性复检和失败自动回滚；Doctor `backup_restore` 已改为只读真实 readiness，只在已到 Alembic head 的 file-backed SQLite、当前用户所有的普通数据库文件和安全的 owner-only 备份目录前置条件全部成立时返回 `ready`，不为诊断创建备份或替换数据库。
- Completed PEN-500 implementation commits：ADR `315039fc`；Domain/持久化 `86aaecdf`；Workflow/Runner Identity `e2314e9b`；Effect Policy/Interactive Guard `8b9ef440`；专用 Admission/Application/API 创建入口 `8f1b2554`；Capability Selection 原子绑定 `33c863ea`；Pentest status/API `70f6f4a0`；Pentest CLI `2271bc8e`；Attack Surface `9a4714fc`；隔离生命周期 `ca12ad9b`；目标交互预算 `ad91c3f4`、`2c0f8004`；运行中用量 `73288673`；Model/Token/Duration 门禁 `b6b5f739`；Tool/Duration 门禁 `53812397`；统一预算停止 `1c379dcc`。
- Verification：阶段 A 完整 Control Plane `67 passed`；Runtime/Execution/Target HTTP/Temporal Worker `467 passed`；预算处理聚焦回归 `215 passed`；全仓 Ruff passed；6 个变更核心源文件 scoped mypy passed；Alembic 单 head `7b3d1e5f9a24`；`git diff --check` passed。
- Stage B verification：B4 受影响 Report/Closure/Worker 回归 `78 passed`；完整 Control Plane `68 passed`；最终聚焦回归 `19 passed`；全仓 Ruff passed；3 个变更生产文件 scoped mypy passed；`git diff --check` passed。
- Stage C/C1 verification：聚焦 E2E `1 passed`；Target HTTP 受影响回归 `47 passed`；完整 Control Plane 加 C1 `69 passed`；全仓 Ruff passed；2 个新测试文件 scoped mypy passed；`git diff --check` passed。
- Stage C/C2 verification：状态化 Web E2E `2 passed`；Target HTTP `63 passed`；Runtime Control Tools `52 passed`；完整 Control Plane `68 passed`；Worker `13 passed`；Tool discovery `11 passed`；全仓 Ruff passed；6 个变更生产文件与 3 个新增/变更测试文件 scoped mypy passed；`git diff --check` passed。`runtime/control_tools.py` 单文件仍仅命中既有 62 个变量复用类型错误。
- Stage C/C3 verification：状态化 Web E2E `2 passed`；Report Application `2 passed`；Temporal Activities `23 passed`；Target HTTP `63 passed`；Runtime Control Tools `52 passed`；Worker Runtime `13 passed`；完整 Control Plane `68 passed`；文档合同 `3 passed`；全仓 Ruff passed；`reports.py`、`api/runtime.py`、`temporal/worker_runtime.py` 与状态化 Web 测试 scoped mypy passed；`git diff --check` passed。`test_control_plane.py` 单文件 mypy 仍仅命中既有 5177–5179 行 3 个 `object` 索引错误。
- Stage D/D1 verification：Operator Skill lifecycle/CLI `3 passed`；Capability Management、Doctor、CLI 与 Skill unit `111 passed`；Capability/Skill/Pentest persistence `10 passed`；状态化 Web `2 passed`；完整 Control Plane `68 passed`；文档合同 `3 passed`；全仓 Ruff passed；5 个变更源/测试文件 scoped mypy passed；`git diff --check` passed。
- Stage D/D2 verification：Report、状态化 Web、Operator Skill lifecycle 与 v1→v2 纵向 E2E `8 passed`；全仓 Ruff passed；`reports.py` scoped mypy passed；`git diff --check` passed。
- Stage E/E1 verification：Onboard、Doctor、CLI、Operator Skill Report 与文档合同 `100 passed`；全仓 Ruff passed；`cli/app.py` scoped mypy 在禁用既有 Typer/Click `override`、`arg-type`、`return-value` 兼容错误后 passed；`git diff --check` passed。
- Stage E/E2 verification：Onboard、Doctor、CLI、隔离 XDG 生产装配/Pentest Admission 与文档合同 `100 passed`；全仓 Ruff passed；`onboarding.py` scoped mypy passed；`git diff --check` passed。
- Next delivery slice：只测量并记录候选模块的生产消费者和默认启动成本；没有可复现收益时不做 lazy-init、拆包或删除。

## 3. 研究与实现基线

| 输入 | 基线 | 用途 |
| --- | --- | --- |
| RiftX | `e40af267` | 正式版计划开始前的产品代码基线 |
| 正式版计划 | `84c657e1` | S0-S8 权威开发计划 |
| 交付重基线 | `9424b82b` | 专业纵向闭环优先、分层验证门禁、V1/增强/生态边界 |
| Pentest-first 优化计划 | `84b45640` | 新权威计划、ADR/账本链接与文档合同迁移 |
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

| Milestone | Status | Exit condition |
| --- | --- | --- |
| O0 平台根基与计划迁移 | completed | S0-S2 已完成；优化计划、ADR、账本和文档合同完成迁移 |
| O1 开箱即用收口 | completed | PACK-302 的真实 Backup/Restore Doctor 检查通过 |
| P1 真实 Pentest Run | completed | 非空 Scope admission、Pentest CLI、真实隔离目标和重启恢复 |
| P2 专业验证闭环 | completed | 网络服务与状态化 Web 的 Hypothesis、Evidence、Negative Result 和 Finding |
| P3 专业结果与收口 | completed | Closure、JSON/Markdown Report、恢复和 Stop Proof |
| P4 Operator 能力成长 | completed | D1 生命周期与 D2 人工复盘/版本迭代闭环完成 |
| R1 Pentest 发布门 | in_progress | Pentest-only 回归、真实场景和安全发布检查通过 |
| O2 代码优化 | in_progress | E1 默认产品面已收缩；删除仍需引用与成本证据 |
| Frozen Code Audit | frozen | 只修复安全、数据兼容和现有用户阻断问题 |
| Post-R1 Ecosystem | deferred | 真实第三方分发或规模化运维需求出现后再启动 |

## 7. Task status

| Task | Dependency | Status | Implementation commit |
| --- | --- | --- | --- |
| SEC-000 | none | completed | `a15e8e94` |
| SEC-001 | SEC-000 | completed | `53161141` |
| CAP-001 | SEC-000 | completed | `0fd20fda`, `84481149` |
| CAP-100 | CAP-001 | completed | `bb1b3b03` |
| CAP-101 | CAP-001 | completed | `73ba9900`, `80276a08`, `a83875d1`, `c6de9413`, `b7e4b969`, `cbc2a2e5`, `546f1466`, `08d746ec`, `203f6c1e`, `8ae9161d`, `abed90b4` |
| CAP-102 | CAP-001 | completed | `69d54ab7`, `e8c047c6`, `c9a6394a`, `27fec108`, `e7fc3461` |
| CAP-103 | CAP-001 | completed | `2c784d8d`, `94d71f3b`, `483ddb81` |
| CAP-104 | CAP-100, CAP-103 | completed | `7fc96d33`, `ab9c2f3c`, `fe5e9a86`, `62627843` |
| COG-200 | CAP-104 | completed | `20b8dc92`, `099ae428`, `cc48c32a` |
| COG-201 | COG-200 | completed | `d9c2e530`, `f54b1ed8`, `97cd44d7` |
| COG-202 | COG-201 | completed | `4413c1f3`, `e21a6d7f`, `eb1d30b5`, `317a20f7` |
| COG-203 | COG-202 | completed | `a8dbdf50`, `87c7381d`, `d369b684` |
| COG-204 | COG-203 | completed | `16a1d800`, `a03654e0`, `21e28b3e`, `de863606`, `7a70ef6f`, `465ea1f0`, `65f12b02` |
| COG-205 | COG-204 | completed | `7849cb2b`, `f09ace2a`, `dc2099a0` |
| PACK-300 | CAP-102, CAP-104, COG-205 | completed | `5e56682e`, `89d43498`, `128f8ae1`, `b87305d9`, `c095ae7f` |
| PACK-301 | CAP-101, CAP-104, COG-205 | completed | `4f74479d`, `81574f56`, `0237a0cb`, `8b1cea9b` |
| PACK-302 | PACK-300, PACK-301 | completed | `d4f6e4eb`, `02cde9fe`, `eb41f77d`, `41eb8896`, `36100d47`, `0c70cf2e`, `3a1f0fc8`, `e4281b2f`, `6550f85a`, `faf12c50`, `ab3f50b6`, `4ba069e4` |
| AUD-400 | CAP-101, COG-202 | pending | — |
| AUD-401 | AUD-400 | pending | — |
| AUD-402 | AUD-400, AUD-401, COG-205, PACK-301 | pending | — |
| AUD-403 | COG-201, AUD-400, AUD-401 | pending | — |
| AUD-404 | AUD-400, AUD-403 | pending | — |
| AUD-405 | CAP-101, AUD-403 | pending | — |
| PEN-500 | CAP-102, COG-202 | completed | `315039fc`, `86aaecdf`, `e2314e9b`, `8b9ef440`, `8f1b2554`, `33c863ea`, `70f6f4a0`, `2271bc8e`, `9a4714fc`, `ca12ad9b`, `ad91c3f4`, `2c0f8004`, `73288673`, `b6b5f739`, `53812397`, `1c379dcc` |
| D1 | Stage C | completed | `6f59e278` |
| D2 | D1 | in_progress | — |
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
| EVAL-702 | EVAL-701, LEARN-603 | pending | — |
| EVAL-703 | EVAL-702, PACK-302 | pending | — |
| ECO-800 | CAP-001, LEARN-604 | pending | — |
| ECO-801 | ECO-800 | pending | — |
| ECO-802 | LEARN-605, ECO-801 | pending | — |

交付说明：AUD-400 至 AUD-405 与 EVAL-700 冻结，不阻塞 Pentest-first R1；ECO-800 至 ECO-802 属于 Post-R1。任务状态仍保留 `pending`，用于保存历史依赖和未来恢复入口，不代表当前排期。

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

- Status：completed
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
- Tenth delivery slice：
  - 已将 `create_worktree` 接入生产 Runtime control tool、Tool Policy 与 Primary Agent，要求逐次 `approval_policy=explicit`，不进入 Subagent resident set；Runtime 直接调用仍校验 `agent_id=primary`，缺少批准时不会调用 Git 或产生文件系统副作用；
  - 只允许 General Run；Code Audit 保持只读。模型只提供 64 字节内 ASCII slug 与 `HEAD`/完整 40 或 64 字符本地提交哈希，不接受任意目标路径、分支名、revision expression、remote ref 或协议；
  - 目标固定为 Run workspace 直接子目录 `.riftx-wt-<run-digest>-<name>`，不经过可替换的中间目录；不同 Run 使用不同 owner digest，Symlink、FIFO、特殊文件、普通未注册目录和冲突 Worktree 均失败关闭；
  - Worktree 固定为 detached commit，避免隐式创建或覆盖 Branch。精确同 owner/name/commit 的重复调用返回 `action=existing`，HEAD 或 detached 状态漂移返回明确冲突；
  - Git 继续使用固定 executable、最小环境、无凭据/网络、禁用 Hook、fsmonitor、external diff、textconv 和签名行为；Repository config 出现 filter、remote、include、credential、submodule 或其他外部行为时在创建前拒绝；
  - 创建前后验证 Workspace FD/path binding、Git 管理区安全形态与配置；结果 Worktree 通过 no-follow 目录 FD 复核，`.git` 链接必须是有界普通文件并指向当前 Repository `.git/worktrees/` 内部；
  - 创建命令或创建后验证失败时，仅对本次确定性 owner destination 执行 `git worktree remove --force` 回滚；无法证明清理完成时返回 `code_worktree_cleanup_failed`，不静默遗留半成品；
  - 成功结果包含相对路径、精确 HEAD commit 与 detached 状态，并进入 Transcript `worktree://`、`git-commit://` source refs；成功 Worktree 作为 Run workspace 内的显式资产保留，本切片不增加通用删除工具；
  - `apply_patch` 和 `revert_patch` 共用新增的 writable-path 防线，拒绝修改任意层级 `.git` 文件或目录，防止 Root Repository 和 linked Worktree 管理状态被原生 Patch 工具伪造。
- Tenth delivery checks：
  - Git Worktree、Patch `.git` 防线与异常回滚定向测试：`40 passed`；
  - Code、Runtime、Agent Factory、Approval/Recovery、Context、Subagent、Tool Discovery/Policy 与 Temporal Worker 关联回归：`310 passed`；
  - RunKind Effect Policy、正式文档、Pack Catalog/Bootstrap 关联回归：`53 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/code/git.py src/riftx/code/models.py src/riftx/tools/discovery.py src/riftx/tools/policy.py`：`Success: no issues found in 4 source files`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5188 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff、`git diff --check` 与 staged `git diff --check`：passed。
- Tenth delivery implementation commit：`8ae9161d`。
- Eleventh delivery slice：
  - `symbol_search`、`find_references`、`call_hierarchy` 和 `diagnostics` 已接入可选的受控 LSP Gateway；四个工具先从既有 owner-bound Workspace 或不可变 Audit Snapshot 构造一次一致的有界源码 Bundle，LSP 失败时使用同一 Bundle 执行 `builtin_static`，不混用两类结果；
  - Gateway 请求只包含来源类型、Snapshot Digest、相对路径、语言、源码内容、内容 Digest、查询参数和硬上限，不包含 Run ID、Audit ID、Workspace Root 或本地绝对源码路径；Code Audit 继续通过 `Run → AuditScan → SourceSnapshot → SnapshotStore` owner binding，绝不读取可变输出目录；
  - RiftX 不直接启动 `clangd`、`rust-analyzer` 或其他目标项目 Language Server。生产 Worker 只连接 Operator 管理的 Unix Socket Gateway，并固定 Backend ID/version；Socket 与父目录必须归 Worker 账号所有且不可被组/其他用户写入，Bearer Token 必须来自进程环境且至少 32 字节；
  - Gateway 必须返回固定 `riftx.controlled-lsp-contract/v1`：content-only、provided-files-only、项目配置关闭、插件/命令关闭、构建/安装/测试/Hook 关闭、网络关闭；请求摘要、Backend 身份/version 或契约不匹配时结果失败关闭；
  - 请求体、响应体、源文件、总源码字节、文件数和返回结果均有硬上限；Gateway 结果中的路径和行列必须落在本次提供的源码内，来源、统计、Backend 元数据和输入 Digest 由 RiftX 覆盖，Backend 不能伪造；
  - 结果明确区分 `backend=controlled_lsp` 与 `backend=builtin_static`，并记录 Backend ID/version、`analysis_input_digest` 和 `fallback_reason`；不可用、失信、不支持、无受支持文件或非法响应不会把静态结果冒充 LSP 精度，下一次调用可独立恢复；
  - 生产配置新增默认关闭的 `code.lsp`，支持 Unix Socket、固定 Backend ID/version、Token 环境引用和请求超时；Temporal Worker 负责创建、注入并在正常关闭或装配失败时释放 Gateway Client。
- Eleventh delivery checks：
  - 受控 LSP、Code Workspace、Runtime Config、Temporal Worker、Control Tool、Tool Discovery/Policy 与 Agent visibility 定向/关联回归：`143 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/code/lsp.py src/riftx/code/models.py src/riftx/code/workspace.py src/riftx/config.py src/riftx/temporal/worker_runtime.py src/riftx/tools/discovery.py`：`Success: no issues found in 6 source files`；
  - `conda run --no-capture-output -n agent pytest -q`：`5205 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff、`git diff --check` 与 staged `git diff --check`：passed。
- Eleventh delivery implementation commit：`abed90b4`。

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

### CAP-103：MCP 生产接入

- Status：completed
- Started：2026-08-06
- Inputs：CAP-001、既有 `GovernedMCPAdapter`、生产 Temporal Worker、`openai-agents 0.19.0` 和 `mcp 1.29.0` 的 Streamable HTTP 契约。
- First delivery slice：
  - 新增 operator-owned MCP Server Registry 配置；首切片只允许远程 `streamable_http`，URL 禁止 Credential、Query 和 Fragment，HTTP Secret 只能通过环境变量名引用；
  - 生产 Worker 启动时并发连接已启用 Server，执行有总量/单 Server 并发限制、熔断与 discovery timeout 的 `tools/list`，并在关闭或构建失败时清理已连接 Client；
  - 单 Server 使用独立连接锁；缺失 Secret、连接失败、超时、非法 Tool 列表或工具数量超限只生成对应 Server 的 typed unavailable snapshot，不阻塞其他 Server 或 Worker；
  - Discovery 结果投影为稳定 qualified Tool ID、可搜索的有界 Tool Index 和按 generation 读取的完整 Schema；allow/block filter 在投影前执行；
  - Tool 名称、标题、描述和 JSON Schema 具有长度、结构、嵌套深度与总字节上限；配置 URL、已解析 Header Secret、POSIX/Windows/file URI 绝对路径不会进入 Snapshot 或模型可见 Schema；
  - Schema 固定携带 `execution_type=mcp`、`approval_policy=explicit` 和 `content_trust=UNTRUSTED_EXTERNAL_CONTENT`；MCP annotations 只保存为不可信 hint，不参与授权或批准决策；
  - Worker Node label 只记录 MCP Server/Tool 数量，不记录 URL、Header、环境变量引用或 Secret；
  - 当前 Adapter 只允许 `tools/list`，没有模型调用入口，不创建 durable ToolCall/Execution，不启动 stdio MCP。完整调用闭环留给后续 CAP-103 切片。
- First delivery checks：
  - `conda run --no-capture-output -n agent python -m pytest -q tests/mcp tests/unit/test_runtime_config.py tests/unit/temporal/test_worker_runtime.py`：`50 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/mcp src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5058 passed, 5 skipped, 11 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- First delivery implementation commit：`2c784d8d`。
- Second delivery slice：
  - 新增常驻 `search_mcp_tools`、`get_mcp_tool` 和 `call_mcp_tool`；模型通过受控包装工具搜索、读取 Schema 和调用 MCP，不直接获得第三方动态 FunctionTool；
  - 空 `allowed_tools` 保持 discovery-only；非空时只投影并允许显式列出的 Tool，`blocked_tools` 继续优先拒绝；Adapter 仅允许 `tools/list` 与 `tools/call`，仍不启动 stdio MCP；
  - `call_mcp_tool` 固定为 `HOST_EXECUTION + DYNAMIC_APPROVAL + explicit approval`，MCP annotation 仍不参与授权；未批准时不会发生 MCP 网络副作用；
  - Provider Control Tool 在执行前校验 durable Intent 的 Tool ID 与完整 Arguments，并通过既有 ToolCall execution-claim CAS 持久化确定性 `execution_key`；
  - `MCPApplicationService` 在调用前后重新验证 RunKind Effect Policy、Run 状态、Run/Session/ToolCall owner、Intent 状态、精确参数和当前 execution claim，直接调用服务不能绕过批准边界；
  - 调用参数保持原始 JSON 语义并具有结构与字节上限；调用结果限制结构、深度、单字符串和总字节，配置 URL、Header Secret、POSIX/Windows/file URI 绝对路径在进入 Artifact 或模型前脱敏；
  - 完整脱敏结果连同 Run、Session、ToolCall、Execution、Server 和 Tool identity 写入 immutable Artifact；模型只接收有界 `UNTRUSTED_EXTERNAL_CONTENT` Preview，Image/Audio 数据不进入模型；
  - Transcript 写入 `mcp-tool://`、`mcp-execution://` 和 `artifact://` source refs；General 与 Code Audit 均经 RunKind Effect Policy，Artifact 分别沿用 `PUBLIC_EXPORT` 与 owner-validated `AUDIT_INTERNAL` 路径；
  - 单 Server 调用超时、熔断、结果非法或超限只使该调用失败，不改变其他 Server Registry 条目；调用后 execution claim 丢失时不写 Artifact，不使已停止的调用产生新的 durable 结果。
- Second delivery checks：
  - `conda run --no-capture-output -n agent python -m pytest -q tests/mcp tests/runtime/test_control_tools.py tests/execution/test_deferred_runtime.py tests/unit/agent/test_tool_policy.py tests/unit/tools/test_discovery.py tests/integration/agent/test_runtime_tool_visibility.py tests/unit/application/test_run_kind_effect_policy.py tests/unit/test_runtime_config.py tests/unit/temporal/test_worker_runtime.py`：`167 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/mcp src/riftx/execution/deferred.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - MCP、Runtime、Execution、Agent、Tool、Context、Subagent、配置和 Worker 关联回归：`592 passed`；
  - `conda run --no-capture-output -n agent ruff check .` 和 `git diff --check`：passed；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5066 passed, 5 skipped, 12 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 与 Pydantic Field metadata 提示。
- Second delivery implementation commit：`94d71f3b`。
- Third delivery slice：
  - 新增可配置的 MCP Discovery refresh interval；Worker 复用既有后台任务生命周期周期执行 Registry refresh 与 Governor health snapshot，不引入独立调度框架；
  - 周期刷新保持 Registry 原子快照语义；单 Server discovery 失败继续只移除对应能力，意外的全局 refresh 失败保留上一份可用快照并持续重试；
  - Node heartbeat 发布 Registry generation、refresh 状态/失败次数、Server/Tool/不可用 Server 数量、active call 和 open circuit 聚合计数，不发布 Server ID、URL、Header、环境变量引用或 Secret；
  - 可选 MCP Server 故障不会把整个 Worker 标为不可用；运维面通过聚合标签观察局部降级；
  - Worker 关闭时先取消并等待 MCP refresh task，再清理 MCP Client，避免 refresh 与 transport cleanup 并发竞态。
- Third delivery checks：
  - `conda run --no-capture-output -n agent python -m pytest -q tests/mcp tests/unit/test_runtime_config.py tests/unit/temporal/test_worker_runtime.py`：`57 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/mcp src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 6 source files`；
  - MCP、Temporal、Runtime、Execution、Agent、Tool、Context 和 Subagent 关联回归：`616 passed`；
  - `conda run --no-capture-output -n agent ruff check .` 和 `git diff --check`：passed；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5067 passed, 5 skipped, 12 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 与 Pydantic Field metadata 提示。
- Third delivery implementation commit：`483ddb81`。
- Completion evidence：Registry、Discovery/Index、Schema、Governor/限流/熔断/超时/周期健康刷新、Tool Policy、Approval、Artifact 与 Transcript 均已接入生产 Worker；Authorization 不可由直接 service 调用绕过，Server 故障局部降级，调用具有 durable ToolCall/execution claim，Secret 与绝对路径不进入模型结果。

### COG-200：Task Graph

- Status：completed
- Started：2026-08-06
- Inputs：CAP-104、既有 Agent Runtime/Context Compiler、Working Memory、Run Graph 与 SQLAlchemy 事务边界。
- Persistence slice：
  - 新增 Task、TaskDependency、TaskAttempt、TaskBudget、TaskEvidenceRequirement 和 TaskGraph 领域契约；
  - 新增六张 durable 表、Repository 与 Alembic revision `4d7f1a8c2e90`，支持 Run 隔离、复合外键、重启恢复和非空降级保护；
  - Task Graph migration 在任何自身 DDL 前执行既有跨版本数据丢失保护，保持 Audit Preflight、Snapshot、Capability、Workflow Signal 与 Runner ownership 的降级契约。
- Planner slice：
  - 新增类型化 add/update/link/block/complete/fail/reopen/cancel 命令、Ready-task resolver 和原子 `claim_ready_task`；
  - 所有变更使用 graph version CAS，拒绝依赖环、陈旧版本、非法状态跃迁和未满足 Evidence Requirement 的完成请求；
  - 依赖未完成的 Task 不可 claim；并发 claim 不重复；失败 Task 必须显式 reopen 后重试，新 Attempt 保留 `retry_of_attempt_id` lineage；
  - 运行中 Attempt 绑定 Agent Session，其他 Session 不能完成、失败或取消该 Attempt。
- Runtime and projection slice：
  - Task Graph 成为 Primary Agent Context 的权威 current plan；存在新图时抑制旧 `working_memory.run_plan`，不存在时保持兼容读取；Subagent Delegation 不接收完整 Task Graph；
  - 生产 Resident Tool 接入 list/add/update/link/block/claim/complete/fail/reopen/cancel；图拓扑只允许 Primary 修改，Subagent 仅可列出、领取并结算自身 Attempt；
  - Temporal Worker 注入真实 SQLAlchemy Planner、Task Graph Context Source 和 Worker identity；Planner mutation 统一投影为 Engine `PLAN_UPDATE`；
  - Run Graph 优先从 durable Task Graph 有界读取 Task 与 Dependency，旧计划仅作 fallback；Task node 使用真实 provenance，显式生成 `depends_on` edge，依赖缺失和 source limit 均标记 partial，不把 Dependency 猜成 blocked reason；
  - Alembic head 常量与跨版本 downgrade regression 已同步到新 revision。
- Checks：
  - Context、Graph、Planner、Runtime、Tool Discovery/Policy、Engine 与 Worker 定向回归：`154 passed`；
  - Task Graph 与跨版本迁移保护回归：`44 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/tasks src/riftx/persistence/task_repositories.py src/riftx/persistence/task_planner.py src/riftx/persistence/graph_repositories.py`：`Success: no issues found in 6 source files`；
  - `conda run --no-capture-output -n agent alembic heads`：`4d7f1a8c2e90 (head)`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5096 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`20b8dc92`、`099ae428`、`cc48c32a`。

### COG-201：Evidence Ledger

- Status：completed
- Started：2026-08-06
- Inputs：COG-200、Artifact immutable content store、owner-bound Code Workspace/Snapshot、Agent Session 与 Task Graph persistence。
- Domain and persistence slice：
  - 新增十类统一 Evidence、Source/Artifact Span/Code Location locator、Trust、Scope、Redaction、Replay metadata 与 canonical Ledger Digest；Evidence 领域对象不可变并拒绝未知字段；
  - Artifact Span 使用 `artifact://<id>#bytes=<start>-<end>`，Code Location 使用规范化相对 POSIX 路径与 end-exclusive 行列坐标；
  - 新增 `evidence_ledger`、SQLAlchemy Repository 与 Alembic revision `5e8a2c4d7f10`，Run/Session/Task/Artifact 外键、枚举/摘要/脱敏约束、索引和非空降级保护完整落库；
  - Repository 重建时重新校验 canonical Ledger Digest，持久化记录被篡改时失败关闭。
- Source verification slice：
  - 新增内部 Evidence Application Service，只开放服务端可复核的 Artifact Span 和 Code Location 注册，不提前开放 COG-203 的模型通用 proposal/write tools；
  - Artifact Span 通过既有 Artifact content lease 重新读取并验证 immutable storage、Run/Audit owner、精确 offset 和内容摘要；
  - Code Location 复用 owner-bound Workspace/SourceSnapshot resolver，拒绝绝对路径、非规范路径、Symlink、特殊文件、非 UTF-8 源码、越界行列和来源漂移；
  - Code Location 记录完整文件 Digest、Snapshot/Workspace replay source、精确位置参数 Digest 和选中内容 Digest；Workspace 文件读取期间继续使用 FD 与文件指纹防止 TOCTOU；
  - Engagement Scope 由 Run 服务端填充；Session、Task、Audit 和 Artifact ownership 在落账前校验，Redaction 与 Replay shape 由类型契约验证。
- Runtime consumption slice：
  - Task Planner 在同一完成事务中验证 Requirement 的全部新旧引用；任意字符串、缺失 Evidence、其他 Run 或其他 Task 的 Evidence 均不能满足完成条件；
  - 当前 Task 可消费当前 Run 的 Run 级 Evidence 或自身 Task 级 Evidence；其他 Task 的 Evidence 被拒绝；
  - 该绑定使 Runtime `complete_task` 通过既有 Planner 路径直接消费 Evidence Ledger，不新增旁路状态。
- Checks：
  - Evidence domain、Repository、Migration、Application Service、Code Location 与 Task Planner 定向回归：passed；
  - 迁移 head 关联文件：`42 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/evidence src/riftx/application/services/evidence.py src/riftx/persistence/evidence_repository.py src/riftx/persistence/task_planner.py`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent alembic heads`：`5e8a2c4d7f10 (head)`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5109 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`d9c2e530`、`f54b1ed8`、`97cd44d7`；migration-head verification commit：`666d055a`。

### COG-202：Reasoning Graph

- Status：completed
- Started：2026-08-06
- Inputs：COG-201、Task Graph、Agent Session、Working Memory、Context Compiler、Evidence Graph 与 SQLAlchemy 聚合事务边界。
- Domain and persistence slice：
  - 新增 Observation、Fact Candidate、Confirmed Fact、Hypothesis、Vulnerability Candidate、Finding、Proof 与 Negative Result 八类 Reasoning Node，以及 supports、contradicts、derived_from、discovered_on、validates、exploits、invalidates 与 depends_on 八类 Edge；
  - Evidence-free Hypothesis 只能保持 unverified；Confirmed Fact 必须由 Fact Candidate 派生，Promoted Candidate 必须保留派生 lineage，Validated Proof 必须 validate Finding，Negative Result 必须 invalidate 或 contradict 既有 claim；
  - Confirmed Finding 必须同时绑定 Evidence 与 Reproduction Contract；Node/Edge Evidence 使用归一化关联表和同 Run 复合外键，不藏入 JSON；
  - 新增 Reasoning Graph、Node、Edge 和 Evidence association 表、SQLAlchemy Repository 与 Alembic revision `8b1d3f5a7c20`；持久化损坏或非法状态重建时失败关闭。
- Reducer and integrity slice：
  - 新增内部确定性 `ReasoningGraphApplicationService`，提供 Node/Edge 创建、Fact/Vulnerability 原子晋升、Proof、Negative Result 与受控状态转换；未开放 COG-203 的模型 proposal/query tools；
  - 所有聚合修改执行 Graph version CAS；既有 Node 修改执行 Node version CAS，禁止删除 Node/Edge、修改 Edge、漂移 Node identity、伪造时间戳、回退 Evidence lineage 或跳跃初始版本；
  - Node 绑定的 Session/Task 必须属于同 Run；绑定节点只接受 Run 级或自身 Session/Task 的 Evidence，跨 Run、Sibling Session 和其他 Task 引用失败关闭；
  - Finding 从 Candidate 晋升为 Confirmed 时，必须至少包含目标直接 Execution、Artifact Span、HTTP、Browser、Code Location/Flow、Scanner 或 deterministic parser Evidence；External Research、CVE 页面、PoC 描述和 User Decision 不能单独完成确认。
- Runtime and projection slice：
  - 生产 Temporal Worker 将 Reasoning Graph Repository 接入 Context Compiler；主 Agent 获取完整耐久认知图，Subagent Delegation 只获取显式选择的 Confirmed Fact；
  - Reasoning Graph 存在时抑制旧 Working Memory Fact/Hypothesis，旧 Run 缺少新图时继续兼容读取；Task Plan、Attempt、Approval 和其他 Working Memory 状态保持原有权威边界；
  - Evidence Graph 优先从 Reasoning Graph 有界读取 Node/Edge 的 ID、类型、状态与 Evidence ID；不选择 claim、structured data 或 Reproduction Contract；旧 Fact/Hypothesis/Finding 仅在新图不存在时回退；
  - Graph 投影保留全部 Reasoning relation、稳定 namespace、Evidence lineage 与 truncation/coverage 语义，截断或不可解析端点不会生成孤立 Edge。
- Checks：
  - Reasoning domain、Repository、Migration、Reducer、跨版本迁移关联回归：`86 passed`；
  - Context、Subagent 与生产 Worker Runtime 关联回归：`68 passed`；
  - Graph Repository、Application 与 API 关联回归：`57 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/reasoning src/riftx/application/graphs.py src/riftx/application/services/reasoning.py src/riftx/application/services/graphs.py src/riftx/context/items.py src/riftx/context/sources.py src/riftx/persistence/reasoning_repository.py src/riftx/persistence/graph_repositories.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 10 source files`；
  - `conda run --no-capture-output -n agent alembic heads`：`8b1d3f5a7c20 (head)`；
  - `conda run --no-capture-output -n agent pytest -q`：`5135 passed, 5 skipped, 18 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 与 Pydantic Field alias 提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`4413c1f3`、`e21a6d7f`、`eb1d30b5`、`317a20f7`。

### COG-203：Primary Agent Proposal Tools

- Status：completed
- Started：2026-08-06
- Inputs：COG-202 Reasoning Graph、Working Memory Reducer、Task Graph、Evidence Ledger、Runtime Control Tools、Tool Discovery/Policy 与 Temporal Worker 生产装配。
- Proposal service slice：
  - 新增 `WorkingMemoryProposalApplicationService`，仅接受结构化 Plan/Focus/Next Action 与 Attempt Proposal，统一经过既有 `WorkingMemoryReducer` 和 Repository CAS 后提交；模型不能替换完整 Working Memory；
  - Task Graph 存在时拒绝旧 `working_memory.run_plan` 拓扑写入，只允许兼容的 Focus/Next Action Proposal，避免形成生产 Context 不消费的第二份计划；
  - 重复 action signature、target、tool 与 normalized arguments 的失败操作，必须引用最近失败 Attempt、前次标记 retryable 且携带 retry reason，否则以稳定冲突码拒绝；
  - Reasoning Graph 新增 Run-bound 有界查询，支持 Node/Kind/Status/Task/Evidence/文本过滤、分页与 Edge 上限；空图返回 version 0，不泄漏其他 Run 状态。
- Runtime tool slice：
  - Primary Agent 新增 `propose_plan_update`、`record_observation`、`propose_fact`、`propose_hypothesis`、`record_attempt`、`propose_finding`、`record_negative_result` 与 `query_reasoning_graph`；
  - Runtime 注入 Run、Session、creator、Node kind 与初始 status；Observation、Fact/Vulnerability Candidate 和 Negative Result 必须绑定 Evidence，Hypothesis 无 Evidence 时固定为 unverified；模型传入 `status`、`run_id`、`session_id` 或 creator 等未知字段会被严格 Schema 拒绝；
  - Proposal 写入继续调用 COG-202 `ReasoningGraphApplicationService`，因此跨 Run、Sibling Session、其他 Task Evidence、陈旧 Graph version 和非法状态晋升均失败关闭；Confirmed Finding 仍只能通过内部原子晋升并满足 Evidence/Reproduction Contract；
  - 八个工具属于 Primary resident set，不进入 Subagent resident set；Proposal/Record 为 `DURABLE_WRITE + RUN_CONTEXT`，查询为 `READ_ONLY + RUN_CONTEXT`，不新增外部副作用审批；
  - Temporal Worker 注入真实 Working Memory Proposal 与 Reasoning Graph Application Service。SDK Agent Factory 删除手写 Control Tool 白名单，改由权威 `RESIDENT_TOOL_IDS` 派生，避免 Tool Discovery 与模型绑定再次漂移。
- Checks：
  - Working Memory Proposal 与 Reasoning Application 定向回归：`12 passed`；
  - Runtime Control Tools、Tool Discovery/Policy 与 Worker 定向回归：`76 passed`；
  - Runtime/Context gate、Reasoning/Working Memory 关联回归：`24 passed`；
  - SDK Agent Factory 根因修复关联回归：`49 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/application/services/working_memory.py src/riftx/application/services/reasoning.py src/riftx/tools/discovery.py src/riftx/tools/policy.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5141 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - 全仓 Ruff、`git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`a8dbdf50`、`87c7381d`、`d369b684`。

### COG-204：Observer Supervisor 与 Projector

- Status：completed
- Started：2026-08-06
- Inputs：COG-203、Working Memory、Task/Reasoning/Evidence Graph、Runtime Event、Tool Intent、Approval、User Input、Takeover、Graph/Report Application Service 与生产 Temporal Worker。
- Supervisor slice：
  - 新增确定性 Observer Supervisor，统一检查 Scope、Approval、重复尝试、Evidence 缺失、Capability mismatch、Budget、死循环和用户输入/人工接管；输出稳定 `CONTINUE`、`YIELD` 或 `BLOCK`，不调用模型；
  - 检查只消费既有权威状态的有界快照，不创建 Observer 数据表或第二套授权、预算、任务、证据和接管状态；
  - 新增 Engine Event、Run Event、Takeover 与 Intent 的 bounded history read，快照收集并发执行，缺失或不一致的安全关键输入失败关闭。
- Runtime slice：
  - Runtime Coordinator 在模型调用前和 Tool Intent 持久化后执行 Observer；BLOCK 转为 `FATAL_FAILURE`，YIELD 复用既有 durable waiting object；
  - Approval/User Input YIELD 保留真实 Approval Request/User Input Request ID，避免只暂停而无法恢复；
  - 每次检查写入脱敏 `runtime.observer_inspected`，只记录 phase、disposition、稳定 reason/check code 和有界计数，不写 Prompt、Tool arguments、Evidence 内容或 Secret；
  - 生产 Temporal Worker 装配真实 Observer Application Service。
- Projector/API slice：
  - 新增只读 Observer Projector，复用授权 `GraphApplicationService` 与 `ReportApplicationService` 输出 Task、Reasoning、Evidence、Attack、Code、Operation、Coverage、Timeline 和可再生成 Report draft；
  - Reasoning/Attack Graph 是 Evidence Graph 的有界派生切片，不成为新权威图；Code Graph 在 AUD 权威来源未接入前稳定返回 partial reason `code_graph_authoritative_source_unavailable`；
  - 新增 `GET /api/v1/runs/{run_id}/projection`，复用 API Principal/Run owner 授权，路由进入 fail-closed API policy 和 RunKind effect inventory；
  - Projector 不持久化派生结果，Report draft 继续可从权威来源重新生成。
- Safety boundaries：
  - Observer 不授予 Scope、Approval、Capability 或 Budget，只能在已有状态上继续、暂停或阻断；
  - 任何缺失的授权服务、Run owner、图来源或安全关键快照不会回退为允许；
  - Projection 只暴露有界、脱敏的应用层视图，不暴露 Evidence 原文、Tool arguments、Prompt、Credential 或本地绝对路径。
- Checks：
  - Observer Supervisor/Application/Projector、bounded repository、Runtime Coordinator/Deferred Runtime、Projection API、API Policy、RunKind Effect Policy 与生产 Worker 定向回归：passed；
  - `conda run --no-capture-output -n agent python -m mypy ...`（COG-204 关联模块）：`Success: no issues found`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5157 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`16a1d800`、`a03654e0`、`21e28b3e`、`de863606`、`7a70ef6f`、`465ea1f0`、`65f12b02`；cleanup commit：`654a72bd`。

### COG-205：Closure Verifier

- Status：completed
- Started：2026-08-06
- Inputs：COG-204、Run Success Criteria、Task Graph/Attempt/Evidence Requirement、Reasoning Graph、Evidence Ledger、Report Source、Run finalization fence 与三类物理资源停止证明。
- Success Criterion mapping slice：
  - `TaskEvidenceRequirement` 新增显式 `success_criterion_index`，Planner 校验索引必须落在当前 Run Success Criteria 范围内；Criterion 不再依赖描述文本、Task 顺序或模型推断进行关联；
  - Alembic revision `3c6e8a1f2b40` 为既有 Requirement 增加可兼容读取的映射字段，并继续在自身 DDL 前执行跨历史版本数据丢失保护；
  - Requirement 仍引用现有 Evidence Ledger ID，不复制 Evidence 内容，也不建立 Criterion completion 表。
- Deterministic verifier slice：
  - 新增只读 `ClosureVerifierApplicationService`，从 Run、Task Graph、Reasoning Graph 与 Evidence Ledger 生成确定性的 `complete` 或 `partial` Closure Report；
  - 必需 Success Criterion 必须存在显式 Requirement 映射，Requirement 已满足，且全部 Evidence ID 在当前 Run 的 Ledger 中真实存在；可选 Criterion 不阻断整体 Closure，但仍保留自身未满足原因；
  - Pending Task 使用 `stop_condition`，Blocked Task 使用 `blocked_reason`，Failed Task 使用最新失败 Attempt，Cancelled Task 使用取消历史解释；缺少解释的非完成 Task 使 Closure 降级为 partial；
  - Confirmed Finding 的全部 Evidence 必须存在且 `replayable=True`；缺失或不可重放时降级为 partial；
  - Closure Report Digest 和 Event ID 由规范化报告确定性生成，重试不会产生重复 Event；Event 只保存版本、Graph version、计数、稳定 reason code 与报告 Digest，不保存 Criterion 文本、Finding claim、Evidence 内容、Prompt 或 Secret。
- Completion and report slice：
  - Temporal Completion Activity 和 standalone/legacy Runtime Coordinator 均在既有 `COMPLETING` admission fence 后写入 `run.closure_evaluated`，随后调用现有 `RunSafetyStopService`；只有 Execution、Browser Session 与 Target HTTP Request 全部取得停止确认后才提交 `COMPLETED`；
  - Closure `partial` 不伪造新的 Run 终态：物理停止成功后 Run 仍按既有生命周期完成，但 Report Source、Markdown、HTML 和 JSON 明确展示 partial 与 reason codes；
  - 待处理用户消息使 completion fence 失败时，不提前生成 Closure Event；物理停止失败时 Run 保持 `COMPLETING`，重试复用相同 Closure Event 和 Digest；
  - 生产 Temporal Worker 创建一个真实 Verifier 实例，同时注入 Runtime Coordinator 与 RiftX Activities；没有新增 Closure Repository、表、状态机或第二套 Completion authority；
  - 旧 Run 没有 Closure Event 时报告稳定降级为 `partial / closure_verification_missing`；非法 complete/reason 组合和未说明的 partial 均 fail-closed 为稳定报告 reason；
  - 同步修复 legacy `cancel_current_execution` Activity 遗留的空方法调用，改为复用现有三类资源 Safety Stop 并在停止无法确认时阻止 Agent 继续产生效果。
- Safety boundaries：
  - Closure 只判断工作是否具备可审查证据，不授予 Scope、Approval、Capability、Budget 或物理停止证明；
  - Closure partial 与 Run physical completion 正交：前者必须被报告，后者必须由现有 Safety Stop gate 独立证明；
  - Closure Event 可公开字段经过 Report Event 白名单过滤，原始 Graph claim、Evidence 内容与内部解释不会进入 Event payload。
- Checks：
  - Closure、Report、Temporal Activity/Workflow、Runtime Coordinator、生产 Worker、Observer Projection 与完整 Control Plane 生命周期定向回归：`106 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/application/services/closure.py src/riftx/application/services/reports.py src/riftx/temporal/activities.py src/riftx/runtime/coordinator.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent alembic heads`：`3c6e8a1f2b40 (head)`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5165 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`7849cb2b`、`f09ace2a`、`dc2099a0`。

### PACK-300：基础渗透 Packs

- Status：completed
- Started：2026-08-06
- Inputs：CAP-102、CAP-104、COG-205、既有 Capability/Pack persistence、Progressive Skill、Technique selection、Tool Policy、Scope/Approval、Evidence Ledger 与 Closure Verifier。
- Official bundle catalog slice：
  - 新增严格 `OfficialPackCatalog` 和发行物源码契约；每个 Bundle 必须包含 Pack Manifest、Skill、Technique、Eval Case、Tool requirements、Evidence contract、negative cases、changelog 与可选 JSON Schema；
  - Skill Capability provenance 绑定完整 Progressive Skill package digest，Technique/Eval Case 与 Pack provenance 绑定完整 Bundle digest；Pack member 精确锁定 Capability version/digest；
  - Bundle 拒绝 Symlink、特殊文件、超限资产、重复 ID、Skill source 伪造、未知生产 Tool、Tool dependency 漂移、Eval/negative 引用漂移、Evidence contract 缺失与 Changelog 版本缺失；
  - setuptools package data 已包含 Official YAML、Markdown 与 JSON Schema，最终 wheel 验证包含 10 个 Pack 的 80 个发行资产。
- Production bootstrap slice：
  - Worker 在 Schema 初始化后复用 `SQLAlchemyCapabilityRepository` 幂等注册 30 个 Capability Version、10 个 Capability Pack、10 个 Official install 和 30 个精确 PackLock；
  - 稳定 UUID、immutable manifest/digest 和既有 Repository 冲突语义使重复启动不产生重复状态，Manifest 或 Skill package 漂移失败关闭；
  - `TechniqueContextManager` 直接从既有 Active Capability Version 读取 10 个 Official Technique，不建立第二套 Pack Registry 或 Runtime authority。
- Skill layering slice：
  - 现有 Progressive Skill Registry 支持多根目录和显式优先级：Official roots 为低优先级，配置的 Operator root 为高优先级；
  - 同优先级重复 Skill ID 失败关闭；Operator 同 ID Skill 可显式覆盖 Official，但必须声明 `source=operator`，不能借覆盖伪造 Official provenance；
  - Worker 默认暴露 10 个 Official Skill；运行中 Session 保留原 Skill document/reference/version/digest，磁盘或 overlay 更新只标记 stale，必须显式 reload；现有持久选择和 Subagent allowlist 继续生效。
- Delivered Packs：
  - `pentest-foundation`、`scope-and-safety`、`passive-recon`、`service-enumeration`、`web-attack-surface`；
  - `web-request-analysis`、`vulnerability-verification`、`evidence-and-reporting`、`negative-results`、`credential-handling`。
- Safety boundaries：
  - Pack 和 Skill 只提供可版本化的专业程序、证据要求与负向纪律，不授予或扩大 Tool、Scope、Approval、Credential、Budget 或 Run lifecycle 权限；
  - target interaction、external service 与 credential access 继续通过生产 Tool Policy、durable approval、Scope guard、Credential Reference、Artifact redaction 和 physical stop gate；
  - Scanner、外部研究、版本匹配、模型置信度和报告文本不能代替直接可重放 Evidence 或 Finding promotion gate。
- Checks：
  - Catalog、Capability Repository、Bootstrap、Technique/Skill selection、Operator overlay/source spoof、Session pin/stale、Subagent allowlist 与生产 Worker 定向回归：passed；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/packs src/riftx/skills/progressive.py src/riftx/skills/registry.py src/riftx/skills/__init__.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack-wheel-pack300`：passed，10 个 Pack 的 80 个发行资产存在；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5176 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`5e56682e`、`89d43498`、`128f8ae1`、`b87305d9`、`c095ae7f`。

### PACK-301：基础代码审计 Packs

- Status：completed
- Started：2026-08-06
- Inputs：CAP-101、CAP-104、COG-205、PACK-300 的 Official Pack Catalog/Bootstrap、Capability Repository、Progressive Skill、Technique Context、Tool Policy、Evidence Ledger 与 Closure Verifier。
- First delivery slice：
  - 新增 `code-audit-foundation`，把 owner-bound Workspace/Audit Snapshot、最小覆盖 Task、Evidence、Hypothesis、Negative Result 与 Closure 组织成基础代码审计循环；
  - 新增 `repository-mapping`，区分 first-party、generated、vendored、test、configuration、deployment 与 unknown/truncated 区域，并形成语言、框架、子系统和信任边界地图；
  - 新增 `entrypoint-discovery`，以注册点、配置、符号、引用和调用关系识别外部输入、事件、任务、CLI 与高权限入口，名称匹配本身不能证明可达性；
  - 每个 Pack 均包含 Capability Manifest、Skill、Technique、精确 Tool requirements、Evidence contract、两个 Negative Case、一个 Eval Case、输入/输出 Schema 和 Changelog；
  - Catalog 自动发现后生产 bootstrap 从 10 Pack/30 Capability 扩展为 13 Pack/39 Capability，继续使用稳定 ID、immutable digest、Official install 与精确 Pack Lock；Worker 直接暴露新增 Skill/Technique，无新 Registry、数据库表或 Runtime authority；
  - wheel 中 Official Pack 发行资产从 80 个扩展为 104 个。
- Second delivery slice：
  - 新增 `authn-authz-audit`，从受保护入口追踪身份、Session、角色、租户、对象、动作与权威 Enforcement；注解、Middleware 名称或可控 ID 本身不能证明控制缺失；
  - 新增 `injection-audit`，覆盖 Query、Command、Template、Expression、Header、Log 与 Interpreter Source-to-Sink，要求攻击者控制、可达转换、Sink 语义、上下文防御与影响证据；
  - 新增 `secret-and-config-audit`，区分真实秘密、Placeholder、Test Fixture、Public Identifier、Encrypted Blob 与无效配置，并追踪 Loader、Precedence、Fallback、Consumer 和部署可达性；
  - 原始 Credential、Token、Cookie、Private Key 或 Recovery Material 不得进入 Evidence、Memory、Finding、日志或报告；无法安全脱敏时停止处理值，仅保留位置和稳定原因；
  - Catalog/Bootstrap 扩展为 16 Pack/48 Capability，wheel 扩展为 128 个 Official Pack 发行资产；仍无新 Registry、数据库表、运行时权限或外部执行能力。
- Third delivery slice：
  - 新增 `dependency-and-supply-chain`，离线核对 Manifest、Lock、Source、Integrity、Install Hook、Build Input 与生产 Consumer；模型记忆、包名或版本相似性不能作为 CVE 或受影响范围证据；
  - 新增 `file-upload-and-path-audit`，追踪文件内容、元数据、名称、Archive Entry 和路径到存储、提取、读取、写入、删除、Serving 或执行 Sink，并验证解码、规范化、最终路径 containment、Link/Race 与有效 Root；
  - 新增 `ssrf-and-outbound-request-audit`，分别追踪 Scheme、Authority、Host、Port、Path、Redirect、Proxy、Resolved Address、Credential 与 Response 使用；可控 Body/Header 或固定主机 Path 不等于 SSRF；
  - 三个 Pack 均不执行 Installer/Build/File Operation/Archive Extraction，也不发送网络请求、解析在线 DNS、查询 Registry 或访问 Credential；
  - Catalog/Bootstrap 扩展为 19 Pack/57 Capability，wheel 扩展为 152 个 Official Pack 发行资产。
- Fourth delivery slice：
  - 新增 `deserialization-audit`，追踪不可信结构化输入、Parser Mode、Type Resolution、Object Construction、Lifecycle Hook、Gadget、Side Effect 与 Integrity/Allowlist；格式或 Parser 名称本身不能证明危险反序列化；
  - 新增 `finding-verification`，从权威 Reasoning Graph 和 owner-bound Source 独立重建 Candidate，检查攻击者控制、每条可达边、防御、反证、影响、Identity 与 Replay；`propose_finding` 只能形成 Candidate，不能写入 Confirmed 状态；
  - 新增 `variant-analysis`，从已验证 Seed 提取 Root-cause Invariant，组合 Lexical/Symbol/Reference/Caller/Flow/Config/Defense 搜索，并对每个 Candidate 独立验证，禁止复制 Seed Evidence；
  - Catalog/Bootstrap 最终扩展为 22 Pack/66 Capability；正式计划中的 12 个代码审计 Pack 每个均包含 Manifest、Skill、Technique、Tool requirements、Evidence contract、Negative cases、Eval Case、Schema 与 Changelog，共 96 个发行资产；
  - wheel 最终包含 22 个 Official Pack 的 176 个发行资产，生产 Worker 暴露全部 22 个 Skill 与 Technique。
- Safety boundaries：
  - 每个代码审计 Pack 的工具集合由测试精确锁定，只包含 owner-bound `list_files`/`glob`/`grep`/`read_many_files`、语义导航、Task/Reasoning/Evidence/Closure 工具；
  - 不依赖 Shell、目标项目执行、Patch/Revert、Worktree、Browser、Web、Target HTTP、Credential 或外部服务；
  - Skill/Technique 只允许无需批准、无需 Target Scope 的本地认知状态写入，Eval Case 为只读；Pack 不扩大生产 Tool、Scope、Approval、Credential、Budget 或 Run lifecycle 权限；
  - Source comment、框架命名、Scanner/模型置信度和 name-only candidate 不能替代 owner-bound Source、Reachability、Input/Privilege Boundary 与 replayable Evidence。
- Checks：
  - Catalog、Bootstrap、Capability persistence、Worker Skill/Technique 暴露与安全契约定向回归：`22 passed`；
  - Pack、Skill、Capability 与生产 Worker 关联回归：`49 passed`；收紧精确工具和权限断言后 Catalog 回归：`6 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/packs src/riftx/skills/progressive.py src/riftx/skills/registry.py src/riftx/skills/__init__.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack301-wheel.g6tBa8`：passed，13 个 Pack 的 104 个发行资产存在；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5206 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- First slice implementation commit：`4f74479d`。
- Second slice checks：
  - 测试先行缺失资产验证：`4 failed, 18 passed`，失败仅为三个 Pack 尚不存在；资产接入后定向回归：`22 passed`；
  - Pack、Skill、Capability 与生产 Worker 关联回归：`49 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/packs src/riftx/skills/progressive.py src/riftx/skills/registry.py src/riftx/skills/__init__.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack301-slice2-wheel.Mppx2C`：passed，16 个 Pack 的 128 个发行资产存在；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5206 passed, 5 skipped, 17 warnings`；跳过和警告原因与第一切片一致；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Third slice checks：
  - 测试先行缺失资产验证：`4 failed, 18 passed`，失败仅为三个 Pack 尚不存在；资产接入后定向回归：`22 passed`；
  - Pack、Skill、Capability 与生产 Worker 关联回归：`49 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/packs src/riftx/skills/progressive.py src/riftx/skills/registry.py src/riftx/skills/__init__.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack301-slice3-wheel.4Eidio`：passed，19 个 Pack 的 152 个发行资产存在；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5206 passed, 5 skipped, 17 warnings`；跳过和警告原因与前两切片一致；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Fourth slice checks：
  - 测试先行缺失资产验证：`4 failed, 18 passed`，失败仅为三个 Pack 尚不存在；资产接入后定向回归：`22 passed`；
  - Pack、Skill、Capability 与生产 Worker 关联回归：`49 passed`；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/packs src/riftx/skills/progressive.py src/riftx/skills/registry.py src/riftx/skills/__init__.py src/riftx/temporal/worker_runtime.py`：`Success: no issues found in 7 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack301-final-wheel.76zz69`：passed，22 个 Pack 的 176 个发行资产存在；
  - `conda run --no-capture-output -n agent python -m pytest -q`：`5206 passed, 5 skipped, 17 warnings`；跳过和警告原因与前三切片一致；
  - 12 个计划代码审计 Pack 的 96 个 `pack.yaml`、Eval、Negative、Changelog、Skill、References 和输入/输出 Schema 文件逐项存在；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`4f74479d`、`81574f56`、`0237a0cb`、`8b1cea9b`。
- Completion：正式文档列出的 12 个基础代码审计 Pack 已全部进入 Official Catalog、Capability persistence、生产 Worker、Session pinning 与 wheel 发行链路；无剩余 Pack。

### PACK-302：Onboard 和 Doctor

- Status：completed
- Started：2026-08-06
- Inputs：PACK-300/PACK-301 Official Pack、严格 RiftX/Model/Tool 配置加载器、Progressive Skill Registry、MCP/LSP 配置、Runner/Workspace/Audit Storage 与既有 `riftx tools doctor`。
- Offline Doctor slice：
  - 新增结构化 `DoctorStatus`、`DoctorCheck` 和 `DoctorReport`，状态固定为 `ready`、`degraded` 或 `failed`；13 个检查 ID 稳定覆盖正式计划要求的全部 Doctor 类别；
  - 顶级 `riftx doctor` 只读运行并使用 Rich 表格展示 Detail 和 Remediation；存在 `failed` 时退出 1，仅有 `degraded` 时退出 0，避免把可选组件缺失误判为产品不可用；
  - Model 检查复用现有 `load_models_config`，验证选定 Profile 与 Credential Reference；Tool 检查复用 `load_tool_config`；Official Pack/Skill 检查复用 `OfficialPackCatalog` 与 `ProgressiveSkillRegistry`，不建立第二套 Catalog；
  - Temporal、Runner、Browser、Tool version、Skill runtime dependency、MCP discovery、LSP handshake、Pack DB Lock 和数据库 Alembic revision 在离线切片中不冒充已验证，统一返回带降级路径的 `degraded`；
  - 已启用 LSP 的 Socket/Credential 缺失、MCP Credential 缺失、Temporal TLS 文件缺失、无效 Model/Tool/Skill/Pack、不可用 Storage 或数据库路径确定性失败关闭；LSP disabled 时明确保留 `builtin_static` 降级路径；
  - Scanner 明确说明 built-in static 可用、可选 Adapter 尚未配置；Backup/Restore 明确标记尚不可验证，不伪造 ready；
  - 本切片不暴露空壳 `--fix`，不创建目录、不写配置、不初始化数据库、不连接外部服务；待修复操作具备原子备份、权限边界和回滚语义后再开放。
- Live probe slice：
  - `APIClient` 新增只读 `/healthz` 调用；顶级 Doctor 使用 3 秒有界超时，先保留离线报告，再通过 Control Plane、配置的 Runner Node 与公开 Tool Registry 覆盖可证明的 live 状态；
  - Control Plane 不可达或返回非 ready health 时，Runner 检查失败关闭并保留其他离线诊断，不丢失 Model、Pack、Skill、Storage 等本地证据；
  - 在线 Runner heartbeat 晋升 Runner 为 ready；`degraded` 保留降级状态，offline/lost/unknown 失败关闭；在线 `worker-local` Node 作为当前 Temporal Worker 连通性的 live proof；
  - Runner 广告 `browser_playwright` 时 Browser 晋升 ready，否则保留 built-in/未探测降级，Doctor 不尝试创建 Browser Session；
  - Tool live 检查只读取公开 Registry：已启用 Tool availability 非 available 时失败，声明 version probe 但无 version 时降级，全部可用且所需 version 已解析时 ready；不调用有写语义的 refresh endpoint；
  - 配置启用 MCP 时复用 Worker heartbeat 的 refresh、unavailable Server 与 open circuit 标签；discovery current 且无 unavailable/open circuit 才 ready，Server 不可用失败，标签不完整或 Circuit open 降级；未配置 MCP 时继续使用 built-in Tool 降级路径；
  - 同步收紧 CLI Client 内部 request kwargs 类型为 `Any`，使实际 httpx 调用边界通过 scoped mypy，不改变请求行为。
- System diagnostics slice：
  - 新增本地操作员只读 `GET /api/v1/system/diagnostics`，复用现有 Database Session、Official Pack Catalog、Capability Repository 和 Pack Lock，不建设第二套权威状态；
  - Database migration 诊断返回 `ready`、`unmanaged` 或 `mismatch`，并报告内嵌 expected revision 与当前 revision set；测试校验内嵌 head `3c6e8a1f2b40` 与真实 Alembic migration graph 一致；
  - Official Pack 诊断校验 22 个 Pack install 的 version ID/version/digest、persisted manifest digest，以及每个 Pack 完整的 capability active lock set，正常状态总计 66 个 active lock；missing、unexpected 或 digest/lock drift 均返回有界 issue code；
  - Doctor live overlay 将 revision 匹配与 Pack install/lock/digest 完整性晋升为 ready；`unmanaged`、`mismatch` 或 Pack drift 失败关闭，数据库修复明确要求先备份再迁移；
  - 新接口已登记 API policy 与 RunKind effect inventory，效果固定为 `GLOBAL` / `READ_ONLY` / `NOT_RUN_SCOPED`。
- Local fix slice：
  - 顶级 `riftx doctor --fix` 先运行本地 Doctor，只对报告中明确 `fixable` 且已登记安全处理器的问题动作；修复后重新运行本地与 live Doctor，退出码仍由复检结果决定；
  - 当前处理器仅初始化缺失的 Operator Skill root、Workspace root 和已启用 Audit 的 Snapshot/Temp/Fix root；所有新建目录均使用 owner-only `0700`；
  - 目录遍历使用 POSIX `dir_fd`、`O_DIRECTORY` 与 `O_NOFOLLOW`，拒绝符号链接、非目录组件、非当前用户/非 root 所有者以及不安全可写祖先；
  - 整个修复批次记录新建目录的 parent/child descriptor 与 inode identity；任一创建失败时按逆序删除本批次空目录，identity drift 或回滚不完整则显式失败，不删除无法证明归属的路径；
  - Database migration、Official Pack reinstall 与配置文件迁移在具备写前备份/回滚处理器之前不再标记为 `fixable`，防止 CLI 暗示尚未存在的自动修复能力。
- SQLite migration fix slice：
  - 新增只读 `inspect_sqlite_migration`，区分 `missing`、`empty`、`ready`、`mismatch`、`unmanaged` 与 `invalid`；仅 file-backed SQLite 的 missing/empty 或已有 Alembic 管理的 mismatch 可自动修复，非 SQLite、内存库、损坏库与 unmanaged 旧 schema 不自动推测迁移；
  - `doctor --fix` 对待修复数据库先探测配置的 Control Plane；只要服务可达就拒绝迁移并要求停服，不让运行中进程继续持有旧 schema；
  - 现有数据库在写入前使用 SQLite Backup API 生成一致性快照，备份目录为 `0700`、文件为 `0600`，通过 `integrity_check` 与 fsync 后才允许迁移；
  - 迁移连接切换 SQLite `locking_mode=EXCLUSIVE`，在同一连接上注入 Alembic environment 并执行唯一 migration graph；迁移异常或 head 复检失败时，已有库从备份原子恢复，新库在 inode identity 仍匹配时删除，回滚不完整显式失败；
  - 原有 Doctor 目录安全原语抽取为 `OwnerDirectoryBatch`，数据库修复失败时仍可回滚同批次新建的 Skill/Storage 目录；
  - `alembic.ini`、`env.py`、`script.py.mako` 和 49 个 migration version 使用 setuptools data-files 进入 wheel；源码和安装版都解析同一套资产，不生成第二份 schema 定义。
- Official Pack drift fix slice：
  - `bootstrap_official_packs` 改为调用 `SQLAlchemyCapabilityRepository` 的整批对账入口；Catalog 在事务外完整加载，66 个 Capability Version、22 个 Pack、22 个 install 与 66 个 active lock 在单次 `serialized_write` 中注册、校验和修复，避免逐 Pack 提交形成半安装状态；
  - Capability Version、Pack Manifest 和 Pack Member 继续作为不可变历史权威；任一内容、Digest、状态或成员漂移均拒绝自动覆盖并回滚整批事务；Official scope 出现未被 Catalog 声明的 install 同样失败关闭；
  - 仅 `missing_install`、`install_drift`、`lock_set_drift` 和 `lock_digest_drift` 属于可重建投影；修复会规范化 install、递增 `state_version`、release 错误 active lock 并创建新的正确 lock，不删除或复活历史 released lock，也不修改 Run Session lock；
  - 离线 Doctor 在 file-backed SQLite 已到 Alembic head 时复用 `Database`、`SystemDiagnosticsService`、Official Pack Catalog 与 Capability Repository 读取权威状态；可修复漂移才标记 `fixable`，unexpected install、Pack/Version/Member immutable drift 明确要求可信恢复；
  - `doctor --fix` 将目录、SQLite migration、Official Pack repair 按顺序执行；数据库或 Pack 持久化修复前统一探测 Control Plane，只要服务可达就拒绝动作；Pack 修复提交后再次运行权威 diagnostics，只有 22 个 install 与 66 个 active lock 全部 ready 才算成功。
- Runtime configuration migration slice：
  - 新增只读 `inspect_runtime_config_migration`，只把与 `AuditSourceIngestConfig()` 完全一致、且可用 YAML Node 行标记精确删除的退役 `audit.source_ingest` 标记为 `migratable`；任何自定义 image digest、资源限制、字段或无法无损编辑的内联布局均标记为 `manual`，不推测操作员意图；
  - `repair_runtime_config` 不重写整份 YAML，只删除目标键值行段与历史示例中精确匹配的两行说明，迁移前后以解析结果证明除 `audit.source_ingest` 外无字段变化，并在第二次读取后重新验证默认值以关闭陈旧检查窗口；
  - 配置读取限制为 1 MiB、当前用户所有的普通文件，路径遍历拒绝任意符号链接组件；写入前生成 `0700` 目录中的 owner-only `0600` 精确字节备份，临时文件使用 `O_EXCL`/`O_NOFOLLOW`，保留原文件 mode，并以 inode identity、原子 `os.replace` 与文件/目录 fsync 完成提交；
  - 迁移后重新运行权威 inspection，未达到 `ready` 时使用原始字节原子恢复；恢复后保留备份，identity drift 或恢复失败显式报告 rollback incomplete；
  - Doctor 新增稳定 `config_migrations` 检查：无选定文件或无需迁移时 ready，精确旧默认 degraded/fixable，自定义旧配置 failed/manual；配置写修复与数据库/Pack 一样要求 Control Plane 不可达，修复结果记录配置与备份路径并复检 ready；
  - CLI 仅选择显式 `--config`、`RIFTX_CONFIG` 或已存在的默认用户配置作为迁移目标，不默认自动修改 `/etc/riftx/riftx.yaml`，继续复用现有分层配置加载器而不建立第二套状态。
- Local onboarding slice：
  - 新增交互式与 `--non-interactive` 顶级 `riftx onboard`；支持 OpenAI 或 OpenAI-compatible 主模型、request mode、base URL、`RIFTX_MODEL_*` Credential Reference、无 API Key 本地模型、自定义 Workspace 与用户配置路径，不接受明文 API Key 参数，也不把 Credential 写入 YAML；
  - 首次运行生成现有 `RiftXConfig`、`ModelsConfig` 与 `ToolRegistryConfig` 可直接加载的 `riftx.yaml`、`models.yaml` 和 `tools.yaml`，所有路径规范化为用户级 XDG Config/State/Data 绝对路径；Runtime DB、Runner state、Credential store、Workspace、Operator Skills 与 Audit staging 不再依赖仓库当前目录；
  - Onboard 复用权威 `configs/tools.yaml`，按当前 `PATH` 检测每个已启用工具的入口；缺失 executable 的可选工具只在新用户副本中禁用并明确输出降级列表，不修改发行模板，也不把缺失工具误报为可用；同一模板通过 setuptools data-files 进入 wheel；
  - 所有新目录由 `OwnerDirectoryBatch` 以 owner-only `0700` 创建并拒绝符号链接或不安全祖先；三个配置文件以 `O_EXCL`/`O_NOFOLLOW`、`0600`、文件 fsync 与目录 fsync 创建，Runtime config 最后发布；任一写入或 fsync 失败时按 inode identity 删除本次文件并回滚新目录，回滚不完整显式失败；
  - 任何目标配置、Model config 或 Tool config 已存在时均拒绝首次写入；再次执行 Onboard 只验证并复用当前用户拥有、无符号链接的既有 Runtime config，不根据新的 onboarding 参数覆盖现有配置；local onboarding 拒绝非 loopback Control Plane；
  - 生成或恢复配置后直接复用 `run_local_doctor` 与 `apply_local_doctor_fixes`；仅在 Control Plane 不可达时初始化缺失目录、SQLite schema 与 Official Pack persistence，服务可达或状态不确定时沿用 Doctor 的停服失败边界；最终展示完整 Doctor 报告、缺失工具和 Credential/管理员 Token 后续动作。
- Safety boundaries：
  - 默认 `riftx doctor` 只读取已有配置、Pack/Skill 发行资产、环境变量是否存在和本地路径/数据库元数据；不输出 Credential 值，不调用目标、不启动 Runner/Browser/MCP/LSP/Scanner，不改变数据库或文件权限；只有显式 `--fix` 才进入已登记的本地修复处理器；
  - `degraded` 表示存在明确降级路径或缺少 live proof，不能被上层解释为 ready；`failed` 只用于已启用或基础必需组件的确定性不可用状态；
  - Pack Digest 继续由 Official Catalog 权威计算，Skill 文档继续由 Progressive Skill Registry 权威解析，Doctor 不持久化第二份健康状态。
- Checks：
  - 测试先行验证：实现前定向测试以 `ModuleNotFoundError: riftx.doctor` 产生 2 个预期收集错误；
  - Doctor 合同与顶级 CLI 定向回归：`7 passed, 55 deselected`；
  - CLI、Runtime Config、Model/Tool Config、Pack Catalog 与 Progressive Skill 关联回归：`143 passed`；
  - `conda run --no-capture-output -n agent env RIFTX_MODEL_API_KEY=doctor-smoke riftx doctor`：退出 0，实际加载 22 个 Official Pack 并输出 13 类 `ready/degraded` 检查；
  - `conda run --no-capture-output -n agent mypy src/riftx/doctor.py src/riftx/cli/render.py`：`Success: no issues found in 2 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5212 passed, 5 skipped, 17 warnings`；跳过项仅涉及当前主机不具备 Windows、PowerShell 或 delegated cgroup 条件，警告为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Live probe checks：
  - 测试先行验证：`run_live_doctor` 尚不存在时产生预期 ImportError；实现后 Doctor/CLI Client/CLI App 关联回归：`83 passed`；
  - `conda run --no-capture-output -n agent env RIFTX_MODEL_API_KEY=doctor-smoke riftx --api-url http://127.0.0.1:9 doctor`：显示完整表格、Runner `failed`、Overall `failed` 并以 1 退出；
  - `conda run --no-capture-output -n agent mypy src/riftx/doctor.py src/riftx/cli/client.py src/riftx/cli/render.py`：`Success: no issues found in 3 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5217 passed, 5 skipped, 17 warnings`；跳过和警告原因与 Offline Doctor slice 一致；
  - `git diff --check` 和 staged `git diff --check`：passed。
- System diagnostics checks：
  - 首次全仓回归发现 `get_system_diagnostics` 未登记 RunKind API effect inventory；补齐并验证 `GLOBAL` / `READ_ONLY` / `NOT_RUN_SCOPED` 后，关联回归 `126 passed`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent mypy src/riftx/diagnostics.py src/riftx/api/routes/system.py src/riftx/api/dependencies.py src/riftx/doctor.py src/riftx/cli/client.py`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent pytest -q`：`5223 passed, 5 skipped, 17 warnings`；跳过和警告原因与 Offline Doctor slice 一致；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Local fix checks：
  - 测试先行验证：`DoctorFix`、`DoctorFixError` 和 `apply_local_doctor_fixes` 尚不存在时产生 2 个预期 ImportError；实现后本地修复与顶级 CLI 定向回归 `6 passed, 64 deselected`；
  - Doctor 和 CLI 关联回归：`104 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/doctor.py`：`Success: no issues found in 1 source file`；`src/riftx/cli/app.py` 单文件 mypy 仍命中既有 `_AuditGroup` Typer/Click override 的 6 个类型错误，本切片未修改该边界；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5227 passed, 5 skipped, 18 warnings`；5 个跳过原因不变，17 个 SQLite datetime adapter 警告与 1 个既有 Pydantic `Field(alias=...)` schema 警告均与本切片无关；
  - `git diff --check` 和 staged `git diff --check`：passed。
- SQLite migration fix checks：
  - 测试先行验证：`riftx.database_maintenance` 尚不存在时产生 3 个预期收集错误；实现后备份/迁移/恢复、Doctor 和 CLI 定向回归 `15 passed, 64 deselected`；
  - Database Maintenance、真实 Alembic parent-to-head、Runtime Migration、主迁移链、System Diagnostics、Doctor 与 CLI 关联回归：`116 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/database_maintenance.py src/riftx/local_fs.py src/riftx/doctor.py`：`Success: no issues found in 3 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack302-db-wheel`：passed；wheel 包含 49 个 migration version 和合计 52 个 Alembic 资产；
  - 首次全仓回归为 `5236 passed, 5 skipped, 17 warnings, 1 failed`，唯一失败是既有 Runner shell exec-replacement 2 秒观测超时；该用例独立连续 3 次均通过；
  - 二次 `conda run --no-capture-output -n agent pytest -q`：`5237 passed, 5 skipped, 18 warnings`；5 个跳过原因不变，17 个 SQLite datetime adapter 和 1 个既有 Pydantic schema 警告与本切片无关；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Official Pack drift fix checks：
  - 测试先行验证：新增定向合同在旧逐 Pack bootstrap 下产生 `5 failed, 3 passed`，失败分别证明 mutable drift 无修复、unexpected install 未拒绝、immutable drift 前发生部分提交，以及 diagnostics 未分类 Version/Member 漂移；
  - Pack Bootstrap/Repair、System Diagnostics、Doctor、CLI 与 Database Maintenance 关联回归：`90 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/doctor.py src/riftx/diagnostics.py src/riftx/packs`：`Success: no issues found in 5 source files`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5243 passed, 5 skipped, 17 warnings`；5 个跳过仍仅为 Windows、PowerShell 或 delegated cgroup 主机条件，17 个警告仍为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Runtime configuration migration checks：
  - 测试先行验证：新增配置维护合同在实现前以 `ModuleNotFoundError: riftx.config_maintenance` 产生预期收集错误；
  - Config Maintenance、Doctor、Runtime/Audit Config 与 CLI 关联回归：`241 passed`；最终 Config Maintenance、Doctor 与 CLI 定向回归：`82 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/config_maintenance.py src/riftx/doctor.py src/riftx/config.py`：`Success: no issues found in 3 source files`；`src/riftx/cli/app.py` 仍仅命中既有 `_AuditGroup` Typer/Click override 的 6 个类型错误，本切片未修改该边界；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5252 passed, 5 skipped, 17 warnings`；5 个跳过仍仅为 Windows、PowerShell 或 delegated cgroup 主机条件，17 个警告仍为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Local onboarding checks：
  - 测试先行验证：新增 Onboarding 合同在实现前以 `ModuleNotFoundError: riftx.onboarding` 产生预期收集错误，顶级 CLI 合同随后以 `No such command 'onboard'` 产生预期失败；
  - Onboarding、CLI、Doctor、Config Maintenance、Database Maintenance、Runtime/Model/Tool Config 与 Tool Registry 关联回归：`174 passed`；最终 Onboarding/Doctor fix 定向回归：`9 passed, 60 deselected`；
  - 真实隔离 XDG 首次启动冒烟：本地无 Key OpenAI-compatible Profile 下退出 0，创建三个 `0600` 配置、用户级目录，运行完整 49 段 Alembic migration，最终 Doctor 证明 schema head `3c6e8a1f2b40`、22 个 Official Pack 和 66 个 active lock ready，并将当前主机缺失的 `masscan`、`msfconsole`、`nmap`、`nuclei` 降级禁用；
  - 同一隔离配置第二次执行 `riftx onboard --non-interactive`：退出 0，明确复用既有配置，Database/Pack 继续 ready，未覆盖三个配置文件；
  - `conda run --no-capture-output -n agent mypy src/riftx/onboarding.py src/riftx/doctor.py src/riftx/config.py src/riftx/models/config.py src/riftx/tools/config.py`：`Success: no issues found in 5 source files`；`src/riftx/cli/app.py` 仍仅命中既有 `_AuditGroup` Typer/Click override 的 6 个类型错误；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-onboard-wheel`：passed；wheel 包含 `share/riftx/templates/tools.yaml`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5259 passed, 5 skipped, 17 warnings`；跳过与警告原因不变；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Offline security Demo checks：
  - 测试先行验证：新增 Demo 合同在实现前以 `ModuleNotFoundError: riftx.demo` 产生预期收集错误；实现后 Demo/CLI 定向回归 `5 passed, 64 deselected`；
  - `riftx demo pentest` 加载 `pentest-foundation`、`scope-and-safety`、`passive-recon`、`service-enumeration` 与 `web-attack-surface` Official Pack，只播放 `https://portal.demo.invalid` 的脱敏离线 transcript，明确证明零网络 I/O；缺失 Nmap/Nuclei 或 Tool 配置不可用时继续运行并输出 bundled transcript 降级路径；
  - `riftx demo code-audit` 加载 `code-audit-foundation`，复用生产 `builtin_detector_registry`、`LocalDetectorRunner`、Source Manifest 与 File Inventory，对 3 个内嵌安全 fixture 文件真实产生 4 条 finding；硬编码凭据证据输出 `[REDACTED]`，不泄露 fixture 值；
  - 真实 `riftx onboard --non-interactive` 后连续运行两个 Demo 的 CLI 合同通过，并证明未创建 Control Plane Client；两个独立真实 CLI 冒烟均退出 0；
  - Demo、CLI、Onboarding、Built-in Detector、Detector Runner 与 Official Pack Catalog 关联回归：`93 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/demo.py`：`Success: no issues found in 1 source file`；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack302-demo-wheel`：passed；wheel 包含 `riftx/demo.py`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5264 passed, 5 skipped, 17 warnings`；5 个跳过仍仅为 Windows、PowerShell 或 delegated cgroup 主机条件，17 个警告仍为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Read-only Capability/Pack management checks：
  - 测试先行验证：新增 Capability Management 合同在实现前以 `ModuleNotFoundError: riftx.capability_management` 产生 2 个预期收集错误；实现后 Capability Management/CLI 定向回归 `5 passed, 66 deselected`；
  - 新增 `riftx capabilities list`，通过 `SQLAlchemyCapabilityRepository.list_active_versions` 读取全部 active Capability Version，展示 ID、Version、Kind、Source 与 Trust Tier，不从 Pack YAML 推测数据库状态；
  - 新增 `riftx capabilities verify`，复用 `SystemDiagnosticsService` 同时验证 Alembic head、Official Pack/Version/Member immutable Digest、22 个 install 与 66 个 active lock；任一 issue 明确输出并退出 1；
  - 新增 `riftx packs list`，复用同一份 Official Catalog 与 Diagnostics 展示每个 Pack 的 packaged version、Capability 数量和 `ready/missing/drifted/unavailable` 持久化状态；
  - 所有命令只支持已到 Alembic head 的 file-backed SQLite；数据库缺失时不创建文件，mismatch/unmanaged/invalid 时要求先运行 Doctor；不连接 Control Plane，不写 Capability/Pack 状态；
  - 锁删除回归确定性产生 `lock_set_drift:pentest-foundation`，Capability verify 失败关闭，Pack list 将对应 Pack 标记为 drifted；
  - 隔离 XDG 真实 Onboard 后，`capabilities verify` 输出 66 active Capability 与 22 Official Pack 并退出 0，`capabilities list` 和 `packs list` 分别展示 66/22 条权威记录；
  - Capability Management、CLI、System Diagnostics、Official Pack Catalog/Repair、Capability Repository、Onboarding 与 Doctor 关联回归：`110 passed`；
  - `conda run --no-capture-output -n agent mypy src/riftx/capability_management.py`：`Success: no issues found in 1 source file`；
  - `conda run --no-capture-output -n agent python -m build --wheel --outdir /private/tmp/riftx-pack302-capability-wheel`：passed；wheel 包含 `riftx/capability_management.py`；
  - `conda run --no-capture-output -n agent ruff check .`：passed；
  - `conda run --no-capture-output -n agent pytest -q`：`5269 passed, 5 skipped, 18 warnings`；5 个跳过仍仅为 Windows、PowerShell 或 delegated cgroup 主机条件，17 个 SQLite datetime adapter 与 1 个既有 Pydantic alias 警告均与本切片无关；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Pack persistence backup/restore guard slice：
  - 新增只接受已到 Alembic head 的 file-backed SQLite 的 `backup_sqlite_database` 与 `restore_sqlite_database_backup`；备份复用 SQLite Backup API、`0700` 目录、`0600` 文件、`integrity_check` 与 fsync，不为缺失或未迁移数据库创建状态；
  - Backup receipt 固定记录源数据库与备份文件的 device/inode identity；恢复前同时验证当前用户所有的普通文件、拒绝符号链接与 identity drift，读取备份时再次通过 `O_NOFOLLOW` descriptor 校验，恢复使用同目录临时文件和原子 `os.replace`；
  - 恢复前验证备份完整性，恢复后再次运行 `integrity_check`；数据库或备份文件被替换时失败关闭，不覆盖无法证明仍为原对象的路径；
  - Doctor Official Pack repair 在写前创建一致性备份，继续复用 `bootstrap_official_packs` 与 `SystemDiagnosticsService` 做整批对账和写后验证；任一 reconciliation、提交或 diagnostics 失败时恢复整个数据库，回滚失败则明确报告 incomplete 并保留备份路径；
  - 定向 Database Maintenance/Doctor 回归：`25 passed`；新增测试证明备份后正常写入不残留锁、后续修改可恢复原值、源数据库或备份 inode 漂移均拒绝恢复，以及 Pack reconcile 注入失败后数据库 marker 回到写前值；
  - `conda run --no-capture-output -n agent python -m mypy src/riftx/database_maintenance.py src/riftx/doctor.py`：`Success: no issues found in 2 source files`；
  - `conda run --no-capture-output -n agent ruff check src/riftx/database_maintenance.py src/riftx/doctor.py tests/unit/test_database_maintenance.py tests/unit/test_doctor.py`：passed；
  - `conda run --no-capture-output -n agent python -m pytest`：`5273 passed, 5 skipped, 17 warnings`；5 个跳过仍仅为 Windows、PowerShell 或 delegated cgroup 主机条件，17 个警告仍为既有 Python 3.12 SQLite datetime adapter 弃用提示；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Backup/Restore readiness slice：
  - 新增只读 `inspect_sqlite_backup_readiness`，复用现有 migration inspection 和数据库 file identity，同时验证备份目录的非符号链接路径、所有者、可写性与已存在目录的精确 `0700` mode；检查本身不创建目录或备份；
  - `backup_sqlite_database` 复用同一 readiness，Doctor 与真实写路径不会对不安全备份目录给出不同结论；既有 receipt-bound restore 继续在恢复前后校验 identity 和 SQLite 完整性；
  - Doctor 对未初始化的 file-backed SQLite 返回可修复降级，对非 file-backed 数据库要求外部备份策略，对 migration/identity/目录安全失败关闭，仅安全且已就绪的 SQLite 返回 `ready`；
  - Database Maintenance/Doctor 定向回归：`27 passed`；`conda run --no-capture-output -n agent python -m mypy src/riftx/database_maintenance.py src/riftx/doctor.py`：`Success: no issues found in 2 source files`；
  - `conda run --no-capture-output -n agent python -m ruff check .`：passed；`conda run --no-capture-output -n agent python -m pytest`：`5275 passed, 5 skipped, 17 warnings`；跳过和警告原因与前一切片一致；
  - `git diff --check` 和 staged `git diff --check`：passed。
- Implementation commits：`d4f6e4eb`、`02cde9fe`、`eb41f77d`、`41eb8896`、`36100d47`、`0c70cf2e`、`3a1f0fc8`、`e4281b2f`、`6550f85a`、`faf12c50`、`ab3f50b6`、`4ba069e4`。
- Plan rebaseline：`9424b82b` 将正式版发布线调整为专业纵向闭环优先；内嵌 Official Packs 的 `packs install/update/rollback` 不再作为 PACK-302 或 V1 blocker，待 ECO-800 出现真实第三方来源、签名分发和多版本缓存边界后实现。
- Completion：PACK-302 计划范围已完成；下一任务是 PEN-500 Pentest admission、真实 Run 与 Attack Surface。

### PEN-500：Pentest Admission 与 Attack Surface

- Status：completed
- Started：2026-08-06
- Completed：2026-08-07
- Workload and admission ADR slice：
  - ADR-0013 选择持久 `RunKind.PENTEST`，拒绝在 `RunKind.GENERAL` 之上叠加平行 workload profile；
  - Pentest 必须使用专用 Application/API/CLI 创建路径，并在创建时持久验证授权引用、具体 Scope、网络 Entry Point、Approval、预算、禁止行为与停止条件；
  - Workflow signal owner、Runner effect binding、Effect Inventory、API discriminant 和数据库约束必须显式支持 `pentest`，未审计分支继续失败关闭；
  - Attack Surface 作为 Run admission、Artifact/Evidence、Reasoning 和 Traffic 的可重建投影，不新建第二套事实库。
- Domain and persistence slice：
  - 新增 `RunKind.PENTEST`、`PentestBudget`、`PentestAdmission`、强制禁止行为与硬停止条件；Pentest Run 缺 Admission、具体正向网络 Scope 或网络 Entry Point 时 Domain 拒绝，非 Pentest Run 携带 Admission 时拒绝；
  - Run ORM 新增 `pentest_admission_json`，数据库约束精确绑定 `kind='pentest'` 与非空 Admission；Mapper、Repository 和 Pentest discriminated read projection 可完整往返；
  - Alembic revision `6f2a9c4d8e17` 保留既有 General/Code Audit 数据，拒绝未知 kind、Pentest/Admission 错配和存在 Pentest 权威事实的降级；跨多级降级在任何 DDL 前继续检查 Task、Evidence、Capability、Preflight、Workflow 与 Runner 权威事实；
  - 新增 RunKind 后，未审计的 Web/MCP/Workflow/Runner effect allowlist 显式维持 General/Code Audit，只有安全通用 Run 读取接受 Pentest，避免 `_ALL_RUN_KINDS` 自动扩权。
- Workflow and runner identity slice：
  - 新增 `riftx.pentest-run-workflow/v1`、`pentest_run` owner kind、`pentest_run:<run_id>` owner identity 与 `riftx-pentest-<run_id>` workflow ID；General/Pentest/Code Audit 使用三个显式 validator 分支；
  - Workflow transport 复用 General 交互信号逻辑，但 Pentest 保持独立持久身份；Approval decision 与 Execution terminal signal 按 RunKind 选择 factory；
  - `RunnerEffectBinding` 显式区分 General、Pentest 与 Code Audit；Runner daemon 允许 General/Pentest 交互运行，仍拒绝 Code Audit 走通用主机副作用；
  - stop reconciliation 查询从 general-only 收敛为 interactive-run 语义，覆盖 Pentest；
  - Alembic revision `7b3d1e5f9a24` 联合更新 Workflow intent 与 Runner binding 约束，并在任何 DDL 前阻断会丢失 Pentest 权威事实的 downgrade。
- Effect Policy and interactive guard slice：
  - 删除含义模糊的 `_ALL_RUN_KINDS`/`_GENERAL_ONLY`，以 `_READABLE_RUNS`、`_INTERACTIVE_RUNS`、`_PENTEST_ONLY` 和 `_AUDIT_ONLY` 表达权限；
  - 加入专用 Pentest 创建入口后，完整策略目录现为 67 条非 Run、14 条 Code Audit 专属、55 条 General/Pentest/Code Audit 共享安全/所有权路径、109 条 General+Pentest 交互路径；测试禁止新增未审计 General-only 漏洞；
  - `require_general_run_operation` 更名为 `require_interactive_run_operation`，28 个生产模块逐项迁移，Code Audit 专属路径仍拒绝 Pentest；
  - Pentest 复用 Run control、Workflow Router、Temporal Activity、Agent cycle、Execution、Approval、Artifact、Finding、Report、Memory、Context/Graph、Terminal、Browser、Target HTTP/Traffic、Web Research、MCP、Connector、Runner command、Safety Stop 与 reconciliation；
  - 修正 Pentest Run Action 判别联合响应，以及 Artifact/Memory 持久可见性、Web Artifact、Code Workspace 和 Git Workspace 的交互 Owner 路径；Pentest owner 不得携带 Code Audit identity。
- Dedicated admission/application/API creation slice：
  - 新增唯一生产 `POST /api/v1/pentests`；普通 `POST /runs` 的 strict schema 继续拒绝 `kind=pentest`，API Route Policy、Effect Policy 与 Managed Effect Inventory 新增专用创建操作；
  - `PentestApplicationService` 验证现有或内联 Engagement 的非空授权引用、Objective、Pentest Admission、具体 Scope、网络 Entry Point 和每个 Entry Point 的现有 `ScopeGuard` 决策；
  - `SQLAlchemyPentestCreationUnitOfWork` 在一个 serialized transaction 中写入 Engagement（若需创建）、Pentest Run、主 Agent Session、上下文事件与首条指令；故障注入证明中途失败不残留任一数据库事实；
  - 客户端 UUID `request_id` 同时提供稳定 Run 恢复身份；创建后以 `riftx-pentest-<run_id>` 对同一消息事件执行 Signal-With-Start，Temporal 故障返回可恢复的 Run/message 身份，重试同一请求不重复写入事件；
  - 当前主 Session 已在 Workflow 前持久存在，消除了 Worker 延迟建 Session 对下一步 Selection 原子绑定的阻断；本切片不伪造重复 Selection JSON。
- Capability Selection admission slice：
  - `CreatePentestRequest` 新增 Pack/Tool/Skill/Technique 选择，默认 `pentest-foundation`；Control Plane 与 Worker 复用同一 Official Pack、Tool Registry、Progressive Skill 和 Capability catalog 生产来源；
  - Tool、Skill、Technique 复用既有运行时固定快照构建器，不建立第二种 Selection 模型；主 Session 同时写入三类不可变 allowlist，空 Tool allowlist 显式禁止未授权动态 Tool；
  - 已选 Official Pack 的每个 CapabilityVersion 都获得 `RUN_SESSION` lock，包括 Skill、Technique 与不可直接选择的 Eval Case；显式 Technique 仍通过 Selection snapshot 固定并在事务内核对 durable catalog；
  - Engagement、Run、主 Session、事件、Selection、allowlist 和 Pack locks 在同一 serialized transaction 提交；未知版本、中途故障与同 request_id 合同漂移全部失败关闭且不残留半成品；
  - Model Profile 继续使用现有 Run/AgentSession 强制绑定；未增加无消费者的 Model 版本表、Run JSON manifest 或平行 Selection/Pack 存储。
- Pentest status and CLI slice：
  - 新增 `GET /api/v1/pentests/{run_id}/status`；先经过现有 Authorized Run Read，再由 Pentest-only Application Service 拒绝非 Pentest Run；Route Policy 与 RunKind Effect Policy 显式登记该只读入口；
  - `SQLAlchemyPentestStatusReader` 在单个只读 Session 中从主 Agent Session、Capability Selection/allowlist/Pack lock、Context Compilation、Tool Intent、Execution 和 Run Event 聚合状态，不新增表、缓存、后台任务或平行 authority；
  - status 返回 Admission、固定 Capability、预算限制与已持久化使用量、Workflow started marker、Runner execution/node、最新 Stop Proof 和 declared entry points；Token 完整性、目标交互和停止确认均失败关闭，不把缺失事实解释为完整证据；
  - status 在 Control Plane 重启后可从数据库重建；API 测试覆盖非 Pentest 类型保护、opaque missing read、动态使用量、Runner 状态、取消 Stop Proof 与跨进程重读；
  - 新增 `riftx pentest start/status/resume/stop`；`start` 调用专用 Admission API，`resume/stop` 先通过专用 status 校验 Pentest 类型，再复用现有 Run resume/cancel 控制路径；CLI 不复制 Scope、Selection、Effect Policy 或 Stop 业务规则。
- Attack Surface and isolated lifecycle slice：
  - `9a4714fc` 从 Entry Point、Target HTTP 与 verified Evidence 重建 declared/observed/verified Attack Surface，不新增第二套资产事实库；
  - `ca12ad9b` 在隔离授权 HTTP 目标上贯通创建、能力固定、成功交互、超时、越界拒绝、状态读取、暂停、恢复、取消和跨进程重读。
- Durable budget slice：
  - `ad91c3f4`、`2c0f8004` 在 Tool execution claim 中原子约束目标交互总量与并发，总量耗尽复用 pause、Safety Stop、Workflow signal 和 Stop Proof；
  - `73288673` 使主/子 Session 与未合并 Cycle 的 Run 级 Model/Tool/Token 用量成为 status 和 admission 的一致权威事实；
  - `b6b5f739` 在 Provider `start/resume` 前原子约束 Model、Token 完整性/余量和 wall-clock Duration，预算外调用不接触 Provider；
  - `53812397` 在共享 Tool execution claim 前约束 Tool 与 Duration；Proposal、等待批准和 Scope 拒绝不计，失败后的新 retry 再计，完全相同的幂等重放不重复计；
  - `1c379dcc` 统一 Model、Runner/PTY、MCP/Browser 控制工具和 Target HTTP 的 `pentest.budget_exhausted` 事实与 pause/Stop Proof 入口；并发容量满继续使用可重试的 `pentest.budget_capacity_reached`。
- Verification：阶段 A 完整 Control Plane `67 passed`；Runtime/Execution/Target HTTP/Temporal Worker `467 passed`；预算处理聚焦回归 `215 passed`；全仓 Ruff passed；6 个变更核心源文件 scoped mypy passed；`git diff --check` passed。
- Implementation commits：ADR `315039fc`；Domain/持久化 `86aaecdf`；Workflow/Runner Identity `e2314e9b`；Effect Policy/Interactive Guard `8b9ef440`；Dedicated Admission/Application/API `8f1b2554`；Capability Selection `33c863ea`；Pentest Status/API `70f6f4a0`；Pentest CLI `2271bc8e`；Attack Surface `9a4714fc`；Isolated Lifecycle `ca12ad9b`；Target Interaction Budget `ad91c3f4`、`2c0f8004`；Live Run Usage `73288673`；Model/Token/Duration Budget `b6b5f739`；Tool/Duration Budget `53812397`；Unified Budget Stop `1c379dcc`。
- Completion：PEN-500 已完成；下一施工阶段是优化计划 Stage B 的单一网络服务专业闭环，不按历史任务图提前启动 PEN-501 状态化 Web。

### Stage B：网络服务专业闭环

- Status：completed
- Started：2026-08-07
- Completed：2026-08-07
- Artifact → Evidence slice：`54c2489b` 在生产 Worker 装配现有 `EvidenceApplicationService`，增加 Primary-only `register_artifact_evidence`，以当前 Run 的 opaque Artifact ID 和精确 byte span 登记幂等、可回放 Evidence，不新建表或 Evidence 模型。
- Service enumeration slice：`c7709790` 复用 `service-enumeration` Pack、注册 Nmap Tool、`ExecutionArtifactStore`、Nmap XML parser 和 localhost fixture，形成 Admission → Execution → Artifact → Evidence → Observation → Hypothesis 的可重启 E2E；Scope、Approval、Tool/Target/Duration 预算在副作用前继续失败关闭。
- Minimal verification slice：`5018f071` 复用 Target HTTP 对 `/diagnostics` 和 `/missing` 做确定性正负验证；受保护 HTTP Artifact 可登记 Evidence 但继续对通用读取面隐藏；Reasoning Vulnerability Candidate 仅能幂等投影为 Draft Finding，精确 404 仅形成对该路径猜测的 Negative Result。
- Closure/Report slice：`b2193139` 将现有 `ReportApplicationService` 扩展为 `riftx.report.v2`，从权威持久事实重建 Engagement、Admission、Capability Selection/allowlist、Pack Lock、预算使用、Execution、Evidence、Reasoning、Task、Draft Finding、Negative Result、Closure 和 Stop Proof；Draft 不升级为 Confirmed。
- Report safety：Target HTTP 原始请求/响应体、授权引用原文和本地路径不进入报告；受保护 Evidence 只输出 opaque ID、Digest、Redaction/Replay 状态和安全 Locator；`pentest.budget_exhausted`、Pause、Resume、Cleanup 和 Closure 只通过明确字段白名单进入 Report。
- E2E：同一 localhost 场景覆盖 Nmap、HTTP 200/404、批准前零副作用、Evidence、Observation/Hypothesis、Draft Finding、Negative Result、目标预算暂停、Resume/Complete、Closure、JSON Report、Control Plane 重启重建和 Report 复用幂等；重放后 Finding 仍只有一条。
- Verification：B4 Report/Closure/Worker 受影响回归 `78 passed`；完整 `tests/integration/api/test_control_plane.py` `68 passed`；最终聚焦回归 `19 passed`；`conda run --no-capture-output -n agent ruff check .` passed；`reports.py`、`api/runtime.py`、`temporal/worker_runtime.py` scoped mypy passed；`git diff --check` passed。
- Non-goals upheld：未新增表、migration、Pack、Scanner、Planner、Graph、Worker、Browser 场景或 UI；未自动确认 Finding。
- Implementation commits：`54c2489b`、`c7709790`、`5018f071`、`b2193139`。
- Completion：Stage B 已完成；下一施工切片是 C1 最小身份/对象授权靶场，不提前开始 Browser、Attack Chain、能力学习或默认产品面删减。

### Stage C：状态化 Web 与报告

- Status：completed
- Started：2026-08-07
- C1 resettable target slice：`d73a0c86` 新增仓库内状态化 localhost HTTP 靶场，每次绑定随机端口并在测试后关闭；只包含 Alice/Bob、两个对象、登录、各自所有权基线和一个确定性跨对象授权分支。
- C1 production boundary E2E：复用现有 Pentest Admission、Target HTTP、Traffic、Scope、Approval 和 Target Interaction Budget；未批准与越界请求零网络副作用，Alice/Bob 登录和各自对象访问成功，Alice 访问 Bob 对象的分支可重复，第六次目标交互在网络前耗尽预算并暂停 Run。
- C1 safety：Event、Traffic、Attack Surface 与 Artifact 标题导出不包含测试密码或会话 Cookie；靶场请求日志只保留 Method、Path、Actor 和 Status；测试失败断言不回显敏感值。
- Verification：`tests/integration/api/test_pentest_stateful_web.py` `1 passed`；Target HTTP 回归 `47 passed`；完整 Control Plane 加 C1 `69 passed`；`conda run --no-capture-output -n agent ruff check .` passed；C1 两文件 scoped mypy passed；`git diff --check` passed。
- Non-goals upheld：C1 未修改生产 schema/service，未新增表、migration、Pack、Browser 场景、Scanner、Planner、Graph、Worker 或 UI，未创建 Finding、Report 或 Attack Chain。
- Implementation commit：`d73a0c86`。
- Completion：C1 已完成；后续 C2 已使用同一靶场接通安全身份引用与专业事实。
- C2 credential boundary：`85decf8d` 为 Target HTTP 增加 Header/Body/Cookie Credential Reference；引用只能由当前 Session 已固定且 active 的 Technique Permission 授权，未选择、未激活、损坏、越权或缺失引用在网络副作用前失败关闭。
- C2 Runner resolution：真实值只在 Runner 发送前按 Environment → Keyring 解析；ToolCallIntent、fingerprint、持久请求快照、Event、Traffic、Transcript 与 Artifact 标题只保留引用或安全元数据。
- C2 production E2E：通过 `RuntimeControlToolService → DeferredExecutionDispatcher → TargetHttpApplicationService → RunnerTargetHttpClient` 完成 Alice/Bob 登录、各自对象基线和一次 Alice 跨对象单变量验证；正常与跨对象响应分别登记受保护 Evidence，并形成 Observation、带前置条件和正负判据的 Hypothesis、结构化 Attempt。
- C2 persistence：Control Plane 重建后 Capability Selection、Traffic、两份 Evidence、Reasoning Graph 和 Working Memory Attempt 可重建；Finding 与 Report 均为空。
- C2 budget/traffic semantics：一次已批准但未授权的引用请求计入 Target Interaction 尝试但保持零网络副作用；随后 5 次真实请求形成 5 条 Traffic 交换，预算状态明确区分已批准尝试与真实流量。
- C2 verification：状态化 Web E2E `2 passed`；Target HTTP `63 passed`；Runtime Control Tools `52 passed`；完整 Control Plane `68 passed`；Worker `13 passed`；Tool discovery `11 passed`；全仓 Ruff passed；6 个变更生产文件与 3 个新增/变更测试文件 scoped mypy passed；`git diff --check` passed。
- C2 non-goals upheld：未新增表、migration、Pack、Browser 场景、Crawler、Fuzzer、Scanner、Planner、Graph、Worker 或 UI，未创建 Finding、Report 或独立 Attack Chain。
- C2 implementation commit：`85decf8d`。
- Completion：C2 已完成；后续 C3 已关闭专业结果与报告。
- C3 professional result：`a8b29a4c` 将跨对象 Observation 投影为 `VULNERABILITY_CANDIDATE` 和幂等 Draft Finding；Finding 引用受保护跨对象 Artifact/Evidence，并明确 Alice 前置、只改变对象 ID 的复现动作、影响、修复和未测试范围；不自动 Confirmed。
- C3 failure semantics：未授权 Credential Reference 作为失败 Attempt 持久，明确其在网络前阻断且不是目标安全结论；成功跨对象 Attempt 与 Reasoning 由现有 Report Source 投影。
- C3 reports：现有 `ReportApplicationService` 生成 JSON/Markdown，Markdown 增加 Pentest Evidence Chain 和 Attempt 摘要；Closure、Finding、Reasoning、Attempt 和两种 Report 经 Control Plane 重建可恢复，`reuse_existing` 不重复生成。
- C3 safety：Secret、Credential 真实值、受保护 HTTP 原始体和本地路径不进入 Report；受保护 Finding Evidence 仅提供安全元数据且 `content_url` 为空。
- C3 verification：状态化 Web E2E `2 passed`；Report Application `2 passed`；Temporal Activities `23 passed`；Target HTTP `63 passed`；Runtime Control Tools `52 passed`；Worker Runtime `13 passed`；完整 Control Plane `68 passed`；全仓 Ruff passed；3 个生产文件与状态化 Web 测试 scoped mypy passed；`git diff --check` passed。
- C3 non-goals upheld：未新增表、migration、Pack、Attack Chain Domain、Browser、Crawler、Fuzzer、Scanner、Planner、Graph、Worker 或 UI。
- C3 implementation commit：`a8b29a4c`。
- Completion：Stage C 已完成；后续 D1 已关闭 Operator Skill 生命周期门禁。

### Stage D：用户驱动能力成长

- Status：completed
- Started：2026-08-07
- D1 lifecycle：`6f59e278` 复用 Progressive Skill Registry、Capability Version/Status 和本地 SQLite，新增 `riftx skills validate/register/activate/list/disable/rollback`；同 ID/version/digest 幂等，同版本源漂移要求提升版本号，Operator 根目录不能伪装 Official 来源。
- D1 admission：显式 Operator Skill 只有在 active Capability Version 的 ID/version/source/source digest 与当前包完全一致时才能进入新 Pentest；未注册、approved、disabled、源漂移或源删除均在 Run 创建和网络副作用前拒绝。
- D1 safety：Capability Manifest 使用保守的 target-interaction/always-approval/scope-required/zero-credential 权限；Skill 的 preferred tools 不扩大 Run Tool allowlist；完整 Document/Reference 继续只由 Session Selection 快照保存。
- D1 rollback：禁用只影响新 Run；回滚要求操作者先从 Git/备份恢复旧源码再切换已有 Version 状态，不建设包缓存；旧 Run 在源码漂移或删除后仍从持久快照恢复原文与 References。
- D1 verification：Operator Skill lifecycle/CLI `3 passed`；Capability Management、Doctor、CLI 与 Skill unit `111 passed`；Capability/Skill/Pentest persistence `10 passed`；状态化 Web `2 passed`；完整 Control Plane `68 passed`；文档合同 `3 passed`；全仓 Ruff passed；5 个变更源/测试文件 scoped mypy passed；`git diff --check` passed。
- D1 non-goals upheld：未新增表、migration、Marketplace、Registry Service、Capability Candidate/Promotion、包下载/缓存、复盘 UI、自动批准或自动改写 Skill。
- D1 implementation commit：`6f59e278`。
- D1 completion：completed。
- D2 report projection：`a929fdb4` 保留 JSON 结构化事实，并向 Markdown 增加 Capability Selection、Allowlist、Stop、Execution 与 Evidence 的确定性脱敏投影；不输出 Credential 值、原始 HTTP 敏感体、本地路径、Skill 全文或原始终端转录。
- D2 version iteration：纵向 E2E 生成 v1 Run/JSON/Markdown Report，随后修改并启用 v2；旧 Run 和已生成 Report 继续固定 v1，新 Run 和 Report 固定 v2。
- D2 professional workflow：新增 `docs/operator-skill-lifecycle.md`，说明 validate/register/activate、Report 人工复盘、方法问题与 Tool/门禁/Negative Result 的区分、版本提升、disable 和先恢复源码再 rollback。
- D2 verification：Report、状态化 Web、Operator Skill lifecycle 与 v1→v2 纵向 E2E `8 passed`；全仓 Ruff passed；`reports.py` scoped mypy passed；`git diff --check` passed。
- D2 non-goals upheld：未新增 Review Domain、表、migration、命令、自动评分、自动生成、自动改写、自动批准、Replay Lab 或 Trajectory Store。
- D2 implementation commit：`a929fdb4`。
- D2 completion：completed。

### Stage E：默认产品面收缩与发布

- Status：in_progress
- Started：2026-08-07
- E1 entrypoints：`fdfa06e5` 将 README 中英版首条路径改为 Onboard → Doctor → Pentest → Approval → Report，并链接 Operator Skill 人工迭代指南；旧通用 Run 示例保留在贡献者/高级区。
- E1 CLI：顶级 help 复用 Typer 原生分组，按 Getting started、Service operation、Pentest workflow、Advanced、Experimental 展示；冗余显式 `interactive` 入口仍可调用但不占默认帮助，Audit 明确 frozen/experimental。
- E1 verification：Onboard、Doctor、CLI、Operator Skill Report 与文档合同 `100 passed`；全仓 Ruff passed；`cli/app.py` scoped mypy 在禁用既有 Typer/Click `override`、`arg-type`、`return-value` 兼容错误后 passed；`git diff --check` passed。
- E1 non-goals upheld：未删除命令、修改 API、重做 WebUI、新增导航框架、拆依赖或开发 Code Audit。
- E1 implementation commit：`fdfa06e5`。
- E1 completion：completed。
- E2 clean environment：`8ea90890` 在隔离 XDG、空工具 PATH 和无外部目标交互条件下真实运行 Onboard 两次，验证配置/模型/Tool Registry/数据库/Workspace/Skill 路径可加载且不覆盖用户文件。
- E2 startup fix：Onboard 原先未写入生产装配必需的 `security.trust_profile`；最小修复固定当前唯一支持的 `local_single_operator`，使生成配置可通过 `APISettings.from_config()` 并启动 Control Plane。
- E2 degradation：Nmap、Nuclei、Masscan、Metasploit 等模板工具全部禁用后，Doctor 保持 degraded 而非 failed，并为 Temporal、Runner、Browser、Tool、Skill、MCP、LSP 和 Scanner 输出修复动作。
- E2 admission：使用 Onboard 产物装配生产 Control Plane 并持久化 Pentest Admission/Selection；未启动 Temporal 时返回明确 `temporal_unavailable`，Run 保留为 `waiting_user` 以供同请求重试。
- E2 verification：Onboard、Doctor、CLI、隔离 XDG 生产装配/Pentest Admission 与文档合同 `100 passed`；全仓 Ruff passed；`onboarding.py` scoped mypy passed；`git diff --check` passed。
- E2 non-goals upheld：未拆依赖、删除可选模块、新增安装器、启动真实模型回合或执行目标交互。
- E2 implementation commit：`8ea90890`。
- E2 completion：completed。
- E3 current target：测量 Code Audit、Browser/MCP/Connector、Candidate/Promotion/Evaluation、General Agent/Memory/Terminal、Demo/前端非主线和重复旧入口的真实消费者与默认成本。
- E3 completion gate：形成带代码证据和可重复命令的处置表；没有消费者为零、无兼容责任和测量收益的共同证据，不删除代码。

## 9. Known pre-existing worktree state

- 当前没有已知的任务外工作树改动；每个切片仍须以当次 `git status` 为权威证据。
