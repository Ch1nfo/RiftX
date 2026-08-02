import { createContext, useContext, useMemo, type ReactNode } from "react";

export type Locale = "en" | "zh-CN";

const STORAGE_KEY = "riftx-demo-locale";

export function translate(locale: Locale, english: string, chinese: string) {
  return locale === "zh-CN" ? chinese : english;
}

export function resolveInitialLocale(): Locale {
  const requested = new URLSearchParams(window.location.search).get("lang");
  if (requested === "en" || requested === "zh-CN") return requested;

  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "en" || stored === "zh-CN") return stored;

  return "en";
}

export function persistLocale(locale: Locale) {
  document.documentElement.lang = locale;
  window.localStorage.setItem(STORAGE_KEY, locale);

  const url = new URL(window.location.href);
  url.searchParams.set("lang", locale);
  window.history.replaceState(window.history.state, "", url);
}

const labels: Record<string, readonly [english: string, chinese: string]> = {
  running: ["Running", "运行中"],
  waiting_approval: ["Awaiting approval", "等待批准"],
  waiting_user: ["Awaiting instruction", "等待指令"],
  paused: ["Paused", "已暂停"],
  completed: ["Completed", "已完成"],
  cancelled: ["Cancelled", "已取消"],
  pending: ["Pending", "待处理"],
  approved: ["Approved", "已批准"],
  rejected: ["Rejected", "已拒绝"],
  read: ["Read only", "只读"],
  approval: ["Approval required", "需要审批"],
  blocked: ["Blocked", "已阻止"],
  confirmed: ["Confirmed", "已确认"],
  active: ["Active", "活动"],
  queued: ["Queued", "已排队"],
  ready: ["Ready", "就绪"],
  draft: ["Draft", "草稿"],
  closed: ["Closed", "已关闭"],
  verified: ["Verified", "已验证"],
  online: ["Online", "在线"],
  offline: ["Offline", "离线"],
  available: ["Available", "可用"],
  unavailable: ["Unavailable", "不可用"],
  balanced: ["Balanced", "平衡"],
  manual: ["Manual", "手动"],
  auto: ["Automatic", "自动"],
  configured: ["Configured", "已配置"],
  "not required": ["Not required", "无需凭据"],
  observing: ["Observing", "观察中"],
  connected: ["Connected", "已连接"],
  attached: ["Attached", "已挂接"],
  agent: ["Agent", "Agent"],
  operator: ["Operator", "Operator"],
  objective: ["Objective", "目标"],
  task: ["Task", "任务"],
  asset: ["Asset", "资产"],
  evidence: ["Evidence", "证据"],
  finding: ["Finding", "发现"],
};

export function displayLabel(locale: Locale, value: string) {
  const pair = labels[value];
  return pair ? translate(locale, pair[0], pair[1]) : value;
}

interface LocaleContextValue {
  locale: Locale;
  t: (english: string, chinese: string) => string;
  label: (value: string) => string;
}

const LocaleContext = createContext<LocaleContextValue | null>(null);

export function LocaleProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  const value = useMemo<LocaleContextValue>(
    () => ({
      locale,
      t: (english, chinese) => translate(locale, english, chinese),
      label: (item) => displayLabel(locale, item),
    }),
    [locale],
  );

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale() {
  const value = useContext(LocaleContext);
  if (!value) throw new Error("useLocale must be used inside LocaleProvider");
  return value;
}
