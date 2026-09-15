# Advanced Web Fingerprinting Techniques

Comprehensive methods for identifying web technologies.

## HTTP Header Analysis

### Server Headers

```bash
# Extract server header
curl -I https://target.com | grep -i server

# Get all headers
curl -I https://target.com
```

### X-Powered-By Headers

```bash
curl -I https://target.com | grep -i "x-powered-by"
```

**Common values:**
- `PHP/7.4.3` - PHP
- `Express` - Node.js Express
- `ASP.NET` - Microsoft .NET
- `Servlet/3.1` - Java Servlet

### Framework-Specific Headers

| Framework | Header Pattern |
|-----------|---------------|
| Laravel | `X-CSRF-TOKEN`, `laravel_session` |
| Django | `csrftoken` |
| Rails | `X-Runtime`, `X-Request-Id` |
| Spring Boot | `X-Application-Context` |
| Next.js | `X-Powered-By: Next.js` |

## Page Content Analysis

### HTML Meta Tags

```bash
# Extract meta tags
curl -s https://target.com | grep -i "<meta"
```

**Generator tags:**
- `<meta name="generator" content="WordPress 5.9">`
- `<meta name="generator" content="Drupal 9">`

### JavaScript Libraries

```bash
# Check for framework files
curl -s https://target.com | grep -E "\.js" | grep -i "react\|vue\|angular"

# Check script tags
curl -s https://target.com | grep -i "<script" | grep -E "src="
```

### CSS Frameworks

```bash
# Check CSS files
curl -s https://target.com | grep -i "\.css" | grep -i "bootstrap\|tailwind\|bulma"
```

## Cookie Analysis

```bash
# Get cookies
curl -I https://target.com | grep -i set-cookie

# Detailed cookie info
curl -v https://target.com 2>&1 | grep -i cookie
```

### Framework Cookies

| Framework | Cookie Pattern |
|-----------|----------------|
| PHP | `PHPSESSID` |
| Laravel | `laravel_session` |
| Django | `sessionid` |
| Rails | `_session_id` |
| Express | `connect.sid` |
| Spring | `JSESSIONID` |
| WordPress | `wp-settings` |

## URL Path Patterns

```bash
# Check for framework-specific paths
curl -I https://target.com/wp-admin
curl -I https://target.com/admin
curl -I https://target.com/user/login
curl -I https://target.com/sitemap.xml
curl -I https://target.com/robots.txt
```

### CMS-Specific Paths

| CMS | Paths |
|-----|-------|
| WordPress | `/wp-admin`, `/wp-login.php`, `/wp-json/` |
| Drupal | `/user/login`, `/admin`, `/modules/` |
| Joomla | `/administrator/`, `/components/` |
| Magento | `/admin/`, `/downloader/` |
| TYPO3 | `/typo3/` |

## Source Map Analysis

```bash
# Check for source maps
curl -s https://target.com | grep -i "\.map"

# Fetch and analyze source map
curl -s https://target.com/main.js.map | jq .
```

## JSON Endpoints

```bash
# Check common API endpoints
curl -s https://target.com/api/v1
curl -s https://target.com/graphql
curl -s https://target.com/__webpack_dev_server__
```

## SSL/TLS Fingerprinting

```bash
# SSL certificate info
openssl s_client -connect target.com:443 -servername target.com

# Certificate details
curl -vI https://target.com 2>&1 | grep -i ssl

# TLS 版本/密码套件逐项探测
for tls in tls1 tls1_1 tls1_2 tls1_3; do
  echo "== $tls =="
  echo | openssl s_client -connect target.com:443 -servername target.com -$tls 2>&1 | grep -E "Protocol|Cipher|Verify"
done
```

## JARM Fingerprinting

```bash
# JARM/SSL Labs 在线 API 需公网,离线跳过;本地用 openssl 指纹替代:
echo | openssl s_client -connect target.com:443 -servername target.com 2>/dev/null |
  openssl x509 -noout -fingerprint -sha256 -subject -issuer -dates
```

## Technology Stack Detection Workflow

### 1. Passive Reconnaissance

```bash
# Headers first
curl -I https://target.com

# Page content
curl -s https://target.com | head -100

# Robots.txt
curl -s https://target.com/robots.txt

# Sitemap
curl -s https://target.com/sitemap.xml
```

### 2. Active Probing

```bash
# Framework-specific paths
curl -I https://target.com/wp-admin
curl -I https://target.com/admin
curl -I https://target.com/api/

# Static files
curl -I https://target.com/favicon.ico
curl -I https://target.com/apple-touch-icon.png
```

### 3. Automated Detection(本地等价)

自动指纹扫描器不在镜像内,用 curl + nmap 等价:

```bash
# 响应头指纹(Server/X-Powered-By/CDN 头)
curl -sI https://target.com

# nmap 服务与脚本探测(版本 + 默认页面指纹)
nmap -sV -sC -p 80,443 target.com

# WAF 检测:对比恶意载荷前后的响应差异
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" "https://target.com/?q=normal"
curl -s -o /dev/null -w "%{http_code} %{size_download}\n" "https://target.com/?q=<script>alert(1)</script>"
# 拦截页/403/连接重置 => 前置 WAF;再手工对比响应头与拦截页特征
```

## Bypass Techniques

### Header Spoofing

```bash
# Change User-Agent
curl -I https://target.com -A "Googlebot/2.1"

# Add common headers
curl -I https://target.com -H "X-Forwarded-For: 1.2.3.4"
```

### Alternate Paths

```bash
# Try different endpoints
curl -I https://target.com/health
curl -I https://target.com/status
curl -I https://target.com/api/v1
```

## Tips

1. **Multiple sources** - Combine multiple tools
2. **Verify findings** - False positives are common
3. **Version accuracy** - May not be precise
4. **Passive first** - Don't trigger security controls
5. **Document everything** - Keep track of findings
