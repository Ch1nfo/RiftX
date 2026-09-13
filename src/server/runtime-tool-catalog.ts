import { readFile, stat } from "node:fs/promises";

export const RUNTIME_TOOL_CATALOG_PATH = "/opt/riftx/tool-catalog.json";
export const MAX_RUNTIME_TOOL_INDEX_CHARS = 3_000;
export const MAX_TOOL_INVENTORY_PAGE_CHARS = 8_000;
const MAX_CATALOG_BYTES = 2 * 1024 * 1024;

export type RuntimeToolKind = "command" | "python" | "wordlist";
export type RuntimeToolEntry = {
  kind: RuntimeToolKind;
  name: string;
  category: string;
  path?: string;
  version?: string;
  installedPackage?: string;
  description?: string;
  sizeBytes?: number;
  available?: true;
  verification?: "path" | "import" | "metadata";
};
export type RuntimeToolCatalog = { schemaVersion: 1; generatedAt?: string; entries: RuntimeToolEntry[] };
export type RuntimeToolCatalogState =
  | { status: "available"; path: string; catalog: RuntimeToolCatalog }
  | { status: "missing" | "unreadable"; path: string; reason: string };
export type ToolInventoryQuery = {
  category?: string; query?: string; name?: string; kind?: RuntimeToolKind; offset?: number; limit?: number;
};

const verificationNote = "Build-time command paths, Python imports and wordlist metadata; not a functional test of every tool.";
const unavailableNote = "The runtime catalog could not be loaded. Installed tools are unconfirmed; use the terminal to check command paths or Python imports.";

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function validText(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.trim().length > 0 && value.length <= maxLength && !/[\u0000-\u001f\u007f]/.test(value);
}

function parseCatalog(value: unknown): RuntimeToolCatalog {
  const source = record(value);
  if (!source || source.schemaVersion !== 1 || !Array.isArray(source.entries) || source.entries.length > 10_000
    || (source.generatedAt !== undefined && !validText(source.generatedAt, 100))) throw new Error("invalid_catalog");
  const entries = source.entries.map((value): RuntimeToolEntry => {
    const entry = record(value);
    if (!entry || !["command", "python", "wordlist"].includes(String(entry.kind))
      || !validText(entry.name, 200) || !validText(entry.category, 60)) throw new Error("invalid_entry");
    for (const [key, limit] of [["path", 1_024], ["version", 200], ["installedPackage", 200], ["description", 700]] as const) {
      if (entry[key] !== undefined && !validText(entry[key], limit)) throw new Error("invalid_entry_field");
    }
    if ((entry.sizeBytes !== undefined && (!Number.isSafeInteger(entry.sizeBytes) || Number(entry.sizeBytes) < 0))
      || (entry.available !== undefined && entry.available !== true)
      || (entry.verification !== undefined && !["path", "import", "metadata"].includes(String(entry.verification)))) throw new Error("invalid_entry_metadata");
    return {
      kind: entry.kind as RuntimeToolKind, name: entry.name, category: entry.category,
      ...Object.fromEntries(["path", "version", "installedPackage", "description", "sizeBytes", "available", "verification"]
        .filter((key) => entry[key] !== undefined).map((key) => [key, entry[key]]))
    };
  });
  entries.sort((left, right) => left.category.localeCompare(right.category) || left.kind.localeCompare(right.kind) || left.name.localeCompare(right.name));
  return { schemaVersion: 1, ...(source.generatedAt ? { generatedAt: source.generatedAt as string } : {}), entries };
}

/** The runtime loads this once and retains the returned snapshot until it ends. */
export async function getRuntimeToolCatalog(path = RUNTIME_TOOL_CATALOG_PATH): Promise<RuntimeToolCatalogState> {
  try {
    if ((await stat(path)).size > MAX_CATALOG_BYTES) return { status: "unreadable", path, reason: "catalog_too_large" };
    const text = await readFile(path, "utf8");
    if (Buffer.byteLength(text) > MAX_CATALOG_BYTES) return { status: "unreadable", path, reason: "catalog_too_large" };
    return { status: "available", path, catalog: parseCatalog(JSON.parse(text)) };
  } catch (error) {
    const code = (error as { code?: string }).code;
    return code === "ENOENT" ? { status: "missing", path, reason: "catalog_not_found" }
      : { status: "unreadable", path, reason: "catalog_unreadable_or_invalid" };
  }
}

export function validateToolInventoryQuery(params: ToolInventoryQuery) {
  if (!record(params)) throw new Error("Inventory arguments must be an object.");
  const allowed = new Set(["category", "query", "name", "kind", "offset", "limit"]);
  if (Object.keys(params).some((key) => !allowed.has(key))) throw new Error("Unknown inventory argument.");
  for (const [key, limit] of [["category", 60], ["query", 200], ["name", 200]] as const) {
    if (params[key] !== undefined && !validText(params[key], limit)) throw new Error(`Invalid inventory ${key}.`);
  }
  if (params.kind !== undefined && !["command", "python", "wordlist"].includes(params.kind)) throw new Error("Invalid inventory kind.");
  if (params.offset !== undefined && (!Number.isSafeInteger(params.offset) || params.offset < 0)) throw new Error("Inventory offset must be a nonnegative integer.");
  if (params.limit !== undefined && (!Number.isSafeInteger(params.limit) || params.limit < 1 || params.limit > 40)) throw new Error("Inventory limit must be an integer from 1 to 40.");
}

/** Queries metadata only: no command, import, filesystem listing or wordlist read is performed. */
export function queryRuntimeToolCatalog(state: RuntimeToolCatalogState, params: ToolInventoryQuery = {}) {
  validateToolInventoryQuery(params);
  if (state.status !== "available") return { status: state.status, catalogPath: state.path, reason: state.reason, note: unavailableNote, items: [], total: 0, nextOffset: null };
  const normalize = (value: string | undefined) => value?.trim().toLowerCase();
  const category = normalize(params.category), name = normalize(params.name), query = normalize(params.query);
  const entries = state.catalog.entries.filter((entry) => (!category || entry.category.toLowerCase() === category)
    && (!name || entry.name.toLowerCase() === name) && (!params.kind || entry.kind === params.kind)
    && (!query || [entry.name, entry.category, entry.path, entry.description, entry.installedPackage, entry.version].some((value) => value?.toLowerCase().includes(query))));
  const offset = params.offset ?? 0;
  if (offset > entries.length) throw new Error("Inventory offset exceeds the number of matching entries.");
  const page = {
    status: "available" as const, catalogPath: state.path, note: verificationNote,
    offset, total: entries.length, nextOffset: null as number | null, items: [] as RuntimeToolEntry[]
  };
  for (let index = offset; index < Math.min(entries.length, offset + (params.limit ?? 20)); index++) {
    const nextOffset = index + 1 < entries.length ? index + 1 : null;
    if (JSON.stringify({ ...page, items: [...page.items, entries[index]], nextOffset }).length > MAX_TOOL_INVENTORY_PAGE_CHARS) {
      if (!page.items.length) throw new Error("Inventory entry exceeds the page budget.");
      break;
    }
    page.items.push(entries[index]);
    page.nextOffset = nextOffset;
  }
  return page;
}

/** An active challenge gets one replaceable preview, never the full catalog. */
export function buildRuntimeToolIndex(state: RuntimeToolCatalogState, activeUniqueCode?: string | null): string {
  if (!activeUniqueCode) return "";
  const packet: Record<string, unknown> = {
    type: "runtime_tool_inventory", activeChallenge: activeUniqueCode.length <= 200 ? activeUniqueCode : undefined,
    catalogPath: state.path, catalogStatus: state.status, previewOnly: true,
    lookup: { tool: "tool_inventory", arguments: { offset: 0, limit: 20 } },
    note: state.status === "available" ? verificationNote : unavailableNote
  };
  if (state.status !== "available") return JSON.stringify(packet);
  packet.counts = Object.fromEntries(["command", "python", "wordlist"].map((kind) => [kind, state.catalog.entries.filter((entry) => entry.kind === kind).length]));
  const categories: Array<{ category: string; commands: string[]; python: string[]; wordlists: string[] }> = [];
  packet.categories = categories;
  for (const category of new Set(state.catalog.entries.map((entry) => entry.category))) {
    const members = state.catalog.entries.filter((entry) => entry.category === category);
    const names = (kind: RuntimeToolKind, limit: number) => members.filter((entry) => entry.kind === kind).slice(0, limit).map((entry) => entry.name);
    categories.push({ category, commands: names("command", 5), python: names("python", 3), wordlists: names("wordlist", 2) });
    if (JSON.stringify(packet).length > MAX_RUNTIME_TOOL_INDEX_CHARS) categories.pop();
  }
  return JSON.stringify(packet);
}
