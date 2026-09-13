import { defineTool, type ToolDefinition } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { getRuntimeToolCatalog, queryRuntimeToolCatalog, type RuntimeToolCatalogState } from "../../runtime-tool-catalog";

export function createToolInventoryTool(getCatalog: () => Promise<RuntimeToolCatalogState> = getRuntimeToolCatalog): ToolDefinition {
  let catalog: Promise<RuntimeToolCatalogState> | undefined;
  return defineTool({
    name: "tool_inventory",
    label: "Runtime tool inventory",
    description: "List this runtime's available commands, Python modules and wordlist paths from its build-time catalog. Filter by category, exact name, kind or text query; follow nextOffset for more results. This reads metadata only and does not execute tools or read wordlist contents.",
    promptSnippet: "Discover runtime commands, Python modules and wordlist paths with tool_inventory; use category, name or query filters.",
    parameters: Type.Object({
      category: Type.Optional(Type.String({ minLength: 1, maxLength: 60 })),
      query: Type.Optional(Type.String({ minLength: 1, maxLength: 200 })),
      name: Type.Optional(Type.String({ minLength: 1, maxLength: 200 })),
      kind: Type.Optional(Type.Union([Type.Literal("command"), Type.Literal("python"), Type.Literal("wordlist")])),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 40 }))
    }, { additionalProperties: false }),
    async execute(_toolCallId, params) {
      const page = queryRuntimeToolCatalog(await (catalog ??= getCatalog()), params);
      return { content: [{ type: "text" as const, text: JSON.stringify(page) }], details: page };
    }
  });
}
