# RX-LN-AUTH 交付报告：部署 Trust Profile、Principal 与对象授权边界

> 阶段：`RX-LN-AUTH`
> 完成日期：2026-08-01
> 选择的 Trust Profile：`local_single_operator`
> 未选择的 Trust Profile：`remote_multiuser`
> 结论：完成；独立审查 `APPROVE`，无未解决 P0–P3

## Outcome

RiftX 现在只允许显式选择 `local_single_operator`，并在服务启动前强制 loopback
监听、loopback 浏览器 Origin、安全 operator credential 和稳定的服务端 LocalPrincipal。
所有 `/api/*` 路由都进入 fail-closed policy inventory；REST、SSE、下载和 WebSocket
使用同一服务端认证与 effect→capability 边界，Approval actor 不再由客户端决定。

本阶段没有把本地单操作员模型描述或实现为 tenant ACL。`remote_multiuser`、非 loopback
监听、proxy identity、Traffic Body、Replay、Route 和 Gateway 均保持不可用或关闭。

## Scope

### Implemented

- 显式、可观察的 `local_single_operator` Trust Profile；未选择、未知、多个 Profile、
  `remote_multiuser`、非 loopback、远程 CORS 和 proxy identity 均启动失败。
- 服务端生成且跨重启稳定的 `LocalPrincipal`；token 与 Principal ID 分离。
- PrincipalStore 使用 POSIX `dir_fd`、`O_NOFOLLOW`、owner/mode 校验、无覆盖原子发布、
  inode 复核以及 file/directory `fsync`；缺少安全原语的平台 fail closed。
- 逐路由 effect→capability：read、write、control、host execute、host control；未知 effect
  在装配和请求时均拒绝。
- 共享 operator/admin credential 的 Admin 路由也进入同一 capability 边界，不再绕过
  `/security/profile` 所公布的权限集合。
- operator 与可选 Runner bootstrap credential 均强制至少 32 个非空白 ASCII 可打印字符；
  两个 credential 相同则启动失败。Bootstrap `None` 明确表示禁用。
- Bootstrap token 从 Pydantic dump/JSON/repr、`APISettings` repr 和
  `RunnerDaemonConfig` repr 排除；两个 `--registration-token` argv surface 已删除，
  只允许 owner-scoped 环境或受管 secret 注入。
- REST/SSE 使用 Bearer header；浏览器 WebSocket 使用固定协议 marker 与编码 credential
  subprotocol，服务端只回选 marker；CLI WebSocket 使用 Authorization header。
- Web token 只存在模块内存，不进入 URL、localStorage 或 sessionStorage；CLI 拒绝向
  非 loopback Control Plane 发送 token。
- Artifact/Evidence/Report 使用 authenticated fetch；下载上限固定为 64 MiB，并同时校验
  Content-Length 与实际流式累计字节，成功和失败均清理 DOM、reader 和 Blob URL。
- Approval 的 `decided_by` 从客户端 Schema 删除并 `extra=forbid`；requester/decider actor
  从服务端 Principal 传播到 Approval、Runtime Approval、grant 和 Event。
- 新增 `GET /api/v1/security/profile`，明确 `tenant_safe=false` 和关闭的高风险 feature。
- Control Plane、Worker、Runner 使用独立最小化环境文件；Worker 不接收 operator/admin
  或 Runner bootstrap credential。

### Explicitly not implemented

- `remote_multiuser` 的 TLS、真实 AuthN、安全 Session/CSRF、登录限流、trusted proxy 和
  tenant/engagement/Run ACL。
- Traffic Body、敏感 reveal、Replay、Route、Gateway 或任何 RX-LN-04B0/04B1 能力。
- 多租户对象 ACL；`LocalObjectAuthorizer` 只是父 Run 校验与未来授权后端扩展点。
- 新数据库迁移、新依赖、部署、push 或 PR。

## Independent design

| 字段 | 内容 |
|---|---|
| Inspired behavior | 明确信任边界、服务端身份、最小权限和跨协议一致认证 |
| RiftX requirement | Profile A、LocalPrincipal、route effect capability、服务端 actor 和父 Run 授权 |
| Existing foundation | API policy inventory、Approval 服务、Runner/Admin 认证、Web/CLI 客户端、Run 子资源模型 |
| Authority/source of truth | 配置选择 Trust Profile；Principal state file 只持久化稳定 ID；route policy 决定 capability；领域库/Event 保持业务权威 |
| Identity/idempotency | token 只认证稳定 LocalPrincipal，不充当 actor ID；安全 Principal 首次创建并发幂等、无覆盖发布 |
| Authorization | HTTP/WS dependency 在 endpoint/service 访问前认证；Admin 与 Local 路由按服务端 effect 映射 capability；未知分类 fail closed |
| Secret handling | credential 不进浏览器持久存储、URL、响应协议、repr/dump 或普通文档示例；WS 握手 credential header 禁止进入日志 |
| Recovery/rollback | Principal ID 跨重启稳定；token 可独立轮换；关闭 Profile A 或删除安全前置不会回退到远程/匿名模式 |
| Independent design | loopback-only Profile A、安全 PrincipalStore、统一 capability enforcement、内存 token gate 和有界 authenticated download |
| Upstream material copied | `None` |

## Clean-room declaration

- Implementation input：本开发手册、RiftX 源码和现有测试。
- LuaN1aoAgent source/assets inspected during implementation：`No`。
- Copied or translated competitor code/tests/prompts/assets：`No`。
- New dependencies and licenses：无。
- Implementers：`/root/auth_impl`、`/root/auth_fix_impl` 及其 Web 下载子任务；均声明
  `competitor_material_seen=No`。
- Independent reviewer：`/root/auth_final_review`；声明
  `competitor_material_seen=No`，冻结 diff 最终结果 `APPROVE`。
- Reviewer result：无未解决 P0、P1、P2 或 P3。

## Verification

所有 Agent 相关命令均通过 conda `agent` 环境运行。

### Final frozen-state evidence

```text
conda run --no-capture-output -n agent python -m pytest -q
1422 passed, 5 skipped in 152.82s

conda run --no-capture-output -n agent python -m pytest -q \
  tests/unit/test_local_operator_auth.py tests/unit/test_api_policy.py \
  tests/unit/test_runtime_config.py tests/unit/cli/test_app.py \
  tests/unit/cli/test_client.py tests/unit/cli/test_terminal.py \
  tests/browser/test_api.py tests/connectors/test_api.py \
  tests/context/test_manifest.py tests/integration/api/test_control_plane.py
208 passed

conda run --no-capture-output -n agent python -m pytest -q \
  tests/unit/cli/test_app.py tests/unit/test_runner_daemon_cli.py \
  tests/unit/test_runtime_config.py tests/runner/test_control_client_protocol.py \
  tests/runner/test_remote_control.py
100 passed

conda run --no-capture-output -n agent pnpm --filter @riftx/web test
17 files, 109 passed

conda run --no-capture-output -n agent pnpm --filter @riftx/web typecheck
PASS

conda run --no-capture-output -n agent pnpm --filter @riftx/web build
PASS (existing >500 kB chunk warning only)

conda run --no-capture-output -n agent python -m ruff check src/riftx tests
PASS

changed Python paths: ruff format --check
PASS

git diff --check
PASS
```

五项 skip 均由当前主机缺少 Windows ConPTY/PowerShell 或真实 delegated cgroup v2 与独立
payload UID/GID 导致，不是测试失败。既有 Pydantic `alias='run_id'` warning 与 Vite chunk
warning 均不由本阶段引入。

## Risks and follow-up

- Profile A 是单工作站、单操作员边界，不是 tenant-safe；远程部署必须另行完成 Profile B。
- 32 字符规则只阻止缺失和显著弱配置，不能证明随机性；部署必须使用安全随机源。
- authenticated download 仍会在浏览器内有界物化最多 64 MiB；更大文件需要独立的安全下载
  机制设计，不能提高现有上限绕过。
- Control Plane 与 Worker 示例仍因共享 owner-only 数据采用同一 OS 账户；独立 env 阻止主动
  分发 inbound credential，但不等同于敌对同 UID 进程隔离。
- 浏览器 WS credential subprotocol 属于 Bearer credential material；代理、访问日志、trace 和
  error report 必须禁止记录 `Sec-WebSocket-Protocol` 请求头。
- Traffic Body/Replay/Route/Gateway 保持关闭；不得用本阶段完成状态提前解锁 RX-LN-04B0/04B1。

## Ledger update

- Previous：`RX-LN-AUTH = in_progress`
- New：`RX-LN-AUTH = done`
- Evidence：本报告；冻结态 1422 Python / 109 Web；typecheck/build/Ruff/format/diff checks；
  clean-room reviewer `APPROVE`。
- Next：`RX-LN-01`，只实现 Run Action Read Model/API，不提前修改 UI。
