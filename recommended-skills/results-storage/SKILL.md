---
name: results-storage
description: How facts and artifacts persist in the benchmark runtime — the benchmark_control checkpoint blackboard and the per-challenge work/ directory. Use this skill when user needs to record findings, persist credentials or footholds across attempts, or asks where test results are saved.
---

# Benchmark 运行时的事实与产物持久化

持久化有两层，分工明确：

- **黑板（checkpoint）**：`benchmark_control(action="checkpoint")` 写入的事实性结论，跨 attempt、跨 worker 共享——压缩或换 worker 后仍可恢复
- **work/ 目录**：本地 cwd 即本题 work/ 目录，跨 attempt 持久（目标环境会重置，本地不会）——但只在本题内有效

## work/ 目录布局（推荐）

```
work/
├── loot/        # 凭据、flag 线索、token dump
├── exploits/    # poc、stage-NN-*.sh 重放脚本
└── notes.md     # 阶段图 / 凭据矩阵 / 待办
```

大产物（响应转储、数据库 dump、扫描输出）落盘 work/ 后，只把**路径**写进 checkpoint 的 `evidenceRef`，不要把内容塞进黑板。

## 事实性结论必须写黑板

凭据、foothold、阶段跃迁、决定性排除等事实性结论，必须 `benchmark_control(action="checkpoint")` 写黑板，不能只存文件：

| 参数 | 说明 |
| :--- | :--- |
| `uniqueCode` | 本题唯一码 |
| `signal` | 事实观察 + 证据 + 不确定性，≤2000 字符 |
| `signalKind` | `foothold` / `credential` / `privilege_change` / `exploit_primitive` / `stage_transition` / `decisive_rule_out` / `new_surface` / `note` |
| `evidenceRef` | 稳定引用——work/ 下落盘文件路径、请求或产物 |
| `triedFamilies` / `ruledOutFamilies` | 已试 / 已排除的打法族 |

拿到 flag 时立即 `benchmark_control(action="submit")`——不要只把 flag 存进 work/ 文件，提交动作本身才是终点。

## 反模式

- 凭据只写 notes.md 不 checkpoint——压缩或换 worker 即丢
- `signal` 超长（上限 2000 字符）——大内容落盘 work/，路径进 evidenceRef
- 把响应体 / dump 原文塞进黑板——黑板放结论与引用，产物放 work/
