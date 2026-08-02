from __future__ import annotations

from pathlib import Path

import pytest

from riftx.executors.containment import (
    LinuxCgroupV2Containment,
    LinuxCgroupV2Manager,
    ProcessContainmentTerminationError,
    ProcessContainmentUnavailableError,
    _execution_digest,
)


def _materialize_boundary(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    (path / "cgroup.procs").write_text("", encoding="ascii")
    (path / "cgroup.kill").write_text("", encoding="ascii")
    (path / "cgroup.max.descendants").write_text("0\n", encoding="ascii")


async def test_containment_identifier_binds_exact_root_and_leaf_kernel_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "delegated"
    leaf = root / "riftx-boundary"
    _materialize_boundary(leaf)
    original = LinuxCgroupV2Containment(path=leaf, digest="a" * 64)
    original_identifier = original.identifier

    # Keep the original inode alive under another name so replacement cannot
    # accidentally reuse it during this assertion.
    old_leaf = root / "old-boundary"
    leaf.rename(old_leaf)
    _materialize_boundary(leaf)
    replacement = LinuxCgroupV2Containment(path=leaf, digest="a" * 64)

    assert replacement.identifier != original_identifier
    assert original.boundary_exists() is False
    with pytest.raises(ProcessContainmentTerminationError, match="identity changed"):
        await original.is_populated()


def test_recovery_identifier_is_unavailable_when_boundary_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "delegated"
    root.mkdir()
    manager = LinuxCgroupV2Manager(root, verify_filesystem=False)
    containment = manager.containment_for("missing-boundary")

    assert not containment.path.exists()
    with pytest.raises(ProcessContainmentUnavailableError, match="is unavailable"):
        _ = containment.identifier


def test_same_execution_key_in_replaced_root_never_reuses_durable_identity(
    tmp_path: Path,
) -> None:
    key = "execution-key"
    digest = _execution_digest(key)
    root = tmp_path / "delegated"
    leaf = root / f"riftx-{digest}"
    _materialize_boundary(leaf)
    original = LinuxCgroupV2Containment(path=leaf, digest=digest)
    original_identifier = original.identifier

    old_root = tmp_path / "old-delegated"
    root.rename(old_root)
    _materialize_boundary(leaf)
    replacement = LinuxCgroupV2Manager(
        root,
        verify_filesystem=False,
    ).containment_for(key)

    assert replacement.identifier != original_identifier
    assert original.boundary_exists() is False
