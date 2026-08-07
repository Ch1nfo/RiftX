# RiftX 过度开发清理计划

> 文档定位：指导 Codex 安全、分阶段、可回滚地清除 RiftX 中偏离 Pentest-first 产品目标的功能、默认装配、抽象和代码
>
> 适用对象：Codex、RiftX 维护者、产品所有者
>
> 校准日期：2026-08-07（Asia/Shanghai）
>
> 当前分支：`ch1nfo/riftx-3-code-audit`
>
> 计划基线：`fea06c87`
>
> 当前状态：Pentest-first R1 已通过发布门；Phase 1 零消费者叶子代码清理完成
>
> 上游产品边界：[`RiftX_正式版_开发优化文档.md`](RiftX_正式版_开发优化文档.md)
>
> 既有消费者证据：[`docs/pentest-r1-consumer-audit.md`](docs/pentest-r1-consumer-audit.md)
>
> R1 发布基线：[`docs/pentest-r1-release-check.md`](docs/pentest-r1-release-check.md)

---

## 0. 一页执行结论

RiftX 当前的唯一产品目标是：

> 成为一个在专业人士手中好用、可控、可恢复，并且能通过持续加入 Skill、Tool、Technique 和实战方法而越来越顺手的授权渗透测试 Agent。

本轮清理不是重写项目，也不是追求一个更漂亮的架构。清理只做四件事：

1. 删除没有生产消费者、没有数据责任、只为未来设想存在的代码；
2. 将有专业价值但不是基础 Pentest 必需的能力改为按需安装或按需初始化；
3. 分阶段退役已经偏离当前产品范围的 Code Audit 纵向栈；
4. 在功能删除后，清理空壳 Protocol、Adapter、Schema、Repository、测试和文档。

清理不能破坏：

- Scope、Approval、Credential Reference、Redaction 和 Effect Policy；
- Run、Execution、Runner ownership、暂停、恢复、取消和 Stop Proof；
- Artifact、Traffic、Evidence、Reasoning、Finding、Closure 和 Report；
- Capability Version、Digest、Provenance、Selection 和 Pack Lock；
- Operator Skill、Tool Registry、Official Pentest Pack 和用户扩展能力；
- migration 历史、Backup/Restore、旧数据完整性和仍受支持的 Temporal Replay。

执行顺序固定为：

```text
冻结新增
→ 建立可恢复基线
→ 删除零消费者叶子代码
→ 删除未接线的未来能力写入面
→ 将可选专业能力改为按需装配
→ 分阶段退役 Code Audit
→ 清理遗留抽象和入口
→ 全仓发布回归
```

任何阶段失败时，停止扩大删除范围，修复或回滚当前独立提交。不得把多个不相关模块合并成一次大删除。

---

## 1. 本计划的权威语义

### 1.1 与既有消费者审计的关系

`docs/pentest-r1-consumer-audit.md` 在 R1 发布前得出“没有可安全直接删除的大模块”的结论。该结论在当时是正确的，因为当时的目标是避免删除阻断发布。

本计划基于两个已经变化的前提重新开启删除：

1. Pentest-first R1 已完成并有完整回归基线；
2. 产品目标已经进一步收敛为“专业渗透测试 Agent”，不再要求同时维护完整通用 Agent 平台和 Code Audit 产品线。

因此：

- 既有消费者审计继续作为证据来源；
- 其“冻结兼容”不是永久禁止删除；
- 本计划规定新的退役前置条件和执行顺序；
- migration 历史仍然永久保留，不因产品范围变化而重写。

### 1.2 本计划不追求的事情

- 不通过评测证明 RiftX 超过 Codex、Claude Code 或 OpenCode；
- 不为了减少行数删除安全、恢复或数据兼容逻辑；
- 不新建通用 Feature Flag 平台、Plugin Framework 或 Service Container；
- 不把 Code Audit 搬进一个新的复杂子项目后宣称完成清理；
- 不重写 Runtime、Temporal Workflow、Repository 或 Control Plane；
- 不把现有 Protocol、DDD 分层或大文件一律判定为过度设计；
- 不删除未来仍直接提高 Pentest 上限的 Browser、MCP、Subagent、Web Research、Tool、Skill 和 Memory 能力；
- 不为了“代码更干净”修改已经完成且没有真实问题的安全路径。

### 1.3 清理目标

清理完成后，RiftX 应满足：

- 默认安装和启动围绕 Pentest 主路径；
- 无配置的可选能力不创建客户端、后台任务或重型运行时；
- 生产包中不存在只验证自身设计、没有产品消费者的能力框架；
- Code Audit 不再占据默认 CLI、API、Worker、Control Plane 和依赖面；
- 旧数据库仍可备份、恢复并保留历史表；
- 旧 Pentest Run、Report、Evidence 和 Capability Selection 仍可读取；
- 专业用户仍可通过 Skill、Tool、MCP、Browser 和 Subagent 提高能力上限；
- 全仓测试、安装、Onboard、Doctor、两个 Pentest 场景和安全失败关闭继续通过。

---

## 2. 不可突破的安全与兼容边界

### 2.1 永久保留

以下内容不进入“是否过度开发”的讨论：

| 能力 | 保留原因 |
| --- | --- |
| Scope / Admission | 决定目标和操作是否被授权 |
| Approval | 高风险操作的人类控制边界 |
| Credential Reference | 避免模型和通用日志直接接触凭据 |
| Redaction | 防止 Secret、Cookie、Token 和原始响应泄漏 |
| RunKind Effect Policy | 防止不同 Run 类型越权执行副作用 |
| Run / Execution / Runner ownership | 可恢复执行和远程/本地所有权基础 |
| Pause / Cancel / Safety Stop / Stop Proof | 预算耗尽和人工停止的真实收敛 |
| Artifact / Traffic / Evidence | 专业结论的可审查依据 |
| Reasoning / Finding / Closure / Report | Pentest 专业事实链 |
| Capability Version / Selection / Pack Lock | 用户能力版本、来源和运行时固定 |
| migration 文件 | 历史数据库可升级、恢复和审计的基础 |
| Backup / Restore | 删除失败和数据事故的恢复边界 |

### 2.2 禁止删除的兼容代码

以下代码只有在其全部历史消费者已经终止后才能删除：

- Temporal Workflow Replay 所需的旧 Activity、字段和枚举分支；
- 旧数据库记录反序列化所需的 Enum value 和字段兼容；
- 旧 Run、Execution、Artifact、Evidence、Finding、Report 的读取映射；
- 当前 Backup/Restore 和 Doctor 数据完整性检查所需的表或元数据；
- `code_audit` 历史 RunKind、owner kind 和 migration 中仍存在的约束值；
- 已持久化 Browser Session 的停止与恢复路径；
- 当前 Pentest Selection 和 Pack Lock 所使用的 Capability 表。

### 2.3 Migration 规则

1. 已提交的 Alembic migration 永不删除、改名、合并或重写；
2. 清理生产代码不等于删除历史数据库表；
3. 若新版本不再使用某张表，默认保留该表，不增加 drop migration；
4. 只有产品所有者明确要求进行数据库物理瘦身，并完成独立备份、导出和恢复演练后，才允许新增 drop migration；
5. 本计划默认不执行数据库物理删表。

---

## 3. 如何判断“过度开发”

### 3.1 四类处置

| 类别 | 定义 | 默认动作 |
| --- | --- | --- |
| A：立即删除 | 无生产、运维、兼容、动态加载和数据消费者 | 删除源码、测试、导出、配置和文档 |
| B：先取消默认装配 | 能力有价值，但基础 Pentest 不需要 | 改为有配置或有调用时初始化 |
| C：冻结兼容后退役 | 当前仍有历史数据、API、Worker 或 Replay 责任 | 先停止新写入，再删运行时，最后删上层模型 |
| D：必须保留 | Pentest、安全、恢复、专业事实或用户上限核心 | 不以清理名义改动 |

### 3.2 单个删除候选的准入门

Codex 只能在以下问题全部回答清楚后删除代码：

1. 定义在哪里？
2. 静态生产调用者有哪些？
3. CLI、API、Web、Worker、Runner、脚本和运维入口有哪些？
4. 是否存在字符串反射、注册表、entry point、动态 import 或 YAML ID 消费？
5. 是否参与 migration、Backup/Restore、Doctor、旧数据读取或 Replay？
6. 是否承担 Scope、Approval、Credential、Evidence、Provenance 或 Stop Proof？
7. 删除后用户行为由什么替代？
8. 删除带来什么可重复收益？
9. 最小目标测试是什么？
10. 如何用一个 Git revert 恢复？

只要第 2 至第 6 项存在未解释消费者，就不能进入 A 类。

### 3.3 证据命令

每个候选至少执行：

```bash
rg -n "CandidateName|function_name|module.path" src tests apps scripts docs migrations
rg -n "include_router|add_typer|entry-points|project.scripts" src pyproject.toml
rg -n "import_module|getattr|registry|register|tool_id|capability_id" src
rg -n "table_name|column_name|enum_value" migrations src/riftx/persistence
git log --oneline --all -- path/to/candidate
```

Agent 相关测试、脚本和运行必须使用：

```bash
conda run --no-capture-output -n agent ...
```

---

## 4. 当前候选清单

### 4.1 A 类：优先删除

#### A1. Security Agent 评测骨架

位置：

- `src/riftx/evaluation/security_agent/`
- `tests/evaluation/security_agent/`

当前事实：

- 约 832 行生产代码和 336 行自验证测试；
- 没有 CLI、API、Worker 或 Runtime 生产入口；
- 不参与 migration、Backup/Restore 或旧数据读取；
- 不被现有 `scripts/qa/release-gate.py` 和 `code-audit-boundary-gate.py` 使用；
- 主要服务于早期“Security Agent Evaluation Harness”设想。

处置：整体删除目录、`riftx.evaluation` 导出、对应测试、只描述该 Harness 的文档合同。

保留：

- 真实 Pentest E2E；
- Scope、Approval、预算、恢复和 Redaction 回归；
- 现有发布检查文档；
- 与生产安全边界直接相关的 QA 脚本。

#### A2. 未接线的 Capability Control Plane Schema

位置：

- `src/riftx/api/schemas/capabilities.py`
- `src/riftx/api/schemas/__init__.py` 中相关导出
- `tests/unit/capabilities/test_models.py` 中只验证未接线 Schema 的部分

当前事实：

- 没有 Capability API Route；
- Candidate、Promotion、Evaluation 请求和响应只在 Schema 文件与测试中出现；
- Pentest 真正使用的是 `api/schemas/pentests.py` 的 Capability Status；
- Operator Skill 使用本地 CLI 和 Capability Repository，不使用该 API Schema。

处置：删除整个未接线 Schema 文件及导出。若其中存在被生产代码引用的 Pack/Version DTO，只保留实际消费者所需的最小 DTO，不为未来 API 保留请求模型。

#### A3. Engagement Fact Promotion 写入栈

位置：

- `src/riftx/facts/`
- `src/riftx/persistence/fact_repositories.py`
- `tests/facts/test_promotion_graph.py`

当前事实：

- `FactPromotionService` 和专用写 Repository 没有生产装配；
- 仅自身测试调用 Promotion；
- `engagement_facts` 和 `fact_relations` 历史表仍被 Graph 读取投影使用；
- 因此写入栈是死代码，历史 ORM Record 和 Graph 读取不是死代码。

处置：

1. 删除 `FactPromotionService`、候选写模型和专用写 Repository；
2. 删除对应测试和公开导出；
3. 保留 migration；
4. 保留 `EngagementFactRecord`、`FactRelationRecord` 和 Graph read query；
5. 不新增第二套 Fact 转换层。

### 4.2 B 类：取消默认装配

#### B1. MCP 默认初始化

当前事实：Worker 启动时即使没有有效 MCP Server，也会创建 Registry、refresh 并构造 Service。

目标：仅在配置至少一个启用的 MCP Server 时创建 Registry 和 Service；无配置时 Tool Registry 正常工作，状态明确为未配置而不是失败。

禁止：建设通用 lazy service framework。

#### B2. Browser 与 Playwright 强制安装

当前事实：

- `playwright` 位于核心依赖；
- Browser 对状态化 Web Pentest 和高能力上限有价值；
- 基础网络服务 Pentest 不需要 Browser。

目标：

- 将 Playwright 移到可选依赖，例如 `riftx[browser]`；
- 未安装时 Doctor 显示可操作的 degraded 信息；
- 未选择 Browser Tool 的 Run 不创建 Browser Engine；
- Browser Session 一旦存在，停止和恢复责任不变。

#### B3. Web Research 默认构造

Web Research 对漏洞资料、CVE、产品行为和公开来源核实有 Pentest 价值，不能删除。仅在配置了 Search Provider 或 Run 实际选择相关 Tool 时构造 Provider Client。

#### B4. Connector 默认 Control Plane 装配

Connector 当前有真实 API、Repository 和 migration，但不是基础 Pentest 必需。

目标：

1. 默认不装配 Connector Route 和 Service；
2. 通过现有简单配置显式启用；
3. 保留旧表和 Backup/Restore；
4. 经过一个清理周期仍无真实用户后，重新审计是否进入 A 类。

### 4.3 C 类：冻结兼容后退役

#### C1. Capability Candidate / Promotion / Evaluation 写入面

位置：

- `src/riftx/capabilities/models.py`
- `src/riftx/capabilities/repository.py`
- `src/riftx/persistence/capability_repository.py`
- `src/riftx/persistence/capability_records.py`
- 相关测试与文档

当前事实：

- 生产源码没有调用 `create_candidate()`、`create_promotion()`、`add_evaluation_result()` 或 `promote_candidate()`；
- Candidate、Promotion 和 Evaluation 表已经存在；
- Capability Version、Selection、Pack、Install、Lock 是 Pentest 和 Operator Skill 核心，不能整体删除。

处置顺序：

1. 删除未接线 API Schema；
2. 删除 Repository Protocol 中 Candidate/Promotion 写方法；
3. 删除 SQLAlchemy Repository 中对应写方法和 mapper；
4. 删除只为这些写方法存在的 Domain Model 和测试；
5. 保留 Capability Version、Permission、Evidence Contract、Pack、Install、Lock；
6. 保留历史 ORM Record 和 migration，直到确认不再需要类型化旧数据读取；
7. 默认不增加 drop migration。

#### C2. Code Audit 产品线

Code Audit 是当前最大的一组过度开发代码。相关范围约包括：

- `src/riftx/audit/`：约 1.26 万行；
- `src/riftx/audit_worker/`：约 0.38 万行；
- Audit Domain、Application Port/Service、Persistence、API、Runner：约 4 万行；
- 相关测试约 3.1 万行；
- Audit、Preflight 和 Preflight Runner 至少 19 个专用路由；
- 另有 Audit Artifact、CLI、Demo、Pack、migration 和 Worker 装配。

产品判断：当前目标是 Pentest Agent，Code Audit 不再属于默认产品线。它必须退出默认安装、启动、CLI、API 和 Worker，但历史数据与 migration 继续保留。

Code Audit 不能一次性删除，必须执行第 9 节的独立退役路线。

### 4.4 D 类：必须保留

| 模块 | 结论 |
| --- | --- |
| General Agent Runtime | Pentest 的模型循环和 Tool execution 核心 |
| Working Memory / Long-term Memory | 专业用户越用越顺手的基础；只清理真正无消费者的旁路 |
| Subagent | 已接入 Worker，提供有边界的并行专业任务上限 |
| Browser | Web Pentest 和登录态/对象授权场景核心可选能力 |
| MCP | 用户增加专业工具和外部系统的关键扩展点 |
| Web Research | Pentest 公开信息与漏洞资料核实能力 |
| Code workspace / Git / Patch / LSP | PoC、脚本、配置和目标源码处理能力 |
| Official Pentest Packs | 开箱即用即战力和领域方法载体 |
| MemoryWriter 的 Finding 写入 | 已有生产消费者，且服务于能力复利 |
| Demo Pentest | 无网络安全演示和新用户验证入口 |

---

## 5. Codex 全局执行规则

### 5.1 每次只做一个删除切片

一个切片必须满足：

- 一个明确模块或一条明确装配链；
- 一个可描述的用户行为变化；
- 一组聚焦测试；
- 一个独立本地提交；
- 可以用一次 `git revert <commit>` 回滚。

禁止将 Evaluation、Capability、Fact、Connector 和 Code Audit 同时塞进一个提交。

### 5.2 先删叶子，再删根

删除顺序固定为：

```text
CLI/API/Schema/测试入口
→ Application Service
→ Protocol / Port
→ Runtime 装配
→ Repository 方法
→ Domain Model
→ ORM Record（通常保留）
→ migration（永久保留）
```

如果 Domain Model 仍用于旧数据读取，不得为了删除上层入口而强行移动或复制它。

### 5.3 不以新增抽象完成删除

清理时禁止新增：

- 通用模块管理器；
- 通用 Feature Flag 平台；
- 新的 Service Locator；
- 新的 Plugin SDK；
- 新的兼容数据库；
- 新的 Audit Archive 微服务；
- 仅有一个实现的新 Protocol；
- 为了保持旧测试而存在的生产 wrapper。

允许增加的兼容代码只限于：

- 一个已有配置字段的简单条件分支；
- 一个必要的失败关闭错误；
- 一个保证旧数据不会被误写的只读保护；
- 一个最小恢复或回滚检查。

### 5.4 编辑与测试规则

- 所有文件修改使用 `apply_patch`；
- 不覆盖或回滚用户的无关改动；
- Agent 相关测试使用 conda `agent` 环境；
- 删除任何文件前先用 `rg` 搜索静态、动态和文档引用；
- 每次提交前运行 `git diff --check`；
- 目标测试通过前不得继续扩大删除范围；
- 阶段 Gate 通过前不得标记阶段完成。

---

## 6. Phase 0：建立清理基线

### 6.1 目标

建立能够证明“删除没有损坏 Pentest”的可恢复基线。

### 6.2 必做事项

1. 确认工作树状态和当前提交；
2. 记录 Python、Node、pnpm、数据库 schema head；
3. 对当前本地 SQLite 执行现有 Backup；
4. 验证 Restore readiness，不在原数据库上做破坏性演练；
5. 记录非终态 Run、Execution、Browser Session、Audit Run 和 Temporal Workflow；
6. 若存在非终态 Code Audit，先完成、取消或安全停止；
7. 运行最小 Pentest 基线；
8. 保存启动和安装基线，作为可选依赖清理的对照。

### 6.3 基线命令

```bash
git status -sb
git log -1 --oneline
conda run --no-capture-output -n agent python --version
conda run --no-capture-output -n agent alembic heads
conda run --no-capture-output -n agent riftx doctor
conda run --no-capture-output -n agent python -m pytest -q \
  tests/e2e \
  tests/integration/api/test_pentest_stateful_web.py \
  tests/unit/test_capability_management.py
```

### 6.4 完成门

- 工作树状态已记录；
- 数据库有可验证备份；
- 没有未处理的 Code Audit 活跃副作用；
- Pentest 核心基线通过；
- 创建独立提交：`docs(cleanup): establish overdevelopment removal baseline`。

若 Phase 0 不通过，禁止开始删除。

---

## 7. Phase 1：删除零消费者叶子代码

### 7.1 Slice 1A：删除 Security Agent Evaluation Harness

步骤：

1. 搜索所有 `riftx.evaluation.security_agent` 引用；
2. 确认 QA scripts 不依赖该包；
3. 删除生产目录和自验证测试；
4. 清理 `__init__.py` 导出、文档任务记录和无效 fixture；
5. 保留普通安全回归；
6. 运行 Evaluation、文档合同和 Pentest 核心回归。

目标测试：

```bash
conda run --no-capture-output -n agent python -m pytest -q \
  tests/evaluation \
  tests/docs/test_formal_agent_docs.py \
  tests/e2e
conda run --no-capture-output -n agent ruff check src tests
```

提交：`refactor(evaluation): remove unused security agent harness`

### 7.2 Slice 1B：删除未接线 Capability API Schema

步骤：

1. 确认没有 Route、CLI Client 和 Web Client 使用；
2. 删除 Schema 文件或只保留真实消费者所需 DTO；
3. 删除 `api/schemas/__init__.py` 导出；
4. 删除只验证这些 Schema 的测试；
5. 运行 API import、OpenAPI、Pentest Admission 和 Operator Skill 回归。

提交：`refactor(api): remove unwired capability schemas`

### 7.3 Slice 1C：删除 Fact Promotion 写入栈

步骤：

1. 再次确认 `FactPromotionService` 无生产装配；
2. 确认 Graph Repository 直接读取 ORM Record；
3. 删除 `src/riftx/facts/` 中写入服务和候选模型；
4. 删除 `persistence/fact_repositories.py`；
5. 删除写入测试；
6. 保留 ORM Record、migration 和 Graph read query；
7. 增加或保留一个旧 Fact 数据图读取回归，证明历史数据仍可投影。

提交：`refactor(facts): remove unused promotion write stack`

### 7.4 Phase 1 完成门

- 三个切片分别提交；
- 没有新增生产抽象；
- `rg` 不再发现悬空导出；
- migration、Graph read、Pentest E2E、Ruff 通过；
- 记录实际删除文件和行数，但不以行数作为正确性标准。

---

## 8. Phase 2：收缩 Capability 未来模型

### 8.1 保留边界

必须保留：

- Capability；
- CapabilityVersion；
- CapabilityManifest；
- Permission 和 Evidence Contract；
- Provenance 和 Digest；
- Pack、Pack Member、Install、Lock；
- Session Capability Selection；
- Operator Skill register/activate/disable/rollback；
- PentestCapabilityResolver。

计划删除：

- CapabilityCandidate 的在线创建面；
- PromotionRun 的在线写入面；
- CapabilityEvaluationResult 的在线写入面；
- 自动 Promotion 原子流程；
- 对应 Protocol 方法、mapper、异常分支和只验证这些流程的测试。

### 8.2 执行切片

#### Slice 2A：删除公共写契约

- 删除 Repository Protocol 中 Candidate/Promotion/Evaluation 方法；
- 删除公共导出；
- 删除未接线请求/响应模型；
- 不改 ORM 和 migration。

#### Slice 2B：删除 SQLAlchemy 写实现

- 删除 `create_candidate()`；
- 删除 `create_promotion()`；
- 删除 `add_evaluation_result()`；
- 删除 `promote_candidate()`；
- 删除只服务这些方法的 mapper；
- 保留 Version、Pack、Install 和 Lock 路径。

#### Slice 2C：收缩 Domain Model

- 搜索 Candidate/Promotion/Evaluation Model 的剩余消费者；
- 若只剩旧数据类型化读取，暂时保留并标记 compatibility；
- 若消费者为零，删除 Model 和 Enum；
- ORM Record 中使用的字符串值不要求 Domain Enum 继续存在；
- 不删除历史表。

### 8.3 关键回归

```bash
conda run --no-capture-output -n agent python -m pytest -q \
  tests/unit/capabilities \
  tests/unit/test_capability_management.py \
  tests/integration/persistence/test_capability_repository.py \
  tests/integration/persistence/test_capability_migration.py \
  tests/integration/api/test_pentest_stateful_web.py
```

### 8.4 完成门

- 新 Pentest 仍能解析 Tool、Skill、Technique 和 Pack；
- Operator Skill 生命周期完整通过；
- 旧 Capability 数据库可升级、备份和恢复；
- Candidate/Promotion/Evaluation 不再有生产写入口；
- 没有引入新的“简化版 Promotion”替代流程。

---

## 9. Phase 3：可选能力按需装配

### 9.1 原则

此阶段的目标不是删除专业能力，而是让基础用户不为未使用能力支付安装、启动、内存和故障成本。

### 9.2 Slice 3A：Playwright 可选依赖

1. 将 `playwright` 从核心依赖移动到 `browser` extra；
2. wheel 无 Browser extra 时必须能安装、Onboard、Doctor、启动 Control Plane 和运行网络服务 Pentest；
3. 请求 Browser 功能时给出明确安装命令；
4. Doctor 将 Browser 缺失标为可选 degraded，而不是整体失败；
5. 安装 `riftx[browser]` 后现有 Browser 回归继续通过。

禁止自动执行 `playwright install` 或静默下载浏览器。

### 9.3 Slice 3B：MCP 按配置初始化

1. 空 MCP 配置不创建 Registry Client；
2. 空配置不执行 refresh 或 health network path；
3. MCP Tool 未选择时 Runtime 不依赖 MCP Service；
4. 配置 MCP 时保持现有 Governance、Artifact 和 ToolCallIntent 边界；
5. 关闭 Worker 时只关闭已创建的 Registry。

### 9.4 Slice 3C：Web Research 按 Provider 初始化

1. 没有 Search Provider 时不创建远程 Provider Client；
2. 本地 PublicWebFetcher 只有在相关 Tool 可用时装配；
3. SSRF、防重定向、Scope 和 Evidence Source 规则不变；
4. 不删除 Web Research 数据表和历史记录。

### 9.5 Slice 3D：Connector opt-in

1. 复用现有配置增加最小启用判断；
2. 默认不 include Connector Router；
3. 默认不构造 ConnectorApplicationService；
4. 启用时全部 API、幂等和 Artifact 回归通过；
5. 禁用时旧表不变，Backup/Restore 不变。

### 9.6 Slice 3E：Browser Engine 惰性创建

1. Control Plane 可保留轻量 BrowserApplicationService；
2. Playwright Engine 和真实 Browser Process 只在首个 Browser Session 时创建；
3. 并发首启必须只有一个权威 Engine 初始化；
4. 初始化失败不能留下假活跃 Session；
5. 现有 Session 的停止、恢复、取消和 Stop Proof 不变。

### 9.7 完成门

- 核心 wheel 不强制安装 Playwright；
- 空 MCP、空 Search Provider、Connector disabled 时无对应客户端或后台任务；
- Network Pentest 在最小安装中通过；
- Stateful Web 在 Browser extra 中通过；
- 没有新增通用 lazy framework。

---

## 10. Phase 4：Code Audit 分阶段退役

### 10.1 退役前置条件

开始前必须全部满足：

- 产品所有者确认 Code Audit 不再属于当前支持范围；
- 当前数据库已备份；
- 没有非终态 Code Audit Run、Execution、Preflight Job、Runner Command 或 Snapshot Mount；
- 已记录所有 Code Audit API、CLI、Worker、Route、表、目录和测试；
- 已确认共享 Artifact、Evidence、Report、Code Workspace、Git、Patch、LSP 哪些仍被 Pentest 使用；
- 已建立恢复旧提交后读取 Audit 数据的演练步骤。

若任何条件不满足，Phase 4 保持 blocked，不通过临时删除绕过。

### 10.2 Slice 4A：停止新 Code Audit 创建

目标：先停止新增历史责任。

步骤：

1. 从默认 CLI 移除 `riftx audit scan/start` 和 `demo code-audit`；
2. 移除或关闭新 Audit 创建、Preflight 创建和 Start API；
3. 保留必要的只读状态、Finding、Report 和 Artifact 查询；
4. Web 不再展示创建入口；
5. 文档明确 Code Audit retired；
6. 不新增复杂 deprecation framework。

若当前 alpha 版本没有外部兼容承诺，可以直接移除写入口；若存在已知外部消费者，则保留一个版本的明确 `410 Gone`，不得无限期维护双路径。

### 10.3 Slice 4B：移除默认 Audit Runtime 装配

1. Control Plane 默认不创建 Audit Service、Preflight Service、Plan Service 和 Reconciler；
2. Worker 默认不创建 Snapshot Store、Audit Materializer、Audit Source Ingest 和 Audit Runtime Tool；
3. Runner 不再注册 Audit Preflight 命令；
4. Safety Stop 继续处理历史已持久化但未清理的资源；
5. Pentest、Browser、Target HTTP 和 Report 装配不得被复制。

### 10.4 Slice 4C：删除 Audit Worker 与 Source Ingest

候选：

- `src/riftx/audit_worker/`
- `src/riftx/audit/source_ingest.py`
- `src/riftx/audit/source_ingest_contract.py`
- `src/riftx/audit/local_materializer.py`
- `src/riftx/audit/snapshot_mount.py`
- Audit Preflight Runner Client/Backend
- 对应测试和配置

前置检查：

- 没有活动 Snapshot Mount；
- 没有 Audit Runner ownership；
- 没有 Pentest 路径复用这些类；
- 删除后 Runner daemon 仍能执行 Terminal、Browser、Target HTTP 和 Process。

### 10.5 Slice 4D：删除 Audit API 与 Application 层

候选：

- `api/routes/audits.py`；
- `api/routes/audit_preflight.py`；
- `api/routes/audit_preflight_runner.py`；
- Audit 专用 Schema；
- Audit Application Service 和 Port；
- Audit Workflow Router 分支；
- Audit 专用 CLI Client 和 Render。

保留：

- Pentest 共用的 Artifact、Finding、Report、Evidence 和 Code Workspace；
- 历史 `RunKind.CODE_AUDIT` 值；
- 旧数据基础读取所需的最小 mapping，直到产品所有者确认归档完成。

### 10.6 Slice 4E：删除 Audit Domain 与 Persistence 上层代码

1. 删除无消费者的 Audit Aggregate、Preflight、Plan、Contract 和 State Model；
2. 删除无消费者的 Repository、UoW、Mapper 和 Snapshot Repository；
3. 保留 migration；
4. 默认保留 ORM Record，直到 Backup/Restore 不再 import 它们；
5. 若 ORM Record 仅用于 SQLAlchemy metadata 而 migration 已负责建表，可删除 ORM Model，但必须验证旧数据库启动、upgrade、backup 和 restore；
6. 不新增 drop migration。

### 10.7 Slice 4F：删除 Audit Detector、Pack、Demo 和前端

1. 删除仅服务 Code Audit 的 Detector 和 Builtin Detector；
2. 删除 Code Audit Official Packs；
3. 删除 Code Audit Demo；
4. 删除 Web Code Audit 页面、类型和客户端；
5. 删除文档中的当前使用说明，保留历史 ADR/进度记录；
6. 保留 Pentest Skill 和通用代码工具。

### 10.8 Slice 4G：Replay 与枚举兼容收尾

1. 搜索 `code_audit`、`RunKind.CODE_AUDIT`、owner kind 和 effect policy；
2. 保留 migration constraint 和旧记录可解析值；
3. 若仍存在 Temporal 历史，保留对应旧 Workflow/Activity 代码或先完成历史清退；
4. 只有所有历史 Workflow 已终态且不再要求 Replay 时，才能删除旧执行分支；
5. 不删除旧 Enum value 以换取少量代码行。

### 10.9 Code Audit 完成门

- 默认 CLI、API、Control Plane、Worker、Runner 和 Web 不再包含 Code Audit 产品面；
- 核心安装不包含 Audit 专用依赖或资源；
- migration head 不变且 upgrade 通过；
- 旧数据库可启动、备份和恢复；
- 没有活动 Audit 资源；
- 两个 Pentest E2E、Operator Skill、Browser optional、MCP optional、全仓回归通过；
- 每个 Slice 都有独立提交和回滚点。

---

## 11. Phase 5：删除遗留入口和空壳抽象

只有前面功能删除完成后才执行本阶段。

### 11.1 入口清理

候选：

- 隐藏 `riftx interactive` 与无子命令交互模式中的重复路径；
- 已退役的 Audit CLI Client 方法；
- 已退役的 Code Audit API client；
- 没有用户和部署消费者的旧 wrapper；
- 已无对应功能的 Web Route 和导航。

`riftx-runner` 是独立部署入口，不能仅因 `riftx runner` 存在就删除。必须先确认实际部署合同。

### 11.2 抽象清理

每删除一条纵向能力后，搜索：

- 只有零个或一个实现且没有测试替换价值的 Protocol；
- 空 `__init__.py` 导出；
- 只包装一次调用的 Adapter；
- 已无消费者的 Repository；
- 已无 Route 的 API Schema；
- 只为了已删除功能存在的异常类型；
- 已无实现的配置字段和环境变量；
- 已无生产代码对应的测试 fixture。

禁止在功能删除前横向合并所有 Repository 或拆分所有大文件。大规模形式重构会掩盖删除回归。

### 11.3 大文件处理原则

以下文件虽然大，但不能仅按行数重写：

- `application/run_kind_effects.py`；
- `persistence/repositories.py`；
- `persistence/orm.py`；
- `runtime/control_tools.py`；
- `temporal/worker_runtime.py`；
- `cli/app.py`。

正确做法是先删除已退役功能的分支和 import。只有清理后仍存在明显低内聚职责，且有真实维护阻断时，再做独立重构。

---

## 12. Phase 6：文档、配置和依赖收尾

### 12.1 文档

更新：

- README 的默认用户路径；
- `RiftX_正式版_开发优化文档.md` 的模块状态；
- 本清理计划的执行账本；
- Onboard、Doctor 和可选 Browser/MCP/Connector 说明；
- API/CLI 参考；
- 已知限制和恢复说明。

保留：

- 历史 ADR；
- migration 说明；
- 已完成任务的历史事实；
- 发布检查记录。

历史文档可以标记 superseded/retired，但不能改写成“从未存在”。

### 12.2 配置

删除已无消费者的：

- Audit runtime 配置；
- Candidate/Promotion/Evaluation 配置；
- 已移除 Connector/Audit 环境变量；
- 无效默认目录和 Onboard 初始化内容。

旧配置字段如果存在真实用户文件，先由现有 config maintenance 忽略或迁移一个版本，再删除解析。不要因为未知字段而导致旧用户无法启动，除非该字段存在安全风险。

### 12.3 依赖

对每个依赖执行：

```bash
rg -n "package_import_name" src tests scripts apps
```

只有生产和测试消费者均为零时才删除。Browser 等可选能力使用 extras，不新建多个发行包。

---

## 13. Phase 7：测试与发布 Gate

### 13.1 每个 Slice 的最小 Gate

```bash
git diff --check
conda run --no-capture-output -n agent ruff check <changed paths>
conda run --no-capture-output -n agent python -m pytest -q <target tests>
```

涉及类型合同的切片运行 scoped mypy，使用项目当前已验证的参数，不为清理引入全新类型规则。

### 13.2 每个 Phase 的 Gate

```bash
conda run --no-capture-output -n agent ruff check src tests
conda run --no-capture-output -n agent python -m pytest -q \
  tests/e2e \
  tests/integration/api/test_pentest_stateful_web.py \
  tests/runtime \
  tests/target_http \
  tests/temporal \
  tests/unit/test_capability_management.py
```

按实际目录存在情况调整，但不得用“测试已删除”代替 Pentest 行为验证。

### 13.3 最终全仓 Gate

1. 全仓 Python tests；
2. Ruff；
3. 必要 scoped mypy；
4. Alembic 单 head、空库 upgrade、旧库 upgrade；
5. Backup/Restore；
6. wheel 构建和隔离安装；
7. 最小依赖安装下 Onboard/Doctor/Control Plane；
8. Network service Pentest E2E；
9. Stateful Web Pentest E2E（Browser extra）；
10. Operator Skill register/activate/use/disable/rollback；
11. Scope、Approval、预算、Redaction、Stop Proof；
12. Web tests 和 production build；
13. 文档合同和链接；
14. `git diff --check`。

最终 Gate 的基线为：

- Python：R1 曾达到 `5383 passed, 5 skipped`；
- Web：270 tests 和 production build 通过；
- wheel、Onboard、Doctor、Control Plane health 通过。

删除测试会使数量下降，因此不要求维持相同测试数量；必须记录删除了哪些测试以及它们对应的生产功能为何已经退役。

---

## 14. 提交、回滚与失败处理

### 14.1 提交规范

推荐提交顺序：

```text
docs(cleanup): establish overdevelopment removal baseline
refactor(evaluation): remove unused security agent harness
refactor(api): remove unwired capability schemas
refactor(facts): remove unused promotion write stack
refactor(capabilities): remove candidate promotion write contracts
refactor(capabilities): remove candidate promotion persistence path
build(browser): make playwright optional
perf(worker): initialize mcp only when configured
perf(worker): initialize web research providers on demand
refactor(connectors): make connector surface opt-in
refactor(audit): stop new code audit creation
refactor(audit): remove default audit runtime wiring
refactor(audit): remove audit worker and source ingest
refactor(audit): remove audit api and application layer
refactor(audit): remove audit domain and persistence services
refactor(audit): remove audit packs demo and web surface
refactor(cleanup): remove retired adapters and configuration
docs(cleanup): close overdevelopment removal plan
```

实际提交名称可以调整，但切片边界不得合并。

### 14.2 回滚规则

- 聚焦测试失败：先修当前切片，不继续下一切片；
- Pentest E2E 失败：立即停止 Phase，定位是否误删共享能力；
- migration/Restore 失败：回滚当前提交，不得用新 drop migration 修补；
- Temporal Replay 失败：恢复兼容分支，直到历史 Workflow 清退；
- Browser Stop/Recovery 失败：恢复 Browser 装配，不以禁用测试绕过；
- 无法证明旧数据安全：保留 ORM/mapper，延后删除；
- 一个切片连续三次无法在不扩大架构的情况下通过，标记 blocked 并请求产品所有者决策。

### 14.3 禁止的回滚方式

- 不使用 `git reset --hard`；
- 不删除本地用户数据库；
- 不覆盖用户配置或 Skill；
- 不修改既有 migration 伪造通过；
- 不跳过失败测试；
- 不把失败路径改成 silent fallback。

---

## 15. 执行账本

Codex 每完成一个 Slice 更新本表，并链接实现提交和验证结果。

| Phase | Slice | 状态 | 实现提交 | 关键验证 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 0 | 清理基线与备份 | completed | `703065b3` | Pentest 基线 `9 passed`；Alembic/Backup/Restore ready；wheel 与 CLI 启动通过 | 删除前置门已满足 |
| 1 | Security Agent Harness | completed | `dc182854` | Evaluation/Docs/E2E、Ruff、Phase Gate 通过 | 删除 10 个 Harness/Fixture 文件，净删约 1,300 行 |
| 1 | Capability API Schema | completed | `2ba28d8c` | API/OpenAPI、CLI、Capability、Pentest 回归通过 | 删除 248 行未接线 Schema 及自验证测试 |
| 1 | Fact Promotion 写入栈 | completed | `c6e55416` | Graph 历史读取、API、migration、Pentest、Ruff 通过 | 删除 790 行无装配写入栈；保留 ORM、migration 和 Graph 投影 |
| 2 | Capability 公共写契约 | completed | 本提交 | Capability Protocol、公开 import、持久化兼容回归通过 | 删除 Candidate/Promotion/Evaluation 写契约与公开导出 |
| 2 | Capability 持久化写路径 | pending | — | — | C1 |
| 2 | Capability Domain 收缩 | pending | — | — | C1 |
| 3 | Playwright 可选依赖 | pending | — | — | B2 |
| 3 | MCP 按配置初始化 | pending | — | — | B1 |
| 3 | Web Research 按需 Provider | pending | — | — | B3 |
| 3 | Connector opt-in | pending | — | — | B4 |
| 3 | Browser Engine 惰性创建 | pending | — | — | B2 |
| 4 | 停止新 Code Audit 创建 | pending | — | — | C2 |
| 4 | 移除默认 Audit 装配 | pending | — | — | C2 |
| 4 | 删除 Audit Worker/Source Ingest | pending | — | — | C2 |
| 4 | 删除 Audit API/Application | pending | — | — | C2 |
| 4 | 删除 Audit Domain/Persistence 上层 | pending | — | — | C2 |
| 4 | 删除 Audit Pack/Demo/Web | pending | — | — | C2 |
| 4 | Replay/Enum 兼容收尾 | pending | — | — | C2 |
| 5 | 入口和空壳抽象清理 | pending | — | — | 删除后执行 |
| 6 | 文档、配置和依赖收尾 | pending | — | — | — |
| 7 | 最终发布 Gate | pending | — | — | — |

状态只允许：`pending`、`in_progress`、`completed`、`blocked`。

### 15.1 Phase 0 基线记录

校准时间：2026-08-07（Asia/Shanghai）

- Git 基线：`fea06c87 docs(release): complete pentest R1 gate`；分支
  `ch1nfo/riftx-3-code-audit`；清理前工作树只有本计划和上游文档链接两项文档改动；
- 运行环境：Python `3.12.11`、Node `v26.3.0`、pnpm `10.32.1`、Alembic head
  `7b3d1e5f9a24`；
- 数据状态：迁移前 12 个 Run 全部为终态，Execution、Browser Session 和 Audit 表均无非终态记录；
  3 个 RiftX Temporal Workflow 均已关闭，剩余运行记录仅为 Temporal 系统扫描任务；
- 数据库恢复点：`riftx doctor --fix` 使用项目内 SQLite Backup API 将本地数据库从
  `f7a9c1d3e526` 升级到 `7b3d1e5f9a24`，保留迁移前备份
  `.riftx/backups/riftx.db.a05e9c6840144824b4c49fd85e63a49c.bak` 和迁移后 Pack 修复前备份
  `.riftx/backups/riftx.db.40845e4a12ea46e1a61797c69326dc28.bak`；两份备份的
  `PRAGMA integrity_check` 均为 `ok`，后者 SHA-256 为
  `bfd6357302917e163c8ed807fd486b32a859b045175107353e4a53d84156f23e`；
- Restore readiness：Doctor 的 `database_migrations`、`pack_integrity` 和 `backup_restore`
  均为 `ready`；整体 Doctor 仍因未配置模型凭据、未启动 Control Plane 而返回失败，这些是当前离线
  清理基线的已知外部运行条件，不是数据库恢复阻断；
- Pentest 基线：
  `python -m pytest -q tests/e2e tests/integration/api/test_pentest_stateful_web.py tests/unit/test_capability_management.py`
  结果为 `9 passed in 10.79s`；
- 安装与启动对照：wheel `riftx-2.0.0a0-py3-none-any.whl` 构建成功，大小
  `1,677,755` bytes，共声明 20 个依赖且 Playwright 仍为核心依赖；`riftx --help` 成功，
  conda 包装下 wall time 为 `2.91s`。

---

## 16. 最终完成定义

本计划只有在以下条件全部成立时才能标记完成：

1. A 类零消费者代码已经删除；
2. Capability Candidate/Promotion/Evaluation 不再有在线写入面；
3. Browser、MCP、Web Research、Connector 不再无条件初始化；
4. Playwright 不再是基础安装强制依赖；
5. Code Audit 已退出默认 CLI、API、Control Plane、Worker、Runner 和 Web；
6. Code Audit 历史 migration 和数据库数据没有被破坏；
7. 没有非终态 Code Audit 或遗留外部副作用；
8. 退役功能的 Schema、Protocol、Adapter、Repository、配置、测试和文档已同步清理；
9. Scope、Approval、Credential、Evidence、Finding、Report、Stop Proof 完整；
10. Capability Version、Selection、Pack Lock 和 Operator Skill 完整；
11. Browser、MCP、Subagent、Web Research 仍能作为专业用户提高上限的能力；
12. 最小安装和 Browser extra 安装均完成验证；
13. migration、Backup/Restore、wheel、Onboard、Doctor、两个 Pentest 场景、全仓 Python 和 Web Gate 通过；
14. 每个删除切片都有独立提交和回滚点；
15. 本账本已记录最终提交、测试结果、已知限制和剩余兼容代码。

清理完成不以“删除最多代码”为标准。正确结果是：

> RiftX 的默认复杂度只服务于专业 Pentest，扩展能力只在用户需要时付出成本，历史安全与数据责任仍然成立。

---

## 17. Codex 每个 Slice 的标准工作循环

Codex 执行任何 Slice 时，严格按以下顺序：

```text
1. 读取本计划对应章节
2. git status 确认用户改动
3. rg 搜索静态、动态、入口、migration 和文档消费者
4. 写出本 Slice 的保留边界
5. 运行修改前聚焦测试
6. 使用 apply_patch 做最小删除
7. 清理 import、export、配置、文档和测试
8. 运行聚焦测试、Ruff、必要 mypy、diff check
9. 运行对应 Phase Gate
10. 检查 git diff，确认没有无关修改
11. 创建一个本地提交
12. 更新执行账本
13. 再开始下一个 Slice
```

若消费者证据与本计划不一致，以当前代码和真实数据责任为准：停止删除、记录差异、更新计划，不强行执行过时判断。
