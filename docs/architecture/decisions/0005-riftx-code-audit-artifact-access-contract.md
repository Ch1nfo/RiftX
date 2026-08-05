# ADR-0005：RiftX Code Audit Artifact 访问、摄取与完整性契约

> 状态：Accepted
>
> 日期：2026-08-03（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-105`
>
> 产品基线：`671735be`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md`
>
> 前置决策：ADR-0001、ADR-0002、ADR-0003、ADR-0004

## 1. 背景与结论

AUD-104 允许经过 Audit 根对象授权后读取 Code Audit 的 Artifact metadata，但旧 Artifact
实现仍有四个不能带入扫描阶段的问题：

1. 数据库中的绝对 `path` 同时承担展示历史和权威对象定位，无法证明它仍属于最初的
   Run/Audit；
2. path-based 注册先 `resolve`，复制时再次 `open`；下载先 `resolve`、再为 hash 打开、最后由
   `FileResponse` 第三次打开，存在路径替换、symlink/hardlink 和文件增长 TOCTOU；
3. 通用 Artifact query 只排除了 Target HTTP body，没有按 Code Audit access class 在 SQL 中
   过滤；
4. Artifact 没有 Audit owner、内容信任级别和可验证的摄取来源，后续 Scanner、模型、报告与
   Evidence 无法使用同一条 fail-closed 契约。

本 ADR 冻结以下结论：

- Artifact 新增 `audit_id`、`access_class`、`content_trust`、版本化 typed ingest provenance 和
  immutable `storage_key`；
- `storage_key` 是唯一权威 locator，绝对 `path` 只为 legacy downgrade/兼容保留，任何读取都
  不再信任或重新打开它；
- 通用 list/detail/download 只解析 `public_export`，过滤必须进入 SQL 并位于分页之前；
- `audit_internal` 与 `restricted_sensitive` 只能从显式 Audit 根对象授权路由读取；
- owner resolution、Audit 授权、完整对象复验和文件 I/O 的顺序不可交换；
- 本地文件摄取使用一次 no-follow open，并在同一个 fd 上完成 `fstat`、bounded copy/hash 和
  最终复验；下载也只使用一个经授权后打开并验证的 fd；
- Code Audit 不增加任何接收 Runner 绝对路径的端点。后续远端 Runner 只能使用认证的 bounded
  chunk stream；本任务仅预留 provenance method，不开放上传 wire；
- Feature Flag 只阻止新 Audit admission，不阻止已授权历史 Artifact 的安全读取和清理。

本决策和实现不使用 Codex Security Provider、代码、Prompt、Schema、Skill、运行时、端点、
依赖或生成物。所有名称、模型、错误和测试均由 RiftX 独立定义。

## 2. Artifact 领域契约

### 2.1 字段与枚举

`Artifact` 增加：

~~~text
audit_id: string | null
access_class: public_export | audit_internal | restricted_sensitive
content_trust: generated | untrusted_source | untrusted_tool_output
storage_key: canonical relative key
ingest_provenance:
  schema_version: riftx.artifact-ingest-provenance/v1
  method: legacy_migrated | local_nofollow_fd | control_plane_bytes |
          authenticated_chunk_stream
  producer_node_id: string | null
  producer_execution_id: string | null
~~~

`content_trust` 描述 bytes 的解释边界，不表示检测结论真假：

- `generated`：RiftX 确定性组件生成且调用方显式声明的结构化内容；
- `untrusted_source`：仓库、HTTP capture 或其他外部源的原始 bytes；
- `untrusted_tool_output`：Scanner、模型、Shell、Browser、Terminal 或其他工具输出；
- 未显式分类的新内容使用最保守的 `untrusted_tool_output`，不得默认提升为 `generated`。

`ingest_provenance` 描述 bytes 如何进入 Artifact store。它不保存 source absolute path、凭据、
命令行或模型 Prompt。`authenticated_chunk_stream` 只是后续 Runner upload contract 的保留值；
AUD-105 不公开对应 endpoint。

### 2.2 owner 与 access invariants

- `audit_id IS NULL` 的 Artifact 必须是 `public_export`；
- `audit_internal` 和 `restricted_sensitive` 必须有 `audit_id`；
- 有 `audit_id` 时数据库 FK 指向 `audit_scans.id`，Repository/Application 还必须复验
  `artifact.run_id == audit.run_id`，不能把单列 FK 当作完整 owner proof；
- Artifact Repository 创建和读取必须同时复验 `Run.kind`：General Run 只能拥有无 `audit_id` 的
  Artifact，Code Audit Run 只能拥有与自身 Run 一致的 Audit-owned Artifact；
- Code Audit Artifact 可以是 `public_export`，但仍必须持久 `audit_id`；
- Artifact row 创建后没有 update API。`run_id/audit_id/access_class/content_trust/provenance/
  storage_key/sha256/size` 都是 immutable facts；改变发布范围只能创建新的 Artifact 或未来的
  distribution revision，不能原地改 class；
- `execution_id` 与 provenance 中非空的 `producer_execution_id` 必须一致；Repository 还必须证明
  Execution 本身属于 Artifact Run。Code Audit Run 上缺失 `audit_id` 的 public Artifact 也按 owner
  corruption fail closed，不能进入 generic read。

### 2.3 immutable storage key

RiftX v1 Artifact key 唯一格式为：

~~~text
runs/{run_id}/artifacts/{artifact_id}/{name}
~~~

它必须是规范化的 POSIX 相对 key；`run_id`、`artifact_id` 和 `name` 均为安全单路径组件，禁止
空值、`.`、`..`、斜杠、反斜杠、NUL、CR/LF、非 printable ASCII 和其他控制字符。Domain 和
数据库 check 都验证传入 key 与 owner/name 推导出的 key 完全相等。该 ASCII 约束仅作用于
存储 key 组件；受信 source root 下的 Unicode 文件路径仍可通过 dirfd 摄取。

`RunnerPaths` 只把验证后的 key 映射到配置的私有 state root。数据库 `path` 在本版本仍保留，
使合法 legacy row 可以无损 downgrade；它不是 locator，不参与授权、open、hash、响应或事件。
Artifact root 与自动创建的子目录收紧为 `0700`；已有祖先必须由当前服务 UID 拥有且不能
group/world writable，否则存储 capability fail closed。

## 3. 持久化与迁移

### 3.1 upgrade

迁移对已有 Artifact 使用以下保守 backfill：

~~~text
audit_id = null
access_class = public_export
content_trust = untrusted_tool_output
ingest_provenance.method = legacy_migrated
storage_key = canonical key derived from run_id/id/name
~~~

AUD-104 已保证 Code Audit draft 无法通过 generic mutation 产生 Artifact。迁移若发现已有
`code_audit` Run 关联 Artifact，就不能猜测 access/trust/owner，必须中止并要求显式修复。

SQLite 迁移在同一个 `BEGIN EXCLUSIVE` 中完成旧行审计、backfill、batch table rebuild、索引、
`PRAGMA foreign_key_check` 和提交；任一 DDL、检查或复制失败都回滚到升级前 Schema。迁移完成后
删除临时 server defaults，安装：

- access class、content trust 和 canonical storage key checks；
- `audit_id -> audit_scans.id` FK；
- `execution_id -> executions.id ON DELETE RESTRICT` FK；
- 非公开 class 必须有 `audit_id` 的 check；
- generic visibility 与 Audit owner/list 所需索引。

SQLite 和 PostgreSQL 使用同一逻辑契约。未知 dialect、无法证明的旧行、非法组件或不合法 JSON
都 fail closed；不得静默降级成公开 Artifact。

### 3.2 downgrade

只有所有行仍可由旧 Schema 无损表示时才允许 downgrade：没有 `audit_id`、全部
`public_export/untrusted_tool_output`，且 provenance 均为 `legacy_migrated`、storage key 与旧
owner/name 一致。存在任何新 Artifact 分类或 provenance 时 downgrade 中止，不能丢弃安全
metadata 后继续。

### 3.3 corrupt-row handling

Mapper 必须把 enum、provenance、storage key、owner 或 digest 校验错误正规化为
`RepositoryIntegrityError`；公开边界返回固定、脱敏的 `503 artifact_persistence_unavailable`。
不得把 SQL、driver cause、path、storage key、原始 JSON 或 Pydantic input 反射给客户端。

通用 resolver 对非公开 Artifact 返回与 missing 相同的不可见结果。授权成功后加载到损坏的
公开 row 不是 404；它是已确认对象的持久完整性失败，因此 list/detail/content 都使用同一个
脱敏 503 envelope。

## 4. 读取与授权路由

### 4.1 通用路由

保留现有：

~~~text
GET /api/v1/runs/{run_id}/artifacts
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/artifacts/{artifact_id}/content
~~~

三条通用读取只返回 `public_export`；SQL predicate 同时继续排除 legacy Target HTTP sensitive
body。list 的 visibility predicate 位于排序、`LIMIT` 和 `OFFSET` 之前。

### 4.2 显式 Audit 路由

AUD-105 增加只读 route：

~~~text
GET /api/v1/audits/{audit_id}/artifacts
GET /api/v1/audits/{audit_id}/artifacts/{artifact_id}
GET /api/v1/audits/{audit_id}/artifacts/{artifact_id}/content
~~~

这些 route 均登记为 `READ_ONLY`，要求 Local Operator `READ` capability，并通过 ADR-0004 的
typed raw Audit binding/object authorizer。显式 list 可以返回该 Audit 的三种 access class；
detail/content 只能返回与 path `audit_id` 和已授权 Audit `run_id` 同时一致的 Artifact。

Feature Flag 关闭时这些安全读取仍可用。路由不创建 Event、不改 last-accessed 字段、不启动
Workflow/Runner，也不把读取当作 Audit progress。

Artifact Domain 保持可完整 round-trip，但 API `ArtifactResponse`、Agent `add_artifact`、Event
projection 与 Report source 都必须使用显式字段白名单。非公开、缺失或损坏 Artifact 的 Event
metadata fail closed；任何边界都不能通过直接 dump Domain 绕过 Artifact API。

### 4.3 不可交换的 detail/content 顺序

~~~text
bounded Artifact owner columns
  -> requested audit_id / RunKind binding
  -> Audit raw binding and object authorization
  -> full Artifact load with exact audit_id + run_id predicate
  -> immutable owner/access/storage metadata revalidation
  -> one no-follow content fd open
  -> fstat + digest/size verification
  -> bounded response stream from that same fd
~~~

missing、非公开 generic lookup、requested owner mismatch、authorizer denied、授权后 owner
消失/变化均为 byte-identical `404 resource_not_accessible`。在这些结果确定前，storage resolver、
`open`、hash 和 response iterator 调用次数必须为零。

认证失败仍优先 401，capability 失败仍优先 403。403 不改写成 404；对象授权拒绝与 missing 才
使用统一 404。

## 5. descriptor-safe 本地摄取

### 5.1 source admission

保留给 `general` Run 的 legacy path registration，但 source 只能位于服务端已知的 Run
workspace 或 Runner state directory。Code Audit 仍被 AUD-104 双层 RunKind bridge 拒绝，且拒绝
发生在任何 path 解析/open 前。

source path 不先 `resolve` 后重开。实现从受信 root dirfd 逐组件 `openat` 等价遍历，目录和最终
组件都使用 `O_NOFOLLOW`；最终组件同时使用 `O_NONBLOCK`，使 FIFO/device 在 `fstat` 前不会
阻塞。路径中的 symlink、`..`、跨 root 与不可解析组件一律拒绝。

### 5.2 单 fd 验证与复制

最终 source 只打开一次，并在整个操作期间持有同一 fd：

1. 第一次 `fstat` 要求 regular file、`st_nlink == 1`、非负且不超过单文件上限；记录
   dev/inode/mode/link/size/mtime/ctime；
2. 从同一 fd bounded 分块读取，同时写私有 staging、累计 byte count 和 SHA-256；
3. 读取超过初始 size、配置上限或预期 size/digest 立即失败；
4. 复制后再次对同一 fd `fstat`，要求 fingerprint/size/timestamps 未变；
5. 使用仍持有的 parent dirfd 对目录项做 `stat(..., follow_symlinks=false)`，要求仍指向最初的
   dev/inode，拒绝并发 unlink/replace；
6. staging `flush + fsync + fchmod(0444)`，同目录 atomic rename 为最终文件，再 fsync 目录；
7. 任一步失败都关闭 fd、删除 `.partial` 和未提交 Artifact directory；数据库 create 失败也
   删除已经落盘的未引用 bytes。

hardlink 即使内容正确也拒绝，因为外部别名可以在摄取期间修改 inode。文件增长、缩短、
内容/时间变化、special file、symlink 和并发 path replacement 均不得产生 Artifact row/Event。

### 5.3 bytes 与未来 stream

Control Plane 自己持有的 bytes 使用同一 staging/limit/fsync/atomic-rename finalizer，默认 trust
仍是 `untrusted_tool_output`；只有确定性 RiftX producer 显式传入时才记录 `generated`。

未来 `authenticated_chunk_stream` 必须在认证、Node/Execution/Audit/plan owner 校验后创建
staging，边收边执行单文件与 Audit 总量限制和 SHA-256，并校验声明 digest/size。它不能把
Runner 发送的 path 转交给本地注册函数。该 wire、租约和 Runner ownership 由后续任务引入。

## 6. descriptor-safe 下载

下载在完成第 4 节授权和 full-row owner 复验后，才把 canonical storage key 映射到私有 root。
从 root dirfd 逐组件 no-follow 打开，最终 fd 必须：

- 是 `regular file` 且 `st_nlink == 1`；
- size 与 row 完全一致且不超过配置上限；
- 从同一 fd 读取并计算的 SHA-256 与 row 完全一致；
- hash 后的 `fstat` 与目录项 dev/inode 仍匹配。

hash 完成后把同一 fd seek 到 0；`StreamingResponse` 从该 fd bounded 分块发送，并在完成、取消、
断连或异常时 `finally` 关闭。禁止 `FileResponse`、path iterator 或 response 开始后重新 open。
blocking worker 使用显式 fd lease ownership；重复取消只能由一个 owner 关闭资源。已验证
fingerprint cache 固定为 128 条 LRU，single-flight 使用 64 个 striped lock，不能随 Artifact ID
数量无限增长。

响应固定包含 `Content-Length`、`ETag: "sha256:{digest}"`、`X-Artifact-SHA256`、
`X-Content-Type-Options: nosniff` 和 attachment `Content-Disposition`。文件名只使用验证后的
Artifact name，并按 RFC 5987 UTF-8 percent encoding 构造，禁止 header injection。

上述完整性保证以 RiftX 私有 state root 为服务端信任边界：未受信 Scanner、目标程序、模型工具
和内容沙箱不得获得该 root 的路径、目录 fd 或写权限，也不得与可任意改写该 root 的服务身份共享
执行边界。`0444` 是防误写措施，不是对同 UID 恶意进程的隔离声明。若某部署不能满足这个前提，
Code Audit Artifact capability 必须标为 unavailable；后续 backend 可用 sealed anonymous snapshot
或独立存储服务进一步加固。否则，恶意进程可能在预哈希后原位改写同一 inode：最终 fingerprint
仍会使响应异常终止，但已经发送的流字节无法撤回。

storage 缺失使用脱敏 `409 artifact_content_missing`；symlink/hardlink/special file、owner/key、
digest/size 或并发变化统一使用脱敏 `409 artifact_integrity_mismatch`。响应不含 path、key、实际
digest/size、inode 或 driver cause。

## 7. 限额与兼容性

- production Artifact Service 使用 `audit.max_artifact_bytes` 作为所有新摄取和验证的硬上限；
- Code Audit 的 `max_total_artifact_bytes` 必须由未来认证 stream/Artifact creation transaction
  在写入前原子执行。AUD-105 没有 Audit write endpoint，因此不能伪造一个非并发安全的总量
  检查；
- 对 `general` Run，公开 metadata 和正常小文件的注册、list/detail/download wire 保持兼容；
- 原 General Artifact 的绝对 `path` row 保留但不再作为读取权威；
- Target HTTP body 的旧 marker/association filter 继续生效，并由新的 public visibility
  predicate 统一包含；
- 新 schema 字段可以进入 Artifact metadata response，但 `storage_key` 与 legacy `path` 永不
  进入 HTTP、Event、报告 source、Agent tool result 或日志。

## 8. 验证要求

AUD-105 至少覆盖：

- Domain/mapper：三种 access class、三种 trust、typed provenance、canonical key 与 owner
  invariants；
- migration：legacy backfill、upgrade/restart、非法 Code Audit legacy row、corrupt row、
  lossless downgrade gate；
- Repository：generic SQL visibility 在分页前、explicit Audit owner list/detail、Target HTTP
  regression；
- API：public generic read、restricted generic opaque 404、explicit restricted list/detail/download、
  missing/denied/owner mismatch byte identity、Feature Flag off 历史读取、OpenAPI/Policy；
- zero-I/O：denied/owner mismatch 前 resolver 之外的 full getter/open/hash/iterator 均为零；
- ingest：symlink、parent symlink、hardlink、FIFO/special、oversize、growth、shrink、并发
  replacement、read/write/fsync/rename/DB 失败清理；
- download：symlink/hardlink、missing、digest mismatch、size mismatch、canonical key mismatch、
  fd 关闭和 Content-Disposition injection；
- storage/worker：private root/ancestor 权限、Unicode source path、重复取消、fd lease、bounded
  verified-fingerprint LRU 与 striped single-flight；
- projection：Code Audit public Artifact 缺失 owner、Execution owner corruption、Event metadata 与
  Agent `add_artifact` locator/provenance 泄漏反例；
- General Run Artifact、Report、Context、Runtime control tool 与 Target HTTP 兼容回归；
- 全量 Python、Ruff、目标 Mypy、independence boundary gate 和 release gate。

Agent 相关命令必须使用：

~~~shell
conda run --no-capture-output -n agent <command>
~~~

## 9. 明确延后

- 认证 Runner chunk upload endpoint、RunnerCommand ownership 和 lease：AUD-106 及执行阶段任务；
- Source Snapshot/CAS Artifact：AUD-202/AUD-205；
- Scanner/Detector/Agent Artifact producer：AUD-300 以后；
- Evidence 对象、Core Seal、distribution revision 与发布清单：AUD-402/AUD-503/AUD-506；
- WebUI restricted cache 清除和 Artifact 浏览体验：AUD-604；
- PostgreSQL production qualification：M10。当前迁移保持 dialect contract，但不能声称已有真实
  PostgreSQL 运行证据。

## 10. provenance

~~~yaml
provenance_id: RXP-AUD-105-001
task_id: AUD-105
artifact_class: architecture_decision
artifact_version: ADR-0005
paths:
  - docs/architecture/decisions/0005-riftx-code-audit-artifact-access-contract.md
  - docs/riftx-3-code-audit-development-spec.md
author: Ch1nfo (Git author); Codex task /root
authored_at: 2026-08-03T15:08:48+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 17.2"
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-105"
  - "docs/architecture/decisions/0004-riftx-code-audit-api-authorization-contract.md"
implementation_inputs:
  - RiftX repository baseline 671735be
  - existing RiftX Artifact, RunnerPaths, FastAPI authorization, and SQLAlchemy contracts
public_standard_versions:
  - SHA-256 (FIPS PUB 180-4)
  - POSIX open/fstat/fsync/rename semantics
  - RFC 5987 Content-Disposition filename encoding
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex tasks /root/audit105_security_review, /root/audit105_test_fix_review, /root
review_sources:
  - ADR-0005 and authoritative specification sections 16.7, 17.2, and 22 / AUD-105
  - AUD-105 Artifact domain, migration, repository, API, event projection, Agent projection, and descriptor-store diff
  - targeted 506-test matrix, independent 175-test security matrix, full Python suite, Ruff, targeted Mypy, boundary gate, and release gate
review_result: approved; no P0/P1/P2 findings accepted as deferred AUD-105 work
commit: ee9adaa99df08f043a3c2a813da3728aeb81a6b6
notes: AUD-105 owns access classification and descriptor-safe local content handling. It does not open an Audit write/upload endpoint, atomic Audit total-byte transaction, authenticated Runner upload, or PostgreSQL production proof, and it does not weaken the AUD-104 RunKind effect bridge.
~~~
