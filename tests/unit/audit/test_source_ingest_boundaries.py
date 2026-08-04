from __future__ import annotations

import ast
import sys
from pathlib import Path


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_control_plane_does_not_import_source_ingest_runtime_or_worker() -> None:
    forbidden = ("riftx.audit.source_ingest", "riftx.audit_worker")
    violations: list[str] = []
    for root in (
        Path("src/riftx/api"),
        Path("src/riftx/application"),
        Path("src/riftx/persistence"),
        Path("src/riftx/temporal"),
    ):
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = (node.module,)
                else:
                    continue
                for name in names:
                    if name == forbidden[1] or name.startswith(f"{forbidden[1]}."):
                        violations.append(f"{path}: {name}")
                    if name == forbidden[0] or name.startswith(f"{forbidden[0]}."):
                        violations.append(f"{path}: {name}")

    assert not violations, "\n".join(violations)


def test_capsule_git_worker_has_only_standard_library_imports() -> None:
    worker = Path("src/riftx/audit_worker/preflight.py")
    non_stdlib = _import_roots(worker) - sys.stdlib_module_names
    assert not non_stdlib, sorted(non_stdlib)
