---
name: reverse-analysis
description: Static and dynamic binary reverse engineering 逆向工程与反编译:file/readelf/strings 侦察、Ghidra 反编译(riftx-decompile/analyzeHeadless)、patch 绕过校验、qemu-user 跑异构、ltrace/strace 动态追踪。Use this skill when the task is understanding a binary to recover a flag or logic — reverse 逆向/反编译/破解/算法还原/加壳/VM 壳.
---

# 逆向工程(Reverse)

## 侦察(所有题通用,前 5 分钟)

1. `file chal` → 架构/静态动态/是否 strip;`readelf -h -d -s chal`(入口、依赖、符号);
   `strings -n 8 chal | less` 先扫 flag 格式、提示语、算法常量。
2. 非 x86/x86_64:`qemu-<arch> ./chal` 直接运行,别当成文件损坏。
3. 高级语言痕迹:Go/Rust(符号多、体积大)、Java(先 `unzip` jar 再 `cfr` 反编译)、
   Android(APK 用 `jadx`/`apktool`)。
4. 工具是否可用拿不准先 `tool_inventory`(按名/分类查,元数据即可)。

## 反编译主路径

- 单函数/中小题:`riftx-decompile chal out.c [函数名子串]`——Ghidra headless 直出
  C 伪代码,比读汇编快一个数量级。
- 整程序批分析:`analyzeHeadless /tmp/proj proj -import chal`(大题后台跑,期间
  继续 strings/动态侧);关键函数配合 `objdump -d -M intel` 对读。
- 定位法:从 `main` 下钻;先在 strings 里找到提示性字符串,反查引用它的函数,
  flag 校验逻辑通常就在引用点附近。

## 校验逻辑的标准打法

- 比较型:提取目标串/哈希/表,用 python 离线复现算法逆推。
- 逐字符校验(常带反调试):`ltrace`/`strace` 看比较参数;或 gdb 断在比较点读
  寄存器/栈,直接拿正确字符。
- 能 patch 就不硬解:比较跳转 `jnz→jmp`(`objdump` 找偏移,python 改字节另存);
  非本机 libc 用 `patchelf` 修解释器/rpath。
- 自修改/VM 壳:动态优先——strace 看是否解包落盘临时文件;qemu+gdb 单步;
  VM 类把 handler 表 dump 出来,用 python 写小反汇编器跑 bytecode。

## RiftX 工作流

- 产物落 work/(cwd 即本题工作目录,跨 attempt 持久):out.c、patch.py、solver.py;
  还原出的算法写成可重放脚本,flag 一出现立即
  `benchmark_control(action="submit", uniqueCode, flag)`。
- 大型 analyzeHeadless 放后台、输出重定向到文件再 grep,不刷上下文。
- 关键结论(算法结构、密钥常量、patch 偏移)`benchmark_control(action="checkpoint")`
  (signalKind=note,evidenceRef=函数名/偏移),不要只在对话里"记着"。
- 每次 attempt 限时 30 分钟;到点前 checkpoint 留进度(nextProbe 写下一步),
  defer 后下次接着推,题不清尾。

## 验收清单(Testing Checklist)

- [ ] file/readelf/strings 三件套跑过,格式与架构确认无误。
- [ ] 校验逻辑已定位到具体函数,证据(反编译片段/断点输出)在 work/ 里。
- [ ] patch/solver 脚本从零重放能再出 flag。
- [ ] flag 已 submit;未解完时已 checkpoint 留算法结论与 nextProbe。

## 反模式

- 只读汇编不反编译——伪代码快一个数量级。
- 忘了 qemu-user:异架构二进制 `./chal` 报 exec format error 不是文件坏了。
- 硬解复杂校验之前,没先试 patch 跳转 / 动态断点读值这两条便宜路。
