import type { BrowserPageInfo, RecordedRequest } from "../types";

export type BrowserSessionState = {
  currentUrl: string;
  cookies: unknown[];
  localStorage: Record<string, string>;
  sessionStorage: Record<string, string>;
  requests: RecordedRequest[];
  pages: BrowserPageInfo[];
};
