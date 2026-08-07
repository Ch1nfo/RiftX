# Operator Skill 使用与人工迭代

Operator Skill 是专业用户维护的本地渗透方法包。RiftX 会在新 Pentest 创建时固定 Skill 的 ID、版本、源码 Digest、来源和完整文档快照；后续修改或禁用本地 Skill，不会改写已有 Run 的事实。

这套流程的目标是让方法随着使用者经验逐步改进，不自动评分、不自动改写 Skill，也不把 Skill 声明的 `preferred_tools` 变成额外执行权限。

## 1. 目录结构

每个 Skill 位于 `skills.path` 下的独立目录：

```text
<skills.path>/
└── service-verification/
    ├── SKILL.md
    └── REFERENCES.md     # 可选
```

最小 `SKILL.md`：

```markdown
---
name: Service Verification
description: 对已授权服务执行一个有证据输出的最小验证
version: 1.0.0
source: operator
required_capabilities:
  - evidence_ledger
preferred_tools:
  - target_http_request
approval_level: always
---

## When to use

在授权范围内的服务观察需要进一步验证时使用。

## Preconditions

确认目标、入口点和拟执行交互均位于当前 Scope。

## Procedure

执行一个能够产生证据的最小验证步骤。

## Decision points

结果已经足够时不扩大交互范围。

## Stop conditions

获得证据、确认 Negative Result 或触发安全门禁后停止。

## Expected output

返回 Evidence 引用、结果类型和剩余不确定性。

## Error handling

区分 Tool 执行失败、权限阻断和目标 Negative Result。
```

Operator Skill 的 `source` 必须是 `operator`。修改正文或 References 都会改变源码 Digest；修改后必须提升 `version`，不能用同一版本覆盖已注册内容。

## 2. 首次启用

```bash
riftx skills validate service-verification
riftx skills register service-verification
riftx skills activate service-verification 1.0.0
riftx skills list service-verification
```

- `validate` 只校验本地包，不写数据库；
- `register` 把当前版本登记为不可变的 `approved` Capability Version；
- `activate` 允许新 Pentest 显式选择该版本；
- 同一个 Skill ID 同时只能有一个 active Operator 版本。

创建 Pentest 时显式传入 Skill ID。RiftX 会在网络或工具副作用发生前检查本地源码与 active Version 是否完全一致。未注册、仅 approved、已 disabled、源码漂移或源码缺失都会拒绝创建新 Run。

## 3. 从 Report 人工复盘

JSON Report 的 `source.pentest` 保存完整结构化事实；Markdown Report 的 `Pentest Method Context` 和 `Pentest Evidence Chain` 提供人工可读投影。复盘时至少检查：

1. `Capability Selections`：本次实际固定的 Skill ID、Version、Digest 和 Source；
2. `Capability Allowlists`：本次 Run 真正允许使用的 Tool、Skill 和 Technique；
3. `Executions`：Tool 状态、退出码、节点和物理停止确认；
4. `Evidence Ledger` 与 `Findings`：证据 Digest、可信度、脱敏状态、可重放性和结论；
5. `Attempts`：做过什么、结果状态、摘要和是否可重试；
6. `Stop Status`：停止事件、确认状态、工作流同步和停止失败的资源类型。

不要把所有“不成功”都归因于 Skill 方法：

| 现象 | 优先判断 | 对 Skill 的处理 |
| --- | --- | --- |
| Tool 正常执行且证据支持目标不存在该现象 | 目标 Negative Result | 保留方法，补充适用边界或停止条件 |
| Tool 为失败状态或存在非预期退出码 | Tool 或环境失败 | 先修 Tool、依赖、参数或运行环境 |
| Scope、Approval、预算或 Credential 阻断 | 安全门禁生效 | 修正授权/配置；不要通过 Skill 绕过门禁 |
| 多次执行正常但证据仍不足，步骤选择或决策点不清晰 | Skill 方法问题 | 修改 Procedure、Decision points 或 Expected output |

Report 不包含 Credential 值、原始 HTTP 敏感体、本地路径、Skill 全文或原始终端转录。需要深入复核时，使用报告中的 Evidence、Artifact 和 Execution 标识回到受控事实源。

## 4. 提升到新版本

根据复盘结果修改 Skill，并提升版本：

```bash
riftx skills validate service-verification
riftx skills register service-verification
riftx skills disable service-verification 1.0.0
riftx skills activate service-verification 2.0.0
```

下一次显式选择该 Skill 的 Pentest 会固定 v2。已有 Run 和已生成 Report 继续显示 v1 的 ID、Version、Digest、Source 和原始快照，因此新旧结果可以人工对照，但 RiftX 不自动断言 v2 一定优于 v1。

## 5. 禁用与回滚

禁用当前 active 版本：

```bash
riftx skills disable service-verification
```

禁用只阻止新 Run 使用该版本，不影响已有 Run 的读取和报告。

RiftX R1 不保存 Operator Skill 历史源码包。回滚前必须先从 Git 或备份恢复目标版本的 `SKILL.md` 和 References，再执行：

```bash
riftx skills rollback service-verification 1.0.0
```

如果恢复后的源码 Digest 与已注册版本不一致，回滚会失败关闭。不要通过手工修改数据库绕过校验。

## 6. 当前边界

- Skill 不能扩大 Tool allowlist；
- Skill 不能降低 Tool Approval、绕过 Scope 或获得未声明的 Credential Reference；
- 一个版本的源码发生任何语义修改后必须提升版本；
- 自动评分、自动改写、自动批准、Marketplace、Replay Lab 和 Trajectory Store 不属于当前正式版范围。
