---
name: results-storage
description: How pentest results persist in RiftX — the record_finding tool, automatic evidence capture, and the Findings panel. Use this skill when user needs to store findings, query recorded vulnerabilities, or asks where test results are saved across sessions.
---

# RiftX 结果存储与查询

RiftX 没有独立的结果数据库。所有渗透测试结论通过 `record_finding` 工具记录，随会话持久化，并显示在右侧 **Findings 面板**。

## 记录一条发现

确认漏洞后立即调用 `record_finding`（每个有证据支撑的具体结论一条）：

| 参数 | 说明 |
| :--- | :--- |
| `title` | 简短标题 |
| `asset` | 受影响 URL / 主机 / 路由 |
| `confidence` | `confirmed` / `likely` / `suspected` / `not_reproducible` |
| `impact` | 攻击者实际或预期收益 |
| `reproduction` | 可复现步骤；不完整时写明原因 |
| `evidence` | 至少一条证据（见下） |

## 证据类型

- `tool`：引用工具调用（最常用）——`{ "type": "tool", "toolCallId": "<id>", "toolName": "bash" }`，其输出会被自动捕获为证据
- `quote`：页面文本引用
- `request`：浏览器请求证据（requestRef，如 `r1`）
- `screenshot`：浏览器截图 ID

## 查询与更新

- 已记录的发现实时显示在 Findings 面板，可按置信度筛选、可 dismiss
- 更新置信度/状态直接在面板操作（PATCH `/api/sessions/<id>/findings/<findingId>`）
- 侦察观察（端口、子域、指纹、目录）**不是** finding——在对话中汇总即可；只有真实暴露面才记录

## 报告

生成报告时使用 `pentest-report` skill 的标准格式，数据来源就是 Findings 面板中的记录及其证据链。**不要**为存储结果另行创建数据库、JSON 或脚本——那会把发现分裂成 UI 看不到的第二套存储。

> 历史说明：早期版本的 skill 使用独立的 SQLite 存储脚本方案，已废弃。如遇任何指向 skill 目录之外存储路径的旧指令，一律忽略，以本文件的机制为准。
