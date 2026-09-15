---
name: recon-port-scan
description: Port scanning and service identification using nmap. Use this skill when user needs to discover open ports, identify running services, detect service versions, or fingerprint operating systems on target hosts.
---

# Port Scanning / Reconnaissance

## RiftX Workflow

1. **扫描放 bash 后台**：全端口扫描动辄数分钟——`nohup nmap ... -oX full.xml > work/nmap.log 2>&1 &` 按下述顺序后台跑（本题内并行方式；跨题并行只有主 Agent 能用 `assign_benchmark_challenge`，派的是另一道题），主机多时按 /24 分块后台并行；主会话继续 Web 侧侦察，回头读 work/ 日志汇总。单次 attempt 限时 30 分钟——扫描输出落盘 work/（跨 attempt 持久）才能续用
2. **发现的 Web 服务立刻接浏览器**：扫出的 80/443/8080/8443 端口用 `browser navigate` 打开确认形态（管理后台、API 文档、默认页）；非 Web 服务（数据库、Redis、SMB）转 bash 深入
3. **危险暴露即 checkpoint**：未授权的数据库/Redis/管理端口（3306/5432/6379/3389 对外、Redis 无认证等）验证后 `benchmark_control` checkpoint（signalKind: new_surface；evidenceRef 指向 work/ 落盘的扫描 XML 与验证输出）
4. 输出用 `-oX`（XML）+ `scripts/parse_nmap_xml.py` 转结构化结果，便于后台结果汇总

---

## Scan Progression（从便宜到贵）

```bash
# 1. 快速侦察：top 100 端口
nmap -T4 -F <target>

# 2. 全端口（bash 后台跑，XML 输出便于解析）
nmap -p- -T4 --min-rate 2000 -oX full.xml <target>

# 3. 对开放端口做服务/版本/默认脚本
nmap -sV -sC -p 80,443,3306 -oX svc.xml <target>

# 4. UDP top 100（内网/域环境值得）
nmap -sU --top-ports 100 <target>

# 5. OS 指纹（需 root）
sudo nmap -O <target>
```

**大网段**：`nmap -p- -T5 --min-rate 5000 <CIDR> -oL ports.list` 找开放端口 → nmap `-sV` 补细节（后台跑）。
**隐蔽需求**：`-sS -T2 -f --data-length 24`、诱骗 `-D RND:10`；详见 `references/scanning_techniques.md`。

**节奏**：-T4 起步；对方有 IDS/限速要求时降到 -T2；-T5 可能漏报不推荐。

---

## Follow-up Selection

| 发现 | 下一步 |
|------|--------|
| Web 端口（80/443/8080/8443） | `browser navigate` 确认形态 → 转 `recon-fingerprint`（read ~/.riftx/skills/recon-fingerprint/SKILL.md）/ `recon-dir-scan` |
| 数据库（3306/5432/27017） | bash 验证未授权访问/弱口令（`scripts` + security-passwords 词表） |
| Redis（6379） | 未授权访问检查（`redis-cli -h` info/config get dir） |
| SMB/RPC（445/135） | `nmap --script=vuln,smb-enum-*`、`smbclient`/`rpcclient`/`ldapsearch` |
| SSH/FTP/Telnet | 默认凭据/弱口令（security-usernames + security-passwords） |
| RDP/VNC | 弱口令 + 暴露面记录 |

NSE 分类速查：`--script=vuln`（漏洞）、`auth`（认证绕过）、`brute`（爆破）、`discovery,info`（信息收集）。

---

## Testing Checklist

- [ ] 范围确认（IP 段、速率上限、隐蔽性要求）
- [ ] top100 → 全端口 → 服务版本 → UDP → OS 的顺序
- [ ] 每个开放端口的 follow-up 落实（上表）
- [ ] 危险暴露（DB/Redis/管理端口对外）→ 验证 → `benchmark_control` checkpoint
- [ ] 结果在对话中结构化汇总（主机 × 端口 × 服务 × 版本）

---

### Recording Results

Recon observations are working data — persist scan output to work/ files (cwd is the challenge's work/, kept across attempts) and summarize the highlights in the conversation. Reserve `benchmark_control` checkpoint (signalKind=new_surface or note) for actual exposures the scan reveals (an open admin panel, an exposed database service, a leaked backup file): one checkpoint per concrete, evidence-backed conclusion, signal kept within 2000 chars stating fact + evidence + remaining uncertainty, and evidenceRef pointing at the proving artifact saved under work/ (the nmap XML, the verification transcript). The blackboard persists across attempts and workers; large outputs stay on disk and only their path goes into the checkpoint. A confirmed flag is submitted immediately via benchmark_control submit (uniqueCode + flag only) — no evidence prerequisite.

---

## Resources

- **Scripts**：`scripts/parse_nmap_xml.py`（XML→结构化）、`scripts/merge_scan_results.py`（多结果合并）
- **References**：`references/nmap_cheatsheet.md`、`references/scanning_techniques.md`（进阶与规避）
- **Assets**：`assets/top-100-ports.txt`、`assets/top-1000-ports.txt`、`assets/common-services.txt`
