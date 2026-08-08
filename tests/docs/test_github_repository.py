from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_repository_has_required_community_files_and_metadata() -> None:
    for relative_path in (
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "README_ZH.md",
        "SECURITY.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
    ):
        assert (ROOT / relative_path).is_file(), relative_path

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "Apache-2.0"
    assert project["requires-python"] == ">=3.12,<3.13"
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    assert "Topic :: Security" in project["classifiers"]
    assert set(project["urls"]) == {
        "Homepage",
        "Documentation",
        "Repository",
        "Issues",
        "Changelog",
    }
    assert all(
        url.startswith("https://github.com/Ch1nfo/RiftX")
        for url in project["urls"].values()
    )
