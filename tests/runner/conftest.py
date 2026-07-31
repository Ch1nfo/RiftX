"""Runner-specific fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from riftx.executors import LinuxCgroupV2Manager

from ._containment_support import FakeKernelContainmentManager


@pytest.fixture(autouse=True)
def explicit_fake_kernel_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject test-only affirmative containment into Runner state tests.

    Individual uncontained safety tests pass ``autodetect_containment=False``;
    real containment tests pass an explicit Linux manager.  There is no
    production/PYTEST environment branch in executor code.
    """

    if os.name != "posix":
        return
    manager = FakeKernelContainmentManager(tmp_path / "fake-kernel-containment")
    monkeypatch.setattr(
        LinuxCgroupV2Manager,
        "autodetect",
        classmethod(lambda cls, **kwargs: manager),
    )
