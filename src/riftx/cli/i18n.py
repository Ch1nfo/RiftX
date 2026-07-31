"""Small dependency-free localization layer for CLI presentation text."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Literal

Language = Literal["en", "zh"]

_language: ContextVar[Language] = ContextVar("riftx_cli_language", default="en")

_ZH: dict[str, str] = {
    "Language for CLI output (en or zh).": "CLI 输出语言（en 或 zh）。",
    "No execution nodes found.": "未找到执行节点。",
    "Execution Nodes": "执行节点",
    "Execution Node": "执行节点",
    "Name": "名称",
    "Status": "状态",
    "Platform": "平台",
    "Runner": "Runner",
    "Capabilities": "能力",
    "Last seen": "最后在线时间",
    "Labels": "标签",
    "Context Inspector": "上下文检查器",
    "Category": "类别",
    "Items": "项目数",
    "Characters": "字符数",
    "Estimated tokens": "估算 Token",
    "Runtime Contract": "运行时契约",
    "Stable Instructions": "稳定指令",
    "Run Contract": "任务契约",
    "Working Memory": "工作记忆",
    "Conversation": "对话",
    "Tool Results": "工具结果",
    "Retrieved Memory": "检索记忆",
    "Subagent Results": "子 Agent 结果",
    "Tool Schemas": "工具 Schema",
    "Model": "模型",
    "Requires API key": "需要 API 密钥",
    "Timeout (seconds)": "超时（秒）",
    "Max retries": "最大重试次数",
    "Estimated input": "估算输入",
    "Actual input/output": "实际输入/输出",
    "Compilation": "编译记录",
    "RiftX Runs": "RiftX 任务",
    "Objective": "目标",
    "Node": "节点",
    "Created": "创建时间",
    "No runs found.": "未找到任务。",
    "RiftX Run": "RiftX 任务",
    "Approval": "审批",
    "Workspace": "工作区",
    "Workflow": "工作流",
    "No long-term memories found.": "未找到长期记忆。",
    "Long-Term Memory": "长期记忆",
    "Type": "类型",
    "Scope": "范围",
    "Pin": "置顶",
    "Pinned": "已置顶",
    "Summary": "摘要",
    "Title": "标题",
    "Content": "内容",
    "Sources": "来源",
    "yes": "是",
    "no": "否",
    "No executions found.": "未找到执行记录。",
    "Executions": "执行记录",
    "Execution": "执行记录",
    "Session": "会话",
    "Tool Call": "工具调用",
    "Attempt": "尝试组",
    "Command": "命令",
    "Exit code": "退出码",
    "Execution key": "执行键",
    "Wait status": "等待状态",
    "Execution status": "执行状态",
    "Next poll": "下次轮询",
    "Output": "输出",
    "Execution Wait": "等待执行",
    "Tools on {node} (generation {generation})": "节点 {node} 上的工具（代次 {generation}）",
    "Tool": "工具",
    "Availability": "可用性",
    "Version": "版本",
    "Executor": "执行器",
    "Terminal": "终端",
    "Owner": "所有者",
    "Working dir": "工作目录",
    "Size": "大小",
    "No approvals found.": "未找到审批请求。",
    "Target": "目标",
    "Environment": "环境变量",
    "Reason": "原因",
    "Decided by": "决定人",
    "No artifacts found.": "未找到制品。",
    "Run Artifacts": "任务制品",
    "Artifact": "制品",
    "Description": "描述",
    "No reports found.": "未找到报告。",
    "Run Reports": "任务报告",
    "Report": "报告",
    "Format": "格式",
    "Findings": "发现项",
    "RiftX API error": "RiftX API 错误",
    "Error": "错误",
    "Safety stop disposition": "安全停止处置明细",
    "Resource type": "资源类型",
    "Resource ID": "资源 ID",
    "Browser session": "浏览器会话",
    "Target HTTP request": "目标 HTTP 请求",
    "Stop result": "停止结果",
    "Stop confirmed": "已确认停止",
    "Stopped ({status})": "已停止（{status}）",
    "Stop unconfirmed": "未确认停止",
    "Unknown node": "未知节点",
    "Runtime Metrics": "运行时指标",
    "Metric": "指标",
    "Value": "值",
    "Counts": "计数",
    "Direction": "方向",
    "Generated": "生成时间",
    "Task Completion Rate": "任务完成率",
    "Repeated Tool Call Rate": "重复工具调用率",
    "Invalid Tool Call Rate": "无效工具调用率",
    "Recovery Success Rate": "恢复成功率",
    "Execution Duplication Rate": "执行重复率",
    "Compaction Fidelity": "压缩保真度",
    "Context Token Efficiency": "上下文 Token 效率",
    "Subagent Utility": "子 Agent 有效性",
    "Approval Resume Success Rate": "审批恢复成功率",
    "Browser Action Failure Rate": "浏览器操作失败率",
    "Citation Coverage": "引用覆盖率",
    "running": "运行中",
    "completed": "已完成",
    "waiting_approval": "等待审批",
    "paused": "已暂停",
    "failed": "失败",
    "cancelled": "已取消",
    "queued": "排队中",
    "starting": "启动中",
    "created": "已创建",
    "hard_timeout": "硬超时",
    "preparing": "准备中",
    "online": "在线",
    "degraded": "降级",
    "offline": "离线",
    "lost": "失联",
    "pending": "待处理",
    "approved": "已批准",
    "rejected": "已拒绝",
    "available": "可用",
    "unavailable": "不可用",
    "misconfigured": "配置错误",
    "disabled": "已禁用",
    "unknown": "未知",
    "default": "默认",
    "RiftX Interactive": "RiftX 交互模式",
    "Type an objective to create a Run, or use /help.": (
        "输入目标以创建任务，或使用 /help 查看帮助。"
    ),
    "Session closed.": "会话已关闭。",
    "Use /exit to leave RiftX.": "使用 /exit 退出 RiftX。",
    "New runs will use node {node}.": "新任务将使用节点 {node}。",
    "Model for new runs: {model}": "新任务使用的模型：{model}",
    "New runs will use model profile {model}.": "新任务将使用模型配置 {model}。",
    "Approval mode for new runs: {mode}": "新任务的审批模式：{mode}",
    "New runs will use {mode} approval mode.": "新任务将使用 {mode} 审批模式。",
    "The Agent has not published a plan yet.": "Agent 尚未发布计划。",
    "Latest plan": "最新计划",
    "Pause confirmed; active effects stopped.": "已确认暂停；活动效果均已停止。",
    "Resume requested.": "已请求恢复。",
    "Run cancellation confirmed; active effects stopped.": (
        "已确认取消任务；活动效果均已停止。"
    ),
    "Context compaction requested.": "已请求压缩上下文。",
    "Approval saved and workflow signaled.": "审批已保存，并已通知工作流。",
    "Approval rejected and workflow signaled.": "审批已拒绝，并已通知工作流。",
    "Message queued.": "消息已加入队列。",
    (
        "Message delivery was not confirmed. Resend the exact same text to retry safely; "
        "RiftX will reuse message_event_id={message_event_id}."
    ): (
        "消息投递尚未确认。请重新发送完全相同的文本以安全重试；"
        "RiftX 将复用 message_event_id={message_event_id}。"
    ),
    (
        "Run created. The objective and boundaries are saved; the Agent is "
        "waiting for your first concrete instruction."
    ): "任务已创建，目标与边界已保存；Agent 正在等待你的第一条具体指令。",
    "No model or Tool will run before that instruction is sent.": (
        "在收到该指令前，不会调用模型或工具。"
    ),
    'Send it with: riftx run message {run_id} "YOUR INSTRUCTION"': (
        '发送方式：riftx run message {run_id} "你的具体指令"'
    ),
    "Open the conversation: {url}": "打开对话：{url}",
    "Streaming events for {run_id}; press Ctrl+C to stop.": (
        "正在流式显示任务 {run_id} 的事件；按 Ctrl+C 停止。"
    ),
    "Stopped watching.": "已停止监听。",
    "No active run; use /new OBJECTIVE or /resume RUN_ID": (
        "没有活动任务；请使用 /new OBJECTIVE 或 /resume RUN_ID"
    ),
    "No active terminal; use /terminal or /attach SESSION_ID": (
        "没有活动终端；请使用 /terminal 或 /attach SESSION_ID"
    ),
    "Attaching to {session_id}; press Ctrl+] to detach.": "正在连接 {session_id}；按 Ctrl+] 分离。",
    "Terminal error": "终端错误",
    "terminal attach requires an interactive TTY; use --read-only otherwise": (
        "连接终端需要交互式 TTY；否则请使用 --read-only"
    ),
    "unsupported terminal control action {action}": "不支持的终端控制操作 {action}",
    "terminal control failed": "终端控制失败",
    "interactive terminal attach currently requires a Unix TTY": (
        "交互式终端连接当前需要 Unix TTY"
    ),
    "Current execution stop confirmed.": "已确认当前执行停止。",
    "Model switch to {model} requested.": "已请求切换模型为 {model}。",
    "Memory deleted.": "记忆已删除。",
    "Memory pin updated.": "记忆置顶状态已更新。",
}


def normalize_language(value: str | None) -> Language:
    """Normalize command-line and environment language aliases."""

    normalized = (value or "en").strip().lower().replace("_", "-")
    if normalized in {"en", "en-us", "en-gb", "english"}:
        return "en"
    if normalized in {"zh", "zh-cn", "zh-hans", "chinese", "中文"}:
        return "zh"
    raise ValueError(f"unsupported language {value!r}; expected 'en' or 'zh'")


def set_language(language: Language) -> None:
    """Set the output language for the current CLI context."""

    _language.set(language)


def get_language() -> Language:
    """Return the current CLI output language."""

    return _language.get()


def tr(message: str, /, **values: object) -> str:
    """Translate a presentation string and interpolate named values."""

    template = _ZH.get(message, message) if get_language() == "zh" else message
    return template.format_map(values) if values else template
