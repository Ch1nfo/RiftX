"""Executable guardrail for the RiftX Code Audit implementation boundary."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import tarfile
import unicodedata
import zipfile
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote

from pydantic import Field, model_validator

from riftx.domain.base import DomainModel

INDEPENDENCE_POLICY_VERSION = "riftx.code-audit-independence/v1"

# This is deliberately narrower than a generic "codex" or "openai" scan. RiftX already
# uses Codex-authored development notes and the OpenAI/Agents SDK as a replaceable model
# provider. The independent-implementation boundary forbids only upstream-specific product,
# namespace, path, package, and endpoint identities.
_UPSTREAM_PRODUCT_IDENTITY_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9])(?:"
    r"(?:@?openai[/._\\-]+)?codex(?:[/._\\ \t-]*security)"
    r")(?![a-z0-9])"
)
_RESERVED_NAMESPACE_TOKEN_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_])(?:@?openai[/._-]+security)"
    r"(?=$|[/\\.\s'\"<>=:;,)\]}])"
)
_FORBIDDEN_IDENTITY_PATTERNS = (
    _UPSTREAM_PRODUCT_IDENTITY_PATTERN,
    _RESERVED_NAMESPACE_TOKEN_PATTERN,
)
_JS_ESCAPE_PATTERN = re.compile(
    r"\\(?:u\{(?P<braced>[0-9a-fA-F]{1,6})\}|u(?P<unicode>[0-9a-fA-F]{4})|x(?P<hex>[0-9a-fA-F]{2}))"
)
_SBOM_NAME_PATTERN = re.compile(r"(?i)(?:^|[._-])(?:sbom|bom|cyclonedx|spdx|cdx)(?:[._-]|$)")
_NORMALIZATION_ROUNDS = 3
_NORMALIZATION_VERSION = "nfkc-percent-html-js-bom-utf16/v1"
_JS_DECODABLE_PUNCTUATION = frozenset("@/._\\- \t")
_REQUIREMENTS_SUFFIXES = (".in", ".lock", ".txt")
_PROJECT_MANIFEST_SUFFIXES = (".csproj", ".fsproj", ".nuspec", ".vbproj")
_DEPENDENCY_MANIFEST_GENERIC_SUFFIXES = (".lockfile",)
_PRODUCTION_TEST_FILENAME_MARKERS = (".spec.", ".test.")
_PAYLOAD_DECODINGS = ("utf-8", "utf-8-sig", "utf-16-bom")
_REQUIRED_REPOSITORY_INPUTS = (
    ("package.json", "file"),
    ("pnpm-lock.yaml", "file"),
    ("pnpm-workspace.yaml", "file"),
    ("pyproject.toml", "file"),
    ("alembic.ini", "file"),
    ("src/riftx", "directory"),
    ("src/riftx/__init__.py", "file"),
    ("migrations/env.py", "file"),
    ("migrations/versions", "directory"),
    ("apps/web/package.json", "file"),
    ("apps/web/src", "directory"),
    ("apps/web/src/main.tsx", "file"),
    ("apps/web/index.html", "file"),
    ("apps/web/vite.config.ts", "file"),
    ("apps/web/tsconfig.json", "file"),
    ("apps/demo/package.json", "file"),
    ("apps/demo/src", "directory"),
    ("apps/demo/src/main.tsx", "file"),
    ("apps/demo/index.html", "file"),
    ("apps/demo/vite.config.ts", "file"),
    ("apps/demo/tsconfig.json", "file"),
    ("apps/browser-extension/package.json", "file"),
    ("apps/browser-extension/src", "directory"),
    ("apps/browser-extension/src/connector.ts", "file"),
    ("apps/browser-extension/src/devtools.ts", "file"),
    ("apps/browser-extension/src/panel.ts", "file"),
    ("apps/browser-extension/static", "directory"),
    ("apps/browser-extension/static/manifest.json", "file"),
    ("apps/browser-extension/scripts/copy-static.mjs", "file"),
    ("apps/browser-extension/tsconfig.json", "file"),
    ("apps/burp-extension/build.gradle.kts", "file"),
    ("apps/burp-extension/settings.gradle.kts", "file"),
    ("apps/burp-extension/src/main", "directory"),
    (
        "apps/burp-extension/src/main/java/com/riftx/burp/RiftXBurpExtension.java",
        "file",
    ),
    ("configs/riftx.example.yaml", "file"),
    ("configs/models.example.yaml", "file"),
    ("configs/tools.example.yaml", "file"),
)
_FAIL_CLOSED_INVARIANTS = (
    "existing_non_symlink_repository_root",
    "required_repository_markers",
    "at_least_one_scanned_input",
    "source_and_artifact_symlink_rejection",
    "walk_and_read_errors",
    "explicit_artifact_presence_when_required",
    "explicit_artifacts_are_regular_files_or_directories",
    "nonempty_explicit_regular_files_and_archives",
    "archive_structure_and_size_limits",
    "unsupported_archive_compression_rejection",
    "tar_member_and_pax_metadata_scanning",
)

_DEPENDENCY_MANIFEST_NAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "cargo.lock",
        "cargo.toml",
        "composer.json",
        "composer.lock",
        "go.mod",
        "go.sum",
        "go.work",
        "go.work.sum",
        "gradle.lockfile",
        "libs.versions.toml",
        "mix.exs",
        "mix.lock",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "package.resolved",
        "package.json",
        "packages.config",
        "packages.lock.json",
        "pipfile",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "pnpm-workspace.yml",
        "pom.xml",
        "poetry.lock",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "settings.gradle",
        "settings.gradle.kts",
        "uv.lock",
        "yarn.lock",
    }
)
_REPOSITORY_WALK_EXCLUDES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pnpm-store",
        ".pytest_cache",
        ".riftx",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "archive",
        "build",
        "dist",
        "docs",
        "node_modules",
        "out",
        "target",
        "tests",
    }
)
_PRODUCTION_SOURCE_ROOTS = ("src", "migrations", "configs", "apps")
_PRODUCTION_WALK_EXCLUDES = frozenset(
    {
        ".git",
        ".impeccable",
        "__pycache__",
        "__tests__",
        "build",
        "dist",
        "fixtures",
        "golden",
        "node_modules",
        "out",
        "target",
        "test",
        "tests",
    }
)
_PRODUCTION_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".mjs",
        ".py",
        ".pyi",
        ".properties",
        ".rs",
        ".sh",
        ".sql",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".webmanifest",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_ZIP_SUFFIXES = frozenset({".jar", ".war", ".whl", ".zip"})
_SUPPORTED_TAR_SUFFIXES = (
    ".tar",
    ".tar.bz2",
    ".tbz",
    ".tbz2",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".crate",
)
_UNSUPPORTED_TAR_SHORTHAND_SUFFIXES = (".taz", ".tlz", ".tzst", ".tzstd")
_UNKNOWN_TAR_COMPRESSION_PATTERN = re.compile(r"(?i)\.tar\.[a-z0-9][a-z0-9_-]*$")
_ARCHIVE_SUFFIX_POLICY_VERSION = "riftx.archive-suffix-policy/v1"
_TAR_MEMBER_METADATA_FIELDS = ("linkname", "uname", "gname")
_TAR_METADATA_POLICY_VERSION = "riftx.tar-metadata-policy/v1"
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024


def _policy_digest() -> str:
    policy = {
        "version": INDEPENDENCE_POLICY_VERSION,
        "forbidden_identity_patterns": [
            pattern.pattern for pattern in _FORBIDDEN_IDENTITY_PATTERNS
        ],
        "js_escape_pattern": _JS_ESCAPE_PATTERN.pattern,
        "sbom_name_pattern": _SBOM_NAME_PATTERN.pattern,
        "normalization_rounds": _NORMALIZATION_ROUNDS,
        "normalization_version": _NORMALIZATION_VERSION,
        "js_decodable_punctuation": sorted(_JS_DECODABLE_PUNCTUATION),
        "requirements_suffixes": list(_REQUIREMENTS_SUFFIXES),
        "project_manifest_suffixes": list(_PROJECT_MANIFEST_SUFFIXES),
        "dependency_manifest_generic_suffixes": list(
            _DEPENDENCY_MANIFEST_GENERIC_SUFFIXES
        ),
        "production_test_filename_markers": list(_PRODUCTION_TEST_FILENAME_MARKERS),
        "payload_decodings": list(_PAYLOAD_DECODINGS),
        "required_repository_inputs": [
            {"path": path, "kind": kind} for path, kind in _REQUIRED_REPOSITORY_INPUTS
        ],
        "fail_closed_invariants": list(_FAIL_CLOSED_INVARIANTS),
        "dependency_manifests": sorted(_DEPENDENCY_MANIFEST_NAMES),
        "repository_walk_excludes": sorted(_REPOSITORY_WALK_EXCLUDES),
        "production_source_roots": list(_PRODUCTION_SOURCE_ROOTS),
        "production_walk_excludes": sorted(_PRODUCTION_WALK_EXCLUDES),
        "production_source_suffixes": sorted(_PRODUCTION_SOURCE_SUFFIXES),
        "zip_suffixes": sorted(_ZIP_SUFFIXES),
        "supported_tar_suffixes": list(_SUPPORTED_TAR_SUFFIXES),
        "unsupported_tar_shorthand_suffixes": list(
            _UNSUPPORTED_TAR_SHORTHAND_SUFFIXES
        ),
        "unknown_tar_compression_pattern": _UNKNOWN_TAR_COMPRESSION_PATTERN.pattern,
        "archive_suffix_policy_version": _ARCHIVE_SUFFIX_POLICY_VERSION,
        "tar_member_metadata_fields": list(_TAR_MEMBER_METADATA_FIELDS),
        "tar_metadata_policy_version": _TAR_METADATA_POLICY_VERSION,
        "max_file_bytes": _MAX_FILE_BYTES,
        "max_archive_members": _MAX_ARCHIVE_MEMBERS,
        "max_archive_uncompressed_bytes": _MAX_ARCHIVE_UNCOMPRESSED_BYTES,
    }
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class IndependenceInputKind(StrEnum):
    DEPENDENCY_MANIFEST = "dependency_manifest"
    PRODUCTION_SOURCE = "production_source"
    BUILD_ARTIFACT = "build_artifact"


class IndependenceBoundaryViolation(DomainModel):
    rule_id: str = Field(min_length=1)
    input_kind: IndependenceInputKind
    path: str = Field(min_length=1)
    archive_member: str | None = None
    line: int | None = Field(default=None, ge=1)
    detail: str = Field(min_length=1)


class IndependenceBoundaryReport(DomainModel):
    policy_version: str = INDEPENDENCE_POLICY_VERSION
    policy_digest: str = Field(default_factory=_policy_digest, pattern=r"^[0-9a-f]{64}$")
    ready: bool
    scanned_dependency_manifests: int = Field(ge=0)
    scanned_production_files: int = Field(ge=0)
    scanned_artifact_files: int = Field(ge=0)
    violations: list[IndependenceBoundaryViolation] = Field(default_factory=list)

    @model_validator(mode="after")
    def readiness_matches_violations(self) -> IndependenceBoundaryReport:
        if self.ready != (not self.violations):
            raise ValueError("independence readiness must match the violation set")
        return self


class IndependenceBoundaryScanner:
    """Scan owned production inputs without treating ordinary design documents as products."""

    def scan(
        self,
        repository_root: Path,
        *,
        artifacts: Iterable[Path] = (),
        require_artifact: bool = False,
    ) -> IndependenceBoundaryReport:
        root = repository_root.absolute()
        artifact_paths = tuple(artifacts)
        violations: list[IndependenceBoundaryViolation] = []
        if root.is_symlink() or not root.is_dir():
            self._append_failure(
                violations,
                rule_id="repository_root_invalid",
                path=root.as_posix(),
                detail="repository root must be an existing non-symlink directory",
                input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
            )
            return IndependenceBoundaryReport(
                ready=False,
                scanned_dependency_manifests=0,
                scanned_production_files=0,
                scanned_artifact_files=0,
                violations=violations,
            )
        for marker, marker_kind in _REQUIRED_REPOSITORY_INPUTS:
            marker_path = root / marker
            marker_type_matches = (
                marker_path.is_file() if marker_kind == "file" else marker_path.is_dir()
            )
            if marker_type_matches and not marker_path.is_symlink():
                continue
            self._append_failure(
                violations,
                rule_id="repository_marker_missing",
                path=marker,
                detail=f"required RiftX repository marker must be a non-symlink {marker_kind}",
                input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
            )

        manifests = self._dependency_manifests(root, violations=violations)
        production_files = self._production_files(root, violations=violations)

        if require_artifact and not artifact_paths:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_required",
                path=".",
                detail="this invocation requires at least one explicit build artifact",
            )

        for path in manifests:
            self._scan_file(
                path,
                root=root,
                input_kind=IndependenceInputKind.DEPENDENCY_MANIFEST,
                violations=violations,
            )
        for path in production_files:
            self._scan_file(
                path,
                root=root,
                input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
                violations=violations,
            )

        scanned_artifact_files = 0
        for artifact in artifact_paths:
            scanned_artifact_files += self._scan_explicit_artifact(
                artifact,
                root=root,
                violations=violations,
            )

        if len(manifests) + len(production_files) + scanned_artifact_files == 0:
            self._append_failure(
                violations,
                rule_id="repository_inputs_empty",
                path=".",
                detail="no dependency manifest, production input, or artifact file was inspectable",
                input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
            )

        violations.sort(
            key=lambda item: (
                item.path,
                item.archive_member or "",
                item.line or 0,
                item.rule_id,
            )
        )
        return IndependenceBoundaryReport(
            ready=not violations,
            scanned_dependency_manifests=len(manifests),
            scanned_production_files=len(production_files),
            scanned_artifact_files=scanned_artifact_files,
            violations=violations,
        )

    def _dependency_manifests(
        self,
        root: Path,
        *,
        violations: list[IndependenceBoundaryViolation],
    ) -> list[Path]:
        manifests: list[Path] = []
        if not root.is_dir():
            return manifests

        def onerror(error: OSError) -> None:
            failed_path = Path(error.filename) if error.filename else root
            self._append_failure(
                violations,
                rule_id="dependency_manifest_walk_failed",
                path=self._display_path(failed_path, root),
                detail=f"could not enumerate dependency inputs: {type(error).__name__}",
                input_kind=IndependenceInputKind.DEPENDENCY_MANIFEST,
            )

        for current, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=onerror,
        ):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                if name.casefold() in _REPOSITORY_WALK_EXCLUDES:
                    continue
                child = current_path / name
                if child.is_symlink():
                    self._append_failure(
                        violations,
                        rule_id="dependency_manifest_tree_symlink",
                        path=self._display_path(child, root),
                        detail="dependency input trees must not contain symbolic links",
                        input_kind=IndependenceInputKind.DEPENDENCY_MANIFEST,
                    )
                else:
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                path = current_path / name
                if _is_dependency_manifest(path):
                    manifests.append(path)
        return sorted(manifests)

    def _production_files(
        self,
        root: Path,
        *,
        violations: list[IndependenceBoundaryViolation],
    ) -> list[Path]:
        files: set[Path] = set()
        for relative_root in _PRODUCTION_SOURCE_ROOTS:
            source_root = root / relative_root
            if source_root.is_symlink():
                self._append_failure(
                    violations,
                    rule_id="production_source_symlink",
                    path=self._display_path(source_root, root),
                    detail="production source roots must not be symbolic links",
                    input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
                )
                continue
            if not source_root.is_dir():
                continue

            def onerror(error: OSError, scan_root: Path = source_root) -> None:
                failed_path = Path(error.filename) if error.filename else scan_root
                self._append_failure(
                    violations,
                    rule_id="production_source_walk_failed",
                    path=self._display_path(failed_path, root),
                    detail=f"could not enumerate production inputs: {type(error).__name__}",
                    input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
                )

            for current, directory_names, file_names in os.walk(
                source_root,
                followlinks=False,
                onerror=onerror,
            ):
                current_path = Path(current)
                safe_directories: list[str] = []
                for name in sorted(directory_names):
                    child = current_path / name
                    if child.is_symlink():
                        self._append_failure(
                            violations,
                            rule_id="production_source_symlink",
                            path=self._display_path(child, root),
                            detail="production source trees must not contain symbolic links",
                            input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
                        )
                    elif name.casefold() not in _PRODUCTION_WALK_EXCLUDES:
                        safe_directories.append(name)
                directory_names[:] = safe_directories
                for name in sorted(file_names):
                    path = current_path / name
                    if path.is_symlink():
                        self._append_failure(
                            violations,
                            rule_id="production_source_symlink",
                            path=self._display_path(path, root),
                            detail="production source trees must not contain symbolic links",
                            input_kind=IndependenceInputKind.PRODUCTION_SOURCE,
                        )
                        continue
                    if not path.is_file() or _is_dependency_manifest(path):
                        continue
                    lowered_name = name.casefold()
                    if any(
                        marker in lowered_name for marker in _PRODUCTION_TEST_FILENAME_MARKERS
                    ):
                        continue
                    if path.suffix.casefold() in _PRODUCTION_SOURCE_SUFFIXES:
                        files.add(path)
        return sorted(files)

    def _scan_explicit_artifact(
        self,
        artifact: Path,
        *,
        root: Path,
        violations: list[IndependenceBoundaryViolation],
    ) -> int:
        path = artifact if artifact.is_absolute() else root / artifact
        display_path = self._display_path(path, root)
        try:
            artifact_stat = path.lstat()
        except FileNotFoundError:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_missing",
                path=display_path,
                detail="explicit build artifact does not exist",
            )
            return 0
        except OSError as exc:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_unreadable",
                path=display_path,
                detail=f"could not inspect artifact type: {type(exc).__name__}",
            )
            return 0
        if stat.S_ISLNK(artifact_stat.st_mode):
            self._append_failure(
                violations,
                rule_id="explicit_artifact_symlink",
                path=display_path,
                detail="explicit build artifacts must not be symbolic links",
            )
            return 0
        if stat.S_ISREG(artifact_stat.st_mode):
            inspected = self._scan_file(
                path,
                root=root,
                input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                violations=violations,
                inspect_archive=True,
            )
            return int(inspected)
        if not stat.S_ISDIR(artifact_stat.st_mode):
            self._append_failure(
                violations,
                rule_id="explicit_artifact_unsupported",
                path=display_path,
                detail="explicit build artifact is neither a regular file nor a directory",
            )
            return 0

        scanned = 0

        def onerror(error: OSError) -> None:
            failed_path = Path(error.filename) if error.filename else path
            self._append_failure(
                violations,
                rule_id="explicit_artifact_walk_failed",
                path=self._display_path(failed_path, root),
                detail=f"could not enumerate artifact inputs: {type(error).__name__}",
            )

        for current, directory_names, file_names in os.walk(
            path,
            followlinks=False,
            onerror=onerror,
        ):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directory_names):
                child = current_path / name
                try:
                    child_stat = child.lstat()
                except OSError as exc:
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_unreadable",
                        path=self._display_path(child, root),
                        detail=f"could not inspect artifact entry type: {type(exc).__name__}",
                    )
                    continue
                if stat.S_ISLNK(child_stat.st_mode):
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_symlink",
                        path=self._display_path(child, root),
                        detail="artifact directory contains a symbolic link",
                    )
                elif stat.S_ISDIR(child_stat.st_mode):
                    safe_directories.append(name)
                else:
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_unsupported",
                        path=self._display_path(child, root),
                        detail="artifact tree entry is not a regular file or directory",
                    )
            directory_names[:] = safe_directories
            for name in sorted(file_names):
                child = current_path / name
                try:
                    child_stat = child.lstat()
                except OSError as exc:
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_unreadable",
                        path=self._display_path(child, root),
                        detail=f"could not inspect artifact entry type: {type(exc).__name__}",
                    )
                    continue
                if stat.S_ISLNK(child_stat.st_mode):
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_symlink",
                        path=self._display_path(child, root),
                        detail="artifact directory contains a symbolic link",
                    )
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_unsupported",
                        path=self._display_path(child, root),
                        detail="artifact tree entry is not a regular file or directory",
                    )
                    continue
                inspected = self._scan_file(
                    child,
                    root=root,
                    input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                    violations=violations,
                    inspect_archive=True,
                )
                scanned += int(inspected)
        if scanned == 0:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_empty",
                path=display_path,
                detail="explicit build artifact directory has no successfully inspected files",
            )
        return scanned

    def _scan_file(
        self,
        path: Path,
        *,
        root: Path,
        input_kind: IndependenceInputKind,
        violations: list[IndependenceBoundaryViolation],
        inspect_archive: bool = False,
    ) -> bool:
        display_path = self._display_path(path, root)
        try:
            file_stat = path.lstat()
        except OSError as exc:
            self._append_failure(
                violations,
                rule_id=f"{input_kind.value}_unreadable",
                path=display_path,
                detail=f"could not inspect input type: {type(exc).__name__}",
                input_kind=input_kind,
            )
            return False
        if stat.S_ISLNK(file_stat.st_mode):
            self._append_failure(
                violations,
                rule_id=f"{input_kind.value}_symlink",
                path=display_path,
                detail="boundary inputs must not be symbolic links",
                input_kind=input_kind,
            )
            return False
        if not stat.S_ISREG(file_stat.st_mode):
            rule_id = (
                "explicit_artifact_unsupported"
                if input_kind is IndependenceInputKind.BUILD_ARTIFACT
                else f"{input_kind.value}_unreadable"
            )
            self._append_failure(
                violations,
                rule_id=rule_id,
                path=display_path,
                detail="boundary input is not a regular file",
                input_kind=input_kind,
            )
            return False
        path_match = _identity_match(display_path)
        if path_match is not None:
            violations.append(
                IndependenceBoundaryViolation(
                    rule_id=f"forbidden_{input_kind.value}_path",
                    input_kind=input_kind,
                    path=display_path,
                    detail="path contains an upstream-specific identity",
                )
            )
        try:
            size = file_stat.st_size
            if input_kind is IndependenceInputKind.BUILD_ARTIFACT and size == 0:
                self._append_failure(
                    violations,
                    rule_id="explicit_artifact_empty",
                    path=display_path,
                    detail="explicit build artifact file is zero bytes",
                )
                return False
            if size > _MAX_FILE_BYTES:
                raise ValueError(f"file exceeds {_MAX_FILE_BYTES} byte boundary limit")
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            self._append_failure(
                violations,
                rule_id=f"{input_kind.value}_unreadable",
                path=display_path,
                detail=f"could not inspect input: {type(exc).__name__}",
                input_kind=input_kind,
            )
            return False

        self._scan_payload(
            payload,
            input_kind=input_kind,
            path=display_path,
            archive_member=None,
            violations=violations,
        )
        if inspect_archive:
            return self._scan_archive(
                path,
                display_path=display_path,
                violations=violations,
            )
        return True

    def _scan_archive(
        self,
        path: Path,
        *,
        display_path: str,
        violations: list[IndependenceBoundaryViolation],
    ) -> bool:
        lowered_name = path.name.casefold()
        if path.suffix.casefold() in _ZIP_SUFFIXES:
            return self._scan_zip(path, display_path=display_path, violations=violations)
        if lowered_name.endswith(_SUPPORTED_TAR_SUFFIXES):
            return self._scan_tar(path, display_path=display_path, violations=violations)
        if lowered_name.endswith(
            _UNSUPPORTED_TAR_SHORTHAND_SUFFIXES
        ) or _UNKNOWN_TAR_COMPRESSION_PATTERN.search(lowered_name):
            self._append_failure(
                violations,
                rule_id="explicit_artifact_unsupported_archive",
                path=display_path,
                detail="archive compression is not supported by the standard-library scanner",
            )
            return False
        return True

    def _scan_zip(
        self,
        path: Path,
        *,
        display_path: str,
        violations: list[IndependenceBoundaryViolation],
    ) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                if not members:
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_empty",
                        path=display_path,
                        detail="explicit ZIP artifact has no members",
                    )
                    return False
                if len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive member count exceeds boundary limit")
                total_size = sum(member.file_size for member in members)
                if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError("archive uncompressed size exceeds boundary limit")
                for member in members:
                    self._scan_archive_member_name(
                        display_path,
                        member.filename,
                        violations=violations,
                    )
                    if member.is_dir():
                        continue
                    if member.file_size > _MAX_FILE_BYTES:
                        raise ValueError("archive member exceeds per-file boundary limit")
                    self._scan_payload(
                        archive.read(member),
                        input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                        path=display_path,
                        archive_member=member.filename,
                        violations=violations,
                    )
            return True
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_invalid_archive",
                path=display_path,
                detail=f"could not inspect archive: {type(exc).__name__}",
            )
            return False

    def _scan_tar(
        self,
        path: Path,
        *,
        display_path: str,
        violations: list[IndependenceBoundaryViolation],
    ) -> bool:
        try:
            with tarfile.open(path, mode="r:*") as archive:
                members = archive.getmembers()
                if not members:
                    self._append_failure(
                        violations,
                        rule_id="explicit_artifact_empty",
                        path=display_path,
                        detail="explicit tar artifact has no members",
                    )
                    return False
                if len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive member count exceeds boundary limit")
                total_size = sum(member.size for member in members if member.isfile())
                if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError("archive uncompressed size exceeds boundary limit")
                for key, value in sorted(archive.pax_headers.items()):
                    self._scan_archive_metadata_value(
                        display_path,
                        key,
                        archive_member="<global-pax-key>",
                        violations=violations,
                    )
                    self._scan_archive_metadata_value(
                        display_path,
                        value,
                        archive_member=f"<global-pax:{key}>",
                        violations=violations,
                    )
                for member in members:
                    self._scan_archive_member_name(
                        display_path,
                        member.name,
                        violations=violations,
                    )
                    for field_name in _TAR_MEMBER_METADATA_FIELDS:
                        self._scan_archive_metadata_value(
                            display_path,
                            getattr(member, field_name),
                            archive_member=f"{member.name} [{field_name}]",
                            violations=violations,
                        )
                    for key, value in sorted(member.pax_headers.items()):
                        self._scan_archive_metadata_value(
                            display_path,
                            key,
                            archive_member=f"{member.name} [pax-key]",
                            violations=violations,
                        )
                        self._scan_archive_metadata_value(
                            display_path,
                            value,
                            archive_member=f"{member.name} [pax:{key}]",
                            violations=violations,
                        )
                    if not member.isfile():
                        continue
                    if member.size > _MAX_FILE_BYTES:
                        raise ValueError("archive member exceeds per-file boundary limit")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError("archive member could not be read")
                    self._scan_payload(
                        extracted.read(),
                        input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                        path=display_path,
                        archive_member=member.name,
                        violations=violations,
                    )
            return True
        except (OSError, ValueError, tarfile.TarError) as exc:
            self._append_failure(
                violations,
                rule_id="explicit_artifact_invalid_archive",
                path=display_path,
                detail=f"could not inspect archive: {type(exc).__name__}",
            )
            return False

    def _scan_archive_metadata_value(
        self,
        display_path: str,
        value: str,
        *,
        archive_member: str,
        violations: list[IndependenceBoundaryViolation],
    ) -> None:
        if not value or _identity_match(value) is None:
            return
        violations.append(
            IndependenceBoundaryViolation(
                rule_id="forbidden_build_artifact_path",
                input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                path=display_path,
                archive_member=archive_member,
                detail="archive metadata contains an upstream-specific identity",
            )
        )

    def _scan_archive_member_name(
        self,
        display_path: str,
        member_name: str,
        *,
        violations: list[IndependenceBoundaryViolation],
    ) -> None:
        if _identity_match(member_name) is None:
            return
        violations.append(
            IndependenceBoundaryViolation(
                rule_id="forbidden_build_artifact_path",
                input_kind=IndependenceInputKind.BUILD_ARTIFACT,
                path=display_path,
                archive_member=member_name,
                detail="archive member path contains an upstream-specific identity",
            )
        )

    def _scan_payload(
        self,
        payload: bytes,
        *,
        input_kind: IndependenceInputKind,
        path: str,
        archive_member: str | None,
        violations: list[IndependenceBoundaryViolation],
    ) -> None:
        text = _decode_payload(payload)
        matched = _normalized_identity_match(text)
        if matched is None:
            return
        normalized, match = matched
        violations.append(
            IndependenceBoundaryViolation(
                rule_id=f"forbidden_{input_kind.value}_content",
                input_kind=input_kind,
                path=path,
                archive_member=archive_member,
                line=normalized[: match.start()].count("\n") + 1,
                detail=(
                    "content contains an upstream-specific package, namespace, path, or endpoint"
                ),
            )
        )

    def _append_failure(
        self,
        violations: list[IndependenceBoundaryViolation],
        *,
        rule_id: str,
        path: str,
        detail: str,
        input_kind: IndependenceInputKind = IndependenceInputKind.BUILD_ARTIFACT,
    ) -> None:
        violations.append(
            IndependenceBoundaryViolation(
                rule_id=rule_id,
                input_kind=input_kind,
                path=path,
                detail=detail,
            )
        )

    def _display_path(self, path: Path, root: Path) -> str:
        try:
            return path.absolute().relative_to(root).as_posix()
        except ValueError:
            return path.absolute().as_posix()


def _is_dependency_manifest(path: Path) -> bool:
    lowered = path.name.casefold()
    if lowered in _DEPENDENCY_MANIFEST_NAMES:
        return True
    if lowered.startswith("requirements") and lowered.endswith(_REQUIREMENTS_SUFFIXES):
        return True
    if lowered.endswith(_PROJECT_MANIFEST_SUFFIXES):
        return True
    if lowered.endswith(_DEPENDENCY_MANIFEST_GENERIC_SUFFIXES):
        return True
    return _SBOM_NAME_PATTERN.search(lowered) is not None


def _decode_payload(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16", errors="replace")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig", errors="replace")
    return payload.decode("utf-8", errors="replace")


def _decode_js_escape(match: re.Match[str]) -> str:
    encoded = match.group("braced") or match.group("unicode") or match.group("hex")
    try:
        character = chr(int(encoded, 16))
    except (TypeError, ValueError, OverflowError):
        return match.group(0)
    if character.isalnum() or character in _JS_DECODABLE_PUNCTUATION:
        return character
    return match.group(0)


def _normalize_identity_input(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    for _ in range(_NORMALIZATION_ROUNDS):
        updated = _JS_ESCAPE_PATTERN.sub(_decode_js_escape, normalized)
        updated = html.unescape(updated)
        updated = unquote(updated)
        updated = unicodedata.normalize("NFKC", updated)
        if updated == normalized:
            break
        normalized = updated
    return normalized


def _normalized_identity_match(value: str) -> tuple[str, re.Match[str]] | None:
    normalized = _normalize_identity_input(value)
    matches = [
        match
        for pattern in _FORBIDDEN_IDENTITY_PATTERNS
        if (match := pattern.search(normalized)) is not None
    ]
    if not matches:
        return None
    match = min(matches, key=lambda candidate: candidate.start())
    return normalized, match


def _identity_match(value: str) -> re.Match[str] | None:
    matched = _normalized_identity_match(value)
    return None if matched is None else matched[1]
