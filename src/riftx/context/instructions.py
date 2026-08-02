"""Bounded hierarchical loading for trusted ``RIFTX.md`` instructions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from riftx.runtime.lifecycle import ContextCompileRequest

from .items import ContextItem, ContextItemKind, ContextLayer
from .token_counter import estimate_context_tokens

_INSTRUCTION_DIRECTORY = ".riftx"
_INSTRUCTION_FILE = "RIFTX.md"
_SYSTEM_LAYER_PREFIX = f"[{ContextLayer.STABLE_INSTRUCTIONS.value}]\n"
_TRUNCATION_MARKER = "\n\n[truncated to the Stable Instructions token budget]"


class StableInstructionScope(StrEnum):
    GLOBAL = "global"
    ENGAGEMENT = "engagement"
    WORKSPACE = "workspace"
    CURRENT_PATH = "current_path"


class StableInstructionLoadError(RuntimeError):
    """A configured instruction path cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class _InstructionCandidate:
    scope: StableInstructionScope
    root: Path
    path: Path
    sequence: int


@dataclass(frozen=True, slots=True)
class _InstructionDocument:
    candidate: _InstructionCandidate
    content: str


class StableInstructionSource:
    """Load trusted instruction files with specific-path precedence and a hard cap."""

    def __init__(
        self,
        *,
        global_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
        max_tokens: int = 4096,
        max_file_bytes: int = 1024 * 1024,
    ) -> None:
        if max_tokens < 64:
            raise ValueError("max_tokens must be at least 64")
        if max_file_bytes < 1024:
            raise ValueError("max_file_bytes must be at least 1024")
        env = os.environ if environment is None else environment
        config_root = Path(env.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
        self._global_path = global_path or config_root / "riftx" / _INSTRUCTION_FILE
        self.max_tokens = max_tokens
        self.max_file_bytes = max_file_bytes

    async def load(self, request: ContextCompileRequest) -> list[ContextItem]:
        candidates = self._candidates(request)
        documents: list[_InstructionDocument] = []
        for candidate in candidates:
            content = await asyncio.to_thread(self._read_if_present, candidate)
            if content is not None:
                documents.append(_InstructionDocument(candidate=candidate, content=content))
        if not documents:
            return []

        content_budget = self.max_tokens - estimate_context_tokens(_SYSTEM_LAYER_PREFIX)
        selected, dropped_paths, truncated_paths = _fit_documents(
            documents,
            max_tokens=max(1, content_budget),
        )
        if not selected:
            raise StableInstructionLoadError(
                "Stable Instruction token budget is too small to retain the most specific "
                "configured RIFTX.md"
            )
        content = "\n\n".join(rendered for _, rendered in selected)
        selected_candidates = [document.candidate for document, _ in selected]
        return [
            ContextItem(
                id="stable-instructions",
                layer=ContextLayer.STABLE_INSTRUCTIONS,
                kind=ContextItemKind.STABLE_INSTRUCTION,
                content=content,
                priority=100,
                required=True,
                compressible=False,
                removable=False,
                source_refs=[candidate.path.as_uri() for candidate in selected_candidates],
                metadata={
                    "instruction_scopes": [
                        candidate.scope.value for candidate in selected_candidates
                    ],
                    "instruction_paths": [str(candidate.path) for candidate in selected_candidates],
                    "dropped_instruction_paths": dropped_paths,
                    "truncated_instruction_paths": truncated_paths,
                    "stable_instruction_token_limit": self.max_tokens,
                },
            )
        ]

    def _candidates(self, request: ContextCompileRequest) -> list[_InstructionCandidate]:
        engagement = _optional_root(request.engagement_path)
        workspace = _optional_root(request.workspace_path)
        current = _optional_root(request.current_path)
        if (
            engagement is not None
            and workspace is not None
            and not workspace.is_relative_to(engagement)
        ):
            raise StableInstructionLoadError(
                f"workspace_path {str(workspace)!r} is outside engagement_path {str(engagement)!r}"
            )
        if current is not None and workspace is None:
            raise StableInstructionLoadError(
                "current_path requires workspace_path for instruction boundary validation"
            )
        if current is not None and workspace is not None and not current.is_relative_to(workspace):
            raise StableInstructionLoadError(
                f"current_path {str(current)!r} is outside workspace_path {str(workspace)!r}"
            )

        raw = [
            _InstructionCandidate(
                scope=StableInstructionScope.GLOBAL,
                root=self._global_path.parent,
                path=self._global_path.expanduser().resolve(strict=False),
                sequence=0,
            )
        ]
        for sequence, (scope, root) in enumerate(
            (
                (StableInstructionScope.ENGAGEMENT, engagement),
                (StableInstructionScope.WORKSPACE, workspace),
                (StableInstructionScope.CURRENT_PATH, current),
            ),
            start=1,
        ):
            if root is not None:
                raw.append(
                    _InstructionCandidate(
                        scope=scope,
                        root=root,
                        path=root / _INSTRUCTION_DIRECTORY / _INSTRUCTION_FILE,
                        sequence=sequence,
                    )
                )

        # If two scopes resolve to the same directory, retain only the more specific label.
        unique_reversed: list[_InstructionCandidate] = []
        seen: set[Path] = set()
        for candidate in reversed(raw):
            normalized = candidate.path.resolve(strict=False)
            if normalized in seen:
                continue
            seen.add(normalized)
            unique_reversed.append(candidate)
        return list(reversed(unique_reversed))

    def _read_if_present(self, candidate: _InstructionCandidate) -> str | None:
        path = candidate.path
        if not path.exists():
            return None
        if not path.is_file():
            raise StableInstructionLoadError(
                f"Stable Instruction path is not a regular file: {str(path)!r}"
            )
        resolved_root = candidate.root.expanduser().resolve(strict=False)
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root):
            raise StableInstructionLoadError(
                f"Stable Instruction file escapes its configured root: {str(path)!r}"
            )
        try:
            size = resolved_path.stat().st_size
            if size > self.max_file_bytes:
                raise StableInstructionLoadError(
                    f"Stable Instruction file exceeds {self.max_file_bytes} bytes: {str(path)!r}"
                )
            return resolved_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise StableInstructionLoadError(
                f"Unable to read Stable Instruction file {str(path)!r}: {exc}"
            ) from exc


def _optional_root(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    return Path(value).expanduser().resolve(strict=False)


def _fit_documents(
    documents: list[_InstructionDocument],
    *,
    max_tokens: int,
) -> tuple[list[tuple[_InstructionDocument, str]], list[str], list[str]]:
    selected_reversed: list[tuple[_InstructionDocument, str]] = []
    dropped: list[str] = []
    truncated: list[str] = []
    remaining = max_tokens
    for document in reversed(documents):
        rendered = _render_document(document)
        tokens = estimate_context_tokens(rendered)
        if tokens <= remaining:
            selected_reversed.append((document, rendered))
            remaining -= tokens
            continue
        fitted = _truncate_document(document, remaining)
        if fitted is not None:
            selected_reversed.append((document, fitted))
            truncated.append(str(document.candidate.path))
            remaining -= estimate_context_tokens(fitted)
        else:
            dropped.append(str(document.candidate.path))
        remaining_documents = documents[: documents.index(document)]
        dropped.extend(str(item.candidate.path) for item in remaining_documents)
        break
    selected = list(reversed(selected_reversed))
    dropped.reverse()
    return selected, list(dict.fromkeys(dropped)), list(reversed(truncated))


def _render_document(document: _InstructionDocument) -> str:
    candidate = document.candidate
    return (
        f"## {candidate.scope.value} RIFTX.md "
        f"(precedence {candidate.sequence})\n"
        f"Source: {candidate.path}\n\n"
        f"{document.content.strip()}"
    )


def _truncate_document(document: _InstructionDocument, max_tokens: int) -> str | None:
    header = _render_document(
        _InstructionDocument(candidate=document.candidate, content="")
    ).rstrip()
    minimum = f"{header}{_TRUNCATION_MARKER}"
    if estimate_context_tokens(minimum) > max_tokens:
        return None
    content = document.content.strip()
    low = 0
    high = len(content)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = f"{header}\n\n{content[:midpoint].rstrip()}{_TRUNCATION_MARKER}"
        if estimate_context_tokens(candidate) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return f"{header}\n\n{content[:low].rstrip()}{_TRUNCATION_MARKER}"
