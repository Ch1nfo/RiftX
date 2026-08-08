from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from tests.integration.api.test_control_plane import (
    RuntimeFixture,
    _build_runtime,
    _client,
    _pentest_request,
)

from riftx.capability_management import (
    activate_operator_skill,
    disable_operator_skill,
    register_operator_skill,
)
from riftx.config import RiftXConfig
from riftx.database_maintenance import repair_sqlite_database
from riftx.domain import RunStatus

_SKILL_ID = "operator-report-review"


def _write_skill(root: Path, *, version: str, guidance: str) -> None:
    directory = root / _SKILL_ID
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"""---
name: Operator Report Review
description: Preserve one bounded verification result ({guidance})
version: {version}
source: operator
required_capabilities:
  - evidence_ledger
preferred_tools:
  - target_http_request
approval_level: always
---

## When to use

Use after an authorized observation needs one bounded verification.

## Preconditions

The target and interaction are inside the current Scope.

## Procedure

Perform one verification and preserve the result.

## Decision points

Treat an execution failure separately from a target negative result.

## Stop conditions

Stop after evidence, a conclusive negative result, or any safety gate.

## Expected output

Return evidence references, the result class, and remaining uncertainty.

## Error handling

Record blocked and failed executions without broadening the interaction.
"""
    )


async def _create_completed_report(runtime: RuntimeFixture, run_id: str) -> dict[str, str]:
    control_plane = runtime.control_plane
    request = _pentest_request(run_id)
    request["capabilities"] = {"skill_ids": [_SKILL_ID]}
    async for client in _client(control_plane):
        created = await client.post("/api/v1/pentests", json=request)
        assert created.status_code == 201, created.text
        await runtime.run_repository.update_status(run_id, RunStatus.RUNNING)
        await runtime.run_repository.update_status(run_id, RunStatus.COMPLETED)
        generated = await client.post(
            f"/api/v1/runs/{run_id}/reports",
            json={"formats": ["json", "markdown"]},
        )
        assert generated.status_code == 201, generated.text
        return {
            str(item["format"]): str(item["content_url"])
            for item in generated.json()["items"]
        }
    raise AssertionError("Control Plane client did not start")


@pytest.mark.asyncio
async def test_operator_skill_reports_pin_v1_while_new_runs_use_v2(tmp_path: Path) -> None:
    database_path = tmp_path / "riftx.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    repair_sqlite_database(database_url, cwd=tmp_path)
    runtime = await _build_runtime(tmp_path, database_path=database_path)
    config = RiftXConfig.model_validate(
        {
            "database": {"url": runtime.control_plane.database.url},
            "skills": {"path": tmp_path / "skills"},
        }
    )
    run_v1 = str(uuid4())
    run_v2 = str(uuid4())
    try:
        _write_skill(config.skills.path, version="1.0.0", guidance="initial method")
        registered_v1 = await asyncio.to_thread(
            register_operator_skill,
            config,
            _SKILL_ID,
            cwd=tmp_path,
        )
        await asyncio.to_thread(
            activate_operator_skill,
            config,
            _SKILL_ID,
            "1.0.0",
            cwd=tmp_path,
        )
        v1_urls = await _create_completed_report(runtime, run_v1)

        _write_skill(config.skills.path, version="2.0.0", guidance="reviewed method")
        registered_v2 = await asyncio.to_thread(
            register_operator_skill,
            config,
            _SKILL_ID,
            cwd=tmp_path,
        )
        await asyncio.to_thread(
            disable_operator_skill,
            config,
            _SKILL_ID,
            "1.0.0",
            cwd=tmp_path,
        )
        await asyncio.to_thread(
            activate_operator_skill,
            config,
            _SKILL_ID,
            "2.0.0",
            cwd=tmp_path,
        )

        old_source = await runtime.control_plane.report_service.build_source(run_v1)
        assert old_source.pentest is not None
        old_selection = next(
            item
            for item in old_source.pentest.capabilities
            if item.capability_id == _SKILL_ID
        )
        assert (old_selection.version, old_selection.digest, old_selection.source) == (
            "1.0.0",
            registered_v1.manifest.provenance.source_digest,
            "operator",
        )

        async for client in _client(runtime.control_plane):
            old_json = await client.get(v1_urls["json"])
            old_markdown = await client.get(v1_urls["markdown"])
            assert old_json.status_code == 200, old_json.text
            assert old_markdown.status_code == 200, old_markdown.text
            old_capability = next(
                item
                for item in old_json.json()["source"]["pentest"]["capabilities"]
                if item["capability_id"] == _SKILL_ID
            )
            assert (old_capability["version"], old_capability["digest"]) == (
                "1.0.0",
                registered_v1.manifest.provenance.source_digest,
            )
            assert "version `1.0.0` from `operator`" in old_markdown.text
            assert "version `2.0.0` from `operator`" not in old_markdown.text

        v2_urls = await _create_completed_report(runtime, run_v2)
        async for client in _client(runtime.control_plane):
            new_json = await client.get(v2_urls["json"])
            new_markdown = await client.get(v2_urls["markdown"])
            assert new_json.status_code == 200, new_json.text
            assert new_markdown.status_code == 200, new_markdown.text
            new_capability = next(
                item
                for item in new_json.json()["source"]["pentest"]["capabilities"]
                if item["capability_id"] == _SKILL_ID
            )
            assert (new_capability["version"], new_capability["digest"]) == (
                "2.0.0",
                registered_v2.manifest.provenance.source_digest,
            )
            assert "version `2.0.0` from `operator`" in new_markdown.text
    finally:
        await runtime.control_plane.close()
