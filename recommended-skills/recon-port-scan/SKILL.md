---
name: recon-port-scan
description: Port scanning and service identification using nmap, masscan, and rustscan. Use this skill when user needs to discover open ports, identify running services, detect service versions, or fingerprint operating systems on target hosts.
---

# Port Scanning / Reconnaissance

## Authorization Warning

端口扫描在无授权时违法：书面授权、明确 IP 段范围后再开始；速率与隐蔽性要求以授权约定为准。

---

## RiftX Workflow

1. **扫描全部交给 subagent**：全端口扫描动辄数分钟——`spawn_subagent` 按下述顺序跑，主机多时按 /24 分块并行派发；主会话继续 Web 侧侦察，结果回来汇总进对话
2. **发现的 Web 服务立刻接浏览器**：扫出的 80/443/8080/8443 端口用 `browser navigate` 打开确认形态（管理后台、API 文档、默认页）；非 Web 服务（数据库、Redis、SMB）转 bash 深入
3. **危险暴露即 finding**：未授权的数据库/Redis/管理端口（3306/5432/6379/3389 对外、Redis 无认证等）验证后 `record_finding`，evidence 引用扫描与验证的工具调用
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

**大网段**：`masscan -p1-65535 <CIDR> --rate=10000 -oL -` 找开放端口 → `scripts/masscan_to_nmap.py` 转换 → nmap `-sV` 补细节。
**快速现代流**：`rustscan -a <target> -- -sV -sC`。
**隐蔽需求**（授权约定时）：`-sS -T2 -f --data-length 24`、诱骗 `-D RND:10`；详见 `references/scanning_techniques.md`。

**节奏**：-T4 起步；对方有 IDS/限速要求时降到 -T2；-T5 可能漏报不推荐。

---

## Follow-up Selection

| 发现 | 下一步 |
|------|--------|
| Web 端口（80/443/8080/8443） | `browser navigate` 确认形态 → 转 recon-fingerprint / recon-dir-scan |
| 数据库（3306/5432/27017） | bash 验证未授权访问/弱口令（`scripts` + security-passwords 词表） |
| Redis（6379） | 未授权访问检查（`redis-cli -h` info/config get dir） |
| SMB/RPC（445/135） | `nmap --script=vuln`、enum4linux |
| SSH/FTP/Telnet | 默认凭据/弱口令（security-usernames + security-passwords） |
| RDP/VNC | 弱口令 + 暴露面记录 |

NSE 分类速查：`--script=vuln`（漏洞）、`auth`（认证绕过）、`brute`（爆破）、`discovery,info`（信息收集）。

---

## Testing Checklist

- [ ] 范围确认（IP 段、速率上限、隐蔽性要求）
- [ ] top100 → 全端口 → 服务版本 → UDP → OS 的顺序
- [ ] 每个开放端口的 follow-up 落实（上表）
- [ ] 危险暴露（DB/Redis/管理端口对外）→ 验证 → `record_finding`
- [ ] 结果在对话中结构化汇总（主机 × 端口 × 服务 × 版本）

---

### Recording Results

Recon observations are working data, not findings — summarize them in the conversation. Reserve `record_finding` for actual exposures the scan reveals (an open admin panel, an exposed database service, a leaked backup file): one finding per concrete, evidence-backed conclusion, `confidence` set honestly, and `evidence` pointing at the proving tool call (`{ "type": "tool", "toolCallId": "<id>", "toolName": "bash" }`). Findings persist with the session; there is no separate results database to write to.

---

## Resources

- **Scripts**：`scripts/parse_nmap_xml.py`（XML→结构化）、`scripts/masscan_to_nmap.py`（结果转换）、`scripts/merge_scan_results.py`（多结果合并）
- **References**：`references/nmap_cheatsheet.md`、`references/masscan_guide.md`、`references/rustscan_guide.md`、`references/scanning_techniques.md`（进阶与规避）
- **Assets**：`assets/top-100-ports.txt`、`assets/top-1000-ports.txt`、`assets/common-services.txt`
