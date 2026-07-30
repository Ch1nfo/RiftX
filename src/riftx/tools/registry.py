"""Node-local Tool Registry with availability probing and hot reload."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import yaml

from riftx.domain import ToolAvailability, ToolState
from riftx.executors import merge_environment

from .config import parse_tool_config
from .models import RawToolDefinition, ToolDefinition, ToolRegistryConfig, ToolSnapshot


class ToolNotFoundError(KeyError):
    pass


class ToolUnavailableError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(
        self,
        config_path: Path,
        *,
        node_id: str,
        host_environment: dict[str, str] | None = None,
    ) -> None:
        self.config_path = config_path
        self.node_id = node_id
        self._host_environment = dict(os.environ if host_environment is None else host_environment)
        self._config: ToolRegistryConfig | None = None
        self._snapshot: ToolSnapshot | None = None
        self._source_digest: str | None = None
        self._generation = 0
        self._refresh_lock = asyncio.Lock()

    @property
    def config(self) -> ToolRegistryConfig:
        if self._config is None:
            raise RuntimeError("tool registry has not been refreshed")
        return self._config

    @property
    def snapshot(self) -> ToolSnapshot:
        if self._snapshot is None:
            raise RuntimeError("tool registry has not been refreshed")
        return self._snapshot

    async def refresh(self) -> ToolSnapshot:
        async with self._refresh_lock:
            content = await asyncio.to_thread(self.config_path.read_bytes)
            return await self._refresh_content(content)

    async def update_tool(
        self,
        tool_id: str,
        definition: RawToolDefinition,
    ) -> ToolSnapshot:
        """Atomically replace one tool definition and immediately re-probe the registry."""

        async with self._refresh_lock:
            content = await asyncio.to_thread(self.config_path.read_bytes)
            current = parse_tool_config(content, source=str(self.config_path))
            raw = current.model_dump(mode="json")
            tools = dict(raw.get("tools", {}))
            tools[tool_id] = definition.model_dump(mode="json")
            raw["tools"] = tools
            updated = ToolRegistryConfig.model_validate(raw)
            serialized = yaml.safe_dump(
                updated.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
            ).encode("utf-8")
            await asyncio.to_thread(_atomic_write, self.config_path, serialized)
            return await self._refresh_content(serialized)

    async def _refresh_content(self, content: bytes) -> ToolSnapshot:
        digest = hashlib.sha256(content).hexdigest()
        config = parse_tool_config(content, source=str(self.config_path))
        definitions = {
            tool_id: ToolDefinition.from_raw(tool_id, raw) for tool_id, raw in config.tools.items()
        }
        states_list = await asyncio.gather(
            *(self._probe(definition) for definition in definitions.values())
        )
        states = {state.tool_id: state for state in states_list}
        self._generation += 1
        snapshot = ToolSnapshot(
            node_id=self.node_id,
            generation=self._generation,
            source_digest=digest,
            definitions=definitions,
            states=states,
        )
        self._config = config
        self._snapshot = snapshot
        self._source_digest = digest
        return snapshot

    async def reload_if_changed(self) -> ToolSnapshot:
        content = await asyncio.to_thread(self.config_path.read_bytes)
        digest = hashlib.sha256(content).hexdigest()
        if self._snapshot is not None and digest == self._source_digest:
            return self._snapshot
        return await self.refresh()

    def get(self, tool_id: str) -> ToolDefinition:
        definition = self.snapshot.definitions.get(tool_id)
        if definition is None:
            raise ToolNotFoundError(tool_id)
        return definition

    def get_available(self, tool_id: str) -> ToolDefinition:
        definition = self.get(tool_id)
        state = self.snapshot.states[tool_id]
        if state.availability is not ToolAvailability.AVAILABLE:
            raise ToolUnavailableError(
                f"tool {tool_id!r} is {state.availability.value}: {state.reason or 'no reason'}"
            )
        return definition

    def available_tools(self) -> list[ToolDefinition]:
        return [
            definition
            for tool_id, definition in self.snapshot.definitions.items()
            if self.snapshot.states[tool_id].availability is ToolAvailability.AVAILABLE
        ]

    def find_by_capability(self, capability: str) -> list[ToolDefinition]:
        return [
            definition
            for definition in self.available_tools()
            if capability in definition.capabilities
        ]

    def build_argv(self, tool_id: str, args: list[str]) -> list[str]:
        definition = self.get_available(tool_id)
        if any("\x00" in arg for arg in args):
            raise ValueError("tool arguments cannot contain null bytes")
        return [*definition.command, *args]

    async def _probe(self, definition: ToolDefinition) -> ToolState:
        if not definition.enabled:
            return ToolState(
                tool_id=definition.id,
                node_id=self.node_id,
                availability=ToolAvailability.DISABLED,
                reason="disabled by configuration",
            )

        environment = merge_environment(
            definition.environment,
            host_environment=self._host_environment,
        )
        resolved, reason = _resolve_command(definition.command, environment)
        if resolved is None:
            return ToolState(
                tool_id=definition.id,
                node_id=self.node_id,
                availability=ToolAvailability.UNAVAILABLE,
                reason=reason,
            )
        prefix_error = _validate_absolute_prefix_paths(definition.command[1:])
        if prefix_error:
            return ToolState(
                tool_id=definition.id,
                node_id=self.node_id,
                availability=ToolAvailability.MISCONFIGURED,
                resolved_command=resolved,
                reason=prefix_error,
            )

        version: str | None = None
        if definition.version_probe is not None:
            probe_resolved, probe_reason = _resolve_command(
                definition.version_probe.command, environment
            )
            if probe_resolved is None:
                return ToolState(
                    tool_id=definition.id,
                    node_id=self.node_id,
                    availability=ToolAvailability.MISCONFIGURED,
                    resolved_command=resolved,
                    reason=f"version probe unavailable: {probe_reason}",
                )
            probe_prefix_error = _validate_absolute_prefix_paths(
                definition.version_probe.command[1:]
            )
            if probe_prefix_error:
                return ToolState(
                    tool_id=definition.id,
                    node_id=self.node_id,
                    availability=ToolAvailability.MISCONFIGURED,
                    resolved_command=resolved,
                    reason=f"version probe {probe_prefix_error}",
                )
            version, probe_error = await _run_version_probe(
                [probe_resolved, *definition.version_probe.command[1:]],
                environment,
                definition.version_probe.timeout_seconds,
            )
            if probe_error:
                return ToolState(
                    tool_id=definition.id,
                    node_id=self.node_id,
                    availability=ToolAvailability.UNAVAILABLE,
                    resolved_command=resolved,
                    reason=probe_error,
                )

        return ToolState(
            tool_id=definition.id,
            node_id=self.node_id,
            availability=ToolAvailability.AVAILABLE,
            resolved_command=resolved,
            version=version,
        )


def _resolve_command(
    command: list[str], environment: dict[str, str]
) -> tuple[str | None, str | None]:
    executable = command[0]
    path = Path(executable)
    if path.is_absolute():
        if not path.is_file():
            return None, f"executable does not exist: {path}"
        if os.name != "nt" and not os.access(path, os.X_OK):
            return None, f"executable is not executable: {path}"
        return str(path), None

    resolved = shutil.which(executable, path=environment.get("PATH"))
    if resolved is None:
        return None, f"executable not found on PATH: {executable}"
    return resolved, None


def _validate_absolute_prefix_paths(prefix: list[str]) -> str | None:
    for value in prefix:
        path = Path(value)
        if path.is_absolute() and not path.exists():
            return f"command prefix path does not exist: {path}"
    return None


async def _run_version_probe(
    argv: list[str], environment: dict[str, str], timeout_seconds: float
) -> tuple[str | None, str | None]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None, f"version probe timed out after {timeout_seconds:g}s"
    except OSError as exc:
        return None, f"version probe failed to start: {exc}"

    if process.returncode != 0:
        detail = (stderr or stdout)[:8192].decode("utf-8", errors="replace").strip()
        return None, f"version probe exited with {process.returncode}: {detail}"
    output = (stdout or stderr)[:8192].decode("utf-8", errors="replace").strip()
    version = output.splitlines()[0] if output else None
    return version, None


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else None
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        if mode is not None:
            os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
