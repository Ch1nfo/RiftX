from riftx.domain import ApprovalLevel, ApprovalMode, requires_approval


def test_auto_mode_never_requires_approval() -> None:
    assert not requires_approval(ApprovalMode.AUTO, ApprovalLevel.ALWAYS)


def test_balanced_mode_only_requires_sensitive_or_always() -> None:
    assert not requires_approval(ApprovalMode.BALANCED, ApprovalLevel.NEVER)
    assert requires_approval(ApprovalMode.BALANCED, ApprovalLevel.SENSITIVE)
    assert requires_approval(ApprovalMode.BALANCED, ApprovalLevel.ALWAYS)


def test_manual_mode_requires_every_effectful_tool_unless_granted() -> None:
    assert requires_approval(ApprovalMode.MANUAL, ApprovalLevel.NEVER)
    assert not requires_approval(
        ApprovalMode.MANUAL,
        ApprovalLevel.ALWAYS,
        granted_for_run=True,
    )
