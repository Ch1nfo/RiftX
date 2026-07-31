"""Trusted, activation-gated Unix PTY child launcher.

The launcher, rather than target-controlled code, joins and verifies the
execution cgroup.  It then claims the PTY slave as its controlling terminal
and waits for an exact activation byte.  The parent only sends that byte after
PID, process-group and containment identity have been durably admitted.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import os
import platform
import re
import sys
import termios
from pathlib import Path

_READY = b"READY\n"
_ACTIVATE = b"\x01"
_LAUNCHER_FAILURE = 125
_MAX_TARGET_ENVIRONMENT_BYTES = 4 * 1024 * 1024
_CGROUP_V2_FILESYSTEM = "cgroup2"
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _write_membership(path: Path) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path / "cgroup.procs", flags)
    try:
        payload = f"{os.getpid()}\n".encode("ascii")
        if os.write(fd, payload) != len(payload):
            raise OSError("short cgroup.procs write")
    finally:
        os.close(fd)


def _verify_membership(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path / "cgroup.procs", flags)
    try:
        raw = os.read(fd, 1024 * 1024).decode("ascii", errors="strict")
    finally:
        os.close(fd)
    if str(os.getpid()) not in raw.splitlines():
        raise RuntimeError("PTY launcher pid is absent from target cgroup")


def _report(control_fd: int, payload: bytes) -> None:
    if os.write(control_fd, payload) != len(payload):
        raise OSError("short PTY launcher control write")


def _read_target_environment(fd: int) -> dict[str, str]:
    chunks: list[bytes] = []
    total = 0
    try:
        while total <= _MAX_TARGET_ENVIRONMENT_BYTES:
            chunk = os.read(fd, min(64 * 1024, _MAX_TARGET_ENVIRONMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > _MAX_TARGET_ENVIRONMENT_BYTES:
            raise ValueError(
                f"target environment exceeds {_MAX_TARGET_ENVIRONMENT_BYTES} bytes"
            )
    finally:
        os.close(fd)

    decoded = json.loads(b"".join(chunks).decode("utf-8", errors="strict"))
    if not isinstance(decoded, dict):
        raise ValueError("target environment must be a JSON object")
    environment: dict[str, str] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("target environment keys and values must be strings")
        if not key or "=" in key or "\x00" in key:
            raise ValueError(f"invalid environment variable name: {key!r}")
        if "\x00" in value:
            raise ValueError(f"environment variable {key!r} contains a null byte")
        environment[key] = value
    return environment


def _unescape_mount_field(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _ancestor_cgroup_procs(delegated_root: Path) -> tuple[Path, ...]:
    root = delegated_root.resolve(strict=True)
    candidates: list[Path] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        try:
            before, after = line.split(" - ", 1)
            fields = before.split()
            filesystem = after.split()[0]
            mount_point = Path(_unescape_mount_field(fields[4])).resolve(strict=True)
        except (IndexError, OSError, ValueError):
            continue
        if filesystem != _CGROUP_V2_FILESYSTEM:
            continue
        try:
            root.relative_to(mount_point)
        except ValueError:
            continue
        candidates.append(mount_point)
    if not candidates:
        raise RuntimeError(f"delegated root {str(root)!r} is not on cgroup v2")

    mount_point = max(candidates, key=lambda candidate: len(candidate.parts))
    paths: list[Path] = []
    current = root
    while True:
        paths.append(current / "cgroup.procs")
        if current == mount_point:
            break
        parent = current.parent
        if parent == current:
            raise RuntimeError("cgroup v2 mount is not an ancestor of delegated root")
        current = parent
    return tuple(paths)


def _set_no_new_privileges() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("payload identity isolation requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise RuntimeError("PR_SET_NO_NEW_PRIVS could not be verified")


def _clear_capabilities() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("payload capability isolation requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapabilityHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapabilityData * 2)()
    capset = getattr(libc, "capset", None)
    if capset is not None:
        capset.argtypes = [
            ctypes.POINTER(_CapabilityHeader),
            ctypes.POINTER(_CapabilityData),
        ]
        capset.restype = ctypes.c_int
        result = capset(ctypes.byref(header), data)
    else:
        syscall_number = {
            "x86_64": 126,
            "amd64": 126,
            "i386": 185,
            "i686": 185,
            "arm": 185,
            "armv6l": 185,
            "armv7l": 185,
            "aarch64": 91,
            "arm64": 91,
            "riscv64": 91,
            "loongarch64": 91,
        }.get(platform.machine().lower())
        if syscall_number is None:
            raise RuntimeError(
                f"capset syscall is unavailable on {platform.machine()!r}"
            )
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(syscall_number, ctypes.byref(header), data)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _verify_capabilities_cleared() -> None:
    expected = {"CapEff", "CapPrm", "CapInh", "CapAmb"}
    values: dict[str, int] = {}
    for line in _read_self_status().splitlines():
        name, separator, raw = line.partition(":")
        if separator and name in expected:
            values[name] = int(raw.strip(), 16)
    missing = expected.difference(values)
    if missing:
        raise RuntimeError(
            "cannot verify cleared payload capabilities: missing "
            + ", ".join(sorted(missing))
        )
    residual = [name for name, value in values.items() if value != 0]
    if residual:
        raise RuntimeError(
            "payload retains capabilities in " + ", ".join(sorted(residual))
        )


def _read_self_status() -> str:
    return Path("/proc/self/status").read_text(encoding="ascii")


def _drop_payload_identity(payload_uid: int, payload_gid: int) -> None:
    if payload_uid == os.geteuid():
        raise RuntimeError("payload uid must differ from the Runner effective uid")
    os.setgroups([])
    os.setgid(payload_gid)
    os.setuid(payload_uid)
    if os.getuid() != payload_uid or os.geteuid() != payload_uid:
        raise RuntimeError("payload uid drop could not be verified")
    if os.getgid() != payload_gid or os.getegid() != payload_gid:
        raise RuntimeError("payload gid drop could not be verified")
    getresuid = getattr(os, "getresuid", None)
    if getresuid is None:
        raise RuntimeError("payload saved uid cannot be verified")
    if any(value != payload_uid for value in getresuid()):
        raise RuntimeError("payload saved uid was not dropped")
    getresgid = getattr(os, "getresgid", None)
    if getresgid is None:
        raise RuntimeError("payload saved gid cannot be verified")
    if any(value != payload_gid for value in getresgid()):
        raise RuntimeError("payload saved gid was not dropped")
    if os.getgroups():
        raise RuntimeError("payload supplementary groups were not cleared")
    _clear_capabilities()
    _set_no_new_privileges()
    _verify_capabilities_cleared()


def _verify_ancestor_migration_denied(paths: tuple[Path, ...]) -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    denied_errors = {errno.EACCES, errno.EPERM, errno.EROFS}
    for path in paths:
        if os.access(path.parent, os.W_OK, effective_ids=True):
            raise RuntimeError(
                "payload identity retains administrative write access to cgroup "
                f"directory {str(path.parent)!r}"
            )
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno in denied_errors:
                continue
            raise RuntimeError(
                f"cannot verify payload write denial for {str(path)!r}: {exc}"
            ) from exc
        else:
            os.close(fd)
            raise RuntimeError(
                f"payload identity retains O_WRONLY access to {str(path)!r}"
            )


def main(argv: list[str] | None = None) -> int:
    if os.name != "posix":
        return _LAUNCHER_FAILURE

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("--control-fd", type=int, required=True)
    parser.add_argument("--target-env-fd", type=int, required=True)
    parser.add_argument("--delegated-root", type=Path)
    parser.add_argument("--payload-uid", type=int)
    parser.add_argument("--payload-gid", type=int)
    parser.add_argument("target", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    target = list(args.target)
    if target[:1] == ["--"]:
        target = target[1:]
    if not target or any(not item for item in target):
        return _LAUNCHER_FAILURE

    try:
        if args.control_fd < 0 or args.target_env_fd < 0:
            raise ValueError("inherited descriptor numbers must be non-negative")
        if args.control_fd == args.target_env_fd:
            raise ValueError("control and target environment descriptors must differ")
        if (args.payload_uid is None) != (args.payload_gid is None):
            raise ValueError("payload uid and gid must be configured together")
        if args.payload_uid is not None and args.payload_uid <= 0:
            raise ValueError("payload uid must be positive")
        if args.payload_gid is not None and args.payload_gid <= 0:
            raise ValueError("payload gid must be positive")

        ancestor_procs: tuple[Path, ...] = ()
        if args.payload_uid is not None:
            if args.cgroup is None or args.delegated_root is None:
                raise ValueError(
                    "payload identity isolation requires cgroup and delegated root"
                )
            ancestor_procs = _ancestor_cgroup_procs(args.delegated_root)
        if args.cgroup is not None:
            _write_membership(args.cgroup)
            _verify_membership(args.cgroup)

        # The parent starts this wrapper as a new session leader. Claiming the
        # slave here avoids unsafe preexec_fn hooks in the multi-threaded Runner.
        fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
        if args.payload_uid is not None and args.payload_gid is not None:
            _drop_payload_identity(args.payload_uid, args.payload_gid)
            _verify_ancestor_migration_denied(ancestor_procs)
        _report(args.control_fd, _READY)
        # EOF (including parent death) is a launch denial.  No target-controlled
        # code runs unless the durable supervisor sends this exact byte.
        if os.read(args.control_fd, 1) != _ACTIVATE:
            return _LAUNCHER_FAILURE
    except BaseException as exc:
        try:
            _report(
                args.control_fd,
                f"ERROR:{type(exc).__name__}:{exc}\n".encode(
                    "utf-8", errors="replace"
                )[:4096],
            )
        except BaseException:
            pass
        return _LAUNCHER_FAILURE

    try:
        target_environment = _read_target_environment(args.target_env_fd)
        # Successful exec closes the control descriptor atomically.  The parent
        # reads EOF as confirmation; an exec failure is reported explicitly.
        flags = fcntl.fcntl(args.control_fd, fcntl.F_GETFD)
        fcntl.fcntl(args.control_fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        os.execvpe(target[0], target, target_environment)
    except BaseException as exc:
        try:
            _report(
                args.control_fd,
                f"EXEC_ERROR:{type(exc).__name__}:{exc}\n".encode(
                    "utf-8", errors="replace"
                )[:4096],
            )
        except BaseException:
            pass
        print(f"riftx PTY launcher exec failed: {exc}", file=sys.stderr, flush=True)
        try:
            os.close(args.control_fd)
        except OSError:
            pass
        return _LAUNCHER_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
