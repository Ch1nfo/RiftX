"""Fail-open Context Source for scope-filtered long-term Memory retrieval."""

from __future__ import annotations

from riftx.context.items import ContextItem, ContextItemKind, ContextLayer
from riftx.runtime.lifecycle import ContextCompileRequest

from .models import MemoryRetrievalScope
from .service import MemoryService


class RetrievedMemoryContextSource:
    def __init__(self, service: MemoryService, *, limit: int = 10) -> None:
        self._service = service
        self._limit = limit

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        try:
            memories = await self._service.retrieve(
                request.input_text or request.objective,
                scope=_scope_from_request(request),
                limit=self._limit,
            )
        except Exception:
            return []
        return [
            ContextItem(
                id=f"memory:{memory.id}",
                layer=ContextLayer.RETRIEVED_MEMORY,
                kind=ContextItemKind.RETRIEVED_MEMORY,
                content={
                    "memory_id": memory.id,
                    "memory_type": memory.memory_type.value,
                    "scope_type": memory.scope_type.value,
                    "scope_id": memory.scope_id,
                    "summary": memory.summary,
                    "content": memory.content,
                    "source_refs": memory.source_refs,
                    "pinned": memory.pinned,
                },
                priority=100 if memory.pinned else round(50 + memory.importance * 40),
                source_refs=[f"memory://{memory.id}", *memory.source_refs],
                relevance=max(memory.confidence, memory.importance),
                metadata={"memory_id": memory.id},
            )
            for memory in memories
        ]


def _scope_from_request(request: ContextCompileRequest) -> MemoryRetrievalScope:
    contract = request.run_contract
    engagement_id = _string(contract.get("engagement_id"))
    asset_ids: list[str] = []
    scope = contract.get("scope")
    if isinstance(scope, dict):
        for key in ("cidrs", "ips", "domains", "url_prefixes", "asset_tags"):
            values = scope.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and value:
                        asset_ids.append(
                            f"{engagement_id}::{value}" if engagement_id else value
                        )
    return MemoryRetrievalScope(
        user_id=_string(contract.get("user_id")),
        node_id=_string(contract.get("node_id")),
        workspace_id=request.workspace_path,
        run_id=request.run_id,
        engagement_id=engagement_id,
        asset_ids=asset_ids,
        tool_ids=_strings(contract.get("tool_ids")),
        skill_ids=_strings(contract.get("skill_ids")),
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
