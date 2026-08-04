# ADR-0010：RiftX Code Audit Source Materializer 与 Manifest

> 状态：Accepted
>
> 实施状态：AUD-202B implemented
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 所属任务：AUD-202B

## 1. Context

AUD-202A 已提供 Project-bound、只读、可恢复的 SnapshotStore/CAS，但输入仍是 synthetic staging
tree，尚不能从 commit 或 dirty working tree 生成不可变 Snapshot。AUD-202B 必须把不可信 Git object、
index 与工作树状态转成确定、版本化、可独立验证的 Manifest，同时保持以下边界：

- commit 不 checkout，working tree 不在后续扫描期间继续读取；
- 原仓库、Git admin、object store 和 index 全程只读；
- symlink、hardlink、submodule、LFS pointer、special file、非法 UTF-8、超限、ignored/untracked
  不能被静默遗漏或隐式跟随；
- capture 期间发生目录集合、Git admin/object 或单文件 inode/metadata/content 变化时 fail closed；
- materialized bytes 与 Manifest 位于 SnapshotStore，不成为首个 Run 的 Artifact；
- 普通 API、Control Plane 和其他 Audit 不能凭 source path 或 digest 枚举 CAS。

## 2. Decision

### 2.1 Source Manifest v1

新增 `riftx.source-manifest/v1` 与 `riftx.source-materializer/v1`。Manifest 顶层冻结：

~~~text
source_kind=revision|working_tree
commit_sha
head_commit_sha
capture_policy_digest
materializer_schema_version
staged / unstaged / untracked
working_tree_digest（working_tree 必填）
tree_digest
snapshot_digest
manifest_digest
file_count / total_bytes（仅 included bytes）
entries[]（按原始 path bytes 排序且唯一）
~~~

每个 entry 至少记录：

~~~text
canonical relative_path 或 raw_path_b64 + path_digest
object_type
origin=commit|tracked_worktree|untracked|ignored
mode / size / sha256 / git_blob_id
language / classification
decision=included|excluded|deferred
reason
~~~

可规范化为 NFC UTF-8、无 control character、无 `.git` component 且不超过 4096 bytes 的路径才可
进入 staging/CAS。其他合法 Git path 以 canonical base64 与 path digest 记录，并明确 deferred；不会把
opaque bytes 猜成替代路径。`manifest_digest` 是不包含自引用字段的 canonical Manifest payload 的
SHA-256；实际 Manifest blob 另有 raw blob SHA-256，由 CAS blob metadata 验证。

`tree_digest` 冻结按 path bytes 排序的完整 capture-tree record，包括 source object identity、mode、
content/sentinel metadata 与 capture decision。它不包含 Audit 内 Scope priority、Detector、模型、Run、
时间或 storage locator；这些后续事实不得回写共享 Manifest。`snapshot_digest` 继续严格使用规范第
8.3 节的 `tree_digest + capture_policy_digest + materializer_schema_version` identity。

### 2.2 Capture Policy v1 与决议

`riftx.source-capture-policy/v1` 的 digest 冻结 include/exclude path、untracked、generated/vendor、
单文件/总量/Manifest entry 上限，以及以下固定模式：

- symlink：included 时只保存 link-target bytes，绝不 follow；
- hardlink：deferred，避免仓库外 alias；
- submodule：excluded；3.0 不递归冻结其工作树；
- LFS pointer：deferred，不运行 LFS、不联网；
- special/socket/device/FIFO：deferred；
- regular content：UTF-8 strict，无法解码时 deferred；
- oversized 或总量预算后的 entry：deferred；
- ignored：记录为 excluded；
- untracked：始终记录，是否 included 由 policy 决定；
- generated/vendor：先分类，再由 policy 明确 included/excluded。

deferred/excluded entry 不进入 content staging，但仍进入 Manifest/tree identity。Scope Ledger 后续只能
在 included bytes 上规划分析优先级，不能把 deferred 偷换成“已扫描”。

### 2.3 Commit materialization

commit 路径复用 AUD-200 的 Git structure/config/object-store snapshot 与 `SafeGitAdapter`：

1. `rev-parse <revision>^{commit}` 固定 commit；
2. `ls-tree -r -z -l --full-tree` 得到 byte path、mode、object type、ID 与 size；
3. submodule/超限/path policy 在读取 blob 前决议；
4. included candidate 只能通过专用 `read_blob(object_id, expected_size)` 执行固定
   `git cat-file blob <lower-hex-id>`；普通 `run()` allowlist 不开放 `cat-file`，不能传
   `--filters`、textconv、driver 或任意 object expression；
5. blob size、SHA-256、UTF-8/LFS 决议完成后，使用 private staging dirfd 创建 exact tree；
6. publish 前后再次验证 Git admin/object guard 与 `git fsck --strict --full`。

不运行 checkout、archive、submodule、LFS、attributes filter、clean/smudge、hook、fsmonitor、external
diff/textconv 或网络协议。会让 `status/ls-files --exclude-standard` 读取仓库外文件的
`core.excludesFile` 在解析 local config 时直接拒绝。

### 2.4 Working-tree materialization 与 TOCTOU

working-tree capture 组合 index stage-0、porcelain v2 status、tracked/untracked/ignored `ls-files` 与
descriptor-bound filesystem leaf walk。unmerged index 没有唯一 source truth，整次 capture 拒绝。

selected entry 从 source-root dirfd 逐组件 `O_NOFOLLOW` 打开；regular file 在同一个 fd 上完成
`fstat -> bounded read -> final fstat`，symlink 使用 `lstat/readlink/lstat`。fingerprint 至少绑定 device、
inode、mode、nlink、uid/gid、size、mtime_ns 与 ctime_ns。全部 entry 处理完后：

1. 再次逐项验证原 fingerprint/missing state；
2. 重新枚举 index、untracked、ignored 与 filesystem leaves；
3. 重新读取 staged/unstaged status；
4. 重新验证 Git admin/object guard 与 fsck。

任何集合、inode、metadata、内容或 Git identity 漂移都返回 path-free
`audit_repository_changed_during_materialization`，删除 partial staging，不发布半成品。

### 2.5 Publication、并发与失败清理

materializer 只返回 private staging root 与已验证 Manifest。可信 publisher 分两次调用 AUD-202A
SnapshotStore：

1. source content tree：descriptor 只包含 included regular/symlink bytes；
2. Manifest tree：独立单 blob `source-manifest.json`，同样绑定 Project、Snapshot digest 与 Manifest
   digest。

因此 `content_storage_key` 与 `manifest_storage_key` 都是 opaque、Project-bound CAS locator；Manifest
不是 Run Artifact，也不混入扫描根目录。并发 identical capture 依赖现有 per-object lock 与 exact replay，
content/Manifest 分别得到同一个 locator；内容或决议变化生成新 identity。若 Manifest publish 失败，已
sealed content 只能成为完整 orphan，不能成为半 sealed Snapshot；retry 走 exact verification。

materializer staging 使用随机 private directory。普通失败删除 partial tree；清理本身失败时返回 typed
cleanup failure并保留有界前缀 orphan。`cleanup_orphans(older_than, dry_run)` 只处理 materializer
staging，不删除 sealed CAS object，也不替代 AUD-202D GC。

### 2.6 Authorization boundary

本任务不新增 public list/open API。SnapshotStore read 仍要求不可枚举 locator 与 Project/Snapshot/
Manifest binding；知道 source relative path、blob digest 或 snapshot digest 本身不足以定位对象。AUD-202C
将在此基础上增加 Audit-bound mount lease/pin，analysis Capsule 只能读取 lease allowlist，不能直接调用
CAS。Control Plane 与普通 Worker 不 import `audit_worker.materializer`，Git 只属于 SourceIngest boundary。

## 3. Explicit non-goals

AUD-202B 不实现：

- `SourceSnapshot` insert-is-seal 数据库 UoW、Audit snapshot reference 创建或 Start admission；
- same-node mount/pin、`SnapshotMountLease`、Runner static effect ownership 或 stopper；
- source Node 到 analysis Node 传输、远程 CAS、mTLS hydration；
- submodule recursive capture、LFS materialize、Git filter/attribute interpretation；
- Snapshot retention/eviction/GC；
- Scope Ledger、Snapshot reader、Artifact/API/CLI/WebUI、Scanner、Detector、模型或 Temporal。

生产资格仍要求真实 local-Linux SourceIngest descriptor/mount/Capsule denial 与 staging/CAS round-trip
smoke；macOS unit fixture 不构成该资格证据。

## 4. Consequences

- AUD-202C 可以只消费 owner-bound content/Manifest locator 和 immutable digests，不再读取原仓库。
- working tree 在 materialization 成功后即被冻结；后续原文件变化不会影响 Snapshot bytes。
- Manifest 对所有候选给出明确 decision，后续 Scope/Coverage 可以区分 included、excluded 与 deferred，
  不把缺失输入伪装成无发现。
- 由于 Manifest 作为独立 CAS tree 保存，Run Artifact 生命周期不能删除共享 source bytes。
