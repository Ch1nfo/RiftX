import { randomUUID } from "node:crypto";
import { mkdir, open, readdir, stat, unlink } from "node:fs/promises";
import { join } from "node:path";

/**
 * Shared projection for verbose custom-tool output. The complete text is kept
 * locally while the model receives a bounded head/tail preview and a path it
 * can inspect with read/grep. No summarizer model is involved.
 */

export const TOOL_OUTPUT_INLINE_CHARS = 16_000;
/** 4M characters per artifact × 64 retained files bounds one session to a
 * practical worst case without letting a hostile MCP server fill the disk. */
export const TOOL_ARTIFACT_MAX_CHARS = 4_000_000;
const TOOL_ARTIFACT_MAX_FILES = 64;
const PREVIEW_HEAD_CHARS = 10_000;
const PREVIEW_TAIL_CHARS = 4_000;

export type ToolOutputProjection = {
  text: string;
  artifactPath?: string;
  truncation?: {
    truncated: true;
    totalChars: number;
    shownChars: number;
    artifactTruncated?: true;
  };
};

export type ToolOutputStore = {
  project(toolName: string, chunks: readonly string[], summary: string): Promise<ToolOutputProjection>;
};

export type ToolArtifactRef = {
  path: string;
  size: number;
  createdAt: string;
};

function safeSegment(value: string) {
  return value.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || "tool";
}

export function toolArtifactDir(root: string, sessionId: string, childId?: string) {
  const sessionDir = join(root, safeSegment(sessionId));
  return childId ? join(sessionDir, `child-${safeSegment(childId)}`) : sessionDir;
}

function previewChunks(chunks: readonly string[]) {
  let head = "";
  let tail = "";
  for (const chunk of chunks) {
    if (head.length < PREVIEW_HEAD_CHARS) head += chunk.slice(0, PREVIEW_HEAD_CHARS - head.length);
    tail = chunk.length >= PREVIEW_TAIL_CHARS
      ? chunk.slice(-PREVIEW_TAIL_CHARS)
      : `${tail}${chunk}`.slice(-PREVIEW_TAIL_CHARS);
  }
  return { head, tail };
}

function projectedPreview(summary: string, totalChars: number, head: string, tail: string, artifactPath?: string, persistError?: unknown): ToolOutputProjection {
  const shownChars = head.length + tail.length;
  const artifactTruncated = totalChars > TOOL_ARTIFACT_MAX_CHARS;
  return {
    text: [
      summary,
      artifactPath
        ? artifactTruncated
          ? `Captured output (first ${TOOL_ARTIFACT_MAX_CHARS} of ${totalChars} chars; safety cap reached): ${artifactPath}`
          : `Full output (${totalChars} chars): ${artifactPath}`
        : `[Full output could not be stored locally: ${persistError instanceof Error ? persistError.message : String(persistError)}]`,
      artifactPath ? `Use read/grep on that file when exact details outside this preview are needed.` : "Only the bounded preview below is available.",
      "\n--- output beginning ---",
      head,
      `\n--- ${Math.max(0, totalChars - shownChars)} chars omitted from context ---`,
      tail,
      "--- output end ---"
    ].join("\n"),
    artifactPath,
    truncation: { truncated: true, totalChars, shownChars, ...(artifactTruncated ? { artifactTruncated: true as const } : {}) }
  };
}

async function pruneOldArtifacts(sessionDir: string) {
  const entries = await readdir(sessionDir, { withFileTypes: true });
  const refs = await Promise.all(entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".txt"))
    .map(async (entry) => ({ name: entry.name, mtimeMs: (await stat(join(sessionDir, entry.name))).mtimeMs })));
  refs.sort((left, right) => right.mtimeMs - left.mtimeMs);
  await Promise.all(refs.slice(TOOL_ARTIFACT_MAX_FILES).map((entry) => unlink(join(sessionDir, entry.name)).catch(() => undefined)));
}

export function createToolOutputStore(root: string, sessionId: string, childId?: string): ToolOutputStore {
  const sessionDir = toolArtifactDir(root, sessionId, childId);
  return {
    async project(toolName, chunks, summary) {
      const totalChars = chunks.reduce((total, chunk) => total + chunk.length, 0);
      if (totalChars <= TOOL_OUTPUT_INLINE_CHARS) return { text: chunks.join("") };

      const { head, tail } = previewChunks(chunks);
      const artifactPath = join(sessionDir, `${Date.now()}-${safeSegment(toolName)}-${randomUUID().slice(0, 8)}.txt`);
      try {
        await mkdir(sessionDir, { recursive: true, mode: 0o700 });
        const file = await open(artifactPath, "wx", 0o600);
        try {
          let remaining = TOOL_ARTIFACT_MAX_CHARS;
          for (const chunk of chunks) {
            if (remaining <= 0) break;
            const captured = chunk.slice(0, remaining);
            await file.writeFile(captured, { encoding: "utf8" });
            remaining -= captured.length;
          }
          if (totalChars > TOOL_ARTIFACT_MAX_CHARS) {
            await file.writeFile(`\n[Artifact capture stopped at the ${TOOL_ARTIFACT_MAX_CHARS}-character safety cap; ${totalChars - TOOL_ARTIFACT_MAX_CHARS} characters were not stored.]\n`, { encoding: "utf8" });
          }
        } finally {
          await file.close();
        }
        await pruneOldArtifacts(sessionDir);
        return projectedPreview(summary, totalChars, head, tail, artifactPath);
      } catch (error) {
        await unlink(artifactPath).catch(() => undefined);
        return projectedPreview(summary, totalChars, head, tail, undefined, error);
      }
    }
  };
}

/** Rebuildable artifact index for compaction/restart continuity. Filenames are
 * opaque to the model; filesystem metadata is enough to recover useful refs
 * without maintaining another concurrent manifest store. */
async function listTxtFiles(dir: string): Promise<ToolArtifactRef[]> {
  let names: string[];
  try {
    names = (await readdir(dir, { withFileTypes: true }))
      .filter((entry) => entry.isFile() && entry.name.endsWith(".txt"))
      .map((entry) => entry.name);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  return Promise.all(names.map(async (name) => {
    const path = join(dir, name);
    const info = await stat(path);
    return { path, size: info.size, createdAt: info.mtime.toISOString() };
  }));
}

export async function listToolArtifacts(root: string, sessionId: string, limit = 12): Promise<ToolArtifactRef[]> {
  const sessionDir = toolArtifactDir(root, sessionId);
  const top = await listTxtFiles(sessionDir);
  let childDirs: string[] = [];
  try {
    childDirs = (await readdir(sessionDir, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory() && entry.name.startsWith("child-"))
      .map((entry) => join(sessionDir, entry.name));
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const nested = (await Promise.all(childDirs.map((dir) => listTxtFiles(dir)))).flat();
  return [...top, ...nested].sort((left, right) => right.createdAt.localeCompare(left.createdAt)).slice(0, Math.max(0, limit));
}
