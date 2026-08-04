# ADR-0007：Code Audit Preflight Job、Runner 协议与 Source Ingest 契约

> 状态：Accepted
>
> 实施状态：AUD-200 completed
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-200`
>
> 产品基线：`c3cd7325`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md` 第 4.5、8.2、15.1–15.2、
> 16.2、17.1、20.1、22/M2/AUD-200 节
>
> 前置决策：ADR-0001 至 ADR-0006

## 1. 背景与结论

AUD-200 是 RiftX Code Audit 第一个允许接触本地授权仓库的任务，但 Preflight 发生在 Audit、Run、
Snapshot 和 Temporal Workflow 创建之前。现有 `RunnerCommand`、`RunnerEffectBinding` 与
`RunnerCommandOwnership` 都以非空 `run_id` 为事实根，不能安全承载 Preflight；将 Git 命令直接放入
Control Plane、普通 Worker 或通用 Runner command 也会破坏 ADR-0006 的 M1 零 enqueue 围栏。

本 ADR 冻结以下结论：

1. `AuditPreflightJob` 是独立、持久、非 Run-scoped 的事实根，使用专用表、Repository、Runner
   transport、capability、owner envelope、lease、stop receipt 和 reconciler。它不包含、推导或伪造
   `audit_id/run_id`。
2. AUD-200 的成功输出是不可变 `AuditPreflightResult/v1`。AUD-201 才把可信 Result 转换为短期、
   可预留/消费的 `AuditPreflightPlan` 与 opaque token；AUD-200 不创建 plan/token 表。
3. Preflight Job 没有关联 Run，因此不写 `RunEventRepository`。Job 本身的状态、版本、结果、错误与
   stop receipt 是 AUD-200 的权威状态流；AUD-201 创建 Audit 后才允许写 RunEvent。
4. Preflight 使用独立 `preflight_job_owner_v1` Runner capability 与 discriminated wire。普通
   `runner_command_ownership_v1` 既不蕴含也不能替代该 capability；Code Audit 的 Run-scoped 普通
   Runner enqueue 在 AUD-200 结束时仍恒为零。
5. Control Plane 只做版本化请求、source-root/descriptor 授权、owner/digest/lease/CAS 和 bounded
   projection；真实 Git/object/index/config 读取只允许在无网络、无凭据、只读源挂载、资源受限且
   可证明停止的 SourceIngest Capsule 中执行。
6. 没有生产级 local Linux backend/image/policy/stop proof 时，真实仓库 Preflight 返回
   `audit_sandbox_unavailable`。fake/in-process backend 只允许测试可信 synthetic fixture，不能宣称
   生产能力。
7. AUD-200 不创建 Snapshot/CAS handle，不解析仓库内容、Security Context、依赖、AST、SARIF 或
   Detector 事实；缺失的后续能力以 typed `unavailable`/blocking fact 表达，不能伪造结果。

本实现不使用 Codex Security Provider、代码、Prompt、Schema、Skill、运行时、依赖、端点、测试或
生成物。所有协议、命名、迁移和安全边界均为 RiftX 自有实现。

## 2. 分阶段对象边界

### 2.1 AUD-200：AuditPreflightResult

`AuditPreflightResult/v1` 是一次已完成 Job 的不可变、只读结果。它至少绑定：

~~~text
schema_version/result_digest
preflight_job_id/request_digest/effect_owner_digest
source_node_id/source_root_identity_digest/repository_identity_digest/content_identity_digest
backend_id/image_digest/policy_digest/capsule_prepare_proof_digest
target_kind/revision/base_revision/mode/include_untracked
head_revision/resolved_revision/resolved_base_revision/merge_base_revision
dirty/staged/unstaged/untracked
file_count/total_bytes/max_file_bytes/language_estimates
capability_matrix/capability_warnings/blocking_errors
minimum_feasible_budget
canonical_empty_context_id/canonical_empty_context_digest
completed_at/expires_at
~~~

Result 只允许 ID、digest、枚举、布尔值、有界计数、版本和安全 warning/error code。绝对路径、Git
stderr、container locator、raw config、token、源码、文件名清单和 Context 原文不能进入 API、普通日志
或 Event。Repository 可以在受限列中保存完成恢复所需的 canonical request document，但不得把该列
直接映射到公开 DTO。

AUD-200 的 `minimum_feasible_budget` 只是有 provenance 的安全区间估计；没有 Inventory/Detector/
parser 能力时必须明确标为 unavailable 或 blocking。它不是 `AuditBudget` 预留，也不授权执行。

### 2.2 AUD-201：AuditPreflightPlan 与 token

AUD-201 从一个未过期、成功且 owner 完整的 Result 生成 `AuditPreflightPlan`，并实现 token hash、
expiry、reservation/consume、Create v2 与 replay/race 语义。Plan 可以复制 Result 的可信摘要，但不能
回写或替换 Result。AUD-200 不接受、返回或持久 token，也不把 `result_id` 当作执行授权。

### 2.3 AUD-202/AUD-206/AUD-209

- AUD-202A/B/C/D 独占 SnapshotStore/CAS、Manifest、materializer、mount/pin 和 GC receipt；AUD-200
  不返回 CAS ingest handle，不封存源码。
- AUD-206 独占 Run-scoped `AuditStaticEffectPlan(content_parse)`、通用 Content Sandbox ledger 与
  `audit_capsules/audit_egress_sessions`；AUD-200 只拥有 pre-Audit SourceIngest Capsule。
- AUD-209 独占非空 `ContextInput/Bundle/Binding`、SECURITY.md/default discovery 与冲突处理。AUD-200
  只接受空 input、空 repository paths、`discover_defaults=false`，并绑定固定版本化 empty-context
  常量；不提前创建 Bundle 表。

## 3. Job、幂等与状态机

### 3.1 持久事实

`audit_preflight_jobs` 至少持久：

~~~text
job_id/client_request_id/operator_principal_id/authorization_scope_digest
request_schema_version/request_digest/restricted_request_json
source_node_id/source_root_identity_digest
backend_id/image_digest/policy_digest
status/state_version
lease_id/lease_owner_instance_id/lease_owner_epoch/lease_expires_at/lease_envelope_digest
capsule_id/effect_owner_digest/capsule_prepare_proof_digest
result_schema_version/result_json/result_digest
safe_error_code
never_created_proof_digest/stop_receipt_digest
expires_at/created_at/updated_at/started_at/finished_at
~~~

原始路径只存在于 `restricted_request_json`，列读取只能经过 Preflight dispatch Repository；list/status/
error mapper 不读取或投影该列。数据库不保存 Git stderr、环境、完整命令、host socket、容器 inspect
结果或凭据。

唯一幂等键是 `(operator_principal_id, client_request_id)`。同键重放必须同时满足
`authorization_scope_digest + request_schema_version + request_digest` 全等，返回同一个 Job；任何漂移
返回 `audit_preflight_idempotency_conflict`。并发 create 只能持久一个 Job，也只能产生一个 dispatch
效果。调用方提交的 digest 一律忽略或拒绝；所有 digest 由服务端从 canonical typed object 计算。

### 3.2 状态与转换

~~~text
pending -> claimed -> running -> succeeded
                         |       -> rejected
                         |       -> failed        （仅有 stop/never-created proof）
pending|claimed|running|outcome_unknown -> cancelling -> cancelled
claimed|running -> outcome_unknown
outcome_unknown -> running | succeeded | rejected | failed | cancelling
~~~

`pending/claimed/running/succeeded/rejected/failed/cancelling/cancelled/outcome_unknown` 是闭集。

- `rejected` 表示仓库、target、source policy 或结构确定不可接受，并且 Capsule 已肯定停止或从未创建；
  `failed` 表示内部、完整性、协议或 sandbox 故障，并满足同样的停止证明门槛。
- `cancelled` 只在 affirmative stop receipt 或可信 `never_created` proof 后成立。
- 发送/启动/完成 RPC 结果不确定时进入 `outcome_unknown`；不得因 lease 过期、HTTP timeout、Runner
  断线或进程消失而猜测从未执行。
- terminal 状态不可离开。terminal exact finish replay 返回原事实；任何 result/error/proof 漂移冲突。

所有 claim、renew、start、finish、cancel、stop ACK、reconcile 都携带从 1 开始的 expected
`state_version` 并使用单条 CAS 或单事务 locked-CAS。状态/timestamp 不是 CAS substitute。

### 3.3 竞态优先级

1. 在 effect 尚未创建时，cancel CAS 可以从 `pending/claimed` 写入 `cancelling + never-created proof`
   并原子完成 `cancelled`。
2. Runner 已提交有效 start 后，cancel 只能围栏新效果并进入 `cancelling`；必须等 stop receipt。
3. finish 与 cancel 竞争时，以数据库中第一个成功 CAS 为准：
   - finish 先赢并写入完整 Result，可进入 `succeeded/rejected`；随后 cancel 是 already terminal；
   - cancel fence 先赢，普通 finish 不得覆盖 `cancelling`，只能提交 bounded observation/stop proof，
     由 reconciler 收敛 `cancelled` 或保持 `outcome_unknown`。
4. lease expiry 不允许第二个 Runner直接重跑 claimed/running Job。Reconciler必须先通过权威 Node/
   Capsule probe 证明 never-created、still-running、terminal result 或 stopped，再决定 reclaim/收敛。

## 4. Owner、lease 与 Runner wire

### 4.1 Effect owner

稳定 root digest 使用 `riftx.audit-preflight-effect-owner/v1` canonical JSON，绑定：

~~~text
job_id/operator_principal_id/authorization_scope_digest
source_node_id/source_root_identity_digest/request_schema_version/request_digest
backend_id/image_digest/policy_digest/created_at/expires_at
~~~

它不含 `audit_id/run_id/plan_digest`。`PreflightJobEffectOwnership` 扩展为携带上述事实并由 operation
catalog 精确验证 owner variant；任何 global/run/legacy fallback 均拒绝。

每次 claim 生成 `riftx.audit-preflight-lease-envelope/v1`，在 effect owner 外再绑定 authenticated
Runner principal、lease ID、lease expiry、expected state version 与 output contract digest。renew 只能
延长同一 principal/lease 的 expiry并生成新的 lease-envelope digest；Runner 必须 echo 当前 digest。

### 4.2 Capability 与 endpoint

Runner credential 的 immutable protocol capability 新增：

~~~text
preflight_job_owner_v1
~~~

Node heartbeat capability 只用于可观测性，不能替代 credential gate。没有 capability 的 Runner 不能
lease、renew、start、finish 或 stop Preflight Job。

Preflight 使用独立 wire family：

~~~text
GET  /api/v1/runner/audit-preflight/next
POST /api/v1/runner/audit-preflight/{job_id}/lease
POST /api/v1/runner/audit-preflight/{job_id}/start
POST /api/v1/runner/audit-preflight/{job_id}/finish
POST /api/v1/runner/audit-preflight/{job_id}/stop
~~~

若实现与普通 poll 共用物理连接，wire 仍必须通过显式 `owner_kind/schema_version` discriminated union
区分，禁止通过 nullable 字段猜 variant。Preflight callback 的校验顺序固定为：

~~~text
Runner authentication
  -> credential capability
  -> exact node/principal
  -> owner/lease envelope schema and digest
  -> job/principal/authorization/request/root/backend/image/policy binding
  -> state_version/status/expiry
  -> bounded payload/result/receipt schema
  -> I/O or mutation
~~~

wrong owner 在 job 状态、target 或 lease 错误之前拒绝，且拒绝路径为零文件、零网络、零 Capsule、零
Event、零 Workflow signal和零状态 mutation。

### 4.3 Stop receipt

`AuditPreflightStopReceipt/v1` 至少绑定 job、effect owner、lease envelope、capsule、Node、Runner
principal、backend/image/policy、stop disposition、process/container identity digest、observed terminal
state、received_at 与 receipt digest。允许的 disposition 只有 `stopped` 与 `never_created`；timeout、
not-found（缺少权威 tombstone）或 Runner 失联不能伪造肯定停止。

stop ACK 与 Job convergence 尽量在同一事务；若 backend ACK 已保存但 terminal projection 暂时失败，
Job 保持 `cancelling/outcome_unknown`，reconciler 幂等投影。stop receipt 永远不创建普通
Execution-completed Workflow signal。

## 5. Operator API

~~~text
POST /api/v1/audits/preflight                 HOST_EXECUTION
GET  /api/v1/audits/preflight/{job_id}        READ_ONLY
POST /api/v1/audits/preflight/{job_id}/cancel HOST_CONTROL
~~~

- POST 在首次成功创建时返回 `202 Accepted`；exact replay 返回 `200 OK`。响应始终至少包含
  `job_id/status/state_version/created/replayed`，可以短暂等待但 HTTP 断线不取消 Job。
- GET 与 cancel 使用 `(job_id, authenticated principal, authorization scope)` bounded resolution；missing、
  跨 principal 和跨 scope 都返回同一 `resource_not_accessible`。
- `audit.enabled=false` 时新 POST 在路径解析、数据库写、Runner lookup 前返回
  `audit_feature_disabled`。已有 Job 的 GET、cancel、stop ACK 与 reconcile 继续可达。
- authentication → operator capability → feature flag（仅 create）→ strict schema/cross-node → bounded
  owner resolution → catalog → state/lease → I/O。422 使用既有全局 canary redaction，不反射 path、
  unknown key/value、`loc` 中敏感动态值或 Pydantic input。
- safe projection 不返回 `repository_path/restricted_request_json/lease_id/capsule_id`、完整 digest owner
  envelope、stderr 或 backend locator；digest 可以使用完整 64 lower-hex 值，显示层再做摘要。

## 6. Source-root 与请求契约

### 6.1 PreflightRequest/v1

AUD-200 只接受：

- `source_execution_target.node_id = local`，source/analysis node 恒等；
- backend 与服务端 `NodeAuditPolicy` 精确匹配，caller 不能选择未授权 image/policy；
- target kind `revision|working_tree`；Diff 用 `mode=diff` 与不同 base/head 表示；
- revision 禁 `include_untracked`；非 Diff 禁 base revision；
- 有界、规范化、仓库相对 include/exclude；禁止 absolute、`.`、`..`、NUL、反斜线和重复 separator；
- AUD-209 前 context 必须是 `input_id=null, repository_paths=[], discover_defaults=false`。

绝对 repository path 使用平台对应 canonical wire 形式。Control Plane 创建 Job 前根据配置的 source
root inventory 做第一次 realpath/descriptor 授权；Source Runner 在打开前用自己的 allowed roots 做
第二次校验。`source_roots=[]` 是 deny-all。任一 source root 与 Snapshot、state/database、Run
workspace、Artifact、audit temp、fix root 存在双向 ancestor/descendant、同目录或 symlink alias
关系时启动或请求 fail closed。

Source root identity 至少绑定平台、filesystem/device/inode（可用时）、canonical root 与 policy
version的 domain-separated digest；repository identity 绑定 root-relative descriptor chain 与 Git
administrative structure。identity 不是内容 digest，AUD-201/202 必须在消费时重验。

### 6.2 Git structure

Runner 在 Capsule 创建前只做 descriptor-safe root admission，不解析 Git。Capsule 内验证 `.git`
directory/file、gitdir、commondir、worktree admin path、object alternates、replace refs 与目标 revision。
指向允许 root 外的 external gitdir/commondir/alternate 默认拒绝。submodule/LFS 只报告 bounded
capability warning；不得 fetch、update、checkout 或 materialize。

## 7. Production SourceIngest Capsule

AUD-200 的生产 backend 固定为同机 Linux container/VM profile。实现可以支持具体 container engine，
但必须证明：

- image 使用不可变 digest，禁止 floating tag/pull-on-run；
- repo 通过 Runner 已持有的 descriptor 或等价 inode-stable binding 只读挂载；不能 resolve path 后释放
  descriptor再重开；
- `network=none`、非 root、capabilities 全 drop、no-new-privileges、只读 rootfs、无宿主 home/
  Docker/SSH/cloud socket、clean allowlist env；
- 独立 tmpfs/out，CPU/memory/pids/wall/input/output/file limits；
- 固定 worker entrypoint 与 argv allowlist，不接受 shell string；
- capsule ID、prepare proof、process/container identity、terminal state与 stop receipt 可持久恢复；
- 原仓库在 Preflight 前后保持只读且 identity 未变。

`SafeGitAdapter` 只存在于 Capsule worker。它使用 `--no-optional-locks`、禁 prompt、system/global
config、hooks、fsmonitor、replace refs、external diff/textconv、filters、credential/helper、URL rewrite和
网络协议；stdout/stderr 都有独立 byte/time limits。Control Plane 与普通 Worker 不 import worker 模块，
不启动 Git，也不解压 object/index。

Inventory 前后均执行 strict/full `git fsck`，且成功返回时 stdout/stderr 必须为空；object guard
绑定仓库 SHA-1/SHA-256 格式、regular/single-link entry fingerprint、`.pack + .idx` 成对关系与
MIDX sidecar 主文件，拒绝 non-empty graft，并对 shallow repository 产生显式 warning/模式限制。

macOS/Windows 若只能把宿主 pathname交给远程 Docker daemon、无法证明 same-host descriptor binding，
必须报告 backend unavailable；受控本地 Linux VM/container backend只有在 mount/identity/stop proof
合同完整时才能声明可用。

## 8. Persistence、恢复与迁移

迁移新增独立 Preflight Job/receipt事实，不修改或放宽 Run-scoped Runner表。升级在 SQLite exclusive
transaction / PostgreSQL固定锁序下建表、索引、约束并验证 FK；offline PostgreSQL upgrade必须可
生成稳定 SQL。downgrade只有表为空时允许；存在任何 Job、receipt 或 protocol capability 时在 DDL
前 fail closed，offline downgrade显式拒绝。earliest revision → head → reopen、rollback/retry和损坏行
均需测试。

Control Plane 周期 reconciler 只读取不含 restricted request/path/capsule locator 的 bounded projection：

- 过期且从未 claim 的 pending Job，只能凭 `attempt=0`、无 lease/capsule 的 DB
  `never_created` proof 进入 `cancelled`；
- lease 已过期的 claimed/running/cancelling Job，只能进入 `outcome_unknown`；
- `outcome_unknown` 不由 expiry batch 继续投影为终态，也不被盲目 redispatch；
- exit/stop terminal 只通过 owner/lease/state/capsule 精确绑定的 receipt callback/replay 收敛；
- Runner journal 与 SourceIngest backend 负责 Capsule/create/start/stop/orphan 的本机恢复与肯定停止证明。

并发 reconciler 依赖 `state_version + effect_owner_digest + expiry/proof facts` 的 CAS；同一 Job
只有一个投影赢家。Feature Flag 关闭后 expiry fencing、receipt replay 与 cleanup 仍可达。

## 9. Effect catalog 与回归围栏

AUD-200 必须为 create/get/cancel、service create/get/cancel、claim/renew/start/finish/stop、Runner
poll/callback、repository mutation与reconciler注册明确 operation/origin/effect/mode。Preflight owner只
允许 `PREFLIGHT_JOB_OWNER_ENVELOPE` resolver；unknown组合 fail closed。

以下回归必须保持：

- General Run/Runner/Workflow行为不变；
- ordinary Code Audit `RunnerControlService.enqueue()` 调用次数恒为零；
- Preflight不创建Audit、Run、Project、Contract、StartIntent、Workflow、Snapshot、Artifact、
  Execution、Finding、Token或Context Bundle；
- Preflight不调用模型、Agent、Scanner、Detector、build、test、PoC、fix、网络fetch或依赖安装；
- 独立性门禁继续证明没有Codex Security表达材料或运行时依赖。

## 10. 验收测试

AUD-200 至少通过：

1. Domain：canonical request/owner/result/receipt digest、全部合法/非法状态转换、path/context/target
   validation、terminal replay漂移。
2. Persistence：幂等竞争、所有CAS、lease expiry不重跑、finish-vs-cancel、receipt projection、corrupt
   row、restart、migration upgrade/downgrade/offline/lock/failure injection。
3. API：route effect、feature flag、auth/cross-principal/scope、safe 404、202/200 replay、断线、cancel、
   422 canary redaction、安全projection。
4. Runner：capability matrix、wrong-owner优先、lease/envelope/state drift、cross-wire callback、bounded
   output、journal replay、stop receipt。
5. Recovery：Control Plane/Runner kill-restart、claim/start/finish timeout、outcome_unknown、orphan Capsule、
   cancel convergence与feature-disabled reconciliation。
6. Security fixture：恶意Git object/index/config/include/hook/fsmonitor/filter/textconv/helper/alternate/
   replace-ref、symlink/hardlink/special file、Unicode/NUL、压缩炸弹、output flood和TOCTOU；证明宿主零
   parser、零repo写入、零网络、零凭据/socket、资源上限生效。
7. 生产 backend：local Linux真实container/VM smoke；fake只能满足unit/contract tests。
8. 全量Python、Runner、Ruff、边界与release gate；所有Agent相关命令使用Conda `agent`环境。

上述第 7 项是生产 backend qualification gate；当前 macOS work unit 记录为 `not executed`，
不阻止 AUD-200 代码/合同收口，但阻止在 Linux smoke 通过前声明生产 backend qualified。

## 11. 实施结论与平台验证边界

AUD-200 实施状态为 `completed`：协议、迁移、API、Runner、SourceIngest、Git guard、恢复、
停止证明、安全投影与可在当前环境执行的回归已收口，独立复审无剩余 P0/P1。下一项为 AUD-201。

本次完成复核运行于 macOS，未执行真实 local-Linux Docker descriptor/mount round-trip smoke。
因此本文不声明 macOS/Windows backend 可用，也不声明生产 Linux backend 已完成环境资格认证；
非 Linux 必须继续 fail closed。真实 Linux smoke 仍是启用/宣传生产 SourceIngest 前不可跳过的
release/acceptance gate，fake/in-process backend 不能替代该证据。
