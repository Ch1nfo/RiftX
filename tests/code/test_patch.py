from __future__ import annotations

import hashlib

import pytest

from riftx.application.errors import ApplicationConflictError
from riftx.code.patch import PatchFileState, parse_code_patch, prepare_code_patch


def test_update_patch_requires_unique_exact_context_and_preserves_crlf() -> None:
    original = b"def value():\r\n    return 1\r\n"
    digest = hashlib.sha256(original).hexdigest()
    parsed = parse_code_patch(
        "*** Begin Patch\n"
        "*** Update File: app.py\n"
        "@@ def value():\n"
        "-    return 1\n"
        "+    return 2\n"
        "*** End Patch"
    )
    prepared = prepare_code_patch(
        parsed,
        expected_sha256=digest,
        original=PatchFileState(content=original, sha256=digest, mode=0o644),
    )
    assert prepared.result_content == b"def value():\r\n    return 2\r\n"

    ambiguous = b"value = 1\nvalue = 1\n"
    ambiguous_digest = hashlib.sha256(ambiguous).hexdigest()
    parsed = parse_code_patch(
        "*** Begin Patch\n"
        "*** Update File: app.py\n"
        "@@\n"
        "-value = 1\n"
        "+value = 2\n"
        "*** End Patch"
    )
    with pytest.raises(ApplicationConflictError) as captured:
        prepare_code_patch(
            parsed,
            expected_sha256=ambiguous_digest,
            original=PatchFileState(
                content=ambiguous,
                sha256=ambiguous_digest,
                mode=0o644,
            ),
        )
    assert captured.value.code == "code_patch_context_mismatch"


def test_patch_parser_rejects_multiple_files() -> None:
    with pytest.raises(ApplicationConflictError) as captured:
        parse_code_patch(
            "*** Begin Patch\n"
            "*** Add File: one.txt\n"
            "+one\n"
            "*** Delete File: two.txt\n"
            "*** End Patch"
        )
    assert captured.value.code == "code_patch_multiple_files"
