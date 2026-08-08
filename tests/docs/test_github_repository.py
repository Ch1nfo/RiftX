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
        ".github/workflows/ci.yml",
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


def test_ci_qualifies_the_docker_free_release_path() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required in (
        "permissions:\n  contents: read",
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "pnpm/action-setup@v4",
        "actions/setup-node@v4",
        'python -m pip install -e ".[dev]"',
        "python -m ruff check src/riftx tests migrations",
        "python -m pytest",
        "python scripts/qa/release-gate.py",
        "pnpm --filter @riftx/web typecheck",
        "pnpm --filter @riftx/web test",
        "pnpm --filter @riftx/web build",
        "git diff --exit-code -- src/riftx/_webui",
    ):
        assert required in workflow

    assert "docker" not in workflow.lower()


def test_readmes_expose_github_project_health_links() -> None:
    for readme_name in ("README.md", "README_ZH.md"):
        readme = (ROOT / readme_name).read_text(encoding="utf-8")
        for required in (
            "https://github.com/Ch1nfo/RiftX/actions/workflows/ci.yml",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
        ):
            assert required in readme
