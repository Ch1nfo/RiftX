"use client";

import { useEffect, useMemo, useState } from "react";

export type Language = "zh" | "en";
const STORAGE_KEY = "riftx-language";
const EVENT_NAME = "riftx-language-change";

const text = {
  zh: {
    settings: "设置", backToWorkbench: "返回工作台", newSession: "新建会话", recentSessions: "最近会话", loading: "加载中…", noSessions: "暂无会话", archive: "归档会话", archived: "归档会话", deleteArchived: "永久删除", confirmDelete: "确定永久删除“{name}”吗？", close: "关闭", workingDirectory: "当前工作目录", localAgent: "本机 Agent", thinking: "Thinking", thinkingNow: "思考中", thinkingDone: "已完成", running: "运行中", failed: "失败", complete: "完成", loadingWorkspace: "正在加载工作区", readingWorkspace: "正在读取会话和模型配置…", ready: "准备开始", noSession: "暂无会话", readOrTest: "让 RiftX 读取代码、检查配置，或协助你梳理安全问题。", createSessionFirst: "新建会话后即可开始使用 RiftX。", overview: "概览当前目录", checkRisks: "检查安全风险", summarizeTitle: "正在总结任务…", unnamed: "未命名任务", newSessionEnglish: "New session", sendGuide: "发送引导消息", stop: "停止运行", send: "发送消息", loadingModel: "加载模型…", ask: "向 RiftX 描述你要完成的工作…", guide: "输入消息，引导 RiftX 的下一步…", shiftEnter: "换行", contextUnknown: "未知", context: "context", input: "输入", output: "输出", cacheRead: "缓存读", cacheWrite: "写", remaining: "剩余", tokens: "tokens", needConfirm: "需要确认", highRisk: "高风险", browserApproval: "需要在浏览器中执行此操作以继续当前任务。", terminalApproval: "需要在本机终端执行命令以继续当前任务。", expandCommand: "查看完整命令", allowOnce: "允许一次", allowTask: "允许本次任务", reject: "拒绝", cannotConnect: "无法连接到 RiftX 后端", sendFailed: "发送失败", approvalExpired: "审批请求已失效，请重新运行任务", approvalMode: "审批模式", switchLight: "切换亮色主题", switchDark: "切换暗色主题", switchToEnglish: "切换 English", switchToChinese: "切换中文", closeNav: "关闭导航", jumpLatest: "回到最新消息", dismiss: "关闭", saved: "已保存", saveSettings: "保存设置", settingsLoadFailed: "无法读取设置", saveFailed: "保存失败", deleteArchivedFailed: "删除归档会话失败", modelAgent: "模型与 Agent", toolSecurity: "工具安全", config: "配置", workspaceSettings: "WORKSPACE SETTINGS", modelSettingsDesc: "配置 RiftX 的连接方式、上下文窗口和子 Agent 行为。", modelProfiles: "模型配置档案", modelProfilesDesc: "主 Agent 与子 Agent 可分别选择模型。", addProfile: "添加配置", removeProfile: "删除配置", childAgent: "子 Agent", childAgentDesc: "为一次性子任务提供独立模型选择。", inheritMain: "继承主 Agent 模型", inheritMainDesc: "开启后，子 Agent 自动使用当前主模型配置。", independentProfile: "独立配置", workDir: "工作目录", workDirDesc: "RiftX 的 read、grep、find、ls 和受控命令都在此目录运行。", currentDir: "当前目录", highRiskNote: "高风险操作由工作台输入框左侧的审批模式控制。", securityDesc: "查看 RiftX 内置工具的默认权限和当前审批策略。", allowedByDefault: "默认允许", readOnlyTools: "read、grep、find、ls 只读工具", needsApproval: "需要审批", riskyTools: "bash、write、edit 和浏览器修改操作", currentMode: "当前审批模式", modeInComposer: "可在工作台输入框左下角切换", archivedDesc: "归档会话不会出现在工作台列表中，可在这里永久删除。", noArchived: "暂无归档会话", apiKeyHint: "仅保存在本机配置文件。"
  },
  en: {
    settings: "Settings", backToWorkbench: "Back to workbench", newSession: "New session", recentSessions: "Recent sessions", loading: "Loading…", noSessions: "No sessions", archive: "Archive session", archived: "Archived sessions", deleteArchived: "Delete permanently", confirmDelete: "Permanently delete “{name}”?", close: "Close", workingDirectory: "Working directory", localAgent: "Local Agent", thinking: "Thinking", thinkingNow: "Thinking", thinkingDone: "Done", running: "Running", failed: "Failed", complete: "Complete", loadingWorkspace: "Loading workspace", readingWorkspace: "Reading sessions and model configuration…", ready: "Ready to start", noSession: "No session", readOrTest: "Ask RiftX to read code, inspect configuration, or help with a security assessment.", createSessionFirst: "Create a session to start using RiftX.", overview: "Inspect current directory", checkRisks: "Check security risks", summarizeTitle: "Summarizing task…", unnamed: "Untitled task", newSessionEnglish: "New session", sendGuide: "Send guidance", stop: "Stop", send: "Send message", loadingModel: "Loading model…", ask: "Describe what you want RiftX to do…", guide: "Enter a message to guide RiftX…", shiftEnter: "newline", contextUnknown: "Unknown", context: "context", input: "Input", output: "Output", cacheRead: "Cache read", cacheWrite: "write", remaining: "Remaining", tokens: "tokens", needConfirm: "Confirmation required", highRisk: "High risk", browserApproval: "This browser action requires confirmation to continue.", terminalApproval: "This local terminal command requires confirmation to continue.", expandCommand: "View full command", allowOnce: "Allow once", allowTask: "Always allow this exact action for this task", reject: "Reject", cannotConnect: "Could not connect to RiftX backend", sendFailed: "Send failed", approvalExpired: "Approval request expired. Run the task again.", approvalMode: "Approval mode", switchLight: "Switch to light theme", switchDark: "Switch to dark theme", switchToEnglish: "Switch to English", switchToChinese: "Switch to Chinese", closeNav: "Close navigation", jumpLatest: "Jump to latest", dismiss: "Dismiss", saved: "Saved", saveSettings: "Save settings", settingsLoadFailed: "Could not load settings", saveFailed: "Save failed", deleteArchivedFailed: "Could not delete archived session", modelAgent: "Model & Agent", toolSecurity: "Tool security", config: "Configuration", workspaceSettings: "WORKSPACE SETTINGS", modelSettingsDesc: "Configure RiftX connections, context windows, and sub-agent behavior.", modelProfiles: "Model profiles", modelProfilesDesc: "Choose models independently for the main and child agents.", addProfile: "Add profile", removeProfile: "Delete profile", childAgent: "Child Agent", childAgentDesc: "Choose an independent model for one-shot child tasks.", inheritMain: "Inherit main Agent model", inheritMainDesc: "Use the current main model configuration for child agents.", independentProfile: "Independent profile", workDir: "Working directory", workDirDesc: "RiftX read, grep, find, ls, and controlled commands run here.", currentDir: "Current directory", highRiskNote: "High-risk actions are controlled by the approval mode in the workbench composer.", securityDesc: "Review default permissions and the current RiftX approval policy.", allowedByDefault: "Allowed by default", readOnlyTools: "read, grep, find, ls read-only tools", needsApproval: "Approval required", riskyTools: "bash, write, edit, and mutating browser actions", currentMode: "Current approval mode", modeInComposer: "Change it from the lower-left of the workbench composer", archivedDesc: "Archived sessions are hidden from the workbench and can be permanently deleted here.", noArchived: "No archived sessions", apiKeyHint: "Stored only in the local configuration file."
  }
} as const;

export function useLanguage() {
  const [language, setLanguage] = useState<Language>("zh");
  useEffect(() => {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "en" || stored === "zh") {
      setLanguage(stored);
      document.documentElement.lang = stored === "en" ? "en" : "zh-CN";
    }
    const onChange = (event: Event) => {
      const next = (event as CustomEvent<Language>).detail;
      if (next === "en" || next === "zh") {
        setLanguage(next);
        document.documentElement.lang = next === "en" ? "en" : "zh-CN";
      }
    };
    window.addEventListener(EVENT_NAME, onChange);
    return () => window.removeEventListener(EVENT_NAME, onChange);
  }, []);
  const set = (next: Language) => {
    setLanguage(next);
    document.documentElement.lang = next === "en" ? "en" : "zh-CN";
    window.localStorage.setItem(STORAGE_KEY, next);
    window.dispatchEvent(new CustomEvent<Language>(EVENT_NAME, { detail: next }));
  };
  const t = useMemo(() => (key: keyof typeof text.zh, vars?: Record<string, string>) => {
    let value: string = text[language][key];
    for (const [name, replacement] of Object.entries(vars ?? {})) value = value.replace(`{${name}}`, replacement);
    return value;
  }, [language]);
  return { language, setLanguage: set, t };
}
