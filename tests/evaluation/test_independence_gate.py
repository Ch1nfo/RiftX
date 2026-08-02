from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from riftx.evaluation import IndependenceBoundaryScanner
from riftx.evaluation import independence as independence_module

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa" / "code-audit-boundary-gate.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_riftx_repository(root: Path) -> None:
    _write(root / "pyproject.toml", '[project]\nname = "fixture"\n')
    _write(root / "package.json", '{"name":"fixture","private":true}')
    _write(root / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(root / "pnpm-workspace.yaml", "packages:\n  - apps/*\n")
    _write(root / "alembic.ini", "[alembic]\n")
    _write(root / "src" / "riftx" / "__init__.py", '"""Synthetic RiftX root."""')
    _write(root / "migrations" / "env.py", "\n")
    (root / "migrations" / "versions").mkdir(parents=True)
    for app in ("web", "demo"):
        _write(
            root / "apps" / app / "package.json",
            f'{{"name":"@fixture/{app}","private":true}}',
        )
        _write(root / "apps" / app / "src" / "main.tsx", "export {};\n")
        _write(root / "apps" / app / "index.html", "<main></main>\n")
        _write(root / "apps" / app / "vite.config.ts", "export default {};\n")
        _write(root / "apps" / app / "tsconfig.json", "{}\n")
    _write(
        root / "apps" / "browser-extension" / "package.json",
        '{"name":"@fixture/browser-extension","private":true}',
    )
    for entry in ("connector.ts", "devtools.ts", "panel.ts"):
        _write(root / "apps" / "browser-extension" / "src" / entry, "export {};\n")
    _write(
        root / "apps" / "browser-extension" / "static" / "manifest.json",
        '{"manifest_version":3}',
    )
    _write(
        root / "apps" / "browser-extension" / "scripts" / "copy-static.mjs",
        "export {};\n",
    )
    _write(root / "apps" / "browser-extension" / "tsconfig.json", "{}\n")
    _write(root / "apps" / "burp-extension" / "build.gradle.kts", "plugins { java }\n")
    _write(root / "apps" / "burp-extension" / "settings.gradle.kts", "\n")
    _write(
        root
        / "apps"
        / "burp-extension"
        / "src"
        / "main"
        / "java"
        / "com"
        / "riftx"
        / "burp"
        / "RiftXBurpExtension.java",
        "package com.riftx.burp;\n",
    )
    for name in ("riftx", "models", "tools"):
        _write(root / "configs" / f"{name}.example.yaml", "version: 1\n")


def test_repository_production_inputs_pass_independence_boundary() -> None:
    report = IndependenceBoundaryScanner().scan(ROOT)

    assert report.ready
    assert report.scanned_dependency_manifests >= 5
    assert report.scanned_production_files > 0
    assert report.violations == []


def test_clean_explicit_bundle_passes_boundary(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    bundle = tmp_path / "dist"
    _write(
        bundle / "assets" / "index.js",
        "const provider = 'openai'; const grid = 'codex-grid-background';",
    )

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[bundle])

    assert report.ready
    assert report.scanned_artifact_files == 1


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("pyproject.toml", 'dependencies = ["codex-security>=1"]'),
        (
            "apps/web/package.json",
            '{"dependencies":{"@openai/codex_security":"1.0.0"}}',
        ),
        ("Cargo.lock", 'name = "codex.security"'),
        ("setup.cfg", "install_requires = codex-security>=1"),
        ("Pipfile", 'codex_security = "*"'),
        ("release/riftx.cdx.json", '{"component":"codex-security"}'),
    ],
)
def test_forbidden_dependency_identity_is_rejected(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    _make_riftx_repository(tmp_path)
    _write(tmp_path / relative_path, content)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert {item.rule_id for item in report.violations} == {
        "forbidden_dependency_manifest_content"
    }


def test_explicit_bundle_content_identity_is_rejected(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    bundle = tmp_path / "dist"
    _write(bundle / "assets" / "index.js", "fetch('https://codex-security.example/v1')")

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[bundle])

    assert not report.ready
    assert any(item.rule_id == "forbidden_build_artifact_content" for item in report.violations)


def test_document_reference_is_not_a_production_violation(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    _write(
        tmp_path / "docs" / "research" / "reference.md",
        "Public design reference: https://github.com/openai/codex-security",
    )
    _write(tmp_path / "src" / "riftx" / "service.py", "PRODUCT = 'RiftX Code Audit'")

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert report.ready
    assert report.scanned_production_files >= 2


def test_percent_encoded_url_and_path_variants_are_rejected(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    _write(
        tmp_path / "src" / "riftx" / "client.py",
        "URL = 'https://github.com/OpenAI/CODEX%2DSECURITY/api'",
    )
    _write(
        tmp_path / "src" / "riftx" / "OPENAI_SECURITY" / "provider.py",
        "PROVIDER = 'external'",
    )

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    rule_ids = {item.rule_id for item in report.violations}
    assert "forbidden_production_source_content" in rule_ids
    assert "forbidden_production_source_path" in rule_ids


@pytest.mark.parametrize(
    "relative_path",
    [
        "apps/browser-extension/static/manifest.json",
        "apps/browser-extension/scripts/copy-static.mjs",
        "apps/web/index.html",
        "apps/demo/public/runtime.txt",
        "apps/demo/vite.config.ts",
    ],
)
def test_shipped_app_input_is_inside_production_allowlist(
    tmp_path: Path,
    relative_path: str,
) -> None:
    _make_riftx_repository(tmp_path)
    _write(tmp_path / relative_path, "UPSTREAM = 'codex-security'")

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(
        item.rule_id == "forbidden_production_source_content" for item in report.violations
    )


@pytest.mark.parametrize(
    "encoded_identity",
    [
        "codex%252Dsecurity",
        r"codex\u002dsecurity",
        "codex&#45;security",
        "ｃｏｄｅｘ－ｓｅｃｕｒｉｔｙ",
    ],
)
def test_bounded_encoding_variants_are_rejected(
    tmp_path: Path,
    encoded_identity: str,
) -> None:
    _make_riftx_repository(tmp_path)
    _write(tmp_path / "src" / "riftx" / "encoded.py", encoded_identity)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(
        item.rule_id == "forbidden_production_source_content" for item in report.violations
    )


def test_utf16_bom_bundle_content_is_rejected(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "index.js").write_bytes("const name = 'codex-security';".encode("utf-16"))

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[bundle])

    assert not report.ready
    assert any(item.rule_id == "forbidden_build_artifact_content" for item in report.violations)


def test_combined_dependency_source_and_archive_canary_is_rejected(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    _write(tmp_path / "Pipfile", 'codex_security = "*"')
    _write(
        tmp_path / "apps" / "browser-extension" / "static" / "manifest.json",
        '{"endpoint":"codex&#45;security"}',
    )
    archive_path = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr("codex_security/plugin.py", "VALUE = 'safe'")
        archive.writestr("safe.js", "fetch('https://codex-security.example/v1')")

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[archive_path])

    assert not report.ready
    rule_ids = {item.rule_id for item in report.violations}
    assert "forbidden_dependency_manifest_content" in rule_ids
    assert "forbidden_production_source_content" in rule_ids
    assert "forbidden_build_artifact_path" in rule_ids
    assert "forbidden_build_artifact_content" in rule_ids


def test_provider_neutral_openai_security_setting_is_not_rejected(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    _write(
        tmp_path / "src" / "riftx" / "settings.py",
        "OPENAI_SECURITY_MODE = 'strict'",
    )

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert report.ready


def test_invalid_repository_root_fails_closed(tmp_path: Path) -> None:
    report = IndependenceBoundaryScanner().scan(tmp_path / "missing")

    assert not report.ready
    assert [item.rule_id for item in report.violations] == ["repository_root_invalid"]


def test_existing_empty_repository_fails_closed(tmp_path: Path) -> None:
    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    rule_ids = {item.rule_id for item in report.violations}
    assert "repository_inputs_empty" in rule_ids
    assert "repository_marker_missing" in rule_ids


def test_sparse_repository_missing_fixed_marker_fails_closed(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    (tmp_path / "package.json").unlink()

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(
        item.rule_id == "repository_marker_missing" and item.path == "package.json"
        for item in report.violations
    )


@pytest.mark.parametrize(
    "required_input",
    [
        "pnpm-lock.yaml",
        "src/riftx/__init__.py",
        "migrations/versions",
        "apps/web",
        "apps/demo/vite.config.ts",
        "apps/browser-extension/static/manifest.json",
        "apps/browser-extension/scripts/copy-static.mjs",
        "apps/burp-extension",
        "configs/riftx.example.yaml",
    ],
)
def test_required_component_input_deletion_fails_closed(
    tmp_path: Path,
    required_input: str,
) -> None:
    _make_riftx_repository(tmp_path)
    target = tmp_path / required_input
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(item.rule_id == "repository_marker_missing" for item in report.violations)


def test_repository_marker_type_is_checked(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    (tmp_path / "package.json").unlink()
    (tmp_path / "package.json").mkdir()

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(
        item.rule_id == "repository_marker_missing" and item.path == "package.json"
        for item in report.violations
    )


def test_require_artifact_rejects_repository_only_invocation(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)

    report = IndependenceBoundaryScanner().scan(tmp_path, require_artifact=True)

    assert not report.ready
    assert any(item.rule_id == "explicit_artifact_required" for item in report.violations)


def test_production_source_symlink_fails_closed(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    external = tmp_path / "external.py"
    _write(external, "VALUE = 'safe'")
    source = tmp_path / "src" / "riftx"
    source.mkdir(parents=True, exist_ok=True)
    (source / "linked.py").symlink_to(external)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(item.rule_id == "production_source_symlink" for item in report.violations)


def test_dependency_manifest_tree_symlink_fails_closed(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    external = tmp_path / "external-dependencies"
    external.mkdir()
    _write(external / "Pipfile", '[packages]\nhttpx = "*"')
    link = tmp_path / "apps" / "linked-dependencies"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(external, target_is_directory=True)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(
        item.rule_id == "dependency_manifest_tree_symlink" for item in report.violations
    )


def test_dependency_walk_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_riftx_repository(tmp_path)
    real_walk = independence_module.os.walk

    def failing_walk(top: Path, *args: object, **kwargs: object):
        if Path(top).absolute() == tmp_path.absolute():
            onerror = kwargs["onerror"]
            assert callable(onerror)
            onerror(PermissionError(13, "denied", str(tmp_path / "unreadable")))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(independence_module.os, "walk", failing_walk)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(item.rule_id == "dependency_manifest_walk_failed" for item in report.violations)


def test_production_walk_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_riftx_repository(tmp_path)
    source_root = tmp_path / "src"
    real_walk = independence_module.os.walk

    def failing_walk(top: Path, *args: object, **kwargs: object):
        if Path(top).absolute() == source_root.absolute():
            onerror = kwargs["onerror"]
            assert callable(onerror)
            onerror(PermissionError(13, "denied", str(source_root / "unreadable")))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(independence_module.os, "walk", failing_walk)

    report = IndependenceBoundaryScanner().scan(tmp_path)

    assert not report.ready
    assert any(item.rule_id == "production_source_walk_failed" for item in report.violations)


def test_artifact_walk_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_riftx_repository(tmp_path)
    bundle = tmp_path / "dist"
    bundle.mkdir()
    real_walk = independence_module.os.walk

    def failing_walk(top: Path, *args: object, **kwargs: object):
        if Path(top).absolute() == bundle.absolute():
            onerror = kwargs["onerror"]
            assert callable(onerror)
            onerror(PermissionError(13, "denied", str(bundle / "unreadable")))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(independence_module.os, "walk", failing_walk)

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[bundle])

    assert not report.ready
    assert any(item.rule_id == "explicit_artifact_walk_failed" for item in report.violations)


def test_artifact_scanner_fail_closed_qualification_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_riftx_repository(tmp_path)
    empty_directory = tmp_path / "empty-directory"
    empty_directory.mkdir()
    zero_file = tmp_path / "zero.js"
    zero_file.touch()
    corrupt_archive = tmp_path / "corrupt.jar"
    corrupt_archive.write_bytes(b"not a zip archive")
    empty_zip = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty_zip, mode="w"):
        pass
    empty_tar = tmp_path / "empty.tar"
    with tarfile.open(empty_tar, mode="w"):
        pass

    missing_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[tmp_path / "missing"],
    )
    empty_directory_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[empty_directory],
    )
    zero_file_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[zero_file],
    )
    empty_zip_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[empty_zip],
    )
    empty_tar_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[empty_tar],
    )
    corrupt_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[corrupt_archive],
    )
    require_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        require_artifact=True,
    )

    walk_bundle = tmp_path / "walk-error"
    walk_bundle.mkdir()
    real_walk = independence_module.os.walk

    def failing_walk(top: Path, *args: object, **kwargs: object):
        if Path(top).absolute() == walk_bundle.absolute():
            onerror = kwargs["onerror"]
            assert callable(onerror)
            onerror(PermissionError(13, "denied", str(walk_bundle / "unreadable")))
            return iter(())
        return real_walk(top, *args, **kwargs)

    with monkeypatch.context() as scoped_monkeypatch:
        scoped_monkeypatch.setattr(independence_module.os, "walk", failing_walk)
        walk_report = IndependenceBoundaryScanner().scan(
            tmp_path,
            artifacts=[walk_bundle],
        )

    expectations = (
        (missing_report, "explicit_artifact_missing"),
        (empty_directory_report, "explicit_artifact_empty"),
        (zero_file_report, "explicit_artifact_empty"),
        (empty_zip_report, "explicit_artifact_empty"),
        (empty_tar_report, "explicit_artifact_empty"),
        (corrupt_report, "explicit_artifact_invalid_archive"),
        (require_report, "explicit_artifact_required"),
        (walk_report, "explicit_artifact_walk_failed"),
    )
    for report, expected_rule in expectations:
        assert not report.ready
        assert any(item.rule_id == expected_rule for item in report.violations)


@pytest.mark.parametrize("artifact_name", ["missing", "empty", "broken.jar"])
def test_explicit_artifact_target_fails_closed(tmp_path: Path, artifact_name: str) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / artifact_name
    if artifact_name == "empty":
        artifact.mkdir()
    elif artifact_name == "broken.jar":
        artifact.write_bytes(b"not a zip archive")

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert report.violations
    assert all(item.rule_id.startswith("explicit_artifact_") for item in report.violations)


def test_zero_byte_explicit_file_fails_closed(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / "empty.js"
    artifact.touch()

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(
        item.rule_id == "explicit_artifact_empty" and item.path == "empty.js"
        for item in report.violations
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO support is unavailable")
@pytest.mark.parametrize("nested", [False, True])
def test_fifo_artifact_is_rejected_before_read(tmp_path: Path, nested: bool) -> None:
    _make_riftx_repository(tmp_path)
    if nested:
        artifact = tmp_path / "bundle"
        artifact.mkdir()
        fifo = artifact / "output.pipe"
    else:
        fifo = tmp_path / "output.pipe"
        artifact = fifo
    os.mkfifo(fifo)

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(
        item.rule_id == "explicit_artifact_unsupported" and item.path.endswith("output.pipe")
        for item in report.violations
    )


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_zero_member_archive_fails_closed(tmp_path: Path, archive_kind: str) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / f"empty.{archive_kind}"
    if archive_kind == "zip":
        with zipfile.ZipFile(artifact, mode="w"):
            pass
    else:
        with tarfile.open(artifact, mode="w"):
            pass

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(item.rule_id == "explicit_artifact_empty" for item in report.violations)


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_zero_byte_archive_member_is_a_scannable_path(
    tmp_path: Path,
    archive_kind: str,
) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / f"one-empty-member.{archive_kind}"
    if archive_kind == "zip":
        with zipfile.ZipFile(artifact, mode="w") as archive:
            archive.writestr("empty.txt", b"")
    else:
        member = tarfile.TarInfo("empty.txt")
        member.size = 0
        with tarfile.open(artifact, mode="w") as archive:
            archive.addfile(member, io.BytesIO(b""))

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert report.ready
    assert report.scanned_artifact_files == 1


@pytest.mark.parametrize(
    ("suffix", "write_mode"),
    [
        ("tar.bz2", "w:bz2"),
        ("tbz", "w:bz2"),
        ("tbz2", "w:bz2"),
        ("tar.xz", "w:xz"),
        ("txz", "w:xz"),
    ],
)
def test_supported_compressed_tar_scans_forbidden_members(
    tmp_path: Path,
    suffix: str,
    write_mode: str,
) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / f"candidate.{suffix}"
    path_payload = b"VALUE = 'safe'"
    content_payload = b"fetch('https://codex-security.example/v1')"
    with tarfile.open(artifact, mode=write_mode) as archive:
        path_member = tarfile.TarInfo("codex_security/plugin.py")
        path_member.size = len(path_payload)
        archive.addfile(path_member, io.BytesIO(path_payload))
        content_member = tarfile.TarInfo("safe.js")
        content_member.size = len(content_payload)
        archive.addfile(content_member, io.BytesIO(content_payload))

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    rule_ids = {item.rule_id for item in report.violations}
    assert "forbidden_build_artifact_path" in rule_ids
    assert "forbidden_build_artifact_content" in rule_ids


@pytest.mark.parametrize(
    "suffix",
    [
        "tar.zst",
        "tar.zstd",
        "tar.lz",
        "tar.lz4",
        "tar.br",
        "tlz",
        "taz",
        "tzst",
        "tzstd",
    ],
)
def test_unsupported_archive_compression_fails_closed(
    tmp_path: Path,
    suffix: str,
) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / f"candidate.{suffix}"
    artifact.write_bytes(b"opaque archive payload")

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(
        item.rule_id == "explicit_artifact_unsupported_archive"
        for item in report.violations
    )


def test_tar_sidecar_signature_is_not_misclassified_and_content_is_scanned(
    tmp_path: Path,
) -> None:
    _make_riftx_repository(tmp_path)
    artifact = tmp_path / "candidate.tar.gz.sig"
    artifact.write_bytes(b"detached signature payload")

    clean_report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert clean_report.ready
    artifact.write_bytes(b"codex-security")

    forbidden_report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not forbidden_report.ready
    rule_ids = {item.rule_id for item in forbidden_report.violations}
    assert "forbidden_build_artifact_content" in rule_ids
    assert "explicit_artifact_unsupported_archive" not in rule_ids


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
@pytest.mark.parametrize("write_mode", ["w:gz", "w:xz"])
def test_compressed_tar_scans_link_target_metadata(
    tmp_path: Path,
    link_type: bytes,
    write_mode: str,
) -> None:
    _make_riftx_repository(tmp_path)
    suffix = "tar.gz" if write_mode == "w:gz" else "tar.xz"
    artifact = tmp_path / f"candidate.{suffix}"
    link = tarfile.TarInfo("safe-link")
    link.type = link_type
    link.linkname = "node_modules/codex-security/index.js"
    with tarfile.open(artifact, mode=write_mode) as archive:
        archive.addfile(link)

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(
        item.rule_id == "forbidden_build_artifact_path"
        and item.archive_member == "safe-link [linkname]"
        for item in report.violations
    )


def test_compressed_tar_scans_pax_metadata_and_allows_safe_link(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    forbidden = tmp_path / "forbidden-pax.tar.gz"
    member = tarfile.TarInfo("safe.txt")
    member.pax_headers = {"riftx.note": "codex-security"}
    with tarfile.open(forbidden, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        archive.addfile(member, io.BytesIO(b""))

    forbidden_report = IndependenceBoundaryScanner().scan(
        tmp_path,
        artifacts=[forbidden],
    )

    assert not forbidden_report.ready
    assert any(
        item.rule_id == "forbidden_build_artifact_path"
        and item.archive_member == "safe.txt [pax:riftx.note]"
        for item in forbidden_report.violations
    )

    safe = tmp_path / "safe-link.tar.gz"
    link = tarfile.TarInfo("safe-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "assets/safe.js"
    with tarfile.open(safe, mode="w:gz") as archive:
        archive.addfile(link)

    safe_report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[safe])

    assert safe_report.ready


def test_explicit_artifact_symlink_fails_closed(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    target = tmp_path / "artifact.js"
    target.write_text("export {};", encoding="utf-8")
    artifact = tmp_path / "artifact-link.js"
    artifact.symlink_to(target)

    report = IndependenceBoundaryScanner().scan(tmp_path, artifacts=[artifact])

    assert not report.ready
    assert any(item.rule_id == "explicit_artifact_symlink" for item in report.violations)


def test_executable_gate_returns_structured_failure_for_missing_artifact(tmp_path: Path) -> None:
    _make_riftx_repository(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--artifact",
            "missing-dist",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["ready"] is False
    assert any(
        item["rule_id"] == "explicit_artifact_missing" for item in payload["violations"]
    )


def test_executable_gate_rejects_existing_empty_repository(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert any(item["rule_id"] == "repository_inputs_empty" for item in payload["violations"])


def test_executable_gate_require_artifact_mode_rejects_repository_only(
    tmp_path: Path,
) -> None:
    _make_riftx_repository(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--require-artifact",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert any(item["rule_id"] == "explicit_artifact_required" for item in payload["violations"])
