# RX-LN-01 交付报告：Run Action Read Model、API 与耐久执行基础

> 阶段：`RX-LN-01`
> 完成日期：2026-08-01
> 前置阶段：`RX-LN-00 = done`、`RX-LN-AUTH = done`
> 结论：完成；独立审查 `APPROVE`，`P0 = 0`、`P1 = 0`

## Outcome

RiftX 现在以持久化 `ToolCallIntent.id` 作为 Agent Tool Action 的唯一主身份，把分散的
Intent、Approval、Execution attempt、Artifact、Finding 和 Event 组合为服务端只读投影。
Web、TUI 和未来客户端可以通过同一组 Run-scoped API 获取稳定、分页、脱敏且可解释的
Action 列表与详情，而不在客户端重新猜测关联关系，也没有新增第二套可写 Action 真相表。

本阶段同时补齐了 Action 正确性依赖的 durable identity、mutation clock、Approval decision、
Execution claim、immutable launch fingerprint 与 PTY admission/recovery 基础。它们解决了跨 cycle
provider call ID 复用、Approval 历史扩权、并发 Execution 重放和 Terminal projection orphan
等会导致 Action 串线、重复 Effect 或错误停止证明的问题。

## Scope

### Implemented

- 新增应用层 `RunActionView` / `RunActionListItemView`，以 ToolCallIntent 为左表语义；
  Approval 或 Execution 缺失时仍返回 orphan/partial Action。
- Approval 通过 `RuntimeApprovalRequest.tool_call_intent_id → Runtime request.id → public Approval`
  桥接；不使用易碰撞的裸 Tool Call ID 猜测关联。
- 一个 Action 保留全部有界 Execution attempt，并以 durable lifecycle timestamp、创建时间和 ID
  tie-breaker 派生 current/latest；顺序不可靠时显式返回 ambiguous/partial reason。
- 列表只返回 metadata；不读取 Runner stdout/stderr、完整命令、环境、绝对路径或大输出。
  详情也只返回有界、服务端脱敏的 arguments/error/event/evidence 元数据；敏感 Artifact 内容仍走
  独立授权接口。
- 对 arguments、feedback、历史 error/Event 文本、URI/query/header、路径和 canary Secret
  执行递归、有深度/节点/字节上限的读取边界脱敏。
- 新增 HMAC 绑定的 opaque snapshot keyset cursor；固定 `created_at DESC, action_id DESC`，
  cursor 绑定 Run、sort、limit 与 snapshot，篡改、跨 Run/limit 重放和格式错误统一 fail closed。
- 新增 Run-scoped、父 Run 授权的 Action list/detail API；即使 Action ID 全局唯一也先验证父 Run，
  避免 opaque-ID IDOR。
- 新路由进入 API policy inventory，OpenAPI/response model 与 production
  `build_control_plane()` 装配同步；未知或漏分类 route 继续 fail closed。
- 增加 durable mutation clock，Action `updated_at` 能反映 Approval、Execution、Artifact、Finding
  和 Event 的已持久化变化，而不依赖本地 wall clock 或 UI 写入。
- ToolCallIntent 身份升级为 `v2 = hash(run_id, session_id, cycle_id, engine_call_id)`；同 Session
  后续 cycle 复用 provider call ID 不再串 Action。只有同 cycle 且完整快照相符的 legacy v1
  才可复用；不可变字段漂移 hard fail，旧 PK/FK 不重写。
- 新 public Approval 使用 `sdk_call_id = ToolCallIntent.id`；legacy v1 保持旧 bridge，复用时验证
  完整身份。历史 Approval 只有 same-ID terminal Runtime decision 可决定 scope；无证据的 approved
  记录保守恢复为 `approve_once`，不得由同 Run/Tool 的宽泛 grant 反向扩权。
- Execution submission 使用 durable claim 和 immutable launch fingerprint，重试只能复用完整
  身份相同的 Effect；status CAS 防止 late start 覆盖安全 stop。
- local/remote PTY 使用 `Execution.CREATED → Terminal projection → CREATED→STARTING CAS`
  准入；只有 execution-ID CAS winner 可以 dispatch。projection 创建失败、post-commit error、
  cancellation、并发 exact caller、legacy STARTING orphan、restart 和 stop race 均 fail closed。
- `NodeExecutionRouter` 按 executor/location 路由停止：local PTY 走 TerminalSupervisor，
  local PROCESS/SHELL 走 ProcessSupervisor，remote execution 走 RemoteExecutionSupervisor；API 与
  Temporal Worker 使用相同 production wiring。

### Read API contract

```text
GET /api/v1/runs/{run_id}/actions
  query: limit=1..100, cursor=<opaque>, sort=created_at_desc

GET /api/v1/runs/{run_id}/actions/{action_id}
```

列表响应包含稳定身份、公开 reason/target、Approval 摘要、全部有界 attempt metadata、
current/latest、lifecycle、result/evidence counts、coverage/truncated、correlation quality、partial
reasons、updated_at、has_more 与 next_cursor。详情增加有界脱敏 arguments、Approval feedback、
Execution error、Finding IDs 和 Event metadata，但不包含 Runner output、raw Event、Secret、
Cookie、Authorization、完整 env 或主机绝对路径。

### Bounded materialization budgets

对一页 `N` 个 Action，持久化读取/物化预算为：

| 关联 | 列表预算 | 单项详情预算 |
|---|---:|---:|
| Approval | `≤ N` | `≤ 1` |
| Execution | `≤ 100N` | `≤ 100` |
| Artifact | `≤ 100N` | `≤ 100` |
| Finding | `N` 个 summary row | `≤ 100` |
| Event | `N` 个 summary row | `≤ 200` |

列表 repository 使用批量查询和预聚合，不按 Action 调 Runner，也不产生每行 Approval、Execution、
Artifact、Finding 或 Event 的应用层 N+1。任何超过预算的来源必须返回 coverage/truncated/partial，
不得静默丢失或把“最近 N 条”伪装为完整历史。

### Migrations

```text
b2c4d6e8f001_add_action_read_foundations.py
c4d6e8f0a213_add_durable_mutation_clocks.py
d5e7f9a1b304_add_tool_call_execution_claims.py
e6f8a0b2c415_add_public_approval_decisions.py
f7a9c1d3e526_add_execution_launch_fingerprints.py
```

Alembic 最终保持单一 head：`f7a9c1d3e526`。升级、旧数据 backfill、冲突恢复与 downgrade
shape 均有 migration 测试；没有重写既有 ToolCallIntent/Execution 主键。

### Explicitly not implemented

- RX-LN-02 的 Action Timeline、Context Inspector 或任何 UI 改动。
- RX-LN-03 Evidence Graph 与 RX-LN-04A Target HTTP Exchange metadata。
- RX-LN-04B0/04B1 的 Traffic Body、Reveal、Replay、Route、Gateway 或敏感访问能力。
- 新的 `run.action_changed` 事件、第二套 Action 写模型或 UI 可写状态。
- remote multi-user Profile、部署、push、PR 或生产数据迁移。

## Independent design

| 字段 | 内容 |
|---|---|
| Inspired behavior | 把工具意图、审批、执行结果与证据组织成可审计的一体化 Action 体验 |
| RiftX requirement | ToolCallIntent 权威身份、Run-scoped 授权、durable attempts、fail-closed 恢复、服务端脱敏和有界读取 |
| Existing foundation | Runtime repositories、Approval/Execution/Artifact/Finding/Event、API policy inventory、LocalPrincipal、SSE 与 Runner supervisors |
| Authority/source of truth | ToolCallIntent/Approval/Execution 等既有 durable tables；Action 只是 application read projection，Event 只是审计，SSE 只是增量通知 |
| Identity/idempotency | cycle-scoped Intent v2、Approval bridge identity、Execution claim/key/fingerprint、execution-ID CAS winner |
| Authorization | API dependency 提供服务端 Principal；object authorizer 验证父 Run 与 READ capability；不信任客户端 actor/role |
| Secret handling | allowlist DTO + bounded recursive redaction；列表 metadata-only；Artifact/raw output 保持独立授权 |
| Recovery/rollback | legacy v1 保留、旧 Approval 保守 scope、migration backfill、partial reason、Terminal predispatch proof 与 CAS recovery |
| Independent design | RiftX 命名、领域模型、API 结构、cursor、状态合并与 admission protocol 均从现有 RiftX primitives 独立设计 |
| Upstream material copied | `None` |

## Clean-room declaration

- Implementation input：本开发手册、RiftX 源码、现有测试和所用框架文档。
- LuaN1aoAgent source/assets inspected during implementation：`No`。
- Copied or translated competitor code/tests/prompts/CSS/assets：`No`。
- New dependencies and licenses：无。
- 最终 Terminal/RunSafety clean-room owners：`/root/av3_legacy_terminal_fix` 与
  `/root/av3_legacy_terminal_fix/terminal_orphan_admission`；均声明
  `competitor_material_seen=No`。
- Independent reviewer：`/root/rx_ln_01_gate_review`；声明
  `competitor_material_seen=No`，冻结 diff 最终结果 `APPROVE`。
- Reviewer result：`P0 = 0`、`P1 = 0`；两个 P2 fail-closed availability residual 见下文。

## Verification

所有 Agent 相关命令均通过 conda `agent` 环境运行。

### Final frozen-state evidence

```text
conda run --no-capture-output -n agent python -m pytest -q
2412 passed, 5 skipped, 1 warning in 230.92s

Action application/query/persistence/API/production control-plane
863 passed

Approval recovery + deferred resolver/runtime + execution service
124 passed

Persistence integration + Runtime repositories
111 passed

Migration/clock focused suite
24 passed

Runner + Runtime Terminal + real production PTY pause/stop
337 passed, 5 skipped

Frozen local/remote Terminal pair
63 passed

conda run --no-capture-output -n agent python scripts/qa/release-gate.py
ready=true; all 15 declared gates passed

conda run --no-capture-output -n agent python -m ruff check src/riftx tests migrations
PASS

85 changed/untracked Python paths: ruff format --check
85 files already formatted

conda run --no-capture-output -n agent alembic heads
f7a9c1d3e526 (head)

git diff --check
PASS
```

五项 skip 均由当前主机缺少 Windows ConPTY/PowerShell 或真实 delegated cgroup v2 与独立
payload UID/GID 导致，不是测试失败。唯一 warning 是 Pydantic 对 `Last-Event-ID` Field alias
位置的提示；本阶段未通过过滤 warning、跳过测试或放宽断言使门禁通过。

真实 production PTY 测试通过 `build_control_plane()` 启动本地 PTY，Run pause 后验证 OS PID
消失、Terminal 为 `CLOSED`、Execution 为 `CANCELLED` 且存在
`physical_stop_confirmed_at`，并核对 pause audit 的 confirmed statuses。

## Risks and follow-up

- **P2 / remote pre-enqueue availability**：中央 remote PTY 若在 Execution/Terminal 已为
  `CREATED`、`TERMINAL_START` 尚未 enqueue 时崩溃，Runner tombstone 会阻止延迟启动，但 Runner
  因没有本地 Execution 行而不能提供物理停止 ACK。Run 可能保持 fenced，直到 exact activity
  retry 收敛。不存在已执行 Effect，也不会伪造停止证明。
- **P2 / CAS post-commit ambiguity**：`CREATED → STARTING` CAS 若提交成功后抛错，调用方无法
  区分自身提交与另一个 winner；实现选择保持 STARTING fail closed，exact replay 也不重新 dispatch。
  极端故障下注入可能需要协调器 reconciliation 或人工恢复。
- **SQLite JSON/CPU bound**：Finding/Event 在 SQLite 内仍需扫描相关历史行并进行 JSON 投影。
  Python 返回行数和响应大小有界，但数据库内部 CPU/扫描工作没有严格硬上界；大规模部署应使用
  PostgreSQL 索引化关联或专用可验证列，并补 query-plan budget。
- **Temporal completion signal**：本阶段没有新增 `run.action_changed`。Action 真相和
  `updated_at` 已耐久，但 UI 若只依赖单个 SSE completion signal，极端 Temporal replay/晚到事件
  下可能延迟刷新；RX-LN-02 必须以分页 snapshot 为校准源，用既有 durable event 定向 invalidate，
  不能把 SSE 当权威状态。
- **平台实机覆盖**：SQLite 与真实 Unix PTY 已覆盖；PostgreSQL 的锁/CAS/query plan、Windows
  ConPTY、真实 remote Runner 网络断连以及 delegated cgroup containment 未在当前主机实机运行。
  对应路径保持 fail closed，但上线前仍需目标平台 CI/故障注入。
- 非 SQLite dialect 的个别 fail-closed 分支仍缺直接 mocked-dialect 单测；高基数 clock 测试中
  Event 主导最终 max-clock，后续可分别补 Artifact/Finding/Execution 截断外 max-clock 断言。
- RX-LN-02 必须消费本 API，不得在 React 组件中从 raw Event 重建 Action，也不得因 UI 需要而
  扩大列表输出、泄漏 Secret 或绕过父 Run 授权。

## Ledger update

- Previous：`RX-LN-01 = in_progress`
- New：`RX-LN-01 = done`
- Evidence：本报告；冻结态 2412 Python；release gate ready；85-file Ruff/format；单一 Alembic
  head；clean-room reviewer `APPROVE`，`P0 = 0`、`P1 = 0`。
- Next：`RX-LN-02`，只实现 Action Timeline 与 Context Inspector；独立提交后再进入 RX-LN-03。
