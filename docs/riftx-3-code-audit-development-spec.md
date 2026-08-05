# RiftX 3.0 — 本地代码安全审计开发规格

> 状态：Accepted / 当前开发目标
>
> 文档日期：2026-08-04（Asia/Shanghai）
>
> 目标版本：3.0.0
>
> 规格版本：`riftx.code-audit-development-spec/v3-local-static`
>
> 产品名称：RiftX Code Audit

## 0. 文档目的

本文档定义 RiftX 3.0 Code Audit 的精简目标：

> 用户指定当前机器上的一个代码文件夹，RiftX 只读取其中的文件，分析可能存在的安全问题，并输出
> Findings 和审计报告。

本文是后续开发的权威规格。旧规格中的多 Agent、远程 Node、Docker/Linux 强制隔离、动态验证、
自动修复、Diff/Deep、评测体系、发布加固等高级能力不再属于当前 3.0 目标。

执行本计划时遵守：

1. 每次只完成一个可独立验收的任务。
2. 每个任务必须包含实现、测试、进度记录和本地 Git 提交。
3. 所有 agent 相关测试和运行使用 Conda `agent` 环境。
4. 不执行目标文件夹中的代码、脚本、Hook、构建或测试命令。
5. 不修改用户指定的原始代码文件夹。
6. 发现问题不等于证明没有其他问题；报告不得宣称项目绝对安全。

## 1. 产品目标

用户应当能够：

1. 指定当前机器上的一个文件夹。
2. 创建一次本地 Code Audit。
3. 查看文件扫描进度和最终状态。
4. 查看发现的安全问题。
5. 按严重性、类型和文件筛选问题。
6. 查看问题所在文件、行号、证据、风险说明和人工处理建议。
7. 导出 JSON 或 Markdown 报告。

完整流程：

```text
选择本地文件夹
    ↓
路径与规模检查
    ↓
创建稳定 Snapshot
    ↓
文件清单与语言识别
    ↓
运行内置静态安全规则
    ↓
归一化并去重 Findings
    ↓
生成报告
```

## 2. 明确不做的功能

以下能力不属于当前 3.0 范围，相关旧计划应删除或停止继续实现：

- 自动修改代码、自动生成补丁或 Fix Workflow；
- Retest、Patch Snapshot 和修复生命周期；
- Build、Test、PoC、Exploit 或任何动态执行；
- 执行目标项目的安装脚本、包管理器或编译命令；
- Docker、Linux VM、远程 Runner 或另一台机器作为必需依赖；
- 跨机器 Source Node、Analysis Node、远程 CAS 或源码传输；
- 多 Agent Hunter/Skeptic/System Mapper 工作流；
- 模型 Egress Broker、远程模型审批和复杂预算编排；
- Standard/Deep/Diff 多模式；当前只有一种本地静态审计模式；
- Baseline、跨版本 Finding 生命周期和长期历史比较；
- 外部 Scanner、SARIF 导入和第三方规则插件；
- 复杂 Approval、Validation Plan、动态 Sandbox 和 Capsule；
- 评测 Corpus、holdout、评分体系、发布认证、GA 加固矩阵；
- SBOM/许可证/独立性发布审查体系；
- 复杂分布式 Workflow、Child Workflow 和跨节点恢复；
- 自动证明“仓库安全”或“没有漏洞”。

旧实现中的相关代码可以分阶段退役，但不得阻塞本地静态审计主流程。

## 3. 部署与运行边界

### 3.1 单机运行

以下组件全部运行在启动 RiftX 的同一台机器上：

- Control Plane；
- 本地 Audit Worker；
- 用户指定的代码文件夹；
- SnapshotStore；
- SQLite 数据库；
- 内置 Detector；
- Finding 和报告存储。

核心审计不得要求：

- Docker daemon；
- Linux 专用主机；
- 虚拟机；
- SSH 主机；
- 远程 Runner；
- 云端代码存储；
- 远程数据库。

macOS 和 Linux 都必须支持同一套核心流程。

### 3.2 信任边界

当前模式面向本机单用户主动选择的代码文件夹。RiftX 把文件内容作为待检查数据，但仍不信任其中的：

- 路径和 symlink；
- Git 配置、Hook、filter、helper；
- 文件声明的类型和编码；
- package scripts；
- SECURITY.md、README 或注释中的操作指令；
- 可执行文件、插件或动态模块。

RiftX 只允许读取，不允许执行这些内容。

## 4. 本地文件夹输入

### 4.1 输入要求

审计请求至少包含：

```text
source_path
include_patterns（可选）
exclude_patterns（可选）
```

`source_path` 必须：

- 是当前机器上的绝对目录；
- 位于配置允许的 source roots 内；
- 经过 realpath 解析后仍位于允许范围；
- 不是 symlink；
- 不与 RiftX state、SnapshotStore 或输出目录重叠；
- 在创建审计时可读取。

Git 仓库可以提供 commit/branch/dirty 状态等附加信息，但 Git 不是强制条件。普通代码文件夹也必须能够
审计。

### 4.2 默认排除目录

默认跳过明显不需要扫描的大型或生成目录，例如：

```text
.git
.hg
.svn
node_modules
vendor
dist
build
target
.venv
venv
__pycache__
.cache
coverage
```

用户可以增加排除规则，但不能用 include/exclude 逃逸出 `source_path`。

### 4.3 输入上限

配置必须提供：

- 最大文件数量；
- 单文件最大字节数；
- 总输入最大字节数；
- 最大路径长度；
- 最大目录深度；
- 单次审计最大时间。

超过上限时应返回明确错误或 warning，不得无限读取。

## 5. Snapshot 与只读保证

### 5.1 为什么保留 Snapshot

Snapshot 用于保证扫描期间看到稳定内容，并让 Finding 能绑定到准确的文件 digest。它不是容器挂载，
也不需要 Docker。

### 5.2 Snapshot 内容

每个 Snapshot 至少保存：

```text
snapshot_id
source identity digest
manifest digest
created_at
file entries[]
```

每个文件 entry 至少包含：

```text
relative_path
object_type
size
mode
content_digest
```

### 5.3 Snapshot 安全要求

- 原始文件夹在审计前后保持不变；
- 文件使用 no-follow 方式打开；
- symlink 只记录 link target，不跟随到 source root 外；
- special file、socket、FIFO 和 device 默认跳过并产生 warning；
- 内容复制或读取前后检查 inode/fingerprint，发现变化则本次 Snapshot 失败；
- SnapshotStore 对象按 digest 校验；
- 损坏或被修改的 Snapshot 不允许继续扫描；
- 绝对 source path 和 CAS locator 不进入普通 API 和报告。

## 6. 文件清单与 Scope

扫描前创建稳定文件清单。每个文件至少记录：

```text
relative_path
language
category
size
content_digest
included | excluded | skipped
reason
```

第一阶段支持的主要语言：

- Python；
- JavaScript；
- TypeScript；
- JSON、YAML、TOML、INI 等配置文件；
- 常见 dependency manifest 和 lock file；
- Dockerfile、CI 配置和 shell 文件作为文本配置检查对象。

无法识别的文本文件可以进入通用 secret/config 检查。二进制文件默认跳过。

## 7. Detector 模型

### 7.1 Detector 约束

Detector 必须是 RiftX 内置、版本固定的 Python 实现。Detector：

- 只接收文件 metadata 和 bounded bytes/text；
- 不接收绝对路径；
- 不使用 shell；
- 不执行目标代码；
- 不加载目标仓库模块或插件；
- 不访问网络；
- 不直接写数据库；
- 返回严格类型的 Signal。

### 7.2 第一阶段 Detector

必须实现以下实用检测类型。

#### Secret Detector

- 常见 API key/token/private key 格式；
- 高风险 credential 文件内容；
- 日志或配置中的明文密码；
- 输出证据必须脱敏，不在报告中完整暴露 secret。

#### Dependency Detector

- 识别 package manifest 和 lock file；
- 检查明显危险的 dependency source；
- 检查未固定版本、HTTP source、Git branch dependency 等规则；
- 当前阶段不要求联网查询漏洞数据库。

#### Configuration Detector

- debug/verbose mode；
- 公开监听地址；
- 弱认证或默认密码；
- 宽松 CORS；
- TLS 校验关闭；
- 危险文件权限；
- CI workflow 中的高风险权限或 secret 使用；
- Dockerfile 中的 root user、敏感文件复制等问题。

#### Source Detector

Python、JavaScript、TypeScript 第一阶段至少覆盖：

- shell/command injection 风险；
- SQL 字符串拼接；
- 路径遍历；
- 不安全反序列化；
- 弱随机数用于安全场景；
- TLS 校验关闭；
- 硬编码凭据；
- 危险 `eval`/动态执行；
- 开放重定向或 SSRF 的明显模式；
- 不安全临时文件和权限使用。

规则应尽可能使用 AST；无法稳定解析时可以使用保守文本规则，但必须降低 confidence，不能伪装成确定
漏洞。

## 8. Signal 与 Finding

### 8.1 Signal

Detector 输出 Signal，至少包含：

```text
rule_id
rule_version
category
severity
confidence
relative_path
start_line/end_line
evidence_digest
message
```

### 8.2 Finding

Signal 经过归一化和去重后形成 Finding：

```text
finding_id
audit_id
rule_id
severity: critical | high | medium | low | info
confidence: high | medium | low
category
title
description
relative_path
start_line/end_line
evidence_excerpt
recommendation
created_at
```

`recommendation` 只提供人工处理建议，不生成补丁、不修改文件。

### 8.3 去重与排序

Finding identity 至少绑定：

```text
rule_id + relative_path + normalized location + evidence digest
```

报告默认按以下顺序排序：

1. severity；
2. relative path；
3. line；
4. rule id。

## 9. 审计状态

精简状态机：

```text
draft -> queued -> scanning -> completed
                         -> failed
                         -> cancelled
```

规则：

- 同一个启动请求只能创建一个扫描 Job；
- 扫描失败必须保存稳定 failure code；
- cancel 后不再产生新 Finding；
- completed 必须表示所有 included 文件都有明确处理结果；
- 有 warning 可以 completed，但报告必须展示 skipped/unsupported 文件；
- “0 Findings”只能表示规则未发现问题，不能表示代码绝对安全。

核心流程使用单个本地 Job，不需要远程 Runner、Child Workflow 或复杂分布式编排。

## 10. 报告

### 10.1 报告内容

报告至少包含：

- 审计 ID 和时间；
- Snapshot/Manifest digest；
- 扫描文件数、跳过文件数和总字节数；
- 语言分布；
- Finding 总数和 severity 汇总；
- 每个 Finding 的位置、证据、风险和人工建议；
- warnings 和 unsupported 文件；
- Detector/rule 版本；
- “未发现不代表绝对安全”的声明。

### 10.2 输出格式

当前版本只要求：

- JSON：供 API、CLI 和后续集成使用；
- Markdown：供用户直接阅读和保存。

不要求 HTML、PDF、SARIF、SBOM 或复杂发布包。

## 11. 最小 API、CLI 与 WebUI

### 11.1 API

只需要以下核心接口：

```text
POST   /audits
POST   /audits/{audit_id}/start
POST   /audits/{audit_id}/cancel
GET    /audits/{audit_id}
GET    /audits/{audit_id}/findings
GET    /audits/{audit_id}/findings/{finding_id}
GET    /audits/{audit_id}/report
```

### 11.2 CLI

核心命令：

```text
riftx audit <folder>
riftx audit status <audit_id>
riftx audit findings <audit_id>
riftx audit report <audit_id> --format json|markdown
riftx audit cancel <audit_id>
```

### 11.3 WebUI

只需要三个简单页面：

1. 新建审计：选择或输入本地文件夹；
2. 审计详情：进度、文件统计、severity 汇总；
3. Findings：列表和单项详情。

不实现复杂 Threat Model、Coverage、Baseline、Deep Workflow、Fix、Retest 或审批页面。

## 12. 最小数据模型

优先复用现有 `Audit`、`Run`、Artifact 和 Snapshot 基础。新增或保留的核心事实只有：

```text
Audit
AuditRun
SourceSnapshot
SnapshotFile
AuditSignal
AuditFinding
AuditReport
```

不需要：

- Baseline/Occurrence 历史比较模型；
- Fix/Patch/Retest 模型；
- ValidationPlan/Approval 模型；
- EgressSession；
- Agent WorkItem/Child Workflow；
- Dynamic Capsule；
- 评测 Corpus/Truth/Score；
- 发布 Candidate/Qualification 模型。

## 13. 现有实现的保留与退役

### 13.1 继续保留

以下已实现基础直接复用：

- `code_audit` RunKind；
- Audit domain、ORM、Repository 和 ApplicationService；
- Audit API authorization 基础；
- Restricted Artifact 访问控制；
- local source root authorization；
- Git preflight 中可复用的本地信息读取；
- `LocalSnapshotStore` 和 Snapshot CAS；
- commit/working-tree deterministic materializer；
- Source Manifest、path safety 和 hash verification；
- Feature Flag 和现有回归测试基础。

### 13.2 退役或停止继续开发

以下实现不进入精简产品主路径：

- Docker Snapshot mount backend；
- pinned Snapshot mount runtime image；
- Linux qualification scripts；
- Snapshot mount release evidence pipeline；
- mount Lease/Pin/StopProof 的产品接线；
- SourceIngest Docker/Linux Capsule 的产品要求；
- cross-node routing；
- dynamic execution；
- Agent workflow；
- Fix/Retest；
- Diff/Deep；
- evaluation/hardening/release 专项功能。

已提交的数据库 migration 若删除会破坏升级/降级兼容，可以暂时保留为空闲历史表，但不得成为新审计
流程依赖。

## 14. 精简实施计划

### S0 — 范围清理

任务 `AUD-S001`：

- 删除 Docker runtime、qualification scripts 和对应 production exports；
- 删除只服务 Docker mount 的测试与 release gate；
- 保留必要历史 migration，但停止产品接线；
- 更新配置、文档和进度 ledger。

退出条件：macOS/Linux 启动和测试不需要 Docker。

### S1 — 本地文件夹与 Snapshot

任务 `AUD-S100`：本地文件夹 admission

- 支持 Git 和普通目录；
- 路径、allowed roots、重叠、symlink 和上限检查；
- 输出稳定 SourceIdentity。

任务 `AUD-S101`：Local Snapshot View

- 复用 SnapshotStore；
- owner-bound descriptor open；
- deterministic file enumeration；
- bounded bytes/text read；
- 不暴露绝对路径和 locator。

任务 `AUD-S102`：SourceSnapshot seal

- 原子保存 Snapshot、Manifest 和 Audit reference；
- retry 返回相同结果；
- 原文件夹变化时拒绝旧 Snapshot。

### S2 — Inventory 与 Detector

任务 `AUD-S200`：文件 Inventory 与 Scope

- 默认排除；
- language/category detection；
- included/excluded/skipped reason；
- 文件及字节统计。

任务 `AUD-S201`：Detector registry 与 runner

- 固定 rule metadata；
- bounded input/output；
- 单文件失败隔离；
- deterministic ordering；
- cancel fence。

任务 `AUD-S202`：内置安全规则

- Secret；
- Dependency；
- Configuration；
- Python；
- JavaScript/TypeScript。

### S3 — Findings 与报告

任务 `AUD-S300`：Signal normalization 与 Finding

- severity/confidence；
- 去重和稳定 ID；
- evidence excerpt 脱敏；
- 文件/行号定位。

任务 `AUD-S301`：JSON/Markdown Report

- 汇总；
- Findings；
- skipped/unsupported；
- rule versions；
- 确定性输出。

### S4 — 本地产品接线

任务 `AUD-S400`：本地 Audit Job

- draft/start/cancel/status；
- 单机 Worker；
- 重启后读取状态；
- completed/failed/cancelled 收敛。

任务 `AUD-S401`：最小 API 与 CLI

- 创建、启动、取消、查询；
- Findings 和 Report；
- CLI 本地文件夹入口。

任务 `AUD-S402`：最小 WebUI

- 新建审计；
- 审计进度；
- Findings 列表和详情。

### S5 — 端到端验收

任务 `AUD-S500`：本地文件夹端到端测试

- macOS 与 Linux 均不要求 Docker；
- 普通目录和 Git 目录都可扫描；
- seeded vulnerable fixture 产生预期 Findings；
- safe fixture 不产生对应误报；
- 原目录扫描前后 digest 不变；
- 取消后无迟到 Finding；
- JSON/Markdown 报告可读取；
- API、CLI 和 WebUI 显示同一权威结果。

完成 `AUD-S500` 即代表当前精简 3.0 Code Audit 功能完成。

## 15. 测试要求

每个任务至少运行：

```bash
conda run --no-capture-output -n agent python -m pytest -q <targeted tests>
conda run --no-capture-output -n agent python -m ruff check <changed paths>
git diff --check
```

高影响任务还需运行完整测试：

```bash
conda run --no-capture-output -n agent python -m pytest -q
conda run --no-capture-output -n agent python -m ruff check src tests migrations scripts/qa
```

测试必须覆盖：

- path traversal 和 symlink escape；
- 文件扫描期间变化；
- 超大文件和文件数量上限；
- invalid UTF-8 和二进制文件；
- Detector 异常；
- duplicate Finding；
- secret evidence 脱敏；
- cancel race；
- 数据库重启读取；
- 原文件夹不被修改。

## 16. 最终验收标准

3.0 精简目标完成必须同时满足：

1. 用户能在当前机器指定一个本地文件夹。
2. 审计不需要 Docker、Linux VM、远程 Runner 或另一台机器。
3. RiftX 不执行目标项目代码。
4. 原始文件夹在审计前后保持不变。
5. Snapshot 和文件清单可复现。
6. Python、JavaScript/TypeScript、dependency、configuration 和 secret 规则可运行。
7. Findings 包含 severity、confidence、文件、行号、证据和风险说明。
8. 用户能通过 API、CLI 和最小 WebUI 查看结果。
9. 用户能导出 JSON 和 Markdown 报告。
10. 取消、失败和重启不会产生重复或迟到 Findings。
11. 完整测试和现有 RiftX 回归测试通过。

不要求自动修复、不要求评测体系、不要求发布认证，也不要求证明代码绝对安全。

## 17. 每次任务交付格式

每个任务完成时报告：

```text
Task ID:
Outcome:
Files changed:
Schema/migration impact:
Security boundary impact:
Tests run:
Test results:
Manual verification:
Known limitations:
Progress document updated:
Next task:
Git commit:
```

如果当前任务未通过测试或仍存在关键缺口，必须保持 `in_progress`，不得用 TODO、空实现、跳过测试或
降低断言伪装成完成。
