from __future__ import annotations

import subprocess

from riftx.runner.process_inspector import _read_posix_command


def test_process_inspector_treats_unavailable_ps_as_unknown(monkeypatch) -> None:
    def denied(*_: object, **__: object) -> object:
        raise PermissionError("ps denied")

    monkeypatch.setattr(subprocess, "run", denied)
    assert _read_posix_command(1234) is None
