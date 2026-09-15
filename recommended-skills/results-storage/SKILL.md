---
name: results-storage
description: How pentest results persist in the TSec benchmark — the challenge blackboard via benchmark_control checkpoint, plus the per-challenge work/ directory that survives across attempts. Use this skill when user needs to persist conclusions across attempts, hand off to the next worker, or asks where test results are saved.
---

# RiftX 结果存储（Benchmark 运行时）

Benchmark 运行时没有独立的结果数据库，也没有 Findings 面板。持久化分两层，分工明确：**黑板存结论，work/ 存原始产物**。

## 黑板（benchmark_control checkpoint）

- 跨 attempt、跨 worker 持久——每个后续 worker 接手时读到的就是黑板摘要，这是唯一可靠的跨会话记忆
- 关键参数：`uniqueCode`、`signal`（≤2000 字符：事实 + 证据 + 剩余不确定性）、`signalKind`（foothold / credential / privilege_change / exploit_primitive / stage_transition / decisive_rule_out / new_surface / note）、`evidenceRef`（指向稳定证据：work/ 产物路径、请求 ref、URL）、`supersedesEvidenceRef`、`triedFamilies`、`ruledOutFamilies`、`currentApproach`、`nextProbe`
- 每个有证据支撑的具体结论一条 checkpoint：侦察类发现用 signalKind=new_surface/note，攻击进展用 foothold/credential 等；已被推翻的结论用 supersedesEvidenceRef 修正

## work/ 目录（本题内跨 attempt 持久）

- cwd 就是本题的 work/，attempt 结束不清理——原始扫描输出、取回的文件、临时脚本都落在这里
- 推荐布局：`work/loot/`（凭据、敏感数据）、`work/exploits/`（可用脚本与 PoC）、`work/notes.md`（自由笔记与线索）
- 大产物（response dump、nmap XML、爆破结果）只落盘，checkpoint 的 signal 里只写路径——signal ≤2000 字符，塞原始输出会把关键结论挤掉

## flag 立即 submit

拿到 flag 立即 `benchmark_control` submit：`uniqueCode` + `flag`，仅此两个参数——**无证据前置条件**，不要等"证据齐了"再交。

## 反模式

- 凭据只写 notes 不 checkpoint——后续 worker 先读黑板，黑板之外的 work/ 内容没人保证被看到；拿到可用凭据必须 checkpoint（signalKind: credential）
- signal 超长——把原始响应/扫描输出整段塞进 signal；输出落盘 work/，signal 只留结论 + 产物路径
