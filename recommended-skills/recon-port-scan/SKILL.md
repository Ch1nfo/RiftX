---
name: recon-port-scan
description: Port scanning and service identification using nmap. Use this skill when user needs to discover open ports, identify running services, detect service versions, or fingerprint operating systems on target hosts.
---

# Port Scanning / Reconnaissance

## RiftX Workflow

1. **扫描放后台**：全端口扫描动辄数分钟——bash 后台跑 nmap（`nohup nmap ... > scan.log 2>&1 &`，按下述顺序），期间主会话继续 Web 侧侦察，完成后解析汇总；主机多时按 /24 分块依次后台跑。子代理（`assign_benchmark_challenge`，仅主 Agent 可派发）派发的是**另一道题**，不要用来扫当前题的目标
2. **发现的 Web 服务立刻接浏览器**：扫出的 80/443/8080/8443 端口用 `browser navigate` 打开确认形态（管理后台、API 文档、默认页）；非 Web 服务（数据库、Redis、SMB）转 bash 深入
3. **危险暴露即 checkpoint**：未授权的数据库/Redis/管理端口（3306/5432/6379/3389 对外、Redis 无认证等）验证后 `benchmark_control(action="checkpoint")` 写黑板（signalKind: `new_surface`；扫描 XML 与验证输出落盘 work/ 后 evidenceRef 指向文件）
4. 输出用 `-oX`（XML）+ `scripts/parse_nmap_xml.py` 转结构化结果，便于 subagent 结果汇总

---

## Scan Progression（从便宜到贵）

```bash
# 1. 快速侦察：top 100 端口
nmap -T4 -F <target>

# 2. 全端口（subagent 跑，XML 输出便于解析）
nmap -p- -T4 --min-rate 2000 -oX full.xml <target>

# 3. 对开放端口做服务/版本/默认脚本
nmap -sV -sC -p 80,443,3306 -oX svc.xml <target>

# 4. UDP top 100（内网/域环境值得）
nmap -sU --top-ports 100 <target>

# 5. OS 指纹（需 root）
sudo nmap -O <target>
```

**大网段**：`nmap -T5 --min-rate=2000 -p- <CIDR>` 找开放端口 → nmap `-sV` 补细节。
**隐蔽需求**：`-sS -T2 -f --data-length 24`、诱骗 `-D RND:10`；详见 `references/scanning_techniques.md`。

**节奏**：-T4 起步；对方有 IDS/限速要求时降到 -T2；-T5 可能漏报不推荐。

---

## Follow-up Selection

| 发现 | 下一步 |
|------|--------|
| Web 端口（80/443/8080/8443） | `browser navigate` 确认形态 → 转 recon-fingerprint / recon-dir-scan |
| 数据库（3306/5432/27017） | bash 验证未授权访问/弱口令（`scripts` + security-passwords 词表） |
| Redis（6379） | 未授权访问检查（`redis-cli -h` info/config get dir） |
| SMB/RPC（445/135） | `nmap --script smb-enum-*`、`smbclient -L //<ip> -N`、`rpcclient -U "" //<ip>`、`ldapsearch` |
| SSH/FTP/Telnet | 默认凭据/弱口令（security-usernames + security-passwords） |
| RDP/VNC | 弱口令 + 暴露面记录 |

NSE 分类速查：`--script=vuln`（漏洞）、`auth`（认证绕过）、`brute`（爆破）、`discovery,info`（信息收集）。

---

## Testing Checklist

- [ ] 范围确认（IP 段、速率上限、隐蔽性要求）
- [ ] top100 → 全端口 → 服务版本 → UDP → OS 的顺序
- [ ] 每个开放端口的 follow-up 落实（上表）
- [ ] 危险暴露（DB/Redis/管理端口对外）→ 验证 → `benchmark_control` checkpoint 写黑板
- [ ] 结果在对话中结构化汇总（主机 × 端口 × 服务 × 版本）

---

### Recording Results

Recon observations are working data — summarize them in the conversation and in `notes.md` under the challenge `work/` directory. Confirmed exposures the scan reveals (an open admin panel, an exposed database service, an unauthenticated Redis) go to the shared blackboard via `benchmark_control(action="checkpoint")`: `signal` states the factual observation, the evidence, and remaining uncertainty (≤2000 chars), `signalKind` = `new_surface` (informational leads use `note`), and `evidenceRef` points at a stable artifact saved under `work/` (e.g. `work/loot/nmap-full.xml`) or the proving request. The blackboard persists across attempts and workers; dump large outputs to `work/` and reference the path instead of pasting bodies into the signal.

---

## Resources

- **Scripts**：`scripts/parse_nmap_xml.py`（XML→结构化）、`scripts/merge_scan_results.py`（多结果合并）
- **References**：`references/nmap_cheatsheet.md`、`references/scanning_techniques.md`（进阶与规避）
- **Assets**：`assets/top-100-ports.txt`、`assets/top-1000-ports.txt`、`assets/common-services.txt`
