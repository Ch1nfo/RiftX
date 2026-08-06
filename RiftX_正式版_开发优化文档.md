# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前阶段唯一的产品收敛、代码优化与开发完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 当前基线：`42b1b6ac`
>
> 实施账本：[`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 安全边界：[`ADR-0012`](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest 决策：[`ADR-0013`](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 1. 产品目标与判断

RiftX 正式版只兑现一个核心结果：

> **成为专业人士手中真正好用，并且能随着 Tool、Skill、Technique、Playbook 和实践经验持续成长的授权渗透测试 Agent。**

它同时需要两个属性：

1. **开箱即用**：新用户完成 Onboard 和 Doctor 后，可以在明确授权、明确 Scope、隔离或可控的目标上启动一条基础 Pentest 工作流，并能查看状态、恢复、停止和获取报告。
2. **上限足够高**：专业用户可以逐步加入自己的工具、技能、验证方法和复盘经验；系统能版本化、审查、回放、启用、禁用和回滚这些能力。

“超过 Codex、Claude Code、OpenCode”是产品追求，不是 V1 的量化发布门。RiftX 真正应建立的优势是：

- 持久、可恢复的专业任务状态；
- 授权、Scope、Approval、预算和停止证明；
- Evidence、Negative Result、Finding 与 Attack Chain；
- 可组合、可追溯、可回滚的专业能力；
- 把操作者方法论沉淀为可复用能力，而不是只保存聊天记录。

### 1.1 当前是否过度开发

**是，已经存在阶段性过度开发。**

问题不是某个安全模块“做得太完整”，而是平台底座、兼容面、任务编号、迁移和测试规模已经很大，用户最需要的 Pentest 主路径仍未完整贯通。当前约有 17.6 万行生产 Python、14.6 万行测试 Python、50 余个 migration，但尚缺少完整的 `pentest start → execute → evidence → report → stop` 用户闭环。

### 1.2 是否应该立即删除大量代码

**不应该。** 当前正确顺序是：

```text
冻结横向扩张
→ 贯通 Pentest 热路径
→ 记录真实生产消费者
→ 收缩默认暴露面和启动路径
→ 删除被证明无消费者的代码
```

在热路径稳定前大规模删除，会同时增加 migration、旧数据、权限、恢复和回归风险。现阶段只允许删除当前切片中已经证明重复、不可达或错误的局部代码。

---

## 2. 当前真实进度

### 2.1 已完成且应复用的能力

以下能力已经存在，不得另建平行系统：

- Durable Run、Temporal Workflow、Runner、Execution、Terminal、取消与 Stop Proof；
- Engagement、授权引用、Scope、Approval、Credential Reference、Redaction；
- 按 RunKind 失败关闭的 Effect Policy；
- Browser、Target HTTP、HTTP Traffic、Web Research、MCP、原生 Code/Git Tool；
- Task Graph、Evidence Ledger、Reasoning Graph、Observer、Closure；
- Capability、Version、Digest、Provenance、Pack、Selection、Progressive Skill；
- Onboard、Doctor、SQLite migration、Backup/Restore、Pack repair；
- Official Pentest/Code Audit Packs；
- 本地脱敏 Pentest Demo 和 Code Audit Demo。

最近完整验证基线：

```text
5326 passed, 5 skipped, 17 warnings
Full Ruff passed
PEN-500 scoped mypy passed
Alembic single head: 7b3d1e5f9a24
```

这些结果证明底座稳定，不代表 Pentest 产品已经完成。

### 2.2 PEN-500 已完成

实现提交：

```text
86aaecdf  Pentest Admission 持久化
e2314e9b  Pentest Workflow/Runner identity
8b9ef440  Pentest Effect Policy 与 interactive guard
8f1b2554  专用 Pentest Admission 创建入口
33c863ea  Admission Capability Selection 原子绑定
```

当前已具备：

- `RunKind.PENTEST`、`PentestAdmission`、预算、禁止行为和硬停止条件；
- 明确的正向网络 Scope 与 Entry Point 校验；
- 专用 `POST /api/v1/pentests`；
- 普通 `POST /runs` 拒绝 `kind=pentest`，不能绕过 Admission；
- Engagement、Run、主 Session、事件、Selection、三类 allowlist 和 Pack locks 原子创建；
- 默认 `pentest-foundation`，也支持显式 Tool、Skill、Technique、Official Pack；
- Tool/Skill/Technique 固定快照，Pack 全成员版本锁；
- 空 Tool allowlist 失败关闭；
- Temporal 启动失败后，同一 `request_id` 保持同一语义身份并可恢复；
- Pentest Workflow、Runner、Signal、Effect 全链路保持 `pentest` 身份；
- 未审计的 Pentest 副作用继续失败关闭。

### 2.3 尚未完成

| 用户结果 | 状态 | 是否阻塞 V1 |
| --- | --- | --- |
| `riftx pentest start/status/resume/stop` | 当前切片 | 是 |
| Pentest status 权威聚合 | 未完成 | 是 |
| declared/observed Attack Surface 投影 | 未完成 | 是 |
| 隔离授权目标 E2E | 未完成 | 是 |
| 一个网络服务闭环 | 未完成 | 是 |
| 一个状态化 Web 身份/授权闭环 | 未完成 | 是 |
| Negative Result、Finding、Attack Chain、专业报告 | 部分底座已有，产品闭环未完成 | 是 |
| 一个 Operator Capability 成长闭环 | 未完成 | 是 |
| Code Audit 完全体 | 冻结 | 否 |
| CVE/PoC 自动研究 | 延后 | 否 |
| Marketplace、组织 Profile、远程集群、多租户 | V1 不做 | 否 |

### 2.4 当前阶段结论

RiftX 目前是“专业安全 Agent 底座较强，真实 Pentest 产品链路未完成”。后续开发不能继续以增加 Domain、Repository、Graph、Adapter、Pack 或 Agent 角色作为主要进度，必须以用户可执行结果为单位交付。

---

## 3. V1 边界与完成定义

### 3.1 V1 必须具备

最小入口：

```text
riftx onboard
riftx doctor
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
riftx pentest report
```

最小产品闭环：

```text
Admission
→ Recon / Enumeration
→ Attack Surface
→ Hypothesis
→ Minimal Verification
→ Evidence 或 Negative Result
→ Finding / Attack Chain
→ Report / Stop Proof
→ Review / Replay / Capability Promotion
```

### 3.2 V1 明确不做

- 通用 Agent 平台；
- Code Audit 完全体；
- 默认多 Agent 团队；
- Marketplace、在线 Registry、远程同步、远程集群、多租户；
- 组织级 Profile 和复杂权限体系；
- 为未来需求建立第二套 Run、Evidence、Selection、Pack、Graph 或 Attack Surface 数据库；
- 为证明“超过通用 Agent”建设排行榜或单一总分；
- 在 CLI 和真实 E2E 完成前继续扩展 UI；
- 在现有 Pack 未证明生产价值前继续增加 Official Pack 数量。

### 3.3 V1 发布完成门

只有同时满足以下条件，Pentest-first V1 才算完成：

1. 新用户通过 Onboard 和 Doctor 可启动真实授权 Pentest Run；
2. 模型、Provider、Profile 或配置文件错误能给出准确原因和修复命令，不能只显示 `Configured model not found`；
3. 所有目标交互都有 Scope、预算、Approval 和停止条件；
4. Run 可查询、恢复、取消、跨进程重读，并保持 Pentest 身份；
5. Browser、Target HTTP、Runner Tool 与至少一个专业扫描工具走生产 Runtime；
6. 一个网络服务场景和一个状态化 Web 场景完整走到报告；
7. 扫描信号、搜索结果和模型猜测不能直接成为 Confirmed Finding；
8. Task、Hypothesis、Attempt、Evidence、Negative Result、Finding、Selection 可跨重启恢复；
9. 取消、失败、超时、重启和人工停止具有可验证 Stop Proof；
10. 专业用户可以添加自己的 Tool、Skill 或 Technique；
11. 至少一个 Operator Capability 完成 Review、Replay、批准、生效、禁用和回滚；
12. 默认产品面不要求用户理解与 Pentest 无关的大量模块；
13. 发布检查覆盖功能、安全、migration、恢复和已知限制。

---

## 4. 优化原则

### 4.1 纵向结果优先

一个切片只有产生以下至少一个结果才算有效进度：

- 用户可以完成一个新的 Pentest 操作；
- 一个真实目标交互进入受控生产路径；
- 一个持久状态可跨进程恢复；
- 一条 Evidence/Negative Result/Finding 链可审查；
- 一个失败、停止或恢复场景被证明正确；
- 一个 Operator Capability 能被安全复用。

只增加模型、接口、空服务、空 CLI、Graph 节点或设计文档，不算完成。

### 4.2 复用现有事实系统

Pentest 必须复用现有：

- `Run`、`Engagement`、`Scope`、Approval；
- Temporal Workflow 与 Runner；
- Session、Execution、Artifact、Traffic；
- Task、Evidence、Reasoning、Observer、Closure；
- Capability、Pack、Selection、Progressive Skill；
- migration、Backup/Restore、Doctor。

只有现有系统无法表达一个当前 V1 用户结果时，才允许新增最小字段、查询或服务。

### 4.3 安全与恢复不能被“优化”掉

以下内容不是过度设计，禁止为了减少代码直接删除：

- 授权引用、Scope、Approval、Credential、Redaction；
- RunKind Effect Policy 与未知类型 fail-closed；
- Execution、Artifact、Evidence、Negative Result；
- Runner ownership、恢复、取消、Stop Proof；
- migration、Backup/Restore、旧数据兼容读取；
- Capability Version、Digest、Provenance、人工批准和回滚。

### 4.4 新增代码准入门

新增抽象、表、依赖或后台服务前必须回答：

1. 哪个当前 Pentest 用户流程不能由已有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会导致什么当前用户失败？
4. 能否改为一个现有 Service 方法、只读查询或确定性投影？
5. 是否扩大权限、migration、恢复和测试面积？

回答不具体时，不实现。

---

## 5. 唯一开发关键路径

```text
P1 CLI 与状态聚合（当前）
→ P2 Attack Surface 与 Pentest E2E
→ P3 网络服务专业闭环
→ P4 状态化 Web 与专业报告
→ P5 Operator Capability 成长闭环
→ P6 默认产品面收缩与代码删减
→ R1 发布检查
```

在当前阶段：

- 不扩 Planner、Scanner 数量、Pack 数量或 Agent 角色；
- 不开发 Marketplace、组织 Profile 或远程同步；
- 不做目录级删除和大规模重构；
- 不把 UI 当成 CLI/E2E 的前置条件；
- 不启动新的 Code Audit 里程碑。

---

## 6. P1：CLI 与 Pentest 状态聚合

### 6.1 用户结果

交付：

```text
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
```

CLI 只负责输入、调用和展示，不复制 Admission、Scope、Selection、Effect Policy 或停止规则。

### 6.2 最小实现路径

优先复用：

- `src/riftx/cli/app.py`；
- `src/riftx/cli/client.py`；
- `src/riftx/cli/render.py`；
- `src/riftx/api/routes/pentests.py`；
- 已有 `get_run`、`resume_run`、`cancel_run`、`get_run_metrics`；
- 已有 Run 控制与 General+Pentest Effect Policy；
- 已有授权 Run 读取依赖。

`resume` 和 `stop` 应优先调用现有 Run 控制服务。若 CLI 需要先校验类型，只读取持久 `kind=pentest`；不得复制控制逻辑。

`status` 需要一个服务端只读聚合，避免 CLI 拼接多个易失请求。它不是第二套状态数据库，只允许从现有持久事实读取或确定性计算。

建议的最小响应：

```text
run
admission
primary_session
capabilities:
  selections
  allowlists
budget:
  limits
  elapsed_seconds
  model_calls
  tokens
  tool_calls
  observed_target_interactions
workflow:
  workflow_id
  persisted_started
runner:
  execution_status_counts
  node_ids
stop:
  latest_event_type
  confirmed
  workflow_synced
  failed_resource_types
attack_surface:
  declared_entry_points
```

计数只能陈述数据库已持久化的观察值；不能把不完整的事件投影宣称为完整计费或完整目标交互账单。

### 6.3 P1 明确不做

- 不新建 Selection 表；
- 不在 Run JSON 中复制 Capability manifest；
- 不为 Model 虚构尚无消费者的版本仓；
- 不新建 Pentest 控制服务副本；
- 不在 CLI 中执行 Scope 或权限判定；
- 不为 status 引入事件流平台、缓存层或新的后台 Worker。

### 6.4 P1 验收

- `start` 调用专用 Pentest API；
- 普通 `run create --kind pentest` 仍失败；
- `status` 展示 Admission、Selection、预算、Workflow、Runner、Stop 与 declared entry points；
- 对非 Pentest Run 调用专用命令时明确拒绝；
- `resume`、`stop` 使用已有权威控制路径；
- API 重启后状态可重读；
- 请求失败、404、409、未授权和服务不可用有清晰 CLI 错误；
- CLI/API 合同测试、失败测试和跨进程读取测试通过。

---

## 7. P2：Attack Surface 与真实 Pentest E2E

### 7.1 Attack Surface 最小模型

P2 不新建 Attack Surface 事实数据库。先提供两个投影：

1. **declared**：由 Admission Entry Points、Scope 和 exclusions 确定性重建；
2. **observed**：由 Traffic、Execution、Artifact、Evidence 中已经持久化的目标事实投影。

只需要支持当前场景使用的类型：

- asset；
- service；
- endpoint；
- parameter。

每个节点至少包含：规范化值、来源等级、Scope decision、来源对象 ID。`verified` 必须引用 Evidence，不能由扫描结果或模型判断直接产生。

### 7.2 隔离授权 E2E

在可复位靶场证明：

- 无授权引用、无正向 Scope、无 Entry Point 或 Entry Point 越界时拒绝创建；
- Run 可启动、查询、恢复、停止和跨进程重读；
- Workflow、Runner、Artifact、Tool Intent 全程保持 Pentest 身份；
- Scope 外 DNS/HTTP/Browser/Runner 副作用在执行前失败关闭；
- 超预算、取消、失败和停止留下可验证事实；
- declared/observed Attack Surface 可从持久数据重建。

### 7.3 P2 完成门

PEN-500 只有在上述 E2E 通过后才能标记 completed。不能因为 Domain、API 或单元测试齐全就提前完成。

---

## 8. P3：一个网络服务专业闭环

只选择一个可复位、明确授权的网络服务靶场，贯通：

```text
目标解析
→ 可达性
→ 端口/服务发现
→ 版本线索
→ Hypothesis
→ 最小验证
→ Evidence 或 Negative Result
→ Finding
```

实现要求：

- 优先复用 Runner Tool、MCP、Execution、Artifact、Evidence 和现有 Pack；
- 可选扫描器缺失时允许降级，但报告必须说明未执行能力；
- 扫描结果只生成线索，不能直接生成 Confirmed Finding；
- 验证动作必须记录前置条件、风险、Approval、正/负判据和 Evidence capture；
- 失败尝试必须形成 Negative Result，不能只留在聊天中；
- 同一目标的重复动作要受到预算和 Observer 约束；
- 至少验证一次暂停、恢复、取消或工具故障后的状态恢复。

P3 不新增扫描框架，只接通一个真实生产工具路径。

---

## 9. P4：状态化 Web、报告与停止证明

### 9.1 一个状态化 Web 场景

选择一个包含登录、角色和对象授权的可复位靶场，完成：

- Browser、Target HTTP、Traffic 使用统一的 Request/Session identity；
- Cookie、Token 只通过 Secret Reference 使用；
- 登录、角色、会话和请求状态可恢复；
- 请求/响应 Diff、重放和最小化；
- 人工接管后生成 Takeover Summary；
- 身份或状态变化造成的响应差异形成 Evidence；
- 越界 URL、重定向和子资源继续执行 Scope 检查。

### 9.2 最小验证语义

复用 Task/Reasoning Graph，只补当前场景确实需要的字段或关系：

- Hypothesis；
- prerequisite；
- minimal action；
- positive/negative criterion；
- risk/approval；
- evidence capture；
- stop condition；
- retry/variant relation。

不要另建 Pentest Planner、Attack Graph 数据库或常驻多 Agent 团队。

### 9.3 专业报告

交付 `riftx pentest report`，至少包含：

- Engagement、授权、Scope、Admission、Selection；
- Attack Surface 与 Coverage；
- Finding、影响、Evidence、复现和修复建议；
- Negative Result、限制、阻断点和未完成项；
- Attack Chain 的已确认段、假设段和前置条件；
- 取消、失败、超时、重启、人工停止后的 Stop Proof。

报告生成只读取权威事实，不能从最后一段模型文本倒推结果。

---

## 10. P5：Operator Capability 成长闭环

V1 只证明一个真实能力的完整成长过程：

```text
Sanitized Trajectory
→ Post-run Review
→ Success/Failure Classification
→ Capability Candidate
→ Original + Variant + Negative + Regression Replay
→ Human Approve/Reject
→ Activate
→ Disable/Rollback
```

### 10.1 最小实现边界

- 复用现有 Capability、Candidate、Version、Digest、Provenance、Selection 和 Pack Lock；
- Trajectory 只保存脱敏、结构化、可检索的事实；
- 先使用现有数据库和 FTS，不引入第二套向量数据库；
- Review/Replay 默认离线，不能调用真实目标交互工具；
- Candidate 不能自动变成 Active；
- 新 Skill/Technique 不能扩大 Scope、降低 Approval 或获得未授权 Tool；
- 用户可以查看版本差异、启用、禁用和回滚；
- 不建设 Marketplace、组织 Profile 或自动发布。

### 10.2 可以沉淀

- 可重复的验证步骤；
- 特定框架、设备或协议的方法；
- 工具参数、输出解析和证据要求；
- 常见失败后的替代路径；
- 报告和复盘规则。

### 10.3 禁止沉淀

- 目标秘密、凭据或未脱敏数据；
- 未验证猜测和一次性偶然成功；
- 大段原始聊天；
- 绕过 Scope 或降低 Approval 的指令；
- 本应由确定性代码完成的脆弱文本解析。

P5 的目标不是自动“自我进化”，而是把专业人士认可的方法安全地变成下一次可复用能力。

---

## 11. P6：项目级收缩与代码删减

### 11.1 现在执行

- 冻结 Code Audit、Marketplace、多租户、远程集群、多 Agent 新功能；
- 禁止继续增加 Official Pack 数量；
- 暂停新 UI 功能；
- 默认帮助、文档和启动流程优先 Pentest 主路径；
- 普通切片只运行目标测试和受影响回归，不重复运行 5000+ 全仓测试；
- 只清理当前触及模块中的重复、错误命名和不可达分支；
- 新模块必须有当前生产消费者。

### 11.2 热路径稳定后执行

建立“模块 → 生产消费者 → 启动成本 → 数据兼容 → 安全价值”清单，然后按顺序处理：

```text
收缩默认 CLI/API/UI 暴露面
→ 改为按需加载
→ 测量启动时间、内存和依赖
→ 隔离可选模块
→ 删除无消费者代码
→ migration/恢复/全仓回归
```

优先审计候选：

- 默认 CLI 命令面和 API routes；
- Worker Runtime eager 初始化；
- `runtime/control_tools.py` 中 Pentest 不使用的控制工具；
- Code Audit 专属 Runtime、preflight、snapshot、source materialization；
- 未进入真实 E2E 的 Connector、Adapter、Demo、Pack 或 UI 页面；
- 只被测试引用、没有产品入口的辅助层；
- 语义重复的 Effect Policy 清单和旧命名。

这些只是审计候选，不是预先批准的删除清单。

### 11.3 删除准入门

生产代码只有同时满足以下条件才允许删除：

1. 没有 Pentest 热路径消费者；
2. 没有默认 CLI/API/UI 消费者；
3. 不是 migration 或旧数据兼容读取所需；
4. 不是安全、审计、恢复、Evidence 或 Provenance 所需；
5. 没有受支持用户数据依赖；
6. 可选功能已有禁用、导出或升级路径；
7. 目标测试、migration 回归和 milestone gate 通过。

Migration 历史不得删除或重写。删除前先收缩入口和按需加载，确认无真实消费者后再删代码。

---

## 12. 验证与 Git 纪律

### 12.1 分层验证

| Gate | 触发条件 | 最小要求 |
| --- | --- | --- |
| Slice | 每个实现提交 | 目标测试、受影响回归、Ruff、必要的 mypy/typecheck、`git diff --check` |
| Task | 一个用户结果完成 | API/CLI 合同、持久化、权限、失败、恢复、跨进程读取 |
| Milestone | P1-P6 或高风险边界完成 | 全仓 Python、相关前端/桌面 build、migration/release checks |
| Release | 发布候选 | 两个真实靶场、升级恢复、安全评审、已知限制 |

所有 Agent 相关测试和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

安全路径、migration、Runner ownership、Effect Policy 和停止证明不能因为测试耗时而跳过 milestone gate。

### 12.2 Git 纪律

- 一个实现提交只表达一个用户可解释或安全可验证的结果；
- 实现提交与实施账本提交分开；
- Task 完成后更新 `FORMAL_AGENT_PROGRESS.md`；
- 不提交无关用户改动；
- 不用破坏性 reset/checkout 清理工作树；
- 提交前检查 staged diff 和 `git diff --cached --check`；
- 每个切片先保证可回滚，再进入下一个切片。

### 12.3 不使用“代码量”作为进度

进度只按以下证据判断：

- 用户命令或 API 能否完成；
- 状态是否持久、可读、可恢复；
- 安全边界是否在真实副作用前执行；
- Evidence 是否可追溯；
- 失败和停止是否可证明；
- 能力是否经过 Replay、批准和回滚。

---

## 13. Codex 逐步执行协议

Codex 每轮只处理一个最小纵向切片。开始前输出或在内部明确：

```text
Current phase:
Pentest user outcome:
Existing production path to reuse:
Smallest implementation slice:
Files expected to change:
Explicit non-goals:
Scope/Approval impact:
Persistence/migration impact:
Evidence/recovery behavior:
Target tests:
Implementation commit:
Ledger commit:
```

执行顺序：

1. 读取当前文档、实施账本、相关 ADR 和当前工作树；
2. 从 CLI/API 入口追踪到持久化和副作用，先找已有实现；
3. 写出最小用户结果和明确非目标；
4. 只修改实现该结果需要的文件；
5. 先跑目标测试，再跑受影响回归；
6. 通过后提交实现；
7. Task 级结果完成后，单独更新实施账本并提交；
8. 下一轮从未完成的第一个验收条件继续。

遇到以下情况必须停止扩张并重新审查：

- 正在为 frozen/post-V1 范围新增功能；
- 新增抽象没有当前生产消费者；
- 新表复制已有权威事实；
- 需要同时修改大量无关模块才能完成一个小功能；
- 单元测试通过但没有真实用户路径；
- 为未来可能性阻塞当前 Pentest E2E。

---

## 14. 下一步施工指令

当前唯一允许开始的开发切片是：

> **交付 `riftx pentest start/status/resume/stop`，以及只读 Pentest status 聚合。**

推荐拆分为两个实现提交：

1. Pentest status 服务端只读聚合、API 合同与测试；
2. CLI `start/status/resume/stop` 薄适配、渲染与失败测试。

完成后：

1. 运行 P1 目标与受影响回归；
2. 提交实现；
3. 单独更新实施账本；
4. 进入 declared Attack Surface；
5. 完成隔离授权目标 E2E；
6. 只有 E2E 通过后才将 PEN-500 标记 completed。

在此之前，不启动 Code Audit、学习系统、Marketplace、更多 Pack、更多 Scanner、UI 扩展或大规模代码删除。

---

## 15. 兼容账本索引

本节只用于保持 ADR、migration、历史提交和实施账本可对账，不是后续开发路线。任务的开发处置以第 5 节 P1-P6 主线为准；`frozen`、`deferred`、`post-V1` 项不得因为出现在本节而自动启动。

### SEC-000：正式版 ADR 与实施账本
**依赖**：无。已完成，只维护权威文档一致性。

### SEC-001：Security Capability Evaluation 骨架
**依赖**：SEC-000。已完成，只为真实 Pentest 增加必要 Fixture/Replay。

### CAP-001：Capability Domain 与持久化
**依赖**：SEC-000。已完成，P5 直接复用。

### CAP-100：接通生产 Progressive Skill
**依赖**：CAP-001。已完成，由 P5 证明真实价值。

### CAP-101：原生代码工具
**依赖**：CAP-001。已完成，只支持当前安全工作流，不扩通用 IDE。

### CAP-102：Browser/Web/Traffic Tool 闭环
**依赖**：CAP-001。已完成，P1-P4 直接复用。

### CAP-103：MCP 生产接入
**依赖**：CAP-001。已完成，只接入真实使用的专业工具。

### CAP-104：持久化 Tool/Skill Selection
**依赖**：CAP-100、CAP-103。已完成，Pentest Admission 已接入。

### COG-200：Task Graph
**依赖**：CAP-104。已完成，不建立 Pentest 平行 Planner。

### COG-201：Evidence Ledger
**依赖**：COG-200。已完成，P3-P4 直接复用。

### COG-202：Reasoning Graph
**依赖**：COG-201。已完成，只补真实场景需要的语义。

### COG-203：Primary Agent Proposal Tools
**依赖**：COG-202。已完成，不再扩 Proposal 类型。

### COG-204：Observer Supervisor 与 Projector
**依赖**：COG-203。已完成，重点验证 Scope、预算、重复与证据门。

### COG-205：Closure Verifier
**依赖**：COG-204。已完成，专业报告直接复用。

### PACK-300：基础渗透 Packs
**依赖**：CAP-102、CAP-104、COG-205。已完成并冻结数量。

### PACK-301：基础代码审计 Packs
**依赖**：CAP-101、CAP-104、COG-205。已完成并冻结，只保留兼容。

### PACK-302：Onboard 和 Doctor
**依赖**：PACK-300、PACK-301。已完成，只修真实用户阻断。

### AUD-400：Repository Intelligence
**依赖**：CAP-101、COG-202。冻结，不阻塞 V1。

### AUD-401：Scanner Adapter
**依赖**：AUD-400。冻结，Pentest Scanner 走已有 Tool/MCP。

### AUD-402：专业角色工作流
**依赖**：AUD-400、AUD-401、COG-205、PACK-301。冻结，不实现常驻审计 Agent 团队。

### AUD-403：代码证据模型
**依赖**：COG-201、AUD-400、AUD-401。冻结，只保留兼容。

### AUD-404：Diff Audit 与 Variant Analysis
**依赖**：AUD-400、AUD-403。冻结，不阻塞 V1。

### AUD-405：受控动态验证
**依赖**：CAP-101、AUD-403。冻结，不增加未知代码默认执行入口。

### PEN-500：Pentest Admission 与 Attack Surface
**依赖**：CAP-102、COG-202。进行中，对应 P1-P2。

### PEN-501：状态化 Web 测试
**依赖**：CAP-102、PEN-500。待完成，对应 P4。

### PEN-502：验证规划器
**依赖**：COG-203、PEN-500、PEN-501。待完成，只补 P3-P4 最小验证语义。

### PEN-503：CVE/PoC Research
**依赖**：CAP-102、PEN-502。延后，不阻塞 V1。

### PEN-504：Attack Chain、Report 与 Stop Proof
**依赖**：COG-201、PEN-500、PEN-502。待完成，对应 P4。

### LEARN-600：Trajectory Store 与 Session Search
**依赖**：COG-205。待完成，对应 P5，优先使用现有数据库和 FTS。

### LEARN-601：Post-run Review
**依赖**：LEARN-600。待完成，只产出 Candidate。

### LEARN-602：Failure Taxonomy
**依赖**：LEARN-601。待完成，只覆盖真实运行中出现的失败。

### LEARN-603：Replay Lab
**依赖**：SEC-001、LEARN-601、LEARN-602。待完成，只验证一个 Operator Capability。

### LEARN-604：Capability Curator
**依赖**：CAP-001、LEARN-603。待完成，交付人工批准、激活、禁用和回滚。

### LEARN-605：Profile、导入和迁移
**依赖**：LEARN-604、PACK-302。延后，V1 不建设组织和远程同步。

### EVAL-700：代码审计语料
**依赖**：SEC-001、AUD-403、AUD-404。冻结。

### EVAL-701：渗透测试靶场
**依赖**：SEC-001、PEN-504。待完成，只固化 P3-P4 两个场景。

### EVAL-702：版本、配置与能力包回归 Harness
**依赖**：EVAL-701、LEARN-603。待完成，只比较 RiftX 自身变化。

### EVAL-703：质量与安全发布检查
**依赖**：EVAL-702、PACK-302。待完成，作为内部发布门。

### ECO-800：Pack SDK
**依赖**：CAP-001、LEARN-604。Post-V1。

### ECO-801：信任与供应链
**依赖**：ECO-800。Post-V1。

### ECO-802：Gateway 与持续运行
**依赖**：LEARN-605、ECO-801。Post-V1。

---

## 16. 最终定位

RiftX 不应继续成长为一个“功能很多但主路径不完整”的通用 Agent 平台。它的正式版应当是：

> 一个知道授权边界、能够持续执行和恢复、会记录证据与失败、能形成专业报告，并能把操作者方法论沉淀为可审查生产能力的渗透测试工作台。

实现这一目标的最短路径不是继续增加架构，而是完成一条真实 Pentest 闭环，再从真实使用中决定保留、优化和删除什么。
