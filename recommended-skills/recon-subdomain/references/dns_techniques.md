# Advanced DNS Enumeration Techniques

Advanced methods for comprehensive DNS reconnaissance.

## Certificate Transparency Log Search

证书透明(CT)日志与第三方在线证书搜索服务均需公网访问,离线环境跳过。离线等价方案是 DNS 字典爆破:

### DNS 字典爆破(离线等价)

```bash
# 查看镜像内可用 DNS 词表
ls /opt/wordlists/DNS/ 2>/dev/null

# 词表 + dig 解析循环
WORDLIST=/opt/wordlists/DNS/dns.txt
while read -r sub; do
  [ -z "$sub" ] && continue
  ans=$(dig +short "${sub}.example.com" A 2>/dev/null)
  [ -n "$ans" ] && echo "${sub}.example.com -> $ans"
done < "$WORDLIST" | tee resolved_subs.txt

# vhost 方式确认(对 Web 服务):ffuf 用 Host 头爆破
ffuf -u http://TARGET_IP/ -H "Host: FUZZ.example.com" -w "$WORDLIST" -fs SIZE_OF_DEFAULT
```

---

## DNS Zone Transfer

### Attempt Zone Transfer

```bash
# Using dig
dig axfr @ns1.example.com example.com

# Using host
host -t axfr example.com ns1.example.com

# Multiple nameservers
for ns in $(dig +short NS example.com); do
  echo "Trying $ns..."
  dig axfr @$ns example.com
done
```

**Note:** Zone transfers are rarely allowed in modern configurations.

---

## DNS Record Types

### ALL Record Query

```bash
# Query all record types
dig ANY example.com +noall +answer

# Specific types
dig A example.com
dig AAAA example.com
dig CNAME example.com
dig MX example.com
dig NS example.com
dig TXT example.com
dig SOA example.com
dig SRV example.com
dig PTR example.com
```

### TXT Record Enumeration

```bash
# All TXT records
dig txt example.com +short

# Specific TXT records (SPF, DKIM, DMARC)
dig txt _dmarc.example.com +short
dig txt default._domainkey.example.com +short
```

---

## SRV Record Discovery

```bash
# Common SRV records
# Active Directory
dig _ldap._tcp.example.com SRV
dig _kerberos._tcp.example.com SRV

# SIP
dig _sip._tcp.example.com SRV

# MySQL
dig _mysql._tcp.example.com SRV
```

---

## DNSSEC Discovery

```bash
# Check for DNSSEC
dig +dnssec example.com DNSKEY +short

# DS records
dig ds example.com +short

# RRSIG records
dig rrsig example.com +short
```

---

## Wildcard Detection Techniques

### Basic Wildcard Test

```bash
# Test random subdomain
dig +short randomtest12345.example.com
```

### Automated Wildcard Detection

```bash
# Generate random subdomains
for i in {1..10}; do
  sub="test$i$(openssl rand -hex 4).example.com"
  if [ -n "$(dig +short "$sub" 2>/dev/null)" ]; then
    echo "Wildcard detected: $sub"
  fi
done
```

---

## Subdomain Permutations

### Custom Permutations

```bash
# Common patterns: generate + resolve in one dig loop
for sub in $(cat subs.txt); do
  # Add dev/stage/prod
  for cand in "${sub}-dev" "${sub}-staging" "${sub}-prod" "dev-${sub}" "stage-${sub}" "prod-${sub}"; do
    ans=$(dig +short "$cand.example.com" 2>/dev/null)
    [ -n "$ans" ] && echo "$cand.example.com -> $ans"
  done
done

# 词表拼接变体(dev/www/api/mail + 已知子域前缀)同理循环 dig
while read -r prefix; do
  while read -r sub; do
    cand="${prefix}.${sub}.example.com"
    ans=$(dig +short "$cand" 2>/dev/null)
    [ -n "$ans" ] && echo "$cand -> $ans"
  done < subs.txt
done < /opt/wordlists/DNS/dns.txt 2>/dev/null
```

---

## DNSSEC and DANE

### Check DANE Records

```bash
# TLSA records
dig _443._tcp.example.com TLSA +short

# SMTP DANE
dig _25._tcp.mail.example.com TLSA +short
```

---

## Reverse DNS Enumeration

### From IP Range

```bash
# Reverse lookup IP range with dig
for ip in $(seq 1 254); do
  out=$(dig +short -x 192.168.1.$ip 2>/dev/null)
  [ -n "$out" ] && echo "192.168.1.$ip -> $out"
done
```

---

## DNS Cache Snooping

```bash
# Check if nameserver has cached record
dig @ns1.example.com sub.example.com A +norecurse
```

---

## Stealthy DNS Enumeration

### Slow Query Rate

```bash
# With delay(逐个 dig 解析,间隔 1s)
while read -r sub; do
  ans=$(dig +short "$sub" 2>/dev/null)
  [ -n "$ans" ] && echo "$sub -> $ans"
  sleep 1
done < subs.txt
```

### Using Different Resolvers

```bash
# Public DNS resolvers
resolvers=(
  "8.8.8.8"
  "1.1.1.1"
  "64.6.64.6"
  "208.67.222.222"
)

for resolver in "${resolvers[@]}"; do
  dig @$resolver example.com
done
```

---

## Security Testing Considerations

1. **Rate Limiting** - Avoid rapid queries to prevent blocking
2. **Source IP Rotation** - Use multiple IPs for large scans
3. **Passive First** - Use passive methods before active enumeration
4. **Wildcards** - Always check for wildcard DNS patterns
5. **Verification** - Resolve all discovered subdomains

---

## Tools Comparison

| Technique | Tools | Detects |
|-----------|-------|---------|
| CT 日志查询 | 需公网,离线跳过(改用字典爆破) | Historical SSL certificates |
| Zone Transfer | dig | Complete zone (if allowed) |
| SRV Discovery | dig | Service-specific records |
| DNSSEC | dig | DNSSEC configuration |
| Reverse DNS | dig -x | Domains from IP ranges |
| Permutations | bash + dig 循环 | Dev/prod variants |
| Wildcards | dig + 随机子域 | Wildcard patterns |
