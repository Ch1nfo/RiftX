export type BrowserConsoleKind = "log" | "warning" | "error" | "pageerror" | "dialog";

export type BrowserConsoleEntry = {
  id: string;
  kind: BrowserConsoleKind;
  text: string;
  location?: string;
  at: number;
};

export type BrowserScope = {
  rules?: string[];
};

export type ElementKind = "button" | "link" | "input" | "textarea" | "select" | "checkbox" | "radio";

export type ElementRef = {
  ref: string;
  kind: ElementKind;
  name: string;
  selector: string;
  type?: string;
  formRef?: string;
};

export type FormRef = {
  ref: string;
  fields: Record<string, string>;
};

export type PageSnapshot = {
  url: string;
  title: string;
  elements: ElementRef[];
  forms: FormRef[];
  visibleText: string;
  text: string;
};

export type RecordedRequest = {
  ref: string;
  pageId: string;
  identity: string;
  method: string;
  url: string;
  resourceType: string;
  requestHeaders: Record<string, string>;
  requestBody?: string;
  status?: number;
  statusText?: string;
  responseHeaders?: Record<string, string>;
  responseBody?: string;
  startedAt: string;
  durationMs?: number;
};

export type BrowserPageInfo = {
  id: string;
  identity: string;
  url: string;
  title: string;
  active: boolean;
};

export type BrowserManagerOptions = {
  scope?: BrowserScope;
  evidenceRoot?: string;
  evidenceSessionId?: string;
  ignoreTlsErrors?: boolean;
};
