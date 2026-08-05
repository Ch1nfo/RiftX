# ADR-0012：RiftX 正式版安全 Agent 平台边界

> 状态：Accepted
>
> 日期：2026-08-05（Asia/Shanghai）
>
> 所属任务：SEC-000
>
> 权威计划：[RiftX 正式版开发文档](../../../RiftX_正式版_开发文档.md)
>
> 实施账本：[正式版 Agent 开发实施账本](../../implementation/FORMAL_AGENT_PROGRESS.md)

## 1. Context

RiftX 已经具备耐久 Run、Temporal Worker、独立 Runner、Approval、Artifact、Context、
Working Memory、Browser、Target HTTP、Skill、Code Audit 和停止证明等基础模块，但这些模块尚未
形成一条生产级的专业安全能力闭环。继续横向增加 Service、Prompt、Tool 或 Skill 会扩大实现面积，
却不会自然产生更强的渗透测试和代码审计能力。

正式版要同时保证：

1. 新用户通过 Official Packs 获得可正常使用的基础即战力；
2. 操作者和组织可以持续积累经验证的 Tool、Skill、Technique、Playbook、Knowledge 和 Eval Case；
3. 专业推理、证据、失败和能力选择可以跨 Worker、Runner 与客户端重启恢复；
4. 自动学习不能绕过授权、评审、版本和回滚边界；
5. General、Pentest 和 Code Audit 工作负载不能因为共享模型或工具而混淆副作用权限。

本 ADR 冻结正式版三个核心系统的职责、权威状态、Run 工作负载边界和数据迁移顺序。它不在
SEC-000 中增加新产品行为，也不开放任何新的执行权限。

## 2. Decision

### 2.1 三个核心系统

正式版围绕以下三个系统建设：

#### Security Capability System

负责：

- Capability、CapabilityVersion 和 Capability Pack 的身份与版本；
- Tool、Skill、Technique、Playbook、Knowledge、Eval Case 的统一 Manifest；
- Dependency、Permission、Evidence Contract、Provenance、Digest 和 Pack Lock；
- Official、Operator、Organization、Engagement 四级能力来源；
- 搜索、加载、启用、禁用、Pin、回滚和兼容性检查。

不负责：

- 直接执行目标副作用；
- 把模型输出晋升为 Confirmed Fact 或 Finding；
- 绕过 Run Scope、Approval、Runner ownership 或 Stop Proof；
- 将一次运行结果直接发布为 Active Capability。

#### Evidence-driven Cognitive Runtime

负责：

- Task Graph、Reasoning Graph、Attack/Code Graph；
- Evidence Ledger、Working Memory 和 Context Compiler 输入；
- Fact、Hypothesis、Attempt、Finding 的分层状态；
- Planner、Primary Agent、Observer、Projector 和 Closure Verifier 的持久协作；
- Success Criteria、Evidence Contract、负结果和停止状态的闭合检查。

不负责：

- 修改 Capability 的正式版本；
- 将未验证 Knowledge 或模型文本直接写成 Confirmed Finding；
- 以对话历史作为唯一状态真相；
- 在缺少持久 ToolCallIntent、Scope 或 Approval 时执行副作用。

#### Capability Learning Flywheel

负责：

- Run Trajectory、Post-run Review 和 Failure Taxonomy；
- Memory Candidate、Capability Candidate 和 Eval Candidate；
- Replay、对照、人工 Review、Promotion 和 Curator；
- 使用情况、成功、误报、回归、冲突、归档和回滚记录。

不负责：

- 后台复盘期间调用执行类工具；
- 自动修改 Official 或 Organization Pack；
- 未经 Replay 和人工批准发布 Candidate；
- 永久删除仍可能用于审计、回滚或 Provenance 的历史版本。

### 2.2 权威状态与写入边界

| 状态 | 权威所有者 | 模型允许动作 | 模型禁止动作 |
| --- | --- | --- | --- |
| Capability Version | Capability Repository | 搜索、请求加载、提出 Candidate | 覆盖 Active/Official 版本 |
| Tool/Skill Selection | Run-scoped Selection Repository | 提出选择或释放建议 | 仅靠 Prompt 隐式改变生产工具面 |
| Task/Reasoning Graph | Cognitive Reducer | 提出节点和边的 Proposal | 直接写最终图状态 |
| Evidence | Evidence Service/Repository | 提交来源、定位和摘要 | 伪造 Artifact、Digest 或重放结果 |
| Confirmed Fact/Finding | Evidence-aware Reducer | 提出确认请求 | 无 Evidence Contract 直接确认 |
| Run side effect | Application Service + Runner ownership | 提交 typed intent | 直接执行未持久化副作用 |
| Capability Candidate | Learning Repository | 生成 Candidate | 直接发布 Active Capability |
| Promotion | Promotion Service + human approval | 请求 Replay/Review | 自批、自签或跳过回归检查 |

LLM、Agent loop 和 Subagent 都不是权威状态所有者。模型输出只能形成 Proposal、Candidate 或 typed
intent；真正状态变化由确定性 Application Service、Reducer、Repository 和 Policy 完成。

### 2.3 工作负载与 Run 边界

#### General Run

`RunKind.GENERAL` 是当前通用 Agent 工作负载。它可以使用已经登记在
`RunKindEffectPolicy`、Tool Policy 和 Approval Policy 中的能力，包括受控 Execution、Terminal、
Browser、Target HTTP、Connector、Artifact、Memory 和 Context。

General Run：

- 不自动获得任何目标授权；
- 不得推断或继承 Code Audit owner；
- 不得通过通用 route 修改 Code Audit aggregate；
- 仍受 Scope、Approval、Runner ownership、Artifact 和 Stop Proof 约束。

#### Pentest Run

Pentest Run 在本计划中首先是明确的工作负载类别，不是当前已经存在的持久 `RunKind`。
`src/riftx/domain/enums.py` 当前只有 `general` 和 `code_audit`。

在专用 `pentest` RunKind、迁移和 effect policy 被后续 ADR 明确实现之前：

- 授权渗透测试只能运行在 `RunKind.GENERAL` 上；
- 必须持久化 Engagement Scope、目标 allowlist、禁止项、批准策略和停止条件；
- Browser、Target HTTP、Execution 与外部工具继续使用现有 typed intent 和 Runner ownership；
- UI、API 和报告可以标注 workload profile，但不得伪装成新的持久 RunKind；
- 不得因安装 Pentest Pack 自动扩大网络、凭据、终端或执行权限。

未来若引入 `RunKind.PENTEST`，必须新增 ADR、数据库迁移、API 投影、Effect Inventory、兼容读取、
Runner ownership 和回滚方案；不能只向 enum 增加一个值。

#### Code Audit Run

`RunKind.CODE_AUDIT` 已存在。当前正式产品边界仍由既有 Code Audit 规格和 ADR 约束：在 RiftX 所在
机器上对用户选择的文件夹进行有界、只读、静态分析，不执行目标项目，也不依赖 Docker、远程
Runner 或另一台机器。

Agentic Code Audit 后续可以增加代码读取、搜索、LSP、Scanner 和推理能力，但必须满足：

- Snapshot、文件 Digest、位置和 Evidence 归属明确；
- 默认只读，不运行安装脚本、Git Hook、构建、测试或目标代码；
- 不能借 General Run 的 Execution、Terminal、Browser 或 Target HTTP 权限执行；
- 动态验证必须由 AUD-405 的专用计划、隔离、审批和残留检查开放；
- Scanner Signal 只能成为线索，不能直接成为 Confirmed Finding。

### 2.4 副作用矩阵

下表是能力设计的默认边界；更严格的现有 Policy 继续优先。

| 副作用 | General Run | Pentest workload | Code Audit Run |
| --- | --- | --- | --- |
| 读取 RiftX Artifact/Memory | 按 owner 和 ACL | 同 General | 按 Audit owner 和 ACL |
| 读取用户代码 | typed file/code tool + scope | 仅任务需要时 | 默认允许，只读 Snapshot/授权目录 |
| 修改工作区文件 | typed patch + Approval | 默认不需要；需要时显式批准 | 默认禁止；未来仅隔离 Worktree |
| 启动进程/终端 | registered Tool + Approval + Runner | 同时要求 Engagement Scope | 默认禁止 |
| 目标 HTTP | Target HTTP intent + Scope | 仅 allowlist 目标 | 默认禁止 |
| 浏览器目标交互 | Browser intent + Scope | 仅 allowlist 目标 | 默认禁止 |
| 公网研究 | Research pipeline + source | 只产生线索 | 只产生线索 |
| MCP 调用 | Manifest permission + policy | 不扩大 Engagement Scope | 不扩大只读 Audit 边界 |
| 安装依赖或运行 Hook | 显式批准 | 显式批准且记录风险 | 默认禁止 |
| 发布 Capability | 只能生成 Candidate | 只能生成 Candidate | 只能生成 Candidate |

### 2.5 Capability 来源优先级

能力来源按以下顺序合并，但更高优先级不能绕过权限上限：

```text
Official < Operator < Organization < Engagement
```

- Official 提供开箱即用基线，由 RiftX 发布流程维护；
- Operator 表达个人习惯和方法；
- Organization 表达团队规范、工具和知识；
- Engagement 只服务当前授权项目，默认不得自动外溢。

同 ID 能力不能静默覆盖。Selection 必须记录最终版本、来源、Digest、依赖解析、权限 Diff 和选择原因。
Capability Pack 的优先级只影响候选和选择，不得越过 RunKind、Scope、Approval 或 Runner Policy。

### 2.6 数据迁移顺序

正式版数据面按以下顺序演进。后续阶段不得越级创建依赖尚未存在的权威状态。

1. **Capability catalog foundation**：Capability、Version、Dependency、Permission、Evidence Contract、
   Pack、Pack Lock、Candidate 和 Promotion 采用 additive migration；旧 Skill/Tool 数据保持可读。
2. **Compatibility import**：将现有 Skill/Tool 元数据映射为明确来源的 Capability Version；不得在迁移中
   自动提升用户生成内容为 Official。
3. **Run-scoped selection**：新增持久 Tool/Skill/Pack selection 与 Context Compiler 接线；旧 Run 在缺少
   selection 时使用固定兼容策略，不从当前全局状态漂移恢复。
4. **Cognitive state**：依次增加 Task Graph、Evidence、Reasoning Graph 和 Projection；先支持双读/影子写，
   再让生产 Agent 依赖新状态。
5. **Professional tools and packs**：在权威 selection、evidence 和 effect policy 完成后开放 Code、Browser、
   Web、Traffic、MCP 与 Official Packs。
6. **Learning state**：Trajectory、Review、Failure、Replay、Candidate 和 Promotion 独立于 Active Capability；
   发布必须是显式事务。
7. **Profiles and ecosystem**：最后增加 Profile 导入导出、Pack SDK、签名、Registry 和持续运维。

每次数据库迁移必须：

- 先提供旧数据兼容读取和重启恢复测试；
- 使用确定性 backfill 和 bounded batch；
- 记录 schema/version/digest；
- 明确 downgrade 是安全可行、拒绝执行，还是需要备份恢复；
- 不删除仍被 Run、Evidence、Pack Lock 或 Promotion 引用的版本。

### 2.7 Evaluation 定位与基线记录

评测用于质量回归、失败复现、Capability 对照和版本决策，不承担证明 RiftX 全面超过通用 Agent 的职责。

SEC-000 冻结以下记录要求：

- 每次评测记录 RiftX commit、模型和 provider、运行时配置、Tool/Skill/Pack 版本；
- 记录目标 Snapshot/Reset、Scope、预算、随机性、网络条件、开始结束时间和 Artifact；
- 开发集、未公开回归集和真实任务复盘可以并存，不要求所有能力映射为单一数字；
- 外部 Agent 结果只能作为可选参考，不构成强制排名或正式版发布门；
- Agent 相关评测和运行统一使用 conda `agent` 环境；
- SEC-001 才实现可执行 Scenario、Run、Trajectory、Evidence Replay 和 Judge schema。

## 3. Competitor and repository baselines

本决策使用以下研究记录，不主张源码兼容或逐行复制：

| 项目 | 基线 |
| --- | --- |
| LuaN1aoAgent | `51af327c29c2` |
| CyberStrikeAI | `f7ba7070ca74` |
| OpenAI Codex | `757c151a0e92` |
| OpenCode | `4a57013cf8cb` |
| OpenClaw | `26a58bcd92ba` |
| Hermes Agent | `1be70d635488` |
| Claude Code | 2026-08-05 前检索的官方技术文档 |
| RiftX 计划输入基线 | `e40af267` |
| 正式版计划提交 | `357ed38e` |

竞品基线用于说明设计来源和复查上游变化，不作为 RiftX 的运行依赖。

## 4. Rejected alternatives

### 4.1 只增加 Prompt、Skill 或 MCP

拒绝。它不会解决持久选择、证据晋升、权限边界、恢复和能力版本问题。

### 4.2 让 Agent 直接修改正式 Skill

拒绝。一次成功可能是偶然、目标特有或不可重放，必须先成为 Candidate。

### 4.3 立即增加 Pentest RunKind

拒绝在 SEC-000 中实施。仅增加 enum 会使 API、持久化、Effect Inventory、Runner ownership 和恢复语义
不完整。当前先冻结 workload boundary，后续按需要单独设计。

### 4.4 用一个统一高权限 Agent Runtime 处理所有工作负载

拒绝。Code Audit 的默认只读边界、Pentest 的目标授权边界和 General Run 的通用副作用不能被模型提示词
代替。

### 4.5 用 Benchmark 胜负作为正式版完成定义

拒绝。评测是反馈工具；真实专业能力还包含长期上下文、团队知识、操作者协作和难以统一量化的任务质量。

## 5. Consequences

正面影响：

- 后续任务拥有稳定的职责和依赖边界；
- Capability 增长不会天然扩大副作用权限；
- 认知状态和 Evidence 可以独立恢复与审计；
- 自动学习有明确的 Candidate、Replay、Review 和 Promotion 隔离；
- General、Pentest workload 和 Code Audit 的安全语义不会被模糊化。

成本与限制：

- 需要新的持久化实体、迁移和兼容读取；
- Progressive Skill 和工具接入必须经过生产 Runtime，而不是直接拼 Prompt；
- Pentest 在专用 RunKind 完成前仍以 General Run 承载；
- Capability Promotion 会比直接写文件更慢，但可审计、可回滚。

## 6. Implementation mapping

- `SEC-000`：本 ADR、实施账本、依赖和文档一致性检查；
- `SEC-001`：Security Capability Evaluation 可执行骨架；
- `CAP-001`：Capability Domain、Repository、Migration 和 API schema；
- `CAP-100` 至 `CAP-104`：生产能力加载和工具闭环；
- `COG-200` 至 `COG-205`：Evidence-driven Cognitive Runtime；
- `LEARN-600` 至 `LEARN-605`：Capability Learning Flywheel；
- `ECO-800` 至 `ECO-802`：Pack 生态、信任与持续运行。

变更本 ADR 的核心边界必须新增 superseding ADR，并同步更新正式版计划和实施账本。
