/** Shared builder for the id-based screenshot serving route (findings panel + chat). */
export function screenshotUrl(sessionId: string, screenshotId: string) {
  return `/api/sessions/${sessionId}/findings/screenshot/${encodeURIComponent(screenshotId)}`;
}
