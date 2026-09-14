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

# TLS protocol/cipher via openssl
openssl s_client -connect target.com:443 -tls1_2 </dev/null 2>/dev/null | grep -E "Protocol|Cipher"

# Check which TLS versions are enabled
for v in tls1 tls1_1 tls1_2 tls1_3; do
  echo "== $v =="
  openssl s_client -connect target.com:443 -$v </dev/null 2>/dev/null | grep -E "Protocol|Cipher" | head -2
done
```

## JARM Fingerprinting

```bash
# Get JARM hash
curl -s https://api.ssllabs.com/api/v3/info -d '{"host":"target.com"}'
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

### 3. Automated Detection

```bash
# Header + content fingerprint
curl -si https://target.com | head -40

# Service/version detection
nmap -sV -sC target.com

# WAF manual fingerprint:
# 1. Baseline: curl -si https://target.com/ — record status/headers/body
# 2. Attack probe: curl -si "https://target.com/?q=<script>alert(1)</script>" — check for block page / 403
# 3. Payload mutation comparison: encode/mutate the probe, diff responses to map WAF behavior
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
