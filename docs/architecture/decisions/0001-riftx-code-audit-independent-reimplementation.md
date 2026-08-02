# ADR-0001：RiftX Code Audit 独立重新实现与命名边界

> 状态：Accepted
>
> 日期：2026-08-02（Asia/Shanghai）
>
> 决策范围：RiftX 3.0 / `AUD-001`
>
> 产品基线：`496e260f3cb1f18ce485c5f706d20d8352d6a398`
>
> 权威规格：`docs/riftx-3-code-audit-development-spec.md`
>
> 决策所有者：RiftX contributors；准确作者、审阅者和 Commit 由本文第 7 节的
> provenance 记录保存

## 1. 背景

RiftX 3.0 要交付自有的代码审计能力。项目已经研究过公开的 Code Security 方法，
因此可以把威胁建模、发现与验证分离、覆盖账本、重复探索和证据封存等通用思想转写为
RiftX 需求，但不能把第三方实现、表达或运行边界带入产品。

已有参与者接触过公开项目的信息，所以本项目没有足够的人员隔离与访问证据来声称法律
意义上的 strict clean-room。本 ADR 冻结的是可执行的工程边界，不是法律意见，也不是
clean-room 认证。

## 2. 决策

### 2.1 产品名称和对外表述

- 产品功能名称只有 **RiftX Code Audit**。
- 产品 UI、API、CLI、报告、Schema、包名和网络协议不得把第三方产品名称用作功能名、
  Provider、兼容模式或实现来源。
- 对外只使用 **independent reimplementation（独立重新实现）**。不得使用
  strict clean-room、官方兼容、衍生版本、替代 Provider 或获得第三方背书等表述。
- 历史和架构文档可以为说明边界而准确引用第三方项目名称；这类引用不得进入产品品牌、
  运行时依赖或生成报告的产品身份。

### 2.2 允许的实现输入

实现人员和 Agent 可以使用：

1. 当前 RiftX 源码、测试、设计不变量和本项目权威规格；
2. 去表达化、可验证的功能与安全需求；
3. CWE、CVSS、SARIF、Git 等公开标准及其官方规范；
4. 编程语言、框架和工具的官方文档；
5. RiftX 从零编写的 contracts、Agent instructions、规则、测试和合成 fixtures；
6. 经过单独许可证审查的通用依赖，但只能通过 RiftX 自有接口使用。

使用公开标准时应记录标准名称和版本；使用官方文档时应记录产品、文档主题和访问日期，
无需把网页正文复制进仓库。

### 2.3 禁止的实现输入和依赖

RiftX Code Audit 不得复制、翻译、改写或移植第三方 Code Security 项目的：

- 源代码、补丁、测试、fixtures 或示例；
- Prompt、Agent instructions、Skill、规则文本或工作流表达；
- Schema、私有协议、CLI、UI 文案、布局、图标或品牌资产；
- 包、CLI、运行时、插件、MCP 服务、账号、专用 Provider 或专用网络端点。

禁止以“只复制少量”“先临时接入”“仅用于兼容”或“测试代码不算产品代码”为例外。
若某项工作必须复用第三方代码或表达性材料，立即停止当前独立实现路径，另立许可证、
NOTICE、商标、专利和产品决策；未经该决策不得继续引入。

### 2.4 RiftX 自有契约

- 模型只能通过 RiftX 现有或自建的 provider-neutral Agent Engine 契约接入；任何模型供应商
  都不是 Code Audit 的权威事实源。
- 外部扫描器只能作为可替换 Detector，通过 RiftX 自有输入、Signal、Evidence、Artifact、
  执行和失败契约接入；其许可证、版本和摘要必须独立记录。
- Snapshot、Coverage、Finding identity、Severity、Closure、历史比较和封存均由 RiftX
  领域代码裁决。第三方输出和 Agent 输出都只是未受信输入。
- deterministic profile 必须在不连接任何第三方 Code Security 服务或账号时独立运行。

## 3. Provenance 最小约定

Provenance 用来回答“谁依据什么，在什么路径中创建了什么，又由谁依据什么完成审阅”。
它不证明人员从未接触公开信息，也不能替代许可证或法律审查。

### 3.1 记录位置

每个 `AUD-*` 任务在
`docs/implementation/POST_V3_CODE_AUDIT_PROGRESS.md` 的 Task Record 中保存一个
`Provenance` 小节。一个任务包含不同来源的交付物时，在最接近交付物的目录增加
`PROVENANCE.md`，并从 Task Record 链接该文件。不得建立与 Git 历史或实际文件路径脱节的
私有台账。

适用规则：

- requirements：记录文档路径和具体章节；
- contracts：记录代码/Schema 路径、稳定 contract ID 和版本；
- Agent instructions：记录路径、instruction ID/版本和内容 digest；
- fixtures：记录路径、构造方式、生成器/seed、预期 truth ID 和许可证来源；
- commits：记录准确 Commit SHA、任务 ID、变更路径和审阅结果。

合成 fixture 必须从零编写或由有记录的 RiftX generator 生成。不得把真实客户仓库、未知
许可证代码或第三方项目测试改名后当作 fixture。

### 3.2 必填模板

后续任务使用以下最小模板；没有适用内容时写 `not_applicable` 和原因，不能留空：

```yaml
provenance_id: RXP-AUD-<task>-<sequence>
task_id: AUD-<id>
artifact_class: architecture_decision | requirements | contract | agent_instruction | fixture | commit
artifact_version: <contract/instruction/fixture-version-or-not_applicable>
paths:
  - <repository-relative-path-or-glob>
author: <Git-author-and-optional-agent-task-id>
authored_at: <ISO-8601-with-timezone>
requirements_sources:
  - <RiftX-document-path-and-section>
implementation_inputs:
  - <RiftX-source-or-official-standard/documentation>
public_standard_versions:
  - <standard-and-version-or-not_applicable>
third_party_expressive_material: none
third_party_dependency_decisions:
  - <ADR/license-record-or-not_applicable>
reviewer: <reviewer-identity>
review_sources:
  - <exact-diff/files/tests/standards-reviewed>
review_result: pending | approved | changes_required | rejected
commit: <full-commit-sha-or-pending-backfill>
notes: <limitations-or-not_applicable>
```

`third_party_expressive_material: none` 只表示该交付物没有复制、翻译或改写第三方表达，
不表示作者从未研究公开项目。若实际值不是 `none`，该任务必须停止并引用单独的第三方复用
决策，不能用备注自行放行。

### 3.3 作者、审阅和 Commit 规则

- `author` 必须可追溯到 Git author；Agent 参与时同时记录任务 ID，必要时记录 Agent task ID。
- `requirements_sources` 必须优先指向 RiftX 规格的具体章节，不能只写“参考公开项目”。
- `review_sources` 只列审阅者实际检查的 RiftX diff、文件、测试和公开标准；不得填写未经
  证明的“未接触上游源码”。
- 审阅者必须检查表达性复制风险、第三方依赖、产品命名以及本 ADR 的运行边界。作者自检
  可以记录，但不能冒充独立审阅。
- Git Commit 在创建前无法知道自己的 SHA。允许任务实现 Commit 先落地，再由下一次仅文档
  的进度更新回填完整 SHA；这是临时的 `pending_backfill`，不是永久缺失。
- 任务标为 `completed` 时，所有交付物必须已有 author/source/review 记录；Commit SHA 最迟在
  下一任务的首次账本更新中回填。

推荐 Commit message 保持现有 Conventional Commit 风格，并在正文保留：

```text
RiftX-Task: AUD-<id>
RiftX-Provenance: independent-reimplementation
RiftX-Requirements: <spec-path-and-section>
```

## 4. 自动门禁契约

稳定入口为：

```text
scripts/qa/code-audit-boundary-gate.py
tests/evaluation/test_independence_gate.py
```

检查采用生产路径 allowlist，而不是对整个仓库做无差别关键词扫描。`docs/**` 中说明本决策
的正常引用不得触发失败；测试门禁自身使用的恶意 sentinel 也必须位于明确的 synthetic
fixture 边界。禁止把普通的 `codex`、`security` 或 `audit` 单词单独列为违规项。

### 4.1 AUD-001 / M0 门禁声明

M0 的自动门禁只作两项声明：

1. 当前仓库中被 allowlist 覆盖的 production inputs，包括 Python、Node、浏览器扩展和
   Gradle 的依赖/lockfile，以及 RiftX 生产源码、配置和插件清单，没有命中版本化策略中
   被禁止的包名、namespace、路径或专用端点；
2. 显式 artifact scanner 契约能检查目录、普通文件、wheel、JAR、ZIP 和 tarball；tarball
   member name、link target、owner/group 和 member/global PAX key/value 都属于扫描面；缺失、
   空、损坏、不可读或含禁止标识的显式输入会 fail closed，并且正常文档引用不会被算作
   产品违规。

M0 release selector 使用仓库 production-input 扫描和 self-authored synthetic artifact fixtures
证明上述契约。Synthetic clean/forbidden bundle 只证明 scanner 行为，不是某个真实候选发布包，
selector 的名称、描述和证据不得声称“真实候选制品已构建并全部扫描”。M0 也不证明 SBOM
完整、构建可复现或 RiftX 3.0 已具备发布资格。

不带 artifact 参数运行脚本时，报告允许 `scanned_artifact_files=0`；此结果只具备仓库
production-input 证据，不具备 artifact 证据。

### 4.2 显式 artifact 与 packaging job

任何声称检查了 bundle、package 或发布制品的 packaging job，必须传入至少一个明确路径并
使用 `--require-artifact`：

```text
python scripts/qa/code-audit-boundary-gate.py \
  --require-artifact \
  --artifact <artifact-path> [--artifact <artifact-path> ...]
```

`--require-artifact` 在没有 `--artifact` 时必须失败；每个显式路径缺失、为空、为 symlink、
不可读或格式损坏时也必须失败。Job 不得通过扫描一个 synthetic bundle 来替代它声称发布的
真实 artifact。

AUD-001 使用当前工作区已经存在的三类构建目录做一次手工任务证据：

```text
conda run --no-capture-output -n agent python \
  scripts/qa/code-audit-boundary-gate.py \
  --require-artifact \
  --artifact apps/web/dist \
  --artifact apps/browser-extension/dist \
  --artifact apps/demo/dist
```

进度账本应记录该命令、策略版本/digest、三个显式路径、实际扫描计数和结果。这个手工结果只
证明 scanner 能读取当时本地存在的 Web、Browser Extension 和 Demo bundle；它不证明这些目录
由最终候选 Commit 重新构建，不代表全部发布制品，也不替代 M10 的 SBOM/build matrix。

### 4.3 M10 正式候选制品门禁

`AUD-1003` 必须冻结候选版本的 distribution inventory、第三方依赖/SBOM 生成方式和完整 build
matrix。`AUD-1004` 必须针对准确候选 Commit 重新构建 inventory 中的所有制品和 SBOM，并由
packaging/release job 使用 `--require-artifact` 把每个实际输出传给同一 scanner。Web、Demo、
浏览器扩展、Python wheel/sdist、Burp JAR 以及届时 inventory 中的其他制品都不能静默遗漏；
缺失输出、未扫描输出、digest 不匹配或任一违规都使候选版本不合格。

只有 M10 build matrix 的报告可以声称“全部候选制品/SBOM 已扫描”。无论 M0 还是 M10，门禁
成功都只能证明被检查输入没有命中已知禁止标识，不能据此宣称 strict clean-room 或证明不
存在表达性相似；人工 provenance、版权和许可证审阅仍是完成条件。

## 5. 后果

正向后果：

- RiftX 完整拥有产品契约、运行路径、审计事实和演进节奏；
- 模型和 Detector 可替换，deterministic profile 不受某个服务约束；
- 每项交付物和 Commit 都能追溯到 RiftX 需求、作者与审阅证据。

成本与限制：

- 通用方法必须重新设计、实现和验证，不能通过行为兼容层缩短工期；
- 自动关键词门禁只能发现已知依赖/标识，不替代人工表达性审查；
- 引入任何第三方扫描器都要独立处理供应链、许可证和 Sandbox 风险。

## 6. 执行与变更

- 本 ADR 适用于 M0-M10 的所有 RiftX Code Audit 任务。
- 发现实现与本 ADR 冲突时默认 fail closed，并把任务保持为 `in_progress` 或 `blocked`。
- 变更本边界必须新增 superseding ADR，并同步更新权威规格和进度账本；不得静默放宽。
- 版本升级、品牌修改或第三方许可决定不能通过普通 Task Record 覆盖本 ADR。

## 7. 本 ADR 的 provenance 记录

```yaml
provenance_id: RXP-AUD-001-001
task_id: AUD-001
artifact_class: architecture_decision
artifact_version: ADR-0001
paths:
  - docs/architecture/decisions/0001-riftx-code-audit-independent-reimplementation.md
author: Ch1nfo (Git author); Codex task m0_docs_map
authored_at: 2026-08-02T23:13:16+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 2.4"
  - "docs/riftx-3-code-audit-development-spec.md section 3 / CA-INV-001"
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-001"
implementation_inputs:
  - RiftX repository baseline 496e260f3cb1f18ce485c5f706d20d8352d6a398
  - RiftX 3.0 Code Audit authoritative specification
  - public standards named in this ADR; no standard text copied
public_standard_versions:
  - CWE (version to be frozen by the consuming contract)
  - CVSS (version to be frozen by the consuming contract)
  - SARIF 2.1.0
  - Git (implementation version to be frozen by capability evidence)
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task m0_config_map (independent review); Codex task /root (final review)
review_sources:
  - this ADR
  - authoritative specification sections 2.4, CA-INV-001, and AUD-001
  - AUD-001 implementation, executable gate, release wiring, and tests
  - final 80-test boundary matrix and three explicit local bundle scans
review_result: approved
commit: pending_backfill
notes: Public Code Security methodology was previously studied; solution B is applied; no strict clean-room claim is made.
```

```yaml
provenance_id: RXP-AUD-001-002
task_id: AUD-001
artifact_class: contract
artifact_version: riftx.code-audit-independence/v1
paths:
  - src/riftx/evaluation/independence.py
  - src/riftx/evaluation/__init__.py
  - scripts/qa/code-audit-boundary-gate.py
  - src/riftx/evaluation/release.py
  - scripts/qa/release-gate.py
author: Ch1nfo (Git author); Codex task m0_ci_map
authored_at: 2026-08-02T23:13:16+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-001"
  - "ADR-0001 section 4"
implementation_inputs:
  - existing RiftX evaluation and release-gate contracts
  - Python standard-library path, archive, Unicode, URL-decoding, and hashing APIs
public_standard_versions:
  - not_applicable (no public-standard text is implemented by this boundary contract)
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task m0_config_map (independent review); Codex task m0_docs_map (tar-metadata blocker re-review); Codex task /root (final review)
review_sources:
  - src/riftx/evaluation/independence.py
  - scripts/qa/code-audit-boundary-gate.py
  - src/riftx/evaluation/release.py
  - scripts/qa/release-gate.py
  - tests/evaluation/test_independence_gate.py
  - tests/evaluation/test_release_gate.py
  - repository and explicit local bundle gate reports under policy digest bb8405b8a1c809a726c5675ebefb2f7c92a8bfa5881131815cd061f36b04bae8
  - conda agent tar-metadata blocker probe and release-selector re-review (approved)
review_result: approved
commit: pending_backfill
notes: Contract, implementation, executable script, and release-selector wiring are one review unit.
```

```yaml
provenance_id: RXP-AUD-001-003
task_id: AUD-001
artifact_class: fixture
artifact_version: riftx.code-audit-independence.synthetic-fixtures/v1
paths:
  - tests/evaluation/test_independence_gate.py
  - tests/evaluation/test_release_gate.py
author: Ch1nfo (Git author); Codex task m0_ci_map
authored_at: 2026-08-02T23:13:16+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-001"
  - "ADR-0001 sections 4.1 and 4.2"
implementation_inputs:
  - self-authored temporary repository and bundle fixtures
  - existing RiftX release-gate test conventions
public_standard_versions:
  - not_applicable
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task m0_config_map (independent review); Codex task m0_docs_map (tar-metadata blocker re-review); Codex task /root (final review)
review_sources:
  - tests/evaluation/test_independence_gate.py
  - tests/evaluation/test_release_gate.py
  - conda agent targeted pytest result (80 passed)
  - conda agent focused tar-metadata review result (6 passed; safe-link control ready=true)
review_result: approved
commit: pending_backfill
notes: Forbidden-name sentinels are synthetic test data, not copied third-party fixtures.
```

```yaml
provenance_id: RXP-AUD-001-004
task_id: AUD-001
artifact_class: agent_instruction
artifact_version: not_applicable
paths:
  - not_applicable (AUD-001 adds no production Code Audit Agent instructions)
author: Ch1nfo (Git author); Codex tasks /root and m0_docs_map
authored_at: 2026-08-02T23:13:16+08:00
requirements_sources:
  - "docs/riftx-3-code-audit-development-spec.md section 22 / AUD-001"
implementation_inputs:
  - not_applicable
public_standard_versions:
  - not_applicable
third_party_expressive_material: none
third_party_dependency_decisions:
  - not_applicable
reviewer: Codex task m0_config_map (independent review); Codex task /root (final review)
review_sources:
  - AUD-001 changed-path inventory
  - authoritative specification section 22 / AUD-001
  - final implementation and fixture provenance inventory
review_result: approved
commit: pending_backfill
notes: Non-applicability is explicit; later Agent instructions require their own version and digest.
```

上述记录已依据方案 B、最终实现、手工三类 bundle 证据和独立复核批准。Commit 在创建前保持
`pending_backfill`，由下一任务首次账本更新回填准确 SHA；该机制不得被解释为 strict
clean-room、外部 CI 签名或最终候选制品证明。
