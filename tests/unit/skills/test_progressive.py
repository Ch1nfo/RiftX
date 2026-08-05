from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from riftx.runtime.lifecycle import ContextCompileRequest, DynamicToolContextCompiler
from riftx.skills import (
    ProgressiveSkillContextManager,
    ProgressiveSkillRegistry,
    SkillDocumentError,
    SkillRegistry,
)
from riftx.tools import ToolContextManager, ToolRegistry

_REQUIRED_BODY = """
## When to use
Use for a confirmed candidate.

## Preconditions
Authorized target and a candidate URL.

## Procedure
Run the selected HTTP tool through the dispatcher.

## Decision points
Stop when the candidate is disproven.

## Stop conditions
Stop after reproducible evidence or a conclusive negative result.

## Expected output
A bounded evidence summary.

## Error handling
Record the error and preserve raw artifacts.
"""


def _write_skill(
    root: Path,
    *,
    body: str = _REQUIRED_BODY,
    front_matter: str | None = None,
) -> Path:
    directory = root / "ssrf-validation"
    (directory / "schemas").mkdir(parents=True)
    front = front_matter or """---
name: ssrf-validation
description: Validate SSRF candidates with reproducible evidence
version: 1
required_capabilities:
  - http_request
preferred_tools:
  - curl
approval_level: sensitive
---
"""
    (directory / "SKILL.md").write_text(front + body)
    (directory / "REFERENCES.md").write_text("REFERENCE SENTINEL")
    (directory / "schemas" / "input.json").write_text(
        json.dumps({"type": "object", "properties": {"url": {"type": "string"}}})
    )
    (directory / "schemas" / "output.json").write_text(
        json.dumps({"type": "object", "properties": {"evidence": {"type": "array"}}})
    )
    return directory


def test_progressive_skill_loads_metadata_body_and_references_in_stages(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    catalog = ProgressiveSkillRegistry(tmp_path)

    summaries = catalog.list_summaries()

    assert summaries[0].model_dump(mode="json") == {
        "id": "ssrf-validation",
        "name": "ssrf-validation",
        "description": "Validate SSRF candidates with reproducible evidence",
        "version": "1",
        "digest": summaries[0].digest,
        "source": "operator",
        "required_capabilities": ["http_request"],
    }
    assert len(summaries[0].digest) == 64
    assert catalog.loaded_document_ids == frozenset()
    assert catalog.loaded_reference_ids == frozenset()

    document = catalog.load_document("ssrf-validation")
    assert "## Procedure" in document.content
    assert document.sections["Stop conditions"].startswith("Stop after")
    assert document.input_schema == {
        "type": "object",
        "properties": {"url": {"type": "string"}},
    }
    assert catalog.loaded_document_ids == frozenset({"ssrf-validation"})
    assert catalog.loaded_reference_ids == frozenset()

    references = catalog.load_references("ssrf-validation")
    assert references.content == "REFERENCE SENTINEL"
    assert catalog.loaded_reference_ids == frozenset({"ssrf-validation"})


def test_skill_registry_integrates_progressive_search_and_selection(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)

    results = registry.search_skill_documents(
        "validate candidate", capability="http request"
    )
    selected = registry.load_skill_document(results[0].skill.id)

    assert results[0].skill.id == "ssrf-validation"
    assert selected.approval_level.value == "sensitive"
    assert registry.load_skill_references(selected.id).content == "REFERENCE SENTINEL"


@pytest.mark.parametrize(
    "front_matter",
    [
        "---\nname: missing-description\n---\n",
        "---\nname: bad-extra\ndescription: demo\nunknown: true\n---\n",
        "name: no-delimiter\ndescription: demo\n",
    ],
)
def test_skill_front_matter_validation_rejects_invalid_documents(
    tmp_path: Path,
    front_matter: str,
) -> None:
    _write_skill(tmp_path, front_matter=front_matter)

    with pytest.raises(SkillDocumentError):
        ProgressiveSkillRegistry(tmp_path).refresh()


def test_skill_body_validation_is_deferred_until_selection(tmp_path: Path) -> None:
    body = _REQUIRED_BODY.replace(
        "## Stop conditions\nStop after reproducible evidence or a conclusive negative result.\n",
        "",
    )
    _write_skill(tmp_path, body=body)
    catalog = ProgressiveSkillRegistry(tmp_path)

    assert catalog.list_summaries()[0].id == "ssrf-validation"
    with pytest.raises(SkillDocumentError, match="Stop conditions"):
        catalog.load_document("ssrf-validation")


def test_skill_hot_reload_invalidates_loaded_content(tmp_path: Path) -> None:
    directory = _write_skill(tmp_path)
    catalog = ProgressiveSkillRegistry(tmp_path)
    first = catalog.load_document("ssrf-validation")
    original_generation = catalog.generation
    path = directory / "SKILL.md"
    path.write_text(path.read_text().replace("A bounded evidence summary.", "Updated evidence."))

    second = catalog.load_document("ssrf-validation")

    assert catalog.generation == original_generation + 1
    assert first.content != second.content
    assert second.sections["Expected output"] == "Updated evidence."


async def test_context_compiler_exposes_skill_content_progressively(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    _write_skill(skill_root)
    skill_registry = SkillRegistry(skill_root)
    skill_context = ProgressiveSkillContextManager(skill_registry)
    tool_config = tmp_path / "tools.yaml"
    tool_config.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {"curl": {"command": [sys.executable], "capabilities": ["http_request"]}},
            }
        )
    )
    tool_registry = ToolRegistry(tool_config, node_id="node-1")
    await tool_registry.refresh()
    compiler = DynamicToolContextCompiler(
        ToolContextManager(tool_registry),
        skill_context,
    )
    request = ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="test-model",
    )

    initial = await compiler.compile(request)
    assert len(initial.available_skills) == 1
    assert initial.available_skills[0] == {
        "id": "ssrf-validation",
        "name": "ssrf-validation",
        "description": "Validate SSRF candidates with reproducible evidence",
        "version": "1",
        "digest": initial.available_skills[0]["digest"],
        "source": "operator",
        "required_capabilities": ["http_request"],
    }
    assert initial.loaded_skill_documents == []
    assert initial.loaded_skill_references == []

    await skill_context.select_skill(
        "ssrf-validation",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    selected = await compiler.compile(request)
    assert selected.loaded_skill_documents[0]["id"] == "ssrf-validation"
    assert "## Procedure" in selected.loaded_skill_documents[0]["content"]
    assert selected.loaded_skill_references == []

    await skill_context.load_references(
        "ssrf-validation",
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
    )
    with_references = await compiler.compile(request)
    assert with_references.loaded_skill_references == [
        {
            "skill_id": "ssrf-validation",
            "version": "1",
            "digest": initial.available_skills[0]["digest"],
            "source": "operator",
            "content": "REFERENCE SENTINEL",
        }
    ]
