import { randomUUID } from "node:crypto";
import { readFile, rename, rm, writeFile } from "node:fs/promises";

/**
 * Shared JSON persistence for RiftX state files.
 *
 * Reads treat only ENOENT as "no data yet". A file that fails to parse is
 * moved aside as `<path>.corrupt-<id>` (keeping the broken bytes for manual
 * recovery) and reported as no data; if that backup rename itself fails, the
 * error surfaces so the original bytes are never treated as disposable. Any
 * other I/O error is surfaced to the caller so a permission problem is never
 * mistaken for empty state.
 *
 * Writes go to a same-directory temporary file followed by an atomic rename;
 * if any step fails the temporary file is removed again, so no `.tmp-*`
 * leftovers remain.
 */

export async function readJsonStore<T>(filePath: string): Promise<T | undefined> {
  let text: string;
  try {
    text = await readFile(filePath, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
    throw error;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    const backup = `${filePath}.corrupt-${randomUUID().slice(0, 8)}`;
    await rename(filePath, backup);
    return undefined;
  }
}

export async function writeJsonStoreAtomic(filePath: string, data: unknown, mode = 0o600) {
  const temporary = `${filePath}.tmp-${process.pid}-${randomUUID()}`;
  try {
    await writeFile(temporary, `${JSON.stringify(data, null, 2)}\n`, { mode });
    await rename(temporary, filePath);
  } catch (error) {
    // Clean the leftover temporary file. Cleanup failures are logged (not
    // silently swallowed) but never mask the original write error.
    await rm(temporary, { force: true }).catch((cleanupError) => {
      console.error(`RiftX failed to clean temporary store ${temporary}:`, cleanupError);
    });
    throw error;
  }
}
