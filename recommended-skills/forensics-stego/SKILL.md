---
name: forensics-stego
description: Forensics and steganography 取证与隐写:文件雕刻(binwalk/foremost)、元数据(exiftool)、隐写检测与提取(zsteg/steghide)、PCAP 流量分析(tshark)、OCR 与二维码(tesseract/zbarimg)。Use this skill when an artifact hides data — forensics 取证/隐写/流量分析/pcap/图片隐藏/文件恢复/内存镜像/压缩包密码.
---

# 取证与隐写(Forensics / Stego)

## 固定开局

1. `file art.*` + `exiftool art.*`(注释/作者/GPS/历史);`strings -n 8` 扫可读串。
2. 图片先验真身:后缀 ≠ 真实格式,`xxd art.png | head` 对魔数。
3. 压缩包:伪加密/注释字段先看;`fcrackzip` 不在镜像里,密码爆破走
   `zip2john` → hashcat + 自带字典。

## 隐写(按载体)

- PNG/BMP:`zsteg -a x.png`(bit 平面/LSB 全扫);EOF 追加数据用 binwalk 扫
  签名、`tail -c +<offset>` 切出。
- JPEG:`steghide extract -sf x.jpg -p <pass>`(空密码先试);带密码的用
   `steghide` + 字典喷(python 循环,字典 /opt/wordlists/Passwords/
  10k-most-common.txt;**没有 rockyou**)。
- 双图/视觉对比:python PIL 差分、通道分离、灰度拉伸。
- 音频:numpy 读样本找异常段;spectrogram 用 numpy + PIL 自绘,肉眼找高频图形。
- 文档:olefile/pypdf(python 已装);宏与嵌入对象先列再提。
- 兜底:`binwalk -e x`(签名内嵌解包)、`foremost x`(雕刻恢复)。

## 流量(pcap)

- 会话定位:`tshark -r x.pcap -q -z conv,tcp`(再看 udp)。
- 读内容:`-z follow,tcp,ascii,<id>`;HTTP 导对象
  `--export-objects http,dir`(同法 smb/ftp/dns)。
- 隧道特征:超长标签的 DNS 查询、ICMP payload 异常;TLS 流查有无泄露的
  keylog(题给 sslkeylogfile 常是提示)。

## 内存/镜像

镜像里没有 volatility:先 `strings` 抽凭据/flag 模式,再 python 手工解析结构
(题给的 profile/符号常是下一步提示)。

## RiftX 工作流

- 切出的文件统一落 work/carved/,按来源命名;flag 一出现立即 submit。
- 关键结论(载体类型、隐写方式、破解出的密码)checkpoint(signalKind=note,
  evidenceRef=文件名+提取命令);破解出的密码可能可复用,跨阶段记牢。
- 大 pcap/镜像扫描放后台、输出重定向到文件再 grep,不刷上下文。

## 验收清单(Testing Checklist)

- [ ] file/exiftool/strings 三件套跑过,载体与格式确认无误。
- [ ] 图片上的文字过了 tesseract、二维码过了 zbarimg(一步到位,别跳过)。
- [ ] carved/ 产物齐全,提取命令写进了 checkpoint 的 evidenceRef。
- [ ] flag 已 submit;密码类发现已记黑板供凭据重用。

## 反模式

- 不做 file/exiftool 直接上 zsteg——载体搞错全白跑。
- 无脑 rockyou 喷密码——镜像里没有,用自带 10k/100k 表。
- 大文件扫描前台裸跑刷屏——后台 + 落盘 + grep。
