from __future__ import annotations

import os
from pathlib import Path

import pytest

from riftx.audit import (
    LOCAL_SOURCE_IDENTITY_DIGEST_DOMAIN,
    REPOSITORY_DESCRIPTOR_CHAIN_DIGEST_DOMAIN,
    REPOSITORY_IDENTITY_DIGEST_DOMAIN,
    SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN,
    LocalSourceKind,
    SourcePathAuthorizationError,
    SourcePathFailure,
    open_authorized_local_source,
    open_authorized_source_repository,
    validate_posix_absolute_path,
    validate_repository_filters,
    validate_repository_relative_path,
    validate_repository_relative_paths,
)
from riftx.audit import paths as paths_module


@pytest.mark.parametrize("value", ["/", "/srv/source", "/srv/代码/模块"])
def test_posix_absolute_path_accepts_canonical_values(value: str) -> None:
    assert validate_posix_absolute_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "relative/repository",
        "~/repository",
        "/srv/./repository",
        "/srv/../repository",
        "/srv//repository",
        "/srv/repository/",
        "//srv/repository",
        "/srv/repository\x00escape",
        "/srv/repository\ncanary",
    ],
)
def test_posix_absolute_path_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(SourcePathAuthorizationError) as captured:
        validate_posix_absolute_path(value)

    assert captured.value.failure is SourcePathFailure.INVALID_ABSOLUTE_PATH


@pytest.mark.parametrize(
    "value",
    ["src", "src/riftx/audit/paths.py", "文档/安全说明.md", "name with spaces/file.py"],
)
def test_repository_relative_path_accepts_canonical_values(value: str) -> None:
    assert validate_repository_relative_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "/src",
        "src/./audit",
        "src/../audit",
        "src//audit",
        "src/audit/",
        "src\\audit",
        "src/\x00audit",
        "src/\naudit",
    ],
)
def test_repository_relative_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(SourcePathAuthorizationError) as captured:
        validate_repository_relative_path(value)

    assert captured.value.failure is SourcePathFailure.INVALID_RELATIVE_PATH


def test_repository_filters_are_bounded_and_preserve_order() -> None:
    result = validate_repository_filters(
        include_paths=("src", "tests/unit"),
        exclude_paths=("src/generated",),
        max_paths=3,
        max_total_bytes=64,
    )

    assert result.include_paths == ("src", "tests/unit")
    assert result.exclude_paths == ("src/generated",)

    with pytest.raises(SourcePathAuthorizationError) as count_error:
        validate_repository_filters(
            include_paths=("src", "tests"),
            exclude_paths=("vendor", "generated"),
            max_paths=3,
        )
    assert count_error.value.failure is SourcePathFailure.FILTER_LIMIT_EXCEEDED

    with pytest.raises(SourcePathAuthorizationError) as byte_error:
        validate_repository_relative_path("source", max_path_bytes=5)
    assert byte_error.value.failure is SourcePathFailure.FILTER_LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("include_paths", "exclude_paths"),
    [
        (("src", "src"), ()),
        (("src",), ("src",)),
    ],
)
def test_repository_filters_reject_duplicates_and_exact_conflicts(
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
) -> None:
    with pytest.raises(SourcePathAuthorizationError) as captured:
        validate_repository_filters(
            include_paths=include_paths,
            exclude_paths=exclude_paths,
        )

    assert captured.value.failure is SourcePathFailure.FILTER_CONFLICT


def test_relative_path_sequence_rejects_a_string_instead_of_iterable_paths() -> None:
    with pytest.raises(SourcePathAuthorizationError) as captured:
        validate_repository_relative_paths("src")

    assert captured.value.failure is SourcePathFailure.INVALID_RELATIVE_PATH


def test_empty_allowed_roots_is_deny_all_before_filesystem_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    open_calls = 0
    original_open = os.open

    def counted_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_source_repository(repository, allowed_roots=())

    assert captured.value.failure is SourcePathFailure.SOURCE_ROOTS_EMPTY
    assert open_calls == 0


def test_outside_allowed_root_is_rejected_before_descriptor_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    open_calls = 0
    original_open = os.open

    def counted_open(*args, **kwargs):
        nonlocal open_calls
        open_calls += 1
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", counted_open)
    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_source_repository(outside, allowed_roots=(allowed,))

    assert captured.value.failure is SourcePathFailure.SOURCE_OUTSIDE_ROOT
    assert open_calls == 0


def test_ordinary_local_directory_has_stable_path_free_identity(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = root / "ordinary"
    source.mkdir(parents=True)

    with open_authorized_local_source(
        source,
        allowed_roots=(root,),
        include_paths=("src",),
        exclude_paths=("vendor",),
    ) as first:
        identity = first.identity
        assert first.source_kind is LocalSourceKind.DIRECTORY
        assert identity.schema_version == LOCAL_SOURCE_IDENTITY_DIGEST_DOMAIN
        assert identity.root_relative_path == "ordinary"
        assert first.filters.include_paths == ("src",)
        assert first.filters.exclude_paths == ("vendor",)
        assert not hasattr(identity, "canonical_path")
        first.verify_unchanged()

    with open_authorized_local_source(source, allowed_roots=(root,)) as replay:
        assert replay.source_identity_digest == identity.identity_digest
        assert replay.identity == identity


@pytest.mark.parametrize("marker_kind", ["directory", "file"])
def test_git_marked_local_directory_is_admitted_without_invoking_git(
    tmp_path: Path,
    marker_kind: str,
) -> None:
    root = tmp_path / "source"
    source = root / "repository"
    source.mkdir(parents=True)
    marker = source / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: ../metadata\n", encoding="utf-8")

    with open_authorized_local_source(source, allowed_roots=(root,)) as admitted:
        assert admitted.source_kind is LocalSourceKind.GIT_DIRECTORY
        assert len(admitted.source_identity_digest) == 64
        admitted.verify_unchanged()


def test_local_source_rejects_unsafe_git_marker_symlink(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = root / "repository"
    metadata = tmp_path / "metadata"
    source.mkdir(parents=True)
    metadata.mkdir()
    (source / ".git").symlink_to(metadata, target_is_directory=True)

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_local_source(source, allowed_roots=(root,))

    assert captured.value.failure is SourcePathFailure.SOURCE_GIT_MARKER_UNSAFE


def test_local_source_identity_detects_git_marker_kind_change(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = root / "repository"
    source.mkdir(parents=True)
    admitted = open_authorized_local_source(source, allowed_roots=(root,))
    try:
        (source / ".git").mkdir()
        with pytest.raises(SourcePathAuthorizationError) as captured:
            admitted.verify_unchanged()
        assert captured.value.failure is SourcePathFailure.SOURCE_CHANGED
    finally:
        admitted.close()


@pytest.mark.parametrize("placement", ["source_inside", "protected_inside", "equal"])
def test_local_source_rejects_protected_path_overlap(
    tmp_path: Path,
    placement: str,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    if placement == "source_inside":
        protected = root / "state"
        source = protected / "source"
        source.mkdir(parents=True)
    elif placement == "protected_inside":
        source = root / "source"
        source.mkdir()
        protected = source / ".riftx-state"
    else:
        source = root / "source"
        source.mkdir()
        protected = source

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_local_source(
            source,
            allowed_roots=(root,),
            protected_paths=(protected,),
        )

    assert captured.value.failure is SourcePathFailure.SOURCE_PROTECTED_PATH_OVERLAP


def test_local_source_protected_overlap_follows_existing_symlink_alias(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    source = root / "source"
    source.mkdir(parents=True)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_local_source(
            source,
            allowed_roots=(root,),
            protected_paths=(alias,),
        )

    assert captured.value.failure is SourcePathFailure.SOURCE_PROTECTED_PATH_OVERLAP


def test_local_source_path_and_depth_limits_are_enforced(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = root / "nested" / "repository"
    source.mkdir(parents=True)

    with pytest.raises(SourcePathAuthorizationError) as path_error:
        open_authorized_local_source(
            source,
            allowed_roots=(root,),
            max_source_path_bytes=len(str(source).encode("utf-8")) - 1,
        )
    assert path_error.value.failure is SourcePathFailure.SOURCE_LIMIT_EXCEEDED

    with pytest.raises(SourcePathAuthorizationError) as depth_error:
        open_authorized_local_source(
            source,
            allowed_roots=(root,),
            max_source_directory_depth=1,
        )
    assert depth_error.value.failure is SourcePathFailure.SOURCE_LIMIT_EXCEEDED


def test_local_source_context_entry_failure_closes_descriptors(tmp_path: Path) -> None:
    root = tmp_path / "source"
    source = root / "repository"
    source.mkdir(parents=True)
    admitted = open_authorized_local_source(source, allowed_roots=(root,))
    (source / ".git").mkdir()

    with pytest.raises(SourcePathAuthorizationError):
        with admitted:
            raise AssertionError("unreachable")

    assert admitted.closed is True


def test_authorized_repository_holds_root_and_repository_descriptors(
    tmp_path: Path,
) -> None:
    broad_root = tmp_path / "source"
    narrow_root = broad_root / "projects"
    repository = narrow_root / "riftx"
    repository.mkdir(parents=True)

    lease = open_authorized_source_repository(
        repository,
        allowed_roots=(broad_root, narrow_root),
    )
    root_fd = lease.root_fd
    repository_fd = lease.repository_fd
    try:
        assert lease.canonical_root == str(narrow_root)
        assert lease.canonical_repository == str(repository)
        assert lease.repository_relative_path == "riftx"
        assert os.fstat(root_fd).st_ino == narrow_root.stat().st_ino
        assert os.fstat(repository_fd).st_ino == repository.stat().st_ino
        assert lease.root_identity.schema_version == SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN
        assert lease.repository_identity.schema_version == REPOSITORY_IDENTITY_DIGEST_DOMAIN
        assert lease.source_root_identity_digest != lease.repository_identity_digest
        assert lease.descriptor_chain_digest not in {
            lease.source_root_identity_digest,
            lease.repository_identity_digest,
        }
        for digest in (
            lease.source_root_identity_digest,
            lease.repository_identity_digest,
            lease.descriptor_chain_digest,
        ):
            assert len(digest) == 64
            assert digest == digest.lower()
            int(digest, 16)
        lease.verify_unchanged()
    finally:
        lease.close()

    assert lease.closed is True
    for file_descriptor in (root_fd, repository_fd):
        with pytest.raises(OSError):
            os.fstat(file_descriptor)


def test_repository_may_equal_the_authorized_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with open_authorized_source_repository(
        repository,
        allowed_roots=(repository,),
    ) as lease:
        assert lease.repository_relative_path == "."
        assert lease.root_identity.inode == lease.repository_identity.inode
        lease.verify_unchanged()


def test_filesystem_root_can_anchor_a_descriptor_walk(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with open_authorized_source_repository(repository, allowed_roots=("/",)) as lease:
        assert lease.canonical_root == "/"
        assert lease.repository_relative_path == str(repository).removeprefix("/")
        lease.verify_unchanged()


@pytest.mark.parametrize("kind", ["root", "parent", "leaf"])
def test_descriptor_walk_rejects_symlink_components(tmp_path: Path, kind: str) -> None:
    real_root = tmp_path / "real-root"
    real_repository = real_root / "real-parent" / "repository"
    real_repository.mkdir(parents=True)

    if kind == "root":
        root = tmp_path / "root-link"
        root.symlink_to(real_root, target_is_directory=True)
        repository = root / "real-parent" / "repository"
    elif kind == "parent":
        root = real_root
        parent = root / "parent-link"
        parent.symlink_to(root / "real-parent", target_is_directory=True)
        repository = parent / "repository"
    else:
        root = real_root
        repository = root / "repository-link"
        repository.symlink_to(real_repository, target_is_directory=True)

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_source_repository(repository, allowed_roots=(root,))

    assert captured.value.failure in {
        SourcePathFailure.SOURCE_SYMLINK,
        SourcePathFailure.SOURCE_NOT_DIRECTORY,
    }


def test_descriptor_walk_rejects_non_directory_repository(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    repository = root / "not-a-directory"
    repository.write_text("content", encoding="utf-8")

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_source_repository(repository, allowed_roots=(root,))

    assert captured.value.failure is SourcePathFailure.SOURCE_NOT_DIRECTORY


def test_failed_descriptor_walk_closes_every_opened_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    missing_repository = root / "missing" / "repository"
    opened_descriptors: list[int] = []
    original_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        file_descriptor = original_open(path, flags, *args, **kwargs)
        opened_descriptors.append(file_descriptor)
        return file_descriptor

    monkeypatch.setattr(os, "open", recording_open)
    with pytest.raises(SourcePathAuthorizationError):
        open_authorized_source_repository(
            missing_repository,
            allowed_roots=(root,),
        )

    assert opened_descriptors
    for file_descriptor in set(opened_descriptors):
        with pytest.raises(OSError):
            os.fstat(file_descriptor)


@pytest.mark.parametrize("mutation", ["root", "parent", "repository", "mode"])
def test_held_descriptor_lease_detects_named_path_replacement_or_identity_change(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = tmp_path / "source"
    repository = root / "nested" / "repository"
    repository.mkdir(parents=True)
    lease = open_authorized_source_repository(repository, allowed_roots=(root,))
    try:
        if mutation == "root":
            old_root = tmp_path / "old-source"
            root.rename(old_root)
            (root / "nested" / "repository").mkdir(parents=True)
        elif mutation == "parent":
            parent = root / "nested"
            parent.rename(root / "old-nested")
            repository.mkdir(parents=True)
        elif mutation == "repository":
            repository.rename(repository.with_name("old-repository"))
            repository.mkdir()
        else:
            repository.chmod(0o700 if repository.stat().st_mode & 0o777 != 0o700 else 0o755)

        with pytest.raises(SourcePathAuthorizationError) as captured:
            lease.verify_unchanged()
        assert captured.value.failure is SourcePathFailure.SOURCE_CHANGED
    finally:
        lease.close()


def test_unrelated_repository_content_change_does_not_replace_path_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    repository = root / "repository"
    repository.mkdir(parents=True)

    with open_authorized_source_repository(repository, allowed_roots=(root,)) as lease:
        (repository / "new-file.py").write_text("pass\n", encoding="utf-8")
        lease.verify_unchanged()


def test_duplicate_descriptor_survives_lease_close(tmp_path: Path) -> None:
    root = tmp_path / "source"
    repository = root / "repository"
    repository.mkdir(parents=True)
    lease = open_authorized_source_repository(repository, allowed_roots=(root,))

    duplicate = lease.duplicate_repository_fd()
    expected_inode = lease.repository_identity.inode
    lease.close()
    try:
        assert os.get_inheritable(duplicate) is False
        assert os.fstat(duplicate).st_ino == expected_inode
    finally:
        os.close(duplicate)

    with pytest.raises(SourcePathAuthorizationError) as captured:
        lease.verify_unchanged()
    assert captured.value.failure is SourcePathFailure.DESCRIPTOR_CLOSED


def test_descriptor_identity_digests_are_stable_and_policy_bound(tmp_path: Path) -> None:
    root = tmp_path / "source"
    repository = root / "repository"
    repository.mkdir(parents=True)

    with open_authorized_source_repository(repository, allowed_roots=(root,)) as first:
        first_digests = (
            first.source_root_identity_digest,
            first.descriptor_chain_digest,
            first.repository_descriptor_identity_digest,
        )
    with open_authorized_source_repository(repository, allowed_roots=(root,)) as replay:
        assert (
            replay.source_root_identity_digest,
            replay.descriptor_chain_digest,
            replay.repository_descriptor_identity_digest,
        ) == first_digests
    with open_authorized_source_repository(
        repository,
        allowed_roots=(root,),
        policy_version="riftx.audit-source-path-policy/test-v2",
    ) as changed_policy:
        assert (
            changed_policy.source_root_identity_digest,
            changed_policy.descriptor_chain_digest,
            changed_policy.repository_descriptor_identity_digest,
        ) != first_digests


def test_descriptor_walk_uses_nofollow_directory_flags_for_every_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "source"
    repository = root / "nested" / "repository"
    repository.mkdir(parents=True)
    original_open = os.open
    component_flags: list[int] = []

    def recording_open(path, flags, *args, **kwargs):
        if kwargs.get("dir_fd") is not None:
            component_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    with open_authorized_source_repository(repository, allowed_roots=(root,)):
        pass

    assert component_flags
    assert all(flags & os.O_DIRECTORY for flags in component_flags)
    assert all(flags & os.O_NOFOLLOW for flags in component_flags)


def test_unsupported_descriptor_platform_fails_closed_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setattr(paths_module, "_descriptor_platform_supported", lambda: False)

    with pytest.raises(SourcePathAuthorizationError) as captured:
        open_authorized_source_repository(repository, allowed_roots=(tmp_path,))

    assert captured.value.failure is SourcePathFailure.PLATFORM_UNSUPPORTED


def test_identity_digest_domains_are_distinct() -> None:
    assert (
        len(
            {
                SOURCE_ROOT_IDENTITY_DIGEST_DOMAIN,
                REPOSITORY_DESCRIPTOR_CHAIN_DIGEST_DOMAIN,
                REPOSITORY_IDENTITY_DIGEST_DOMAIN,
            }
        )
        == 3
    )
