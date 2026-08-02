# RX-LN-02 交付报告：Action Timeline 与 Context Inspector

> 阶段：`RX-LN-02`
> 完成日期：2026-08-01
> 前置阶段：`RX-LN-01 = done`、`RX-LN-AUTH = done`
> 结论：完成；独立审查 `APPROVE`，`P0 = 0`、`P1 = 0`

## Outcome

RiftX Run Detail 现在提供服务端权威的 Action Timeline 与 URL-addressable Context Inspector。
工具意图、Approval、Execution attempts、结果与证据只从 RX-LN-01 typed Action API 获取，
不再由 Web 从 raw Event、Execution 或 Approval 列表重新猜测 Action。用户可以在同一工作台中
阅读 Conversation、查看审计 Action、定位 Artifact/Finding 引用，并保留独立的高层 Timeline
与明确标注窗口范围的 Raw Events。

Action UI 支持稳定 cursor 分页、多 Session/cycle 身份、partial/truncated/ambiguous 语义、
stop confirmation、URL 深链、浏览器历史、键盘与焦点恢复、中英文主要路径，以及
fail-closed 401/403 cache handling。SSE 只作为通知通道；服务端 snapshot 始终是权威校准源。

## Scope

### Implemented

- 新增 Web typed DTO、client、query keys、infinite Action list query 与按 Run/action 隔离的 detail
  query；query cache identity 至少包含 `run_id + action_id`。
- Actions 取代旧 Tool Calls 视图；旧 Execution command/cwd/path 不再作为默认工具叙事显示。
- 折叠 Action 卡展示公开 why/what、Approval、lifecycle、current/latest exact attempt、Runner
  node、started/duration、exit code、stop confirmation、Finding/Artifact counts 与数据质量标记。
- current attempt 缺失、correlation partial 或 attempt order ambiguous 时显示 unknown/partial，
  不回退到旧 latest attempt，也不把 Intent `created_at` 误写成 Execution started time。
- Context Inspector 按选择才读取 detail，展示服务端脱敏 arguments、Approval actor/feedback、
  Execution lifecycle/error、stop confirmation、coverage/truncation 与 Artifact/Finding references。
- Artifact 内容不内联、不进入 URL/storage；下载继续使用既有 authenticated、no-store、64 MiB
  capped 通道。切 Action/Run 使用 keyed remount/归属隔离，旧下载 promise/error 不落入新 Inspector。
- Action list 使用服务端 opaque cursor 和可访问的 Load more；分页去重使用 stable
  `run_id:action_id` key，不固定截取最近 N 个 Action。
- 加载下一页的普通 500 以内联错误呈现并保留已加载页；任何 list/detail/SSE 401/403 立即
  fail closed，Action 卡、Inspector 与 tab count 一并遮蔽。
- Action list/detail 对 401/403 零重试，普通错误仍按既有策略重试一次；避免默认 retry delay
  期间继续显示撤权缓存。
- 增加 per-Run Authorization latch：detail 撤权后即使按 X/Escape 关闭 Inspector，旧 list
  也不会重现；只有之后真正成功且版本更新的 list reauthorization 才解除。
- URL query `action=<action_id>` 是 Inspector selection 的事实源；刷新、分享、Back/Forward
  和 Run 切换保持一致。旧 Run selection/detail 在新 Run 首帧即不可见。
- 用户触发打开时聚焦 Inspector close control；X/Escape 关闭按当前 Action ID 恢复来源卡焦点，
  卡不存在时回退 Actions tab。直接深链不会无条件抢焦点。
- Run Detail tablist 增加 roving `tabIndex`、Arrow/Home/End、`aria-selected`、`aria-controls`
  和有效 tabpanel 关系；Action disclosure、Inspector、live/error/status 均使用明确 ARIA 语义。
- Action live announcement 按 batch 更新并使用 atomic polite region；不播报 token delta，不把整张
  长列表设为 live region。
- 高层 Timeline fail-closed 排除 `action.*`、`agent.tool_*`、`tool.*`、`execution.*` 与
  `target_http.*` Action-family Event；这些只留在受权 Raw Events，不用 raw payload 形成第二套
  Action 叙事。signed URL/canary/command/env 不进入默认 Conversation/Timeline DOM。
- Raw Events 文案明确为“已加载事件中的 latest window”，不虚报全局 total；当前仍有界显示
  latest 200。
- SSE 对 arbitrary arrival order 按 `run_id + sequence` 排序/去重，保持 append fast path；
  只连续推进 cursor。duplicate、out-of-order、gap、snapshot/batch race、late HTTP 和跨 Run
  response 均有回归。
- 首次连接、gap 与 reconnect 都校准 Action snapshot；gap repair 的 500 会 abort 活跃 stream、
  进入 backoff reconnect、再次校准并在成功后清错。401/403 fatal 不无限重连并清除 Action cache。
- 普通 Action Event 只 invalidate list/可信 detail，保留已加载页、selection 和焦点；只有
  reconnect/gap reconciliation 才 reset Action snapshot。
- 最小扩展 RX-LN-01 list attempt contract：增加 allowlisted `node_id` 与已有 `exit_code` 投影；
  仍为固定 7 SELECT、attempt 上限 100，无 N+1、Runner I/O 或 error/command/env/path 文本。
- 新增 RiftX 原生 Action/Inspector 样式，复用既有 design tokens；桌面保持两栏，受限宽度与
  ≤930 px 自然堆叠，320 px/高缩放安全换行；partial/truncated/unconfirmed 不只依赖颜色，
  focus-visible、reduced-motion、dark/light 对比与 hard-timeout danger 状态同步。

### Explicitly not implemented

- RX-LN-03 Task/Evidence/Operation read views、Evidence Graph 或 Graph UI。
- RX-LN-04A Target HTTP Exchange metadata History/Inspector。
- RX-LN-04B0/04B1 Traffic Body、Reveal、Replay、Route 或 Gateway。
- 从 raw Event 推导 Action、展示 hidden chain-of-thought、未经授权的 Artifact 正文或 command preview。
- remote multi-user Profile、部署、push 或 PR。

## Independent design

| 字段 | 内容 |
|---|---|
| Inspired behavior | 在可读对话旁提供可审计工具行动与上下文详情 |
| RiftX requirement | typed Action API 唯一语义源、snapshot 权威、Run-scoped cache/selection、fail-closed auth、可访问分页 |
| Existing foundation | RX-LN-01 Action list/detail、TanStack Query、useEventStream、Run Detail、LocalPrincipal、authenticated Artifact download、RiftX design tokens |
| Authority/source of truth | Action/Approval/Execution durable state与Action API；SSE仅通知，URL仅保存selection，不保存业务状态 |
| Identity/idempotency | `run_id + action_id` query/DOM key；服务端 cursor；Event `run_id + sequence`；跨 cycle不使用 engine_call_id 猜测 |
| Authorization | 客户端只展示服务端 actor/status；list/detail/SSE任一401/403触发同Run Action surface latch与cache遮蔽 |
| Secret handling | 默认卡片只用list allowlist；detail只用服务端脱敏DTO；action-family raw payload不进入高层Timeline；Artifact按需受权下载 |
| Recovery | 首连/gap/reconnect snapshot校准；普通event保留cache，异常重建snapshot；500退避恢复，auth fatal停止重连 |
| Accessibility | URL history、roving tabs、button disclosure、Escape/X、focus restore、polite atomic live region、文字+形状状态 |
| Independent design | RiftX现有信息架构、组件、颜色token、API与SSE语义的独立实现；无竞品视觉或代码移植 |
| Upstream material copied | `None` |

## Clean-room declaration

- Implementation input：本开发手册、RiftX 源码、现有测试和所用框架官方文档。
- LuaN1aoAgent source/assets inspected during implementation：`No`。
- Copied or translated competitor code/tests/prompts/CSS/assets：`No`。
- New dependencies and licenses：无。
- Primary implementer：`/root/rx_ln_02_ui_impl`；声明 `competitor_material_seen=No`。
- SSE/a11y/CSS clean-room specialist：`/root/rx_ln_02_sse_a11y_audit`；声明
  `competitor_material_seen=No`。
- Independent reviewer：`/root/rx_ln_02_ui_review`；声明
  `competitor_material_seen=No`，冻结 diff 最终结果 `APPROVE`。
- Reviewer result：`P0 = 0`、`P1 = 0`；一个 bundle-size P2 见下文。

## Verification

所有 Agent 相关命令均通过 conda `agent` 环境运行。

### Final frozen-state evidence

```text
conda run --no-capture-output -n agent pnpm --filter @riftx/web test
18 test files, 164 tests passed

conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
PASS

conda run --no-capture-output -n agent pnpm --filter @riftx/web build
PASS
RunDetailPage JS: 525.67 kB, gzip 139.64 kB

Action application/query/persistence/API targeted
809 passed

conda run --no-capture-output -n agent python -m pytest -q
2412 passed, 5 skipped in 219.76s

conda run --no-capture-output -n agent python scripts/qa/release-gate.py
ready=true; all 15 declared gates passed

8 changed Python paths: ruff check / ruff format --check
PASS / 8 files already formatted

git diff --check
PASS
```

五项 Python skip 均由当前主机缺少 Windows ConPTY/PowerShell 或真实 delegated cgroup v2 与独立
payload UID/GID 导致，不是测试失败。Web build 只有下述 bundle-size warning；没有跳过测试、
放宽断言或关闭类型检查。

## Risks and follow-up

- **P2 / bundle size**：`RunDetailPage` production chunk 为 525.67 kB（gzip 139.64 kB），超过
  Vite 500 kB warning line。当前不影响正确性，但 RX-LN-03 增加 Graph 前应先按 tab/Inspector
  拆分 lazy chunks，避免继续增长和阻塞 Action 首屏。
- Raw Events 当前只显示已加载 history 的 latest 200，虽明确标注 partial window，但不能导航
  更早事件；后续需要独立的服务端分页审计视图，不得把固定窗口描述成完整历史。
- SSE append fast path 已避免常态全量 sort，但每次 flush 仍会复制已加载 Event 数组；超长 Run
  需要窗口化 Event cache/虚拟列表。重连退避已有上限但尚无 jitter。
- Finding/Event/lifecycle source 中部分引用当前只显示稳定 ID；RX-LN-03 才增加确定性 Evidence
  view/Graph，不得在本阶段按时间猜 lineage。
- Action API 安全地没有提供 command preview 或 stdout/stderr 正文；UI 通过 tool/target/reason、
  result metadata 与授权 Artifact 回答用户问题。未来若确需 preview，必须先扩服务端有界脱敏契约，
  不能读取 raw Event/Execution 补齐。
- Profile A 现有 Artifact 下载使用全局 Artifact ID；本阶段保持 authenticated endpoint 且不内联。
  进入 remote multi-user 前必须完成父 Run/tenant-safe Artifact ACL，不得把本阶段视为 Profile B。

## Ledger update

- Previous：`RX-LN-02 = in_progress`
- New：`RX-LN-02 = done`
- Evidence：本报告；164 Web、2412 Python、release gate ready、typecheck/build/Ruff/diff；
  clean-room reviewer `APPROVE`，`P0 = 0`、`P1 = 0`。
- Next：`RX-LN-03`，只实现确定性的 Task/Evidence/Operation read views 与 Graph UI；独立提交后
  再进入 RX-LN-04A。
