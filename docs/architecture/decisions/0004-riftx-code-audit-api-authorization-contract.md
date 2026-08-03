# ADR-0004：RiftX Code Audit API、对象授权与临时 RunKind 围栏契约

> 状态：Accepted
>
> 日期：2026-08-03（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-104`
>
> 产品基线：`51fa06cb2d5a213942bd2c46e2873eaceae0461a`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md`
>
> 前置决策：ADR-0001、ADR-0002、ADR-0003
>
> 决策所有者：RiftX contributors；准确作者、审阅者和 Commit 由本文第 12 节的
> provenance 记录保存

## 1. 背景与结论

`AUD-103` 已经能够在一个事务中创建 Code Audit draft，但还没有 HTTP wire contract、
逐路由 Policy 或对象级授权。直接把内部 Application Service 暴露给 HTTP 会产生四类问题：

1. 调用方可能把 `authorization_reference`、Audit/Project ID、workspace 或 Workflow ID
   当作可控字段，进而选择服务端授权域或持久化绑定；
2. 先加载完整 `AuditContractRecord` 再判断对象归属，会在拒绝请求之前读取绝对 source path、
   canonical Contract 和其他敏感事实；
3. list 若先分页再做内存过滤，会泄漏对象数量、制造稀疏页，并允许未授权对象影响排序和游标；
4. `code_audit` Run 已经存在于通用 Run、Terminal、Browser、Execution、Artifact 等能力旁边，
   在专用 Workflow router 尚未完成时可能误入通用效果路径。

本 ADR 冻结以下结论：

- `AUD-104` 只公开 draft 创建、Audit list 和 Audit detail；没有 Preflight、Start 或 Audit 控制
  端点；
- HTTP 调用方只能选择顶层 caller-owned draft facts；为验证现有 M1 Domain/Persistence 而接收的
  full-contract-shaped body 中，proof/consent/selection 类字段只是 synthetic untrusted
  assertions，没有任何授权或执行效力。授权引用、聚合身份和真正执行身份全部由服务端生成或
  解析；
- 单对象读取先以不含 canonical Contract 的 typed raw binding 完成授权，再在同一个一致读取
  中加载和复验完整 aggregate；
- list 的授权 Engagement scope 必须进入 SQL，并且位于排序、`LIMIT` 和 `OFFSET` 之前；
- Audit 与 generic Code Audit Run 都使用显式安全投影，不序列化敏感持久化字段；
- missing 与 denied 使用统一 404；同一类创建冲突和不支持的 RunKind 操作使用稳定、脱敏的
  409；
- 在 `AUD-106` 的 machine-readable `RunKindEffectPolicy` 和 Workflow router 落地前，使用双层
  临时围栏阻止 Code Audit Run 进入通用 mutation；
- Feature Flag 是新效果 admission fence，不是数据可见性开关，更不能移除停止、清理和物理
  stop proof 的收敛路径。

本决策不引入第三方 Code Security Provider、代码、Prompt、Schema、Skill、运行时或端点。

## 2. AUD-104 HTTP 边界

### 2.1 唯一公开路由

本任务只增加下列 route operation：

| HTTP | Route name | `RouteEffect` | Local Operator capability |
| --- | --- | --- | --- |
| `POST /api/v1/audits` | `create_audit` | `DURABLE_WRITE` | `WRITE` |
| `GET /api/v1/audits` | `list_audits` | `READ_ONLY` | `READ` |
| `GET /api/v1/audits/{audit_id}` | `get_audit` | `READ_ONLY` | `READ` |

三项操作必须逐项登记在 `api/policy.py`。Policy inventory 与 OpenAPI 扩展是同一权威来源；
未知 route name 继续 fail closed。不能把 `create_audit` 登记成 `HOST_EXECUTION`，因为本路由
严格禁止执行宿主效果；也不能在 `DURABLE_WRITE` handler 内顺手执行 Preflight、Start 或
Workflow signal。

OpenAPI 在 AUD-104 只能出现上述三个 Audit operation。以下路由仍不存在：

- `/api/v1/audits/preflight`；
- `/api/v1/audits/{audit_id}/start`；
- Audit pause、resume、cancel；
- Audit Finding、Evidence、Report、Fix、Retest 等后续领域端点。

这些端点只能由后续任务按各自的 `HOST_EXECUTION`、`WORKFLOW_CONTROL` 或 `HOST_CONTROL`
边界引入，不能把 AUD-104 的 `DURABLE_WRITE` 当作授权先例。

### 2.2 draft-only 效果

`POST /api/v1/audits` 只调用 ADR-0003 的原子 creation UoW，并产生一个
`AuditLifecycleStatus.DRAFT` 的 Audit 及其 `Run(kind=code_audit, status=created)`。它不得：

- 读取、解析、`realpath` 或执行 source repository；
- 接受、伪造、hash、reserve 或 consume `preflight_token`；
- 创建 Snapshot、CAS bytes、Scope、StartIntent 或 distribution Artifact；
- provision workspace 目录或进行任何文件系统写入；
- 启动 Temporal、Runner、Terminal、Browser、Target HTTP、Scanner、Detector 或模型调用；
- 自动执行 Start、pause、resume、cancel 或安全停止。

请求中保存的 `repository_path` 只是尚未授权执行的敏感 Contract fact。调用方提供的
`repository_identity_digest` 也只是 draft identity 输入，不是 source authorization、Preflight
proof 或读取许可。M2/AUD-201 必须以版本化 preflight plan/token 扩展同一个 creation UoW；
不得把本 draft-only 路径解释为已经完成 Preflight。

### 2.3 draft wire shape

`CreateAuditDraftRequest` 的顶层字段固定为：

~~~text
client_request_id
project_name
repository_identity_digest
contract
engagement_id（可选）
default_branch（可选）
~~~

其中：

- `client_request_id` 必须是 non-zero、lowercase canonical UUID；
- digest、ID、token、版本、字符串、tuple 数量和预算都使用有界 wire 类型；
- 每一层 Pydantic object 都使用 `extra=forbid` 和 `hide_input_in_errors`；
- wire adapter 保留 FastAPI JSON 的普通 scalar coercion，但 shape、枚举、长度、交叉字段和最终
  `AuditContract` 必须再次严格验证；
- Contract wire 中的 caller policy/target/profile/budget 作为 draft input；看似
  `operator_consent_at`、capability `proof_digest`、source/analysis prepare proof、selected
  node/backend/image/policy/candidates 的字段在 AUD-104 只是受限测试夹具，不能证明对应事实。
  服务端在 UoW 内只绑定真实 `audit_id` 与 `project_id`；
- body 顶层或 Contract 内出现未知 server-owned 字段必须返回 422，不能忽略。

这是 M1-only wire compromise，不是最终 API。所有 AUD-104 draft 都没有 authoritative
`preflight_plan_id`（或等价 provenance），也没有 Start endpoint，因此永不可执行。AUD-201
必须发布 `riftx.audit-create-draft-request/v2`，从 body 移除 proof/selection/consent 字段，由
持久 Preflight plan、Capability Registry 和服务端 consent event 构造权威 Contract。现有 v1
draft 不能原地补 token 或替换 immutable Contract；必须重新 Preflight 并创建 v2 draft。

调用方明确不能提交：

~~~text
authorization_reference
preflight_token
audit_id / project_id / run_id / contract_id
workspace_path
temporal_workflow_id
request_digest
start / auto_start
~~~

Audit mutation 的 request validation error 必须把所有 body `input` 统一替换为 `[redacted]`，并
清除错误文本中出现的任意 body string。扫描面包含 mapping 的 key 与 value、sequence value，
以及 Pydantic `loc` 中来自 body 的 segment；未知顶层、Contract 或深层字段名不能以
`extra_forbidden` location 回显。已知安全字段名由固定 allowlist 保留，其余 segment 使用
`[redacted]`。绝对 path、canonical policy、provider disclosure 或调用方插入的未知敏感字段均
不得通过 422 回显。

### 2.4 创建与重放响应

首次成功创建返回 HTTP 201。`audit` 不是只含 ID 的缩略对象，而是第 6.1 节的完整 M1
`AuditResponse` positive allowlist；下面给出完整 shape（值仅作示意）：

~~~json
{
  "created": true,
  "replayed": false,
  "audit": {
    "id": "audit-id",
    "run_id": "run-id",
    "project": {
      "id": "project-id",
      "engagement_id": "engagement-id",
      "display_name": "RiftX",
      "vcs_kind": "git",
      "default_branch": "main"
    },
    "state_version": 1,
    "snapshot_id": null,
    "base_snapshot_id": null,
    "baseline_audit_id": null,
    "purpose": "primary",
    "parent_audit_id": null,
    "mode": "standard",
    "analysis_profile": "deterministic",
    "lifecycle_status": "draft",
    "current_phase": "authorize_and_freeze",
    "terminal_outcome": null,
    "closure_status": null,
    "publication_status": "not_started",
    "initial_distribution_revision_id": null,
    "latest_distribution_revision_id": null,
    "model_profile": null,
    "run_status": "created",
    "created_at": "2026-08-03T00:00:00Z",
    "started_at": null,
    "analysis_finished_at": null,
    "publication_finished_at": null,
    "sealed_at": null
  }
}
~~~

同一 `client_request_id` 与相同 versioned request digest 的 exact replay 返回 HTTP 200：

~~~json
{
  "created": false,
  "replayed": true,
  "audit": "<同一完整 AuditResponse；返回当前持久 lifecycle/state_version>"
}
~~~

第二个片段是结构说明而非可发送的 JSON fixture；contract/OpenAPI tests 必须使用第一个片段的
完整 `audit` shape，并把 `created/replayed` 翻转。后续生命周期已经推进时，字段使用当前值，
不能强制仍为 draft。

两项布尔值必须恰有一个为 true。Replay 返回当前已持久化 lifecycle/state version；不追加
Event、不改写 Contract、不重新生成 ID，也不把已推进对象降回 draft。同 key 异 payload 使用
`409 audit_idempotency_conflict`，且不得返回原 payload、digest 或对象细节。

## 3. 服务端派生 authorization reference

### 3.1 不是 wire 字段，也不是凭据

`authorization_reference` 是 Engagement 持久化授权域的 server-owned label。它：

- 由认证后的 `LocalPrincipal`、Trust Profile、namespace 和版本化 domain separator 派生；
- 使用 lowercase SHA-256 digest 作为稳定、不含路径的表示；
- 不包含 source path，不进入 Audit response、OpenAPI、Event 或普通日志；
- 不是 bearer credential、Preflight proof、source-root proof 或 capability token；
- 不能由 HTTP caller 选择、覆盖或从 Contract 间接提供。

Route 从 app-owned `AuditObjectAuthorizer` 获取引用；Application Service 再从同一 authenticated
principal 独立派生期望值，并以 constant-time primitive 比较 command 中的值。值缺失、不规范、
被替换或与 principal 域不一致时，创建在 UoW 前 fail closed。

### 3.2 typed Engagement scope

授权接口同时返回 `AuditEngagementScope`：

~~~text
all_engagements: bool
engagement_ids: frozenset[str]
can_create_engagement: bool
~~~

`all_engagements` 必须显式表达，禁止把空集合含混地解释成“全部”。读取已有 Engagement 和创建
新 Engagement 是两个独立权限事实。当前 Profile A 的单 Operator 实现返回显式 all/create；
Port 仍使用 typed scope，以便未来 ACL 收窄时不改变 Repository 信任边界。

显式 `engagement_id` 必须属于该 scope；没有显式 Engagement 时只有
`can_create_engagement=true` 才能在 creation UoW 中创建授权根。自然键命中已有 Project、
exact replay、并发唯一键恢复和新建路径都必须重新验证：

~~~text
actual Engagement in authorized scope
AND actual Engagement.authorization_reference == server-derived reference
~~~

任何创建分支都不能因为 client-request replay、Project natural-key replay 或竞态恢复而绕过该
检查。

### 3.3 principal-scoped request identity

ADR-0003 的 v1 request digest 不是“只对 HTTP body 做 hash”。它绑定：

~~~text
canonical_request_identity = {
  caller_payload_without_client_request_id,
  server_derived_authorization_reference
}
~~~

因此相同 body/client_request_id 从不同 principal 或 authorization domain 到达时必须得到
`audit_idempotency_conflict`，不能把原 principal 的 Audit 当成 exact replay 返回。这是有意的
request-key ownership 防线。`authorization_reference` 仍不是 wire 字段或凭据，也不得出现在
错误/响应。

AUD-201 引入 Preflight reservation 时必须升级为
`riftx.audit-create-draft-request/v2`，再绑定稳定的 plan identity/digest；raw opaque token 不进入
canonical payload。v1 只存在于 Feature Flag 默认关闭的 M1，不形成对外兼容承诺。

## 4. typed raw binding 与 authorization-before-contract-load

### 4.1 原始绑定只包含 owner columns

`GET /audits/{id}` 和由 `run_id` 反查 Audit 的 generic read 先读取
`AuditAuthorizationBinding`。该对象只包含以下 ID、kind 和 digest owner columns：

| 来源 | 绑定事实 |
| --- | --- |
| request path | requested Audit ID |
| AuditScan | Audit、Run、Project、Engagement、Contract ID 与 Contract digest |
| Run | Run ID、Engagement ID、RunKind |
| AuditProject / Engagement | Project ID、实际 Engagement owner root |
| AuditContractRecord | Contract ID、Audit ID、Contract digest |
| AuditClientRequest | Audit、Run、Project、Engagement、Contract ID 与 Contract digest |

该查询不得选择 `canonical_contract_json`、source path、workspace、request payload、Prompt、
模型内容或 storage locator。Outer join 是为了让缺失 owner 事实进入一个可拒绝的 typed binding，
而不是在授权前用不同数据库错误暴露损坏位置。

### 4.2 授权不变量

`AuditObjectAuthorizer.require_audit_binding` 必须在加载完整 aggregate 前证明：

1. authenticated principal 具备所需 READ/WRITE capability；
2. requested Audit ID 与 Scan Audit ID 相同；
3. Scan↔Run↔Project↔Engagement 全部属于同一 owner root；
4. RunKind 恰为 `code_audit`；
5. Scan↔Contract 的 ID、Audit owner 和 digest 一致；
6. immutable client-request 的 Audit/Run/Project/Engagement/Contract/digest 绑定与 owner graph
   完全一致；
7. 任何 null、foreign ID、digest mismatch、重复/ambiguous binding 或未知 kind 均 fail closed。

ID 与 digest 比较使用 constant-time primitive。调用方提供相同字符串、数据库 FK 存在或
Scan 的冗余 `engagement_id` 单独相等都不构成完整授权证明。

### 4.3 一致读取与二次复验

Repository 的顺序固定为：

~~~text
open one consistent-read session
  -> read raw authorization binding
  -> invoke typed authorizer
  -> load strict complete aggregate in the same session
  -> compare every loaded owner fact with the authorized raw binding
  -> validate Audit↔Run state projection
  -> return positive allowlist response
~~~

不得在授权前 parse canonical Contract；不得在两个独立 session 中完成授权和加载；不得在 raw
binding 改变、消失或完整 aggregate 映射失败时继续返回部分对象。无法安全分类的持久化或完整性
失败统一转为脱敏 `503 audit_persistence_unavailable`。

## 5. scope-before-pagination

Audit list 的授权不是 response filter。Application Service 先从 authorizer 取得
`AuditEngagementScope`，Repository 再把该 scope 与 caller filters 求交并写入 SQL：

~~~text
authorized Project.engagement_id predicate
AND optional run/project/engagement/status/mode/created-range filters
ORDER BY AuditScan.created_at DESC, AuditScan.id DESC
LIMIT bounded_limit OFFSET bounded_offset
~~~

规则如下：

- 受限 scope 必须依据实际 `AuditProject.engagement_id` 过滤，不能用可能损坏的 Scan 冗余 owner
  做 `OR` 扩张；
- 空授权集合直接返回空页，绝不解释为 unrestricted；
- caller 的 `engagement_id`、`project_id` 或 `run_id` 只是收窄 filter，不能扩大授权 scope；
- 授权 predicate 位于 ordering/pagination 之前，未授权对象不影响页大小、offset 或排序；
- AUD-104 page size 默认 50、最大 200，offset 非负；时间必须 timezone-aware 且 from ≤ to；
- 选中 ID 后在同一 consistent-read session 批量加载完整 aggregate；页内任一损坏对象使整页
  fail closed，不能静默过滤；
- 最终 signed cursor 与 snapshot-version 语义仍属于 AUD-600，本任务不伪装 offset 为最终 cursor。

`/runs?kind=code_audit` 在 M1 复用同一授权页契约，limit 同样最大 200。不得为了满足更大 limit
循环打开多个 consistent-read session 再拼成一个 HTTP page；并发插入会使这种拼页重复或遗漏。
超限请求在 AUD-600 signed cursor 落地前直接 validation fail。

## 6. 安全投影与 generic Run 读取

### 6.1 AuditResponse 是 positive allowlist

`AuditResponse` 只投影经过完整验证的 aggregate，并固定为下列类别：

- Audit/Run ID、Audit `state_version`；
- 有界 Project summary：ID、Engagement ID、display name、VCS kind、default branch；
- Snapshot/base/baseline/parent ID（draft 时允许 null）；
- purpose、mode、analysis profile、lifecycle/current phase、terminal/closure/publication status；
- distribution revision ID；
- model profile 名称、Run status 与 lifecycle timestamps。

响应中禁止出现：

~~~text
repository_path / repository_identity_digest
authorization_reference
canonical_contract_json / Contract or policy document
contract_digest / request_digest
workspace_path / storage locator
temporal_workflow_id
preflight token or proof
source code / model output / credential
~~~

Audit response model必须 `extra=forbid` 且 frozen。后续任务若要增加字段，必须显式审阅其 access
class；不得使用 `model_validate(aggregate)` 自动扩张 wire surface。

这是权威规格第 16.4 节 GA projection 的 M1 子集。Snapshot object/status 由 AUD-205，core seal
与 distribution revision 详情由 AUD-307/AUD-506，progress/budget/usage/pending approval、完整
execution/model-egress summary 由 AUD-600（依赖相应后端任务）引入。AUD-104 不返回占位成功值，
也不能把本子集误称为最终 3.0 最低字段。

### 6.2 Code Audit Run 的安全兼容投影

通用 Run API 保持 general Run 的历史 response 不变，但 `code_audit` 使用 discriminated
`CodeAuditRunResponse`。后者只包含安全的 Run identity、Engagement/Node、目标摘要、criteria、
entry points、Scope、kind/status/approval/model profile 和 timestamps；明确排除
`workspace_path` 与 `temporal_workflow_id`。

Generic Run read 必须遵守：

- `/runs` 默认仍只查询 `general`；
- `/runs?kind=code_audit` 从经过授权的 Audit aggregate 投影，不能直接 list raw Run rows；
- `/runs/{run_id}` 解析到 `code_audit` 后必须以 `get_by_run_authorized` 回到 Audit ACL root；
- 没有关联有效 Audit aggregate 的 bare/orphan Code Audit Run 不得出现在 list，也不得通过 detail
  返回；
- Event、Execution、Finding、Artifact、Report、Approval、Connector 等接受或间接解析
  `run_id` 的 generic read，必须先经过同一个 Audit root authorizer；child-ID read 必须先做
  bounded child→run owner resolution，再授权 Run，之后才可决定读取是否属于 M1 allowlist；
- access-class-sensitive Artifact 内容仍由 AUD-105 继续收紧；Audit root authorization 不能替代
  `restricted_sensitive` 的显式下载契约。

所有 child-ID read 的不可交换顺序为：

~~~text
bounded child owner columns
  -> resolve RunKind
  -> Audit raw binding / object authorization
  -> full child or content/Runner IO
  -> exact child ID + immutable owner Run/Audit revalidation
  -> positive allowlist projection
~~~

bounded resolver 成功后 full getter 消失，和 resolver missing/authorizer denied 一样统一为固定
`404 resource_not_accessible`；不能退化成带实体名/ID 的 legacy not-found。授权拒绝或 owner
mismatch 前，Finding Evidence、Artifact file、Runner output、Browser observation、Context
Manifest 和 Memory content 的 getter/open/hash 调用必须为零。

Code Audit Execution detail/list 不能复用 legacy 全量 `ExecutionResponse`。M1 allowlist 只允许
`kind/id/run_id/node_id/executor_type/tool_id/tool_version/status/exit_code/started_at/finished_at/
physical_stop_confirmed_at`；`argv`、`command_text`、executable/cwd、`env_diff`、stdout/stderr
path、PID/process group、containment ID 和 host platform fingerprint 一律禁止。Artifact metadata
继续排除 storage path，restricted content 由 AUD-105 处理。

Audit root authorization 只是必要条件，不是读取许可。M1 generic Code Audit read 的完整白名单
只有 Run、Event、Execution 与 Artifact。Finding、Report、Approval、Action、Graph、Run metrics、
Target HTTP/Traffic、Terminal、Browser、Context、Memory 与 Connector facade 均在授权后、任何
full object/content getter 之前返回 `409 run_kind_operation_unsupported`。全局 Node、Tool、Model
profile 与 Security Profile 不属于 Run-scoped read。AUD-106 的 operation catalog 逐项决定是否
保持拒绝或增加专用安全投影。任何 WebSocket/SSE/stream 若后续开放，每批读取都要复验握手时冻结
的 child/run ownership，不能只授权连接建立。

被 Policy 标为 `READ_ONLY` 的 route 不得偷偷追加 Event 或写投影。例如
`wait_execution` 可以等待/读取当前 Execution 与 bounded output，但不能写
`execution.wait_completed` Event。只读行为若未来需要持久 receipt，必须先更改 RouteEffect 和
授权契约。

## 7. 统一失败语义

公开错误继续使用现有 `ErrorResponse` envelope。AUD-104 冻结以下映射：

| 情况 | HTTP / code | 约束 |
| --- | --- | --- |
| 未认证 | `401` / 现有 authentication code | 不访问对象或效果 Service |
| capability 不足 | `403 local_operator_capability_denied` | READ 与 WRITE 分开 |
| Audit 不存在或 raw binding 被对象授权拒绝 | `404 resource_not_accessible` | body 必须不可区分；不得返回 entity ID/owner |
| missing Engagement、跨 owner Project 或创建授权域不一致 | `409 audit_creation_conflict` | 同类冲突使用相同脱敏 body |
| 同 idempotency key、不同 caller payload | `409 audit_idempotency_conflict` | 不回显原 payload/digest |
| Code Audit Run 调用 generic-only mutation | `409 run_kind_operation_unsupported` | 所有该类操作使用相同 code/message |
| Feature Flag 阻止新 admission | `503 feature_disabled` | first create 与 exact replay 相同 |
| aggregate/integrity/persistence 无法安全读取 | `503 audit_persistence_unavailable` | 无 SQL、path、canonical Contract 或 driver cause |
| Audit body Schema/Contract 无效 | `422 validation_error` | 所有 body input 与 string literal 脱敏 |

特别地，`GET /audits/{id}` 的 missing 与 denied 响应必须 byte-identical。创建时“指定的
Engagement 不存在”和“已有 repository identity 属于另一授权根”也必须使用相同
`audit_creation_conflict` body，避免形成 Engagement/Project existence oracle。

Byte identity 必须由 Audit Service/API 边界主动正规化：捕获 typed object-authorization denial
和 Repository missing 后重新构造固定 `_audit_not_accessible()`（或等价 factory），而不是允许
authorizer 的 message/details 原样穿透。测试中的 denying authorizer 必须故意返回不同的 canary
message/details，最终 body 仍与 missing 完全一致。Child owner resolver 后 full getter 消失也使用
同一 factory；授权成功后真正的 integrity/persistence failure 仍按下段返回脱敏 503。

422 的 literal 收集器必须遍历 mapping key 和 value，并处理攻击者可控的 `loc` segment。测试至少
注入顶层、Contract 和任意深层未知字段名 canary；response 的 bytes 中不得出现 key/value、绝对
path、canonical policy、provider disclosure 或原始 `input`。只遍历 mapping value 不满足本 ADR。

409 只表示请求与当前授权/RunKind/幂等事实冲突，不表示 Audit 已启动、已取消或已完成任何
物理效果。404 也不能用来掩盖一个已经确认属于当前 principal、但其完整 aggregate 无法安全
恢复的持久化错误；后一类使用脱敏 503。

## 8. 临时 RunKind 双层效果围栏

### 8.1 为什么 AUD-104 必须提前围栏

权威规格原计划由 `AUD-106` 完整实现 machine-readable `RunKindEffectPolicy`。但 AUD-104 开始
允许 HTTP 查询 `code_audit` Run；若此时继续让其进入通用 Run 或 child-resource mutation，
就会在 Audit Workflow、plan ownership 和 dedicated approval 尚不存在时形成 confused-deputy
路径。因此本任务先安装最小 fail-closed bridge。

该 bridge 不是最终 router。它只有一条普通 admission 规则：

~~~text
generic operation + RunKind != general
  -> 409 run_kind_operation_unsupported
~~~

API route 在调用 effect Service 前检查一次；Application Service 在任何 Event、Artifact、Hook、
Runner、文件、网络或持久 mutation 前再检查一次。外层防止 HTTP 误接线，内层保护 Worker、
测试、插件或未来非 HTTP caller。只做单层检查不满足本 ADR。

### 8.2 临时拒绝面

下列 generic mutation 对 `code_audit` 一律拒绝：

| family | 临时拒绝的普通操作 |
| --- | --- |
| Run | pause、resume、cancel、cancel-current、message、compact、switch-model |
| Terminal | create、write、resize、interrupt、takeover、release、close、WebSocket effect |
| Browser | open、observe、act、takeover、release、public close、stream/observation mutation |
| Target HTTP / Connector | execute、capture；kind check 必须早于 idempotent result/receipt replay |
| Memory | RUN-scope create、update、delete、pin、supersede；current/candidate/old owner 都检查 |
| Finding | generic create/update，以及其 Memory promotion side effect |
| Artifact / Report | path/content register、generic report generation |
| Approval | generic approve/reject/Run grant |
| Execution | Operator generic cancel |
| Runner callback | 普通 execution status 与 output callback |

拒绝必须发生在首个副作用之前。测试需要分别证明没有新 Event、row、Artifact 文件、Hook、
Submission、Runner call、Supervisor/process、网络请求、Workflow signal 或 Memory promotion。
General Run 的历史行为和 response 必须保持兼容。

未来 Audit-owned内部调用不能通过“跳过检查”复用这些 generic 方法。AUD-106 必须为 origin、
Audit ownership、plan digest 和 RouteEffect 建立显式 operation catalog 与专用 alternative。

### 8.3 安全收敛不是普通 mutation

Feature 关闭、Operator Audit cancel 或进程崩溃后仍必须收敛已有资源。因此下列动作不受
“generic operation only supports general”规则阻断：

- `RunSafetyStopService` 对所有 RunKind 的 stop sweep；
- Browser `stop_run` 使用私有 safety-close，而不是 public `close`；
- Target HTTP `stop_run` 围栏并收敛 READY/EXECUTING intent；
- 已认证且 owner 匹配的 affirmative Runner physical-stop proof；
- 后续 Audit-owned cancel/cleanup、Capsule destroy、lease revoke 和 reconciler。

这些例外只允许减少或证明既有效果已经停止，不能创建新 session、命令、Artifact、网络或
Workflow progress。无法获得肯定 stop proof 时必须保持 unconfirmed；不得因 bridge 存在而把
“拒绝 callback”误报为“进程已停止”。

Generic `/runs/{id}/cancel` 对 Code Audit 仍然拒绝。它只有 `WORKFLOW_CONTROL` 权限，不能作为
需要 `HOST_CONTROL` 的 Audit cancel 旁路。真正 Audit cancel 由后续专用端点/router 负责，
内部 safety stopper 则始终可达。

## 9. Runner callback 临时契约与 AUD-106 延后项

### 9.1 当前 callback 顺序

Runner status/output callback 的 admission 顺序固定为：

~~~text
authenticate Runner token + principal tuple
  -> resolve Execution
  -> prove node + RunnerPrincipal owns Execution
  -> resolve Execution.run_id and RunKind
  -> apply temporary RunKind callback fence
  -> mutate status or append output
~~~

Owner mismatch 必须在 RunKind 判断前返回现有 Runner authentication/scope failure，避免 foreign
Runner 利用 Audit callback 探测 Execution。普通 Code Audit status/output callback 返回
`409 run_kind_operation_unsupported`，且不得改变 Execution 或 output bytes。

唯一临时例外是 status 属于 stop-proof terminal set，且
`physical_stop_confirmed=true` 的 affirmative report。它可以让 Execution 收敛并记录物理停止
事实；任意普通 terminal status、仅有 exit code、未确认 cancel 或 output append 都不属于例外。

Worker completion callback 在看到 `code_audit` Run 时不得错误 signal
`riftx-run-{run_id}`。专用 `riftx-code-audit-{audit_id}` 路由在 AUD-106 前不存在，因此当前只
完成本地清理并抑制不安全的 generic signal。

### 9.2 RunnerCommand ownership 明确延后 AUD-106

现有 `RunnerCommand` 尚未携带足以证明 Audit command origin 的不可变 ownership envelope，至少
缺少：

~~~text
run_id
origin / operation family
execution_id（不可变 execution identity；Audit 可命名 audit_execution_id）
audit_id（Audit origin 时）
plan_digest（Audit effect plan 时）
~~~

因此 AUD-104 的 callback fence 只能根据已存在的 Execution→Run 绑定拒绝普通 Audit callback；
它不能证明历史 pending command、dequeue、ACK 或 journal replay 是否由合法 Audit plan 创建，
也不能安全地为 Audit 执行开放 generic completion。

`AUD-106` 必须在解除围栏前：

1. 版本化 RunnerCommand ownership envelope，并以独立 typed column/record 将 command、不可变
   execution identity、Execution、Node、Runner principal、RunKind、Audit 和 plan digest 绑定；
   不能把 execution identity 藏在自由 `payload` 或从 path/target 推断；
2. 在 enqueue、claim/dequeue、poll、lease renew、idempotent replay、status/output/finish/stop ACK
   与 Workflow callback 全链重复校验；
3. 对缺少 ownership 的 legacy/pending/replayed command 建立显式 quarantine/reconciliation，
   不能从 payload 或 path 猜测归属后继续执行；
4. 按 RunKind 把合法 completion 路由到正确 Workflow；
5. 保留 authenticated、owner-checked 的 cancel/physical-stop ACK 收敛能力，不能用 blanket deny
   让历史进程变成无法确认停止的资源。

在上述条件完成前，不得让 Code Audit 创建 Runner execution，也不得把普通 callback 的 409
理解为最终 Audit runner policy。

## 10. Feature Flag：读取和清理始终可达

`audit.enabled=false` 的语义固定为 admission fence：

- `POST /audits` 在调用 creation UoW、查询 client-request 或执行 replay 前返回
  `503 feature_disabled`；first create 与 exact replay 没有例外；
- `GET /audits`、`GET /audits/{id}` 和经过 Audit ACL、M1 allowlist 允许的 generic safe reads 继续
  返回已有对象；
- 关闭开关不得删除、隐藏、降级或伪造已有 Audit 的 lifecycle；
- Service、typed authorizer、Audit aggregate read adapter、Run safety stopper、Runner stop
  callback 和 cleanup/reconciler composition 必须仍然注册；
- 新 Terminal/Browser/HTTP/Connector/Execution、Start、resume 或其他扩大效果的路径保持拒绝；
- 已有非终态 Audit 必须能先围栏新效果，再由 Audit-owned pause/cancel/cleanup 和肯定物理停止
  proof 收敛；Feature Flag 不能解除 resource ownership 或把未确认停止改成只读完成。

AUD-104 尚未公开 Audit pause/cancel endpoint，且 generic Run controls 对 Code Audit 仍被临时
bridge 拒绝。这不是取消能力的最终 UI 契约；它表示公开控制必须等待 AUD-106 的正确
RouteEffect、Audit ownership 和 Workflow router，而内部安全清理从现在起不得被 Feature Flag
移除。

## 11. 任务边界、后果与验收

### 11.1 AUD-104 内

- strict draft request、safe Audit response/list response 与 OpenAPI；
- v1 proof/consent/selection 字段的 synthetic-untrusted 定位，以及所有 v1 draft 不可 Start 的
  明确前置门禁；
- 三项 Audit routes、显式 Policy 和 local principal capability；
- server-derived authorization reference 与 typed Engagement scope；
- raw binding authorization-before-contract-load、scope-before-pagination；
- generic Code Audit Run 的 Audit-rooted read 与 discriminated safe projection；
- child read 的 bounded-owner-first 管线、full-get disappearance 正规化和 Code Audit Execution
  positive allowlist；
- 双层 temporary RunKind effect bridge、Runner callback fence 和 safety cleanup exceptions；
- 401/403/404/409/422/503 的稳定脱敏映射，包括任意 authorizer denial canary 的固定 404，以及
  body key/value/loc canary 的 422 清除；
- real persistence、cross-object、zero-side-effect、general regression 与 OpenAPI inventory tests。

Agent 相关验证必须按仓库规则使用：

~~~shell
conda run --no-capture-output -n agent pytest <AUD-104 test targets>
~~~

### 11.2 明确延后

- Preflight、signed token、source authorization proof：AUD-200/AUD-201；
- restricted Artifact access class、bounded ingest/download：AUD-105；
- machine-readable mutation inventory、Audit Workflow router、包含不可变 execution identity 的
  完整 RunnerCommand ownership、
  Approval/Execution callback alternative：AUD-106；
- Snapshot、Detector、Agent、Finding identity、Report distribution、CLI 和 WebUI：后续各自任务；
- signed cursor：AUD-600。

### 11.3 后果

正向后果：

- HTTP caller 无法选择持久化授权域或 server-generated identity；
- 拒绝对象请求时无需先加载 source path 和 canonical Contract；
- ACL 收窄时 pagination 语义保持正确，未授权对象不会影响页面；
- Code Audit Run 可被安全读取，但在专用效果 router 完成前不能借通用能力执行；
- Feature Flag 关闭后，历史 Audit 仍可调查并能安全收敛资源。

成本与限制：

- AUD-104 wire 是 M1 draft-only contract，不是最终用户创建流；
- 临时 blanket bridge 会拒绝未来合法的 Audit-owned效果，必须由 AUD-106 的 typed origin/policy
  有序替换，不能长期堆叠例外；
- offset pagination 只用于 M1，小规模授权读取；最终大规模 API 仍需 signed cursor；
- Artifact root authorization 尚不等于 restricted content download 完成，AUD-105 是 M1 exit 的
  必要后续任务；
- RunnerCommand ownership 未完成前，Code Audit 不具备 Runner 执行资格。

## 12. 本 ADR 的 provenance 记录

~~~yaml
provenance_id: RXP-AUD-104-001
task_id: AUD-104
artifact_class: architecture_decision
artifact_version: ADR-0004
paths:
  - docs/architecture/decisions/0004-riftx-code-audit-api-authorization-contract.md
  - docs/architecture/decisions/0003-riftx-code-audit-application-contract.md
  - docs/riftx-3-code-audit-development-spec.md
author: Ch1nfo (Git author); Codex task /root/aud104_adr
authored_at: 2026-08-03T09:10:27+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md sections 4.3, 4.4, 13.5, 16, and 20.4"
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-104"
  - "docs/architecture/decisions/0003-riftx-code-audit-application-contract.md"
implementation_inputs:
  - RiftX repository baseline 51fa06cb2d5a213942bd2c46e2873eaceae0461a
  - AUD-103 RiftX-owned Application Service and atomic creation contract
  - existing RiftX FastAPI policy, local-principal, Run, child-resource, Runner, and safety-stop contracts
  - current AUD-104 implementation diff and synthetic RiftX tests
public_standard_versions:
  - SHA-256 (FIPS PUB 180-4)
  - JSON (RFC 8259; RiftX wire and canonical encodings are defined by RiftX contracts)
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task /root (final implementation/security review pending)
review_sources:
  - this ADR
  - authoritative specification sections listed above
  - AUD-104 API, authorization, RunKind bridge, Runner callback, Feature Flag, and regression diff/tests
review_result: pending
commit: pending_backfill
notes: AUD-104 exposes draft-only persistence and safe reads. The M1 synthetic-field, principal-scoped request identity, GA/M1 projection, opaque-error, and child-read clarifications are part of the same acceptance contract. RunnerCommand ownership and the final machine-readable RunKind effect/router contract remain explicitly blocked on AUD-106.
~~~
