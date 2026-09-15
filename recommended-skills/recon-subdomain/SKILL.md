---
name: recon-subdomain
description: Subdomain enumeration and DNS reconnaissance using dig, bash resolution loops, and ffuf Host-header vhost fuzzing. Use this skill when user needs to discover subdomains, perform DNS enumeration, gather DNS records, or find hidden subdomains of a target domain.
---

# Subdomain Enumeration / DNS Reconnaissance

主动枚举/爆破控制速率与并发。

## RiftX Workflow

1. **枚举流水线放 bash 后台**：字典爆破 + 解析 + 存活探测是分钟级任务——把下面 Workflow 存成脚本 `nohup bash dns-enum.sh > work/dns-enum.log 2>&1 &` 后台跑（本题内并行方式；跨题并行只有主 Agent 能用 `assign_benchmark_challenge`，派的是另一道题），主会话继续已发现资产的手工侦察。离线镜像无公网——没有证书透明度/DNS 聚合等被动枚举源，只能主动枚举；单次 attempt 限时 30 分钟，结果落盘 work/（跨 attempt 持久）续用
2. **存活资产接浏览器**：dig 解析出的子域先 curl 探活，存活者逐个 `browser navigate` + `snapshot` 分类（管理后台 / API / 旧系统 / 默认页面）——旧系统和 forgotten 后台是最肥的攻击面
3. **注意 scope**：浏览器导航受 scope 规则约束——发现的新子域若不在当前 scope，navigate 会走 scope 审批流程，属预期行为
4. **危险发现即 checkpoint**：subdomain takeover 特征（CNAME 指向已释放的云资源）、可 zone transfer 的 DNS——验证后 `benchmark_control` checkpoint（signalKind: new_surface；evidenceRef 指向 work/ 落盘的解析/验证输出）

---

## Workflow

```bash
# 0. 离线约束：镜像无公网，被动枚举源（证书透明度日志、DNS 聚合库）全部不可用——只能主动

# 1. Wildcard 检测（必做，否则爆破结果全是误报）
dig +short randomtest12345.target.com        # 有解析 = 存在 wildcard，记下其 IP 用于过滤

# 2. 字典爆破 + 解析（bash 循环；镜像词表 /opt/wordlists/DNS/subdomains-top1million-5000.txt，后台跑）
while read -r sub; do
  ans=$(dig +short "$sub.target.com" 2>/dev/null | grep -v '^;' | head -1)
  [ -n "$ans" ] && echo "$sub.target.com $ans"
done < /opt/wordlists/DNS/subdomains-top1million-5000.txt > work/subdomains-resolved.txt

# 3. 记录类型枚举（TXT 泄露内部信息/验证 token；CNAME 指向云资源 = 接管候选）
dig target.com TXT MX NS +noall +answer
while read -r h _; do dig "$h" CNAME +short; done < work/subdomains-resolved.txt | sort -u

# 4. Zone transfer（极少成功，但一次命中即全量泄露）
dig axfr @ns1.target.com target.com

# 5. HTTP 存活（curl 逐个探）
while read -r h _; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$h")
  [ "$code" != "000" ] && echo "$h $code"
done < work/subdomains-resolved.txt > work/subdomains-alive.txt

# 6. vhost 枚举（ffuf Host 头，基于已解析 IP，补 DNS 解析不到的虚拟主机）
ffuf -w /opt/wordlists/DNS/subdomains-top1million-5000.txt -u http://<ip>/ -H "Host: FUZZ.target.com" -ac
```

接管检测全手工：CNAME 指向已释放的云资源（Heroku/Azure/S3 等）时，对照 `references/subdomain-takeover.md` 的特征表逐条验证，再尝试在对应平台上认领。

合并去重：`scripts/merge_subdomains.py`；解析过滤：`scripts/filter_resolved.py`；统计：`scripts/subdomain_stats.py`。

---

## Special Checks

| 检查 | 命令 | 价值 |
|------|------|------|
| Zone transfer | `dig axfr @ns1.target.com target.com` | 极少成功但一次命中即全量泄露 |
| DNS 记录枚举 | `while read -r h _; do dig "$h" A CNAME TXT MX NS +noall +answer; done < subs.txt` | TXT 泄露内部信息/验证 token；CNAME 指向云资源 |
| Takeover（手工） | `dig +short CNAME <sub>` 后对照 `references/subdomain-takeover.md` 特征表验证认领 | CNAME → 已释放的 Heroku/Azure/S3 等 |
| 通配符 | `dig +short random<TOKEN>.target.com`，记录 wildcard IP 并从爆破结果中过滤 | 否则爆破结果全是误报 |

进阶技术：`references/dns_techniques.md`。

---

## Tips

1. 离线无公网——没有被动枚举源，全部靠主动：字典爆破 + dig 查询 + ffuf vhost
2. 多来源合并（DNS 爆破、zone transfer、vhost 枚举、页面内链），没有单一来源能找全
3. 爆破前必做 wildcard 检测
4. 发现 ≠ 解析 ≠ 存活，逐层过滤
5. 长爆破放 bash 后台并落盘 work/——单次 attempt 30 分钟未必跑完，下个 attempt 从落盘结果续跑

---

### Recording Results

Recon observations are working data — persist enumeration output to work/ files (cwd is the challenge's work/, kept across attempts) and summarize the highlights in the conversation. Reserve `benchmark_control` checkpoint (signalKind=new_surface or note) for actual exposures the recon reveals (a confirmed subdomain takeover candidate, a zone transfer that succeeded, an exposed admin panel): one checkpoint per concrete, evidence-backed conclusion, signal kept within 2000 chars stating fact + evidence + remaining uncertainty, and evidenceRef pointing at the proving artifact saved under work/. The blackboard persists across attempts and workers; large outputs stay on disk and only their path goes into the checkpoint. A confirmed flag is submitted immediately via benchmark_control submit (uniqueCode + flag only) — no evidence prerequisite.

---

## Resources

- **Scripts**：`scripts/merge_subdomains.py`、`scripts/filter_resolved.py`、`scripts/subdomain_stats.py`
- **References**：`references/dns_techniques.md`、`references/subdomain-takeover.md`（接管特征与验证手法）
- **Assets**：`assets/subdomains-top5k.txt`、`assets/resolvers.txt`、`assets/wildcard-test.txt`
