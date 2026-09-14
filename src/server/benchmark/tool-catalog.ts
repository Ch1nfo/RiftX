import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";

const execFileAsync = promisify(execFile);
const CATALOG = {
  web: ["curl", "wget", "nmap", "ffuf", "gobuster", "nikto", "sqlmap"],
  internal: ["nmap", "smbclient", "rpcclient", "ldapsearch", "ssh", "socat", "proxychains4"],
  reverse_pwn: ["file", "strings", "readelf", "objdump", "nm", "gdb", "checksec", "python3"],
  exploit: ["msfconsole", "searchsploit", "gcc", "make", "zip", "unzip"]
} as const;

async function installed(command: string): Promise<boolean> {
  try { await execFileAsync("sh", ["-lc", `command -v ${command}`]); return true; } catch { return false; }
}

export function createBenchmarkToolCatalogTool(): ToolDefinition {
  return {
    name: "benchmark_tool_catalog",
    label: "Benchmark tool catalog",
    description: "List tools available in the current Benchmark runtime, grouped by web, internal, reverse/pwn and exploit capabilities.",
    parameters: Type.Object({ refresh: Type.Optional(Type.Boolean({ description: "Recheck PATH instead of using the session snapshot" })) }),
    async execute() {
      const groups = await Promise.all(Object.entries(CATALOG).map(async ([group, commands]) => [group, await Promise.all(commands.map(async (command) => ({ command, installed: await installed(command) })))] as const));
      return { content: [{ type: "text", text: `<riftx-benchmark-tool-catalog>\n${groups.map(([group, items]) => `## ${group}\n${items.map((item) => `- ${item.command}: ${item.installed ? "available" : "missing"}`).join("\n")}`).join("\n\n")}\n</riftx-benchmark-tool-catalog>` }], details: { groups } };
    }
  };
}
