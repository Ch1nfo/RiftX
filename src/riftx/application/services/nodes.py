"""Runner registration and durable execution-node lifecycle management."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from riftx.application.errors import EntityNotFoundError
from riftx.application.ports.repositories import NodeRepository
from riftx.domain import Node, NodeStatus
from riftx.domain.base import utc_now


@dataclass(frozen=True, slots=True)
class NodeRegistration:
    node_id: str
    name: str
    platform: str
    architecture: str
    runner_version: str = "unknown"
    capabilities: tuple[str, ...] = ()
    labels: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class NodeHeartbeat:
    status: NodeStatus = NodeStatus.ONLINE
    capabilities: tuple[str, ...] | None = None
    labels: dict[str, str] | None = None
    runner_version: str | None = None


class NodeApplicationService:
    """Owns idempotent registration, heartbeats, and liveness transitions."""

    def __init__(
        self,
        repository: NodeRepository,
        *,
        offline_after: timedelta = timedelta(seconds=30),
        lost_after: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if offline_after <= timedelta(0):
            raise ValueError("offline_after must be positive")
        if lost_after <= offline_after:
            raise ValueError("lost_after must be greater than offline_after")
        self._repository = repository
        self._offline_after = offline_after
        self._lost_after = lost_after
        self._clock = clock

    async def register(self, registration: NodeRegistration) -> tuple[Node, bool]:
        now = self._clock()
        existing = await self._repository.get(registration.node_id)
        if existing is None:
            node = Node(
                id=registration.node_id,
                name=registration.name,
                platform=registration.platform,
                architecture=registration.architecture,
                runner_version=registration.runner_version,
                status=NodeStatus.ONLINE,
                capabilities=list(registration.capabilities),
                labels=registration.labels or {},
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            return await self._repository.create(node), True

        updated = {
            **existing.model_dump(),
            "name": registration.name,
            "platform": registration.platform,
            "architecture": registration.architecture,
            "runner_version": registration.runner_version,
            "status": NodeStatus.ONLINE,
            "capabilities": list(registration.capabilities),
            "labels": registration.labels or {},
            "last_seen_at": now,
            "updated_at": now,
        }
        return await self._repository.save(Node.model_validate(updated)), False

    async def heartbeat(self, node_id: str, heartbeat: NodeHeartbeat) -> Node:
        if heartbeat.status not in {NodeStatus.ONLINE, NodeStatus.DEGRADED}:
            raise ValueError("heartbeat status must be online or degraded")
        node = await self._require(node_id)
        now = self._clock()
        updates: dict[str, object] = {
            "status": heartbeat.status,
            "last_seen_at": now,
            "updated_at": now,
        }
        if heartbeat.capabilities is not None:
            updates["capabilities"] = list(heartbeat.capabilities)
        if heartbeat.labels is not None:
            updates["labels"] = heartbeat.labels
        if heartbeat.runner_version is not None:
            updates["runner_version"] = heartbeat.runner_version
        return await self._repository.save(Node.model_validate({**node.model_dump(), **updates}))

    async def disconnect(self, node_id: str) -> Node:
        node = await self._require(node_id)
        if node.status in {NodeStatus.OFFLINE, NodeStatus.LOST}:
            return node
        return await self._repository.save(
            Node.model_validate(
                {
                    **node.model_dump(),
                    "status": NodeStatus.OFFLINE,
                    "updated_at": self._clock(),
                }
            )
        )

    async def get(self, node_id: str) -> Node:
        await self.refresh_liveness()
        return await self._require(node_id)

    async def list(self, *, status: NodeStatus | None = None) -> Sequence[Node]:
        await self.refresh_liveness()
        return await self._repository.list(status=status)

    async def refresh_liveness(self) -> int:
        now = self._clock()
        changed = 0
        for node in await self._repository.list():
            target = self._liveness_status(node, now)
            if target is node.status:
                continue
            updated = Node.model_validate(
                {**node.model_dump(), "status": target, "updated_at": now}
            )
            await self._repository.save(updated)
            changed += 1
        return changed

    def _liveness_status(self, node: Node, now: datetime) -> NodeStatus:
        if node.last_seen_at is None:
            return NodeStatus.UNKNOWN
        age = now - node.last_seen_at
        if age >= self._lost_after:
            return NodeStatus.LOST
        if age >= self._offline_after:
            return NodeStatus.OFFLINE
        return node.status

    async def _require(self, node_id: str) -> Node:
        node = await self._repository.get(node_id)
        if node is None:
            raise EntityNotFoundError("Node", node_id)
        return node
