---
name: recon-fingerprint
description: Web fingerprinting and WAF detection using curl, nmap, and openssl. Use this skill when user needs to identify web technologies, detect WAF/CDN, analyze server headers, or fingerprint web applications and frameworks.
---

# Web Fingerprinting & WAF Detection

## RiftX Workflow

1. **浏览器是 SPA 指纹的最强工具**：`browser navigate` + `snapshot` 直接看到渲染后的 DOM——script bundle 路径、meta generator、`ng-app`/`__NEXT_DATA__`/`wp-json` 等特征全在快照里；`requests` 列出全部已加载资源（JS/CSS/字体），bundle 命名即技术栈
2. **头部证据**：`request_detail` 按 ref 取任意请求的完整响应头——比 curl 单次请求更全面（含 XHR/API 端点的头部）
3. **WAF 先行**：主动扫描/爆破前必须先判 WAF（`curl -i` 响应头签名 + 恶意探测请求的行为差异），否则后续 exploit skill 会白白撞墙
4. **版本 → 过期组件 checkpoint**：确认具体版本后，对照已知 CVE 得出「使用了含已知漏洞版本」的结论——`benchmark_control(action="checkpoint")` 写黑板（signalKind: `new_surface`，signal 注明不确定性；指纹输出落盘 work/ 后 evidenceRef 指向文件）；实际可利用性需复现后另行 checkpoint

---

## Toolbelt

| 场景 | 命令 |
|------|------|
| 快速技术栈 | `curl -i https://target.com`（Server/X-Powered-By/Cookie 特征）+ 浏览器快照 |
| WAF/CDN | `curl -i` 对照 `assets/waf-signatures.txt`；发恶意探测请求看拦截页/行为差异 |
| 全面技术识别 | 头部 + 快照 + `nmap -sV -sC` 多源交叉（可派子代理跑） |
| 批量探测 | `while read -r u; do curl -sI "$u" | grep -iE 'server|x-powered-by|via'; done < urls.txt` |
| 服务版本 | `nmap -sV -sC -p 80,443 target` |
| SSL/TLS | `openssl s_client -connect target:443 </dev/null`（证书链/协议/套件）、`nmap --script ssl-cert,ssl-enum-ciphers -p 443 target` |

---

## Fingerprint 速查

### 后端/服务器特征

| 技术 | 特征 |
|------|------|
| nginx / Apache / IIS | `Server:` 头 + 版本号 |
| PHP / Express(JWT) / JSP / .NET | `X-Powered-By`、`.php` / `.aspx` / `.do` 后缀 |
| CMS | WordPress `/wp-login.php` `wp-json`、Drupal `Drupal.settings`、Joomla `/administrator/components` |

### 前端框架（浏览器快照里找）

React（`__NEXT_DATA__`/react-dom bundle）、Vue（`v-if`/vue bundle）、Angular（`ng-app`/zone.js）、jQuery。

### WAF/CDN 签名

| 产品 | 特征头/Cookie |
|------|--------------|
| Cloudflare | `cf-ray`、`cf-cache-status` |
| AWS CloudFront/WAF | `x-amz-cf-id`、`via: CloudFront` |
| Imperva | `X-Iinfo`、`X-CDN` |
| Akamai | `akamai-origin` |
| F5 ASM | `BIGipServer` cookie |
| ModSecurity | `Mod_Security` 头 |

完整签名：`assets/waf-signatures.txt`、`assets/tech-headers.txt`、`assets/cms-fingerprints.txt`。

---

## Tips

1. 先被动（头部/快照）后主动（curl 探测/nmap -sV）
2. 多源交叉验证——单一来源的版本判定常有误
3. CDN 会挡住真实服务器信息——`request_detail` 看 `via`/`x-served-by` 判 CDN 链路
4. 指纹结果在对话中汇总（host → 技术 → 版本 → 置信度），过期组件走 checkpoint

---

### Recording Results

Recon observations are working data — summarize them in the conversation and in `notes.md` under the challenge `work/` directory. Confirmed exposures the fingerprinting reveals (a component running a known-vulnerable version, an exposed admin panel, a TLS stack with deprecated ciphers) go to the shared blackboard via `benchmark_control(action="checkpoint")`: `signal` states the factual observation, the evidence, and remaining uncertainty (≤2000 chars), `signalKind` = `new_surface` (informational leads use `note`), and `evidenceRef` points at a stable artifact saved under `work/` (e.g. `work/loot/fingerprint.md`) or the proving request. The blackboard persists across attempts and workers; dump large outputs to `work/` and reference the path instead of pasting bodies into the signal.

---

## Resources

- **Scripts**：`scripts/extract_headers.py`（头部分析）、`scripts/tech_matcher.py`（技术匹配）、`scripts/waf_detector.py`（WAF 判定）
- **References**：`references/fingerprinting_techniques.md`（进阶方法）
- **Assets**：`assets/waf-signatures.txt`、`assets/tech-headers.txt`、`assets/cms-fingerprints.txt`
