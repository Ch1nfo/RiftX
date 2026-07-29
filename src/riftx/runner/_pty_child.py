"""Minimal exec wrapper that claims fd 0 as the child session's controlling TTY."""

from __future__ import annotations

import fcntl
import os
import sys
import termios


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit("PTY child wrapper requires a target command")
    if os.name != "posix":
        raise SystemExit("PTY child wrapper requires POSIX")

    # The parent starts this wrapper as a new session leader. Claiming the slave
    # here (after exec rather than in preexec_fn) avoids unsafe fork hooks in the
    # multi-threaded Runner while providing a proper controlling terminal.
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    main()
