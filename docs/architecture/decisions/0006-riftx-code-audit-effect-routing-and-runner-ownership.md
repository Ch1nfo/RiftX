# ADR-0006：RunKind 效果路由、Workflow 控制与 Runner 所有权契约

> 状态：Accepted
>
> 实施状态：AUD-106 completed
>
> 日期：2026-08-03（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-106`
>
> 产品基线：`ee9adaa9`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md` 第 4.3–4.5、14、20.4、22/M1/AUD-106 节
>
> 前置决策：ADR-0001、ADR-0002、ADR-0003、ADR-0004、ADR-0005

## 1. 背景与结论

AUD-104 使用 `require_general_run_operation()` 在 API 和 Application Service 两层临时阻止
Code Audit 进入通用 Run 的效果路径。该 bridge 保证 M1 draft 不会执行，但不能作为 3.0 的最终
架构：内部 Worker、Runner callback、WebSocket、cleanup reconciler 和 Temporal Activity 不一定
经过 HTTP 路由；RunnerCommand 也只有 Node/Runner principal，没有可持久验证的 Run、Execution、
Audit 或执行计划所有权。

本 ADR 冻结以下结论：

1. 建立 application-owned、可机读、默认拒绝的 `RunKindEffectPolicy`。它同时描述 operation、
   origin、effect class、RunKind、owner resolver、模式和显式 Audit alternative；不得把 API decorator
   当成唯一 inventory。
2. `RunWorkflowControlRouter` 只负责根据已经验证的 RunKind/owner 选择 Workflow 协议。General 分支
   原样委托现有 `TemporalRunClient`；Code Audit 永远不 fallback 到 `riftx-run-{run_id}`。
3. Audit 控制由独立 mutation layer 消费 ADR-0003 的只读 `AuditControlPlan`。Generic Run controls
   对 Code Audit 继续拒绝；generic cancel 不得自动升级、转写或转发成 Audit cancel。
4. Runner host effect 使用不可变 typed ownership。所有 command enqueue、replay、lease、poll、renew、
   output、finish、stop ACK、Execution callback、Workflow completion 和 reconciliation 使用同一验证链，
   不得从 payload、path、session、tool call、idempotency key 或 command kind 猜 owner。
5. 所有 legacy RunnerCommand 显式 quarantine。迁移不从旧 payload 回填 owner；需要继续停止旧资源时，
   reconciler 根据权威资源账本创建新的 verified replacement stop command。迁移时已 leased 的 stop
   只能用原认证 principal + 原 lease 进入独立 legacy stop-proof owner；它不构成 Run/effect owner。
6. Safety allowance 只能减少或证明已有副作用。它不能创建普通效果、推进错误 Workflow、猜终态或以
   command kind 代替 safety origin。
7. AUD-106/M1 没有任何权威 Code Audit effect plan，因此 Code Audit Runner enqueue 保持为零。
   这只是 M1 admission fence：M2/M3 后续只能按 operation family 逐项开放
   `AuditStaticEffectPlan`，M7/M9 的 Build/Test/PoC/Fix 则必须使用独立 `AuditExecutionPlan` 和
   `mandatory_one_plan` 审批。任何阶段都不得用 policy digest、contract digest 或调用方字符串
   冒充 plan proof。

本实现不使用 Codex Security Provider、代码、Prompt、Schema、Skill、运行时、依赖、端点、测试或
生成物。所有 contract、命名、迁移和测试均为 RiftX 自有实现。

## 2. RunKindEffectPolicy

### 2.1 可机读模型

稳定 application contract 至少包含：

~~~text
RunEffectRule
  operation: RunEffectOperation
  origin: EffectOrigin
  family: RunEffectFamily
  owner_kind: EffectOwnerKind = global | run | preflight_job | legacy_runner_command
  allowed_run_kinds: frozenset[RunKind]
  required_effect: OperationEffect
  ownership_resolver: OwnershipResolverKind
  required_claims: frozenset[OwnershipClaim]
  audit_alternative: RunEffectOperation | none
  mode: EffectMode

EffectEntryPoint
  qualified_name
  surface: route | websocket | service | callback | reconciler | activity | runner_command
  operation
  origin

ResolvedEffectOwnership =
  GlobalEffectOwnership
    owner_kind: global
    administrative_scope_digest
  | RunEffectOwnership
    owner_kind: run
    run_id
    run_kind
    audit_id?
    plan_digest?
    execution_id?
    effect_execution_id?
    resource_kind?
    resource_id?
    node_id?
    runner_principal?
    runner_command_id?
  | PreflightJobEffectOwnership
    owner_kind: preflight_job
    preflight_job_id
    operator_principal_id
    authorization_scope_digest
    request_digest
    node_id
    capsule_id?
    lease_identity?
  | LegacyRunnerCommandEffectOwnership
    owner_kind: legacy_runner_command
    node_id
    runner_principal
    runner_command_id
    lease_identity
    quarantine_state = quarantined:legacy_ownership_missing
~~~

`RunEffectRule` 沿用既有命名，但 `owner_kind` 是必填判别器；`preflight_job` variant 只为 M2 的
Preflight route/Runner protocol 保留，不表示 AUD-106 已实现该 Job。任何代码先读取 `run_id` 再猜
owner kind 都是错误实现。Global、Run、Preflight 与 Legacy Runner ownership 不能相互 fallback 或由
nullable 字段组合推断。`legacy_runner_command` 只服务迁移后 ownership-missing leased stop ACK，
必须同时证明原 node/principal/command/lease 与固定 quarantine state；它不能读取或声称 RunKind，
不能进入普通 finish/stop ACK/Preflight/global operation。`owner_kind=run` 时 `allowed_run_kinds` 必须
非空；其余 variant 必须为空且不得读取 RunKind。

`EffectMode` 固定区分：

- `read_only`：只返回安全投影；不能隐式 append Event、同步 intent 或更新 last-accessed；
- `normal`：普通业务 mutation/effect；
- `ownership_callback`：经过认证和完整 owner 复验的 callback；
- `safety_reduce_only`：只允许围栏、停止、撤销 lease 或销毁资源；
- `stop_proof`：只允许记录肯定的、typed、owner-bound 物理停止事实；
- `reconcile`：只把权威事实收敛到允许的更安全状态；
- `global`：不解析 Run owner 的管理操作，不能被 Run-scoped caller 复用。

catalog key 是 `(operation, origin)`。operation、origin、owner kind、RunKind（适用时）、effect 或
mode 任何未知值均默认拒绝。
`audit_alternative` 只用于错误提示、文档和 UI discoverability；Policy 不得自动改写请求。

### 2.2 Inventory 范围

CI 不能只枚举 FastAPI decorator。它必须覆盖：

- API Policy 中所有 `DURABLE_WRITE`、`WORKFLOW_CONTROL`、`HOST_EXECUTION`、`HOST_CONTROL`、
  `RUNNER_CALLBACK` 和具有隐藏写入的 read route；
- WebSocket connect、每条 command/message 和每批 stream；
- Application Service 与 runtime recorder 的 public mutation/callback 方法；
- Temporal Workflow client、Activity、Runtime coordinator 和 execution completion；
- Control Plane/Worker cleanup、finalization、supervisor recovery 和 safety reconciler；
- Runner command producer、poll/lease/output/finish/stop ACK；
- Approval recorder、deferred execution dispatcher、Connector、Browser、Terminal 与 Target HTTP。

首批受管类型至少包括：

~~~text
RunApplicationService
AuditApplicationService
ApprovalApplicationService
ApprovalRequestRecorder
RuntimeApprovalRequestRecorder
ArtifactApplicationService
ExecutionApplicationService
ExecutionService
DeferredExecutionDispatcher
FindingApplicationService
ReportApplicationService
MemoryService
TerminalApplicationService
BrowserApplicationService
TargetHttpApplicationService
ConnectorApplicationService
RunnerControlService
RuntimeCoordinator
RiftXActivities
RuntimeCycleActivities
RunSafetyStopService
ExecutionReconciler
ControlPlane
TemporalWorkerRuntime
~~~

CI 对受管类型的 public async 方法要求恰好归入 read、mutation、callback、safety/reconcile 或显式
out-of-scope 分类。新增 route、method、callback 或 reconciler 没有 entrypoint/rule 时立即失败。
Route 的 `RouteEffect.value` 必须与 catalog 的 `OperationEffect.value` 恒等。

### 2.3 失败与授权顺序

Run-scoped 外部边界统一使用：

~~~text
authentication/capability
  -> bounded raw owner resolution
  -> exact parent/child owner proof
  -> catalog lookup and RunKind decision
  -> state/lease/action admission
  -> I/O, Event, mutation, Workflow signal or host effect
~~~

Preflight 边界使用：

~~~text
authentication/capability
  -> owner_kind=preflight_job
  -> bounded job/principal/authorization/request resolution
  -> catalog/effect decision
  -> job state/lease/capsule admission
  -> I/O, mutation or host effect
~~~

Run-scoped Runner 边界使用更严格顺序；M2 的 Preflight Runner 使用独立
`preflight_job_owner` envelope 和同等严格的 job/principal/request/lease proof，不得制造 run_id：

~~~text
Runner authentication
  -> node/principal owner
  -> ownership schema/quarantine/digest
  -> Run/Execution/effect/Audit/plan binding
  -> catalog RunKind/mode decision
  -> lease/action admission
  -> I/O or state mutation
~~~

wrong owner 必须先于 RunKind/对象状态错误，避免跨 owner 探测。拒绝路径必须是零 Event、零数据库
mutation、零文件、零 Runner command、零 Workflow signal、零网络和零 Memory promotion。

## 3. Workflow 路由与控制

### 3.1 Router 职责

`RunWorkflowControlRouter` 只做协议选择和 owner-bound dispatch，不做授权、状态投影或资源停止：

- `RunKind.GENERAL`：逐字保持现有 workflow ID、signal 名称、payload、调用顺序、错误映射和
  Temporal history；直接委托现有 `RunWorkflowClient/TemporalRunClient`。
- `RunKind.CODE_AUDIT`：由已授权 `audit_id` 定位 `riftx-code-audit-{audit_id}` 的专用协议；禁止
  使用 run_id 猜 Audit，禁止 fallback 到 general client。
- M1 尚未注册真实 Audit Workflow。draft/never-started Audit 的 cancel 只做原子 lifecycle fence 与
  safety sweep，不发送不存在的 Workflow signal；pause/resume 按 ADR-0003 返回不可控制/feature
  disabled，不制造假 Workflow。

Router、`AuditRunStateProjector`、`RunSafetyStopService` 和 authorizer 是不同职责。任何一个类不得
同时根据当前状态猜 owner、发 signal、改写 Audit/Run 和停止资源。

### 3.2 Audit controls

新增 `AuditControlApplicationService` 或等价 mutation layer：

1. 通过 Audit 根授权与同一 consistent-read aggregate 获取 ADR-0003 `AuditControlPlan`；
2. 使用 state_version/CAS 先建立 admission fence；
3. pause/resume 在目标 Workflow 存在时通过 Router 发专用 signal；
4. cancel 先围栏新效果，再执行 kind-aware safety stop；只有完整 proof 后才投影可见终态；
5. signal 结果不确定时保持更安全 fence，并写 durable intent 由 reconciler 查询/重放；不能回滚到
   可产生效果的状态；
6. projection、Event 和 intent 在同一 UoW，重复请求按 operation/request identity 幂等。

Audit cancel route 必须登记为 `HOST_CONTROL`。Generic `/runs/{id}/cancel` 保持
`WORKFLOW_CONTROL` 且对 Code Audit 永远返回 `run_kind_operation_unsupported`。

### 3.3 Cleanup 与 completion

以下入口必须在首个 effect/state mutation 前按 RunKind 分流：

- `RunApplicationService.stop_resources_for_cleanup`；
- `ControlPlane._reconcile_completing_runs`；
- `TemporalWorkerRuntime._safety_reconciler_loop` 与 finalization reconciliation；
- `RiftXActivities.cleanup_run_activity` 和 compatibility finalizer；
- `ExecutionReconciler`；
- execution/effect completion signal。

General-only cleanup 方法增加内层门禁。Code Audit 使用 Audit-owned cleanup delegate，不能调用
generic pause/cancel/finalization 或 signal `riftx-run-{run_id}`。

Execution/effect completion、stop ACK 与 Workflow signal 之间使用 durable receipt/outbox 和幂等
reconciler。进程崩溃、Temporal 暂时不可用或 Worker 重启不能永久丢失 completion，也不能重复推进
Workflow。stop ACK 永不走普通 `execution_completed` signal。

该 outbox 固定为通用 `workflow_signal_intents`，以 `owner_kind=general_run|code_audit`、exact
workflow identity/protocol、event identity 和 payload digest 做判别/唯一约束；AUD-106 必须把
General completion/Approval 一并迁入。只新增 Audit-specific 表而保留 General 内存 retry 不满足本
ADR，Audit intent 也不能 fallback 到 General workflow ID/signal。

Delivery state 固定为 `pending -> claimed -> delivered`，以及三条显式分支：明确未发送且仍可能恢复
时进入 `retryable`；请求已经因 owner、policy、payload、Workflow identity 或终态 Run 永久失效时进入
terminal `superseded`；发送已开始但响应不确定，或 delivery claim 过期时进入 `outcome_unknown`，只能由
history/projection probe 明确判定 `delivered` 或 `not_delivered` 后再收敛。`superseded` 不携带 lease、
retry schedule 或 delivery receipt，且不会再次被 claim；无参数的历史 signal 不能证明某个具体 intent
已经送达，因此保持 unknown，禁止用同名旧 history 事件冒充 receipt。

General intent 发送前必须重新加载 authoritative Run，并逐字节验证
`Run.temporal_workflow_id == intent.workflow_id`。验证通过后，outbox 将该持久化 ID 显式传入 router 与
Temporal client；只能由普通、非 outbox 调用沿用当前配置推导 Workflow ID。配置前缀变化不能改写或
误投递历史 Run 的 signal。

Feature Flag 关闭时：新 Audit admission/resume 仍拒绝；历史 read、cancel、cleanup、stop proof、
quarantine reconciliation 和 durable signal reconciliation 继续可达。

## 4. Runner ownership

### 4.1 两层不可变账本

Runner host effect 使用两个 typed、1:1、insert-only/immutable identity：

~~~text
RunnerEffectBinding/v1
  id
  schema_version
  run_id
  run_kind
  origin
  operation_family
  execution_id or effect_execution_id
  resource_kind/resource_id
  node_id
  runner_instance_id/runner_epoch
  audit_id?
  plan_digest?
  binding_digest
  created_at

RunnerCommandOwnership/v1
  command_id
  schema_version
  effect_binding_id
  operation
  operation_family
  payload_digest
  output_contract_digest
  envelope_digest
  ownership_state: verified | quarantined
  quarantine_reason?
  created_at
~~~

实现可以在 v1 先把等价字段放进一张 1:1 ownership 表，但不能把 legacy nullable owner columns
伪装成 verified。新 command 必须在同一事务写 command + ownership；安全 FK 使用
`ON DELETE RESTRICT`。

所有 family 必须有 typed execution identity。Process/PTTY 可以绑定现有 `Execution.id`；Browser、
Target HTTP 与其他非 Execution family 必须创建 durable effect identity 或等价 typed resource
binding。不得把 session ID、path、tool_call_id 或 payload 字段冒充 execution identity。

### 4.2 Domain invariants

- schema version 固定为 `riftx.runner-command-ownership/v1`；
- General ownership 的 `audit_id/plan_digest` 必须同时为空；
- Code Audit ownership 的 `audit_id/plan_digest` 必须同时非空，并与 Run、Audit、Node、effect identity
  及该 operation family 的权威计划恒等；M2/M3 静态 family 只接受 `AuditStaticEffectPlan`，M7/M9
  动态 family 只接受 `AuditExecutionPlan`，两类 digest 不可互换；
- `payload_digest`、`output_contract_digest`、binding/envelope digest 使用 domain-separated canonical
  JSON + SHA-256；客户端提供的 digest 不可信；
- output byte limit、允许 stream、result schema、stop ACK schema 等安全参数进入 typed output
  contract，不再从 payload 读取；
- Runner poll response 携带完整 envelope/binding digest；renew/output/finish 必须原样回传并在任何
  handler、文件或网络 I/O 前验证；
- command `schema_version` 不是并发 token；lease/finish 使用独立 `state_version` CAS；
- `CANCEL/BROWSER_CLOSE/...` command kind 不能证明 origin、family 或 safety 资格。

AUD-106 时尚无任何正式 Code Audit effect plan，因此 Code Audit 的 Execution、effect binding 和
RunnerCommand admission 均为 deny-all；测试必须证明 enqueue 数量恒为零。该结论只描述 M1：
后续任务必须通过版本化 plan registry、operation-family allowlist 和 protocol capability 显式扩展，
不能删除 ownership validator 或把全部 Code Audit command 一次性放行。

### 4.3 Repository 与 callback

idempotent enqueue 只有 kind、target principal、完整 ownership/envelope、payload digest 和 output
contract 全部恒等时才能返回旧 command。相同 `(node_id, idempotency_key)` 的任一字段不同都返回
冲突且零副作用。

enqueue、replay、claim/poll、renew、output、finish、Execution status/output、stop ACK、completion
和 reconciliation 调用同一个 ownership validator。poll 遇到缺 ownership、坏 digest 或跨 owner
记录时，原子 quarantine 后继续有界查找下一条，不能让腐化行阻塞队列。

安全优先级和 `safety_only` 资格只能来自 catalog 中 `safety_reduce_only/stop_proof` rule。Repository
不得再按 command kind 建立 safety allowlist。

stop ACK 使用 family-specific typed schema，绑定 command、envelope digest、effect binding digest、
execution/effect identity 和 principal。肯定 ACK、command terminal state、resource stop projection 与
immutable `RunnerStopReceipt` 在单一 UoW 原子提交；若暂时不能原子完成，只能进入
`ack_projection_pending`，reconciler 完成前不能视为 stop confirmed。ACK 不触发普通 Workflow
completion。

### 4.4 Runner protocol capability

新增 `runner_command_ownership_v1` capability/protocol gate。新服务器不向旧 Runner lease verified
新格式普通命令；旧 Runner 不得通过忽略 envelope 字段继续执行。Runner journal/idempotency key 必须
包含 binding/envelope digest。HTTP callback 明确拆分：旧 `/commands/{id}/finish` 只接受 legacy
stop ACK wire，并在 API/Service/Repository 三层验证 legacy owner；ownership-v1 Runner 只调用
`/commands/{id}/finish-owned`，其 state/envelope/binding 字段全部必填。两个 Schema 交叉提交均在
进入 Service 前拒绝，不能再用“缺 identity 字段”切换 owner variant。

### 4.5 分阶段计划与 Preflight owner

本 ADR 只实现 AUD-106/M1 的通用 ownership/protocol 骨架和零 enqueue fence，不在本任务创建
Preflight、Snapshot、Detector 或动态验证计划。后续 admission 固定为：

- M2 `AuditPreflightJob`：发生在 Audit/Run 创建之前，使用独立的 pre-Audit owner envelope、lease、
  request digest、status/cancel、stop receipt 和 reconciler；它不是 Run，不能伪造 `run_id/audit_id`，
  也不能复用本 ADR 的 Run-scoped ownership 作为事实根。若经过 Runner transport，必须发布独立
  `preflight_job_owner` capability。
- M2/M3 `AuditStaticEffectPlan`：只允许 Snapshot materialize/mount、content parse 和已注册静态
  Detector；固定 same-node、只读源码、无网络、无凭据和 bounded out/tmp。每个 family 单独注册，
  未注册 family 保持 deny-all。
- M7/M9 `AuditExecutionPlan`：只用于 Build/Test/PoC/Fix/Retest，必须绑定
  `mandatory_one_plan` Approval；Static plan 或 General Run grant 不能替代。

未来计划可以复用本 ADR 的 `plan_digest` ownership slot 和 digest-echo 验证链，但必须由各自 ADR/
任务冻结具体 schema、owner resolver、capability、family allowlist 和迁移。AUD-702A 冻结动态计划/
Approval/admission；AUD-702B 在 Capsule Evidence 与停止恢复完成后开放 API/read models；
AUD-702C/D 再分别开放 CLI/WebUI。它们都不是所有 Code Audit Runner 效果的首次开放点。

## 5. Legacy migration、quarantine 与 downgrade

### 5.1 Upgrade

所有缺 ownership 的既有 RunnerCommand，不论 pending、leased 或 terminal，统一创建 quarantine
record；禁止从 payload、path、kind、target 或 idempotency key 回填 owner，禁止原地解封：

- pending/leased 普通效果永不再 lease；
- 旧 in-flight stop 只允许原认证 principal + 原 lease 的窄 legacy ACK sink；ACK 只保存隔离证据，
  不结束 command、不创建普通 stop receipt/projection、不直接推进权威资源或 Workflow；exact replay
  幂等，漂移、伪造的 pre-migration namespaced evidence、错误 principal/lease/state 一律拒绝；
- reconciler 从 Execution、Terminal、Target HTTP intent、BrowserSession 等权威账本创建新的 verified
  replacement stop command；owner 无法证明时保持 unconfirmed/manual reconciliation；
- replacement/reconciliation 幂等，Feature Flag 关闭仍运行。

SQLite upgrade 在同一 `BEGIN EXCLUSIVE` 中完成 legacy audit、表/索引/FK 建立、quarantine seed、
`PRAGMA foreign_key_check` 与 commit；任一步失败回滚。PostgreSQL 使用固定锁序和同等 owner audit。
迁移不得读取 Runner output 文件或发 command。

### 5.2 Downgrade

存在任一 verified ownership/effect binding/stop receipt/replacement command/reconciliation progress、
非空 protocol capability，或任一 `runner_commands.state_version <> 0` 的 post-migration command state
时 downgrade fail closed。后者保护 legacy ACK evidence 依赖的 CAS 版本，禁止降级删除版本后再次
升级而使 proof 失去可验证性。PostgreSQL online downgrade 在任何读取/DDL 前按固定顺序锁定全部
Runner fact tables；offline downgrade 明确拒绝。只有新表为空或全部是可无损删除的 untouched
quarantine metadata 且 command state 未变化时，才允许回到旧 Schema；不得丢 owner/proof 后继续。

### 5.3 Runtime metadata bootstrap

`Database.create_schema()` 必须执行与 Alembic 等价的 legacy quarantine/空表证明。带
`alembic_version` 的旧 Schema 不允许 metadata 自动补出一半新表；必须先运行迁移。

## 6. Generic read 最终决策

AUD-106 冻结 M1 generic Code Audit read allowlist：

- Run、Event、Execution 和 `public_export` Artifact：继续使用 ADR-0004/0005 的专用安全投影；
- Finding、Report、Approval、Action、Graph、Metrics、Target HTTP/Traffic、Terminal、Browser、
  Context、Memory、Connector：没有 Audit 专用产品投影与字段 allowlist 时继续拒绝；
- Generic Run grant/`approve_for_run` 永远不能作用于 Audit；Audit 只接受绑定一个 immutable plan 的
  dedicated decision；
- 若未来开放 stream/WebSocket，每批数据都重新验证 frozen child/run identity 和 auth session，不能
  只在握手时检查。

## 7. 强制实施顺序

1. 接受本 ADR，回填 AUD-105 commit/provenance，标记 AUD-106 `in_progress`。
2. 建立 catalog 与纯验证测试，temporary bridge 保持不变。
3. 完成 route/service/callback/reconciler/runner-command 全入口 inventory。
4. 注入 Policy；General 保持原行为，Code Audit 普通效果仍默认拒绝，safety 只 reduce/prove。
5. 引入 Workflow Router，先证明 General 路径与旧协议完全等价。
6. 增加 effect identity、Runner ownership、protocol capability 和 legacy quarantine migration。
7. 更新所有 command producers、Runner API/daemon、lease/output/finish/ACK 与 callback validator。
8. 增加 durable completion/stop receipt outbox 与 reconciler。
9. 增加 Audit dedicated controls/projector/router；M1 只支持不需要真实 Audit Workflow 的安全行为。
10. 完成全链回归后才移除可替代的 temporary bridge；没有 catalog coverage 的 bridge 继续保留。

## 8. 验收与反例

必须证明：

- unknown operation/origin/owner kind/RunKind/effect/mode 默认拒绝；global/run/preflight variant 不能
  通过 nullable 字段或 fallback 互换；
- route effect 与 catalog 不一致、受管 public async method 未分类时 CI 失败；
- owner mismatch 优先于 RunKind/状态错误；拒绝路径零副作用；
- generic cancel 不会转成 Audit cancel；Code Audit 不进入 generic Agent cycle、Execution、
  `WAITING_USER`、`COMPACTING` 或 generic finalization；
- General Run API/CLI/Web/Runner、workflow ID、signal payload 和旧 Temporal history 完全回归；
- exact/divergent command replay、并发 claim 单赢家、digest echo、I/O 前拒绝；
- corrupt/quarantined row 不阻塞 poll 且永不产生普通效果；
- 新服务器不向不支持 ownership protocol 的 Runner 发普通命令；
- family-specific stop ACK 原子恢复，stop ACK 不发送普通 completion；
- legacy quarantine/replacement/ACK 幂等，Terminal ledger 歧义在 admission 前退化为权威 Execution
  cancel，迁移/重启/失败注入/downgrade 无数据或 proof 丢失；
- Feature Flag 关闭后 read、cancel、cleanup、stop proof、quarantine/signal reconcile 仍可达；
- General 与 Audit completion/Approval 都通过通用 durable outbox；进程崩溃后不会回退到内存 retry；
- AUD-106/M1 没有权威 Code Audit effect plan，因此 Runner enqueue 恒为零；M2/M3 的静态 family
  和 M7/M9 的动态 family 必须分别证明对应 plan/capability，未注册 family 仍为零。

目标测试、全量 Python、Ruff、核心 Mypy、Alembic 全链、independence boundary、release gate 与
`git diff --check` 全部通过后，AUD-106 才能标为 completed。

## 9. Provenance

```yaml
provenance_id: RXP-AUD-106-001
task_id: AUD-106
artifact_class: architecture_decision
artifact_version: ADR-0006
paths:
  - docs/architecture/decisions/0006-riftx-code-audit-effect-routing-and-runner-ownership.md
author: Ch1nfo (Git author); Codex task aud106
authored_at: 2026-08-03T15:54:50+08:00
requirements_sources:
  - docs/riftx-3-code-audit-development-spec.md sections 4.3-4.5, 14, 20.4, 22/M1/AUD-106
implementation_inputs:
  - RiftX source tree at ee9adaa9
  - ADR-0001 through ADR-0005
public_standard_versions:
  - not_applicable
third_party_expressive_material: none
third_party_dependency_decisions:
  - ADR-0001
reviewer: Codex subagent aud106_completion_audit
review_sources:
  - complete AUD-106 diff and effect inventory
  - Runner protocol, legacy ACK, Terminal replacement, migration, and downgrade review
  - focused 152-test independent audit run plus full repository/release evidence
review_result: accepted; no remaining P0/P1 findings
commit: pending_backfill
notes: This ADR records an independent RiftX contract and does not claim strict clean-room isolation.
```
