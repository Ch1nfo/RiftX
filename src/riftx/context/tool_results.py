"""Deterministic Tool Result processing with bounded Agent context summaries."""

from __future__ import annotations

import asyncio
import codecs
import shlex
from collections import Counter
from collections.abc import Callable
from functools import partial

from riftx.config import ExecutionOutputConfig
from riftx.domain import Execution, ExecutorType
from riftx.runner import OpenedArtifactContent
from riftx.tools import (
    ToolDefinition,
    ToolOutputParseError,
    parse_generic_json,
    parse_masscan_json,
    parse_nmap_xml,
    parse_nuclei_jsonl,
)

from .artifacts import ExecutionArtifactStore, SpilledArtifact
from .models import OutputStream, ProcessedToolResult, RawArtifactReference, StreamPreview

_MAX_STRUCTURED_PARSE_BYTES = 8 * 1024 * 1024
_ERROR_TERMS = ("error", "failed", "failure", "denied", "timeout", "fatal", "exception")
_MATCH_TERMS = ("open", "found", "vulnerable", "critical", "high", "success", "matched")


class ToolResultProcessor:
    """Produce immutable raw artifacts, deterministic structure, and a bounded summary."""

    def __init__(
        self,
        artifact_store: ExecutionArtifactStore,
        *,
        config: ExecutionOutputConfig | None = None,
    ) -> None:
        self._artifacts = artifact_store
        self._config = config or ExecutionOutputConfig()

    async def process(
        self,
        execution: Execution,
        tool: ToolDefinition,
    ) -> ProcessedToolResult:
        spilled: list[SpilledArtifact] = []
        try:
            for stream in (OutputStream.STDOUT, OutputStream.STDERR):
                spilled.append(await self._artifacts.spill(execution, stream))
            return await self._process_spilled(execution, tool, spilled)
        finally:
            for item in spilled:
                if item.content_lease is not None:
                    item.content_lease.close()

    async def _process_spilled(
        self,
        execution: Execution,
        tool: ToolDefinition,
        spilled: list[SpilledArtifact],
    ) -> ProcessedToolResult:
        previews = await self._build_previews(spilled)
        artifact_errors = [
            f"{item.reference.stream.value} artifact unavailable: {item.reference.error}"
            for item in spilled
            if not item.reference.available
        ]
        parser_name = _select_parser(tool, previews)
        parser_error: str | None = None
        try:
            structured = await _complete_blocking_operation(
                lambda: _parse_structured(
                    parser_name,
                    execution,
                    spilled,
                    previews,
                )
            )
        except (OSError, ToolOutputParseError, UnicodeError, ValueError) as exc:
            parser_error = f"{parser_name} parser failed: {exc}"
            parser_name = _fallback_parser(previews)
            structured = _generic_structure(parser_name, execution, spilled, previews)

        observations = _observations(parser_name, structured, spilled)
        errors = [*artifact_errors]
        if execution.exit_code not in {None, 0}:
            errors.append(f"Execution exited with code {execution.exit_code}.")
        if parser_error:
            errors.append(parser_error)
        errors.extend(_extract_error_lines(previews))
        errors = _deduplicate(errors)[:100]
        statistics = _statistics(execution, spilled, structured)
        summary = _context_summary(
            execution=execution,
            parser_name=parser_name,
            parser_error=parser_error,
            observations=observations,
            errors=errors,
            previews=previews,
            artifacts=[item.reference for item in spilled],
            max_characters=self._config.max_context_tokens * 4,
        )
        return ProcessedToolResult(
            execution_id=execution.id,
            status=execution.status,
            tool_id=execution.tool_id or tool.id,
            exit_code=execution.exit_code,
            duration_seconds=_duration(execution),
            parser=parser_name,
            parser_error=parser_error,
            raw_artifacts=[item.reference for item in spilled],
            structured_result=structured,
            context_summary=summary,
            key_observations=observations,
            errors=errors,
            statistics=statistics,
            previews=previews,
        )

    async def _build_previews(
        self,
        spilled: list[SpilledArtifact],
    ) -> list[StreamPreview]:
        ordered = sorted(
            spilled,
            key=lambda item: (
                item.reference.stream is not OutputStream.STDERR,
                -item.reference.size,
            ),
        )
        remaining = self._config.max_inline_bytes
        previews: list[StreamPreview] = []
        for item in ordered:
            desired = self._config.preview_head_bytes + self._config.preview_tail_bytes
            allowance = min(remaining, desired, item.reference.size)
            remaining -= allowance
            previews.append(
                await _complete_blocking_operation(
                    partial(
                        _preview,
                        item,
                        allowance,
                        self._config.preview_head_bytes,
                        self._config.preview_tail_bytes,
                    )
                )
            )
        return previews


async def _complete_blocking_operation[T](operation: Callable[[], T]) -> T:
    """Keep lease ownership until a preview or parser worker has stopped using it."""

    worker = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # The processor consumes the outcome after the worker settles.
            pass
    try:
        result = worker.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        if cancelled:
            raise asyncio.CancelledError() from None
        raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


def _select_parser(tool: ToolDefinition, previews: list[StreamPreview]) -> str:
    preferred = (tool.output.preferred or "").strip().lower()
    tool_id = tool.id.lower()
    if tool.executor is ExecutorType.SHELL or tool_id in {"run_shell", "shell"}:
        return "shell_result"
    if tool_id == "nmap" or preferred == "xml":
        return "nmap_xml"
    if tool_id == "nuclei" or preferred == "jsonl":
        return "nuclei_jsonl"
    if tool_id == "masscan":
        return "masscan_json"
    if preferred == "json":
        return "generic_json"
    stdout = next(
        (preview for preview in previews if preview.stream is OutputStream.STDOUT),
        None,
    )
    if stdout is not None and not stdout.binary and stdout.text.lstrip().startswith(("{", "[")):
        return "generic_json"
    return _fallback_parser(previews)


def _fallback_parser(previews: list[StreamPreview]) -> str:
    return (
        "generic_binary"
        if any(preview.binary and preview.size > 0 for preview in previews)
        else "generic_text"
    )


def _parse_structured(
    parser_name: str,
    execution: Execution,
    spilled: list[SpilledArtifact],
    previews: list[StreamPreview],
) -> dict[str, object]:
    if parser_name in {"generic_text", "generic_binary", "shell_result"}:
        return _generic_structure(parser_name, execution, spilled, previews)
    stdout = spilled[0]
    if not stdout.reference.available or stdout.content_lease is None:
        raise ToolOutputParseError("stdout artifact is unavailable")
    if stdout.reference.size > _MAX_STRUCTURED_PARSE_BYTES:
        raise ToolOutputParseError(f"structured input exceeds {_MAX_STRUCTURED_PARSE_BYTES} bytes")
    content = _read_all(stdout)
    if parser_name == "generic_json":
        return parse_generic_json(content)
    if parser_name == "nmap_xml":
        return parse_nmap_xml(content)
    if parser_name == "nuclei_jsonl":
        return parse_nuclei_jsonl(content)
    if parser_name == "masscan_json":
        return parse_masscan_json(content)
    raise ToolOutputParseError(f"unsupported Tool Result parser {parser_name!r}")


def _generic_structure(
    parser_name: str,
    execution: Execution,
    spilled: list[SpilledArtifact],
    previews: list[StreamPreview],
) -> dict[str, object]:
    structure: dict[str, object] = {
        "adapter": parser_name,
        "status": execution.status.value,
        "exit_code": execution.exit_code,
        "stdout_bytes": spilled[0].reference.size,
        "stderr_bytes": spilled[1].reference.size,
        "binary_streams": [
            preview.stream.value for preview in previews if preview.binary and preview.size > 0
        ],
    }
    if previews:
        structure["truncated_streams"] = [
            preview.stream.value for preview in previews if preview.truncated
        ]
    return structure


def _preview(
    artifact: SpilledArtifact,
    allowance: int,
    configured_head: int,
    configured_tail: int,
) -> StreamPreview:
    reference = artifact.reference
    binary = _is_binary_content(artifact)
    if not reference.available or artifact.content_lease is None or allowance <= 0 or binary:
        return StreamPreview(
            stream=reference.stream,
            size=reference.size,
            mime_type=reference.mime_type,
            binary=binary,
            truncated=reference.size > 0 and allowance <= 0,
        )
    head_bytes = min(configured_head, allowance)
    tail_bytes = min(configured_tail, max(0, allowance - head_bytes))
    data, truncated = _read_head_tail(
        artifact.content_lease,
        reference.size,
        head_bytes=head_bytes,
        tail_bytes=tail_bytes,
    )
    return StreamPreview(
        stream=reference.stream,
        size=reference.size,
        mime_type=reference.mime_type,
        text=data.decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def _is_binary_content(artifact: SpilledArtifact) -> bool:
    reference = artifact.reference
    lease = artifact.content_lease
    if not reference.available or lease is None or reference.size == 0:
        return False
    sample = _read_prefix(lease, min(reference.size, 4096))
    if b"\x00" in sample:
        return True
    try:
        decoder = codecs.getincrementaldecoder("utf-8")()
        decoder.decode(sample, final=False)
    except UnicodeDecodeError:
        return True
    return False


def _read_head_tail(
    lease: OpenedArtifactContent,
    size: int,
    *,
    head_bytes: int,
    tail_bytes: int,
) -> tuple[bytes, bool]:
    budget = head_bytes + tail_bytes
    if size <= budget:
        lease.seek(0)
        content = _read_lease_bytes(lease, size)
        lease.verify_unchanged()
        return content, False
    lease.seek(0)
    head = _read_lease_bytes(lease, head_bytes)
    tail = b""
    if tail_bytes:
        lease.seek(max(0, size - tail_bytes))
        tail = _read_lease_bytes(lease, tail_bytes)
    lease.verify_unchanged()
    omitted = size - len(head) - len(tail)
    marker = f"\n... <{omitted} bytes omitted> ...\n".encode()
    return head + marker + tail, True


def _read_prefix(lease: OpenedArtifactContent, size: int) -> bytes:
    lease.seek(0)
    content = _read_lease_bytes(lease, size)
    lease.verify_unchanged()
    return content


def _read_all(artifact: SpilledArtifact) -> bytes:
    lease = artifact.content_lease
    if lease is None:
        raise ToolOutputParseError("Artifact content is unavailable")
    lease.seek(0)
    content = _read_lease_bytes(lease, artifact.reference.size)
    lease.verify_unchanged()
    return content


def _read_lease_bytes(lease: OpenedArtifactContent, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = lease.read(min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _observations(
    parser_name: str,
    structured: dict[str, object],
    spilled: list[SpilledArtifact],
) -> list[str]:
    observations: list[str] = []
    if parser_name == "nmap_xml":
        observations.append(
            f"Nmap parsed {structured.get('host_count', 0)} hosts and "
            f"{structured.get('open_port_count', 0)} open ports."
        )
        raw_hosts = structured.get("hosts")
        hosts = raw_hosts if isinstance(raw_hosts, list) else []
        open_ports = sorted(
            {
                str(port.get("port"))
                for host in hosts
                if isinstance(host, dict)
                for port in host.get("ports", [])
                if isinstance(port, dict)
                and port.get("state") == "open"
                and port.get("port") is not None
            }
        )
        if open_ports:
            observations.append(f"Open ports: {', '.join(open_ports[:100])}.")
    elif parser_name == "nuclei_jsonl":
        raw_findings = structured.get("findings")
        findings = raw_findings if isinstance(raw_findings, list) else []
        severities = Counter(
            str(item.get("severity") or "unknown") for item in findings if isinstance(item, dict)
        )
        observations.append(f"Nuclei parsed {structured.get('finding_count', 0)} findings.")
        if severities:
            observations.append(
                "Severity counts: "
                + ", ".join(f"{name}={count}" for name, count in sorted(severities.items()))
                + "."
            )
    elif parser_name == "generic_json":
        observations.append(
            f"JSON top level is {structured.get('top_level_type')} with "
            f"{structured.get('item_count')} items."
        )
        keys = structured.get("top_level_keys")
        if isinstance(keys, list) and keys:
            observations.append(f"Top-level keys: {', '.join(map(str, keys[:50]))}.")
    elif parser_name == "masscan_json":
        observations.append(
            f"Masscan parsed {structured.get('host_count', 0)} hosts and "
            f"{structured.get('open_port_count', 0)} open ports."
        )
    elif parser_name == "shell_result":
        observations.append(
            f"Shell produced {spilled[0].reference.size} stdout bytes and "
            f"{spilled[1].reference.size} stderr bytes."
        )
    elif parser_name == "generic_binary":
        raw_binary = structured.get("binary_streams")
        binary = [str(item) for item in raw_binary] if isinstance(raw_binary, list) else []
        observations.append(
            f"Binary output detected in {', '.join(binary)}; content remains artifact-only."
        )
    else:
        observations.append(
            f"Text output contains {spilled[0].reference.size} stdout bytes and "
            f"{spilled[1].reference.size} stderr bytes."
        )
    return observations


def _extract_error_lines(previews: list[StreamPreview]) -> list[str]:
    lines: list[str] = []
    stderr_previews = [preview for preview in previews if preview.stream is OutputStream.STDERR]
    for preview in [*stderr_previews, *previews]:
        for raw_line in preview.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            lowered = line.lower()
            if preview.stream is OutputStream.STDERR or any(
                term in lowered for term in _ERROR_TERMS
            ):
                lines.append(f"{preview.stream.value}: {_clip(line, 500)}")
            if len(lines) >= 100:
                return _deduplicate(lines)
    return _deduplicate(lines)


def _statistics(
    execution: Execution,
    spilled: list[SpilledArtifact],
    structured: dict[str, object],
) -> dict[str, object]:
    statistics: dict[str, object] = {
        "stdout_bytes": spilled[0].reference.size,
        "stderr_bytes": spilled[1].reference.size,
        "total_output_bytes": sum(item.reference.size for item in spilled),
        "artifact_count": sum(item.reference.available for item in spilled),
    }
    duration = _duration(execution)
    if duration is not None:
        statistics["duration_seconds"] = duration
    nested = structured.get("statistics")
    if isinstance(nested, dict):
        statistics.update(nested)
    for key in ("host_count", "open_port_count", "finding_count", "item_count"):
        if key in structured:
            statistics[key] = structured[key]
    return statistics


def _context_summary(
    *,
    execution: Execution,
    parser_name: str,
    parser_error: str | None,
    observations: list[str],
    errors: list[str],
    previews: list[StreamPreview],
    artifacts: list[RawArtifactReference],
    max_characters: int,
) -> str:
    command = execution.command_text or (shlex.join(execution.argv) if execution.argv else "")
    lines = [
        f"Tool execution {execution.id}: status={execution.status.value} "
        f"exit_code={execution.exit_code}.",
        f"Command: {_clip(command or execution.tool_id or 'registered tool', 1000)}",
        f"Parser: {parser_name}."
        + (" Deterministic parser fallback was used." if parser_error else ""),
    ]
    if observations:
        lines.append("Key observations:")
        lines.extend(f"- {item}" for item in observations)
    if errors:
        lines.append("Errors and warnings:")
        lines.extend(f"- {_clip(item, 300)}" for item in errors[:5])
    visible_previews = [preview for preview in previews if preview.text]
    per_preview_characters = max(
        256,
        (max_characters // 2) // max(1, len(visible_previews)),
    )
    for preview in visible_previews:
        label = f"{preview.stream.value} preview"
        if preview.truncated:
            label += " (head/tail)"
        lines.extend(
            (
                f"{label}:",
                _clip_head_tail(preview.text, per_preview_characters),
            )
        )
    body = "\n".join(lines).strip()
    artifact_lines = ["Raw artifacts:"]
    for artifact in artifacts:
        availability = "available" if artifact.available else "unavailable"
        artifact_lines.append(
            f"- {artifact.stream.value}: {artifact.uri} "
            f"({artifact.size} bytes, {artifact.mime_type}, {availability})"
        )
    references = "\n".join(artifact_lines)
    if len(body) + len(references) + 1 > max_characters:
        marker = "\n... <context summary truncated>"
        body_limit = max(0, max_characters - len(references) - len(marker) - 1)
        body = body[:body_limit].rstrip() + marker
    summary = f"{body}\n{references}".strip()
    return summary[:max_characters]


def _duration(execution: Execution) -> float | None:
    if execution.started_at is None or execution.finished_at is None:
        return None
    return max(0.0, (execution.finished_at - execution.started_at).total_seconds())


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _clip_head_tail(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n... <preview clipped for context> ...\n"
    available = max(0, limit - len(marker))
    head = available // 2
    tail = available - head
    return value[:head] + marker + value[-tail:]
