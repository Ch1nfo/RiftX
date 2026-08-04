# ADR-0011：RiftX Code Audit Static Effect 与 Snapshot Mount Authority

> 状态：Accepted
>
> 实施状态：AUD-202C C1/C2a/C2b1 implemented；C2b2 Linux qualification pending
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 所属任务：AUD-202C

## 1. Context

AUD-202A/B 已能把 commit 或 working tree 冻结为 Project-bound content/Manifest CAS，但 analysis
执行仍不能安全消费这些 bytes。直接向 worker 返回 CAS locator、宿主绝对路径或共享目录会绕过
Audit/Run/Snapshot/Node owner 校验，也无法在取消、过期或 Runner 重启后证明访问权已经撤销。

AUD-202C 因此拆成四个可独立验证的内部阶段：

1. C1 冻结 Static Plan、Lease、Pin、Stop Proof 与持久事务权威；
2. C2a 实现 trusted CAS source、backend proof contract、expiry/revocation coordinator 与 restart
   reconciler；
3. C2b1 实现每 effect execution 私有的只读 materialization/mount backend；
4. C2b2 在真实 local-Linux daemon/image 上记录 qualification proof。

本 ADR 冻结 C1/C2a/C2b1 合同，并限定 C2b2 必须在同一合同上资格验证。backend 代码与 mocked
Docker evidence 完成不等于生产可用，也不开放 Runner enqueue、API、Event、Temporal 或产品扫描能力。

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
现有 Code Audit Runner command 继续 deny-all。`target_runner_principal` 是未来 C2b backend session 的
owner generation，不是已开放的通用 Runner command grant。

C2b 必须消费本 ADR 的 Plan/Lease/Pin authority，实现每 effect execution 私有只读 materialization，
并在 backend I/O 前验证 nonce、principal、Node、expiry、Manifest/blob allowlist 和剩余 bytes。请求
另一 Node 必须返回 `audit_cross_node_not_supported`；不得回退 NFS、SMB、共享宿主路径、临时 HTTP、
明文复制或扩展 local locator。

### 2.6 C2a trusted source、backend proof 与 reconciliation

C2a 在产品 dispatch 之外新增内部 `SnapshotMountCoordinator`。激活前依次验证 Lease 存在、backend
owner、same-node 请求、nonce、Runner generation 与 expiry；认证失败之前不得读取 raw CAS locator
或调用 backend。trusted SQL resolver 只从 authoritative `source_snapshots` 行取 opaque content key，
再由 `SnapshotStore.describe` 重新验证 Project/Snapshot/Manifest、storage-key digest、完整 blob
allowlist、file/byte limit 与 descriptor identity。resolver 返回值仍由 coordinator 二次校验，内部 port
不能仅靠实现自证可信。

backend 的 prepare/inspect/stop 结果均为 path-free typed proof，并绑定 Lease/Pin digest、Node、backend
digest 与原 Runner principal。prepare proof 还必须精确匹配 CAS descriptor digest/file count/bytes 与
请求时间；任何 absent/live inspection 在进入状态分支前都先做完整 owner 校验，不能用伪造 absent
绕过清理。stop 只有在零 fd/process、namespace unmounted、Lease/Pin revoked 和 worker path
inaccessible 全部肯定时，才原子写入 terminal Stop Proof；terminal 调用读取并返回同一 durable proof，
不重复执行 backend stop。

restart reconciler 只枚举本 Node 的 bounded nonterminal authority。仍活跃且未过期的 mount 保留；过期
mount 走同一 stop/Stop Proof 路径；issued orphan 尝试 cleanup 后进入 `outcome_unknown`；active 对象
缺失、owner drift 或 inspection 不可用均进入 `outcome_unknown`。无法肯定撤权时 Pin 不被乐观 revoke。
Runner credential 轮换不妨碍旧 generation authority 的检查与撤权。C2a 没有引入新的 Runner family、
enqueue、API/Event、Temporal 或跨 Node hydration。

### 2.7 C2b1 Docker private materialization backend

新增 `DockerSnapshotMountBackend`，其 component digest 绑定 pinned image、non-root container user、
container-private tmpfs、network none、read-only rootfs、root-owned read-only source tree 与 backend schema。
availability 必须证明本机为 Linux、Docker server 为 Linux、image ID 与 pinned digest 恒等，并执行
non-root tmpfs read/write-denial round trip；qualification probe 无论成功或失败都按确定性 owner/name
发现并肯定删除，response loss 不能遗留匿名容器。

prepare 在任何 Docker effect 前读取并验证完整 Snapshot blob set。regular file 仅保留 read/execute
语义，目录为 `0555`，文件为 `0444/0555`，symlink 为 root-contained link；absolute 或逃逸 target
拒绝。tar 只存在于 bounded process memory，并经 `docker cp -` 写入容器私有 `/workspace` tmpfs，
不落宿主明文 path。容器使用 pinned image、network none、read-only rootfs、all capabilities dropped、
no-new-privileges、non-root UID、bounded pids/memory/disk 和 empty-safe environment。

materialization 完成后写入 immutable path-free proof document。non-root probe 对全树执行 lstat、owner/
mode 校验与 regular/symlink bytes hash，拒绝额外类型、写权限或 proof drift。inspect 通过确定性 owner
name 和 labels 找回容器，并重新验证 Plan image/limits、Lease/Pin/Node/backend/principal、Docker
security config 与 proof；仅 running + exact proof 返回 active。stop 只在 stop/remove 后按 container ID
与 name 双重确认 absence，才证明零 process/fd、namespace removed 和 worker path inaccessible。

同一 Lease 的并发激活以 mount key/proof 为物理幂等 identity。一个请求赢得 durable CAS 后，另一个
请求若观察到相同 active proof，返回 exact convergence，不得执行 cleanup；不同 proof 或 owner drift
继续 fail closed。C2b1 仍未向 Runner/API/Temporal 注册执行入口。

## 3. Explicit non-goals

C1/C2a/C2b1 不实现：

- 真实 local-Linux pinned-image qualification evidence 与 production scheduler/service wiring；
- analysis command admission/exec、fd broker 或对 worker 暴露宿主 mount path；
- source Node 到 analysis Node 传输、远程 CAS、mTLS hydration；
- Content Sandbox、content parser、Detector、Scanner、模型或动态 Execution Plan；
- Snapshot reader、Retention/GC、Artifact、API、CLI、WebUI、Event、Start 或 Temporal dispatch。

因此 AUD-202C 仍为 `in_progress`，只有 C2b2 在真实 backend 上证明 private read-only mount、撤权和重启
收敛后才能标记 completed。

## 4. Consequences

- 后续 static execution 不需要再发明 owner、materialization 或 reconciliation schema，可以围绕
  durable Lease/Pin 与 qualified Docker backend 接入。
- Runner generation 轮换后仍可读取旧 authority 进行撤权；新 mount 签发只接受当前 principal。
- raw CAS locator 不进入 authority envelope，知道 digest、relative path 或同 UID 宿主身份都不足以访问
  Snapshot。
- C1 增加持久事实但不增加可执行能力，保持 AUD-106 的 Code Audit zero-enqueue fence。
