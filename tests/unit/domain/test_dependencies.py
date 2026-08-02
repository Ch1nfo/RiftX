import ast
from pathlib import Path

FORBIDDEN_IMPORT_ROOTS = {
    "alembic",
    "fastapi",
    "sqlalchemy",
    "temporalio",
    "agents",
}


def test_domain_package_does_not_import_infrastructure() -> None:
    domain_root = Path("src/riftx/domain")
    violations: list[str] = []

    for path in sorted(domain_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            forbidden = roots & FORBIDDEN_IMPORT_ROOTS
            if forbidden:
                violations.append(f"{path}: {sorted(forbidden)}")

    assert not violations, "\n".join(violations)
