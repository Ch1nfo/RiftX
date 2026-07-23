# RiftX

RiftX is an AI-assisted penetration testing agent for explicitly authorized security assessments. It combines the open-source Codex agent runtime with isolated execution environments, enforceable network scope, human approval, structured pentest state, evidence, audit trails, and report generation.

RiftX 是面向明确授权安全测试的智能渗透测试 Agent。项目以开源 Codex Agent 运行时为底座，通过隔离执行环境、网络层 scope 强制、人工审批、结构化渗透状态、证据与审计链路完成可控的安全测试闭环。

> RiftX 仅用于具有明确书面授权、目标范围和有效时间窗的安全测试。

## 当前状态

项目已完成 P0-P6 的代码基线：Codex App Server/exec-server 远程环境、Gateway/CLI、managerd、Linux 网络策略、四个结构化工具、Agent 隔离、审批、状态、审计和报告链路已落地。P7 的 Docker 安全验收由 Ubuntu CI 执行；macOS 只用于功能开发。

- 技术方案：[RiftX-技术实现方案.md](./RiftX-技术实现方案.md)
- 上游锁定：[codex-upstream.lock](./codex-upstream.lock)
- 同步约定：[UPSTREAM.md](./UPSTREAM.md)

## 目标架构

```text
RiftX UI/API
  -> RiftX Gateway
  -> Codex App Server/Core
  -> sandbox-managerd
  -> sandbox exec-server
  -> authorized security targets
```

Codex Core 不直接访问 Docker API。`sandbox-managerd` 独占容器生命周期、资源限制、网络策略、artifact 和 kill switch；所有目标探测必须在 engagement 对应的 sandbox 内执行。

## 源码布局

```text
codex-rs/                       Codex Rust workspace and RiftX Rust crates
codex-rs/app-server/            Codex App Server
codex-rs/app-server-protocol/   App Server protocol types
codex-rs/core/                  Codex agent core
codex-rs/exec-server/           Remote process and PTY execution service
codex-cli/                      Legacy TypeScript CLI package
sdk/                            Codex SDKs
services/sandbox-managerd/      Docker lifecycle and Linux network policy
deploy/sandbox/                 RiftX sandbox image and fixed tool assets
deploy/demo/                    Digest-pinned Juice Shop and DVWA fixtures
security/                       Linux security acceptance criteria
RiftX-技术实现方案.md            RiftX architecture and delivery plan
```

RiftX 自有模块位于 `codex-rs/riftx-*` 和 `services/sandbox-managerd`，领域模型不写入 `codex-core`。

## 开发基线

仓库内 Agent 相关命令统一通过 conda 的 `agent` 环境执行。Rust workspace 使用上游固定的 toolchain，首次构建前需确保该环境可以调用 `rustup`、`cargo` 和 `just`。

```bash
conda run -n agent rustc --version
conda run -n agent just --version
conda run -n agent sh -lc 'cd codex-rs && cargo check -p codex-app-server -p codex-exec-server'
```

后续修改 `codex-rs` 时遵循根目录 [AGENTS.md](./AGENTS.md) 的格式化与测试要求。

## Upstream Attribution

RiftX incorporates source code from [openai/codex](https://github.com/openai/codex), pinned to the commit recorded in `codex-upstream.lock`. The upstream Apache-2.0 `LICENSE` and `NOTICE` files are retained. RiftX is an independent project and is not an official OpenAI product.
