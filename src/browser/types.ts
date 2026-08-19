export type BrowserAction =
  | "navigate"
  | "snapshot"
  | "click"
  | "fill"
  | "press"
  | "select"
  | "back"
  | "reload"
  | "requests"
  | "request_detail"
  | "response_body"
  | "cookies"
  | "storage"
  | "screenshot"
  | "tabs"
  | "close";

export type BrowserToolInput = {
  action: BrowserAction;
  url?: string;
  ref?: string;
  value?: string;
  key?: string;
  values?: string[];
};

export type BrowserScope = {
  allowedOrigins?: string[];
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
  url: string;
  title: string;
  active: boolean;
};

export type BrowserManagerOptions = {
  cwd: string;
  sessionId: string;
  scope?: BrowserScope;
};
