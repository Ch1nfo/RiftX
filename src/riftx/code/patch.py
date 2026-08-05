"""Strict single-target patch parsing and digest-bound content derivation."""

from __future__ import annotations

import difflib
import hashlib
import hmac
from dataclasses import dataclass
from typing import Literal

from riftx.application.errors import ApplicationConflictError

_MAX_PATCH_BYTES = 256 * 1024
_MAX_PATCH_FILE_BYTES = 1024 * 1024
_MAX_DIFF_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PatchFileState:
    content: bytes
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class _PatchChunk:
    context: str | None
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    end_of_file: bool = False


@dataclass(frozen=True, slots=True)
class ParsedCodePatch:
    operation: Literal["add", "update", "delete"]
    path: str
    patch: str
    add_content: str | None = None
    chunks: tuple[_PatchChunk, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedCodePatch:
    operation: Literal["add", "update", "delete"]
    path: str
    patch: str
    patch_sha256: str
    original: PatchFileState | None
    result_content: bytes | None
    result_sha256: str | None
    diff: str
    diff_truncated: bool


def parse_code_patch(patch: str) -> ParsedCodePatch:
    if not patch or len(patch.encode("utf-8")) > _MAX_PATCH_BYTES or "\x00" in patch:
        raise _conflict("code_patch_invalid", "Patch is empty or exceeds the bounded limit")
    lines = patch.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise _conflict("code_patch_invalid", "Patch must start with '*** Begin Patch'")
    if lines[-1].strip() != "*** End Patch":
        raise _conflict("code_patch_invalid", "Patch must end with '*** End Patch'")
    body = lines[1:-1]
    if not body:
        raise _conflict("code_patch_invalid", "Patch must contain one file hunk")

    markers: tuple[tuple[str, Literal["add", "update", "delete"]], ...] = (
        ("*** Add File: ", "add"),
        ("*** Update File: ", "update"),
        ("*** Delete File: ", "delete"),
    )
    header = body[0]
    selected = next(
        ((prefix, operation) for prefix, operation in markers if header.startswith(prefix)),
        None,
    )
    if selected is None:
        raise _conflict("code_patch_invalid", "Patch contains an unsupported file hunk")
    prefix, operation = selected
    path = header.removeprefix(prefix)
    if not path:
        raise _conflict("code_patch_invalid", "Patch file path is empty")
    hunk_lines = body[1:]
    if any(line.startswith(tuple(prefix for prefix, _ in markers)) for line in hunk_lines):
        raise _conflict("code_patch_multiple_files", "One apply_patch call may change one file")

    if operation == "add":
        if not hunk_lines or any(not line.startswith("+") for line in hunk_lines):
            raise _conflict("code_patch_invalid", "Added file lines must start with '+'")
        return ParsedCodePatch(
            operation=operation,
            path=path,
            patch=patch,
            add_content="\n".join(line[1:] for line in hunk_lines) + "\n",
        )
    if operation == "delete":
        if hunk_lines:
            raise _conflict("code_patch_invalid", "Delete file hunk must not contain content")
        return ParsedCodePatch(operation=operation, path=path, patch=patch)
    return ParsedCodePatch(
        operation=operation,
        path=path,
        patch=patch,
        chunks=_parse_update_chunks(hunk_lines),
    )


def prepare_code_patch(
    parsed: ParsedCodePatch,
    *,
    expected_sha256: str | None,
    original: PatchFileState | None,
) -> PreparedCodePatch:
    if parsed.operation == "add":
        if expected_sha256 is not None:
            raise _conflict(
                "code_patch_expected_digest_invalid",
                "New files must not provide expected_sha256",
            )
        if original is not None:
            raise _conflict("code_patch_target_exists", "Added file already exists")
        assert parsed.add_content is not None
        result = parsed.add_content.encode("utf-8")
    else:
        if original is None:
            raise _conflict("code_patch_target_missing", "Patch target does not exist")
        if expected_sha256 is None or not hmac.compare_digest(
            expected_sha256,
            original.sha256,
        ):
            raise _conflict(
                "code_patch_digest_mismatch",
                "Patch target does not match expected_sha256",
            )
        if parsed.operation == "delete":
            result = None
        else:
            result = _apply_update_chunks(original.content, parsed.chunks)

    if result is not None and len(result) > _MAX_PATCH_FILE_BYTES:
        raise _conflict("code_patch_file_too_large", "Patched file exceeds the bounded limit")
    if original is not None and result == original.content:
        raise _conflict("code_patch_no_changes", "Patch does not change the target file")
    result_digest = hashlib.sha256(result).hexdigest() if result is not None else None
    diff, truncated = _bounded_diff(
        parsed.path,
        original.content if original is not None else None,
        result,
    )
    return PreparedCodePatch(
        operation=parsed.operation,
        path=parsed.path,
        patch=parsed.patch,
        patch_sha256=hashlib.sha256(parsed.patch.encode("utf-8")).hexdigest(),
        original=original,
        result_content=result,
        result_sha256=result_digest,
        diff=diff,
        diff_truncated=truncated,
    )


def validate_patch_receipt_content(
    *,
    content: bytes,
    expected_sha256: str | None,
) -> None:
    if len(content) > _MAX_PATCH_FILE_BYTES:
        raise _conflict("code_patch_receipt_invalid", "Receipt content exceeds the bounded limit")
    observed = hashlib.sha256(content).hexdigest()
    if expected_sha256 is None or not hmac.compare_digest(observed, expected_sha256):
        raise _conflict("code_patch_receipt_invalid", "Receipt content digest does not match")


def reverse_patch_diff(
    path: str,
    *,
    current: bytes | None,
    restored: bytes | None,
) -> tuple[str, bool]:
    return _bounded_diff(path, current, restored)


def _parse_update_chunks(lines: list[str]) -> tuple[_PatchChunk, ...]:
    if not lines:
        raise _conflict("code_patch_invalid", "Update file hunk is empty")
    chunks: list[_PatchChunk] = []
    context: str | None = None
    old_lines: list[str] = []
    new_lines: list[str] = []
    end_of_file = False

    def flush() -> None:
        nonlocal context, old_lines, new_lines, end_of_file
        if not old_lines and not new_lines:
            return
        chunks.append(
            _PatchChunk(
                context=context,
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                end_of_file=end_of_file,
            )
        )
        context = None
        old_lines = []
        new_lines = []
        end_of_file = False

    for index, line in enumerate(lines):
        if line == "@@" or line.startswith("@@ "):
            flush()
            context = line[3:] if line.startswith("@@ ") else None
            continue
        if line == "*** End of File":
            if index != len(lines) - 1:
                raise _conflict("code_patch_invalid", "End-of-file marker must be last")
            end_of_file = True
            continue
        if line == "":
            line = " "
        if line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        else:
            raise _conflict(
                "code_patch_invalid",
                "Update lines must start with space, '+', '-' or '@@'",
            )
    flush()
    if not chunks:
        raise _conflict("code_patch_invalid", "Update file hunk has no changes")
    return tuple(chunks)


def _apply_update_chunks(content: bytes, chunks: tuple[_PatchChunk, ...]) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _conflict(
            "code_patch_encoding_unsupported",
            "Patch target must be UTF-8 text",
        ) from exc
    lines, newline, trailing_newline = _split_text(text)
    cursor = 0
    for chunk in chunks:
        search_start = cursor
        if chunk.context is not None:
            context_positions = [
                index
                for index in range(cursor, len(lines))
                if lines[index] == chunk.context
            ]
            if len(context_positions) != 1:
                raise _conflict(
                    "code_patch_context_mismatch",
                    "Patch context marker is missing or ambiguous",
                )
            search_start = context_positions[0] + 1
        position = _unique_subsequence(lines, chunk.old_lines, start=search_start)
        if chunk.end_of_file and position + len(chunk.old_lines) != len(lines):
            raise _conflict("code_patch_context_mismatch", "Patch hunk is not at end of file")
        lines[position : position + len(chunk.old_lines)] = chunk.new_lines
        cursor = position + len(chunk.new_lines)
    rendered = newline.join(lines)
    if trailing_newline and lines:
        rendered += newline
    return rendered.encode("utf-8")


def _unique_subsequence(lines: list[str], needle: tuple[str, ...], *, start: int) -> int:
    if not needle:
        return start
    limit = len(lines) - len(needle) + 1
    positions = [
        index
        for index in range(start, max(start, limit))
        if tuple(lines[index : index + len(needle)]) == needle
    ]
    if len(positions) != 1:
        raise _conflict(
            "code_patch_context_mismatch",
            "Patch context is missing or ambiguous",
        )
    return positions[0]


def _split_text(text: str) -> tuple[list[str], str, bool]:
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise _conflict(
            "code_patch_encoding_unsupported",
            "Patch target has unsupported mixed line endings",
        )
    newline = "\r\n" if "\r\n" in text else "\n"
    if not text:
        return [], newline, False
    trailing = text.endswith(newline)
    body = text[: -len(newline)] if trailing else text
    return body.split(newline), newline, trailing


def _bounded_diff(
    path: str,
    original: bytes | None,
    result: bytes | None,
) -> tuple[str, bool]:
    before = _diff_lines(original)
    after = _diff_lines(result)
    diff = "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    encoded = diff.encode("utf-8")
    if len(encoded) <= _MAX_DIFF_BYTES:
        return diff, False
    return encoded[:_MAX_DIFF_BYTES].decode("utf-8", errors="ignore"), True


def _diff_lines(content: bytes | None) -> list[str]:
    if content is None:
        return []
    try:
        return content.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ["<binary content>\n"]


def _conflict(code: str, message: str) -> ApplicationConflictError:
    return ApplicationConflictError(code, message)


__all__ = [
    "ParsedCodePatch",
    "PatchFileState",
    "PreparedCodePatch",
    "parse_code_patch",
    "prepare_code_patch",
    "reverse_patch_diff",
    "validate_patch_receipt_content",
]
