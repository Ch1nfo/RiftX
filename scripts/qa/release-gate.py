#!/usr/bin/env python3
"""Run the executable RiftX release qualification gates."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from riftx.evaluation import (  # noqa: E402
    ReleaseGateEvaluator,
    ReleaseGateEvidence,
    release_gate_manifest,
)


def main() -> int:
    manifest = release_gate_manifest()
    selectors = list(
        dict.fromkeys(
            selector
            for _, gate_selectors in manifest.values()
            for selector in gate_selectors
        )
    )
    outcomes: dict[str, bool] = {}
    for selector in selectors:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", selector],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        outcomes[selector] = completed.returncode == 0
        if completed.returncode != 0:
            print(f"release evidence failed: {selector}", file=sys.stderr)
            print(completed.stdout, file=sys.stderr)
            print(completed.stderr, file=sys.stderr)

    report = ReleaseGateEvaluator().evaluate(
        [
            ReleaseGateEvidence(
                gate=gate,
                passed=all(outcomes[selector] for selector in gate_selectors),
                test_selectors=list(gate_selectors),
                detail=detail,
            )
            for gate, (detail, gate_selectors) in manifest.items()
        ]
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
