from __future__ import annotations

from pathlib import Path

import pytest

from riftx.context import (
    ContextCategory,
    ContextCompiler,
    StableInstructionLoadError,
    StableInstructionSource,
)
from riftx.runtime.lifecycle import ContextCompileRequest, MinimalContextCompiler


def _write_instruction(root: Path, content: str) -> Path:
    path = root / ".riftx" / "RIFTX.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _request(
    *,
    engagement: Path | None = None,
    workspace: Path | None = None,
    current: Path | None = None,
) -> ContextCompileRequest:
    return ContextCompileRequest(
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        model_profile="test-model",
        objective="Inspect the authorized target",
        run_contract={
            "objective": "Inspect the authorized target",
            "scope": {"ips": ["192.0.2.10"], "exclusions": []},
        },
        engagement_path=str(engagement) if engagement is not None else None,
        workspace_path=str(workspace) if workspace is not None else None,
        current_path=str(current) if current is not None else None,
        input_text="Continue",
    )


async def test_loads_four_instruction_layers_in_specificity_order(tmp_path: Path) -> None:
    config = tmp_path / "config"
    engagement = tmp_path / "engagement"
    workspace = engagement / "workspace"
    current = workspace / "services" / "api"
    global_path = config / "riftx" / "RIFTX.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("GLOBAL-RULE", encoding="utf-8")
    engagement_path = _write_instruction(engagement, "ENGAGEMENT-RULE")
    workspace_path = _write_instruction(workspace, "WORKSPACE-RULE")
    current_path = _write_instruction(current, "CURRENT-RULE")
    source = StableInstructionSource(
        environment={"XDG_CONFIG_HOME": str(config)},
        max_tokens=1000,
    )

    compiled = await ContextCompiler(stable_instruction_source=source).compile(
        _request(engagement=engagement, workspace=workspace, current=current)
    )

    instructions = compiled.system_instructions
    assert instructions.index("GLOBAL-RULE") < instructions.index("ENGAGEMENT-RULE")
    assert instructions.index("ENGAGEMENT-RULE") < instructions.index("WORKSPACE-RULE")
    assert instructions.index("WORKSPACE-RULE") < instructions.index("CURRENT-RULE")
    assert compiled.context_manifest["instruction_scopes"] == [
        "global",
        "engagement",
        "workspace",
        "current_path",
    ]
    stable_usage = compiled.context_manifest["categories"][
        ContextCategory.STABLE_INSTRUCTIONS.value
    ]
    assert stable_usage["source_refs"] == [
        global_path.resolve().as_uri(),
        engagement_path.resolve().as_uri(),
        workspace_path.resolve().as_uri(),
        current_path.resolve().as_uri(),
    ]
    assert stable_usage["estimated_tokens"] <= 1000


async def test_budget_preserves_more_specific_instructions_first(tmp_path: Path) -> None:
    config = tmp_path / "config"
    global_path = config / "riftx" / "RIFTX.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("GLOBAL-RULE\n" + "G" * 20_000, encoding="utf-8")
    workspace = tmp_path / "workspace"
    current = workspace / "src"
    _write_instruction(current, "CURRENT-MUST-WIN\n" + "C" * 20_000)
    source = StableInstructionSource(
        environment={"XDG_CONFIG_HOME": str(config)},
        max_tokens=160,
    )

    compiled = await ContextCompiler(stable_instruction_source=source).compile(
        _request(workspace=workspace, current=current)
    )

    assert "CURRENT-MUST-WIN" in compiled.system_instructions
    assert "GLOBAL-RULE" not in compiled.system_instructions
    assert compiled.context_manifest["truncated_instruction_paths"] == [
        str(current / ".riftx" / "RIFTX.md")
    ]
    assert compiled.context_manifest["dropped_instruction_paths"] == [str(global_path.resolve())]
    stable_usage = compiled.context_manifest["categories"][
        ContextCategory.STABLE_INSTRUCTIONS.value
    ]
    assert stable_usage["estimated_tokens"] <= 160


async def test_duplicate_workspace_and_current_path_load_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    instruction_path = _write_instruction(workspace, "ONE-COPY")
    source = StableInstructionSource(global_path=tmp_path / "missing.md")

    item = (await source.load(_request(workspace=workspace, current=workspace)))[0]

    assert item.metadata["instruction_scopes"] == ["current_path"]
    assert item.source_refs == [instruction_path.resolve().as_uri()]
    assert str(item.content).count("ONE-COPY") == 1


async def test_current_path_cannot_escape_workspace(tmp_path: Path) -> None:
    source = StableInstructionSource(global_path=tmp_path / "missing.md")

    with pytest.raises(StableInstructionLoadError, match="outside workspace_path"):
        await source.load(
            _request(
                workspace=tmp_path / "workspace",
                current=tmp_path / "outside",
            )
        )


async def test_workspace_cannot_escape_engagement(tmp_path: Path) -> None:
    source = StableInstructionSource(global_path=tmp_path / "missing.md")

    with pytest.raises(StableInstructionLoadError, match="outside engagement_path"):
        await source.load(
            _request(
                engagement=tmp_path / "engagement",
                workspace=tmp_path / "outside",
            )
        )


async def test_instruction_symlink_cannot_escape_its_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    instruction_dir = workspace / ".riftx"
    instruction_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE", encoding="utf-8")
    (instruction_dir / "RIFTX.md").symlink_to(outside)
    source = StableInstructionSource(global_path=tmp_path / "missing.md")

    with pytest.raises(StableInstructionLoadError, match="escapes its configured root"):
        await source.load(_request(workspace=workspace))


async def test_runtime_compatibility_compiler_loads_global_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / "config"
    global_path = config / "riftx" / "RIFTX.md"
    global_path.parent.mkdir(parents=True)
    global_path.write_text("GLOBAL-RUNTIME-RULE", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    compiled = await MinimalContextCompiler().compile(_request())

    assert "GLOBAL-RUNTIME-RULE" in compiled.system_instructions
    assert compiled.context_manifest["instruction_paths"] == [str(global_path.resolve())]
