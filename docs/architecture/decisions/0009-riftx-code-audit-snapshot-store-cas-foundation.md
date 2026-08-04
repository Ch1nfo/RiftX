# ADR-0009：RiftX Code Audit SnapshotStore/CAS Foundation

> 状态：Accepted
>
> 实施状态：AUD-202A implemented
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 所属任务：AUD-202A

## 1. Context

AUD-102 已冻结 `SourceSnapshot` 的 insert-is-seal 数据库语义，但没有实现 Snapshot bytes、CAS、
引用或 crash recovery。AUD-202B 需要一个先于 Git materializer 存在、可用 synthetic staging tree
独立验证的存储边界。这个边界必须满足以下条件：

- bytes 位于所有 source root 之外，不能依赖原 commit/dirty tree 重建；
- 同一 project 内可按完整 Snapshot/Manifest/blob metadata 安全复用；
- 知道 snapshot digest、相对路径或另一个 project 的 locator 不能形成读取 oracle；
- 半写、损坏、power loss 和并发相同 publish 不得产生可读的伪 sealed object；
- 数据库引用必须同时绑定 Audit、Project 与 Snapshot，不能只靠一个可猜 ID；
- 本任务不能提前实现 Git capture、最终 Manifest 决议、mount/pin、retention/GC 或 Start。

## 2. Decision

### 2.1 CAS identity 与 opaque locator

`riftx.snapshot-cas-object/v1` descriptor 固定绑定：

~~~text
project_id
snapshot_digest
manifest_digest
object_type=tree
sorted unique blobs[
  relative_path
  blob_digest
  size
  mode
  object_type=regular_file|symlink
]
file_count
total_bytes
~~~

`descriptor_digest` 使用 domain-separated canonical JSON SHA-256。内部 locator 固定为
`snapshot-cas:v1:<descriptor_digest>`；它不是磁盘路径，不进入普通 API、Event 或日志。因为
`project_id` 是 descriptor 的一部分，同样 Snapshot/Manifest/blob metadata 在不同 Project 得到不同
locator。Snapshot 复用仍以规范要求的 Project owner 为界，不建立跨 owner 全局 digest oracle。

AUD-202A 的 `manifest_digest` 只冻结未来 AUD-202B Source Manifest 的权威 digest 与 CAS index binding；
本任务不定义最终 Manifest entry 的 capture decision、language/classification 或 Git provenance。

### 2.2 put、fsync 与 insert-is-seal

`LocalSnapshotStore.put_staged_tree` 先验证 staging root 与 store root 不重叠，并逐项验证声明的
regular file/symlink type、mode、size 和 digest。实现把 source staging 复制到 SnapshotStore 自己的
`staging/`，因此 publish staging 与最终 `objects/` 在同一文件系统：

~~~text
copy exact declared tree
  -> fsync every regular file
  -> write/fsync canonical private CAS index
  -> fsync and read-only seal children
  -> atomic rename into owner-bound object key
  -> read-only seal final object root
  -> fsync object and parent directory
  -> full independent verify
~~~

macOS 不允许把已经完全只读的目录跨父目录 rename，因此 staging root 在 rename 前保留仅
owner-write；它的 children 已只读且 staging parent 为 private `0700`。rename 后立即把最终 object root
降为只读并 fsync。这不扩大可见性，也不把半写 staging 当作 sealed object。

### 2.3 exact replay、坏对象与 power loss

同 locator 已存在时，只有 canonical descriptor、digest、size、object type、Manifest binding、目录
内容和权限全部恒等且完整 verify 才返回 `reused=true`。任何半写、metadata 不 canonical、missing/
extra blob、digest/size/mode drift 或 writable object 都先原子移入 private `quarantine/`，当前请求失败；
不得覆盖或“修好后继续成功”。下一次明确 retry 才能创建新 object。

每个 object key 使用跨进程 file lock 串行 publish。故障注入固定覆盖：

- `staging_created`；
- `staging_synced`；
- `published`。

发布前 crash 留下有界命名的 staging orphan，`cleanup_staging_orphans(older_than, dry_run)` 可审阅并
清理；发布后 crash 的 retry 必须完整 verify 并 exact replay。这个 cleanup 只处理未发布 staging，
不是 AUD-202D retention/GC，也不删除 sealed CAS object。

### 2.4 read 与 verify

`open_blob` 和 `verify` 都要求 `SnapshotCASBinding(project_id, snapshot_digest,
manifest_digest)` 与 locator 内 descriptor 完全一致。调用者不能按 digest/list/path 枚举 object；
relative path 只能命中 descriptor 的 allowlist。regular file 使用 `O_NOFOLLOW`、只读 inode、size/digest/
fingerprint 校验后返回 bounded reader；symlink 只返回已校验的 link-target bytes，不跟随 target。

当前实现每次 open 都先完整 verify object，优先保证基础安全语义；后续性能优化必须保持相同 binding
与不可变 fingerprint，不得引入跨 Audit locator cache oracle。

### 2.5 durable Snapshot references

新增 `snapshot_references`：

~~~text
primary key(audit_id, snapshot_id, role)
project_id
schema_version=riftx.snapshot-reference/v1
reference_digest
created_at
~~~

两个复合外键分别绑定 `(audit_id, project_id)` 与 `(snapshot_id, project_id)`，从数据库证明 Audit 和
Snapshot 属于同一 Project。role v1 为 `primary/base/baseline/finding_evidence/retest_parent/
distribution_revision`。Repository 只允许 exact replay，跨 Project、同 key 异内容和损坏 digest 均
fail closed。downgrade 在存在任何 durable reference 时先于 DDL 拒绝，不能静默丢失保护事实。

`release_reference` 只删除一个精确引用事实；它不代表对象可回收。AUD-202D 必须另行检查所有
Snapshot/Audit/Baseline/Finding/Retest/Distribution 与 active pin，再生成 GC plan/receipt。

## 3. Explicit non-goals

AUD-202A 不实现：

- Git object/index reader、commit/working-tree materializer 或 TOCTOU capture；
- 最终 Source Manifest schema/capture decision、`SourceSnapshot` seal UoW 或 Audit snapshot 绑定；
- SourceIngest Runner/Capsule 写 CAS 的生产协议；
- SnapshotMountLease、mount namespace、pin、static effect ownership 或 stopper；
- sealed object retention/GC、disk pressure eviction 或 `SnapshotGCReceipt`；
- Artifact 投影、API/CLI/WebUI、Start admission、Temporal、Detector、Scanner、模型或网络访问。

## 4. Consequences

- AUD-202B 可以只负责 deterministic materialization/Manifest，并把已经声明完整的 staging tree 交给
  本 ADR 的 Store；它不能绕过 owner/digest/type/size 校验直接写 object path。
- CAS object 在数据库 `SourceSnapshot` 插入前可能成为 orphan，但它始终完整 sealed、可验证，并由
  后续 retention task 识别；半成品只存在于 private staging，不伪装成 SourceSnapshot。
- 生产资格仍依赖真实 local-Linux SourceIngest descriptor/mount/Capsule deny smoke。本 macOS 实现与
  synthetic fixture 不是该资格证据。
