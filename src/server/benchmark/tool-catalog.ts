import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";
import { Type } from "@sinclair/typebox";
import type { ToolDefinition } from "@mariozechner/pi-coding-agent";

const execFileAsync = promisify(execFile);

/** Build-time inventory written by docker/build-tool-catalog.py: commands,
 * Python modules and wordlist metadata with verified availability. Using it
 * keeps this tool and the image provisioning from drifting apart. */
export const IMAGE_TOOL_CATALOG_PATH = "/opt/riftx/tool-catalog.json";

type ImageCatalogEntry = {
  kind?: unknown; name?: unknown; category?: unknown;
  path?: unknown; version?: unknown; available?: unknown;
};
type ImageCatalog = { entries?: unknown };

type CatalogItem = { name: string; detail: string };
type CatalogGroup = { category: string; items: CatalogItem[] };
type ResolvedCatalog = { source: "image-catalog" | "path-probe"; groups: CatalogGroup[] };

/** Dev fallback when the image catalog is absent: probe PATH directly. */
const FALLBACK_COMMANDS = {
  web: ["curl", "wget", "nmap", "ffuf", "gobuster", "sqlmap"],
  network: ["ssh", "nc", "socat", "dig", "proxychains4", "tcpdump", "tshark"],
  passwords: ["hydra", "john", "hashcat"],
  crypto: ["openssl"],
  reverse: ["file", "strings", "readelf", "objdump", "nm", "gdb", "checksec", "ROPgadget", "strace", "ltrace"],
  runtime: ["python3", "gcc", "make", "zip", "unzip"]
} as const;

async function installed(command: string): Promise<boolean> {
  try { await execFileAsync("sh", ["-lc", `command -v ${command}`]); return true; } catch { return false; }
}

async function probeCommands(): Promise<CatalogGroup[]> {
  return Promise.all(Object.entries(FALLBACK_COMMANDS).map(async ([category, commands]) => ({
    category,
    items: await Promise.all([...commands].map(async (command) => ({
      name: command, detail: await installed(command) ? "available" : "missing"
    })))
  })));
}

function groupsFromImageCatalog(catalog: ImageCatalog): CatalogGroup[] {
  const byCategory = new Map<string, CatalogItem[]>();
  for (const entry of Array.isArray(catalog.entries) ? catalog.entries as ImageCatalogEntry[] : []) {
    if (entry.available === false) continue;
    const name = typeof entry.name === "string" ? entry.name : undefined;
    const category = typeof entry.category === "string" ? entry.category : undefined;
    if (!name || !category) continue;
    const kind = entry.kind === "wordlist" ? "wordlist" : entry.kind === "python" ? "python module" : "command";
    // Wordlist paths are the actionable part; command paths are noise.
    const detail = [kind,
      typeof entry.version === "string" ? entry.version : undefined,
      entry.kind === "wordlist" && typeof entry.path === "string" ? entry.path : undefined
    ].filter(Boolean).join(" ");
    const items = byCategory.get(category) ?? [];
    items.push({ name, detail });
    byCategory.set(category, items);
  }
  return [...byCategory.entries()].map(([category, items]) => ({ category, items }));
}

const catalogCache = new Map<string, ResolvedCatalog>();

async function resolveCatalog(catalogPath: string): Promise<ResolvedCatalog> {
  try {
    const groups = groupsFromImageCatalog(JSON.parse(await readFile(catalogPath, "utf8")) as ImageCatalog);
    if (groups.length) return { source: "image-catalog", groups };
  } catch {
    // Missing or unreadable image catalog (e.g. local dev): probe PATH instead.
  }
  return { source: "path-probe", groups: await probeCommands() };
}

export function createBenchmarkToolCatalogTool(catalogPath = IMAGE_TOOL_CATALOG_PATH): ToolDefinition {
  return {
    name: "benchmark_tool_catalog",
    label: "Benchmark tool catalog",
    description: "List the tools installed in this benchmark runtime — commands, Python modules, and bundled wordlist paths — grouped by category (web, network, ad, passwords, crypto, reverse, forensics, databases, runtime, ...). The hosted image has no public Internet and no package installation, so call this once when an attempt starts and treat it as authoritative over tool names mentioned in prompts or playbooks.",
    promptSnippet: "benchmark_tool_catalog(refresh?)",
    parameters: Type.Object({ refresh: Type.Optional(Type.Boolean({ description: "Rebuild the listing instead of reusing this process's cached catalog snapshot" })) }),
    async execute(_toolCallId: string, params: { refresh?: boolean }) {
      let catalog = params.refresh ? undefined : catalogCache.get(catalogPath);
      if (!catalog) {
        catalog = await resolveCatalog(catalogPath);
        catalogCache.set(catalogPath, catalog);
      }
      const text = `<riftx-benchmark-tool-catalog>\nsource: ${catalog.source}\n${catalog.groups.map((group) => `## ${group.category}\n${group.items.map((item) => `- ${item.name}: ${item.detail}`).join("\n")}`).join("\n\n")}\n</riftx-benchmark-tool-catalog>`;
      return { content: [{ type: "text" as const, text }], details: { source: catalog.source, groups: catalog.groups } };
    }
  };
}
