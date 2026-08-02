"""Kernel-backed process containment for host-native executions.

POSIX process groups are useful for delivering terminal signals, but they are
not an ownership boundary: a child can call ``setsid()`` or double-fork out of
the original group.  On Linux, a cgroup v2 leaf is the ownership boundary used
here.  Forked children inherit cgroup membership regardless of their process
group/session, ``cgroup.kill`` terminates the complete leaf, and
``cgroup.events`` reporting ``populated 0`` is the stop confirmation.

The configured cgroup root must be a delegated cgroup v2 directory controlled
by the Runner.  Production deployments must also prevent executed payloads
from writing an ancestor ``cgroup.procs`` file (normally by using a container,
systemd service hardening, or a distinct payload uid).  Otherwise a malicious
payload with cgroup administration access could deliberately migrate itself
out; cgroups cannot defend against their own administrator.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_CGROUP_NAME_PREFIX = "riftx-"
_CGROUP_V2_FILESYSTEM = "cgroup2"
_DEFAULT_CONFIRMATION_SECONDS = 0.5
_DEFAULT_POLL_SECONDS = 0.02
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class ProcessContainmentError(RuntimeError):
    """Base error for a kernel containment operation."""


class ProcessContainmentUnavailableError(ProcessContainmentError):
    """Raised when trustworthy containment cannot be established."""


class ProcessContainmentTerminationError(ProcessContainmentError):
    """Raised when an entire containment cannot be proven empty."""


class ProcessContainment(Protocol):
    """A prepared ownership boundary for one execution."""

    @property
    def identifier(self) -> str: ...

    @property
    def launcher_membership_path(self) -> Path: ...

    def boundary_exists(self) -> bool: ...

    async def is_populated(self) -> bool: ...

    def launcher_argv(
        self,
        target_argv: Sequence[str],
        *,
        control_fd: int,
        target_env_fd: int,
    ) -> list[str]: ...

    async def wait_empty(self, timeout_seconds: float | None) -> bool: ...

    async def terminate(self, *, grace_seconds: float) -> None: ...

    async def force_terminate(self, *, confirmation_seconds: float) -> None: ...

    async def cleanup(self) -> None: ...


class ProcessContainmentManager(Protocol):
    """Creates one containment leaf for an execution key."""

    async def prepare(self, execution_key: str) -> ProcessContainment: ...

    def containment_for(self, execution_key: str) -> ProcessContainment: ...


@dataclass(slots=True)
class LinuxCgroupV2Containment:
    """One cgroup v2 leaf containing all descendants of an execution."""

    path: Path
    digest: str
    payload_uid: int | None = None
    payload_gid: int | None = None
    poll_seconds: float = _DEFAULT_POLL_SECONDS
    _durable_identifier: str | None = field(default=None, init=False, repr=False)
    _confirmed_empty: bool = field(default=False, init=False, repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)
    _operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def identifier(self) -> str:
        # A path is not a kernel identity.  The same path can resolve to a
        # different cgroup after a mount/cgroup namespace change or root
        # replacement.  Persist the root/leaf inode and namespace identity so
        # recovery cannot adopt a different boundary with the same pathname.
        if self._durable_identifier is None:
            self._durable_identifier = _durable_cgroup_identifier(
                self.path,
                execution_digest=self.digest,
            )
        return self._durable_identifier

    @property
    def launcher_membership_path(self) -> Path:
        """Return the cgroup leaf a trusted child launcher must join."""

        return self.path

    def boundary_exists(self) -> bool:
        if not self.path.is_dir():
            return False
        try:
            self._assert_boundary_identity()
        except ProcessContainmentError:
            return False
        return True

    async def is_populated(self) -> bool:
        return await asyncio.to_thread(self._read_populated)

    def launcher_argv(
        self,
        target_argv: Sequence[str],
        *,
        control_fd: int,
        target_env_fd: int,
    ) -> list[str]:
        if not target_argv or any(not item for item in target_argv):
            raise ValueError("target argv must contain non-empty elements")
        launcher = Path(__file__).with_name("_cgroup_launcher.py")
        return [
            sys.executable,
            "-I",
            "-S",
            str(launcher),
            "--cgroup",
            str(self.path),
            "--control-fd",
            str(control_fd),
            "--target-env-fd",
            str(target_env_fd),
            *(
                [
                    "--delegated-root",
                    str(self.path.parent),
                    "--payload-uid",
                    str(self.payload_uid),
                    "--payload-gid",
                    str(self.payload_gid),
                ]
                if self.payload_uid is not None and self.payload_gid is not None
                else []
            ),
            "--",
            *target_argv,
        ]

    async def wait_empty(self, timeout_seconds: float | None) -> bool:
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + max(timeout_seconds, 0.0)
        while True:
            populated = await asyncio.to_thread(self._read_populated)
            if not populated:
                self._confirmed_empty = True
                return True
            if deadline is None:
                await asyncio.sleep(self.poll_seconds)
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.poll_seconds, remaining))

    async def terminate(self, *, grace_seconds: float) -> None:
        async with self._operation_lock:
            if self._cleaned and self._confirmed_empty:
                return
            # Never enumerate cgroup.procs and signal numeric PIDs here. A PID
            # can exit and be reused between the read and os.kill(), which could
            # signal an unrelated same-UID process. cgroup.kill is atomic with
            # respect to membership and is the only whole-boundary primitive on
            # which the emergency-stop contract relies.
            await self._force_terminate_locked(
                confirmation_seconds=max(grace_seconds, _DEFAULT_CONFIRMATION_SECONDS)
            )

    async def force_terminate(self, *, confirmation_seconds: float) -> None:
        async with self._operation_lock:
            if self._cleaned and self._confirmed_empty:
                return
            await self._force_terminate_locked(
                confirmation_seconds=max(confirmation_seconds, _DEFAULT_CONFIRMATION_SECONDS)
            )

    async def _force_terminate_locked(self, *, confirmation_seconds: float) -> None:
        # Even when the leaf happens to be empty at this instant, requiring a
        # successful cgroup.kill write proves the configured backend actually
        # provides the primitive on which the safety contract depends.
        await asyncio.to_thread(self._write_kill)
        if not await self.wait_empty(confirmation_seconds):
            raise ProcessContainmentTerminationError(
                f"cgroup {self.identifier!r} remains populated after cgroup.kill"
            )

    async def cleanup(self) -> None:
        async with self._operation_lock:
            if self._cleaned:
                return
            if self._read_populated():
                raise ProcessContainmentTerminationError(
                    f"refusing to remove populated cgroup {self.identifier!r}"
                )
            self._confirmed_empty = True
            try:
                await asyncio.to_thread(os.rmdir, self.path)
            except FileNotFoundError:
                # A missing leaf is acceptable only after this object itself
                # observed it empty.  External disappearance is not evidence.
                if not self._confirmed_empty:
                    raise
            except OSError as exc:
                raise ProcessContainmentTerminationError(
                    f"failed to remove empty cgroup {self.identifier!r}: {exc}"
                ) from exc
            self._cleaned = True

    def _read_populated(self) -> bool:
        if self._cleaned:
            if self._confirmed_empty:
                return False
            raise ProcessContainmentTerminationError(
                f"cgroup {self.identifier!r} disappeared without empty confirmation"
            )
        self._assert_boundary_identity()
        events_path = self.path / "cgroup.events"
        try:
            raw = _read_control_file(events_path)
        except OSError as exc:
            raise ProcessContainmentTerminationError(
                f"cannot read populated state for cgroup {self.identifier!r}: {exc}"
            ) from exc
        values: dict[str, str] = {}
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) == 2:
                values[fields[0]] = fields[1]
        populated = values.get("populated")
        if populated not in {"0", "1"}:
            raise ProcessContainmentTerminationError(
                f"cgroup {self.identifier!r} returned invalid populated state"
            )
        return populated == "1"

    def _write_kill(self) -> None:
        self._assert_boundary_identity()
        kill_path = self.path / "cgroup.kill"
        try:
            _write_control_file(kill_path, b"1\n")
        except OSError as exc:
            raise ProcessContainmentTerminationError(
                f"cgroup.kill failed for {self.identifier!r}: {exc}"
            ) from exc

    def _assert_boundary_identity(self) -> None:
        try:
            current = _durable_cgroup_identifier(
                self.path,
                execution_digest=self.digest,
            )
        except ProcessContainmentUnavailableError as exc:
            raise ProcessContainmentTerminationError(str(exc)) from exc
        expected = self._durable_identifier
        if expected is None:
            self._durable_identifier = current
            return
        if current != expected:
            raise ProcessContainmentTerminationError(
                "cgroup boundary kernel identity changed; refusing to inspect or kill "
                f"the replacement at {str(self.path)!r}"
            )


@dataclass(frozen=True, slots=True)
class LinuxCgroupV2Manager:
    """Creates collision-resistant leaves below a delegated cgroup v2 root."""

    root: Path
    verify_filesystem: bool = True
    payload_uid: int | None = None
    payload_gid: int | None = None

    def __post_init__(self) -> None:
        if (self.payload_uid is None) != (self.payload_gid is None):
            raise ValueError("payload uid and gid must be configured together")
        if self.payload_uid is not None:
            if (
                not isinstance(self.payload_uid, int)
                or isinstance(self.payload_uid, bool)
                or self.payload_uid <= 0
            ):
                raise ValueError("payload uid must be a positive integer")
        if self.payload_gid is not None:
            if (
                not isinstance(self.payload_gid, int)
                or isinstance(self.payload_gid, bool)
                or self.payload_gid <= 0
            ):
                raise ValueError("payload gid must be a positive integer")

    @classmethod
    def autodetect(
        cls,
        *,
        payload_uid: int | None = None,
        payload_gid: int | None = None,
    ) -> LinuxCgroupV2Manager | None:
        if not sys.platform.startswith("linux"):
            return None
        configured = os.environ.get("RIFTX_CGROUP_V2_ROOT")
        try:
            if configured:
                root = Path(configured)
            else:
                mount = _current_cgroup_v2_mount()
                if mount is None:
                    return None
                mount_point, mount_root, process_cgroup = mount
                try:
                    relative = process_cgroup.relative_to(mount_root)
                except ValueError:
                    return None
                root = mount_point / relative / "riftx"
            root.mkdir(mode=0o700, parents=False, exist_ok=True)
            manager = cls(
                root=root,
                payload_uid=payload_uid,
                payload_gid=payload_gid,
            )
            manager._validate_root()
        except (OSError, ProcessContainmentError, ValueError):
            return None
        return manager

    async def prepare(self, execution_key: str) -> LinuxCgroupV2Containment:
        if not execution_key:
            raise ValueError("execution key must not be empty")
        self.require_distinct_payload_identity()
        return await asyncio.to_thread(self._prepare_sync, execution_key)

    def containment_for(self, execution_key: str) -> LinuxCgroupV2Containment:
        """Resolve the deterministic leaf used by durable recovery code."""

        digest = _execution_digest(execution_key)
        return LinuxCgroupV2Containment(
            path=self.root.resolve(strict=False) / f"{_CGROUP_NAME_PREFIX}{digest}",
            digest=digest,
            payload_uid=self.payload_uid,
            payload_gid=self.payload_gid,
        )

    def require_distinct_payload_identity(self) -> None:
        """Reject strong-containment mode without a distinct payload identity."""

        if self.payload_uid is None or self.payload_gid is None:
            raise ProcessContainmentUnavailableError(
                "strong Linux containment requires payload_uid and payload_gid"
            )
        if self.payload_uid == os.geteuid():
            raise ProcessContainmentUnavailableError(
                "strong Linux containment requires payload_uid to differ from "
                "the Runner effective uid"
            )

    def _prepare_sync(self, execution_key: str) -> LinuxCgroupV2Containment:
        root = self._validate_root()
        digest = _execution_digest(execution_key)
        leaf = root / f"{_CGROUP_NAME_PREFIX}{digest}"
        containment = LinuxCgroupV2Containment(
            path=leaf,
            digest=digest,
            payload_uid=self.payload_uid,
            payload_gid=self.payload_gid,
        )
        try:
            leaf.mkdir(mode=0o700)
        except FileExistsError as exc:
            # Reusing a populated leaf could adopt unrelated/stale processes;
            # fail closed.  Empty leaves are also left for explicit recovery
            # and cleanup rather than silently recycling identity.
            raise ProcessContainmentUnavailableError(
                f"cgroup leaf already exists for execution {digest!r}"
            ) from exc
        try:
            required = (
                leaf / "cgroup.events",
                leaf / "cgroup.procs",
                leaf / "cgroup.kill",
                leaf / "cgroup.max.descendants",
            )
            missing = [path.name for path in required if not path.is_file()]
            if missing:
                raise ProcessContainmentUnavailableError(
                    "cgroup v2 leaf is missing required controls: " + ", ".join(missing)
                )
            _write_control_file(leaf / "cgroup.max.descendants", b"0\n")
            if containment._read_populated():
                raise ProcessContainmentUnavailableError(
                    f"new cgroup leaf {containment.identifier!r} is unexpectedly populated"
                )
        except BaseException:
            try:
                os.rmdir(leaf)
            except OSError:
                pass
            raise
        return containment

    def _validate_root(self) -> Path:
        try:
            root = self.root.resolve(strict=True)
        except OSError as exc:
            raise ProcessContainmentUnavailableError(
                f"cgroup root {str(self.root)!r} is unavailable: {exc}"
            ) from exc
        if not root.is_dir():
            raise ProcessContainmentUnavailableError(
                f"cgroup root {str(root)!r} is not a directory"
            )
        if self.verify_filesystem and _cgroup_v2_mount_containing(root) is None:
            raise ProcessContainmentUnavailableError(
                f"cgroup root {str(root)!r} is not on a cgroup v2 filesystem"
            )
        for name in ("cgroup.events", "cgroup.procs"):
            if not (root / name).is_file():
                raise ProcessContainmentUnavailableError(
                    f"cgroup root {str(root)!r} is missing {name!r}"
                )
        return root


def _execution_digest(execution_key: str) -> str:
    # Full SHA-256 keeps deterministic recovery without introducing a
    # practically meaningful collision or embedding attacker-controlled text.
    return hashlib.sha256(execution_key.encode("utf-8")).hexdigest()


def _durable_cgroup_identifier(path: Path, *, execution_digest: str) -> str:
    """Bind a persisted containment id to the exact visible kernel boundary.

    ``st_dev``/``st_ino`` identify the delegated root and execution leaf in the
    current mount view.  Namespace inode identities ensure a Runner restarted
    in a different mount or cgroup namespace cannot use a coincidentally equal
    path/inode tuple as absence evidence for the old boundary.
    """

    try:
        root = path.parent.resolve(strict=True)
        leaf = path.resolve(strict=True)
        root_stat = os.stat(root, follow_symlinks=False)
        leaf_stat = os.stat(leaf, follow_symlinks=False)
    except OSError as exc:
        raise ProcessContainmentUnavailableError(
            f"cgroup boundary {str(path)!r} is unavailable: {exc}"
        ) from exc
    if not root.is_dir() or not leaf.is_dir() or leaf.parent != root:
        raise ProcessContainmentUnavailableError(
            f"cgroup boundary {str(path)!r} is not a direct directory below its root"
        )

    identity_parts = [
        "cgroup-v2-kernel-identity-v1",
        os.fsdecode(os.fsencode(root)),
        str(root_stat.st_dev),
        str(root_stat.st_ino),
        str(leaf_stat.st_dev),
        str(leaf_stat.st_ino),
    ]
    for namespace in ("mnt", "cgroup"):
        namespace_path = Path("/proc/self/ns") / namespace
        try:
            namespace_stat = os.stat(namespace_path, follow_symlinks=True)
        except OSError:
            identity_parts.extend((namespace, "unavailable"))
        else:
            identity_parts.extend(
                (namespace, str(namespace_stat.st_dev), str(namespace_stat.st_ino))
            )
    boundary_digest = hashlib.sha256(
        "\x00".join(identity_parts).encode("utf-8", errors="surrogateescape")
    ).hexdigest()
    return f"cgroup-v2:{boundary_digest}:{execution_digest}"


def _read_control_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(map(len, chunks)) > 1024 * 1024:
                raise OSError("cgroup control file exceeded safety limit")
        return b"".join(chunks).decode("ascii", errors="strict")
    finally:
        os.close(fd)


def _write_control_file(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        written = os.write(fd, value)
        if written != len(value):
            raise OSError(f"short write to {path.name!r}")
    finally:
        os.close(fd)


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mountinfo_entries() -> Iterable[tuple[Path, Path, str]]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    entries: list[tuple[Path, Path, str]] = []
    for line in lines:
        try:
            before, after = line.split(" - ", 1)
            fields = before.split()
            filesystem = after.split()[0]
            mount_root = Path(_unescape_mount_field(fields[3]))
            mount_point = Path(_unescape_mount_field(fields[4]))
        except (IndexError, ValueError):
            continue
        entries.append((mount_point, mount_root, filesystem))
    return tuple(entries)


def _cgroup_v2_mount_containing(path: Path) -> tuple[Path, Path] | None:
    candidates: list[tuple[Path, Path]] = []
    for mount_point, mount_root, filesystem in _mountinfo_entries():
        if filesystem != _CGROUP_V2_FILESYSTEM:
            continue
        try:
            path.relative_to(mount_point)
        except ValueError:
            continue
        candidates.append((mount_point, mount_root))
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0].parts))


def _current_cgroup_v2_mount() -> tuple[Path, Path, Path] | None:
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    process_path: Path | None = None
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0" and fields[1] == "":
            process_path = Path(fields[2])
            break
    if process_path is None or not process_path.is_absolute():
        return None
    candidates = [
        (mount_point, mount_root, process_path)
        for mount_point, mount_root, filesystem in _mountinfo_entries()
        if filesystem == _CGROUP_V2_FILESYSTEM
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0].parts))
