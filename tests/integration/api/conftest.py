"""API integration fixtures for explicit host-process containment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.runner._containment_support import FakeKernelContainmentManager

from riftx.executors import LinuxCgroupV2Manager


@pytest.fixture(autouse=True)
def explicit_fake_kernel_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give ordinary API lifecycle tests affirmative test-only stop evidence."""

    if os.name != "posix":
        return
    manager = FakeKernelContainmentManager(tmp_path / "fake-kernel-containment")
    monkeypatch.setattr(
        LinuxCgroupV2Manager,
        "autodetect",
        classmethod(lambda cls, **kwargs: manager),
    )
