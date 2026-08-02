# ADR-0003：RiftX Code Audit 应用层与原子创建契约

> 状态：Accepted
>
> 日期：2026-08-03（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-103`
>
> 产品基线：`0b48b957eb4879d402f7be03b2a96e14cd9084bd`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md`
>
> 前置决策：ADR-0001、ADR-0002
>
> 决策所有者：RiftX contributors；准确作者、审阅者和 Commit 由本文第 11 节的
> provenance 记录保存

## 1. 背景与结论

`AUD-102` 已经提供严格 Audit domain、持久化表和可在同一
`AsyncSession` 中组合的 primitive，但现有通用 Run Application Service 和 Repository
具有各自的提交、目录创建、Workflow 调用和 `general` Run 默认值。顺序调用这些接口无法
原子创建 Code Audit，也可能在失败后留下孤立 Engagement、Run、Event、Contract 或 Scan。

`AUD-103` 因此新增 RiftX 自有的 `AuditApplicationService`、
`AuditCreationUnitOfWork` 和不可变的 `audit_client_requests`。一个 draft
必须在一个数据库事务中创建或验证完整聚合；读取和请求重放必须重新验证全部 ownership
绑定和 Audit↔Run 状态映射；控制接口在本任务只返回只读计划，不执行任何效果。

本决策不引入任何第三方 Code Security Provider、运行时或代码，也不读取被审计仓库。

## 2. 应用层边界

### 2.1 AuditApplicationService

`AuditApplicationService` 暴露以下能力：

- `create_draft`：通过单一 creation UoW 创建 draft，或执行严格的请求重放；
- `get`、`list`：读取完整、已验证的 Audit aggregate；
- `pause`、`resume`、`cancel`：只做门禁并返回
  `AuditControlPlan`，不修改状态。

Service 可以依赖配置、creation UoW、aggregate read Port、纯 factory、时钟和 ID source。它
不得依赖 Git adapter、SnapshotStore、Temporal client、Runner、Scanner、stopper、通用
`RunApplicationService` 或可创建目录的 workspace service。

Feature Flag 不能决定是否构造或注册 Service。Service 始终存在，方法级 admission fence
决定哪些操作可用，避免关闭功能后丢失读取、暂停、取消和安全停止能力。

### 2.2 完整 Audit aggregate

本 ADR 中的“完整 aggregate”至少包含：

~~~text
AuditScan + Audit state_version
AuditContractRecord + Contract state_version
AuditProject + Project state_version
Engagement
Run
AuditClientRequest
~~~

Contract 的 canonical bytes 仍是合同恢复源，但不能复制到 client-request 行、Event、普通日志
或控制计划。任何返回 API schema 的 projection 都从上述已验证 aggregate 构造，不能分别调用
多个 Repository 后在 Service 中凭 ID 拼接。

## 3. 不可变 client-request 记录

### 3.1 表与字段

新增 `audit_client_requests`。v1 行的最小字段冻结为：

~~~text
client_request_id
operation = create_draft
request_schema_version = riftx.audit-create-draft-request/v1
request_digest
audit_id
run_id
project_id
engagement_id
contract_id
contract_digest
temporal_workflow_id
created_at
~~~

约束如下：

- `client_request_id` 是全局唯一幂等键；`audit_id` 也唯一，一个 Audit 只有一个
  创建请求事实；
- `operation` 和 `request_schema_version` 使用 CHECK 与严格 mapper 双重验证；
- 所有 ID/digest/workflow 绑定均为非空；可建立的 FK 使用 `ON DELETE RESTRICT`；
- 该行不可变，不带 `state_version`，Port 只提供 insert 和随 aggregate 的严格读取；
- mapper 必须重新加载实际 Scan、Contract、Project、Engagement 和 Run，逐项证明同域；
- `temporal_workflow_id` 必须等于
  `riftx-code-audit-{audit_id}`，即使本任务不启动 Temporal；
- 数据库唯一键或 FK 不是唯一信任边界；FK 关闭、旧数据或 raw corruption 下也必须 fail
  closed。

该表明确不得保存：

- 规范化或原始请求 payload；
- source path、repository path 或 workspace source path；
- preflight token、token hash 或 reservation；
- `canonical_contract_json`；
- Prompt、源代码、模型内容或 SQL/driver 诊断。

### 3.2 请求摘要

服务端计算请求摘要，客户端不能直接提供或覆盖。算法冻结为：

~~~text
request_schema_version = "riftx.audit-create-draft-request/v1"
request_digest = SHA256(
  UTF8(request_schema_version)
  || 0x00
  || canonical_normalized_caller_payload_utf8
)
~~~

canonical payload 使用 UTF-8、稳定 key 排序、无多余空白和枚举 wire value。它必须覆盖所有
会影响 Engagement/Project、Run、Contract 或 Scan 初始事实的调用方字段，包括显式
`engagement_id` 和授权/目标 identity；必须排除：

- `client_request_id` 自身；
- 服务端生成的 Audit/Run/Project/Contract/Event ID；
- 服务端时间戳与 persistence `state_version`；
- 本任务尚不接受或预留的 preflight token。

`AUD-103` 的 request surface 因而没有 preflight 字段。M2 若让 reservation 成为创建语义，
必须先版本化本摘要 schema，或纳入稳定的 preflight plan identity/digest；不得把新增的
caller-owned token/plan 从幂等比较中静默排除。

若某个调用方字段会改变最终聚合却未进入摘要，测试必须失败。若字段只改变服务端生成 ID 或
时间，则不得导致摘要变化。摘要比较使用 constant-time primitive。

### 3.3 重放语义

`create_draft` 在事务中的第一项持久化检查是 client-request：

- 同一 `client_request_id`、相同 operation/schema/digest：读取并返回当前完整
  aggregate，`replayed=true`，不执行任何写入；
- 同一 key、不同 operation/schema/digest：返回
  `audit_idempotency_conflict`，不得泄漏原请求或已存在对象细节；
- 记录存在但任一 aggregate binding 缺失、跨域或损坏：fail closed，不能当成“未创建”重新写；
- 精确重放不重新生成、比较或替换 server-generated ID，也不把初始 draft candidate 与当前
  lifecycle 作相等比较。

因此，一个已经推进到 queued、running、terminal 或提高过 `state_version` 的 Audit，
精确 create 重试返回的仍是当前持久状态和当前版本；它绝不把对象写回 draft。

## 4. AuditCreationUnitOfWork

### 4.1 唯一事务边界

`AuditCreationUnitOfWork` 是 create_draft 的唯一写边界。Application Port 不向 Service
暴露 `AsyncSession`，而是提供 one-shot aggregate operation。SQLAlchemy adapter
在 SQLite 使用 `serialized_write`/`BEGIN IMMEDIATE`，在支持行级锁的数据库
使用等价的串行化 read-decision-write 边界。

一次首次创建的顺序必须是：

~~~text
AuditApplicationService
  0. audit.enabled admission fence
AuditCreationUnitOfWork / one serialized transaction
  1. 按 client_request_id 检查 exact replay/conflict
  2. 按 repository identity 解析权威 Project 与 Engagement
  3. 调用纯 AuditDraftAggregateFactory 构造绑定权威 ID 的最终事实
  4. 创建缺失的 Engagement / Project，或严格复用已验证对象
  5. 创建 Run(kind=code_audit, status=created)
  6. 创建 RunEvent(sequence=1, event_type=run.created)
  7. 用 session-bound primitive 创建 Contract + AuditScan
  8. 创建 RunEvent(sequence=2, event_type=audit.created)
  9. 创建 audit_client_requests 行
 10. commit exactly once
~~~

任一步失败必须回滚本次事务的全部新事实。不得在 commit 后补写 Event 或 client-request，也
不得在事务外先创建 Engagement、Run、workspace 或目录。

### 4.2 权威 Project/Engagement 解析

Project 的自然身份是全局 `repository_identity_digest`。UoW 必须先在事务内决定实际
Project，再生成引用它的最终 Contract/Scan：

1. 已有 Project：加载其真实 Engagement root，验证 repository/VCS/authorization identity；
   显式 requested Engagement 必须与真实 owner 一致；
2. 不存在 Project：验证指定 Engagement，或在本事务内构造新的 Engagement；随后用
   session-bound primitive 创建 Project；
3. 并发唯一键竞争后，失败方只能在新事务重新解析胜出的 Project，不能继续使用事务外预生成
   的 surrogate ID；
4. 跨 Engagement 或授权不一致返回不泄漏 Project 是否存在的 conflict。

Factory 必须是纯内存构造器：不得查询数据库、读取 Git/source、创建目录、调用网络或启动
Workflow。它可以使用注入的时钟/ID source，但最终 `build` 必须接收 UoW 已解析的
权威 Project/Engagement，并重新生成所有相关绑定。UoW 对 factory 输出再次运行严格 domain
和 mapper-equivalent binding 校验，不能信任调用方预拼接的 ID。

### 4.3 Run、workspace 与 Contract

首次创建的 Run 必须满足：

- `kind=code_audit`；
- `status=created`；
- Engagement 与 Project/Scan 相同；
- `node_id` 和 `model_profile` 来自冻结 Contract；
- `temporal_workflow_id=riftx-code-audit-{audit_id}`；
- workspace 字段只保存 RiftX 管理输出目录的受控字符串，绝不指向 source repository。

`AUD-103` 不执行 `mkdir`，也不验证或读取该路径内容。Contract 与 Scan 继续使用
ADR-0002 的 `create_scan_contract_pair(session, ...)`，先 Contract 后 Scan，同事务
flush，不自行 commit。

### 4.4 禁止组合 auto-commit Repository

以下做法不满足本 ADR：

- 顺序调用现有 EngagementRepository、RunRepository、RunEventRepository 和
  AuditRepository 的公共 create；
- 调用通用 `RunApplicationService.create_run` 后再“补齐” Audit；
- 用 compensating delete 模拟原子性；
- 先提交 Run/Contract，再以 background task 写 Event 或 client-request；
- 在进程内 lock 下组合多个独立事务。

UoW 必须调用 session-bound persistence primitive，且只有最外层 transaction 可以 commit。

### 4.5 IntegrityError 恢复

唯一键竞争可能把 canonical contract 或绝对路径作为 driver exception 的 SQL parameters。
恢复流程必须：

1. 在 driver `IntegrityError` handler 中只完成 rollback/分类所需的最小动作；
2. 离开该 exception handler，丢弃原 driver exception；
3. 打开新的 transaction/session；
4. 重新检查 client-request、Project natural identity 和完整 aggregate bindings；
5. exact replay 返回当前 aggregate；同键异 payload 返回稳定 conflict；仍不可解释的碰撞返回
   脱敏 integrity/conflict error。

不得在 failed transaction 中查询恢复，不得从原 `IntegrityError` 链式抛出公开异常，
不得记录 SQL、parameters、canonical contract 或路径。公开 exception 使用稳定 reason code，
并显式去除 driver cause/context。

数据库 engine 默认启用 SQLAlchemy `hide_parameters` 作为纵深防御。`OperationalError`、
`StatementError`、`DataError` 等非幂等竞争型 `SQLAlchemyError` 不进入 replay recovery；UoW 必须
同样先离开 driver handler、丢弃原异常，再抛出无 cause/context 的脱敏
`RepositoryUnavailableError`，Service 将其映射为稳定的 `audit_persistence_unavailable`。

## 5. Event 契约

Audit 没有独立 AuditEvent 表。所有 Audit 事件都是关联 Run 上的 `RunEvent`。

draft 首次创建固定产生且只产生：

| sequence | event_type | 所属 |
| --- | --- | --- |
| 1 | `run.created` | 关联 Code Audit Run |
| 2 | `audit.created` | 同一个 Run |

两个 Event 与聚合在同一事务写入。重放不追加 Event。payload 只允许安全的 ID、枚举、计数和
digest；不得包含 source/workspace 绝对路径、request payload、preflight token、canonical
contract、Prompt、源代码或模型输出。

## 6. 完整读取与状态映射

### 6.1 单 session aggregate load

`get`、`list` 和 exact create replay 必须复用同一个 aggregate loader：

- 一次调用只使用一个数据库 session/一致读取快照；
- list 先做有界、稳定排序的 ID page，再在同一 session eager/batched 加载完整 aggregate，
  不允许 N+1 独立 session；
- 验证 Scan↔Contract、Scan↔Project↔Engagement、Scan↔Run、Run kind/node/model/workflow、
  canonical contract/digest，以及 client-request 的全部冗余绑定；
- mapper 必须从实际 owner root 重新证明 ownership；调用方给出相同 ID 或 FK 存在不构成证明；
- 页面中任一行损坏时整次读取 fail closed，不能静默漏掉损坏 Audit。

### 6.2 唯一 Audit↔Run mapping policy

第 6.6 节的状态表由一个集中、纯的 mapping policy 实现。create、get/list、exact replay、
control plan 和后续 `AuditRunStateProjector` 必须调用同一 policy；API/UI 不复制
switch/case。

draft create 只接受 `Audit=draft ↔ Run=created`。其他生命周期必须按第 6.6 节以及
cleanup convergence/publication facts 得到唯一 Run 状态。任何不在策略内的组合返回
`audit_run_state_conflict`，不能单边修复、猜测较新一侧或把损坏对象返回给调用方。

## 7. pause/resume/cancel 只读计划

### 7.1 AuditControlPlan

`AUD-103` 的三个控制方法只加载并验证 aggregate，然后返回不可变计划：

~~~text
operation: pause | resume | cancel
disposition: transition | reconcile | already_satisfied | safety_only
audit_id
run_id
current_audit_lifecycle
current_run_status
target_audit_lifecycle
target_run_status
required_effect
expected_audit_state_version
reason_code
~~~

计划中的 `expected_audit_state_version` 是后续 projector 使用的 CAS token。计划本身
不预留版本、不锁定对象，也不代表效果已经发生。

### 7.2 门禁矩阵

| 操作 | 当前状态/事实 | disposition | 目标与 required_effect |
| --- | --- | --- | --- |
| pause | running / waiting_approval | transition | Audit/Run → pausing；`pause_workflow_then_project` |
| pause | pausing | reconcile | Audit/Run → paused；`reconcile_pause` |
| pause | paused | already_satisfied | 保持 paused；`none` |
| pause | 其他状态 | error | `audit_not_pauseable` |
| resume | paused 且 Feature 开启 | transition | Audit/Run → running；`resume_workflow_then_project` |
| resume | 其他状态 | error | `audit_not_resumable` |
| cancel | cleanup convergence 前的普通活跃状态 | transition | Audit/Run → cancelling；`fence_new_effects_and_stop` |
| cancel | cancelling，或尚未 convergence 的 cleaning | reconcile | 向 cleaning/stop proof 收敛；`reconcile_cancel_stop` |
| cancel | cleanup 已 convergence、publication lifecycle 或任一 terminal | safety_only | 保持当前状态；`safety_stop_sweep_only` |

“cleanup 已 convergence”以同 Audit 的 `cleanup_proof_digest + run_terminal_status` 为准，
不能仅凭进程退出或 lifecycle 名称判断。cancel 的 safety-only 计划仍是有效结果：它允许后续
router 重做无害的 stopper/destroy sweep，但不得把 completed/failed/cancelled 或发布状态改写成
cancelling。

本任务的控制方法严禁：

- 更新 Scan、Run 或 CAS version；
- 追加 Event；
- signal/query/start Temporal；
- 调用 Execution/Browser/Target HTTP/Capsule stopper；
- 创建或删除文件、目录、workspace；
- 返回“已暂停”“已恢复”或“已安全取消”的虚假完成结果。

实际 mutation、Event 和效果路由属于后续 caller-owned
`AuditRunStateProjector`/`RunWorkflowControlRouter`。

## 8. Feature Flag 的 flag-first 语义

`audit.enabled=false` 时：

- 所有 `create_draft` 在访问 creation UoW 前直接返回 `feature_disabled`；
- 即使 `client_request_id` 是已存在 exact replay，也同样拒绝；Feature Flag 优先于幂等
  重放；
- `get`、`list`、`pause`、`cancel` 继续可用；
- `resume` 在读取/生成计划前返回 `feature_disabled`；
- Service、cancel/stop adapter 和安全清理能力不得从 composition root 消失。

关闭开关是 admission fence，不是删除数据或宣称已有效果已停止。`AUD-103` 只提供控制
计划；自动围栏、投影与物理停止由后续 router/reconciler 完成。

Composition root 必须把 `AuditApplicationService` 作为 `ControlPlane` 的非可选依赖始终
构造。它只注入共享 `session_factory` 上的 creation UoW、完整 aggregate read adapter、启动时
冻结的 `audit.enabled` 和 `audit.temp_root` 路径策略，不注入通用 Run Service、Temporal、source
adapter 或文件效果。`audit.temp_root` 在本任务只是持久 Run workspace 字符串的受控 Audit 根；
本任务不创建该根或其子目录。首次真实 provision 前必须另行冻结子命名空间、引用期 GC pin、
realpath/symlink/no-follow 复验规则。

## 9. AUD-103 明确排除

本任务不实现或触发：

- Preflight plan/token 的创建、hash、reservation、consume 或 expiry；
- Git 命令、source path 读取、realpath、Snapshot 或 CAS；
- Temporal Workflow、Signal、Query、StartIntent 或 dispatcher；
- Scanner、Detector、Agent、Runner、模型调用或动态验证；
- `mkdir`、workspace provisioning 或任何 filesystem write；
- pause/resume/cancel 的数据库 mutation、Event 或物理效果；
- HTTP API/UI/CLI 接线。

M2 的 `AUD-201` 在不改变 client-request 和单提交原则的前提下，把 preflight
reservation 扩展进同一个 creation UoW；不得另建第二个“先 reserve、后 create”提交链。

## 10. 迁移与验收

### 10.1 Migration

- migration 只新增 `audit_client_requests` 及必要 unique/check/index/FK；
- upgrade 不改写已有 Run/Audit 数据；由于旧 Audit 没有可证明的 caller request digest，
  online upgrade 必须先锁定 `audit_scans` 并证明为空，非空时在任何 DDL 前 fail closed，等待
  显式版本化的 legacy compatibility migration，严禁伪造 request 回填；PostgreSQL offline SQL
  必须包含等价的执行时 guard；
- online downgrade 在任何 DDL 前证明表为空，非空立即 fail closed；
- offline downgrade 无法证明为空，因此 fail closed；
- PostgreSQL offline upgrade SQL 必须先输出 `ACCESS EXCLUSIVE` lock，再执行 legacy emptiness
  guard，最后才允许 request-table DDL；只检查不锁表不构成等价保护；
- `Database.create_schema()` 这类 embedded/runtime metadata bootstrap 不能绕过 upgrade guard；
  只要数据库带 `alembic_version` 且 metadata 有缺表，就必须在任何 DDL 前拒绝并要求先运行
  Alembic；没有 Alembic state 的 embedded bootstrap 在 `audit_scans` 已存在而 request 表缺失
  时，也必须先以同等写锁证明旧 Scan 为空；
- 禁止 cascade/drop、自动删除 request 行或伪造回填。

### 10.2 必须通过的测试

领域/应用单元测试：

- request schema、domain-separated digest、constant-time exact/different comparison；
- digest 对每个 caller-owned 字段敏感，对 generated ID/time/version 不敏感；
- Feature 关闭时 create UoW 零调用，exact replay 也 `feature_disabled`；
- get/list/pause/cancel 在关闭时可用，resume 被拒绝；
- pause/resume/cancel 全 lifecycle 矩阵和计划 CAS version；
- control 方法零 Repository write、零 Event、零 Workflow/stopper/filesystem 调用；
- pure factory 不能访问 persistence、Git、Temporal 或 filesystem。

真实 persistence/integration 测试：

- 成功后恰有一个正确的 Engagement/Project 解析结果、Run、Contract、Scan、request row 和两个
  顺序 Event；
- 每个写入阶段 failpoint 都证明完整回滚且没有目录副作用；
- exact retry 返回同 Audit 的当前 lifecycle/version，不新增 Event、不重写 draft；
- 同 key 异 payload 返回稳定 conflict；
- 两连接同 key 并发恰一创建，失败方在新事务恢复；
- 不同 request key 并发创建同 repository digest，共享一个权威 Project、各自产生 Audit；
- cross-Engagement/Project/Run/Contract/request binding 与 raw corruption fail closed；
- exact client-request key 与 Project natural identity 的重复损坏 rowset 均 bounded load 并
  fail closed；owner filter 同时考虑真实 Project owner 与 Scan 冗余 owner，不能在完整 loader
  前静默隐藏损坏；
- 每个非法 Audit↔Run 组合返回 `audit_run_state_conflict`；
- Event 和所有 SQLAlchemy failure 的公开异常/context/日志均不含 source path、canonical
  contract、token 或 SQL parameters；
- mapper dispose/reopen 后仍可恢复完整 aggregate；
- migration 从最早支持版本升级，空表 downgrade 成功，非空与 offline downgrade 拒绝；
- 从旧 revision 升级时若已有 Audit，必须在创建 request 表前拒绝且原数据/版本不变；
- runtime metadata bootstrap 对非空 legacy Audit 必须在 request 表 DDL 前拒绝；对 Alembic
  管理的空旧 schema 也不得补表或制造 revision/schema 分叉；
- Feature Flag 开/关两种启动配置下，`ControlPlane.audit_service` 都存在且不创建 workspace；
- general Run create/control/replay 行为完全不变。

## 11. 本 ADR 的 provenance 记录

~~~yaml
provenance_id: RXP-AUD-103-001
task_id: AUD-103
artifact_class: architecture_decision
artifact_version: ADR-0003
paths:
  - docs/architecture/decisions/0003-riftx-code-audit-application-contract.md
  - docs/riftx-3-code-audit-development-spec.md
author: Ch1nfo (Git author); Codex task /root/aud103_contract_review
authored_at: 2026-08-03T06:21:25+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 6.6"
  - "docs/riftx-3-code-audit-development-spec.md sections 13.5, 16.3, 17.1, and 20.4"
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-103"
  - "docs/architecture/decisions/0002-riftx-code-audit-persistence-contract.md"
implementation_inputs:
  - RiftX repository baseline 0b48b957eb4879d402f7be03b2a96e14cd9084bd
  - AUD-101 strict Audit domain contracts
  - AUD-102 session-bound persistence primitives
  - existing RiftX transaction and RunEvent conventions
public_standard_versions:
  - SHA-256 (FIPS PUB 180-4)
  - JSON (RFC 8259; canonical encoding is frozen by this ADR)
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex tasks /root/aud103_app_patterns and /root/aud103_contract_review; Codex task /root (final review)
review_sources:
  - this ADR
  - authoritative specification sections listed above
  - current RiftX application, persistence, transaction, Run, and Audit contracts
review_result: accepted as the AUD-103 implementation and acceptance-test contract
commit: pending_backfill
notes: AUD-103 is intentionally database-only. M2 extends the same creation UoW with preflight reservation; later projector/router tasks own mutation and physical control effects.
~~~
