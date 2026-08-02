from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

from riftx.domain import Engagement, ExecutionStatus, Objective, Run
from riftx.executors import ShellKind
from riftx.persistence import (
    Database,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import ProcessSupervisor, RunnerPaths
from riftx.skills import (
    RegisteredToolArguments,
    RegisteredToolSkill,
    ShellArguments,
    ShellSkill,
    SkillContext,
)
from riftx.tools import ToolRegistry, ToolUnavailableError

FIXTURE = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_tool.py"


def write_tools(path: Path, *, execution_policy: str = "open") -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "execution_policy": execution_policy,
                "shells": {
                    "default": {
                        "linux": "/bin/bash",
                        "macos": "/bin/zsh",
                        "windows": "pwsh.exe",
                    }
                },
                "tools": {
                    "custom": {
                        "command": [sys.executable, str(FIXTURE)],
                        "executor": "process",
                        "capabilities": ["custom_verification"],
                        "version_probe": {"command": [sys.executable, str(FIXTURE), "--version"]},
                        "timeout": 30,
                    }
                },
            },
            sort_keys=False,
        )
    )


async def make_context(
    tmp_path: Path,
    *,
    agent_step_id: str,
    execution_policy: str = "open",
    stdout_excerpt_bytes: int = 16 * 1024,
) -> tuple[Database, ProcessSupervisor, SkillContext]:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'riftx.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-1", name="Skill tests")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-1",
            node_id="node-1",
            objective=Objective(description="Exercise skills"),
            workspace_path=str(tmp_path),
        )
    )
    config_path = tmp_path / "tools.yaml"
    write_tools(config_path, execution_policy=execution_policy)
    tools = ToolRegistry(config_path, node_id="node-1")
    await tools.refresh()
    supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(database.session_factory),
        RunnerPaths(tmp_path / "state"),
        termination_grace_seconds=0.1,
    )
    context = SkillContext(
        run_id="run-1",
        node_id="node-1",
        agent_step_id=agent_step_id,
        cwd=tmp_path,
        supervisor=supervisor,
        tool_registry=tools,
        run_environment={"RIFTX_SKILL_TEST": "1"},
        stdout_excerpt_bytes=stdout_excerpt_bytes,
    )
    return database, supervisor, context


async def test_registered_tool_skill_runs_custom_python_script(tmp_path: Path) -> None:
    database, supervisor, context = await make_context(tmp_path, agent_step_id="step-1")
    skill = RegisteredToolSkill()
    arguments = RegisteredToolArguments(tool_id="custom", args=["hello", "world"])

    first = await skill.execute(context, arguments)
    retried = await skill.execute(context, arguments)

    assert first.status is ExecutionStatus.EXITED
    assert first.exit_code == 0
    assert first.stdout_excerpt == b"args=hello|world\n"
    assert retried.execution_id == first.execution_id
    await supervisor.close()
    await database.dispose()


async def test_registered_tool_skill_bounds_agent_output_but_keeps_full_log(
    tmp_path: Path,
) -> None:
    database, supervisor, context = await make_context(
        tmp_path,
        agent_step_id="step-large",
        stdout_excerpt_bytes=1024,
    )

    result = await RegisteredToolSkill().execute(
        context,
        RegisteredToolArguments(tool_id="custom", args=["--large"]),
    )
    execution = await supervisor.get(result.execution_id)

    assert len(result.stdout_excerpt) == 1024
    size = await asyncio.to_thread(lambda: Path(execution.stdout_path).stat().st_size)
    assert size == 100_000
    await supervisor.close()
    await database.dispose()


async def test_shell_skill_runs_explicit_pipeline(tmp_path: Path) -> None:
    database, supervisor, context = await make_context(tmp_path, agent_step_id="step-shell")

    result = await ShellSkill().execute(
        context,
        ShellArguments(
            script="printf 'alpha\\nbeta\\n' | grep beta",
            shell=ShellKind.BASH,
        ),
    )

    assert result.status is ExecutionStatus.EXITED
    assert result.stdout_excerpt == b"beta\n"
    await supervisor.close()
    await database.dispose()


async def test_shell_skill_respects_registered_only_policy(tmp_path: Path) -> None:
    database, supervisor, context = await make_context(
        tmp_path,
        agent_step_id="step-shell-disabled",
        execution_policy="registered_only",
    )

    with pytest.raises(ToolUnavailableError, match="disabled by execution policy"):
        await ShellSkill().execute(
            context,
            ShellArguments(script="echo blocked", shell=ShellKind.BASH),
        )

    await supervisor.close()
    await database.dispose()


async def test_registered_tool_parses_machine_output_and_falls_back_on_failure(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'adapter.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-adapter", name="Adapter tests")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-adapter",
            node_id="node-1",
            objective=Objective(description="Parse scanner output"),
            workspace_path=str(tmp_path),
        )
    )
    config_path = tmp_path / "adapter-tools.yaml"
    scanner = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_nmap.py"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "nmap": {
                        "command": [sys.executable, str(scanner)],
                        "capabilities": ["port_scan"],
                        "output": {"preferred": "xml"},
                    }
                },
            },
            sort_keys=False,
        )
    )
    registry = ToolRegistry(config_path, node_id="node-1")
    await registry.refresh()
    supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(database.session_factory),
        RunnerPaths(tmp_path / "adapter-state"),
    )
    context = SkillContext(
        run_id="run-1",
        node_id="node-1",
        agent_step_id="step-adapter",
        cwd=tmp_path,
        supervisor=supervisor,
        tool_registry=registry,
    )

    parsed = await RegisteredToolSkill().execute(
        context,
        RegisteredToolArguments(tool_id="nmap"),
    )
    fallback = await RegisteredToolSkill().execute(
        context,
        RegisteredToolArguments(
            tool_id="nmap",
            args=["--invalid"],
            execution_key="invalid-adapter-output",
        ),
    )

    assert parsed.structured["open_port_count"] == 1
    assert parsed.structured["hosts"][0]["ports"][0]["port"] == 80  # type: ignore[index]
    assert fallback.structured == {}
    assert "structured parser fallback" in fallback.summary
    assert fallback.stdout_excerpt == b"not xml\n"
    await supervisor.close()
    await database.dispose()


async def test_port_scan_skill_enforces_scope_and_builds_machine_output_args(
    tmp_path: Path,
) -> None:
    from riftx.domain import Scope
    from riftx.scope import ScopeViolationError
    from riftx.skills import PortScanArguments, PortScanSkill

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'port-scan.db'}")
    await database.create_schema()
    await SQLAlchemyEngagementRepository(database.session_factory).create(
        Engagement(id="engagement-port-scan", name="Port scan tests")
    )
    await SQLAlchemyRunRepository(database.session_factory).create(
        Run(
            id="run-1",
            engagement_id="engagement-port-scan",
            node_id="node-1",
            objective=Objective(description="Run an authorized port scan"),
            workspace_path=str(tmp_path),
        )
    )
    scanner = Path(__file__).parents[2] / "tools" / "fixtures" / "fake_nmap.py"
    config_path = tmp_path / "port-scan-tools.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "nmap": {
                        "command": [sys.executable, str(scanner)],
                        "capabilities": ["port_scan"],
                        "output": {"preferred": "xml"},
                    }
                },
            },
            sort_keys=False,
        )
    )
    registry = ToolRegistry(config_path, node_id="node-1")
    await registry.refresh()
    supervisor = ProcessSupervisor(
        SQLAlchemyExecutionRepository(database.session_factory),
        RunnerPaths(tmp_path / "port-scan-state"),
    )
    context = SkillContext(
        run_id="run-1",
        node_id="node-1",
        agent_step_id="step-port-scan",
        cwd=tmp_path,
        supervisor=supervisor,
        tool_registry=registry,
        scope=Scope(cidrs=["192.0.2.0/24"]),
    )

    result = await PortScanSkill().execute(
        context,
        PortScanArguments(target="192.0.2.10", ports="80", service_detection=True),
    )
    execution = await supervisor.get(result.execution_id)

    assert execution.argv[-6:] == ["-oX", "-", "-sV", "-p", "80", "192.0.2.10"]
    assert result.structured["open_port_count"] == 1
    with pytest.raises(ScopeViolationError, match="outside authorized scope"):
        await PortScanSkill().execute(
            context,
            PortScanArguments(target="203.0.113.10"),
        )
    await supervisor.close()
    await database.dispose()
