---
name: recon-subdomain
description: Subdomain enumeration and DNS reconnaissance using subfinder, amass, dnsx, and other tools. Use this skill when user needs to discover subdomains, perform DNS enumeration, gather DNS records, or find hidden subdomains of a target domain.
---

# Subdomain Enumeration / DNS Reconnaissance

主动枚举/爆破控制速率与并发。

## RiftX Workflow

1. **枚举流水线交 subagent**：被动枚举+解析+存活探测是分钟级任务——`spawn_subagent` 跑下面 Workflow，主会话继续已发现资产的手工侦察
2. **存活资产接浏览器**：httpx 出来的存活子域逐个 `browser navigate` + `snapshot` 分类（管理后台 / API / 旧系统 / 默认页面）——旧系统和 forgotten 后台是最肥的攻击面
3. **注意 scope**：浏览器导航受 scope 规则约束——发现的新子域若不在当前 scope，navigate 会走 scope 审批流程，属预期行为
4. **危险发现即 finding**：subdomain takeover 特征（CNAME 指向已释放的云资源）、可 zone transfer 的 DNS——验证后 `record_finding`

---

## Workflow

```bash
# 1. 被动枚举（多源合并）
subfinder -d target.com -silent > passive.txt
assetfinder --subs-only target.com >> passive.txt
amass enum -passive -d target.com >> passive.txt     # 全面但慢，可选
curl -s "https://crt.sh/?q=%.target.com&output=json" | jq -r '.[].name_value' >> passive.txt
sort -u passive.txt -o passive.txt

# 2. 解析验证（注意先做 wildcard 检测）
echo "randomtest12345.target.com" | dnsx -silent      # 有解析 = 存在 wildcard
dnsx -l passive.txt -silent -resp -a -cname > resolved.txt

# 3. HTTP 存活
cat resolved.txt | httpx -silent -status-code -title -tech-detect > alive.txt

# 4. 可选：主动爆破补漏（subagent 跑）
puredns bruteforce assets/subdomains-top5k.txt target.com -r assets/resolvers.txt >> resolved.txt
sort -u resolved.txt
```

合并去重：`scripts/merge_subdomains.py`；解析过滤：`scripts/filter_resolved.py`；统计：`scripts/subdomain_stats.py`。

---

## Special Checks

| 检查 | 命令 | 价值 |
|------|------|------|
| Zone transfer | `dig axfr @ns1.target.com target.com` | 极少成功但一次命中即全量泄露 |
| DNS 记录枚举 | `dnsx -l subs.txt -a -cname -txt -mx -ns` | TXT 泄露内部信息/验证 token；CNAME 指向云资源 |
| Takeover | `nuclei -l resolved.txt -t takeover-templates/` 或 subjack | CNAME → 已释放的 Heroku/Azure/S3 等 |
| 通配符 | `puredns discard wildcards.txt < subs.txt` | 否则爆破结果全是误报 |

进阶技术：`references/dns_techniques.md`。

---

## Tips

1. 先被动后主动——被动不触发目标告警
2. 多工具合并，没有单一工具能找全
3. 爆破前必做 wildcard 检测
4. 发现 ≠ 解析 ≠ 存活，逐层过滤
5. amass 最全但慢——只在值得时上，其余时候 subfinder+assetfinder+crt.sh 够用

---

### Recording Results

Recon observations are working data, not findings — summarize them in the conversation. Reserve `record_finding` for actual exposures the scan reveals (an open admin panel, an exposed database service, a leaked backup file): one finding per concrete, evidence-backed conclusion, `confidence` set honestly, and `evidence` pointing at the proving tool call (`{ "type": "tool", "toolCallId": "<id>", "toolName": "bash" }`). Findings persist with the session; there is no separate results database to write to.

---

## Resources

- **Scripts**：`scripts/merge_subdomains.py`、`scripts/filter_resolved.py`、`scripts/subdomain_stats.py`
- **References**：`references/subfinder_guide.md`、`references/amass_guide.md`、`references/dnsx_guide.md`、`references/dns_techniques.md`
- **Assets**：`assets/subdomains-top5k.txt`、`assets/resolvers.txt`、`assets/wildcard-test.txt`
