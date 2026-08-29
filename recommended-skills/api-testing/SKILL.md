---
name: api-testing
description: API security testing 接口安全测试：REST/GraphQL 面发现、认证与 JWT 操纵、mass assignment、动词篡改、限流、GraphQL introspection。Use this skill when user mentions API 测试, 接口测试, REST, GraphQL, swagger, JWT, or needs to test backend API endpoints and tokens.
---

# API 安全测试

## RiftX Workflow

1. **API 面发现靠浏览器的网络记录**：以正常用户在浏览器里走完全部功能，`requests` 自动记录所有 XHR/fetch——这是最真实的 API 清单（比 swagger 文档更接近实际部署）；`request_detail` 逐个看真实结构（方法、路径、头、体、token 形态）
2. **文档端点用浏览器确认**：`/swagger` `/openapi.json` `/api-docs` `/graphql` `/graphiql`——命中即拿到全量接口定义（本身若未鉴权也是一个 finding，`confidence: likely`）
3. **重放与篡改在 bash**：`cookies_export` 导出会话 → curl 重放修改（换方法/换路径/加字段/改 token）；GraphQL 在浏览器 `evaluate` 里发 query 最方便（页面上下文自带 token 与同源策略）
4. **并行**：参数/端点 fuzz 交给 `spawn_subagent`，主会话手工做逻辑类测试（越权、mass assignment——交叉 `exploit-authz`）

---

## Methodology

### 1. 面发现

| 来源 | 位置 |
|------|------|
| 文档 | `/swagger` `/swagger.json` `/openapi.json` `/api-docs` `/v1|v2|v3/` `/graphql` |
| 被动 | 浏览器 `requests` 记录、前端 JS bundle 里的硬编码端点 |
| 主动 | `recon-dir-scan` 的 `api-endpoints.txt`；ffuf 参数枚举 |

### 2. 认证与会话

- **JWT**：解码看 alg/claims → `alg: none` 与弱密钥（`jwt-tool`/hashcat 离线爆破）、claim 篡改（sub/role/exp）重放
- **令牌泄露**：token 在 URL（进日志/Referer）、前端 JS 硬编码的 API key、localStorage 里的长期 token（`storage` action 直接看）
- **令牌作用域**：用户 token 访问管理 API、登出后 token 仍有效、刷新令牌无轮换

### 3. 逻辑类

- **Mass assignment**：资料/注册接口追加 `{"role":"admin"}` `{"isVip":true}` 类字段
- **动词篡改**：`GET`→`POST`/`PUT`/`DELETE`/`PATCH`；`GET /api/users` 改 `GET /api/users/admin`
- **BOLA/越权**：对象 ID 替换重放（详见 `exploit-authz`——API 越权的高发区）
- **错误信息**：栈迹/SQL 片段/内部字段名泄露

### 4. GraphQL 专项

- Introspection 开放 → 全 schema 导出（`evaluate` 一段 query 即可）
- 字段建议错误信息枚举（typo 提示逐字母还原隐藏字段/mutation）
- Mutation 的对象 ID 越权（与 REST BOLA 同理）
- 查询深度/批量（batching）滥用——仅验证存在性，不施压

### 5. 限流与滥用

- 登录/验证码/敏感操作端点连续请求观察限流（无限流=暴力破解面，记录 finding）
- 批量枚举控制速率与并发，不发起拒绝服务级别的压力

---

## Testing Checklist

- [ ] 浏览器全功能走查，`requests` 收集 API 面
- [ ] 文档端点探测（swagger/openapi/graphql）
- [ ] JWT 解码与篡改重放、token 泄露位置
- [ ] Mass assignment、动词篡改
- [ ] 对象级越权（交叉 exploit-authz 打法）
- [ ] GraphQL：introspection/字段建议/mutation 越权
- [ ] 限流验证
- [ ] 每个确认点：`request_detail` 证据 + `record_finding`

---

## 相关 Skills

`exploit-authz`（越权/BOLA 深入打法）、`recon-dir-scan`（端点发现）、`exploit-sqli`（注入类 API 参数）、`results-storage`（findings 持久化机制）
