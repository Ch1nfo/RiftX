/**
 * Prompt payload validation and composition for user-sent images and text
 * attachments. Pure functions: shared by the API route (authoritative) and
 * the composer (immediate feedback).
 */

export const MAX_PROMPT_IMAGES = 4;
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
export const IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"] as const;

export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_CHARS = 2 * 1024 * 1024;
export const MAX_ATTACHMENT_TOTAL_CHARS = 4 * 1024 * 1024;
/** Common AI-readable text/code formats. PDF needs a parser and is out of scope. */
export const ATTACHMENT_EXTENSIONS = [
  "txt", "md", "markdown", "log", "json", "jsonl", "ndjson", "csv", "tsv", "xml", "yaml", "yml",
  "html", "htm", "css", "js", "mjs", "cjs", "ts", "tsx", "jsx", "py", "go", "java", "php", "rb",
  "sql", "sh", "bash", "zsh", "env", "conf", "ini", "toml", "properties", "http", "proto", "graphql"
] as const;

export type PromptImage = { data: string; mimeType: string };
export type PromptAttachment = { name: string; content: string };

export function attachmentExtension(name: string) {
  const match = /\.([A-Za-z0-9]+)$/.exec(name.trim());
  return match ? match[1]!.toLowerCase() : "";
}

/** Images are counted in base64 characters; decode overestimate keeps the cap honest without buffering. */
export function promptImagesError(images: unknown): string | null {
  if (images === undefined) return null;
  if (!Array.isArray(images)) return "images must be an array";
  if (images.length > MAX_PROMPT_IMAGES) return `at most ${MAX_PROMPT_IMAGES} images per message`;
  for (const image of images) {
    if (!image || typeof image !== "object") return "each image must be an object";
    const candidate = image as Partial<PromptImage>;
    if (typeof candidate.data !== "string" || !candidate.data) return "each image requires base64 data";
    // Malformed base64 must fail here, not at the provider: charset, padding,
    // decoded size, and format magic bytes are all checkable without buffering
    // anything beyond the image itself.
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(candidate.data) || candidate.data.length % 4 !== 0) return "each image requires valid base64 data";
    const decoded = Buffer.from(candidate.data, "base64");
    if (decoded.length === 0 || decoded.length > MAX_IMAGE_BYTES) return "each image must be at most 8 MB";
    if (typeof candidate.mimeType !== "string" || !(IMAGE_MIME_TYPES as readonly string[]).includes(candidate.mimeType)) {
      return `image type must be one of ${IMAGE_MIME_TYPES.join(", ")}`;
    }
    if (!hasImageMagicBytes(decoded, candidate.mimeType)) return "image data does not match the declared type";
  }
  return null;
}

function hasImageMagicBytes(decoded: Buffer, mimeType: string) {
  if (decoded.length < 12) return false;
  if (mimeType === "image/png") return decoded[0] === 0x89 && decoded[1] === 0x50 && decoded[2] === 0x4e && decoded[3] === 0x47;
  if (mimeType === "image/jpeg") return decoded[0] === 0xff && decoded[1] === 0xd8 && decoded[2] === 0xff;
  if (mimeType === "image/gif") return decoded.toString("ascii", 0, 3) === "GIF";
  // RIFF....WEBP
  return decoded.toString("ascii", 0, 4) === "RIFF" && decoded.toString("ascii", 8, 12) === "WEBP";
}

export function promptAttachmentsError(attachments: unknown): string | null {
  if (attachments === undefined) return null;
  if (!Array.isArray(attachments)) return "attachments must be an array";
  if (attachments.length > MAX_ATTACHMENTS) return `at most ${MAX_ATTACHMENTS} attachments per message`;
  let total = 0;
  for (const attachment of attachments) {
    if (!attachment || typeof attachment !== "object") return "each attachment must be an object";
    const candidate = attachment as Partial<PromptAttachment>;
    if (typeof candidate.name !== "string" || !candidate.name.trim()) return "each attachment requires a name";
    if (candidate.name.length > 200) return "attachment names must be at most 200 characters";
    if (typeof candidate.content !== "string") return "each attachment requires text content";
    // Byte limits must count UTF-8 bytes: CJK text is ~3 bytes per character,
    // so code-unit lengths would silently admit ~3x the advertised cap.
    const bytes = Buffer.byteLength(candidate.content, "utf8");
    if (bytes > MAX_ATTACHMENT_CHARS) return `attachment ${candidate.name} exceeds 2 MB`;
    if (!(ATTACHMENT_EXTENSIONS as readonly string[]).includes(attachmentExtension(candidate.name))) {
      return `attachment type not supported: ${candidate.name} (allowed: ${ATTACHMENT_EXTENSIONS.join(", ")})`;
    }
    total += bytes;
    if (total > MAX_ATTACHMENT_TOTAL_CHARS) return "attachments together exceed 4 MB";
  }
  return null;
}

/** Fenced blocks appended to the prompt text so the model, transcript, and UI all see the same thing. */
export function composeAttachmentText(attachments: readonly PromptAttachment[]): string {
  if (!attachments.length) return "";
  const blocks = attachments.map((attachment) => {
    const language = attachmentExtension(attachment.name) || "";
    const bytes = Buffer.byteLength(attachment.content, "utf8");
    const truncated = bytes > MAX_ATTACHMENT_CHARS;
    const body = truncated ? Buffer.from(attachment.content, "utf8").subarray(0, MAX_ATTACHMENT_CHARS).toString("utf8") : attachment.content;
    // A file containing its own ``` fence must not close the block early:
    // grow the fence past the longest backtick run in the content.
    const longestRun = body.match(/`+/g)?.reduce((max, run) => Math.max(max, run.length), 0) ?? 0;
    const fence = "`".repeat(Math.max(3, longestRun + 1));
    const name = attachment.name.replace(/[\r\n\u0000-\u001f]/g, " ").trim();
    return [
      `--- attachment: ${name} (${attachment.content.length} chars${truncated ? ", truncated" : ""}) ---`,
      fence + language,
      body,
      fence
    ].join("\n");
  });
  return `\n\n${blocks.join("\n\n")}`;
}

/** Per-session composer attachment state, mirroring the session-draft pattern. */
export type SessionAttachments<T = PromptAttachment> = Readonly<Record<string, T[]>>;

export function sessionAttachments<T>(attachments: SessionAttachments<T>, sessionId: string): T[] {
  return sessionId ? attachments[sessionId] ?? [] : [];
}

export function withSessionAttachments<T>(attachments: SessionAttachments<T>, sessionId: string, value: T[]): SessionAttachments<T> {
  if (!sessionId) return attachments;
  if (!value.length) {
    if (!(sessionId in attachments)) return attachments;
    const next = { ...attachments };
    delete next[sessionId];
    return next;
  }
  return { ...attachments, [sessionId]: value };
}

/** Merge a failed send back into the composer without destroying either batch.
 * A newer selection with the same kind/name wins, but overflow remains visible
 * for the user to remove deliberately instead of being silently discarded. */
export function mergeRecoveredAttachments<T extends { kind: string; name: string }>(existing: readonly T[], restored: readonly T[]): T[] {
  const merged = [...existing];
  for (const attachment of restored) {
    if (!merged.some((item) => item.kind === attachment.kind && item.name === attachment.name)) merged.push(attachment);
  }
  return merged;
}
