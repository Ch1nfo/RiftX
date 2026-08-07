from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_CONTAINER_RUNTIME_PACKAGES = frozenset(
    {
        "docker",
        "docker-py",
        "dockerode",
        "podman",
        "podman-py",
        "python-on-whales",
        "testcontainers",
    }
)
_CONTAINER_ASSET_NAMES = frozenset(
    {
        ".dockerignore",
        "compose.yaml",
        "compose.yml",
        "containerfile",
        "docker-compose.yaml",
        "docker-compose.yml",
        "dockerfile",
    }
)


def test_distribution_declares_no_docker_runtime_contract() -> None:
    python_manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = python_manifest["project"]
    python_requirements = list(project.get("dependencies", ()))
    for requirements in project.get("optional-dependencies", {}).values():
        python_requirements.extend(requirements)

    javascript_dependencies: set[str] = set()
    for path in (ROOT / "package.json", *(ROOT / "apps").glob("*/package.json")):
        package = json.loads(path.read_text(encoding="utf-8"))
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            javascript_dependencies.update(package.get(field, {}))

    declared = {_python_distribution_name(item) for item in python_requirements}
    declared.update(name.casefold() for name in javascript_dependencies)
    assert declared.isdisjoint(_CONTAINER_RUNTIME_PACKAGES)

    deployment_assets = [
        path
        for root in (
            ROOT,
            ROOT / "apps",
            ROOT / "configs",
            ROOT / "migrations",
            ROOT / "packaging",
            ROOT / "scripts",
            ROOT / "src",
        )
        for path in (root.iterdir() if root == ROOT else root.rglob("*"))
        if path.is_file() and path.name.casefold() in _CONTAINER_ASSET_NAMES
    ]
    assert deployment_assets == []


def _python_distribution_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", requirement)
    assert match is not None, requirement
    return match.group(0).replace("_", "-").casefold()
