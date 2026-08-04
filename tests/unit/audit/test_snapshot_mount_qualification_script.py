from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "qa" / "audit-snapshot-mount-qualification.py"


def test_qualification_script_fails_closed_without_a_qualified_pinned_image() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--image-digest",
            "0" * 64,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["schema_version"] == ("riftx.audit-snapshot-mount-real-linux-qualification/v1")
    assert report["ready"] is False
    assert report["checks"]["availability"] is False
    assert report["failure_code"] is not None
    assert len(report["evidence_digest"]) == hashlib.sha256().digest_size * 2
    assert completed.stderr == ""


def test_qualification_script_rejects_unpinned_image_reference() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--image-digest",
            "sha256:" + "0" * 64,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "64 lowercase hexadecimal characters" in completed.stderr
