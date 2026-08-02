from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.application.services.artifacts import ArtifactApplicationService
from riftx.config import ExecutionOutputConfig
from riftx.context import (
    ExecutionArtifactStore,
    OutputStream,
    ToolResultProcessor,
    execution_artifact_uri,
    parse_execution_artifact_uri,
)
from riftx.domain import (
    Engagement,
    Execution,
    ExecutionStatus,
    ExecutorType,
    Objective,
    Run,
)
from riftx.domain.base import utc_now
from riftx.persistence import (
    Database,
    SQLAlchemyArtifactRepository,
    SQLAlchemyEngagementRepository,
    SQLAlchemyExecutionRepository,
    SQLAlchemyRunEventRepository,
    SQLAlchemyRunRepository,
)
from riftx.runner import RunnerPaths
from riftx.tools import RawToolDefinition, ToolDefinition, ToolOutputConfig

GOLDEN = Path(__file__).parents[1] / "tools" / "golden"


@dataclass(slots=True)
class Harness:
    database: Database
    execution: Execution
    processor: ToolResultProcessor
    artifact_store: ExecutionArtifactStore
    artifact_service: ArtifactApplicationService

    async def close(self) -> None:
        await self.database.dispose()


@dataclass(slots=True)
class HarnessFactory:
    harnesses: list[Harness]

    async def create(
        self,
        tmp_path: Path,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        executor_type: ExecutorType = ExecutorType.PROCESS,
    ) -> Harness:
        harness = await _harness(
            tmp_path,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            executor_type=executor_type,
        )
        self.harnesses.append(harness)
        return harness

    async def close(self) -> None:
        for harness in reversed(self.harnesses):
            await harness.close()


@pytest.fixture
async def harness_factory() -> AsyncIterator[HarnessFactory]:
    factory = HarnessFactory([])
    try:
        yield factory
    finally:
        await factory.close()


async def _harness(
    tmp_path: Path,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
    executor_type: ExecutorType = ExecutorType.PROCESS,
) -> Harness:
    await asyncio.to_thread(tmp_path.mkdir, parents=True, exist_ok=True)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
    await database.create_schema()
    engagements = SQLAlchemyEngagementRepository(database.session_factory)
    runs = SQLAlchemyRunRepository(database.session_factory)
    executions = SQLAlchemyExecutionRepository(database.session_factory)
    artifacts = SQLAlchemyArtifactRepository(database.session_factory)
    events = SQLAlchemyRunEventRepository(database.session_factory)
    await engagements.create(Engagement(id="engagement-1", name="Authorized"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    await runs.create(
        Run(
            kind="general",
            id="run-1",
            engagement_id="engagement-1",
            node_id="local",
            objective=Objective(description="Process tool output"),
            workspace_path=str(workspace),
        )
    )
    runner_paths = RunnerPaths(tmp_path / "runner")
    output_paths = runner_paths.execution("run-1", "execution-1")
    output_paths.directory.mkdir(parents=True)
    output_paths.stdout.write_bytes(stdout)
    output_paths.stderr.write_bytes(stderr)
    started = utc_now()
    execution = Execution(
        id="execution-1",
        execution_key="execution-key-1",
        run_id="run-1",
        session_id=None,
        tool_call_id=None,
        attempt_group="initial",
        node_id="local",
        executor_type=executor_type,
        argv=["registered-tool", "--authorized-target"],
        command_text="registered-tool --authorized-target",
        tool_id="demo",
        cwd=str(workspace),
        status=ExecutionStatus.COMPLETED,
        exit_code=exit_code,
        stdout_path=str(output_paths.stdout),
        stderr_path=str(output_paths.stderr),
        started_at=started,
        finished_at=started + timedelta(seconds=1.25),
    )
    await executions.create_if_absent(execution)
    artifact_service = ArtifactApplicationService(
        run_repository=runs,
        execution_repository=executions,
        artifact_repository=artifacts,
        event_repository=events,
        paths=runner_paths,
    )
    artifact_store = ExecutionArtifactStore(artifact_service)
    return Harness(
        database=database,
        execution=execution,
        processor=ToolResultProcessor(artifact_store),
        artifact_store=artifact_store,
        artifact_service=artifact_service,
    )


def _tool(
    tool_id: str = "demo",
    *,
    preferred: str | None = None,
    executor: ExecutorType = ExecutorType.PROCESS,
) -> ToolDefinition:
    return ToolDefinition.from_raw(
        tool_id,
        RawToolDefinition(
            command=["registered-tool"],
            executor=executor,
            output=ToolOutputConfig(preferred=preferred),
        ),
    )


def test_execution_artifact_uri_round_trip_and_rejects_invalid_values() -> None:
    uri = execution_artifact_uri("run with space", "execution:1", OutputStream.STDOUT)

    assert uri == "artifact://runs/run%20with%20space/executions/execution%3A1/stdout"
    assert parse_execution_artifact_uri(uri) == (
        "run with space",
        "execution:1",
        OutputStream.STDOUT,
    )
    with pytest.raises(ValueError, match="invalid execution Artifact URI"):
        parse_execution_artifact_uri("file:///runner/private/stdout.log")
    with pytest.raises(ValueError, match="invalid execution Artifact URI"):
        parse_execution_artifact_uri("artifact://runs/run%2Fescape/executions/execution-1/stdout")


async def test_one_kilobyte_text_stays_inline_and_has_raw_artifact(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    content = ("authorized result ✓\n" * 48).encode()[:1024]
    harness = await harness_factory.create(tmp_path, stdout=content)

    result = await harness.processor.process(harness.execution, _tool())

    assert result.parser == "generic_text"
    assert "authorized result ✓" in result.context_summary
    assert result.raw_artifacts[0].uri == ("artifact://runs/run-1/executions/execution-1/stdout")
    assert result.raw_artifacts[0].size == len(content)
    assert all("/runner/" not in artifact.uri for artifact in result.raw_artifacts)


async def test_two_hundred_kilobytes_uses_head_tail_preview(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    content = b"HEAD-MARKER\n" + (b"x" * (200 * 1024)) + b"\nTAIL-MARKER"
    harness = await harness_factory.create(tmp_path, stdout=content)

    result = await harness.processor.process(harness.execution, _tool())

    stdout = next(item for item in result.previews if item.stream is OutputStream.STDOUT)
    assert stdout.truncated
    assert "HEAD-MARKER" in stdout.text
    assert "TAIL-MARKER" in stdout.text
    assert "bytes omitted" in stdout.text
    assert len(result.context_summary) <= 8000
    assert result.raw_artifacts[0].size == len(content)


async def test_empty_stderr_does_not_consume_inline_preview_budget(
    tmp_path: Path,
    harness_factory: HarnessFactory,
) -> None:
    content = b"HEAD\n" + (b"x" * 2048) + b"\nTAIL"
    harness = await harness_factory.create(tmp_path, stdout=content)
    harness.processor = ToolResultProcessor(
        harness.artifact_store,
        config=ExecutionOutputConfig(
            max_inline_bytes=1024,
            preview_head_bytes=512,
            preview_tail_bytes=512,
        ),
    )

    result = await harness.processor.process(harness.execution, _tool())

    stdout = next(item for item in result.previews if item.stream is OutputStream.STDOUT)
    assert stdout.truncated
    assert "HEAD" in stdout.text
    assert "TAIL" in stdout.text


async def test_fifty_megabytes_is_spilled_without_entering_context(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    size = 50 * 1024 * 1024
    harness = await harness_factory.create(tmp_path)
    await asyncio.to_thread(
        _write_large_output,
        Path(harness.execution.stdout_path),
        size,
    )

    result = await harness.processor.process(harness.execution, _tool())

    assert result.raw_artifacts[0].size == size
    assert len(result.context_summary) <= 8000
    assert "HEAD-50MB" in result.context_summary
    assert "TAIL-50MB" in result.context_summary
    assert str(tmp_path) not in result.context_summary
    read = await harness.artifact_store.read(
        result.raw_artifacts[0].uri,
        offset=size - 9,
        max_bytes=9,
    )
    assert read.data == b"TAIL-50MB"
    assert read.eof


async def test_utf8_is_preserved_and_binary_remains_artifact_only(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    utf8 = "授权扫描完成：发现开放端口 443。\n".encode()
    text_harness = await harness_factory.create(tmp_path / "text", stdout=utf8)
    text_result = await text_harness.processor.process(text_harness.execution, _tool())
    assert "授权扫描完成" in text_result.context_summary
    assert not text_result.previews[-1].binary

    binary = b"\x00\xffSECRET-BINARY\x01\x02"
    binary_harness = await harness_factory.create(tmp_path / "binary", stdout=binary)
    binary_result = await binary_harness.processor.process(binary_harness.execution, _tool())
    assert binary_result.parser == "generic_binary"
    assert binary_result.raw_artifacts[0].mime_type == "application/octet-stream"
    assert "SECRET-BINARY" not in binary_result.context_summary
    assert "artifact-only" in binary_result.context_summary


@pytest.mark.parametrize(
    ("tool", "content", "expected_parser", "expected_key"),
    [
        (
            _tool(preferred="json"),
            b'{"target":"authorized.example","ports":[80,443]}',
            "generic_json",
            "top_level_keys",
        ),
        (_tool("nmap", preferred="xml"), (GOLDEN / "nmap.xml").read_bytes(), "nmap_xml", "hosts"),
        (
            _tool("nuclei", preferred="jsonl"),
            (GOLDEN / "nuclei.jsonl").read_bytes(),
            "nuclei_jsonl",
            "findings",
        ),
    ],
)
async def test_deterministic_structured_parsers(
    tmp_path: Path,
    tool: ToolDefinition,
    content: bytes,
    expected_parser: str,
    expected_key: str,
    harness_factory: HarnessFactory,
) -> None:
    harness = await harness_factory.create(tmp_path, stdout=content)
    harness.execution.tool_id = tool.id

    result = await harness.processor.process(harness.execution, tool)

    assert result.parser == expected_parser
    assert expected_key in result.structured_result
    assert result.parser_error is None


async def test_shell_result_parser_records_stream_and_exit_metadata(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    harness = await harness_factory.create(
        tmp_path,
        stdout=b"shell output\n",
        stderr=b"warning\n",
        exit_code=2,
        executor_type=ExecutorType.SHELL,
    )
    tool = _tool("run_shell", executor=ExecutorType.SHELL)

    result = await harness.processor.process(harness.execution, tool)

    assert result.parser == "shell_result"
    assert result.structured_result["exit_code"] == 2
    assert result.statistics["stdout_bytes"] == 13
    assert "Execution exited with code 2" in result.context_summary


async def test_parser_failure_falls_back_without_losing_raw_output(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    harness = await harness_factory.create(tmp_path, stdout=b"not valid nmap xml\nopen 443\n")
    tool = _tool("nmap", preferred="xml")

    result = await harness.processor.process(harness.execution, tool)

    assert result.parser == "generic_text"
    assert result.parser_error is not None
    assert "nmap_xml parser failed" in result.parser_error
    assert result.raw_artifacts[0].available
    assert "Deterministic parser fallback" in result.context_summary


async def test_missing_artifact_has_stable_error_and_no_path_leak(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    harness = await harness_factory.create(tmp_path, stdout=b"durable output")
    first = await harness.processor.process(harness.execution, _tool())
    stdout = first.raw_artifacts[0]
    artifact, content_path = await harness.artifact_service.content_path(stdout.artifact_id or "")
    content_path.unlink()

    with pytest.raises(ApplicationConflictError) as exc_info:
        await harness.artifact_store.read(stdout.uri)
    assert exc_info.value.code == "artifact_content_missing"

    second = await harness.processor.process(harness.execution, _tool())
    missing = next(item for item in second.raw_artifacts if item.stream is OutputStream.STDOUT)
    assert not missing.available
    assert "artifact_content_missing" in (missing.error or "")
    assert artifact.path not in second.context_summary
    assert stdout.uri in second.context_summary


async def test_large_stderr_is_prioritized_over_small_stdout(
    tmp_path: Path, harness_factory: HarnessFactory
) -> None:
    stderr = b"FATAL permission denied for authorized check\n" * 3000
    harness = await harness_factory.create(
        tmp_path, stdout=b"short stdout\n", stderr=stderr, exit_code=1
    )

    result = await harness.processor.process(harness.execution, _tool())

    assert result.previews[0].stream is OutputStream.STDERR
    assert "FATAL permission denied" in result.context_summary
    assert result.statistics["stderr_bytes"] > result.statistics["stdout_bytes"]
    assert any(error.startswith("stderr:") for error in result.errors)


def _write_large_output(path: Path, size: int) -> None:
    with path.open("wb") as stream:
        stream.write(b"HEAD-50MB\n")
        chunk = b"z" * (1024 * 1024)
        for _ in range(49):
            stream.write(chunk)
        remaining = size - stream.tell() - len(b"\nTAIL-50MB")
        stream.write(b"z" * remaining)
        stream.write(b"\nTAIL-50MB")
