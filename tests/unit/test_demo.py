from __future__ import annotations

from pathlib import Path

import yaml

from riftx.config import RiftXConfig
from riftx.demo import run_code_audit_demo, run_pentest_demo


def _config(tool_path: Path) -> RiftXConfig:
    return RiftXConfig.model_validate({"tools": {"path": str(tool_path)}})


def test_pentest_demo_is_sanitized_and_reports_optional_tool_fallbacks(
    tmp_path: Path,
) -> None:
    tool_path = tmp_path / "tools.yaml"
    tool_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "nmap": {"enabled": False, "command": ["nmap"]},
                    "nuclei": {"enabled": False, "command": ["nuclei"]},
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    result = run_pentest_demo(_config(tool_path))

    assert result.sanitized is True
    assert result.target == "https://portal.demo.invalid"
    assert result.pack_ids == (
        "pentest-foundation",
        "scope-and-safety",
        "passive-recon",
        "service-enumeration",
        "web-attack-surface",
    )
    assert result.available_optional_tools == ()
    assert result.unavailable_optional_tools == ("nmap", "nuclei")
    assert "bundled transcript" in result.degradation_path
    assert all("request sent" not in step.evidence for step in result.steps)


def test_pentest_demo_degrades_when_tool_config_is_unavailable(tmp_path: Path) -> None:
    result = run_pentest_demo(_config(tmp_path / "missing-tools.yaml"))

    assert result.unavailable_optional_tools == ("nmap", "nuclei")
    assert result.tool_config_issue is not None
    assert "bundled transcript" in result.degradation_path


def test_code_audit_demo_runs_real_builtin_detectors_and_redacts_credentials() -> None:
    result = run_code_audit_demo()

    assert result.sanitized is True
    assert result.pack_id == "code-audit-foundation"
    assert result.files_scanned == 3
    assert {finding.rule_id for finding in result.findings} == {
        "configuration.insecure_setting",
        "dependency.unpinned",
        "python.dangerous_api",
        "secret.hardcoded_credential",
    }
    assert result.degradation_path.startswith("Built-in static detectors")
    rendered_evidence = "\n".join(finding.evidence for finding in result.findings)
    assert "demo-secret-value" not in rendered_evidence
    assert "[REDACTED]" in rendered_evidence
