---
name: recon-dir-scan
description: Directory and file enumeration using ffuf and gobuster. Use this skill when user needs to discover hidden directories, enumerate files, find backup files, or map application structure through path fuzzing.
---

# Directory and File Enumeration

目录扫描请求量大：控制扫描速率与并发，避免对目标造成压力。

## RiftX Workflow

1. **扫描放 bash 后台**：目录扫描耗时且噪音大——`nohup ffuf ... -o work/dir-scan.json > work/dir-scan.log 2>&1 &` 后台跑（按下面 Methodology 出词表与过滤参数），主会话继续手工探索；结果落盘 work/（跨 attempt 持久），回头读日志汇总进对话。单次 attempt 限时 30 分钟——长扫描务必落盘，下个 attempt 接着用
2. **发现物用浏览器定性**：扫出的路径（尤其 `/admin`、登录墙、403 页）用 `browser navigate` + `snapshot` 确认真实形态——403 在浏览器里可能是可绕过的路径归一化问题，登录墙本身是攻击面
3. **敏感暴露即 checkpoint**：扫到 `.git/`、`.env`、`*.bak` 等直接用 `browser response_body`/bash 取内容验证后 `benchmark_control` checkpoint（signalKind: new_surface；evidenceRef 指向取回内容落盘的 work/ 产物）
4. 扫描结果落盘 work/ 并在对话中汇总要点（见 Recording Results），不要写入 skill 目录等任何外部存储

---

## Core Command

```bash
# 基础扫描 + 积极过滤（默认起点）
ffuf -w common.txt -u https://target.com/FUZZ -mc 200,204,301,302,307,401,403 -ac

# 递归（深度 2 起步）；扩展名组合；认证/会话
ffuf -w common.txt -u https://target.com/FUZZ -recursion -recursion-depth 2
ffuf -w words.txt:FUZZ -w exts.txt:EXT -u https://target.com/FUZZ.EXT -mc 200
ffuf -w common.txt -u https://target.com/FUZZ -H "Cookie: session=<token>"
```

结果解析：`scripts/ffuf_results_parser.py`；词表合并：`scripts/merge_wordlists.py`；状态码分布分析：`scripts/status_code_analyzer.py`。

---

## Fuzzing Targets（按价值排序）

| 目标 | FUZZ 位置 | 说明 |
|------|----------|------|
| 隐藏目录/文件 | `/FUZZ` | 词表见下表；同尺寸响应多为误报（`-fs` 过滤） |
| 备份/配置 | `/FUZZ.bak,.old,.tmp,.swp`、`.git`、`.env*` | 直接信息泄露，命中即 finding |
| API 端点 | `/api/v1/FUZZ`、`/rest/FUZZ`、`/graphql` | 配合方法枚举 |
| 虚拟主机 | `Host: FUZZ.target.com` 头 | 内网 IP 直连场景 |
| 隐藏参数 | `/page?FUZZ=test` | 值模糊：`?param=FUZZ` |
| 扩展名组合 | `FUZZ.EXT` | PHP 站优先 `.php,.bak,.old` |

**状态码语义**：200 有效页面；301/302 重定向；401 需认证（存在）；403 禁止（存在，尝试绕过：路径大小写、`%2e`、尾随 `/.`、`;//`）。

---

## Wordlists

| 词表 | 规模 | 场景 |
|------|------|------|
| SecLists `common.txt` | ~4,600 | 默认起点，快 |
| SecLists `raft-medium-directories` | ~30,000 | 第二轮 |
| DirBuster `directory-list-2.3-medium` | ~220,000 | 全面评估（bash 后台跑） |
| 本 skill `assets/` | — | `common-dirs.txt`、`common-files.txt`、`hidden-files.txt`、`api-endpoints.txt` |

镜像离线：内置词表在 `/opt/wordlists/Web-Content/`（`common.txt`、`raft-*`），优先用这些路径。

选型与自建词表：`references/wordlist_guide.md`；ffuf/gobuster 完整用法：`references/ffuf_guide.md`、`references/gobuster_guide.md`。

---

## Tips

1. 小词表先行拿快赢，再逐级加大
2. 积极过滤（`-mc` / `-fs` / `-ac`）降噪；同尺寸响应≈误报
3. 限速（`-rate`）防封禁与告警
4. 有趣结果一律浏览器手工复核
5. 403 不是终点——是绕过测试的起点

---

### Recording Results

Recon observations are working data — persist scan output to work/ files (cwd is the challenge's work/, kept across attempts) and summarize the highlights in the conversation. Reserve `benchmark_control` checkpoint (signalKind=new_surface or note) for actual exposures the scan reveals (an open admin panel, an exposed database service, a leaked backup file): one checkpoint per concrete, evidence-backed conclusion, signal kept within 2000 chars stating fact + evidence + remaining uncertainty, and evidenceRef pointing at the proving artifact saved under work/. The blackboard persists across attempts and workers; large outputs stay on disk and only their path goes into the checkpoint. A confirmed flag is submitted immediately via benchmark_control submit (uniqueCode + flag only) — no evidence prerequisite.

---

## Resources

- **Scripts**：`scripts/ffuf_results_parser.py`、`scripts/merge_wordlists.py`、`scripts/status_code_analyzer.py`
- **References**：`references/ffuf_guide.md`、`references/gobuster_guide.md`、`references/wordlist_guide.md`
- **Assets**：`assets/common-dirs.txt`、`assets/common-files.txt`、`assets/hidden-files.txt`、`assets/api-endpoints.txt`
