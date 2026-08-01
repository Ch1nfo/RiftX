# RX-LN-03 交付报告：Task、Evidence 与 Operation 语义视图

> 阶段：`RX-LN-03`
> 完成日期：2026-08-02
> 前置阶段：`RX-LN-01 = done`、`RX-LN-02 = done`、`RX-LN-AUTH = done`
> 结论：完成；独立审查 `APPROVE`，`P0 = 0`、`P1 = 0`

## Outcome

RiftX Run Detail 现在提供三种确定性、只读且可追溯的语义视图：Task、Evidence 和
Operation。三者均从 RiftX 已有领域记录即时投影，不新增通用可写 Graph 真相表，不调用模型，
不根据时间、当前焦点或相同字符串猜测关系。

服务端新增 Run-scoped Graph API、窄化 source DTO、严格父 Run/Engagement 授权、稳定签名游标、
source coverage 与 partial/truncated 语义。Web 新增独立 lazy Graph workspace、SVG/DOM 视图、
完整列表 fallback、服务端 metadata 驱动的图例/筛选、URL 深链和 Action 双向跳转。Graph 未激活
时不下载 chunk、不发送 Graph 请求，也不阻塞 Action 首屏。

审查期间发现并关闭了四类重要边界：模型自报 evidence type 不再能确认 Fact/Hypothesis；
旧 Unicode/含冒号 Action ID 保持可读且只返回 `graph_ref = null`；Action 与 Graph 的双向跳转
严格验证同 Run typed identity；纯 foreign-Run Engagement Fact 在 application boundary 直接省略。

## Scope

### Implemented

#### Server projection contract

- 新增 `GraphView DTO`、source snapshot contract、application projector、SQLAlchemy read repository、
  API schema/route/error mapping、runtime wiring 和 policy inventory。
- Graph source DTO 只允许身份、状态、typed lineage 与 coverage 字段；结构上不能携带 Run objective、
  workspace path、Fact value/natural language、Finding description、UserDecision 文本、command、argv、
  env、cwd、output、Artifact path、Credential、Cookie、Token 或 Secret。
- 第一版不创建 graph node/edge 持久真相表，不修改 Temporal 调度，不写入 Working Memory，
  不调用 LLM/projector，也不虚构 dependency。
- 使用 typed、scope-qualified ID，例如 `action:<run_id>:<action_id>`、
  `artifact:<run_id>:<artifact_id>`、`engagement_fact:<engagement_id>:<fact_id>`，避免相同原始 ID
  在不同 Run 或 Run/Engagement namespace 间碰撞。
- Projection 在返回前拒绝重复 node/edge ID 与孤儿边；分页以 edge unit 为单位，每一条边与两端节点
  同页物化，独立节点作为单独 unit。

#### Three views and explicit gaps

| View | Authoritative sources | Exact projection | Explicitly partial / unavailable |
|---|---|---|---|
| Task | RunPlan、ToolCallIntent、Execution、Artifact、Finding | `Action → Execution → Artifact → Finding` 的已有同 Run lineage；Plan item sequence/status | 现有 Action/Finding 没有 durable `plan_item_id`，所以全部进入明确的 `unassigned` 分区；blocked/completed Plan item 的 blocker/completion evidence 标为 unavailable，不按时间猜测 |
| Evidence | Working Memory Fact/Hypothesis/UserDecision、Finding、Execution、Artifact、EngagementFact、FactRelation | 同 Run Execution/Artifact、稳定 UserDecision 节点、active same-Engagement relation | Working Memory Fact association 可由模型写入，只能是 candidate/partial；EngagementFact 持久层已丢失 evidence source type，始终 `unverified`；跨 Engagement、纯 foreign Run、superseded 或 unresolved relation 被省略 |
| Operation | Runtime Session、Execution、RiftX execution host | Session parent lineage、Session contains Execution、Execution ran-on runner host | 当前没有可治理的 target Host/Service/Endpoint/Credential read model，因此不把 runner node、URL 或文本误写成目标资产 |

#### Confirmation and provenance rules

- Working Memory `confirmed_facts` 的 `source_refs/source_types` 可由 Subagent/model candidate 写入；
  即使自报 `deterministic_parser` 或 `user_decision` 且引用同 Run Artifact/Execution/UserDecision，
  Graph 也只保留 partial candidate edge，Fact 与由其支撑的 Hypothesis 不得成为 `confirmed`。
- Working Memory Fact 的合法 `disputed`、`superseded` 生命周期保持原状态；只有缺乏权威 association
  的 `confirmed` 降级为 `unverified`，未知状态 fail closed。
- Finding 只有在引用同 Run、可解析且具备 Execution lineage 的 Artifact/Execution，或可解析的稳定
  UserDecision 时才能保留 `confirmed`；orphan Artifact 不构成确认依据。
- 裸字符串 `user_confirmed` 不是用户决定。UserDecision 使用独立稳定节点，且模型自报的关联本身仍不
  获得 authority。
- EngagementFact 因当前 schema 无法证明 evidence source type，一律 `unverified`；FactRelation 只保留
  same Engagement、active endpoints、same Run lineage、显式 allowlisted relation type，并标为
  `partial`。
- Hypothesis 没有权威支持时不能确认；存在可信 contradiction 时也不能确认。当前 schema 尚无不可由
  模型写入的 Fact evidence-association，因此不会把 Working Memory Fact 升为 authoritative Fact。

#### API, authorization and pagination

- 新增 `GET /api/v1/runs/{run_id}/graph`，支持 `view`、`node_type`、`edge_type`、`focus`、
  `search`、`limit`、`cursor`。
- Query schema `extra=forbid`；拒绝客户端 `actor/role`、空 search、控制 Unicode、非法 type token、
  非法 Graph ID、超长 cursor，并且 validation/error body 不回显恶意输入。
- 路由进入 API policy inventory，部署策略为 `LOCAL_OPERATOR / READ_ONLY`；Principal 只由服务端认证
  依赖注入，客户端不能自报身份或 capability。
- app-owned object authorizer 先解析父 Run 与 Engagement，再执行 `READ` 授权；unknown Run、foreign
  child 和不可访问对象使用同形 not-accessible 语义，避免 IDOR/枚举差异。
- app 启动时生成至少 32-byte Graph cursor signing key。HMAC-SHA256 cursor 绑定 principal、Run、
  Engagement、view、全部 filters、limit、offset 与 snapshot；签名错误返回 stable 422，合法旧 snapshot
  返回 stable 409。
- Snapshot 同时绑定 topology 与 source coverage。普通分页 `has_more/next_cursor` 和 source budget
  `truncated/partial_reasons` 是两套独立语义；repository 使用 `limit + 1` 探测 coverage，禁止静默 hard cap。
- View 使用固定且不随对象数量增长的 metadata-only SQL 形状：Task 6 个 SELECT、Evidence 15 个
  SELECT、Operation 4 个 SELECT；无 per-node N+1、Runner I/O 或解密路径。

#### Action bidirectional reference

- Action list/detail 由服务端派生可选 `graph_ref`：严格可表示时返回 Task view exact typed node ID；
  repository 无法自报或覆盖该字段。
- Graph component 只接受 ASCII、单组件、最长 128 字符，完整 node ID 最长 512；同原始 Action ID
  在不同 Run 生成不同节点。
- 为保持旧 Run 可读，历史安全 Unicode、含冒号/斜杠或其他无法无歧义表示的合法 Action/Run ID
  返回 `graph_ref = null`，不会让整个 Action list/detail 失败；危险 Unicode 与超过原 Action contract
  上限的 ID 仍 fail closed。
- Action→Graph 与 Graph→Action 都验证 current Run、Action domain ID、canonical typed node ID、view
  和 `projection_quality = exact`；foreign/wrong-action/partial/malformed ref 不产生跳转。

#### Web workspace and interaction

- `RunGraphWorkspace` 是独立 dynamic import chunk；未切到 Graph tab 时不 import、不 query。
- 提供 Task/Evidence/Operation 切换、server metadata 驱动的 legend/type filters、search、focus、
  load-more、SVG/DOM Graph 和语义完整的列表 fallback。
- URL 使用 `graph_view=task|evidence|operation` 与 `graph_focus=<typed-node-id>`；支持刷新、分享、
  Back/Forward。切换语义 view 会清除旧 focus，离开 Graph 会清除 Graph params。
- 已加载节点切换 focus 不重复请求；属性更新保留布局、selection 和 viewport，拓扑变化或显式 re-layout
  才重算布局。
- 对 Run/Engagement/view/snapshot/cursor、重复 edge、孤儿边和跨页 context node 做客户端完整语义校验；
  同 ID context node 只有所有可见字段一致时才去重。
- 401/403 对 Graph query 零重试并触发 per-Run auth latch：立即 mask/purge 整个 Run Graph cache，
  跨 view、卸载/重进也不能回显撤权数据；request epoch 防止旧 403 覆盖较新的成功授权结果。
- 普通 500/网络 refetch 失败保留最后一次已验证数据，但明确显示 stale/error/retry；首次 loading、empty、
  partial、forbidden、truncated、stale 与 pagination 都有独立状态。
- X/Escape、Back/Forward 与 tab 切换具备焦点恢复；timer 在 cleanup 时取消，避免旧导航抢焦点。
  列表 fallback、tab、filter、节点、状态和 live/error 文案具备 keyboard/ARIA 语义，不只依赖颜色。
- 中英文 Graph 主路径同步；metadata/node/edge labels 通过统一 i18n helper，动态 `Plan item N`
  安全转换，未知服务端 label 原样回退，不把 type switch 散落到 UI。

### Explicitly not implemented

- 通用可写 Graph、模型自动补边、按时间/current focus 猜 PlanItem dependency，或把 Graph 当作新的
  无治理模型记忆。
- Durable Task lineage、可选 dependency DAG、Event-to-Candidate Projector、AttackGraph 写入或
  Temporal 调度变更。
- Target domain Host/Service/Endpoint/Credential 推断；Operation View 只显示 RiftX runtime/runner
  已有对象。
- RX-LN-04A Target HTTP Exchange metadata、任何 Header/Cookie/Body reveal、Replay、Safety Gate、
  Route 或 Gateway。
- 数据库 migration、新依赖、部署、push 或 PR。

## Independent design

| 字段 | 内容 |
|---|---|
| Inspired behavior | 在 Action 工作台旁提供 Task/Evidence/Operation 三种可审计语义视图与双向定位 |
| RiftX requirement | 确定性 read projection、typed identity、server Principal、父对象授权、no-orphan 分页、显式 partial/truncated |
| Reused RiftX primitives | RunPlan/WorkingMemory、ToolCallIntent、Finding/FactRelation/EngagementFact、Execution/Artifact、RuntimeSession、ExecutionHost、RX-LN-01 Action API、LocalObjectAuthorizer、TanStack Query、Run Detail tokens |
| Authority/source of truth | 现有 durable domain records；Graph API snapshot 是读取权威，URL 只保存 view/focus，SSE/前端不创造边 |
| Identity/idempotency | typed scope-qualified node IDs；HMAC cursor 绑定 principal/scope/view/filter/limit/snapshot；无 Graph 写 effect |
| Authorization/Scope | server-owned Principal；父 Run→Engagement resolve 后 `READ` authorizer；unknown/foreign 同形 not-accessible |
| Secret handling | source DTO 与 SQL SELECT 双重 allowlist；不选择/返回 prose、command、argv/env/path/output、Fact value、Credential 或 Secret |
| Recovery/rollback | 纯 read-only、无 migration；普通 refetch 可显示 verified stale snapshot，auth failure 立即遮蔽；整阶段可回退而不迁移或丢数据 |
| Accessibility | lazy workspace、完整列表 fallback、keyboard/ARIA、focus cleanup、文字化 partial/truncated/status、中文/英文路径 |
| Independent design | 基于 RiftX 现有领域模型、API policy、授权、前端信息架构独立设计；无竞品源码、Prompt、CSS、测试或视觉资产移植 |

## Clean-room declaration

- Implementation input：本开发手册、RiftX 源码、现有测试和所用框架官方文档。
- LuaN1aoAgent source/assets inspected during implementation：`No`。
- Copied or translated competitor code/tests/prompts/CSS/screenshots/assets：`No`。
- Root Agent（接触过竞品研究材料）的职责仅限规格、测试要求、审查协调、交付文档与本地提交；
  未编写阶段功能代码。
- 功能实现由声明 `competitor_material_seen=No` 的 clean-room Agents 完成；最终集成与加固 Agent：
  `/root/rx_ln_03_web_impl`，包含模型 trust、legacy Action compatibility 与双向跳转收口。
- 独立 reviewer：`/root/rx_ln_03_graph_review`；未接触 upstream source，冻结 diff 最终结果
  `APPROVE`、`P0 = 0`、`P1 = 0`。
- New dependencies and licenses：无。
- Database migration：无。

## Verification

所有 Agent/Runtime 相关命令均通过 conda `agent` 环境运行。

### Final frozen-state evidence

```text
conda run --no-capture-output -n agent python -m pytest -q \
  tests/unit/application/test_graphs.py \
  tests/unit/application/test_actions.py \
  tests/integration/persistence/test_graph_repository.py \
  tests/integration/api/test_graph_api.py
834 passed in 5.81s

Independent Graph/API/persistence lifecycle recheck
49 passed

conda run --no-capture-output -n agent python -m pytest -q
2473 passed, 5 skipped in 215.04s

conda run --no-capture-output -n agent pnpm --filter @riftx/web test
19 test files, 217 tests passed

conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
PASS

conda run --no-capture-output -n agent pnpm --filter @riftx/web build
PASS
RunGraphWorkspace: 21.57 kB, gzip 6.84 kB
RunDetailPage: 528.49 kB, gzip 140.59 kB

conda run --no-capture-output -n agent python -m ruff check <21 changed Python paths>
PASS

conda run --no-capture-output -n agent python -m ruff format --check <21 changed Python paths>
21 files already formatted

conda run --no-capture-output -n agent python scripts/qa/release-gate.py
ready=true; all 15 declared gates passed

git diff --check
PASS
```

五项 Python skip 均因为当前主机缺少 Windows ConPTY/PowerShell，或缺少真实 delegated cgroup v2
与独立 payload UID/GID，不是阶段失败。一次并行聚焦运行的既有 PTY 集成测试曾遇到 SQLite
`database is locked`；该测试随后单独通过，最终全量 suite 与包含同一测试的 release gate 均通过。

## Risks and follow-up

- **P2 / cursor-page rebuild**：每次 cursor 请求当前都会重新读取、投影、排序并计算完整有界 Graph，
  再按 offset 切页；source 上限可达每类 10,000。查询数固定且无 N+1，但遍历全部页面时可能接近
  二次总工作量。后续应评估 server-owned materialized snapshot 或 repository keyset/page-unit index，
  同时保持 cursor 的 principal/scope/snapshot 绑定和 no-orphan invariant。
- **P2 / existing RunDetail chunk warning**：Graph 已独立 lazy 为 gzip 6.84 kB，但 RunDetailPage 主
  chunk 仍为 528.49 kB，超过 Vite 500 kB warning line。后续可继续拆分非首屏 tab/Inspector；不得
  为消除 warning 把 Graph 合回 Action 首屏。
- Working Memory 目前没有不可由模型写入的 authoritative Fact evidence-association，因此 Fact 与
  Hypothesis 保守显示；未来若增加 durable association，必须有独立 producer authority、migration、
  provenance 和 confirmation regression，不能重新信任 candidate `source_type`。
- Operation View 明确不包含目标 Host/Service/Endpoint/Credential；若未来增加，必须先建立有治理的
  typed domain read model，不得从 runner host、URL、输出文本或模型描述推断。
- Repository source cap 会显式返回 `truncated + partial_reason`；大 Run 仍可能需要运营侧调优，但禁止
  通过隐藏 hard cap 或客户端假装完整来优化。

## Ledger update

- Previous：`RX-LN-03 = in_progress`
- New：`RX-LN-03 = done`
- Evidence：本报告；834 targeted、2473 Python、217 Web、typecheck/build、21-file Ruff、
  release gate `ready=true`、diff check；clean-room reviewer `APPROVE`，`P0 = 0`、`P1 = 0`。
- Next：完成本阶段独立本地提交后，才把 `RX-LN-04A` 标为 `in_progress`；04A 只实现 Target HTTP
  metadata-only History/Inspector，不解密/返回 Header、Cookie、Authorization、Client Certificate
  或 Body，也不发送 Replay。
