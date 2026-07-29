"""Local runner directory layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionPaths:
    directory: Path
    stdout: Path
    stderr: Path


@dataclass(frozen=True, slots=True)
class TerminalPaths:
    directory: Path
    transcript: Path


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    directory: Path
    content: Path


class RunnerPaths:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def run_directory(self, run_id: str) -> Path:
        return self.root / "runs" / _safe_component(run_id)

    def execution(self, run_id: str, execution_id: str) -> ExecutionPaths:
        directory = self.run_directory(run_id) / "executions" / _safe_component(execution_id)
        return ExecutionPaths(
            directory=directory,
            stdout=directory / "stdout.log",
            stderr=directory / "stderr.log",
        )

    def ensure_run_layout(self, run_id: str) -> Path:
        run_directory = self.run_directory(run_id)
        for name in (
            "workspace",
            "executions",
            "terminals",
            "artifacts",
            "findings",
            "reports",
        ):
            (run_directory / name).mkdir(parents=True, exist_ok=True)
        return run_directory

    def terminal(self, run_id: str, session_id: str) -> TerminalPaths:
        directory = self.run_directory(run_id) / "terminals" / _safe_component(session_id)
        return TerminalPaths(directory=directory, transcript=directory / "transcript.log")

    def artifact(self, run_id: str, artifact_id: str, name: str) -> ArtifactPaths:
        directory = self.run_directory(run_id) / "artifacts" / _safe_component(artifact_id)
        return ArtifactPaths(directory=directory, content=directory / _safe_component(name))


def _safe_component(value: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value or "\x00" in value:
        raise ValueError(f"unsafe path component: {value!r}")
    return value
