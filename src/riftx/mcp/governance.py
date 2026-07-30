"""Concurrency and failure governance applied only around external MCP adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from riftx.config import MCPConfig

from .models import MCPCircuitState, MCPHealthSnapshot, MCPServerHealth


class MCPAdapter(Protocol):
    async def call(
        self,
        server_id: str,
        method: str,
        arguments: dict[str, object],
    ) -> object: ...


class MCPCircuitOpenError(RuntimeError):
    def __init__(self, server_id: str, retry_after_seconds: float) -> None:
        self.server_id = server_id
        self.retry_after_seconds = max(0.0, retry_after_seconds)
        super().__init__(
            f"MCP server {server_id!r} circuit is open; retry after "
            f"{self.retry_after_seconds:.3f}s"
        )


@dataclass(slots=True)
class _ServerState:
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    circuit_state: MCPCircuitState = MCPCircuitState.CLOSED
    failure_count: int = 0
    opened_at: float | None = None
    half_open_probe_in_flight: bool = False
    active_calls: int = 0
    completed_calls: int = 0
    failed_calls: int = 0


class GovernedMCPAdapter:
    """Wrap an MCP adapter without affecting Process, Shell, PTY, or target HTTP."""

    def __init__(
        self,
        adapter: MCPAdapter,
        *,
        config: MCPConfig | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._adapter = adapter
        self._config = config or MCPConfig()
        self._clock = clock
        self._global = asyncio.Semaphore(self._config.max_concurrent_total)
        self._states: dict[str, _ServerState] = {}
        self._states_lock = asyncio.Lock()
        self._active_calls = 0

    async def call(
        self,
        server_id: str,
        method: str,
        arguments: dict[str, object] | None = None,
    ) -> object:
        server_id = server_id.strip()
        method = method.strip()
        if not server_id or not method:
            raise ValueError("MCP server_id and method must not be empty")
        state = await self._state(server_id)
        probe = await self._reserve_circuit(server_id, state)
        try:
            async with state.semaphore, self._global:
                if not probe:
                    probe = await self._reserve_circuit(server_id, state)
                await self._started(state)
                try:
                    result = await self._adapter.call(server_id, method, arguments or {})
                except Exception:
                    await self._failed(state, probe=probe)
                    raise
                else:
                    await self._succeeded(state)
                    return result
                finally:
                    await self._finished(state)
        except BaseException:
            if probe:
                await self._release_cancelled_probe(state)
            raise

    async def health_snapshot(self) -> MCPHealthSnapshot:
        async with self._states_lock:
            items = list(self._states.items())
        servers = [await self._server_health(server_id, state) for server_id, state in items]
        return MCPHealthSnapshot(
            active_calls=self._active_calls,
            max_concurrent_total=self._config.max_concurrent_total,
            max_concurrent_per_server=self._config.max_concurrent_per_server,
            servers=sorted(servers, key=lambda item: item.server_id),
        )

    async def _state(self, server_id: str) -> _ServerState:
        async with self._states_lock:
            state = self._states.get(server_id)
            if state is None:
                state = _ServerState(
                    semaphore=asyncio.Semaphore(
                        self._config.max_concurrent_per_server
                    )
                )
                self._states[server_id] = state
            return state

    async def _reserve_circuit(self, server_id: str, state: _ServerState) -> bool:
        async with state.lock:
            if state.circuit_state is MCPCircuitState.CLOSED:
                return False
            remaining = self._cooldown_remaining(state)
            if state.circuit_state is MCPCircuitState.OPEN and remaining <= 0:
                state.circuit_state = MCPCircuitState.HALF_OPEN
            if state.circuit_state is MCPCircuitState.HALF_OPEN:
                if not state.half_open_probe_in_flight:
                    state.half_open_probe_in_flight = True
                    return True
                raise MCPCircuitOpenError(server_id, remaining)
            raise MCPCircuitOpenError(server_id, remaining)

    async def _started(self, state: _ServerState) -> None:
        async with state.lock:
            state.active_calls += 1
            self._active_calls += 1

    async def _finished(self, state: _ServerState) -> None:
        async with state.lock:
            state.active_calls -= 1
            self._active_calls -= 1

    async def _succeeded(self, state: _ServerState) -> None:
        async with state.lock:
            state.completed_calls += 1
            state.failure_count = 0
            state.opened_at = None
            state.half_open_probe_in_flight = False
            state.circuit_state = MCPCircuitState.CLOSED

    async def _failed(self, state: _ServerState, *, probe: bool) -> None:
        async with state.lock:
            state.failed_calls += 1
            state.failure_count += 1
            if probe or state.failure_count >= self._config.circuit_breaker.failure_threshold:
                state.circuit_state = MCPCircuitState.OPEN
                state.opened_at = self._clock()
            state.half_open_probe_in_flight = False

    async def _release_cancelled_probe(self, state: _ServerState) -> None:
        async with state.lock:
            if state.half_open_probe_in_flight:
                state.half_open_probe_in_flight = False
                state.circuit_state = MCPCircuitState.OPEN

    async def _server_health(
        self,
        server_id: str,
        state: _ServerState,
    ) -> MCPServerHealth:
        async with state.lock:
            circuit_state = state.circuit_state
            remaining = self._cooldown_remaining(state)
            if circuit_state is MCPCircuitState.OPEN and remaining <= 0:
                circuit_state = MCPCircuitState.HALF_OPEN
            return MCPServerHealth(
                server_id=server_id,
                circuit_state=circuit_state,
                failure_count=state.failure_count,
                cooldown_remaining_seconds=remaining,
                half_open_probe_in_flight=state.half_open_probe_in_flight,
                active_calls=state.active_calls,
                completed_calls=state.completed_calls,
                failed_calls=state.failed_calls,
            )

    def _cooldown_remaining(self, state: _ServerState) -> float:
        if state.opened_at is None:
            return 0.0
        return max(
            0.0,
            self._config.circuit_breaker.cooldown_seconds
            - (self._clock() - state.opened_at),
        )
