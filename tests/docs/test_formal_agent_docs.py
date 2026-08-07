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
RELEASE_CHECK = REPOSITORY_ROOT / "docs/pentest-r1-release-check.md"
README = REPOSITORY_ROOT / "README.md"
README_ZH = REPOSITORY_ROOT / "README_ZH.md"
AUTHORITATIVE_DOCUMENTS = (PLAN, ADR, PENTEST_ADR, LEDGER, RELEASE_CHECK)

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
        "completed",
    ]
    assert "### 7.8 D2：复盘与人工改进（completed）" in plan_text
    assert "- Stage：`Pentest-first R1 — Stage E 默认产品面收缩与发布`" in ledger_text
    assert "- Current task：`E4 — Pentest R1 发布门`" in ledger_text
    assert "- Status：`completed`" in ledger_text
    assert "013d1e3a" in plan_text
    assert "013d1e3a" in ledger_text
    release_text = RELEASE_CHECK.read_text(encoding="utf-8")
    assert "5383 passed, 5 skipped" in release_text
    assert "Stage E 可以标记为 completed" in release_text


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


def test_readmes_expose_one_pentest_first_quickstart() -> None:
    contracts = (
        (
            README.read_text(encoding="utf-8"),
            "## Pentest-first quick start",
            "## Why RiftX",
            "The Code Audit product surface is retired.",
        ),
        (
            README_ZH.read_text(encoding="utf-8"),
            "## Pentest-first 快速开始",
            "## 为什么选择 RiftX",
            "Code Audit 产品面已退役。",
        ),
    )
    for text, start_heading, end_heading, retirement_label in contracts:
        assert text.index(start_heading) < text.index(end_heading)
        quickstart = text.split(start_heading, maxsplit=1)[1].split(
            end_heading,
            maxsplit=1,
        )[0]
        for command in (
            "riftx onboard",
            "riftx doctor",
            "riftx serve",
            "riftx worker",
            "riftx pentest start",
            "riftx approvals",
            "riftx report generate",
            "riftx skills",
        ):
            assert command in quickstart
        assert "riftx run create" not in quickstart
        assert retirement_label in text
        assert "apps/demo" not in text
        assert "frozen/experimental" not in text
