# RiftX 正式版开发优化文档

> 文档定位：RiftX 当前阶段唯一的产品收敛、项目优化与开发完成指南
>
> 适用对象：Codex、RiftX 开发者、专业渗透测试用户
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 当前已提交基线：`c7a79e87`
>
> 实施事实与测试账本：[`docs/implementation/FORMAL_AGENT_PROGRESS.md`](docs/implementation/FORMAL_AGENT_PROGRESS.md)
>
> 平台边界：[`ADR-0012`](docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md)
>
> Pentest Admission 与 Attack Surface：[`ADR-0013`](docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md)

---

## 1. 产品目标

RiftX 正式版只兑现一个核心结果：

> **成为专业人士手中真正好用，并且能随着 Tool、Skill、Technique、Playbook 和实践经验持续成长的授权渗透测试 Agent。**

产品同时具备两个属性：

1. **开箱即用的即战力**：新用户完成 Onboard 和 Doctor 后，可以在明确授权和明确 Scope 下启动基础 Pentest，查看状态、恢复、停止并获得可审查结果。
2. **专业用户可持续抬高上限**：用户能够加入自己的工具、技能、验证方法和复盘经验；RiftX 能对这些能力进行版本化、选择、审查、回放、启用、禁用和回滚。

“超过直接使用 Codex、Claude Code、OpenCode”是长期追求，不是 V1 必须量化证明的发布条件。RiftX 应建立的是通用 Agent 难以默认提供的领域复利：

- 持久、可恢复的专业任务状态；
- 授权、Scope、Approval、预算和停止证明；
- Attack Surface、Hypothesis、Attempt、Evidence、Negative Result、Finding 与 Attack Chain；
- 可组合、可追溯、可回滚的专业能力；
- 把操作者认可的方法沉淀为下一次可复用能力，而不是只保存聊天记录。

RiftX 不再以“功能最多的安全 Agent 平台”为目标，也不建设用于证明超过通用 Agent 的单一排行榜。

---

## 2. 当前判断：底座较强，产品闭环仍未完成

### 2.1 是否已经过度开发

**存在阶段性过度开发，但不应立即大规模删代码。**

当前项目已经拥有大量平台能力、兼容面、migration 和测试，但用户最需要的完整 Pentest 热路径仍没有跑通。问题不在于安全边界“做得太多”，而在于横向平台建设曾经快于纵向用户结果。

正确处理顺序是：

```text
冻结横向扩张
→ 完成真实 Pentest 热路径
→ 用生产消费者证明模块价值
→ 收缩默认暴露面和启动路径
→ 隔离可选模块
→ 删除被证明无消费者的代码
```

在真实热路径稳定前进行目录级删除，会同时放大 migration、旧数据、恢复、权限和回归风险。当前只删除正在修改范围内已经证明重复、错误或不可达的局部代码。

### 2.2 已完成且必须复用

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

最近已记录的验证基线：

```text
5326 passed, 5 skipped, 17 warnings
Full Ruff passed
PEN-500 scoped mypy passed
Alembic single head: 7b3d1e5f9a24
P1 affected regression: 283 passed
```

这些结果证明底座和 P1 切片稳定，不代表 Pentest V1 已经完成。

### 2.3 PEN-500 已交付部分

已提交实现：

```text
86aaecdf  Pentest Admission 持久化
e2314e9b  Pentest Workflow/Runner identity
8b9ef440  Pentest Effect Policy 与 interactive guard
8f1b2554  专用 Pentest Admission 创建入口
33c863ea  Admission Capability Selection 原子绑定
70f6f4a0  Pentest status 权威聚合与 API
2271bc8e  pentest start/status/resume/stop CLI
c7a79e87  P1 文档与账本收口
```

已经具备：

- `RunKind.PENTEST`、`PentestAdmission`、预算、禁止行为和硬停止条件；
- 明确的授权引用、正向网络 Scope 和 Entry Point 校验；
- 专用 `POST /api/v1/pentests`，普通 `POST /runs` 不能绕过 Admission；
- Engagement、Run、主 Session、事件、Selection、allowlist 和 Pack lock 原子创建；
- 默认 `pentest-foundation`，并支持显式 Tool、Skill、Technique、Official Pack；
- Tool/Skill/Technique 固定快照和 Pack 成员版本锁；
- Temporal 启动失败后的稳定 `request_id` 恢复语义；
- Workflow、Runner、Signal、Effect 全链路保持 Pentest 身份；
- `GET /api/v1/pentests/{run_id}/status` 可从持久事实重建 Admission、Selection、预算、Workflow、Runner、Stop 与 declared entry points；
- `riftx pentest start/status/resume/stop` 已交付；
- 未审计的 Pentest 副作用继续失败关闭。

### 2.4 当前工作树中的进行中切片

当前工作树存在尚未提交、尚未完成验收的 P2 Attack Surface 实现，涉及：

```text
src/riftx/application/ports/pentests.py
src/riftx/application/ports/__init__.py
src/riftx/application/services/pentests.py
src/riftx/application/services/pentest_attack_surface.py
src/riftx/persistence/pentest_status.py
src/riftx/api/schemas/pentests.py
src/riftx/cli/render.py
```

当前实现方向：

- declared：由 Admission Entry Point 与 Scope 确定性生成；
- observed：由持久化 Target HTTP Request 生成；
- verified：只由特定 Evidence 引用生成；
- 节点仅支持 `asset/service/endpoint/parameter`；
- URL 参数只保留参数名，不保存参数值；
- 每个节点包含规范化值、来源等级、Scope decision 和来源对象引用；
- 相同节点合并，来源等级为 `verified > observed > declared`；
- Scope denied 不能被允许结果覆盖；
- 投影有容量上限和 `truncated` 标记；
- 不新增 Attack Surface 表。

该切片当前只能标记为 **in progress**。完成 Ruff、mypy、投影单元测试、API/CLI 集成测试和重启重建测试之前，不能写入已完成清单。

### 2.5 当前缺失的用户结果

| 用户结果 | 当前状态 | V1 是否需要 |
| --- | --- | --- |
| Pentest Admission 与控制命令 | 已完成 | 是 |
| 权威 Pentest status | 已完成 | 是 |
| declared/observed/verified Attack Surface 投影 | 实现中，未验收 | 是 |
| 隔离授权目标生命周期 E2E | 未完成 | 是 |
| 一个网络服务专业闭环 | 未完成 | 是 |
| 一个状态化 Web 身份/授权闭环 | 未完成 | 是 |
| Negative Result、Finding、Attack Chain、专业报告 | 底座部分已有，产品闭环未完成 | 是 |
| 一个 Operator Capability 成长闭环 | 未完成 | 是 |
| 默认产品面收缩与无消费者代码审计 | 未开始 | 是 |
| Code Audit 完全体 | 冻结 | 否 |
| CVE/PoC 自动研究 | 延后 | 否 |
| Marketplace、多租户、远程集群 | Post-V1 | 否 |

按交付阶段判断，而不是按代码量判断：**P1 已完成，P2 正在进行，P3、P4、P5、P6 和 R1 尚未完成。**

---

## 3. 优化边界：保留、冻结、删除

### 3.1 必须保留

以下能力是专业安全 Agent 的核心，不属于可随意删除的“过度设计”：

- 授权引用、Scope、Approval、Credential、Redaction；
- RunKind Effect Policy 与未知类型 fail-closed；
- Execution、Artifact、Traffic、Evidence、Negative Result；
- Runner ownership、恢复、取消、Stop Proof；
- migration、Backup/Restore、旧数据兼容读取；
- Capability Version、Digest、Provenance、Selection、人工批准和回滚；
- 能被 Pentest 热路径直接消费的 Browser、HTTP、MCP、Runner 和 Graph 能力。

### 3.2 立即冻结

在 R1 前不新增：

- Code Audit 新里程碑；
- Marketplace、在线 Registry、组织 Profile、远程同步、多租户；
- 常驻多 Agent 团队和新的 Agent 角色；
- 新 Planner、第二套 Graph、第二套 Run/Evidence/Selection/Pack 存储；
- 更多 Official Pack 和更多 Scanner；
- 与 CLI/E2E 无关的新 UI；
- 为未来可能需求建立的抽象、表、后台服务和兼容层。

冻结不是删除。安全修复、数据兼容和现有用户阻断问题仍可处理。

### 3.3 何时允许删除

生产代码只有同时满足以下条件才允许删除：

1. 没有 Pentest 热路径消费者；
2. 没有默认 CLI/API/UI 消费者；
3. 不是 migration 或旧数据兼容读取所需；
4. 不是安全、审计、恢复、Evidence 或 Provenance 所需；
5. 没有受支持用户数据依赖；
6. 可选功能已有禁用、导出或升级路径；
7. 目标测试、migration 回归和 milestone gate 通过。

Migration 历史不得删除或重写。优先收缩入口和改为按需加载，确认无消费者后再删除实现。

---

## 4. V1 完成定义

### 4.1 最小用户入口

```text
riftx onboard
riftx doctor
riftx pentest start
riftx pentest status
riftx pentest resume
riftx pentest stop
riftx pentest report
```

### 4.2 最小专业闭环

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

### 4.3 发布完成门

只有以下条件同时成立，Pentest-first V1 才算完成：

1. 新用户通过 Onboard 和 Doctor 可启动真实授权 Pentest Run；
2. Model、Provider、Profile 或配置错误能指出准确原因和修复动作，不能只显示 `Configured model not found`；
3. 所有目标交互都受到 Scope、预算、Approval 和停止条件约束；
4. Run 可查询、恢复、取消、跨进程重读，并始终保持 Pentest 身份；
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

## 5. 唯一开发关键路径

| 阶段 | 状态 | 必须交付的用户结果 |
| --- | --- | --- |
| P1 CLI 与状态聚合 | completed | `start/status/resume/stop` 和可重建状态 |
| P2 Attack Surface 与隔离 E2E | in progress | 可重建投影和完整 Pentest 生命周期 |
| P3 网络服务专业闭环 | pending | Recon 到 Evidence/Negative Result/Finding |
| P4 状态化 Web 与专业报告 | pending | 身份/授权验证、Attack Chain、Report、Stop Proof |
| P5 Operator Capability 成长 | pending | 一项真实能力的 Review/Replay/批准/回滚 |
| P6 默认产品面收缩 | pending | 按消费者审计、按需加载和安全删减 |
| R1 发布检查 | pending | 两个靶场、恢复、安全和发布门通过 |

除非当前阶段满足完成门，否则不得启动下一阶段的扩展功能。

---

## 6. P2：完成 Attack Surface 与隔离授权 E2E

### 6.1 P2-A：先完成当前 Attack Surface 切片

最小事实来源固定为：

| 来源等级 | 当前权威来源 | 说明 |
| --- | --- | --- |
| declared | Admission Entry Point + Scope | 用户声明，不代表已观察 |
| observed | Target HTTP Request | 已发生并持久化的目标交互 |
| verified | 指定 Evidence 的 `target_refs` | 必须有 Evidence 引用 |

当前切片不读取 Execution 或 Artifact 来扩展 Attack Surface。只有真实场景证明 Target HTTP 与 Evidence 不足时，才增加一个现有持久来源。

实现约束：

- 投影是确定性只读计算，不新建表、不建缓存、不建后台 Worker；
- 节点只实现 `asset/service/endpoint/parameter`；
- URL 规范化默认端口，参数去值，只保存参数名；
- IP、Domain、CIDR 生成 asset；
- 节点去重，来源对象 ID 去重排序；
- `verified > observed > declared`，但 denied Scope 决策最高优先；
- 数据异常失败关闭，容量上限必须显式返回 `truncated`；
- status/API/CLI 只展示投影，不复制业务规则。

最小测试：

- URL、IP、CIDR、Domain 规范化；
- 默认 HTTP/HTTPS 端口和参数去值；
- 节点去重与来源等级升级；
- Scope allowed/excluded/denied；
- source ref 去重和 truncation；
- declared API 响应；
- Target HTTP 形成 observed；
- 合法 Evidence 形成 verified；
- 非认可 Evidence 不得升级；
- Control Plane 重启后重建相同投影；
- CLI 渲染新结构且不暴露参数值。

P2-A 通过后形成一个独立实现提交，不同时修改实施账本。

### 6.2 P2-B：固化隔离授权生命周期 E2E

在可复位、明确授权的本地靶场证明：

- 无授权引用、无正向 Scope、无 Entry Point 或 Entry Point 越界时拒绝创建；
- Run 可创建、查询、恢复、停止和跨进程重读；
- Workflow、Runner、Artifact、Tool Intent 全程保持 Pentest 身份；
- Scope 外 DNS/HTTP/Browser/Runner 副作用在执行前失败关闭；
- 超预算、取消、失败和人工停止留下可验证事实；
- declared/observed/verified Attack Surface 可从持久数据重建；
- 测试结束后靶场可复位，不依赖公网和未授权目标。

P2-B 通过后形成独立实现提交，再单独更新实施账本。Attack Surface 和隔离 E2E 同时通过后，PEN-500 才能标记 `completed`。

---

## 7. P3：一个网络服务专业闭环

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
- 只接通一个真实专业工具路径，不新增扫描框架；
- 可选扫描器缺失时允许降级，但报告必须说明未执行能力；
- 扫描结果只能生成线索，不能直接生成 Confirmed Finding；
- 验证动作记录前置条件、风险、Approval、正/负判据和 Evidence capture；
- 失败尝试形成 Negative Result，不能只留在聊天中；
- 重复动作受到预算和 Observer 约束；
- 至少证明一次暂停、恢复、取消或工具故障后的状态恢复。

P3 完成门是“一个专业网络服务闭环可重复运行”，不是 Scanner、Planner 或 Pack 数量。

---

## 8. P4：状态化 Web、Attack Chain 与报告

### 8.1 一个状态化 Web 场景

选择一个包含登录、角色和对象授权的可复位靶场，完成：

- Browser、Target HTTP、Traffic 使用统一的 Run/Session/Request identity；
- Cookie、Token 只通过 Secret Reference 使用；
- 登录、角色、会话和请求状态可恢复；
- 请求/响应 Diff、重放和最小化；
- 人工接管后生成 Takeover Summary；
- 身份或状态变化造成的响应差异形成 Evidence；
- 越界 URL、重定向和子资源继续执行 Scope 检查。

### 8.2 最小验证语义

复用现有 Task/Reasoning Graph，只补真实场景缺少的最小字段或关系：

- Hypothesis；
- prerequisite；
- minimal action；
- positive/negative criterion；
- risk/approval；
- evidence capture；
- stop condition；
- retry/variant relation。

不另建 Pentest Planner、Attack Graph 数据库或常驻多 Agent 团队。

### 8.3 专业报告

交付 `riftx pentest report`，至少包含：

- Engagement、授权、Scope、Admission、Selection；
- Attack Surface 与 Coverage；
- Finding、影响、Evidence、复现和修复建议；
- Negative Result、限制、阻断点和未完成项；
- Attack Chain 的已确认段、假设段和前置条件；
- 取消、失败、超时、重启、人工停止后的 Stop Proof。

报告只读取权威事实，不能从最后一段模型文本倒推结果。

---

## 9. P5：兑现“越用越好用”

V1 只证明一个真实 Operator Capability 的完整成长过程：

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

最小边界：

- 复用现有 Capability、Candidate、Version、Digest、Provenance、Selection 和 Pack Lock；
- Trajectory 只保存脱敏、结构化、可检索的事实；
- 先使用现有数据库和 FTS，不引入第二套向量数据库；
- Review/Replay 默认离线，不能调用真实目标交互工具；
- Candidate 不能自动成为 Active；
- 新能力不能扩大 Scope、降低 Approval 或获得未授权 Tool；
- 用户能查看版本差异、启用、禁用和回滚；
- 不建设 Marketplace、组织 Profile 或自动发布。

允许沉淀：可重复验证步骤、特定框架/设备/协议方法、工具参数与输出解析、证据要求、失败替代路径、报告与复盘规则。

禁止沉淀：目标秘密、凭据、未脱敏数据、未验证猜测、一次性偶然成功、大段原始聊天、绕过 Scope/Approval 的指令，以及本应由确定性代码完成的脆弱文本解析。

P5 的完成门不是“自我进化”，而是专业人士认可的一项方法能安全影响下一次运行，并且可以回滚。

---

## 10. P6：项目优化与安全删减

P2-P5 形成稳定生产消费者后，建立以下清单：

| 模块 | Pentest 消费者 | 默认入口 | 启动成本 | 数据兼容 | 安全价值 | 处置 |
| --- | --- | --- | --- | --- | --- | --- |
| 待审计 | CLI/API/Worker/E2E 引用 | 默认/可选/无 | 实测 | migration/历史数据 | 必需/可选/无 | 保留/按需加载/隔离/删除 |

处理顺序：

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
- Pentest 不使用的控制工具；
- Code Audit 专属 Runtime、preflight、snapshot 和 source materialization；
- 未进入真实 E2E 的 Connector、Adapter、Demo、Pack 或 UI 页面；
- 只被测试引用、没有产品入口的辅助层；
- 语义重复的 Effect Policy 清单和旧命名。

这些是审计候选，不是预先批准的删除清单。每次删减必须单独提交，并证明默认 Pentest 主路径、migration、恢复和安全边界未退化。

---

## 11. R1：发布检查

R1 至少包含：

- 全新环境 Onboard、Doctor、配置错误诊断；
- P2 隔离生命周期 E2E；
- P3 网络服务场景；
- P4 状态化 Web 场景；
- P5 Operator Capability 回放和回滚；
- Scope、Approval、预算、凭据、Redaction、Effect Policy 安全检查；
- 取消、失败、超时、重启和 Stop Proof；
- migration upgrade/downgrade 边界、Backup/Restore；
- 默认 CLI/API 产品面与安装包资产；
- 已知限制、可选工具缺失和降级行为。

评测用于 RiftX 自身的回归、复现、发布检查和能力演进。可以同时使用定性复盘和定量指标，但不要求用单一分数证明超过通用 Agent。

---

## 12. 开发准入规则

任何新增抽象、表、依赖或后台服务前必须回答：

1. 哪个当前 Pentest 用户流程不能由已有组件完成？
2. 第一个生产消费者是谁？
3. 不实现会导致什么当前用户失败？
4. 能否改为一个现有 Service 方法、只读查询或确定性投影？
5. 是否扩大权限、migration、恢复和测试面积？

答案不具体时，不实现。

一个切片只有产生以下至少一个结果才算有效进度：

- 用户可以完成一个新的 Pentest 操作；
- 一个真实目标交互进入受控生产路径；
- 一个持久状态可跨进程恢复；
- 一条 Evidence/Negative Result/Finding 链可审查；
- 一个失败、停止或恢复场景被证明正确；
- 一个 Operator Capability 能被安全复用。

只增加 Domain、Repository、Graph、Adapter、空 CLI、空服务或设计文档，不算完成。

---

## 13. 验证与 Git 纪律

### 13.1 分层验证

| Gate | 触发条件 | 最小要求 |
| --- | --- | --- |
| Slice | 每个实现提交 | 目标测试、受影响回归、Ruff、必要 mypy/typecheck、`git diff --check` |
| Task | 一个用户结果完成 | API/CLI 合同、持久化、权限、失败、恢复、跨进程读取 |
| Milestone | P2-P6 完成 | 全仓 Python、相关前端/桌面 build、migration/release checks |
| Release | 发布候选 | 两个真实靶场、能力成长、升级恢复、安全评审、已知限制 |

所有 Agent 相关测试和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

安全路径、migration、Runner ownership、Effect Policy 和 Stop Proof 不能因为测试耗时而跳过 milestone gate。

### 13.2 Git 纪律

- 一个实现提交只表达一个用户可解释或安全可验证的结果；
- 实现提交与实施账本提交分开；
- Task 完成后更新 `FORMAL_AGENT_PROGRESS.md`；
- 不提交无关用户改动；
- 不使用破坏性 reset/checkout 清理工作树；
- 提交前检查 staged diff 和 `git diff --cached --check`；
- 每个切片先保证可回滚，再进入下一个切片。

进度不以代码量、表数量、Pack 数量或测试总数判断，只看用户路径、持久恢复、安全边界、证据链、停止证明和能力复用是否成立。

---

## 14. Codex 执行协议

Codex 每轮只处理一个最小纵向切片。开始前明确：

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

1. 读取本文件、实施账本、相关 ADR 和当前工作树；
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
- 一个小功能要求同时修改大量无关模块；
- 单元测试通过但没有真实用户路径；
- 为未来可能性阻塞当前 Pentest E2E。

实施账本是提交、测试和历史任务状态的唯一事实来源。本文件只在附录保留机器可校验的任务依赖图，不复制提交号、测试记录和详细任务历史。

---

## 15. 当前唯一施工指令

当前只继续以下工作：

1. 完成工作树中 declared/observed/verified Attack Surface 最小投影；
2. 使用 `conda` 的 `agent` 环境运行 Ruff、scoped mypy 和目标测试；
3. 补齐纯投影、API、重启重建和 CLI 渲染测试；
4. 形成独立 Attack Surface 实现提交；
5. 固化隔离授权 Pentest 生命周期 E2E；
6. 形成独立 E2E 实现提交；
7. 单独更新实施账本并将 PEN-500 标记为 completed；
8. 进入 P3 的一个网络服务专业闭环。

在 PEN-500 完成前，不启动 Code Audit、学习系统、Marketplace、更多 Pack、更多 Scanner、UI 扩展或大规模代码删除。

---

## 16. 任务依赖兼容附录

本附录用于满足 ADR、实施账本和文档合同的依赖对账。它不构成当前排期；实际状态、提交和验证证据以实施账本为准。当前只执行第 5 节定义的 P2→P6→R1 主线。

### SEC-000：正式版 ADR 与实施账本

**依赖**：无。

### SEC-001：Security Capability Evaluation 骨架

**依赖**：SEC-000。

### CAP-001：Capability Domain 与持久化

**依赖**：SEC-000。

### CAP-100：接通生产 Progressive Skill

**依赖**：CAP-001。

### CAP-101：原生代码工具

**依赖**：CAP-001。

### CAP-102：Browser/Web/Traffic Tool 闭环

**依赖**：CAP-001。

### CAP-103：MCP 生产接入

**依赖**：CAP-001。

### CAP-104：持久化 Tool/Skill Selection

**依赖**：CAP-100、CAP-103。

### COG-200：Task Graph

**依赖**：CAP-104。

### COG-201：Evidence Ledger

**依赖**：COG-200。

### COG-202：Reasoning Graph

**依赖**：COG-201。

### COG-203：Primary Agent Proposal Tools

**依赖**：COG-202。

### COG-204：Observer Supervisor 与 Projector

**依赖**：COG-203。

### COG-205：Closure Verifier

**依赖**：COG-204。

### PACK-300：基础渗透 Packs

**依赖**：CAP-102、CAP-104、COG-205。

### PACK-301：基础代码审计 Packs

**依赖**：CAP-101、CAP-104、COG-205。

### PACK-302：Onboard 和 Doctor

**依赖**：PACK-300、PACK-301。

### AUD-400：Repository Intelligence

**依赖**：CAP-101、COG-202。

### AUD-401：Scanner Adapter

**依赖**：AUD-400。

### AUD-402：专业角色工作流

**依赖**：AUD-400、AUD-401、COG-205、PACK-301。

### AUD-403：代码证据模型

**依赖**：COG-201、AUD-400、AUD-401。

### AUD-404：Diff Audit 与 Variant Analysis

**依赖**：AUD-400、AUD-403。

### AUD-405：受控动态验证

**依赖**：CAP-101、AUD-403。

### PEN-500：Pentest Admission 与 Attack Surface

**依赖**：CAP-102、COG-202。

### PEN-501：状态化 Web 测试

**依赖**：CAP-102、PEN-500。

### PEN-502：验证规划器

**依赖**：COG-203、PEN-500、PEN-501。

### PEN-503：CVE/PoC Research

**依赖**：CAP-102、PEN-502。

### PEN-504：Attack Chain、Report 与 Stop Proof

**依赖**：COG-201、PEN-500、PEN-502。

### LEARN-600：Trajectory Store 与 Session Search

**依赖**：COG-205。

### LEARN-601：Post-run Review

**依赖**：LEARN-600。

### LEARN-602：Failure Taxonomy

**依赖**：LEARN-601。

### LEARN-603：Replay Lab

**依赖**：SEC-001、LEARN-601、LEARN-602。

### LEARN-604：Capability Curator

**依赖**：CAP-001、LEARN-603。

### LEARN-605：Profile、导入和迁移

**依赖**：LEARN-604、PACK-302。

### EVAL-700：代码审计语料

**依赖**：SEC-001、AUD-403、AUD-404。

### EVAL-701：渗透测试靶场

**依赖**：SEC-001、PEN-504。

### EVAL-702：版本、配置与能力包回归 Harness

**依赖**：EVAL-701、LEARN-603。

### EVAL-703：质量与安全发布检查

**依赖**：EVAL-702、PACK-302。

### ECO-800：Pack SDK

**依赖**：CAP-001、LEARN-604。

### ECO-801：信任与供应链

**依赖**：ECO-800。

### ECO-802：Gateway 与持续运行

**依赖**：LEARN-605、ECO-801。

---

## 17. 最终定位

RiftX 不应继续成长为“功能很多但主路径不完整”的通用 Agent 平台。正式版应当是：

> 一个知道授权边界、能够持续执行和恢复、会记录证据与失败、能形成专业报告，并能把操作者方法论沉淀为可审查生产能力的渗透测试工作台。

完成目标的最短路径是：先跑通一条真实 Pentest 闭环，再用真实使用决定保留、优化和删除什么。当前不是继续扩架构的时候，而是完成 P2。
