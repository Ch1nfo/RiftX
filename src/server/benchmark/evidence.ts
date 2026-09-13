import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { mkdir, open, realpath, rename, rm, stat } from "node:fs/promises";
import { extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import type { BrowserManager } from "@/browser";

const MAX_REFERENCE_CHARS = 500;
const REQUEST_REF = /^r-[a-f0-9-]{36}-\d+$/;
const SCREENSHOT_REF = /^s-[a-f0-9-]{36}$/;

export type BenchmarkEvidenceContext = {
  /** A durable directory owned by this benchmark challenge, outside rolling artifacts. */
  directory: string;
  cwd: string;
  allowedRoots: readonly string[];
  evidenceDirectory?: string;
  browser?: Pick<BrowserManager, "requestEvidence" | "screenshotEvidence">;
  resolveToolEvidence?: (toolCallId: string) => { toolName: string; content: string; artifactPath?: string } | undefined;
};

function checkedReference(reference: string) {
  if (reference.length > MAX_REFERENCE_CHARS) throw new Error(`Evidence reference exceeds ${MAX_REFERENCE_CHARS} characters; use a shorter task-local path.`);
  return reference;
}

function inside(root: string, path: string) {
  const part = relative(root, path);
  return !part || (part !== ".." && !part.startsWith(`..${sep}`) && !isAbsolute(part));
}

async function scopedPath(path: string, context: BenchmarkEvidenceContext) {
  const roots = await Promise.all([...context.allowedRoots, context.directory].map(async (root) => {
    try { return await realpath(root); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
  }));
  const canonical = await realpath(resolve(context.cwd, path));
  if (!roots.some((root) => root && inside(root, canonical))) throw new Error("Evidence files must be inside this task's workspace, artifacts, or evidence directories.");
  return canonical;
}

/** Hash the bytes actually written, so reuse never substitutes a later file version. */
async function saveSnapshot(chunks: AsyncIterable<Uint8Array> | Iterable<Uint8Array>, extension: string, context: BenchmarkEvidenceContext) {
  await mkdir(context.directory, { recursive: true, mode: 0o700 });
  const directory = await realpath(context.directory);
  checkedReference(join(directory, `${"0".repeat(64)}${extension}`));
  const temporary = join(directory, `.evidence-${randomUUID()}.tmp`);
  const file = await open(temporary, "wx", 0o600);
  try {
    const hash = createHash("sha256");
    for await (const chunk of chunks) {
      hash.update(chunk);
      await file.writeFile(chunk);
    }
    await file.sync();
    await file.close();
    const destination = checkedReference(join(directory, `${hash.digest("hex")}${extension}`));
    await rename(temporary, destination);
    return destination;
  } finally {
    await file.close().catch(() => undefined);
    await rm(temporary, { force: true });
  }
}

async function pinFile(path: string, context: BenchmarkEvidenceContext) {
  const canonical = await scopedPath(path, context);
  // Refuse a leaf symlink swapped in after the scope check, and never read a device or pipe.
  const source = await open(canonical, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    if (!(await source.stat()).isFile()) throw new Error("Evidence references must identify regular files.");
    const extension = /^\.[a-zA-Z0-9]{1,12}$/.test(extname(canonical)) ? extname(canonical).toLowerCase() : ".bin";
    return await saveSnapshot(source.createReadStream({ autoClose: false }), extension, context);
  } finally {
    await source.close();
  }
}

/** Resolve temporary references before checkpointing, then keep an independent durable snapshot. */
export async function persistBenchmarkEvidenceRef(reference: string | undefined, context: BenchmarkEvidenceContext): Promise<string> {
  const ref = checkedReference(reference?.trim() ?? "");
  if (!ref) return "";

  if (/^request:/i.test(ref) || /^r-/.test(ref)) {
    const requestRef = ref.replace(/^request:/i, "");
    if (!REQUEST_REF.test(requestRef) || !context.browser) throw new Error("Request evidence must identify an available captured request.");
    const evidence = await context.browser.requestEvidence(requestRef);
    if (!evidence.artifactPath) throw new Error("Request evidence could not be persisted.");
    return pinFile(evidence.artifactPath, context);
  }

  if (/^screenshot:/i.test(ref) || /^s-/.test(ref)) {
    let screenshotRef = ref.replace(/^screenshot:/i, "");
    if (screenshotRef === "latest" && context.browser) screenshotRef = (await context.browser.screenshotEvidence("latest")).screenshotId;
    if (!SCREENSHOT_REF.test(screenshotRef) || !context.evidenceDirectory) throw new Error("Screenshot evidence must identify an available saved screenshot.");
    return pinFile(join(context.evidenceDirectory, "shots", `${screenshotRef}.png`), context);
  }

  if (/^(?:tool|toolCall|toolCallId):/i.test(ref) || /^(?:call_|toolu_)/.test(ref)) {
    const toolCallId = ref.replace(/^(?:tool|toolCall|toolCallId):/i, "");
    const evidence = context.resolveToolEvidence?.(toolCallId);
    if (!evidence?.content) throw new Error("Tool evidence is unavailable; checkpoint a saved task-local artifact instead.");
    const artifactPath = evidence.artifactPath ? await pinFile(evidence.artifactPath, context) : undefined;
    return saveSnapshot([Buffer.from(JSON.stringify({ toolCallId, ...evidence, artifactPath }))], ".json", context);
  }

  if (/^(?:element|dom|page|tab):/i.test(ref) || /^e\d+$/.test(ref)) throw new Error("Live browser references cannot be used as durable evidence; save a request, screenshot, or task-local artifact.");
  if (/^(?:platform|flag|https?):/i.test(ref)) return ref;
  if (/^artifact:/i.test(ref)) return pinFile(ref.replace(/^artifact:/i, ""), context);
  if (/^file:/i.test(ref)) return pinFile(/^file:\/\//i.test(ref) ? fileURLToPath(ref) : ref.replace(/^file:/i, ""), context);
  if (isAbsolute(ref) || ref.startsWith(".") || ref.includes("/") || /^\S+\.[a-zA-Z0-9]{1,12}$/.test(ref)) return pinFile(ref, context);

  // A bare existing filename is valid too; ordinary descriptive references remain notes.
  try {
    await stat(resolve(context.cwd, ref));
    return pinFile(ref, context);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  return ref;
}
