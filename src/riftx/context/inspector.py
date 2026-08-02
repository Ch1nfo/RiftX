"""Application service for durable Context inspection and usage backfill."""

from __future__ import annotations

from collections.abc import Mapping

from riftx.application.errors import EntityNotFoundError

from .manifest import (
    ContextCompilation,
    ContextCompilationRepository,
    usage_token_counts,
)


class ContextApplicationService:
    def __init__(self, repository: ContextCompilationRepository) -> None:
        self._repository = repository

    async def create(self, compilation: ContextCompilation) -> ContextCompilation:
        return await self._repository.create(compilation)

    async def get(self, compilation_id: str) -> ContextCompilation:
        compilation = await self._repository.get(compilation_id)
        if compilation is None:
            raise EntityNotFoundError("ContextCompilation", compilation_id)
        return compilation

    async def latest_for_session(self, session_id: str) -> ContextCompilation:
        compilation = await self._repository.latest_for_session(session_id)
        if compilation is None:
            raise EntityNotFoundError("ContextCompilation for Session", session_id)
        return compilation

    async def latest_for_run(self, run_id: str) -> ContextCompilation:
        compilation = await self._repository.latest_for_run(run_id)
        if compilation is None:
            raise EntityNotFoundError("ContextCompilation for Run", run_id)
        return compilation

    async def record_usage(
        self,
        compilation_id: str,
        usage: Mapping[str, object],
    ) -> ContextCompilation:
        actual_input_tokens, actual_output_tokens = usage_token_counts(usage)
        return await self._repository.update_usage(
            compilation_id,
            actual_input_tokens=actual_input_tokens,
            actual_output_tokens=actual_output_tokens,
        )
