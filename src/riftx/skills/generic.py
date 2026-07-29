"""Generic skills that execute registered tools or explicit shell scripts."""

from __future__ import annotations

import platform
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ApprovalLevel, Execution, ExecutorType
from riftx.executors import ShellKind
from riftx.runner import ExecutionLaunchRequest
from riftx.tools import ExecutionPolicy, ToolUnavailableError

from .base import BaseSkill, SkillContext, SkillResult


class RegisteredToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    environment: dict[str, str | None] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    execution_key: str | None = None


class ShellArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: str = Field(min_length=1)
    shell: ShellKind | None = None
    environment: dict[str, str | None] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    execution_key: str | None = None


class RegisteredToolSkill(BaseSkill):
    id = "run_registered_tool"
    description = "Run an available tool declared in tools.yaml"
    approval_level = ApprovalLevel.NEVER
    arguments_model = RegisteredToolArguments

    async def execute(self, context: SkillContext, arguments: BaseModel) -> SkillResult:
        parsed = RegisteredToolArguments.model_validate(arguments)
        definition = context.tool_registry.get_available(parsed.tool_id)
        if definition.executor is ExecutorType.PTY:
            raise ToolUnavailableError(f"tool {definition.id!r} requires the terminal subsystem")

        argv = context.tool_registry.build_argv(definition.id, parsed.args)
        environment = _merge_environment_diffs(
            context.node_environment,
            definition.environment,
            context.run_environment,
            parsed.environment,
        )
        timeout = parsed.timeout_seconds or definition.timeout_seconds
        execution_key = parsed.execution_key or _default_execution_key(
            context.run_id, context.agent_step_id, self.id, definition.id
        )

        if definition.executor is ExecutorType.PROCESS:
            request = ExecutionLaunchRequest(
                execution_key=execution_key,
                run_id=context.run_id,
                node_id=context.node_id,
                executor_type=ExecutorType.PROCESS,
                cwd=context.cwd,
                argv=argv,
                env=environment,
                timeout_seconds=timeout,
            )
        else:
            shell, shell_path = _default_shell(context)
            request = ExecutionLaunchRequest(
                execution_key=execution_key,
                run_id=context.run_id,
                node_id=context.node_id,
                executor_type=ExecutorType.SHELL,
                cwd=context.cwd,
                command_text=_join_shell_words(argv),
                shell=shell,
                shell_path=shell_path,
                env=environment,
                timeout_seconds=timeout,
            )
        execution = await context.supervisor.start(request)
        execution = await context.supervisor.wait(execution.id)
        return await _skill_result(context, execution, definition.id)


class ShellSkill(BaseSkill):
    id = "run_shell"
    description = "Run an explicit shell script when execution policy permits"
    approval_level = ApprovalLevel.SENSITIVE
    arguments_model = ShellArguments

    async def execute(self, context: SkillContext, arguments: BaseModel) -> SkillResult:
        parsed = ShellArguments.model_validate(arguments)
        if context.tool_registry.config.execution_policy is ExecutionPolicy.REGISTERED_ONLY:
            raise ToolUnavailableError("raw shell execution is disabled by execution policy")
        default_shell, default_shell_path = _default_shell(context)
        shell = parsed.shell or default_shell
        shell_path = (
            default_shell_path if parsed.shell is None else _configured_shell_path(context, shell)
        )
        environment = _merge_environment_diffs(
            context.node_environment,
            context.run_environment,
            parsed.environment,
        )
        execution_key = parsed.execution_key or _default_execution_key(
            context.run_id, context.agent_step_id, self.id, shell.value
        )
        execution = await context.supervisor.start(
            ExecutionLaunchRequest(
                execution_key=execution_key,
                run_id=context.run_id,
                node_id=context.node_id,
                executor_type=ExecutorType.SHELL,
                cwd=context.cwd,
                command_text=parsed.script,
                shell=shell,
                shell_path=shell_path,
                env=environment,
                timeout_seconds=parsed.timeout_seconds,
            )
        )
        execution = await context.supervisor.wait(execution.id)
        return await _skill_result(context, execution, "shell")


async def _skill_result(context: SkillContext, execution: Execution, label: str) -> SkillResult:
    output = await context.supervisor.read_output(
        execution.id,
        max_bytes=max(context.stdout_excerpt_bytes, context.stderr_excerpt_bytes),
    )
    stdout = output.stdout.data[: context.stdout_excerpt_bytes]
    stderr = output.stderr.data[: context.stderr_excerpt_bytes]
    summary = (
        f"{label} finished with status={execution.status.value} exit_code={execution.exit_code}"
    )
    return SkillResult(
        summary=summary,
        stdout_excerpt=stdout,
        stderr_excerpt=stderr,
        execution_id=execution.id,
        status=execution.status,
        exit_code=execution.exit_code,
    )


def _default_execution_key(
    run_id: str, agent_step_id: str, skill_id: str, discriminator: str
) -> str:
    return f"{run_id}:{agent_step_id}:{skill_id}:{discriminator}"


def _default_shell(context: SkillContext) -> tuple[ShellKind, Path]:
    system = platform.system().lower()
    if system == "windows":
        shell = ShellKind.POWERSHELL
    elif system == "darwin":
        shell = ShellKind.ZSH
    else:
        shell = ShellKind.BASH
    return shell, _configured_shell_path(context, shell)


def _configured_shell_path(context: SkillContext, shell: ShellKind) -> Path:
    defaults = context.tool_registry.config.shells.default
    if shell is ShellKind.BASH:
        return Path(defaults.linux)
    if shell is ShellKind.ZSH:
        return Path(defaults.macos)
    if shell is ShellKind.POWERSHELL:
        return Path(defaults.windows)
    return Path("cmd.exe")


def _merge_environment_diffs(
    *layers: dict[str, str | None],
) -> dict[str, str | None]:
    merged: dict[str, str | None] = {}
    for layer in layers:
        merged.update(layer)
    return merged


def _join_shell_words(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)
