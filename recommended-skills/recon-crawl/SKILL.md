---
name: recon-crawl
description: Attack-surface crawling with the crawl tool — BFS link/form/hidden-field collection, JS-bundle API route extraction, and auth boundary mapping through the scoped browser. Use this skill when user needs to map a web application's attack surface, enumerate endpoints, discover hidden API routes, or build an endpoint inventory before vulnerability testing.
---

# Attack-Surface Crawling (crawl)

## 授权声明

**本 Skill 仅用于授权安全测试**：crawl 走真实浏览器访问目标，必须遵守浏览器 scope 与速率约定；页面预算用最小够用的值，不要无差别扫全站。

---

## RiftX Workflow

1. **拿到入口就爬**：`browser navigate` 建立首屏基线后，立即 `crawl` 一次性拿到结构化攻击面清单——这比手工逐页 snapshot 快一个数量级，且不会漏掉 JS 路由
2. **参数选择**：默认 `maxPages=15, maxDepth=2`；小型站点可 `maxPages=30`；只要单页深度信息时 `maxDepth=0`（只提取入口页自身的链接/表单/JS 路由）
3. **读清单的顺序**：
   - **JS-discovered routes**——SPA 的 API 地图，模型最容易漏的就是这里的非标准端点；逐个对照后续 exploit skill 的注入面清单
   - **Forms（含 hidden 字段）**——隐藏域是 mass assignment 与越权测试的入口（`exploit-authz`）
   - **AUTH 标记的页面**——login-walled 端点用 `use_identity` + `cookies_import` 带认证会话再测；匿名可达面优先测未授权访问
   - **Cross-host leads**——crawl 不跟随跨主机链接，但记下了 host 清单：确认在授权范围内后可扩大 scope 或交给 `recon-subdomain`/`recon-dir-scan`
4. **分流**：爬完把端点清单交给对应的 exploit skill（/api/* → `api-testing`；表单反射 → `exploit-xss`；下载/文件参数 → `exploit-file-download`）；大站可 `spawn_subagent` 并行处理不同端点组
5. **记录**：爬到的**暴露面**（无需认证的管理端点、泄露的调试接口）验证后 `record_finding`；普通端点清单留在对话里即可，不要把每个 URL 都记成 finding

---

## Methodology

### 何时用 crawl vs 其他侦察
| 需求 | 工具 |
|------|------|
| 端点/表单/API 路由清单 | `crawl`（本 skill） |
| 隐藏目录/备份文件 | `recon-dir-scan`（词表爆破，互补不替代） |
| 技术栈/版本 | `recon-fingerprint` |
| 子域 | `recon-subdomain` |

标准顺序：`crawl`（拿地图）→ `recon-fingerprint`（定技术栈）→ `recon-dir-scan`（补爆破面）→ exploit skills。

### crawl 结果的局限
- 只跟随同 host 链接；跨 host 记为线索不访问
- JS 路由提取是正则启发式：能拿到大部分字面量端点，拿不到运行时拼接的动态路由——对可疑前端代码配合 `browser evaluate` 手工追
- 登录墙后的页面内容拿不到（只标注 AUTH）；需要认证后的攻击面，先带身份再对关键路径逐个测

---

## Testing Checklist

- [ ] 入口页 crawl（默认参数）拿到基线清单
- [ ] JS routes 逐条分派到 exploit skills
- [ ] Forms 的 hidden 字段单独过一遍（tamper/mass assignment）
- [ ] AUTH 页面带认证身份复测
- [ ] cross-host leads 核对授权范围
- [ ] 大站按端点组分派 subagent 并行
- [ ] 暴露面验证后 record_finding

---

### Recording Results

Recon observations are working data, not findings — summarize them in the conversation. Reserve `record_finding` for actual exposures the crawl reveals (an unauthenticated admin endpoint, a leaked debug interface, a sensitive file in a linked path): one finding per concrete, evidence-backed conclusion, `confidence` set honestly, and `evidence` pointing at the proving tool call (`{ "type": "tool", "toolCallId": "<id>", "toolName": "crawl" }`). Findings persist with the session; there is no separate results database to write to.

---

## 相关 Skills

`api-testing`（API 面深入）、`exploit-authz`（越权/隐藏域）、`recon-dir-scan`（目录爆破互补）、`recon-fingerprint`（技术栈）、`results-storage`（findings 机制）
