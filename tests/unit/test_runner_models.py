from pathlib import Path

import pytest
from pydantic import ValidationError

from riftx.domain import ExecutorType
from riftx.executors import ShellKind
from riftx.runner import ExecutionLaunchRequest, RunnerPaths


def test_process_launch_requires_argv(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires non-empty argv"):
        ExecutionLaunchRequest(
            execution_key="key",
            run_id="run",
            node_id="node",
            executor_type=ExecutorType.PROCESS,
            cwd=tmp_path,
        )


def test_shell_launch_requires_script_and_shell(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires command_text and shell"):
        ExecutionLaunchRequest(
            execution_key="key",
            run_id="run",
            node_id="node",
            executor_type=ExecutorType.SHELL,
            cwd=tmp_path,
            command_text="echo ok",
        )

    request = ExecutionLaunchRequest(
        execution_key="key",
        run_id="run",
        node_id="node",
        executor_type=ExecutorType.SHELL,
        cwd=tmp_path,
        command_text="echo ok",
        shell=ShellKind.BASH,
    )
    assert request.shell is ShellKind.BASH


def test_runner_paths_create_expected_layout(tmp_path: Path) -> None:
    paths = RunnerPaths(tmp_path / "state")
    run_directory = paths.ensure_run_layout("run-1")
    execution = paths.execution("run-1", "execution-1")

    assert (run_directory / "workspace").is_dir()
    assert (run_directory / "reports").is_dir()
    assert execution.stdout == (run_directory / "executions" / "execution-1" / "stdout.log")


def test_runner_paths_reject_traversal(tmp_path: Path) -> None:
    paths = RunnerPaths(tmp_path)
    with pytest.raises(ValueError, match="unsafe path"):
        paths.run_directory("../escape")


def test_output_slice_json_uses_base64() -> None:
    from riftx.runner import OutputSlice

    output = OutputSlice(data=b"\xff\x00", cursor=0, next_cursor=2, eof=True)
    assert '"data":"_wA="' in output.model_dump_json()
