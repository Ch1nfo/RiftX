export type RiftxErrorCode = "SESSION_ARCHIVED" | "SESSION_NOT_IN_WORKSPACE" | "SESSION_NOT_FOUND" | "SESSION_NOT_ARCHIVED" | "SESSION_BUSY" | "MODEL_AUTH_MISSING" | "INVALID_WORKING_DIRECTORY" | "MODEL_DOES_NOT_SUPPORT_IMAGES" | "DUPLICATE_REQUEST_ID";

export class RiftxError extends Error {
  constructor(message: string, readonly code: RiftxErrorCode, readonly status: number) {
    super(message);
    this.name = "RiftxError";
  }
}

function errorStatus(error: unknown, fallback: number) {
  return error instanceof RiftxError ? error.status : fallback;
}

export function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

/** Uniform route-handler error envelope: RiftxError keeps its status/message/code, anything else falls back. */
export function errorResponse(error: unknown, fallbackMessage: string, fallbackStatus = 500) {
  const payload = error instanceof RiftxError
    ? { error: error.message, code: error.code }
    : { error: errorMessage(error, fallbackMessage) };
  return Response.json(payload, { status: errorStatus(error, fallbackStatus) });
}
