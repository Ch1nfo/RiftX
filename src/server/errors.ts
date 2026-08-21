export type RiftxErrorCode = "SESSION_ARCHIVED" | "SESSION_NOT_IN_WORKSPACE" | "SESSION_NOT_FOUND" | "SESSION_NOT_ARCHIVED" | "INVALID_WORKING_DIRECTORY";

export class RiftxError extends Error {
  constructor(message: string, readonly code: RiftxErrorCode, readonly status: number) {
    super(message);
    this.name = "RiftxError";
  }
}

export function errorStatus(error: unknown, fallback: number) {
  return error instanceof RiftxError ? error.status : fallback;
}

export function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}
