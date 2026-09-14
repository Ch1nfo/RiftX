---
name: crypto-attacks
description: Cryptographic challenge solving 密码学攻击:编码识别、经典密码与频率分析、RSA/AES/流密码弱点、格与 LWE、z3/gmpy2/fpylll 离线数学、加密容器爆破。Use this skill when the challenge is cryptographic — crypto 密码学/加密/解密/RSA/AES/异或/编码/古典密码/数学题.
---

# 密码学(Crypto)

## 先分层:编码 ≠ 加密

- base64/base32/hex/base85/rot13、URL 编码:`base64 -d`、`xxd -r -p` 或 python
  一把梭;不明 blob 先把常见编码各试一遍。
- 题面(标题、文件名、图片内容)常直接暗示算法——先读题再套公式。

## 经典与对称

- 异或:已知明文头(flag{、PNG 魔数)直接还原密钥流;重复密钥按密钥长度分列
  频率统计破解。
- AES:ECB(等长块重排/copy-paste 拼接)、CBC(IV 位翻转、padding oracle)、
  CTR/流密码(nonce 重用 → 两密文异或消密钥流)、密钥来自弱源(时间戳、
  小字典、ID)。
- 古典密码:频率分析 + python 复现;栅栏/维吉尼亚/培根先跑快速检验。

## RSA

- 小 e + 短消息:直接开 e 次方(gmpy2.iroot)。
- e 偏大:Wiener(d 小)、共模、相关素数(费马分解 |p-q| 小)。
- N 需要分解:先估规模——小 N 用 sympy.factorint/小素数筛;**没有在线分解
  服务(无公网)**,2048 位无弱点 N 不是本题解法,回题面找提示。
- 格/线性代数:HNP、Coppersmith 小根、LWE → z3 约束或 fpylll 本地解。

## 工具(全部本地,无在线服务)

- python3:pycryptodome(Crypto)、sympy(整数函数)、gmpy2(大数)、z3(约束)、
  fpylll(格)、PyJWT。
- openssl 命令行:证书/非对称验算、对称加解密复现。
- 加密压缩包/office/pdf:`zip2john`/`office2john`/`pdf2john` 系列(john 自带)
  提 hash → hashcat 爆破;字典用 /opt/wordlists/Passwords/
  10k-most-common.txt 或 100k-most-used-passwords-NCSC.txt(**没有 rockyou**)。

## RiftX 工作流

- 推导逐步写进 work/solve.py(可重放),还原出的中间量(n、d、密钥、明文片段)
  checkpoint 留黑板(signalKind=note,evidenceRef=中间值来源);flag 立即 submit。
- 先估算可行性再动手:数学题的"便宜口子"几乎总在题面或参数本身。

## 验收清单(Testing Checklist)

- [ ] 编码/加密已分层确认(不是把 base64 当 AES 打)。
- [ ] 参数(n/e/c/p/q/nonce)已抄录进 work/,不是只在对话里出现过。
- [ ] solve.py 重放可再出 flag;flag 已 submit。
- [ ] 爆破类已核对字典路径存在(ls /opt/wordlists/...)再开跑。

## 反模式

- 对无弱点的 2048 位 N 硬算——出题人一定留了更便宜的路线。
- 忘了无公网:在线 factordb/RsaCtfTool 类工具/服务不存在。
- 加密容器爆破用不存在的 rockyou——先 ls 字典目录。
