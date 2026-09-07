import { access, readFile, readdir, stat } from "node:fs/promises";
import {
  createEditToolDefinition,
  createFindToolDefinition,
  createGrepToolDefinition,
  createLsToolDefinition,
  createReadToolDefinition,
  createWriteToolDefinition,
  type ToolDefinition
} from "@mariozechner/pi-coding-agent";
import { withToolDeadline } from "./tool-deadline";

export const LOCAL_TOOL_TIMEOUT_MS = 60_000;

/**
 * RiftX-owned copies of Pi's local tools with a finite caller-visible
 * deadline. grep/ls receive async filesystem operations so a slow mount does
 * not synchronously block Node's event loop and prevent the timer/Stop signal
 * from firing.
 */
export function createTimedLocalTools(cwd: string, timeoutMs = LOCAL_TOOL_TIMEOUT_MS): ToolDefinition[] {
  const definitions: ToolDefinition[] = [
    createReadToolDefinition(cwd) as ToolDefinition,
    createGrepToolDefinition(cwd, {
      operations: {
        isDirectory: async (path) => (await stat(path)).isDirectory(),
        readFile: (path) => readFile(path, "utf8")
      }
    }) as ToolDefinition,
    createFindToolDefinition(cwd) as ToolDefinition,
    createLsToolDefinition(cwd, {
      operations: {
        exists: async (path) => access(path).then(() => true, () => false),
        stat,
        readdir
      }
    }) as ToolDefinition,
    createWriteToolDefinition(cwd) as ToolDefinition,
    createEditToolDefinition(cwd) as ToolDefinition
  ];

  return definitions.map((tool) => withToolDeadline(tool, timeoutMs));
}
