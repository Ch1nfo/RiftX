"use client";

import * as Select from "@radix-ui/react-select";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Check, CaretDown, HandPalm, Moon, Sparkle, Sun, Translate, Warning, WarningCircle, X } from "@phosphor-icons/react";
import { useEffect, useState, type ReactNode } from "react";
import type { ApprovalMode } from "@/lib/types";
import { useLanguage } from "@/lib/i18n";

function syncFavicon(theme: "dark" | "light") {
  const href = theme === "light" ? "/riftx-logo-light.png" : "/riftx-logo-dark.png";
  let link = document.querySelector<HTMLLinkElement>('link[data-riftx-favicon="true"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    link.dataset.riftxFavicon = "true";
    document.head.appendChild(link);
  }
  link.href = href;
}

export function RiftxLogo({ decorative = false, className = "" }: { decorative?: boolean; className?: string }) {
  const lightAsset = "/riftx-logo-light-mark.png";
  const darkAsset = "/riftx-logo-dark-mark.png";
  return <span className={`riftx-logo ${className}`} aria-label={decorative ? undefined : "RiftX"} aria-hidden={decorative}><img className="riftx-logo-light" src={lightAsset} alt="" /><img className="riftx-logo-dark" src={darkAsset} alt="" /></span>;
}

export function SelectField({ value, onValueChange, options, placeholder }: { value: string; onValueChange: (value: string) => void; options: Array<{ value: string; label: string }>; placeholder?: string }) {
  return (
    <Select.Root value={value} onValueChange={onValueChange}>
      <Select.Trigger className="select-trigger" aria-label={placeholder}>
        <Select.Value placeholder={placeholder} />
        <Select.Icon><CaretDown size={14} /></Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="select-content" position="popper" sideOffset={6}>
          <Select.Viewport className="select-viewport">
            {options.map((option) => (
              <Select.Item className="select-item" value={option.value} key={option.value}>
                <Select.ItemText>{option.label}</Select.ItemText>
                <Select.ItemIndicator><Check size={14} weight="bold" /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

const approvalModeLabels: Record<ApprovalMode, string> = {
  request: "请求审批",
  auto: "帮我审批",
  full: "完全访问"
};

const approvalModeDescriptions: Record<ApprovalMode, string> = {
  request: "关键操作执行前询问",
  auto: "由 AI 自动评估风险并审批",
  full: "跳过审批并允许完整访问"
};

const approvalModeLabelsEn: Record<ApprovalMode, string> = { request: "Request approval", auto: "Help me approve", full: "Full access" };
const approvalModeDescriptionsEn: Record<ApprovalMode, string> = { request: "Ask before risky actions", auto: "AI evaluates and approves", full: "Skip approval for full access" };

const approvalModeIcons: Record<ApprovalMode, ReactNode> = {
  request: <HandPalm size={14} weight="regular" aria-hidden="true" />,
  auto: <Sparkle size={14} weight="regular" aria-hidden="true" />,
  full: <Warning size={14} weight="regular" aria-hidden="true" />
};

export function ApprovalModeMenu({ value, onValueChange, disabled }: { value: ApprovalMode; onValueChange: (value: ApprovalMode) => void; disabled?: boolean }) {
  const { language, t } = useLanguage();
  const labels = language === "en" ? approvalModeLabelsEn : approvalModeLabels;
  const descriptions = language === "en" ? approvalModeDescriptionsEn : approvalModeDescriptions;
  return (
    <Select.Root value={value} onValueChange={(next) => onValueChange(next as ApprovalMode)} disabled={disabled}>
      <Select.Trigger className={`approval-mode-trigger ${value === "full" ? "full" : ""}`} aria-label={t("approvalMode")}>
        <span className="approval-mode-trigger-value">{approvalModeIcons[value]}<Select.Value>{labels[value]}</Select.Value></span>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="select-content approval-mode-content" position="popper" side="top" sideOffset={8} align="start" alignOffset={-8}>
          <Select.Viewport className="select-viewport">
            {(Object.keys(labels) as ApprovalMode[]).map((mode) => (
              <Select.Item className={`select-item approval-mode-item ${mode === "full" ? "warning" : ""}`} value={mode} key={mode}>
                <span className="approval-mode-item-icon">{approvalModeIcons[mode]}</span>
                <span className="approval-mode-item-copy"><span className="approval-mode-item-title"><Select.ItemText>{labels[mode]}</Select.ItemText></span><span className="approval-mode-item-description">{descriptions[mode]}</span></span>
                <Select.ItemIndicator className="approval-mode-check"><Check size={14} weight="bold" /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

export function ModelMenu({ value, onValueChange, options, disabled }: { value: string; onValueChange: (value: string) => void; options: Array<{ value: string; label: string }>; disabled?: boolean }) {
  const { language } = useLanguage();
  return (
    <Select.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <Select.Trigger className="model-menu-trigger" aria-label={language === "en" ? "Current model" : "当前模型"}>
        <Select.Value />
        <Select.Icon><CaretDown size={12} /></Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content className="select-content model-menu-content" position="popper" sideOffset={6} align="end">
          <Select.Viewport className="select-viewport">
            {options.map((option) => (
              <Select.Item className="select-item" value={option.value} key={option.value}>
                <Select.ItemText>{option.label}</Select.ItemText>
                <Select.ItemIndicator><Check size={14} weight="bold" /></Select.ItemIndicator>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const { t } = useLanguage();

  useEffect(() => {
    const stored = window.localStorage.getItem("riftx-theme");
    const next = stored === "light" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    syncFavicon(next);
  }, []);

  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    window.localStorage.setItem("riftx-theme", next);
    syncFavicon(next);
  };

  return <Tip content={theme === "dark" ? t("switchLight") : t("switchDark")}><button className="icon-button theme-toggle" onClick={toggle} aria-label={theme === "dark" ? t("switchLight") : t("switchDark")}>{theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}</button></Tip>;
}

export function LanguageToggle() {
  const { language, setLanguage, t } = useLanguage();
  const next = language === "zh" ? "en" : "zh";
  return <Tip content={language === "zh" ? t("switchToEnglish") : t("switchToChinese")}><button className="language-toggle" onClick={() => setLanguage(next)} aria-label={language === "zh" ? t("switchToEnglish") : t("switchToChinese")}><Translate size={16} /><span>{language === "zh" ? "中" : "EN"}</span></button></Tip>;
}

function Tip({ children, content }: { children: ReactNode; content: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={180}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
        <Tooltip.Portal><Tooltip.Content className="tooltip-content" sideOffset={8}>{content}<Tooltip.Arrow className="tooltip-arrow" /></Tooltip.Content></Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint ? <span className="field-hint">{hint}</span> : null}</label>;
}

export function ContextRing({ percent, label, detail }: { percent: number | null; label: string; detail: ReactNode }) {
  const safe = percent ?? 0;
  return (
    <Tip content={detail}>
      <span className={`context-ring-wrap ${safe >= 85 ? "danger" : safe >= 65 ? "warn" : ""}`}>
        <span className="context-ring" style={{ "--progress": `${safe}%` } as React.CSSProperties}><span>{label}</span></span>
      </span>
    </Tip>
  );
}

export function ErrorNotice({ message, onDismiss }: { message: string; onDismiss?: () => void }) {
  const { t } = useLanguage();
  return <div className="error-notice"><WarningCircle size={17} weight="fill" /><span>{message}</span>{onDismiss ? <button className="icon-button" onClick={onDismiss} aria-label={t("dismiss")}><X size={15} /></button> : null}</div>;
}
