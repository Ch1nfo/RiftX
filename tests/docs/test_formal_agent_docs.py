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

PLAN_STAGE_ROW = re.compile(
    r"^\| ([A-E])\. ([^|]+?) \| ([^|]+?) \| ([^|]+?) \|$",
    re.MULTILINE,
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def test_formal_agent_document_links_resolve() -> None:
    for document in AUTHORITATIVE_DOCUMENTS:
        assert document.is_file(), f"missing authoritative document: {document}"
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", maxsplit=1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            assert resolved.exists(), f"broken link in {document}: {raw_target}"


def test_plan_and_ledger_share_the_current_delivery_route() -> None:
    plan_text = PLAN.read_text(encoding="utf-8")
    ledger_text = LEDGER.read_text(encoding="utf-8")
    stages = PLAN_STAGE_ROW.findall(plan_text)

    assert [stage_id for stage_id, _name, _status, _result in stages] == list("ABCDE")
    assert [status.strip() for _stage_id, _name, status, _result in stages] == [
        "completed",
        "completed",
        "completed",
        "completed",
        "in progress；E1 当前施工",
    ]
    assert "### 7.8 D2：复盘与人工改进（completed）" in plan_text
    assert "- Stage：`Pentest-first R1 — Stage E 默认产品面收缩与发布`" in ledger_text
    assert "- Current task：`E1 — 默认产品入口与 Quickstart 单路径`" in ledger_text
    assert "a929fdb4" in plan_text
    assert "a929fdb4" in ledger_text


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
