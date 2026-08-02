from __future__ import annotations

import asyncio

import pytest

from riftx.config import MCPCircuitBreakerConfig, MCPConfig
from riftx.mcp import GovernedMCPAdapter, MCPCircuitOpenError, MCPCircuitState


class BlockingAdapter:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Queue[tuple[str, str]]()
        self.active = 0
        self.max_active = 0
        self.active_by_server: dict[str, int] = {}
        self.max_by_server: dict[str, int] = {}

    async def call(
        self,
        server_id: str,
        method: str,
        _arguments: dict[str, object],
    ) -> object:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        active = self.active_by_server.get(server_id, 0) + 1
        self.active_by_server[server_id] = active
        self.max_by_server[server_id] = max(self.max_by_server.get(server_id, 0), active)
        await self.started.put((server_id, method))
        try:
            await self.release.wait()
            return {"server_id": server_id, "method": method}
        finally:
            self.active -= 1
            self.active_by_server[server_id] -= 1


class FailingAdapter:
    def __init__(self) -> None:
        self.fail = True
        self.calls = 0
        self.probe_started = asyncio.Event()
        self.release_probe = asyncio.Event()

    async def call(
        self,
        _server_id: str,
        _method: str,
        _arguments: dict[str, object],
    ) -> object:
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream unavailable")
        self.probe_started.set()
        await self.release_probe.wait()
        return {"ok": True}


async def test_per_server_and_global_semaphores_bound_only_mcp_calls() -> None:
    adapter = BlockingAdapter()
    governed = GovernedMCPAdapter(
        adapter,
        config=MCPConfig(max_concurrent_per_server=2, max_concurrent_total=3),
    )
    tasks = [
        asyncio.create_task(governed.call(server, f"call-{index}"))
        for index, server in enumerate(["one", "one", "one", "two", "two", "three"])
    ]
    for _ in range(3):
        await asyncio.wait_for(adapter.started.get(), timeout=1)

    snapshot = await governed.health_snapshot()
    assert snapshot.active_calls == 3
    assert adapter.max_active == 3
    assert adapter.max_by_server["one"] == 2

    adapter.release.set()
    await asyncio.gather(*tasks)
    assert adapter.max_active <= 3
    assert all(value <= 2 for value in adapter.max_by_server.values())


async def test_circuit_opens_cools_down_and_allows_one_half_open_probe() -> None:
    now = [10.0]
    adapter = FailingAdapter()
    governed = GovernedMCPAdapter(
        adapter,
        config=MCPConfig(
            circuit_breaker=MCPCircuitBreakerConfig(
                failure_threshold=2,
                cooldown_seconds=5,
            )
        ),
        clock=lambda: now[0],
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="upstream unavailable"):
            await governed.call("docs", "tools/call")
    with pytest.raises(MCPCircuitOpenError) as blocked:
        await governed.call("docs", "tools/call")
    assert blocked.value.retry_after_seconds == 5
    assert adapter.calls == 2

    now[0] += 5
    adapter.fail = False
    probe = asyncio.create_task(governed.call("docs", "tools/call"))
    await asyncio.wait_for(adapter.probe_started.wait(), timeout=1)
    with pytest.raises(MCPCircuitOpenError):
        await governed.call("docs", "tools/call")
    snapshot = await governed.health_snapshot()
    assert snapshot.servers[0].circuit_state is MCPCircuitState.HALF_OPEN
    assert snapshot.servers[0].half_open_probe_in_flight is True

    adapter.release_probe.set()
    assert await probe == {"ok": True}
    recovered = await governed.health_snapshot()
    assert recovered.servers[0].circuit_state is MCPCircuitState.CLOSED
    assert recovered.servers[0].failure_count == 0
    assert recovered.servers[0].completed_calls == 1
