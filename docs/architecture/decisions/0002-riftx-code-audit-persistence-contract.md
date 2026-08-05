# ADR-0002：RiftX Code Audit 最小持久化契约

> 状态：Accepted
>
> 日期：2026-08-03（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-102`
>
> 产品基线：`9b5f435fd920c5f0b7a19ca5a39839e2726f333c`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md`
>
> 决策所有者：RiftX contributors；准确作者、审阅者和 Commit 由本文第 8 节的
> provenance 记录保存

## 1. 背景

`AUD-101` 已冻结 `AuditContractRecord` 和 `AuditScan` 的严格领域契约，但 `AUD-102`
还需要把 Project、Snapshot、启动意图、阶段、Scope 和 WorkItem 变成可恢复的数据库事实。
原规格列出了最小字段，却没有完整回答以下实现问题：

- Audit 与 Contract 的外键方向和原子插入顺序；
- Project、Run、Snapshot、Scope 与 WorkItem 的跨授权域约束；
- Snapshot 在数据库中的 seal 生命周期和稳定摘要编码；
- 可变行的 compare-and-set token；
- Scope/Work 重试时的稳定身份；
- 尚未落地的 DistributionRevision 如何避免形成虚假 FK；
- `AUD-102` Repository 与 `AUD-103` aggregate creation UoW 的边界。

这些问题如果留给 ORM 实现临场决定，会形成不可兼容的迁移、并发语义和恢复行为。本 ADR
冻结 `AUD-102` 的最小持久化 contract；它不授权读取 Git、创建 CAS 内容、启动 Temporal 或
执行真实审计。

## 2. 领域名称、枚举与稳定身份

### 2.1 Project 与 VCS

- 数据库表名为 `audit_projects`，严格领域模型名为 `AuditProject`；它表示产品规格中的
  `CodeProject` 概念，Port 名称为 `AuditProjectRepository`。
- `AuditVcsKind` v1 只有 `git`。
- `repository_identity_digest` 在当前单实例部署边界内全局唯一，不按 Engagement 分区。
  命中已有 digest 时，Repository 只有在调用者已通过同一 Project/Engagement 授权且全部
  immutable identity 字段一致时才可返回已有 Project；其他情况使用不泄漏对象存在性的冲突。
- 该全局唯一性不是跨独立 RiftX 实例的全局 registry，也不能被用作未授权的 repository
  existence oracle。

### 2.2 持久化枚举

下列值是 v1 的完整集合，ORM 必须使用数据库 CHECK，mapper 必须再次按严格 enum 解析：

| 类型 | 值 |
| --- | --- |
| `AuditStartIntentStatus` | `pending`, `claimed`, `started`, `retryable`, `outcome_unknown`, `cancelled` |
| `AuditPhaseRunStatus` | `queued`, `running`, `completed`, `failed`, `deferred`, `cancelled`, `not_applicable` |
| `AuditScopeKind` | `file`, `symbol`, `diff_hunk`, `dependency`, `endpoint`, `configuration`, `trust_boundary` |
| `AuditRiskTier` | `low`, `medium`, `high`, `critical` |
| `AuditScopeStatus` | `included`, `analyzed`, `excluded`, `deferred`, `failed` |
| `AuditWorkStatus` | `queued`, `leased`, `running`, `completed`, `failed`, `deferred`, `cancelled`, `outcome_unknown` |

`strategy` 和 `closure_code` 是有界 `AuditToken`，不是自由文本。`closure_reason`、
`error_summary` 等说明字段也必须有字节上限并在写入前脱敏，完整诊断只进入受限 Artifact。

### 2.3 stable_key

- ScopeUnit 与 WorkItem 的 `stable_key` 都是 64 位小写十六进制 SHA-256。
- ScopeUnit 唯一键为
  `(audit_id, snapshot_id, kind, stable_key)`；Diff Audit 因此可以为 base/head Snapshot 保存
  同一种逻辑对象而不碰撞。
- WorkItem 唯一键为 `(audit_id, phase, epoch, stable_key)`。
- `stable_key` 的 payload 必须由对应 Planner 的版本化、canonical identity schema 生成。
  `AUD-102` 只验证 SHA-256 形状和唯一/幂等语义，不自行推导 M2 的 Scope 身份或 M3/M4 的
  Work planning identity。
- 同一唯一键的重试只有在所有 identity/input digest 字段恒等时才返回已有行；同键异内容是
  `RepositoryConflictError`，不能静默覆盖。

## 3. SourceSnapshot seal 与摘要

### 3.1 插入即 sealed

`source_snapshots` 中每一行都表示已经 seal 的 Snapshot。staging、blob ingest、Manifest 构建
和完整性验证发生在数据库行创建之前；未完成的 staging 不以半成品 SourceSnapshot 行表示。

- 新增非空 `sealed_at`，且 `sealed_at >= created_at`。
- Repository 只有 create/get/list，不提供 update/save；复用只按
  `(project_id, snapshot_digest)`，并逐一校验所有 immutable 字段。
- `parent_snapshot_id`、`base_tree_digest`、`patch_digest` 为 all-or-none：三个字段必须同时为
  NULL，或同时非 NULL。后一种形态表示 Retest 派生 Snapshot。
- Snapshot 行创建后，任何字段都不可修改。业务事务失败时不得留下可被读取为 sealed 的
  数据库行；事务外已写入的 CAS staging/bytes 必须保持可识别、可校验、可由后续 GC 回收的
  orphan 状态，但 `AUD-102` 不实现 SnapshotStore 或 GC。

### 3.2 snapshot_digest

v1 identity document 精确为以下三个 key，使用 UTF-8、按 key 排序、无多余空白的 canonical
JSON；digest 字段使用小写十六进制：

```json
{"capture_policy_digest":"<sha256>","materializer_schema_version":"<version>","tree_digest":"<sha256>"}
```

摘要算法为：

```text
snapshot_digest = SHA256(
  UTF8("riftx.source-snapshot/v1")
  || 0x00
  || canonical_identity_json_utf8
)
```

不得使用裸字符串拼接、未加 domain separator 的 payload hash，或只按 `tree_digest` 去重。
Repository/mapper 必须重新计算摘要；数据库中的冗余值不一致时 fail closed。

## 4. 外键方向与授权域

### 4.1 AuditScan、Run 与 Project

`audit_scans` 增加冗余的非空 `engagement_id`、`run_kind` 和执行 Node 绑定：

- `run_kind` 的唯一合法值为 `code_audit`；
- `runs` 增加可被引用的复合唯一键 `(id, engagement_id, kind)`；
- `runs` 另增加 `(id, engagement_id, kind, node_id)` 与 `(id, status)` 唯一候选键；Node ID
  在 Run、Contract 和 Scan 中统一为最多 64 字符；
- `audit_projects` 增加复合唯一键 `(id, engagement_id)`；
- Scan 使用 `(run_id, engagement_id, run_kind, selected_node_id)` 复合 FK 指向 Run，并使用
  `(project_id, engagement_id)` 复合 FK 指向 Project；两个 FK 都使用 `ON DELETE RESTRICT`；
- nullable `(run_id, run_terminal_status)` 复合 FK 指向 `runs(id, status)`；未收敛时 NULL 不阻止
  Run 状态推进，收敛事实写入后则禁止 Run 状态漂移；
- `run_id` 继续全局唯一，禁止已有 `general` Run 获得 AuditScan。

Snapshot、base Snapshot、baseline Audit 与 WorkItem primary Scope 等关系仍使用复合 FK和
Repository join 双重验证同 Project/Audit 授权域。Project 读取还必须重新证明其 Engagement
root owner 存在；Scan 读取必须重验 Snapshot、related Audit、Run Node、model profile、非空
Workflow ID 和 terminal status。调用方传入正确 ID 或数据库启用了 FK 都不是唯一安全边界。
Audit Contract/Scan 的 `model_profile` 最大 255 字符，与权威 Run domain/column 精确一致；不能
让 256 字符的通用 AuditToken 在 PostgreSQL 等长度强制数据库中形成 domain-valid、Run-invalid
的半可持久化状态。

### 4.2 AuditScan 与 AuditContractRecord

为避免不可移植的循环 FK，v1 只建立 Scan 指向 Contract 的单向复合 FK：

- `audit_contracts` 保留 `audit_id UNIQUE`，但 `audit_id` 不反向 FK 到 `audit_scans.id`；
- `audit_contracts` 提供唯一候选键
  `(contract_id, audit_id, contract_digest)`；
- `audit_scans(contract_id, id, contract_digest)` 复合 FK 指向上述候选键；
- `canonical_contract_json` 使用 `Text` 原样保存，不能使用会重新编码的 JSON column；
- canonical contract、所有冗余列和 digest 在 insert/read 时重新解析、重算并交叉验证。

不存在可独立 create 的公共 AuditContract Repository 操作。`AUD-102` 必须提供 session-bound
`create_scan_contract_pair` persistence primitive，在同一事务中先写 Contract、再写 Scan；
任一步失败都回滚两行。`AUD-103` 的完整 creation UoW 调用该 primitive。直接提交 orphan
Contract、先后调用两个 auto-commit Repository，或为打破插入环临时写 NULL 均不允许。

## 5. CAS、幂等与终态保护

### 5.1 state_version

所有可变行使用 `state_version INTEGER NOT NULL`，初值为 1，每次成功 mutation 加 1：

- `audit_projects`；
- `audit_contracts`；
- `audit_scans`；
- `audit_start_intents`；
- `audit_phase_runs`；
- `audit_scope_units`；
- `audit_work_items`。

不可变 `source_snapshots` 不需要 `state_version`。Domain model 不携带 persistence metadata；
Repository Port 使用统一的 `Versioned[T]`/等价只读 wrapper 返回 `value + state_version`。

CAS 的 SQL predicate 必须至少包含主键和 `expected_state_version`，成功更新时原子执行
`state_version = state_version + 1`。rowcount 为 0 时重新区分 not-found 与 version conflict，
并返回稳定的 Repository error。禁止使用 `updated_at`、`status` 或二者组合作为 CAS token；
禁止 read-then-unconditional-save。

### 5.2 lease 与状态

- StartIntent claim、lease reclaim、started/retryable/outcome_unknown/cancelled 更新均使用
  `state_version` CAS。`outcome_unknown` 只能进入 reconciliation，不能直接重新发送 Start RPC。
- PhaseRun 和 WorkItem claim/renew/finish 使用 CAS；过期 lease reclaim 也必须与旧 version 竞争。
- queued/running PhaseRun 的 `output_artifact_ids` 和 `summary_counts` 必须为空；terminal
  PhaseRun 可以保留 partial output，但每个 output Artifact 必须存在且属于同一 Audit 的 Run。
  写入 CAS 锁定并验证 Artifact，get/list/create replay/stale CAS/reopen 对缺失或跨 Run 绑定都
  fail closed；请求中的非法绑定是 conflict，已持久化的非法绑定是 integrity failure。
- Scope risk 只能按 `low < medium < high < critical` 单调提升。
- exact terminal retry 可以返回同一结果；试图以不同 payload 改写 terminal row 必须冲突。
- `cleanup_proof_digest + run_terminal_status` 只能由 caller-owned `AuditRunStateProjector`
  事务写入：同一事务先锁定并把 Run 推进到匹配终态，再使用受限 Scan CAS 记录 convergence；
  auto-commit Audit Repository 不得单独写该事实。读路径对“Run 已终态但 Scan 无 convergence”
  以及任意 status mismatch 都 fail closed；事务异常必须同时回滚 Run 与 Scan。
- SQLite 使用条件 UPDATE/rowcount，必要的 candidate read/write transaction 使用
  `BEGIN IMMEDIATE`；不得依赖 SQLite 忽略的 `SELECT FOR UPDATE`。

### 5.3 create 重放信封

`idempotent create` 比较的是自然键对应的创建业务信封，不把后来合法推进的 lifecycle、lease、
risk elevation 或生成型 row ID/时间当作原始请求差异，也绝不借重放覆盖已有事实：

- 任一请求若 surrogate ID 命中一行、自然键又命中另一行，必须作为 ambiguous identity collision
  冲突，不能依赖数据库返回顺序选择其中一个；
- candidate 必须先按其自身持久 owner/binding 验证：合法的跨 Audit surrogate/natural collision
  统一返回不泄漏归属的 conflict，只有 candidate 按自身 owner 重验仍损坏才返回 integrity
  failure；Contract 的 generated ID 虽不参加正常 replay identity，但若已属于另一 Audit 也属于
  ambiguous collision；
- Project 以全局 repository digest 为自然键，重验 Engagement/VCS/repository identity；展示名和
  default branch 的变化只能走 Project CAS；
- Snapshot 以 `(project_id, snapshot_digest)` 为自然键，除 surrogate ID、created/sealed 时间外，
  source、parent/retest、commit、tree/policy/schema、storage、manifest 和计数字段逐项恒等；
- Contract/Scan pair 以 Audit ID 与 canonical contract digest 为业务身份；generated contract ID 和
  创建时间不用于判定，但 canonical bytes、所有冗余 digest/selection 字段必须恒等；Scan 创建
  重放中的 `snapshot_id=None` 表示“创建时未指定”，允许返回后来已绑定 Snapshot 的同一 Audit，
  非 NULL Snapshot/base 必须精确匹配；
- StartIntent、PhaseRun、ScopeUnit、WorkItem 分别按其数据库自然唯一键重验所有冻结 identity/input；
  Scope 已经单调提高的 risk floor 可以满足较低的原创建 floor，但 create 不负责提升 risk，较高
requested floor 必须冲突并由显式 elevation mutation 处理。

数据库唯一键竞争的 `IntegrityError` 可能携带 canonical contract、绝对路径或 storage locator
作为 SQL parameters。所有 create recovery 必须先退出 driver exception handler，再在新事务中
重验 candidate；对外异常不得保留 SQLAlchemy cause/context，也不得把 SQL/parameters 写入普通
日志或 traceback。仅使用稳定、脱敏的 Repository error/reason code。

无论重放发生在 queued、terminal 或已提升风险之后，返回的都是当前持久对象和当前
`state_version`，绝不把 requested 初始对象写回。AUD-103 的 `client_request_id` 仍负责 API 请求
级 payload 幂等；Repository create envelope 不能替代该记录。

## 6. DistributionRevision 暂存边界

`AuditScan` 的 domain contract 已包含 publication 和 distribution projection 字段，但
`audit_distribution_revisions` 由 `AUD-506` 实现。在该表及同 Audit 复合 FK 落地之前：

- `AUD-102` Repository 不接受 `publication_status=published`；
- 不接受非 NULL 的 `initial_distribution_revision_id`、
  `latest_distribution_revision_id` 或 `publication_finished_at`；
- mapper 遇到这类数据库行必须 fail closed，不能把未受 FK 保护的字符串解释为已发布事实；
- 不创建 placeholder revision，也不把未来 ID 错误指向通用 Artifact。

`AUD-506` 通过新迁移、revision Repository 和原子 Publisher 解除该临时 admission fence。

## 7. 任务边界、迁移与测试后果

### 7.1 `AUD-102` 内

- 八张最小表、严格 mapper、独立 Repository/Ports、unique/check/index/FK；
- `Versioned[T]` wrapper 和 `state_version` CAS；
- session-bound scan+contract pair primitive；
- 同 Scope/授权域验证、稳定排序、有界分页、idempotent create、terminal protection；
- SQLite 并发、重启恢复、损坏行 fail-closed 和 Alembic 全链测试。

### 7.2 明确不在 `AUD-102`

- Git/Preflight、SourceIngestCapsule、SnapshotStore/CAS bytes、hydration、GC；
- API/UI/CLI、Temporal Workflow、StartIntent dispatcher/reconciler；
- Inventory、真实 Scope/Work planning、Detector/Agent、Receipt/Evidence/Finding/Closure；
- `AuditCreationUnitOfWork` application service。完整 draft/start UoW 由 `AUD-103` 实现；
  `AUD-102` 只提供可在同一 `AsyncSession` 内组合的 persistence primitives。

### 7.3 downgrade

AUD-102 migration 的 online downgrade 必须在执行任何 DDL 前证明所有新增 Audit 表为空；
任一表非空即 fail closed，保留 Alembic revision 和全部数据。offline downgrade 无法证明为空，
因此必须 fail closed。不得通过 cascade/drop、虚假回填或忽略 SQLite FK 来实现有损降级。

## 8. 本 ADR 的 provenance 记录

```yaml
provenance_id: RXP-AUD-102-001
task_id: AUD-102
artifact_class: architecture_decision
artifact_version: ADR-0002
paths:
  - docs/architecture/decisions/0002-riftx-code-audit-persistence-contract.md
  - docs/riftx-3-code-audit-development-spec.md
author: Ch1nfo (Git author); Codex task /root/m0_docs_map
authored_at: 2026-08-03T03:39:22+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 8.3"
  - "docs/riftx-3-code-audit-development-spec.md section 8.4"
  - "docs/riftx-3-code-audit-development-spec.md sections 13.2, 13.5, and 13.7"
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-102"
implementation_inputs:
  - RiftX repository baseline 9b5f435fd920c5f0b7a19ca5a39839e2726f333c
  - AUD-101 RiftX-owned Audit domain contracts
  - existing RiftX SQLAlchemy, Alembic, Repository, and SQLite concurrency conventions
public_standard_versions:
  - SHA-256 (FIPS PUB 180-4)
  - JSON (RFC 8259; RiftX canonical encoding is defined by this ADR)
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task /root/aud102_adversarial_design (requirements review); Codex tasks /root/aud102_repository_review and /root/aud102_final_security (independent implementation/security review); Codex task /root (final review)
review_sources:
  - this ADR
  - authoritative specification sections 8.3, 8.4, 13.2, 13.5, 13.7, and AUD-102
  - AUD-101 domain contract and existing RiftX persistence conventions
review_result: approved after all P0/P1/P2 findings were closed and independently reverified
commit: pending_backfill
notes: AUD-102 implements this contract in RiftX-owned domain models, ORM, migration, strict mappers, repositories, transactions, and synthetic tests. PostgreSQL DDL/locking is contract-tested without claiming runtime support until a real PostgreSQL CI matrix exists.
```
