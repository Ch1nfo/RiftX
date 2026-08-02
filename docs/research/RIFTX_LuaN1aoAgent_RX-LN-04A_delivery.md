# RX-LN-04A 交付报告：Target HTTP metadata-only History/Inspector

> 阶段：RX-LN-04A
> 完成日期：2026-08-02
> 前置阶段：RX-LN-01 至 03、RX-LN-AUTH 均为 done
> 结论：完成；独立审查 APPROVE，P0 = 0、P1 = 0

## Outcome

RiftX Run Detail 现在提供 Target HTTP Exchange 的只读 History 与 Inspector。该能力只投影
脱敏元数据，不读取或返回 Header、Cookie、Authorization、Proxy Authorization、Client
Certificate、请求/响应 Body、正文 excerpt、原始 URL、签名 query、Artifact 路径或 Secret，
也不提供 Reveal、Download、Replay 或任何网络 effect。

服务端新增 Run-scoped list/detail API、typed traffic.metadata.read capability、父 Run/Engagement
授权、HMAC 签名游标、窄 SQL leaf projection、server-instance keyed request digest 与不可逆
Artifact opaque ref。新 Exchange 在既有 result_json 内附加 server-generated safe metadata；
旧记录不回读 raw URL/request JSON 补空，而是明确显示 partial/unavailable。

Web 新增独立 lazy Traffic workspace、稳定 URL 深链、History 分页与 Inspector。客户端对服务端
envelope 做 exact runtime validation；401/403 使用 Run 级 rejection fence 清除所有 Traffic
缓存，拒绝授权失败前已启动请求的 late success。普通网络/500 refetch 只保留最后一次已验证
metadata，并明确标记 stale。

本阶段同时关闭了旧敏感 Artifact 的旁路：通用 Artifact list/get/content、Event REST/SSE、
Report、Graph 与 Action 都复用数据库关联加服务端 marker 的 fail-closed 可见性判定。

## Scope

### Server read model

- 权威来源是 TargetHttpRequestRecord，以及同 Run 的 ToolCallIntent、Approval、Node 与 Artifact
  existence metadata；没有新增第二张 Traffic 真相表或 migration。
- exchange_id 等于 request_id；稳定排序键为 created_at + id。
- SQL 只选择明确 JSON leaf 与必要身份字段，不选择完整 request_json/result_json、raw URL，
  也不选择 Artifact name/path/description/mime/hash/size/content。
- 新记录在 result_json 中附加 _riftx_safe_read_metadata_v1，内容仅包括：
  - http/https scheme 与规范化 origin；
  - 不含路径值的 path shape 与 segment count；
  - redirect count 与 origin-only hops；
  - request body 是否存在。
- legacy row 缺少 safe metadata 时返回 unavailable/partial，不从旧 raw URL 或正文推断。
- canonical request digest 在 persistence 内用独立 domain-separated HMAC 派生；裸 request hash
  不进入 application DTO 或 API。
- request/response Artifact 只返回 traffic-artifact:v1:... 形式的 keyed opaque ref、recorded
  presence 与 metadata-only access；真实 Artifact ID 不进入 Traffic API。
- Content-Type 只允许 15 个固定安全 base media type。参数、非法值及语法合法但未 allowlist 的
  attacker-controlled subtype 均丢弃并标 partial。
- TLS 仅返回 availability、verified 与 client-certificate-used 布尔值；不返回证书 ref。
- Runner LOST 是独立 Node 状态，不伪造为 HTTP failure；orphan Intent/Approval、missing Artifact
  与 legacy metadata 都有 typed partial reason。

### API, authorization and pagination

- GET /api/v1/runs/{run_id}/target-http/exchanges
- GET /api/v1/runs/{run_id}/target-http/exchanges/{exchange_id}
- list query 只接受 method、status_class、limit、cursor；actor、role、capability 与未知字段拒绝。
- 路由进入 API policy inventory，策略为 LOCAL_OPERATOR / READ_ONLY；没有 POST、Reveal 或 Replay
  产品路由。
- typed traffic.metadata.read 由服务端 adapter 映射到 OperatorCapability.READ。Principal 只来自
  认证依赖，客户端不能自报身份或 capability。
- 服务端先解析父 Run 与 Engagement，再授权子资源；unknown、foreign 与 wrong-Run detail 返回
  同形 resource_not_accessible，避免 IDOR 与枚举差异。
- HMAC cursor 绑定 Principal、Run、Engagement、method/status filters、limit、snapshot boundary
  和 page key；tamper 返回 value-free 422，合法但过期 snapshot 返回 value-free 409。
- 1003 条同 timestamp 记录通过 keyset 分页无重复、无遗漏；snapshot 后新增记录不漂入旧页面，
  删除 snapshot 内记录会使旧 cursor stale。
- list 使用固定数量 metadata-only SQL，不触发 Runner、Artifact loader、解密或网络 I/O。

### Event and Artifact bypass closure

- 新 target_http.request_started Event 从源头只写安全 URL summary；失败、响应与取消 Event 只写
  固定 category/status metadata，不写 exception、runner reason、execution key、request ID 或 URL。
- Event 出口对全部已知 target_http.* 使用 exact safe projection，未知 target_http.* payload
  全隐藏；REST 与 SSE 走同一 projector。
- generic-named legacy Artifact 通过 target_http FK 关联识别；in-flight/orphan Artifact 通过
  authoritative Artifact marker 识别。Event payload name 仅作即时保守 fallback，不能覆盖数据库
  authority。
- Artifact association lookup 是批量、跨 Run、fail-closed 的；历史 cross-Run FK 不能绕过。
- 通用 Artifact list/get/content、Report source、Graph Artifact nodes 与 Action artifact lineage
  都排除 marker-owned 或 Target HTTP referenced Artifact；普通 Artifact 行为保持不变。
- Graph SQL 安全测试允许 name/description 仅出现在 WHERE visibility predicate，同时禁止这些
  字段、通配符、raw Target HTTP 列进入 SELECT projection，并检查 SQL parameters 不含 canary。
- 外部 path/query validation error 统一 value-free，未知 query name/value、cursor、method、
  status 与 exchange identity 不回显攻击输入。

### Web workspace and interaction

- RunTrafficWorkspace 是独立 dynamic import chunk；未激活 Traffic tab 时不加载模块、不请求 API。
- URL 使用 traffic_view=history|inspector 与 traffic_exchange=<exchange-id>，支持刷新、分享、
  Back/Forward 与 direct Inspector URL。
- History/Inspector exact validator 直接消费后端生成的 list/detail JSON fixtures；extra、missing、
  identity/status/governance/body invariant 变异一律 fail closed。
- Web 保留 safe Unicode 与 legacy execution/exchange identity，拒绝换行、ZWSP 和全部 Unicode
  C 类字符。
- 401/403 零重试并立即 mask/purge 同 Run 的 active/inactive History 与 detail cache。
  rejection fence 覆盖失败时所有已启动 epoch；只有失败后新发起并成功的 revalidation 才能解除。
- 普通 refetch failure 显示 stale 且保留最后 verified metadata；loading、empty、partial、
  forbidden、truncated、stale 与 pagination 均为独立状态。
- History row 支持 roving keyboard focus；Inspector 的 Escape/X/Back/Forward 与 Action↔Traffic
  导航恢复正确焦点，Run 切换不会回显旧 Run metadata。
- 中英文主路径、ARIA、非颜色状态文本与窄屏布局已覆盖。
- UI 没有 Body reveal、Artifact download、Replay 按钮或链接；Artifact opaque ref 仅作不可点击
  metadata 展示。

### Explicitly not implemented

- Header、Cookie、Authorization、Proxy、Client Certificate、Body、excerpt、Artifact 正文或解密。
- Replay、Reveal、Download、SensitiveAccessIntent、SafetyGateRequest、Scope DNS/peer-IP
  enforcement 或任何新的网络 effect。
- RX-LN-04B0/04B1；它们仍为 not_started，必须由用户明确选择，且 B0 完成后才能进入 B1。
- 通用可写 Traffic 表、模型补全 legacy metadata、数据库 migration 或新依赖。
- remote/multi-user Trust Profile 扩张、部署、push 或 PR。

## Independent design

| 字段 | 内容 |
|---|---|
| Inspired behavior | 在 Run 工作台提供可分页的 Target HTTP History 与 metadata Inspector |
| RiftX requirement | metadata-only、父对象授权、稳定 signed cursor、typed partial、无正文/无 effect |
| Reused RiftX primitives | TargetHttpRequestRecord、ToolCallIntent/Approval/Node、Artifact、LocalObjectAuthorizer、API policy、TanStack Query、Run Detail tokens |
| Authority/source of truth | 既有 Target HTTP durable record；Event 只通知/审计，不作为 Exchange authority |
| Identity/idempotency | exchange_id=request_id；HMAC cursor；server-instance request digest；opaque Artifact ref；GET 无写 effect |
| Authorization/Scope | server Principal；父 Run→Engagement resolve；OperatorCapability.READ；unknown/foreign 同形 404 |
| Secret handling | field-level SQL allowlist、safe metadata v1、fixed media allowlist、exact Event projector、Artifact visibility guard |
| Recovery/rollback | 无 migration；纯 read projection；auth purge；普通 stale fallback；可移除 API/UI 而不改历史 Exchange |
| Accessibility | lazy workspace、list fallback、keyboard/ARIA、focus restoration、中英文、非颜色状态 |
| Independent design | 只依据本 playbook、RiftX 代码与现有测试独立实现；未移植竞品代码、Prompt、CSS、测试或视觉资产 |

## Clean-room declaration

- Implementation input：本开发手册、RiftX 源码、现有测试和所用框架官方行为。
- LuaN1aoAgent source/assets inspected during implementation：No。
- Copied or translated competitor code/tests/prompts/CSS/screenshots/assets：No。
- Root Agent 接触过竞品研究材料，职责仅限规格、测试要求、审查协调、交付文档与本地提交；
  未编写阶段功能代码。
- 功能实现 Agent：
  - /root/rx_ln_04a_backend，competitor_material_seen=No；
  - /root/rx_ln_04a_web，competitor_material_seen=No。
- 独立 reviewer：/root/rx_ln_04a_security_review；其 backend edge audit 同样声明
  competitor_material_seen=No。最终结论 APPROVE，P0=0，P1=0。
- New dependencies and licenses：无；package/lockfile 无变化。
- Database migration：无。

## Verification

所有 Agent/Runtime 相关命令均通过 conda agent 环境运行。

### Backend targeted gates

    conda run --no-capture-output -n agent python -m pytest -q tests/unit/application/test_event_projection.py tests/unit/application/test_traffic.py tests/unit/models/test_api_schemas.py tests/unit/test_api_runtime.py tests/integration/persistence/test_traffic_repository.py tests/integration/persistence/test_repositories.py tests/target_http tests/integration/application/test_reports.py
    124 passed

    conda run --no-capture-output -n agent python -m pytest -q tests/integration/api/test_control_plane.py tests/integration/api/test_traffic_api.py tests/integration/api/test_graph_api.py tests/integration/api/test_actions_api.py
    87 passed

    conda run --no-capture-output -n agent python -m pytest -q tests/integration/persistence/test_graph_repository.py
    12 passed

### Final frozen-state gates

    conda run --no-capture-output -n agent python -m pytest -q
    2519 passed, 5 skipped in 228.63s

    conda run --no-capture-output -n agent pnpm --filter @riftx/web test
    20 test files, 254 tests passed

    conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
    PASS

    conda run --no-capture-output -n agent pnpm --filter @riftx/web build
    PASS
    RunTrafficWorkspace: 35.70 kB, gzip 9.82 kB

    conda run --no-capture-output -n agent python -m ruff check src tests
    All checks passed

    conda run --no-capture-output -n agent python -m ruff format --check <35 changed Python paths>
    35 files already formatted

    conda run --no-capture-output -n agent python scripts/qa/release-gate.py
    ready=true; all 15 declared gates passed

    git diff --check
    PASS

五项 Python skip 均来自当前主机缺少 Windows ConPTY/PowerShell，或缺少真实 delegated cgroup v2
与独立 payload UID/GID，不是阶段失败。

第一次全量运行曾有三个 RX-LN-03 Graph SQL 旧测试失败：旧断言把 visibility WHERE 中合法的
Artifact description marker 误判为 SELECT 泄漏。只精化了测试门禁，没有修改生产逻辑；新门禁
明确禁止敏感列与 wildcard 进入 SELECT，同时检查 raw Traffic 列、SQL parameters 与 canary。
原失败 3/3、Graph repository 12/12 和最终全量均通过。

## Risks and follow-up

- P2 / snapshot content mutation：snapshot fingerprint 绑定 boundary、total、scope/filter，但不包含
  每行内容。应用记录为 immutable；若数据库被原地篡改或同计数替换，cursor 不一定 stale。后续可
  引入 immutable row version/content digest，不得以读取 raw payload 解决。
- P2 / cross-Run opaque-ref linkability：Artifact opaque ref 当前由 artifact_id 派生；历史同一
  Artifact 被跨 Run 引用时可得到相同 ref，形成低风险关联，但不会泄漏真实 ID 或正文。后续可将
  Run/Engagement 纳入 HMAC domain。
- P2 / legacy Finding reference：旧 Finding evidence 可能序列化已经持有的 Target HTTP Artifact
  真实 ID；新写入已由 guarded ArtifactRepository 拒绝，正文路由也始终 404。后续应为 legacy
  Finding/Report 增加 association-aware evidence redaction。
- P2 / truncated display invariant：后端生成的 response.truncated 与 body.response.truncated 一致，
  但 schema/Web 尚未交叉验证两者相等。后续应增加 DTO model invariant 与 Web exact validator。
- P2 / existing RunDetail chunk warning：Traffic 已独立 lazy，但 RunDetailPage 主 chunk 仍为
  530.44 kB，超过 Vite 500 kB warning line。后续继续拆分非首屏 workspace，不得把 Traffic 合回
  Action 首屏。
- 固定 safe Content-Type allowlist 会把未知 vendor media type 显示为 unavailable/partial；这是
  为避免 attacker-controlled subtype canary 的保守兼容取舍。

## Ledger update

- Previous：RX-LN-04A = in_progress。
- New：RX-LN-04A = done。
- Evidence：本报告；124 backend core、87 API、12 Graph repository、2519 Python、254 Web、
  typecheck/build、Ruff/format、release gate ready=true、独立 reviewer APPROVE/P0=0/P1=0。
- Default core：RX-LN-00、RX-LN-AUTH、RX-LN-01、02、03、04A 均为 done，因此 playbook 定义的
  默认核心交付完成。
- Next：停止。RX-LN-04B0/04B1、05A 至 08 未获用户明确选择，不得自动开始；RX-LN-09 继续
  blocked。没有 push、PR 或部署。
