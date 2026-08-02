from datetime import UTC, datetime, timedelta

import pytest

from riftx.application.errors import EntityNotFoundError
from riftx.application.services import NodeApplicationService, NodeHeartbeat, NodeRegistration
from riftx.domain import Node, NodeStatus


class MemoryNodeRepository:
    def __init__(self) -> None:
        self.items: dict[str, Node] = {}

    async def create(self, node: Node) -> Node:
        self.items[node.id] = node
        return node

    async def get(self, node_id: str) -> Node | None:
        return self.items.get(node_id)

    async def save(self, node: Node) -> Node:
        self.items[node.id] = node
        return node

    async def list(
        self,
        *,
        status: NodeStatus | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[Node]:
        nodes = sorted(self.items.values(), key=lambda node: (node.name, node.id))
        if status is not None:
            nodes = [node for node in nodes if node.status is status]
        return nodes[offset : offset + limit]


@pytest.mark.asyncio
async def test_registration_is_idempotent_and_refreshes_metadata() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    repository = MemoryNodeRepository()
    service = NodeApplicationService(repository, clock=lambda: now)

    first, created = await service.register(
        NodeRegistration(
            node_id="runner-a",
            name="Kali A",
            platform="linux",
            architecture="x86_64",
            runner_version="2.0.0",
            capabilities=("port_scan", "port_scan", " scripting "),
            labels={"zone": "lab"},
        )
    )
    second, created_again = await service.register(
        NodeRegistration(
            node_id="runner-a",
            name="Kali Primary",
            platform="linux",
            architecture="aarch64",
            runner_version="2.0.1",
            capabilities=("vulnerability_scan",),
        )
    )

    assert created is True
    assert created_again is False
    assert first.capabilities == ["port_scan", "scripting"]
    assert second.name == "Kali Primary"
    assert second.architecture == "aarch64"
    assert second.runner_version == "2.0.1"
    assert second.status is NodeStatus.ONLINE
    assert second.created_at == first.created_at


@pytest.mark.asyncio
async def test_heartbeat_updates_health_and_liveness_expiry() -> None:
    current = datetime(2026, 7, 29, tzinfo=UTC)
    repository = MemoryNodeRepository()
    service = NodeApplicationService(
        repository,
        offline_after=timedelta(seconds=10),
        lost_after=timedelta(seconds=30),
        clock=lambda: current,
    )
    await service.register(
        NodeRegistration(
            node_id="runner-a",
            name="Windows A",
            platform="windows",
            architecture="amd64",
        )
    )

    degraded = await service.heartbeat(
        "runner-a",
        NodeHeartbeat(status=NodeStatus.DEGRADED, capabilities=("powershell",)),
    )
    assert degraded.status is NodeStatus.DEGRADED
    assert degraded.capabilities == ["powershell"]

    current += timedelta(seconds=11)
    assert (await service.get("runner-a")).status is NodeStatus.OFFLINE

    current += timedelta(seconds=20)
    assert (await service.get("runner-a")).status is NodeStatus.LOST

    recovered = await service.heartbeat("runner-a", NodeHeartbeat())
    assert recovered.status is NodeStatus.ONLINE
    assert recovered.last_seen_at == current


@pytest.mark.asyncio
async def test_disconnect_and_missing_node_are_explicit() -> None:
    now = datetime(2026, 7, 29, tzinfo=UTC)
    repository = MemoryNodeRepository()
    service = NodeApplicationService(repository, clock=lambda: now)
    await service.register(
        NodeRegistration(
            node_id="runner-a",
            name="Runner A",
            platform="linux",
            architecture="x86_64",
        )
    )

    assert (await service.disconnect("runner-a")).status is NodeStatus.OFFLINE
    with pytest.raises(EntityNotFoundError, match="Node 'missing' was not found"):
        await service.heartbeat("missing", NodeHeartbeat())
