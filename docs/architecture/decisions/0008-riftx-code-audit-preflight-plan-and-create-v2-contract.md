# ADR-0008：RiftX Code Audit Preflight Plan、Create v2 与启动准入契约

> 状态：Accepted
>
> 实施状态：Plan/token、issuance API 与 Create v2 已实现；Start admission pending
>
> 日期：2026-08-04（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-201`
>
> 产品基线：`3ee0cbf9`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md` 第 8.2、10.3.1、13.2–13.3、
> 14.1、15.1–15.2、16.2–16.3、20.3、22/M2/AUD-201、AUD-208、AUD-209 节
>
> 前置决策：ADR-0001 至 ADR-0007
>
> 决策所有者：RiftX contributors；准确作者、审阅者、测试证据和 Commit 由本文第 17 节的
> provenance 记录保存

## 1. 背景与结论

ADR-0007/AUD-200 已经能够在独立的 `AuditPreflightJob` owner 下读取授权仓库的 Git 元数据，并
生成不可变 `AuditPreflightResult/v1`。Result 证明“一次 Preflight 在什么 owner、Node、backend、
image、policy 和 target 下得到了什么有界结果”，但它不是 bearer credential，也没有绑定一个
Audit。另一方面，ADR-0003/ADR-0004 的 M1 `CreateAuditDraftRequest/v1` 为验证原子持久化而暂时
接受 full-contract-shaped assertion；其中 proof、selection 和 consent 全部是 synthetic、untrusted
测试事实，不能成为 3.0 的执行授权。

若直接把 Result ID 当作 token、把 M1 draft 原地补成可执行对象，或在创建 Audit 后再异步补写
Preflight/Context binding，将产生 token steal/replay、跨 principal 接管、TOCTOU、半聚合、伪造
Contract proof 和错误启动等问题。AUD-201 因此冻结 Result 与可创建 Audit 之间的唯一桥梁：
短期、持久、可预留/消费的 `AuditPreflightPlan`，以及只接受 caller preference 和 opaque token 的
Create v2。

本 ADR 的核心结论如下：

1. `POST /api/v1/audits/preflight/{job_id}/plan` 只在调用方完成显式授权后，从同一 owner 下
   `succeeded`、未过期且完整可重算的 Result 签发 Plan。它不读取 Git、不创建 Audit、Run、
   Snapshot、StartIntent 或 Workflow。
2. token 使用 32-byte CSPRNG nonce 和 Control Plane 独立 HMAC-SHA-256 key。数据库只保存
   `token_version/key_id/nonce/token_hash`，不保存 bearer bytes；raw token 只允许出现在 Plan
   issuance 响应，并强制 `Cache-Control: no-store`。
3. Plan 生命周期冻结为 `ready -> reserved -> consumed`，并允许
   `ready|reserved -> stale|revoked|expired`。`now >= expires_at` 是不可绕过的 admission fence；
   所有状态变更使用 `state_version` CAS，任何终态 token 永不释放给另一个 Audit。
4. `riftx.audit-create-draft-request/v2` 只接受 caller-owned preference 与 opaque token。proof、
   selection、consent timestamp、source target、Scope、Context Bundle 或其他 server-owned 字段在
   body 中出现即拒绝，不能采用“忽略客户端值、随后覆盖”的宽松实现。
5. Create v2 在一个 serialized database transaction 中完成 Plan reservation、Project/Engagement、
   Code Audit Run/Event、`riftx.audit-contract/v2`、`riftx.model-data-egress/v2`、AuditScan、最小
   `AuditSecurityContextBinding` 和 `audit_client_requests/v2`；任一步失败全部回滚。
6. Create v2 的结果是诚实的 `preflight_bound_draft`：它证明 draft 绑定了可信 Preflight，但没有
   Snapshot、CAS、Manifest、StartIntent、Temporal、Runner enqueue 或执行许可，不得投影为
   `start_ready`。
7. AUD-201 只允许固定的 canonical-empty Security Context ID/digest，并创建最小 insert-only
   Binding root；完整 ContextInput、Bundle、entry、parser 和非空上下文由 AUD-209 独占。
8. Start admission 拥有 same-node revalidation、Plan consume、Audit/Run queued projection 和 pending
   `AuditStartIntent` 的原子准入决策；AUD-208 只负责 intent claim/lease/dispatch/retry/reconcile，
   不能替代或放宽 AUD-201 的 source/Plan/Contract admission。
9. M1/v1 Contract、client-request parser 和历史读取继续保留，但 v1 proof 永远是 synthetic/
   untrusted。历史 v1 draft 不可 Start、不可原地升级、不可补 token/Plan；Operator 必须重新
   Preflight 并创建新的 v2 Audit。
10. AUD-200 的 `content_identity_digest` 只是短期 stale/revalidation fingerprint，不是 Snapshot、
    CAS object、tree 或 Manifest digest。真正的 byte SHA-256、mode 与 Manifest 必须由 AUD-202
    重新读取后计算。

本实现不使用 Codex Security Provider、代码、Prompt、Schema、Skill、运行时、依赖、端点、测试或
生成物。RiftX 可以学习通用的“先预检、再冻结、后执行”和“证据可追溯”思想，但所有 Domain、API、
Persistence、Token、Workflow 和测试合同均为 RiftX 自有设计与实现。由于团队已经了解过公开项目的
一般思路，本文不宣称法律或流程意义上的 strict clean-room；对外应使用“RiftX 独立实现”表述。

## 2. 信任链与分阶段边界

### 2.1 唯一允许的主链

~~~text
AuditPreflightJob(succeeded)
  + immutable AuditPreflightResult/v1
  + authenticated principal / authorization domain
        |
        | POST .../preflight/{job_id}/plan
        v
AuditPreflightPlan/v1 (ready, short-lived)
        |
        | POST /audits with CreateDraftRequest/v2
        | same transaction: reserve + create full draft aggregate
        v
Audit(preflight_bound_draft) + Plan(reserved)
        |
        | later Start admission: same-node revalidation + reviewed contract
        | same transaction: consume + queued + pending StartIntent
        v
Audit(queued) + Plan(consumed) + AuditStartIntent(pending)
        |
        | AUD-208 only
        v
intent claim / Temporal dispatch / retry / reconcile
~~~

不允许跳过任何中间事实，也不允许使用 Result ID、Result digest、Contract digest、Context digest、
CapabilityMatrix digest 或 repository identity 直接替代 token。Plan 是短期创建/启动准入事实，不是
通用 Runner capability、Agent tool credential 或 Snapshot reader authorization。

### 2.2 字段所有权

| 所有者 | 可以决定的事实 | 明确不能决定的事实 |
| --- | --- | --- |
| Caller | request ID、项目显示名、允许的 Engagement、mode/profile、model/egress、validation、baseline、budget 和受约束 execution preference | proof、实际 Node/backend/image/policy、repository/content identity、Scope、Context Bundle、Contract/Run/Audit ID |
| Auth/Trust Profile | principal、authorization domain、Engagement scope、create capability | token、source proof、用户偏好 |
| AUD-200 Result/Plan | source Node/root/repository/content identity、target、Scope、SourceIngest backend/image/policy/prepare proof、capability/budget envelope、empty-context identity | Audit ID、StartIntent、Snapshot/Manifest、Detector 或模型执行结果 |
| Capability Registry/current policy | 当前仍允许的能力、上限和更严格 policy | 覆盖已冻结的 Plan identity，或把 unavailable 伪造成 proof |
| Server consent ledger | 有效 consent event 及其时间、principal、policy binding | 信任 caller 提交的 consent timestamp |
| Create/Start UoW | server-owned ID、reservation/consume、Contract、状态、Event、Intent | Git/Snapshot/Temporal 等事务外效果 |

## 3. Plan issuance 契约

### 3.1 路由与效果

新增唯一 issuance route：

~~~text
POST /api/v1/audits/preflight/{job_id}/plan
RouteEffect = DURABLE_WRITE
Local Operator capability = WRITE
~~~

该 route 必须登记到 API Policy、`RunKindEffectPolicy`/entrypoint inventory 和 OpenAPI。它不是
`HOST_EXECUTION`：handler 不得读取 Git、打开 repository path、联系 Runner 或创建 Capsule；也不是
`HOST_CONTROL` 或 `WORKFLOW_CONTROL`。

处理顺序固定为：

~~~text
authentication
  -> operator capability
  -> audit.enabled fence
  -> bounded Job owner resolution
  -> exact principal + authorization scope proof
  -> succeeded/result/request/owner digest reconstruction
  -> expiry/blocking/capability admission
  -> token key availability
  -> Plan insert or exact issuance replay
  -> no-store response
~~~

在 owner 授权前不得读取 `restricted_request_json`、repository path 或完整 Result JSON。missing、跨
principal、跨 authorization scope 和 denied 使用统一 `resource_not_accessible`，不能泄漏 Job 是否
存在或是否成功。

### 3.2 可签发条件

首次签发必须同时证明：

- Job status 精确为 `succeeded`，不存在 running/outcome-unknown/cancelled 等兼容分支；
- Job、restricted request、Result canonical bytes、schema version、request/result/effect-owner digest
  可以完整重算且逐字段相等；
- Job/Result owner、principal、authorization scope、source Node/root、backend/image/policy/prepare
  proof、target、mode、Scope 与 empty-context binding 全等；
- Result 没有 blocking error，并且所需 capability/budget facts 结构完整；
- `now < min(job.expires_at, result.expires_at)`；Plan 的 `expires_at` 不得晚于该 owner expiry；
- active token key 存在、key ID 合法且解码后的 key 至少 32 bytes。

任一验证失败均不能创建半 Plan、nonce 或 token hash。Plan issuance 不修改 Job/Result，也不能把
Result 的 expiry 延长为 Plan expiry。

默认 `plan_ttl_seconds=900`，实际 `expires_at` 取
`min(now + configured_ttl, job.expires_at, result.expires_at)`；配置必须有安全上下限，不能用零、负数或
无界 TTL。`max_outstanding_plans_per_principal` 默认 16，并在 issuance transaction 内按同一
principal/authorization domain 的 `ready|reserved` Plan 串行化计数。exact issuance replay 不重复占用
quota；超限返回稳定 safe code，不得通过并发请求绕过，也不得为了腾 quota 自动 revoke 旧 Plan。

### 3.3 幂等与历史 Job

`audit_preflight_plans.preflight_job_id` 唯一。相同 Job、Result digest、owner 和 Plan identity：

- 首次创建返回 HTTP 201，`created=true/replayed=false`；
- 已有 `ready` Plan 且全部 immutable/verifier facts 相等时，可由持久 nonce 与对应 key 重新派生同一
  raw token，返回 HTTP 200，`created=false/replayed=true`；
- 已有 Plan 但 identity/verifier 不一致时 fail closed，不能生成第二个 Plan；
- Plan 已 `reserved/consumed/stale/revoked/expired` 时不得再次回显 raw token，也不得创建替代 Plan。

AUD-201 migration 不为任何历史 AUD-200 `succeeded` Job 自动补 Plan。升级后的显式 issuance 请求只在
该历史 Job 仍未过期且满足本节全部新校验时才可创建 Plan；否则 Operator 必须重新 Preflight。

### 3.4 issuance 响应

安全响应最小 shape 为：

~~~json
{
  "created": true,
  "replayed": false,
  "plan": {
    "id": "plan-id",
    "digest": "64-lower-hex",
    "status": "ready",
    "preflight_job_id": "job-id",
    "expires_at": "2026-08-04T12:00:00Z"
  },
  "preflight_token": "opaque-bearer-token"
}
~~~

该响应必须设置 `Cache-Control: no-store`，并建议同时设置 `Pragma: no-cache`。raw token 不出现在
GET/list/status、错误 body、Event、Artifact、trace、audit log、access log、exception repr 或 metrics
label。Plan projection 不返回 nonce、token hash、key ID、restricted path 或 canonical Plan JSON。

## 4. Plan identity、持久字段与状态机

### 4.1 不可变 identity

`AuditPreflightPlan/v1` 的 immutable identity 至少包含：

~~~text
schema_version = riftx.audit-preflight-plan/v1
plan_id / plan_digest
preflight_job_id / preflight_client_request_id
operator_principal_id / authorization_scope_digest
request_schema_version / request_digest
result_schema_version / result_digest / effect_owner_digest
source_node_id / source_root_identity_digest
repository_identity_digest / content_identity_digest
backend_id / image_digest / policy_digest / capsule_prepare_proof_digest
target_schema_version / canonical target / target_digest
scope_schema_version / canonical sorted include/exclude / scope_digest
capability_matrix_schema_version / capability_matrix_digest
minimum_feasible_budget_schema_version / minimum_feasible_budget_digest
security_context_id / security_context_digest
preflight_completed_at / created_at / expires_at
~~~

`plan_digest` 使用 `riftx.audit-preflight-plan/v1` domain-separated SHA-256，对稳定 canonical identity
计算；它排除 token verifier、status、reservation/consumption、`state_version` 和 `updated_at`。target、
Scope、CapabilityMatrix、budget 与 Security Context 均先使用自己的 schema/domain digest，再进入
Plan identity，禁止跨对象类型复用裸 JSON digest。

repository path 如为恢复所必需，只能存在于受限 canonical Plan/target 列，不进入普通 projection、
Event 或日志。所有冗余查询列必须与 canonical identity 重新解析结果恒等；任一缺失、未知字段、
duplicate key、非 canonical encoding 或 digest 漂移都 fail closed。

### 4.2 token verifier 与生命周期字段

Plan row 另行保存：

~~~text
token_version / key_id / nonce / token_hash
status / state_version
reserved_audit_id / reserved_client_request_id / reserved_request_digest / reserved_at
consumed_audit_id / consumed_start_request_id / consumed_at
terminal_reason_code / stale_at / revoked_at / expired_at
updated_at
~~~

数据库不得保存 raw token。`reserved_*`、`consumed_*` 和 terminal facts 必须使用 all-or-none CHECK；
Plan/ClientRequest/Audit 可建立的 FK 使用 `ON DELETE RESTRICT`。`token_hash`、`preflight_job_id`、
`reserved_audit_id` 和 `consumed_audit_id` 使用唯一约束或等价复合约束，并由 mapper 再次证明 owner。

### 4.3 状态与转换

稳定状态闭集为：

~~~text
ready -> reserved -> consumed
  |          |
  +----------+----> stale
  +----------+----> revoked
  +----------+----> expired
~~~

- `ready`：Plan 已签发，尚未绑定 Audit；
- `reserved`：Create v2 已提交，永久绑定 exact Audit、client request 和 request digest；
- `consumed`：Start admission 已提交 queued state 与 pending StartIntent；
- `stale`：source/content/owner/capability/context revalidation 与冻结事实不再相等；
- `revoked`：授权、policy、key compromise、人工安全动作或完整性事件使 Plan 永久不可用；
- `expired`：expiry reconciler 的持久投影；即使尚未投影，时间判断仍然生效。

`consumed/stale/revoked/expired` 是终态。`ready` 或 `reserved` 在
`now >= expires_at` 时不得 reserve/consume；所有入口必须直接检查时间，不能依赖周期任务先写入
`expired`。每次转换以 `id + expected_state_version + expected_status` 做 CAS，并原子增加
`state_version`；timestamp、status 单独使用或进程内 lock 均不能替代 CAS。

一旦 reservation 提交，Plan 永不回到 `ready`，即使 Audit 从未 Start、Create 响应丢失、后续
policy 收紧、token 过期或 draft 被取消。事务整体 rollback 时 reservation 从未成为持久事实，Plan
自然保持 `ready`；不得另外实现“释放 token”补偿接口。

### 4.4 竞态与重放

- 同一 token 的两个 Create 首次请求竞争时只有一个 reservation CAS 可以成功；失败方返回稳定、
  脱敏 conflict，不能得知胜出 Audit。
- 同一 `client_request_id` 的 exact v2 retry 必须返回同一 Audit 当前 lifecycle/version，不追加
  Event、不更新 Plan、不把 Audit 写回 draft。
- exact retry 可以在 Plan 已 reserved、consumed 或随后 terminal 时读取已存在 aggregate，因为它不
  是新的 admission；前提是 client-request、request digest、Plan/Audit binding 和完整 aggregate
  全部恒等。该分支绝不能创建新对象或重新消费 token。
- Plan 显示 `reserved` 但 client-request/aggregate 缺失，或 client-request 指向 Audit 但 Plan 仍
  `ready`/绑定不同对象，均视为持久完整性错误；不能“补写缺失行”恢复。
- expiry、revoke、stale 与 reservation/consume 竞争时，以第一个成功 CAS 为准；失败方重新读取后
  只能返回对应终态，不得根据旧内存对象继续。

## 5. Opaque token 与 key lifecycle

### 5.1 Token codec

token v1 固定使用：

~~~text
nonce_bytes = CSPRNG(32 bytes)
nonce = base64url_no_padding(nonce_bytes)
mac = HMAC-SHA256(
  active_control_plane_key,
  UTF8("riftx.audit-preflight-token/v1") || 0x00 ||
  canonical_utf8({key_id, plan_id, plan_digest, nonce})
)
raw_token = base64url_no_padding(nonce_bytes || mac)
token_hash = SHA256(
  UTF8("riftx.audit-preflight-token-hash/v1") || 0x00 || UTF8(raw_token)
)
~~~

具体 wire 是实现私有、固定上限的 opaque bearer token；客户端不得解析或依赖内部布局。它不是 JWT，
不含可由客户端修改的 JSON claim/proof，也不能把 nonce、Plan ID、Result digest 或 token hash 单独当作
credential。

新的 reserve/consume admission 验证顺序必须是 bounded canonical token parse、domain-separated token
hash lookup、constant-time hash 比较、按 key ID 选择 verifier key、重算 HMAC、重算 Plan digest、验证
authenticated owner、Plan state 与 expiry。对已经提交 client-request 的纯读取 exact replay，可以在
旧 key 按轮换规则退役后使用 constant-time token-hash match 加完整 client-request/Plan/Audit binding
证明；该分支不得产生任何新写入或效果。公开错误不区分“格式错误、hash 未命中、MAC 错误、key 不
存在、跨 principal、已被其他 Audit 使用”，避免形成 token/Plan oracle。

### 5.2 Key 来源与失效策略

- active key 来自 Control Plane secret provider，或
  `RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY`；解码后至少 32 bytes；
- `RIFTX_AUDIT_PREFLIGHT_TOKEN_KEY_ID` 是有界非秘密标识，不是 key 或 credential；
- 缺 key、key 太短、key ID 重复/未知、secret provider 故障时 issuance 与新的 reserve/consume
  admission fail closed；禁止固定开发 key、无 key SHA-256、Result digest 或配置 digest fallback；
- key 只存在于 Control Plane token service/keyring。不得注入 API child、Runner、Worker、
  SourceIngest、Content Sandbox、Agent、Detector 或 Temporal payload；
- 敏感 key bytes 不进入 YAML、普通 config dump、日志、trace、Event、Artifact、crash message 或
  exception chain。

rotation 使用“一个 active issuance key + 多个 verify-only retained keys”。旧 key 至少保留到该 key
签发的所有 Plan 都已 `consumed/expired/stale/revoked`，且不存在 `ready/reserved` Plan；在此之前删除
旧 key 会破坏幂等 token re-derivation 或 admission，必须由启动检查拒绝。key compromise 触发按 key ID
批量 CAS revoke 尚未消费的 Plan，但不能删除或改写已经创建的 Audit/Contract 历史事实。

## 6. Create v2 wire 与请求摘要

### 6.1 唯一生产创建 schema

`POST /api/v1/audits` 在 AUD-201 后的新建路径只接受
`riftx.audit-create-draft-request/v2`。典型 wire 为：

~~~json
{
  "client_request_id": "uuid",
  "preflight_token": "opaque-bearer-token",
  "project_name": "RiftX",
  "engagement_id": null,
  "mode": "standard",
  "analysis_profile": "deterministic",
  "model_profile": null,
  "model_data_egress": {
    "mode": "local_only"
  },
  "validation_policy": "static_only",
  "baseline_audit_id": null,
  "execution_target": {
    "node_id": "local",
    "required_sandbox_backend": "linux_container"
  },
  "budget": {
    "max_wall_seconds": 7200,
    "max_model_calls": 0,
    "max_input_tokens": 0,
    "max_output_tokens": 0,
    "max_worker_jobs": 64,
    "max_epochs": 8,
    "max_candidates": 1000
  }
}
~~~

示例值不是扩大能力的承诺。每个 preference 必须与 Plan 已冻结事实相等，或落在 Plan capability/
minimum-feasible-budget envelope 与当前更严格服务端 policy 内；超出范围返回
`audit_preflight_plan_mismatch`、`audit_budget_infeasible` 或
`audit_contract_review_required`，不能静默缩小/替换 caller 选择。

v2 的每层 object 都使用 `extra=forbid`、strict bounded type、canonical UUID/digest/path enum 和
`hide_input_in_errors`。以下字段在 body 任意层出现都必须拒绝：

~~~text
contract / contract_digest / canonical_contract_json
authorization_reference / operator_consent_at / consent_event_id
audit_id / run_id / project_id / contract_id / workflow_id / workspace_path
repository_path / repository_identity_digest / content_identity_digest
source_target / include_paths / exclude_paths / scope / capture_policy
source_prepare_proof / analysis_prepare_proof / proof_digest
selected_node / selected_backend / image_digest / policy_digest / candidates_digest
security_context_bundle_id / security_context_bundle_digest / context entries
snapshot_id / manifest_digest / cas_handle / start / auto_start
request_digest / plan_digest / token_hash / nonce / key_id
~~~

不能先接受这些字段再覆盖为服务端值；这种实现会使 wire ownership、错误回显和未来兼容性不再可
判定。422 继续使用 ADR-0004 的全局 canary redaction：body value、未知 key、动态 `loc` segment 和
Pydantic input 均不得回显，token/path/proof 必须显示为 `[redacted]`。

### 6.2 v2 request digest

`client_request_id` 是幂等 key，不进入自身 identity；raw token 只用于定位并证明 Plan，也不进入摘要。
服务端在成功解析并验证 Plan 后计算：

~~~text
request_schema_version = "riftx.audit-create-draft-request/v2"

canonical_request_identity = {
  authorization_domain_digest,
  preflight_plan_id,
  preflight_plan_digest,
  security_context_id,
  security_context_digest,
  caller_preferences_without_client_request_id_and_token
}

request_digest = SHA256(
  UTF8(request_schema_version)
  || 0x00
  || canonical_json_utf8(canonical_request_identity)
)
~~~

摘要排除 server-generated Audit/Run/Project/Contract/Event/Intent ID、时间戳、state version、raw token、
token hash 与 key ID。任何会改变 Project/Engagement、Contract、ModelDataEgress、Scan 或 execution
preference 的 caller 字段都必须进入摘要；字段增删的 property test 必须证明遗漏会失败。

相同 `client_request_id + schema + digest` 是 exact replay；相同 key 但 schema、authorization domain、
Plan、empty-context 或 preference 任一不同，均为 `audit_idempotency_conflict`。digest 比较使用
constant-time primitive。

## 7. Contract v2 与诚实 draft

### 7.1 权威构造源

Create v2 factory 只能从以下来源构造 `riftx.audit-contract/v2`：

- authenticated principal/authorization domain 与已授权 Engagement scope；
- verified `AuditPreflightPlan/v1` 的 source、target、Scope、same-node、backend/image/policy/proof、
  capability、minimum budget 与 empty-context facts；
- caller preference，但只能作为 Plan/current-policy envelope 内的选择；
- current Capability Registry/NodeAuditPolicy 的“仍然允许”判断；
- server-owned consent ledger 中已经存在且 exact-bound 的 consent event；
- 服务端 ID、clock 和 deterministic selection policy。

Contract 必须保存 `preflight_plan_id/preflight_plan_digest` 或语义等价的 authoritative provenance，
并从 durable Plan 逐项冻结 plan/job/request/result/effect-owner/authorization/source identity 的全链
schema 与 digest；不能重新读取当前配置来补 proof。canonical Contract、冗余查询列、AuditScan、Run、
Plan reservation 和 Security Context Binding 必须逐项恒等。source/analysis node 在 3.0 为 same-node；
Run `node_id` 来自 Plan/服务端选择，不来自 body 同名值。

Plan capability snapshot 必须原样、完整地进入 Contract：entry set、排序、status、component version/
digest、proof 或 unavailable reason 任一额外、缺失或漂移都 fail closed。`available` capability 必须有
完整实现/proof；`unavailable|blocking` capability 不得携带占位实现 proof。Create 不能把“同一 Node”
或“配置中声明支持”推定为已经 prepare。

### 7.2 ModelDataEgress v2

权威 Contract 必须使用 `riftx.model-data-egress/v2`，并绑定同一 Security Context ID/digest、模型
locality、provider/origin disclosure、retention/training policy、redaction/byte/token 上限和 consent
provenance。AUD-201 不执行模型调用；不存在有效 server-side consent 时，caller 提交 remote preference
只能得到 review/capability error，不能用 caller timestamp 伪造 consent。

ModelDataEgress v1 不能嵌入 Contract v2；v2 policy 使用独立 domain digest，任一字段变化必须改变
Contract/request review identity。token、source path、secret 或 provider credential 不进入 policy。

### 7.3 `preflight_bound_draft`

AUD-201 创建的 Contract/Audit 投影必须显式表达：

~~~text
contract_stage = preflight_bound_draft
preflight_binding = authoritative
start_eligible = false
snapshot_id = null
snapshot_status = not_created
run_status = created
audit_lifecycle_status = draft
~~~

对尚未完成的 analysis backend、SnapshotStore、materializer、mount、Scope ledger、Detector registry、
start delivery 等能力，只能保存 typed `unavailable/blocked` 或 requirement，不能填充占位 proof digest、
伪造 candidate digest 或声称已经 prepare。Contract 中任何非空 proof 都必须能追溯到 AUD-200 Result、
Plan、Capability Registry 或 server consent ledger；AUD-202 及以后才产生的 proof 在 AUD-201 Contract
中必须不存在。

Create 成功不表示 Start 已授权，也不表示源码 bytes 已冻结。UI/API 可以显示“Preflight 已绑定，等待
Snapshot/Start 能力”，不能显示“扫描已排队”“源码已快照”或“可以安全执行”。

## 8. Create v2 单事务契约

### 8.1 唯一写边界

ADR-0003 的 `AuditCreationUnitOfWork` 继续是唯一 create 写边界。首次 v2 create 在一个 serialized
transaction 中执行：

~~~text
0. transaction 外：authentication/capability/audit.enabled/strict wire fence

AuditCreationUnitOfWork / one serialized transaction
1. 由 bounded token hash 定位 Plan，验证 token/owner/immutable digest，读取当前 lifecycle/expiry
2. 从 Plan + authorization domain + caller preference 计算 request_digest
3. 按 client_request_id 检查 v1/v2 exact replay、conflict 或 corrupt binding；exact replay 直接返回
4. 新 admission 强制 Plan status=ready 且 now < expires_at，并复验 active/retained key
5. 生成 server-owned Audit/Run/Contract/Project candidate ID
6. CAS: Plan ready -> reserved，写 exact audit/client_request/request_digest binding
7. 解析或创建同授权域的 Engagement / Project
8. 构造 Contract v2 + ModelDataEgress v2 + AuditScan + minimal Context Binding
9. 创建 Run(kind=code_audit, status=created)
10. 创建 RunEvent(sequence=1, event_type=run.created)
11. session-bound primitive 创建 Contract v2 + AuditScan(draft)
12. 插入 AuditSecurityContextBinding(canonical-empty, insert-only)
13. 创建 RunEvent(sequence=2, event_type=audit.created)
14. 插入 audit_client_requests(request_schema_version=v2)
15. commit exactly once
~~~

步骤 6–14 的实际 FK 安全顺序可以按数据库约束调整，但逻辑上必须属于同一 transaction，且只能由最
外层 commit。任一步校验、flush、unique/FK/CHECK 或 Event 写入失败，Plan reservation、Project、
Engagement、Run、Contract、Scan、Binding、Event 和 request row 全部回滚。

严禁：

- 调用多个 auto-commit Repository 后用 compensating delete 模拟事务；
- 先 reserve token，再在第二个 transaction 创建 Audit；
- 先提交 draft，再用 background task 补 Contract、Context Binding 或 client-request；
- 在 transaction 中读取 Git、创建目录、调用 Runner/Temporal、materialize Snapshot 或访问网络；
- 让 token、Plan canonical JSON、source path 或 Contract canonical bytes进入 Event/普通日志。

### 8.2 client-request v2

v2 `audit_client_requests` 在 ADR-0003 字段基础上必须绑定：

~~~text
request_schema_version = riftx.audit-create-draft-request/v2
request_digest
preflight_plan_id / preflight_plan_digest
security_context_id / security_context_digest
audit_id / run_id / project_id / engagement_id / contract_id / contract_digest
temporal_workflow_id = riftx-code-audit-{audit_id}
created_at
~~~

该行不可变，不保存 request body、source path、raw token、token hash、nonce、key ID、canonical Contract、
consent payload 或 provider disclosure。mapper 必须重新加载 Plan、Binding 和完整 aggregate，逐项证明
同一 authorization domain 与 owner；FK 关闭或历史损坏时也必须 fail closed。

### 8.3 重放与异常恢复

exact replay 在任何写入前返回当前完整 aggregate，`created=false/replayed=true`。Plan 已 consumed、
Audit 已 queued/running/terminal 或 `state_version` 已增加都不能把对象降回 draft。

unique race 的 `IntegrityError` 恢复沿用 ADR-0003：先 rollback 并离开 driver handler、丢弃 SQL/
parameters/cause，再在新 session 中重新读取 Plan、client-request 与 aggregate；只允许 exact replay、
稳定 conflict 或脱敏 persistence error。不得在 failed transaction 内查询，也不得记录可能包含 token、
path 或 canonical Contract 的 driver exception。

## 9. Canonical-empty Security Context Binding

### 9.1 AUD-201 唯一允许的上下文

固定 ID 为：

~~~text
riftx.audit-empty-security-context/v1
~~~

digest 使用该 schema/domain 对 canonical empty document 计算，并与 ADR-0007 Job/Result、Plan、
Contract v2 和 ModelDataEgress v2 中的值逐字节相等。AUD-201 的 Preflight request 继续只接受：

~~~text
input_id = null
repository_paths = []
discover_defaults = false
~~~

任何非空 input/path/default discovery、caller-supplied Bundle ID/digest 或不同 context digest 返回
`audit_security_context_unavailable`/capability error，不能把“没有实现 Bundle”解释成省略 binding。

### 9.2 最小 Binding row

Create v2 必须插入最小、insert-only 的 `audit_security_context_bindings` root：

~~~text
binding_schema_version
audit_id (unique)
preflight_plan_id / preflight_plan_digest
authorization_domain_digest
security_context_id = riftx.audit-empty-security-context/v1
security_context_digest = fixed canonical-empty digest
created_at
~~~

数据库 CHECK/复合 FK 与 mapper 必须证明 Binding 的 Audit、Plan、Contract 和 context 全等。该表不提供
update/save/hot-fill；不存在 Binding 的 v2 Audit 是损坏 aggregate，不能继续 Start。

AUD-201 不创建完整 `AuditSecurityContextInput`、`AuditSecurityContextBundle`、entry/manifest 表，不
解析 SECURITY.md，不读取 repository context paths，也不伪造一个“空 Bundle row”。AUD-209 引入非空
Bundle 后，历史 v2 Audit 不得原地换绑；context 任何变化都要求新的 input、Preflight Plan 和 Audit。

## 10. Start admission 与 AUD-208 边界

### 10.1 AUD-201 所有的准入事实

Start 必须接受独立 `start_request_id + reviewed_contract_digest`。AUD-201 冻结的 Start admission
顺序为：

~~~text
authentication/capability/feature flag
  -> Audit-root authorization and complete aggregate read
  -> Contract v2 + Plan + Context Binding exact proof
  -> same-node source/root/repository/content/backend/policy revalidation
  -> current capability/policy/model-egress consent review
  -> one transaction:
       Plan reserved -> consumed
       Audit draft -> queued
       Run created -> queued
       append Audit/Run events
       insert AuditStartIntent(status=pending)
  -> return persisted intent projection
~~~

真实 same-node revalidation 如需 SourceIngest Capsule，可以在数据库 transaction 前产生短期、
owner-bound、single-use 的 bounded proof；Start UoW 只在 transaction 内接受并重验该 proof。不得在开放
数据库 transaction 时等待 Runner/Git I/O，也不得让 AUD-208 dispatcher 自行读取 source。无论采用何种
具体 Port，source revalidation 的发起、proof schema、freshness、Plan/Audit/Contract binding 和接受
判定属于 Start admission，而不是 intent delivery。

若 root/repository/content、target、Node、backend/image/policy、empty context 或 reviewed Contract 任一
变化，Start 不消费 token、不写 queued/Intent；Plan 以 CAS 进入 `stale` 或 `revoked`，返回
`audit_snapshot_changed`/`audit_contract_review_required`。原 Audit/Contract 保持不可变 draft；用户
只能重新 Preflight 并创建新 Audit，不能给旧 Audit 换 Plan。

只有 transaction 已同时提交 Plan consume、Audit/Run queued 与 pending Intent 时，Start 才算准入
成功。AUD-201 本身不调用 Temporal，也不把 HTTP background task 当作可靠投递。

AUD-201 的 OpenAPI 可以暂不新增 Start route，但本 ADR/AUD-201 已拥有并冻结可独立测试的 Start
admission service/UoW，包括 same-node proof acceptance、Plan consume、queued projection 和 pending
Intent 原子写入。若公开 `POST /api/v1/audits/{audit_id}/start` 的薄 route adapter 与运行时 wiring 按
roadmap 落在 AUD-208，它只能调用该既有 admission contract；AUD-208 的新增业务职责仍然只有提交后
intent claim/lease/dispatch/retry/reconcile。AUD-201 完成时 Temporal client、普通 Runner enqueue 和
Audit Workflow start 调用次数必须恒为零。

### 10.2 AUD-208 只负责投递

AUD-208 的职责严格限于：

- claim/lease/reclaim pending/retryable intent；
- 使用持久 `workflow_id = riftx-code-audit-{audit_id}` 调用 Temporal；
- 处理 started、retryable、outcome_unknown、cancelled；
- 根据 Temporal history/数据库 projection 幂等 reconcile；
- 保证 cancel 先赢时不再 dispatch。

AUD-208 不得签发/验证新 Plan、读取 Git、重新选择 Node/backend、补 Contract/Binding、消费尚未消费的
token、把 draft 改为 queued，或在 admission 失败时“为了投递成功”放宽 source/content/expiry 检查。

## 11. v1、迁移与历史兼容

### 11.1 v1 永久隔离

`riftx.audit-contract/v1`、`riftx.audit-create-draft-request/v1` parser 和历史 aggregate mapper 继续保留，
以支持 list/detail、迁移校验、备份恢复和只读 exact-history tests。它们必须显式投影：

~~~text
preflight_provenance = synthetic_untrusted
preflight_plan_id = null
start_eligible = false
~~~

M1 body 中的 `operator_consent_at`、proof digest、selected node/backend/image/policy/candidates 永远不能
升级为权威事实。禁止：

- 给 v1 row 回填 Plan ID、token hash、Context Binding 或 Contract v2 digest；
- 重新编码/重算 v1 canonical Contract bytes；
- 通过 migration 或 admin command 把 v1 draft 转成 v2；
- 对 v1 Audit 开放 Start；
- 接受一个 v1 body 但按 v2 语义创建新对象。

新建生产路径只接受 v2。若保留内部 v1 replay adapter，它只能读取并返回已存在、完整匹配的历史
aggregate；client-request 不存在时必须拒绝，不能创建新的 v1 draft。公开 API 不允许通过省略 token、
添加 `contract` 字段或 content-type/header 差异触发 downgrade。

### 11.2 数据库迁移

AUD-201 migration 至少完成：

- 新增 `audit_preflight_plans`、最小 `audit_security_context_bindings`；
- 为 client-request/Contract/Scan 增加版本化 v2 binding 所需列、候选键、FK、CHECK 与索引；
- 保持所有 v1 canonical bytes 和历史 ID 不变，并显式标记无 authoritative Plan；
- 不扫描 repository、不生成 nonce/token、不创建 Plan/Binding/Audit/StartIntent；
- SQLite 使用 exclusive/serialized migration；PostgreSQL 使用固定锁序并可生成稳定 offline SQL；
- downgrade 在任何 DDL 前证明所有 v2 Plan/Binding/client-request/Contract/Audit facts 均为空，否则
  fail closed；offline downgrade 无法证明为空时直接拒绝。

测试覆盖 earliest supported revision → head → reopen、rollback/retry、并发升级、FK on/off、损坏行、
offline PostgreSQL 和 downgrade refusal。

## 12. `content_identity_digest`、Snapshot 与 Linux 资格边界

### 12.1 短期 fingerprint，不是源码封存

AUD-200 `content_identity_digest` 是对该 Preflight 算法可观察 Git/object/index/working-tree identity 的
短期 stale fingerprint，用于 Plan issuance 与 Start same-node revalidation。相等只表示“按同一版本
算法观察到的短期身份没有变化”，不证明所有源码 byte 已经进入 RiftX 管理的 immutable store。

因此它不得被当作：

- SourceSnapshot `snapshot_digest`、`tree_digest` 或 `manifest_digest`；
- CAS object key 或 blob SHA-256；
- file mode、symlink target、untracked byte 或 capture policy 的完整 Manifest；
- AuditStaticEffectPlan、mount lease、pin、GC reference 或 reader authorization；
- “Start 后仓库不可能再变化”的证明。

AUD-201 不创建 Snapshot/CAS/Manifest/materializer/mount/pin，也不返回伪造 handle。

### 12.2 AUD-202 的唯一职责

AUD-202 必须从同一授权 Node/root descriptor 链重新读取实际 bytes，并独立计算：

~~~text
per-object SHA-256 + size + object type
repository-relative canonical path + file mode + symlink policy
capture_policy_digest
deterministic Manifest/tree/snapshot digest
CAS staging/fsync/atomic-rename facts
Snapshot row + references + mount/pin ownership
~~~

即使 Start revalidation 刚刚通过，AUD-202 capture 仍必须重新验证 root/repository identity，并在读取
期间检测 TOCTOU；任何变化都不能继续沿用旧 `content_identity_digest` 创建 Snapshot。

### 12.3 不可跳过的生产资格门禁

真实授权仓库的 SourceIngest/Snapshot 生产资格必须等待真实本地 Linux container/VM 测试证明：

1. Control Plane/Runner 持有的 directory descriptor 或等价 inode-stable binding 能完成真实
   descriptor-to-mount round-trip，不能退化为“校验 path 后释放 descriptor，再按字符串重开”；
2. Capsule 内源挂载为只读，且对已有文件写入以及 create、chmod、rename、unlink 的实际尝试全部
   失败；
3. network=none、clean env、无凭据/socket、read-only rootfs、non-root、drop capabilities、资源上限
   与 stop receipt 均在真实 backend 生效；
4. mount 前后 root/repository identity 与宿主原仓库未写入事实可验证。

macOS 开发机、remote Docker pathname mount、fake/in-process backend、mocked chmod 或只检查 mount
flag 都不能替代该 gate。在 Linux 证据完成前必须返回 `audit_sandbox_unavailable`，且发布文档不能
宣称生产 SourceIngest/Snapshot 已 qualified。

## 13. 错误、脱敏与可观测性

稳定 reason code 至少区分：

~~~text
resource_not_accessible
audit_feature_disabled
audit_preflight_not_succeeded
audit_preflight_expired
audit_preflight_plan_unavailable
audit_preflight_plan_mismatch
audit_preflight_plan_quota_exceeded
audit_preflight_token_invalid
audit_preflight_token_key_unavailable
audit_idempotency_conflict
audit_budget_infeasible
audit_contract_review_required
audit_security_context_unavailable
audit_snapshot_changed
audit_persistence_unavailable
~~~

公开状态码可以按既有 API error policy 映射，但以下信息必须统一隐藏：Plan/token 是否存在、原 owner、
胜出 Audit、token hash/key ID/nonce、repository path、source proof、canonical Contract、SQL parameters 和
driver cause。invalid/stolen/cross-principal/expired/already-used token 的外部响应不得提供可组合 oracle；
详细分类只允许进入无敏感值的内部安全计数。

日志/trace/Event 只记录稳定 operation、safe reason code、必要的 server-owned object ID 与状态；token
对象必须 `repr=False` 或等价，HTTP body capture 对 Plan issuance/Create route 必须关闭或执行结构化
redaction。token/key 不进入 Runner、Worker、Sandbox、Agent 环境的自动化回归必须作为 release gate。

## 14. 实施顺序

Codex 实施 AUD-201 时按以下顺序推进；每一步独立通过 targeted tests 后再进入下一步，不能先开放
API 再补安全边界。

### 14.1 Domain 与 codec

1. 定义 Plan/target/Scope/token verifier 的严格、冻结、`extra=forbid` 模型与 domain-separated digest；
2. 实现 `ready/reserved/consumed/stale/revoked/expired` 状态机、expiry predicate 和 validated replace；
3. 实现 CSPRNG + HMAC token issue/re-derive/verify/hash，所有比较使用 constant-time primitive；
4. 加入 Job/Result/restricted request 的逐字段 cross-binding factory；
5. 用 property/fuzz tests 证明 identity 任一字段漂移都会失败，生命周期字段不改变 plan digest。

### 14.2 Secret/config 与 persistence

1. 增加 active key + retained verifier keyring Port；配置解析只接收 secret reference/key ID；
2. 缺 key、短 key、重复 key ID、rotation 缺旧 key均 fail closed；
3. 新增 Plan/Binding migration、schema、mapper、repository 和 CAS primitive；
4. 为 issuance、reserve、consume、stale/revoke/expire、exact replay 和 corruption 建立 transaction tests；
5. 确认 persistence DTO/exception/repr 不包含 raw token。

### 14.3 Contract/Create v2

1. 定义 caller-only v2 API schema、request canonicalization 和 downgrade/unknown-field redaction；
2. 定义 Contract v2、ModelDataEgress v2、`preflight_bound_draft` 与 unavailable/blocked capability 表达；
3. 扩展纯 AggregateFactory，只接受 verified Plan + caller preference + server facts；
4. 扩展同一个 `AuditCreationUnitOfWork`，加入 Plan reservation、Binding 和 v2 client-request；
5. 在每个 flush/insert/Event 前后注入失败，证明整个 transaction 包括 reservation 完整回滚；
6. 保持 v1 historical mapper/read，新增 v1 Start/downgrade rejection tests。

### 14.4 API、Policy 与后续 Start Port

1. 增加 Plan issuance route、policy inventory、OpenAPI、no-store middleware 和安全 projection；
2. 将 `POST /audits` 新建路径切到 v2，Feature Flag 必须在 token/client-request lookup 前拒绝；
3. 定义 Start revalidation proof Port 和 Start UoW contract，但 AUD-201 不调用 Temporal；
4. 为 AUD-208 提供仅接收已提交 pending Intent 的 dispatcher Port，禁止 dispatcher 反向承担 admission；
5. 更新 CLI/UI contract fixture 时只展示安全 Plan/Contract 摘要，不持久化 token 到 local/session
   storage；刷新或 token stale 后要求重新 Preflight。

## 15. 验收测试与 Definition of Done

AUD-201 至少通过以下测试矩阵：

1. **Plan factory**：非 succeeded、expired、blocking Result、Job/Request/Result owner 漂移、target/Scope/
   context/capability/budget digest 漂移全部拒绝；Plan 不读取 Git、不创建 Audit。
2. **Token**：nonce 长度/随机性、canonical base64url、MAC/hash/domain separation、tamper、truncate、
   replay、错误 key、key rotation、重启 re-derive、缺 key fail closed、raw token leak canary。
3. **状态/CAS**：所有合法/非法转换、`now == expires_at`、reserve-vs-expire、reserve-vs-revoke、
   consume-vs-stale、多进程竞争、state-version drift、terminal no-release。
4. **Issuance API**：auth/scope、feature flag、201/200 idempotency、历史 Job 不自动 backfill、non-ready 不
   回显 token、no-store、422/404/409/503 脱敏和 OpenAPI/policy inventory。
5. **Create v2 wire**：proof/selection/consent/Bundle/source/path/unknown-field/downgrade 注入全部拒绝；
   token steal、跨 principal/scope、Plan mismatch、budget/policy 收紧、Context swap fail closed。
6. **请求幂等**：相同 key/body/token、响应丢失、Plan 后续 consumed/expired 的 exact read replay；同 key
   异 Plan/auth/context/preference conflict；同 token 异 request race 只有一个 Audit。
7. **原子性**：reservation、Engagement、Project、Run、每个 Event、Contract、Scan、Binding、request
   row 任一点 failure injection 后数据库均无半事实；restart 后重放收敛。
8. **Contract v2**：canonical bytes/digest、v2 ModelDataEgress、Plan/Contract/Run/Scan/Binding 全等；未来
   proof 不得伪造；结果始终是 `preflight_bound_draft/start_eligible=false`。
9. **v1/migration**：earliest→head→reopen、v1 canonical bytes 未改、v1 list/detail 可读、v1 Start/new
   create/downgrade 拒绝、损坏 Plan/Binding/client-request fail closed。
10. **Start contract**：same-node/content/policy/context/review drift 不 consume；成功 transaction 同时产生
    consumed + queued Audit/Run + pending Intent；AUD-201 Temporal 调用次数恒为零。
11. **Snapshot 边界**：AUD-201 无 Snapshot/CAS/Manifest/mount/pin/materializer；测试不能把
    `content_identity_digest` 接到 Snapshot digest 字段。
12. **生产 gate**：真实 Linux descriptor mount round-trip 与 Capsule write/create/chmod/rename/unlink
    deny smoke；macOS/fake 只能标记 `not executed`，不能标记 qualified。
13. **回归**：General Run/Runner/Workflow 行为不变；Code Audit ordinary Runner enqueue 仍为零；无模型、
    Agent、Scanner、Detector、网络 fetch、依赖安装或 Temporal Start。
14. **独立性**：依赖、license/provenance、禁用 import/string/fixture 扫描继续证明没有 Codex Security
    Provider、代码、Prompt、Schema、运行时或生成物进入产品。

所有 agent 相关 Python 测试、lint、migration 与运行命令使用 Conda `agent` 环境，例如：

~~~text
conda run --no-capture-output -n agent python -m pytest <targeted-tests>
conda run --no-capture-output -n agent python -m pytest <full-python-suite>
conda run --no-capture-output -n agent ruff check <changed-python-paths>
~~~

只有上述 targeted/full regression、migration、redaction、independence gate 全部通过，且真实 Linux gate
被明确记录为 `passed` 或发布资格仍保持 disabled，AUD-201 才能标记 completed。测试通过不自动开启
`audit.enabled`，也不代表 M2 Exit 或 3.0 扫描能力已完成。

## 16. 非目标与禁止实现

AUD-201 明确不做：

- SnapshotStore、CAS、Manifest、materializer、mount/pin、GC；
- Inventory、Scope ledger、Detector、SARIF、AST、dependency、SBOM、Secret scan；
- 完整 Security Context Input/Bundle/entry、SECURITY.md discovery/parser；
- Temporal Workflow 启动、intent dispatcher/reconciler 的 AUD-208 实现；
- build、test、PoC、fix、依赖下载、target network 或 writable source；
- 模型/Agent 调用、远程源码 egress 或 provider 接入；
- 把 Plan token 暴露给 Runner/Worker/Sandbox/Agent；
- 把 v1 draft 转换为 v2，或给已创建 Audit 热替换 Plan/Context/Contract；
- 以 macOS/fake sandbox 结果宣称生产 Linux backend qualified。

任何实现若需要上述能力，必须停在 typed unavailable/blocked 状态，并由对应后续 Work Item/ADR 明确
引入，不能以“临时兼容”进入 AUD-201。

## 17. Provenance 与变更纪律

本文决策依据仅包括：

- RiftX 仓库内 ADR-0001 至 ADR-0007；
- `docs/riftx-3-code-audit-development-spec.md`；
- RiftX 当前 AUD-200 Domain、API、Persistence、Runner 与测试合同；
- 团队对通用代码审计产品模式的独立分析。

实现 PR 必须记录：

~~~text
implementation commit
authors / reviewers
schema and migration revision
token/key configuration review
targeted/full test commands and results
Linux qualification environment and evidence, or explicit not-executed/disabled status
independence/provenance scan result
known follow-up work items (AUD-202/AUD-208/AUD-209)
~~~

如果实现发现需要改变 token wire、Plan identity/state、Create v2 ownership、empty-context Binding、v1
隔离、Start/AUD-208 边界或 Snapshot integrity 边界，必须先修改本 ADR或新增 superseding ADR，再修改
代码。不能通过 mapper fallback、migration backfill、Feature Flag 分支或测试 fixture 静默改变本合同。
