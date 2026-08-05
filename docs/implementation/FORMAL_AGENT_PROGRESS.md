# RiftX 正式版 Agent 开发实施账本

> 状态：active
>
> 启动日期：2026-08-05（Asia/Shanghai）
>
> 实现分支：`ch1nfo/riftx-3-code-audit`
>
> 计划输入基线：`e40af267`
>
> 正式版计划提交：`357ed38e`
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

- Stage：`S0 — 规格、基线与评测骨架`
- Completed task：`SEC-000 — 正式版 ADR 与实施账本`
- Status：`completed`
- Product behavior：不变；SEC-000 只冻结边界、依赖、迁移顺序和研发记录。
- Next task：`SEC-001 — Security Capability Evaluation 骨架`。
- Also eligible：CAP-001；SEC-001 与 CAP-001 都依赖 SEC-000。

## 3. 研究与实现基线

| 输入 | 基线 | 用途 |
| --- | --- | --- |
| RiftX | `e40af267` | 正式版计划开始前的产品代码基线 |
| 正式版计划 | `357ed38e` | S0-S8 权威开发计划 |
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
- 外部 Agent 结果是可选参考，不是正式版发布门；
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
| S0 规格、基线与评测骨架 | in_progress | ADR/账本完成；Evaluation schema 可重复运行代码审计和 Web 案例 |
| S1 生产 Capability Plane | pending | Capability 可持久加载；Code/Browser/Web/MCP 接入生产 Runtime |
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
| SEC-000 | none | completed | pending backfill |
| SEC-001 | SEC-000 | pending | — |
| CAP-001 | SEC-000 | pending | — |
| CAP-100 | CAP-001 | pending | — |
| CAP-101 | CAP-001 | pending | — |
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
- Implementation commit：pending backfill。

## 9. Known pre-existing worktree state

- `apps/burp-extension/.gradle/` 是 SEC-000 开始前已经存在的未跟踪目录。
- 该目录不属于正式版 Agent 任务，不得暂存或提交。
