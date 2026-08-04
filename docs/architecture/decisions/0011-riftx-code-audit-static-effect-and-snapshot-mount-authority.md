# ADR-0011：RiftX Code Audit Static Effect 与 Snapshot Mount Authority

> 状态：Accepted
>
> 实施状态：AUD-202C C1 implemented；C2 backend/reconciliation pending
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 所属任务：AUD-202C

## 1. Context

AUD-202A/B 已能把 commit 或 working tree 冻结为 Project-bound content/Manifest CAS，但 analysis
执行仍不能安全消费这些 bytes。直接向 worker 返回 CAS locator、宿主绝对路径或共享目录会绕过
Audit/Run/Snapshot/Node owner 校验，也无法在取消、过期或 Runner 重启后证明访问权已经撤销。

AUD-202C 因此拆成两个可独立验证的内部阶段：

1. C1 冻结 Static Plan、Lease、Pin、Stop Proof 与持久事务权威；
2. C2 实现每 effect execution 私有的只读 materialization/mount backend、expiry/revocation stopper 与
   restart reconciler。

本 ADR 冻结完整 C1 合同，并限定 C2 必须在同一合同上实现。C1 完成不代表存在可用的 mount backend，
也不开放 Runner enqueue、API、Event、Temporal 或产品扫描能力。

## 2. Decision

### 2.1 AuditStaticEffectPlan v1

新增 `riftx.audit-static-effect-plan/v1`。C1 operation family 只接受：

~~~text
snapshot_materialize
snapshot_mount
~~~

Plan 由 `riftx_policy` 创建，使用 domain-separated canonical JSON 计算 `plan_digest`，并冻结
Project/Audit/Run/Snapshot、resolved durable Node identity、`private_materialization` backend、backend/
image/policy digest、单一只读 Snapshot mount、bounded output root、clean environment、network none、
资源上限、input Manifest/output contract 和 policy version/time。

CAS `content_storage_key` 与 `manifest_storage_key` 只在持久化 owner 校验时以
`riftx.snapshot-storage-key-digest/v1` 做 role-separated digest；Plan、普通 API、Event 和日志不保存或
返回 raw locator。Plan 固定 `snapshot_reference_role=primary`，创建时必须证明：

- Audit、Run、Snapshot、Project 和 resolved Node identity 全部恒等；
- Run kind 为 `code_audit`，Audit 已绑定该 primary Snapshot；
- durable `snapshot_references` primary owner 存在；
- Snapshot/Manifest digest、storage-key digest 与 sealed `source_snapshots` 行恒等；
- input byte limit 不小于 sealed Snapshot included bytes。

3.0 对外的 `local` selector 必须先解析成一个 durable Node identity；Plan/Lease 存的是该 resolved ID，
不能把所有部署共用的字符串 `local` 当作跨重启 owner，也不能把另一个 Node 当作 fallback。

### 2.2 SnapshotMountLease 与 Pin

新增 `riftx.snapshot-mount-lease/v1` 与 `riftx.snapshot-mount-pin/v1`。Lease 绑定：

~~~text
lease_id + nonce_hash
Project/Audit/Run/Snapshot/Manifest
plan_id + plan_digest
effect_execution_id
target Runner instance_id + epoch
target Node/backend + backend digest
allowed blob digests + max bytes + expiry + mount policy digest
~~~

raw nonce 只在签发结果中出现，数据库仅保存 domain-separated hash。Pin 复制 Lease 的 immutable
Audit/Run/Snapshot/plan/backend/Runner owner facts及 identity digest；一个 Lease 只能有一个 Pin，一个
effect execution 只能有一个 Lease。Lease 与 Pin 在同一事务原子签发，重复请求只有完整 canonical
authority 和 nonce hash 恒等时才是 exact replay。

持久化前重新通过 strict model validation，不能依赖 Pydantic `model_copy(update=...)` 的未验证更新。
内部状态迁移均以完整 model rebuild 重跑 lifecycle、timestamp、digest 和 owner invariants。

### 2.3 Lifecycle、撤权与 Stop Proof

Lease 状态机固定为：

~~~text
issued -> active -> revocation_pending -> revoked
                 -> expiration_pending -> expired
issued|active|pending -> outcome_unknown
~~~

Pin 状态机固定为：

~~~text
pending -> active -> revocation_pending -> revoked
~~~

正常边严格使用连续 `state_version` CAS。expiry 由 `requested_at >= expires_at` 决定，不能把已过期
Lease 标成普通 revocation，也不能把未到期撤销伪装成 expiry。terminal Lease/Pin 保留内部 opaque
mount key 仅用于 proof/reconciliation identity；它不是 path，也不进入外部投影。

新增 `riftx.snapshot-mount-stop-proof/v1`。肯定 Stop Proof 必须同时证明：

- active fd count 为零；
- active process count 为零；
- mount namespace 已卸载；
- backend Lease capability 已撤销；
- Pin 已撤销；
- worker path 不可访问。

Proof 绑定 Lease/Pin/Plan identity digest、effect execution、Project/Audit/Run/Snapshot/Manifest、Node、
backend digest、Runner principal、mount-key digest、disposition 与 stopped time。Stop Proof 插入、Lease
terminal CAS 和 Pin revoke CAS 在单一事务提交；任一 owner 漂移、旧 state version 或 proof drift 都不
产生部分终态。无法肯定证明时只能进入 `outcome_unknown`，不能伪造 revoked/expired。

### 2.4 Durable authority 与 downgrade

C1 新增四张表：

~~~text
audit_static_effect_plans
snapshot_mount_leases
snapshot_mount_pins
snapshot_mount_stop_proofs
~~~

每行保存 canonical JSON 与安全关键冗余列；读取时重新构造 strict domain model，并逐列验证 canonical
bytes、digest、owner 和 lifecycle。Repository 提供 Plan insert-only replay、Lease/Pin atomic issue、
pair CAS、atomic stop proof 和 bounded same-node reconciliation listing。

迁移 head 为 `9c2e4f6a8b10`。存在任一 C1 fact 时 downgrade fail closed；跨越 Snapshot reference、Preflight
Plan 或 Preflight Job 边界时，旧边界的不可逆事实检查必须在 C1 执行任何 DDL 之前完成，避免先删空
C1 表再被旧迁移拦截。

### 2.5 Capability boundary

C1 不增加 `RunnerOperationFamily`、`RunnerResourceKind`、Runner command protocol 或 enqueue allowlist。
现有 Code Audit Runner command 继续 deny-all。`target_runner_principal` 是未来 C2 backend session 的
owner generation，不是已开放的通用 Runner command grant。

C2 必须消费本 ADR 的 Plan/Lease/Pin authority，实现每 effect execution 私有只读 materialization，
并在 backend I/O 前验证 nonce、principal、Node、expiry、Manifest/blob allowlist 和剩余 bytes。请求
另一 Node 必须返回 `audit_cross_node_not_supported`；不得回退 NFS、SMB、共享宿主路径、临时 HTTP、
明文复制或扩展 local locator。

## 3. Explicit non-goals

C1 不实现：

- 实际目录 materialization、mount namespace、fd broker、unmount、目录删除或 filesystem proof；
- expiry scheduler、stopper、Runner restart backend inspection/reconciliation；
- source Node 到 analysis Node 传输、远程 CAS、mTLS hydration；
- Content Sandbox、content parser、Detector、Scanner、模型或动态 Execution Plan；
- Snapshot reader、Retention/GC、Artifact、API、CLI、WebUI、Event、Start 或 Temporal dispatch。

因此 AUD-202C 仍为 `in_progress`，只有 C2 在真实 backend 上证明 private read-only mount、撤权和重启
收敛后才能标记 completed。

## 4. Consequences

- C2 不需要再发明 owner schema，可以围绕 durable Lease/Pin state machine 实现 backend。
- Runner generation 轮换后仍可读取旧 authority 进行撤权；新 mount 签发只接受当前 principal。
- raw CAS locator 不进入 authority envelope，知道 digest、relative path 或同 UID 宿主身份都不足以访问
  Snapshot。
- C1 增加持久事实但不增加可执行能力，保持 AUD-106 的 Code Audit zero-enqueue fence。
