from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLAN = REPOSITORY_ROOT / "RiftX_正式版_开发优化文档.md"
ADR = (
    REPOSITORY_ROOT
    / "docs/architecture/decisions/0012-riftx-formal-security-agent-platform-boundaries.md"
)
PENTEST_ADR = (
    REPOSITORY_ROOT
    / "docs/architecture/decisions/0013-riftx-pentest-run-admission-and-attack-surface.md"
)
LEDGER = REPOSITORY_ROOT / "docs/implementation/FORMAL_AGENT_PROGRESS.md"
AUTHORITATIVE_DOCUMENTS = (PLAN, ADR, PENTEST_ADR, LEDGER)

TASK_HEADING = re.compile(r"^### ([A-Z]+-\d+)：", re.MULTILINE)
DEPENDENCY_LINE = re.compile(r"\*\*依赖\*\*：(.+?)。")
LEDGER_ROW = re.compile(
    r"^\| ([A-Z]+-\d+) \| ([^|]+?) \| "
    r"(pending|in_progress|blocked|completed) \| ([^|]+?) \|$",
    re.MULTILINE,
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _task_blocks(plan_text: str) -> dict[str, str]:
    matches = list(TASK_HEADING.finditer(plan_text))
    return {
        match.group(1): plan_text[
            match.end() : matches[index + 1].start() if index + 1 < len(matches) else None
        ]
        for index, match in enumerate(matches)
    }


def _dependencies(value: str) -> frozenset[str]:
    normalized = value.strip().rstrip("。").strip()
    if normalized.lower() in {"none", "无"}:
        return frozenset()
    return frozenset(
        item.strip() for item in re.split(r"[、,]", normalized) if item.strip()
    )


def _ledger_rows(ledger_text: str) -> dict[str, tuple[frozenset[str], str, str]]:
    task_table = ledger_text.split("## 7. Task status", maxsplit=1)[1].split(
        "## 8. Task records", maxsplit=1
    )[0]
    return {
        task_id: (_dependencies(dependency), status, commit.strip())
        for task_id, dependency, status, commit in LEDGER_ROW.findall(task_table)
    }


def test_formal_agent_document_links_resolve() -> None:
    for document in AUTHORITATIVE_DOCUMENTS:
        assert document.is_file(), f"missing authoritative document: {document}"
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            assert resolved.exists(), f"broken link in {document}: {raw_target}"


def test_plan_and_ledger_have_the_same_explicit_task_graph() -> None:
    plan_text = PLAN.read_text(encoding="utf-8")
    ledger_text = LEDGER.read_text(encoding="utf-8")
    blocks = _task_blocks(plan_text)
    rows = _ledger_rows(ledger_text)

    assert blocks
    assert set(rows) == set(blocks)

    plan_dependencies: dict[str, frozenset[str]] = {}
    for task_id, block in blocks.items():
        match = DEPENDENCY_LINE.search(block)
        assert match is not None, f"{task_id} has no explicit dependency declaration"
        plan_dependencies[task_id] = _dependencies(match.group(1))

    ledger_dependencies = {
        task_id: dependency for task_id, (dependency, _status, _commit) in rows.items()
    }
    assert ledger_dependencies == plan_dependencies

    known_tasks = set(blocks)
    for task_id, dependencies in plan_dependencies.items():
        assert task_id not in dependencies
        assert dependencies <= known_tasks, (
            f"{task_id} references unknown dependencies: {dependencies - known_tasks}"
        )


def test_formal_task_dependency_graph_is_acyclic() -> None:
    blocks = _task_blocks(PLAN.read_text(encoding="utf-8"))
    graph = {
        task_id: _dependencies(DEPENDENCY_LINE.search(block).group(1))
        for task_id, block in blocks.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        assert task_id not in visiting, f"task dependency cycle includes {task_id}"
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)


def test_adr_freezes_all_workload_and_system_boundaries() -> None:
    adr_text = ADR.read_text(encoding="utf-8")
    for required_text in (
        "Security Capability System",
        "Evidence-driven Cognitive Runtime",
        "Capability Learning Flywheel",
        "General Run",
        "Pentest Run",
        "Code Audit Run",
        "数据迁移顺序",
        "Evaluation 定位与基线记录",
    ):
        assert required_text in adr_text
