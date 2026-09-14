---
name: recon-subdomain
description: Subdomain enumeration and DNS reconnaissance using dig, bash resolution loops, and ffuf vhost fuzzing with local wordlists. Use this skill when user needs to discover subdomains, perform DNS enumeration, gather DNS records, or find hidden subdomains of a target domain.
---

# Subdomain Enumeration / DNS Reconnaissance

主动枚举/爆破控制速率与并发。

## RiftX Workflow

1. **枚举流水线放后台**：词表解析+存活探测是分钟级任务——bash 后台跑下面 Workflow（`nohup bash enum.sh > enum.log 2>&1 &`），期间主会话继续已发现资产的手工侦察，完成后 grep 汇总。子代理（`assign_benchmark_challenge`，仅主 Agent 可派发）派发的是**另一道题**，不要用来枚举当前题的域名
2. **存活资产接浏览器**：解析成功且端口可达的子域逐个 `browser navigate` + `snapshot` 分类（管理后台 / API / 旧系统 / 默认页面）——旧系统和 forgotten 后台是最肥的攻击面
3. **注意 scope**：浏览器导航受 scope 规则约束——发现的新子域若不在当前 scope，navigate 会走 scope 审批流程，属预期行为
4. **危险发现即 checkpoint**：subdomain takeover 特征（CNAME 指向已释放的云资源）、可 zone transfer 的 DNS——验证后 `benchmark_control(action="checkpoint")` 写黑板（signalKind: `new_surface`，evidenceRef 指向 work/ 落盘的解析与验证输出）

---

## Workflow

```bash
# 0. 离线环境：无公网、无被动情报源（证书透明度日志等均不可达），专用子域工具也未安装
#    ——枚举全靠本地 dig + 词表解析 + ffuf vhost 枚举三路合并

# 1. Wildcard 检测（随机子域有解析 = 存在 wildcard，爆破结果全是误报）
dig +short randomtest12345.target.com

# 2. AXFR 尝试（对目标每个 NS；极少成功但一次命中即全量泄露）
for ns in $(dig +short NS target.com); do dig axfr @"$ns" target.com; done

# 3. 本地词表爆破解析（bash 循环，向 challenge-dns 查询；控制速率）
while read -r s; do
  a=$(dig +short "$s.target.com" @challenge-dns 2>/dev/null | head -1)
  [ -n "$a" ] && echo "$s.target.com $a"
done < /opt/wordlists/DNS/subdomains-top1million-5000.txt | tee resolved.txt

# 4. HTTP 存活（curl 批量探测）
while read -r h _; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$h")
  echo "$h $code"
done < resolved.txt > alive.txt

# 5. vhost 枚举（DNS 不通时用 Host 头；先取基线大小再用 -fs 过滤）
curl -s http://<target-ip> -H "Host: baseline.target.com" | wc -c   # 基线
ffuf -w /opt/wordlists/DNS/subdomains-top1million-5000.txt -u http://<target-ip> \
  -H "Host: FUZZ.target.com" -fs <基线大小> -mc 200,301,302,401,403
```

合并去重：`scripts/merge_subdomains.py`；解析过滤：`scripts/filter_resolved.py`；统计：`scripts/subdomain_stats.py`。

---

## Special Checks

| 检查 | 命令 | 价值 |
|------|------|------|
| Zone transfer | `dig axfr @ns1.target.com target.com` | 极少成功但一次命中即全量泄露 |
| DNS 记录枚举 | `dig +short CNAME/TXT/MX/NS <sub>`（bash 循环逐条） | TXT 泄露内部信息/验证 token；CNAME 指向云资源 |
| Takeover | 手工：`dig +short CNAME <sub>`，对照云厂商别名特征（已释放的 Heroku/Azure/S3/CloudFront 等，特征表见 `references/subdomain-takeover.md`） | CNAME → 悬空云资源即接管面 |
| 通配符 | `dig +short random12345.target.com` 有解析即 wildcard | 否则爆破结果全是误报 |

进阶技术：`references/dns_techniques.md`。

---

## Tips

1. 本环境离线——无被动情报源，先便宜的 AXFR/词表解析，再 ffuf vhost 枚举
2. DNS 解析与 vhost 枚举两路合并，没有单一途径能找全
3. 爆破前必做 wildcard 检测
4. 发现 ≠ 解析 ≠ 存活，逐层过滤
5. DNS 解析不通 ≠ 子域不存在——vhost（Host 头）枚举补盲区

---

### Recording Results

Recon observations are working data — summarize them in the conversation and in `notes.md` under the challenge `work/` directory. Confirmed exposures the enumeration reveals (a dangling CNAME ready for takeover, a zone transfer that succeeds, a forgotten admin panel on an old subdomain) go to the shared blackboard via `benchmark_control(action="checkpoint")`: `signal` states the factual observation, the evidence, and remaining uncertainty (≤2000 chars), `signalKind` = `new_surface` (informational leads use `note`), and `evidenceRef` points at a stable artifact saved under `work/` (e.g. `work/loot/subdomains-resolved.txt`) or the proving query. The blackboard persists across attempts and workers; dump large outputs to `work/` and reference the path instead of pasting bodies into the signal.

---

## Resources

- **Scripts**：`scripts/merge_subdomains.py`、`scripts/filter_resolved.py`、`scripts/subdomain_stats.py`
- **References**：`references/dns_techniques.md`、`references/subdomain-takeover.md`（接管特征与验证手法）
- **Assets**：`assets/subdomains-top5k.txt`、`assets/resolvers.txt`、`assets/wildcard-test.txt`
