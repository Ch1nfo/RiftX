"""Explicit process-containment test doubles for Runner state-machine tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import riftx.executors.process as process_module
from riftx.executors import LinuxCgroupV2Containment
from riftx.executors.containment import _execution_digest


class FakeKernelContainment(LinuxCgroupV2Containment):
    """PGID-backed fake used only where tests need affirmative stop evidence.

    It deliberately does not pretend to cover ``setsid()``.  Production and
    escape coverage uses real Linux cgroup v2 tests; uncontained POSIX tests
    explicitly disable autodetection and assert fail-closed behavior.
    """

    def _leader_pid(self) -> int | None:
        try:
            raw = (self.path / "cgroup.procs").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        first = raw.splitlines()[0] if raw else ""
        return int(first) if first else None

    async def wait_empty(self, timeout_seconds: float | None) -> bool:
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        while True:
            pid = self._leader_pid()
            if pid is None or not process_module._posix_process_group_exists(pid):
                self._confirmed_empty = True
                return True
            if deadline is not None and loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)

    async def is_populated(self) -> bool:
        pid = self._leader_pid()
        return pid is not None and process_module._posix_process_group_exists(pid)

    async def terminate(self, *, grace_seconds: float) -> None:
        pid = self._leader_pid()
        if pid is not None:
            await process_module._terminate_posix_process_group(
                pid,
                grace_seconds=grace_seconds,
            )
        self._confirmed_empty = True

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        await self.terminate(grace_seconds=0.0)

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        if not await self.wait_empty(0):
            raise RuntimeError("fake containment is still populated")
        try:
            children = tuple(self.path.iterdir())
        except FileNotFoundError:
            self._cleaned = True
            return
        for child in children:
            child.unlink(missing_ok=True)
        try:
            self.path.rmdir()
        except FileNotFoundError:
            pass
        self._cleaned = True


class FakeKernelContainmentManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()

    async def prepare(self, execution_key: str) -> FakeKernelContainment:
        containment = self.containment_for(execution_key)
        containment.path.mkdir()
        (containment.path / "cgroup.events").write_text("populated 0\n", encoding="ascii")
        (containment.path / "cgroup.procs").write_text("", encoding="ascii")
        (containment.path / "cgroup.kill").write_text("", encoding="ascii")
        (containment.path / "cgroup.max.descendants").write_text("0\n", encoding="ascii")
        return containment

    def containment_for(self, execution_key: str) -> FakeKernelContainment:
        digest = _execution_digest(execution_key)
        return FakeKernelContainment(
            path=self.root / f"riftx-{digest}",
            digest=digest,
        )
