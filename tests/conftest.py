"""Shared pytest configuration for RiftX."""

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
    """Give host-process tests explicit, test-only stop evidence.

    Production code has no pytest or environment shortcut. Safety tests that
    exercise the uncontained fail-closed path pass ``autodetect_containment=False``;
    real Linux qualification tests pass an explicit cgroup-v2 manager.
    """

    if os.name != "posix":
        return
    manager = FakeKernelContainmentManager(tmp_path / "fake-kernel-containment")
    monkeypatch.setattr(
        LinuxCgroupV2Manager,
        "autodetect",
        classmethod(lambda cls, **kwargs: manager),
    )
