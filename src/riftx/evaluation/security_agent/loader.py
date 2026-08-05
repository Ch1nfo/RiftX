"""Load and verify immutable local security evaluation scenarios."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .models import EvaluationModel, ResetReceipt, SecurityScenario, aware_datetime


class ScenarioLoadError(ValueError):
    """Raised when a scenario or its immutable fixture fails validation."""


@dataclass(frozen=True, slots=True)
class LoadedScenario:
    scenario: SecurityScenario
    manifest_path: Path
    fixture_path: Path
    fixture_digest: str
    scenario_digest: str


def canonical_json(model: EvaluationModel) -> str:
    """Serialize one evaluation model without platform-dependent formatting."""

    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_digest(model: EvaluationModel) -> str:
    return hashlib.sha256(canonical_json(model).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


class SecurityScenarioLoader:
    """Load public or sealed manifests while keeping fixture access root-bound."""

    def __init__(self, benchmark_root: Path) -> None:
        resolved_root = benchmark_root.resolve()
        if not resolved_root.is_dir():
            raise ScenarioLoadError(f"benchmark root is not a directory: {benchmark_root}")
        self._root = resolved_root

    @property
    def benchmark_root(self) -> Path:
        return self._root

    def load_all(self) -> tuple[LoadedScenario, ...]:
        manifests = sorted(self._root.glob("**/scenario.yaml"))
        if not manifests:
            raise ScenarioLoadError("benchmark root contains no scenario.yaml manifests")
        loaded = tuple(self.load(path) for path in manifests)
        scenario_ids = [item.scenario.scenario_id for item in loaded]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ScenarioLoadError("scenario IDs must be unique within one benchmark root")
        return loaded

    def load(self, manifest_path: Path) -> LoadedScenario:
        manifest = self._regular_file_within_root(manifest_path, label="scenario manifest")
        if manifest.name != "scenario.yaml":
            raise ScenarioLoadError("scenario manifest must be a regular scenario.yaml file")
        try:
            raw: Any = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            scenario = SecurityScenario.model_validate(raw)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            raise ScenarioLoadError(f"invalid scenario manifest {manifest}: {exc}") from exc

        fixture = self._regular_file_within_root(
            manifest.parent / scenario.target.fixture_path,
            label="scenario fixture",
        )
        observed_digest = file_digest(fixture)
        if observed_digest != scenario.target.snapshot_digest:
            raise ScenarioLoadError("scenario fixture does not match target snapshot digest")
        if observed_digest != scenario.reset.expected_digest:
            raise ScenarioLoadError("scenario fixture does not match reset digest")
        return LoadedScenario(
            scenario=scenario,
            manifest_path=manifest,
            fixture_path=fixture,
            fixture_digest=observed_digest,
            scenario_digest=canonical_digest(scenario),
        )

    def reset(self, loaded: LoadedScenario, *, reset_at: datetime) -> ResetReceipt:
        aware_datetime(reset_at)
        current_digest = file_digest(loaded.fixture_path)
        if current_digest != loaded.fixture_digest:
            raise ScenarioLoadError("immutable fixture changed and cannot be reset safely")
        return ResetReceipt(
            scenario_id=loaded.scenario.scenario_id,
            strategy=loaded.scenario.reset.strategy,
            fixture_digest=current_digest,
            reset_at=reset_at,
        )

    def _within_root(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self._root):
            raise ScenarioLoadError(f"path escapes benchmark root: {path}")
        return resolved

    def _regular_file_within_root(self, path: Path, *, label: str) -> Path:
        lexical = Path(os.path.abspath(path))
        if not lexical.is_relative_to(self._root):
            raise ScenarioLoadError(f"{label} escapes benchmark root: {path}")
        current = self._root
        for part in lexical.relative_to(self._root).parts:
            current /= part
            if current.is_symlink():
                raise ScenarioLoadError(f"{label} cannot use symbolic links: {path}")
        resolved = self._within_root(lexical)
        if not resolved.is_file():
            raise ScenarioLoadError(f"{label} must be a regular file: {path}")
        return resolved
