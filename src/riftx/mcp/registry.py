"""Production MCP server lifecycle and bounded Tool Index projection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from agents.mcp import MCPServer, MCPServerStreamableHttp
from mcp.types import CallToolResult
from mcp.types import Tool as SDKMCPTool

from riftx.config import MCPConfig, MCPServerConfig
from riftx.domain import ToolAvailability

from .governance import GovernedMCPAdapter, MCPAdapter
from .models import (
    MCPHealthSnapshot,
    MCPRegistrySnapshot,
    MCPServerAvailability,
    MCPServerSnapshot,
    MCPToolIndexEntry,
    MCPToolSchema,
)

_WORDS = re.compile(r"[a-z0-9]+")
_UNSAFE_TOOL_ID = re.compile(r"[^A-Za-z0-9_-]+")
_FILE_ABSOLUTE_PATH = re.compile(r"(?i)\bfile:///[^\s'\"<>]+")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9:/])/(?!/)(?:[^\s'\"<>]+)")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:[^\s'\"<>]+)")


class MCPServerClient(Protocol):
    async def connect(self) -> None: ...

    async def cleanup(self) -> None: ...

    async def list_tools(self) -> list[SDKMCPTool]: ...

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object] | None,
        meta: dict[str, object] | None = None,
    ) -> CallToolResult: ...


class MCPServerFactory(Protocol):
    def __call__(
        self,
        server_id: str,
        definition: MCPServerConfig,
    ) -> MCPServerClient: ...


class OpenAIMCPServerFactory:
    """Build only operator-configured remote MCP transports inside the Worker."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def __call__(
        self,
        server_id: str,
        definition: MCPServerConfig,
    ) -> MCPServer:
        headers: dict[str, str] = {}
        for header, reference in definition.header_env.items():
            value = self._environment.get(reference)
            if not value:
                raise MCPServerConfigurationError(server_id, "mcp_secret_unavailable")
            headers[header] = value
        return MCPServerStreamableHttp(
            params={
                "url": definition.url,
                "headers": headers,
                "timeout": definition.request_timeout_seconds,
                "sse_read_timeout": definition.read_timeout_seconds,
            },
            cache_tools_list=False,
            name=f"riftx:{server_id}",
            client_session_timeout_seconds=definition.request_timeout_seconds,
            max_retry_attempts=0,
            require_approval="always",
            failure_error_function=None,
        )


class MCPServerConfigurationError(RuntimeError):
    def __init__(self, server_id: str, code: str) -> None:
        self.server_id = server_id
        self.code = code
        super().__init__(f"MCP server {server_id!r} configuration is unavailable ({code})")


class MCPToolInvocationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OpenAIMCPAdapter:
    """Own connected SDK clients without exposing transport configuration."""

    def __init__(
        self,
        servers: Mapping[str, MCPServerConfig],
        *,
        factory: MCPServerFactory | None = None,
    ) -> None:
        self._servers = dict(servers)
        self._factory = factory or OpenAIMCPServerFactory()
        self._clients: dict[str, MCPServerClient] = {}
        self._connect_locks = {server_id: asyncio.Lock() for server_id in self._servers}
        self._lock = asyncio.Lock()
        self._closed = False

    async def call(
        self,
        server_id: str,
        method: str,
        arguments: dict[str, object],
    ) -> object:
        if method not in {"tools/list", "tools/call"}:
            raise ValueError("MCP adapter method is not permitted")
        client = await self._client(server_id)
        if method == "tools/list":
            return await client.list_tools()
        if method == "tools/call":
            tool_name = arguments.get("name")
            tool_arguments = arguments.get("arguments")
            execution_key = arguments.get("execution_key")
            if (
                not isinstance(tool_name, str)
                or not tool_name
                or not isinstance(tool_arguments, dict)
                or not isinstance(execution_key, str)
                or not execution_key
            ):
                raise ValueError("MCP tools/call envelope is invalid")
            return await client.call_tool(
                tool_name,
                tool_arguments,
                meta={"riftx/execution-key": execution_key},
            )
        raise AssertionError("unreachable MCP adapter method")

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        results = await asyncio.gather(
            *(client.cleanup() for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result

    async def _client(self, server_id: str) -> MCPServerClient:
        definition = self._servers.get(server_id)
        connect_lock = self._connect_locks.get(server_id)
        if definition is None or connect_lock is None or not definition.enabled:
            raise MCPServerConfigurationError(server_id, "mcp_server_not_configured")
        async with connect_lock:
            async with self._lock:
                if self._closed:
                    raise RuntimeError("MCP adapter is closed")
                existing = self._clients.get(server_id)
                if existing is not None:
                    return existing
            client = self._factory(server_id, definition)
            try:
                await client.connect()
            except BaseException:
                try:
                    await client.cleanup()
                except BaseException:
                    pass
                raise
            async with self._lock:
                if self._closed:
                    should_cleanup = True
                else:
                    self._clients[server_id] = client
                    should_cleanup = False
            if should_cleanup:
                await client.cleanup()
                raise RuntimeError("MCP adapter is closed")
            return client


class MCPToolIndex:
    """Search the latest safe MCP discovery snapshot."""

    def __init__(self, registry: MCPServerRegistry) -> None:
        self._registry = registry

    def list_tools(self) -> list[MCPToolIndexEntry]:
        return list(self._registry.snapshot.tools)

    def get(self, tool_id: str) -> MCPToolIndexEntry:
        try:
            return self._registry._entries[tool_id]
        except KeyError as exc:
            raise KeyError(tool_id) from exc

    def schema(self, tool_id: str) -> MCPToolSchema:
        try:
            full_schema = self._registry._schemas[tool_id]
        except KeyError as exc:
            raise KeyError(tool_id) from exc
        return MCPToolSchema(
            tool_id=tool_id,
            generation=self._registry.snapshot.generation,
            full_schema=full_schema,
        )

    def search(self, query: str, *, max_results: int = 20) -> list[MCPToolIndexEntry]:
        if max_results < 1 or max_results > 100:
            raise ValueError("max_results must be between 1 and 100")
        terms = set(_WORDS.findall(query.lower()))
        if not terms:
            return self.list_tools()[:max_results]
        scored: list[tuple[int, str, MCPToolIndexEntry]] = []
        for entry in self._registry.snapshot.tools:
            searchable = f"{entry.server_id} {entry.name} {entry.title or ''} {entry.description}"
            corpus = set(_WORDS.findall(searchable.lower()))
            score = len(terms & corpus)
            if score:
                scored.append((-score, entry.id, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [entry for _, _, entry in scored[:max_results]]


class MCPServerRegistry:
    """Discover configured MCP servers without making one failure global."""

    def __init__(
        self,
        config: MCPConfig,
        *,
        adapter: MCPAdapter | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        resolved_environment = os.environ if environment is None else environment
        self._adapter = adapter or OpenAIMCPAdapter(
            config.servers,
            factory=OpenAIMCPServerFactory(resolved_environment),
        )
        self._redactions = _configured_redactions(config, resolved_environment)
        self._governed = GovernedMCPAdapter(self._adapter, config=config)
        self._generation = 0
        self._snapshot: MCPRegistrySnapshot | None = None
        self._entries: dict[str, MCPToolIndexEntry] = {}
        self._schemas: dict[str, dict[str, object]] = {}
        self._refresh_lock = asyncio.Lock()
        self.index = MCPToolIndex(self)

    @property
    def snapshot(self) -> MCPRegistrySnapshot:
        if self._snapshot is None:
            raise RuntimeError("MCP server registry has not been refreshed")
        return self._snapshot

    async def refresh(self) -> MCPRegistrySnapshot:
        async with self._refresh_lock:
            enabled = [
                (server_id, definition)
                for server_id, definition in self._config.servers.items()
                if definition.enabled
            ]
            discovered = await asyncio.gather(
                *(self._discover(server_id, definition) for server_id, definition in enabled)
            )
            server_snapshots = [
                MCPServerSnapshot(
                    server_id=server_id,
                    availability=MCPServerAvailability.DISABLED,
                )
                for server_id, definition in self._config.servers.items()
                if not definition.enabled
            ]
            entries: dict[str, MCPToolIndexEntry] = {}
            schemas: dict[str, dict[str, object]] = {}
            for server, server_entries, server_schemas in discovered:
                server_snapshots.append(server)
                for entry in server_entries:
                    if entry.id in entries:
                        raise RuntimeError("MCP qualified Tool ID collision")
                    entries[entry.id] = entry
                    schemas[entry.id] = server_schemas[entry.id]
            self._generation += 1
            self._entries = entries
            self._schemas = schemas
            self._snapshot = MCPRegistrySnapshot(
                generation=self._generation,
                source_digest=_config_digest(self._config),
                servers=sorted(server_snapshots, key=lambda item: item.server_id),
                tools=sorted(entries.values(), key=lambda item: item.id),
            )
            return self._snapshot

    async def close(self) -> None:
        close = getattr(self._adapter, "close", None)
        if close is not None:
            await close()

    async def health_snapshot(self) -> MCPHealthSnapshot:
        return await self._governed.health_snapshot()

    async def invoke(
        self,
        tool_id: str,
        arguments: dict[str, object],
        *,
        execution_key: str,
    ) -> tuple[MCPToolIndexEntry, dict[str, object]]:
        try:
            entry = self.index.get(tool_id)
        except KeyError:
            raise MCPToolInvocationError("mcp_tool_not_found") from None
        if not entry.invocation_enabled:
            raise MCPToolInvocationError("mcp_tool_invocation_disabled")
        definition = self._config.servers.get(entry.server_id)
        if definition is None or not definition.enabled:
            raise MCPToolInvocationError("mcp_server_not_configured")
        try:
            validated_arguments = _bounded_json_object(
                arguments,
                max_bytes=self._config.max_call_argument_bytes,
            )
        except (TypeError, ValueError):
            raise MCPToolInvocationError("mcp_call_arguments_invalid") from None
        try:
            async with asyncio.timeout(definition.request_timeout_seconds):
                result = await self._governed.call(
                    entry.server_id,
                    "tools/call",
                    {
                        "name": entry.name,
                        "arguments": validated_arguments,
                        "execution_key": execution_key,
                    },
                )
        except TimeoutError:
            raise MCPToolInvocationError("mcp_call_timeout") from None
        except Exception:
            raise MCPToolInvocationError("mcp_server_unavailable") from None
        if not isinstance(result, CallToolResult):
            raise MCPToolInvocationError("mcp_call_result_invalid")
        try:
            sanitized = _sanitize_json(
                result.model_dump(mode="json", by_alias=True),
                redactions=self._redactions,
                max_string_length=self._config.max_call_result_bytes,
                truncate_strings=False,
            )
        except (TypeError, ValueError):
            raise MCPToolInvocationError("mcp_call_result_invalid") from None
        if not isinstance(sanitized, dict):
            raise MCPToolInvocationError("mcp_call_result_invalid")
        if len(_canonical_bytes(sanitized)) > self._config.max_call_result_bytes:
            raise MCPToolInvocationError("mcp_call_result_too_large")
        return entry, sanitized

    async def _discover(
        self,
        server_id: str,
        definition: MCPServerConfig,
    ) -> tuple[
        MCPServerSnapshot,
        list[MCPToolIndexEntry],
        dict[str, dict[str, object]],
    ]:
        try:
            async with asyncio.timeout(self._config.discovery_timeout_seconds):
                result = await self._governed.call(server_id, "tools/list", {})
        except TimeoutError:
            return _unavailable(server_id, "mcp_discovery_timeout")
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, MCPServerConfigurationError)
                else "mcp_server_unavailable"
            )
            return _unavailable(server_id, code)
        if not isinstance(result, Sequence) or isinstance(result, str | bytes):
            return _unavailable(server_id, "mcp_tool_list_invalid")
        if len(result) > self._config.max_tools_per_server:
            return _unavailable(server_id, "mcp_tool_limit_exceeded")

        names: set[str] = set()
        entries: list[MCPToolIndexEntry] = []
        schemas: dict[str, dict[str, object]] = {}
        rejected = 0
        for raw_tool in result:
            if not isinstance(raw_tool, SDKMCPTool):
                rejected += 1
                continue
            name = raw_tool.name.strip()
            if not name or name in names:
                rejected += 1
                continue
            names.add(name)
            if definition.allowed_tools and name not in definition.allowed_tools:
                continue
            if name in definition.blocked_tools:
                continue
            try:
                entry, schema = _project_tool(
                    server_id,
                    raw_tool,
                    max_schema_bytes=self._config.max_schema_bytes,
                    redactions=self._redactions,
                    invocation_enabled=bool(definition.allowed_tools),
                )
            except Exception:
                rejected += 1
                continue
            entries.append(entry)
            schemas[entry.id] = schema
        return (
            MCPServerSnapshot(
                server_id=server_id,
                availability=MCPServerAvailability.READY,
                tool_count=len(entries),
                rejected_tool_count=rejected,
            ),
            entries,
            schemas,
        )


def _unavailable(
    server_id: str,
    error_code: str,
) -> tuple[MCPServerSnapshot, list[MCPToolIndexEntry], dict[str, dict[str, object]]]:
    return (
        MCPServerSnapshot(
            server_id=server_id,
            availability=MCPServerAvailability.UNAVAILABLE,
            error_code=error_code,
        ),
        [],
        {},
    )


def _project_tool(
    server_id: str,
    raw_tool: SDKMCPTool,
    *,
    max_schema_bytes: int,
    redactions: tuple[str, ...],
    invocation_enabled: bool,
) -> tuple[MCPToolIndexEntry, dict[str, object]]:
    name = raw_tool.name.strip()
    if (
        len(name) > 256
        or any(ord(character) < 32 for character in name)
        or _contains_absolute_path(name)
        or any(value in name for value in redactions)
    ):
        raise ValueError("invalid MCP tool name")
    input_schema = _bounded_schema(
        raw_tool.inputSchema,
        max_schema_bytes,
        redactions=redactions,
    )
    schema_bytes = _canonical_bytes(input_schema)
    title = _safe_text(raw_tool.title, max_length=256, redactions=redactions)
    description = _safe_text(
        raw_tool.description,
        max_length=1000,
        redactions=redactions,
    )
    if not description:
        description = f"Untrusted MCP tool metadata for {name}."
    annotations = getattr(raw_tool, "annotations", None)
    tool_id = _qualified_tool_id(server_id, name)
    entry = MCPToolIndexEntry(
        id=tool_id,
        server_id=server_id,
        name=name,
        title=title,
        description=description,
        availability=ToolAvailability.AVAILABLE,
        invocation_enabled=invocation_enabled,
        input_schema_digest=hashlib.sha256(schema_bytes).hexdigest(),
        read_only_hint=_optional_bool(annotations, "readOnlyHint"),
        destructive_hint=_optional_bool(annotations, "destructiveHint"),
        idempotent_hint=_optional_bool(annotations, "idempotentHint"),
        open_world_hint=_optional_bool(annotations, "openWorldHint"),
    )
    return entry, {
        "type": "function",
        "name": tool_id,
        "description": description,
        "parameters": input_schema,
        "x-riftx": {
            "tool_id": tool_id,
            "execution_type": "mcp",
            "approval_policy": "explicit",
            "content_trust": "UNTRUSTED_EXTERNAL_CONTENT",
            "mcp_server_id": server_id,
            "mcp_tool_name": name,
        },
    }


def _bounded_schema(
    value: object,
    max_schema_bytes: int,
    *,
    redactions: tuple[str, ...],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("MCP input schema must be an object")
    sanitized = _sanitize_json(value, redactions=redactions)
    if not isinstance(sanitized, dict):
        raise TypeError("MCP input schema must remain an object")
    schema_type = sanitized.get("type")
    if schema_type not in {None, "object"}:
        raise ValueError("MCP input schema root must be an object")
    sanitized.setdefault("type", "object")
    properties = sanitized.setdefault("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("MCP input schema properties must be an object")
    required = sanitized.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError("MCP input schema required must contain strings")
    encoded = _canonical_bytes(sanitized)
    if len(encoded) > max_schema_bytes:
        raise ValueError("MCP input schema exceeds the configured bound")
    return sanitized


def _sanitize_json(
    value: object,
    *,
    redactions: tuple[str, ...],
    depth: int = 0,
    max_string_length: int = 4096,
    truncate_strings: bool = True,
    redact_paths: bool = True,
) -> object:
    if depth > 64:
        raise ValueError("MCP schema exceeds the configured nesting bound")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > max_string_length:
            if not truncate_strings:
                raise ValueError("MCP JSON string exceeds the configured bound")
            value = value[:max_string_length]
        return _redact_sensitive(value, redactions, redact_paths=redact_paths)
    if isinstance(value, list):
        if len(value) > 1024:
            raise ValueError("MCP schema array exceeds the configured structural bound")
        return [
            _sanitize_json(
                item,
                redactions=redactions,
                depth=depth + 1,
                max_string_length=max_string_length,
                truncate_strings=truncate_strings,
                redact_paths=redact_paths,
            )
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 1024:
            raise ValueError("MCP schema object exceeds the configured structural bound")
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256 or _contains_absolute_path(key):
                raise ValueError("MCP schema contains an unsafe key")
            if any(value in key for value in redactions):
                raise ValueError("MCP schema contains a sensitive key")
            normalized[key] = _sanitize_json(
                item,
                redactions=redactions,
                depth=depth + 1,
                max_string_length=max_string_length,
                truncate_strings=truncate_strings,
                redact_paths=redact_paths,
            )
        return normalized
    raise TypeError("MCP schema must contain JSON values only")


def _safe_text(
    value: object,
    *,
    max_length: int,
    redactions: tuple[str, ...],
) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(_redact_sensitive(value, redactions).split())[:max_length].strip()
    return normalized or None


def _redact_sensitive(
    value: str,
    redactions: tuple[str, ...],
    *,
    redact_paths: bool = True,
) -> str:
    for sensitive in redactions:
        value = value.replace(sensitive, "[REDACTED_SECRET]")
    return _redact_paths(value) if redact_paths else value


def _redact_paths(value: str) -> str:
    redacted = _FILE_ABSOLUTE_PATH.sub("[REDACTED_PATH]", value)
    redacted = _WINDOWS_ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)
    return _POSIX_ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)


def _contains_absolute_path(value: str) -> bool:
    return bool(
        _FILE_ABSOLUTE_PATH.search(value)
        or _WINDOWS_ABSOLUTE_PATH.search(value)
        or _POSIX_ABSOLUTE_PATH.search(value)
    )


def _optional_bool(value: object, attribute: str) -> bool | None:
    candidate = getattr(value, attribute, None)
    return candidate if isinstance(candidate, bool) else None


def _qualified_tool_id(server_id: str, tool_name: str) -> str:
    server_slug = _UNSAFE_TOOL_ID.sub("_", server_id)[:16].strip("_") or "server"
    tool_slug = _UNSAFE_TOOL_ID.sub("_", tool_name)[:20].strip("_") or "tool"
    digest = hashlib.sha256(f"{server_id}\0{tool_name}".encode()).hexdigest()[:19]
    return f"mcp__{server_slug}__{tool_slug}__{digest}"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _bounded_json_object(value: object, *, max_bytes: int) -> dict[str, object]:
    normalized = _sanitize_json(
        value,
        redactions=(),
        max_string_length=max_bytes,
        truncate_strings=False,
        redact_paths=False,
    )
    if not isinstance(normalized, dict):
        raise MCPToolInvocationError("mcp_call_arguments_invalid")
    if len(_canonical_bytes(normalized)) > max_bytes:
        raise MCPToolInvocationError("mcp_call_arguments_too_large")
    return normalized


def _config_digest(config: MCPConfig) -> str:
    return hashlib.sha256(_canonical_bytes(config.model_dump(mode="json"))).hexdigest()


def _configured_redactions(
    config: MCPConfig,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    values = {definition.url for definition in config.servers.values()}
    values.update(
        value
        for definition in config.servers.values()
        for reference in definition.header_env.values()
        if (value := environment.get(reference))
    )
    return tuple(sorted(values, key=lambda value: (-len(value), value)))
