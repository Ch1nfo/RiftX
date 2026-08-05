"""Local runner directory layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


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
    storage_key: str


class RunnerPaths:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def run_directory(self, run_id: str) -> Path:
        return self.root / "runs" / _safe_component(run_id, max_length=64)

    def execution(self, run_id: str, execution_id: str) -> ExecutionPaths:
        directory = self.run_directory(run_id) / "executions" / _safe_component(
            execution_id,
            max_length=64,
        )
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
            "browsers",
            "artifacts",
            "findings",
            "reports",
        ):
            (run_directory / name).mkdir(parents=True, exist_ok=True)
        return run_directory

    def terminal(self, run_id: str, session_id: str) -> TerminalPaths:
        directory = self.run_directory(run_id) / "terminals" / _safe_component(
            session_id,
            max_length=64,
        )
        return TerminalPaths(directory=directory, transcript=directory / "transcript.log")

    def browser_profile(self, profile_id: str) -> Path:
        return self.root / "browser-profiles" / _safe_component(profile_id, max_length=64)

    def browser_downloads(self, run_id: str, session_id: str) -> Path:
        return (
            self.run_directory(run_id)
            / "browsers"
            / _safe_component(session_id, max_length=64)
            / "downloads"
        )

    def artifact(self, run_id: str, artifact_id: str, name: str) -> ArtifactPaths:
        storage_key = self.artifact_storage_key(run_id, artifact_id, name)
        content = self.artifact_from_storage_key(storage_key)
        return ArtifactPaths(
            directory=content.parent,
            content=content,
            storage_key=storage_key,
        )

    def artifact_storage_key(self, run_id: str, artifact_id: str, name: str) -> str:
        """Return the canonical, root-relative key for one immutable Artifact."""

        return PurePosixPath(
            "runs",
            _safe_component(run_id, max_length=64),
            "artifacts",
            _safe_component(artifact_id, max_length=64),
            _safe_component(name, max_length=255),
        ).as_posix()

    def artifact_from_storage_key(self, storage_key: str) -> Path:
        """Map a validated relative Artifact key into the private Runner root."""

        key = PurePosixPath(storage_key)
        if (
            not storage_key
            or key.is_absolute()
            or key.as_posix() != storage_key
            or len(key.parts) != 5
            or key.parts[0] != "runs"
            or key.parts[2] != "artifacts"
        ):
            raise ValueError("invalid Artifact storage key")
        _safe_component(key.parts[1], max_length=64)
        _safe_component(key.parts[3], max_length=64)
        _safe_component(key.parts[4], max_length=255)
        return self.root.joinpath(*key.parts)

    def command_output(self, command_id: str) -> Path:
        return (
            self.root
            / "command-output"
            / _safe_component(command_id, max_length=64)
            / "response.bin"
        )


def _safe_component(value: str, *, max_length: int) -> str:
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or len(value) > max_length
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError(f"unsafe path component: {value!r}")
    return value
