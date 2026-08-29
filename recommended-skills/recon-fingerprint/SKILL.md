---
name: recon-fingerprint
description: Web fingerprinting and WAF detection using wafw00f, whatweb, nuclei, and httpx. Use this skill when user needs to identify web technologies, detect WAF/CDN, analyze server headers, or fingerprint web applications and frameworks.
---

# Web Fingerprinting & WAF Detection

## RiftX Workflow

1. **浏览器是 SPA 指纹的最强工具**：`browser navigate` + `snapshot` 直接看到渲染后的 DOM——script bundle 路径、meta generator、`ng-app`/`__NEXT_DATA__`/`wp-json` 等特征全在快照里；`requests` 列出全部已加载资源（JS/CSS/字体），bundle 命名即技术栈
2. **头部证据**：`request_detail` 按 ref 取任意请求的完整响应头——比 curl 单次请求更全面（含 XHR/API 端点的头部）
3. **WAF 先行**：主动扫描/爆破前必须先判 WAF（wafw00f + 响应头特征），否则后续 exploit skill 会白白撞墙
4. **版本 → 过期组件 finding**：确认具体版本后，对照已知 CVE 得出「使用了含已知漏洞版本」的结论——`record_finding`（confidence: likely，evidence 引用取版本的工具调用）；实际可利用性是另一条 finding，需复现证明

---

## Toolbelt

| 场景 | 命令 |
|------|------|
| 快速技术栈 | `whatweb https://target.com -a 3` |
| WAF/CDN | `wafw00f https://target.com`、`httpx -u URL -cdn` |
| 全面技术识别 | `nuclei -u https://target.com -tags tech,cms -severity info`（`spawn_subagent` 跑） |
| 快速批量探测 | `httpx -l urls.txt -tech-detect -status-code -title -server -cdn` |
| 服务版本 | `nmap -sV -p 80,443 target` |
| SSL 配置 | `nmap --script ssl-cert,ssl-enum-ciphers -p 443 target`、`testssl.sh URL` |

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

1. 先被动（头部/快照）后主动（whatweb -a3/nuclei）
2. 多源交叉验证——单一工具的版本判定常有误
3. CDN 会挡住真实服务器信息——`request_detail` 看 `via`/`x-served-by` 判 CDN 链路
4. 指纹结果在对话中汇总（host → 技术 → 版本 → 置信度），过期组件走 finding

---

### Recording Results

Recon observations are working data, not findings — summarize them in the conversation. Reserve `record_finding` for actual exposures the scan reveals (an open admin panel, an exposed database service, a leaked backup file): one finding per concrete, evidence-backed conclusion, `confidence` set honestly, and `evidence` pointing at the proving tool call (`{ "type": "tool", "toolCallId": "<id>", "toolName": "bash" }`). Findings persist with the session; there is no separate results database to write to.

---

## Resources

- **Scripts**：`scripts/extract_headers.py`（头部分析）、`scripts/tech_matcher.py`（技术匹配）、`scripts/waf_detector.py`（WAF 判定）
- **References**：`references/whatweb_guide.md`、`references/wafw00f_guide.md`、`references/httpx_guide.md`、`references/fingerprinting_techniques.md`（进阶方法）
- **Assets**：`assets/waf-signatures.txt`、`assets/tech-headers.txt`、`assets/cms-fingerprints.txt`
