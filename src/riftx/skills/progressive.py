"""Lazy parser and hot-reload catalog for file-backed Progressive Skills."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import yaml
from pydantic import ValidationError

from riftx.capabilities import CapabilitySource

from .models import (
    SkillDocument,
    SkillFrontMatter,
    SkillReference,
    SkillSearchResult,
    SkillSummary,
)

_REQUIRED_SECTIONS = (
    "When to use",
    "Preconditions",
    "Procedure",
    "Decision points",
    "Stop conditions",
    "Expected output",
    "Error handling",
)
_WORDS = re.compile(r"[a-z0-9]+")
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class SkillDocumentError(ValueError):
    pass


class SkillReferenceNotFoundError(FileNotFoundError):
    pass


@dataclass(frozen=True, slots=True)
class SkillPackageRoot:
    path: Path
    expected_source: CapabilitySource | None = None
    priority: int = 0


@dataclass(frozen=True, slots=True)
class _DiscoveredSkill:
    path: Path
    expected_source: CapabilitySource | None
    priority: int


class ProgressiveSkillRegistry:
    """Index front matter eagerly while keeping Skill bodies and references lazy."""

    def __init__(self, root: Path | Sequence[SkillPackageRoot]) -> None:
        self.roots = (
            (SkillPackageRoot(root),)
            if isinstance(root, Path)
            else tuple(root)
        )
        if not self.roots:
            raise ValueError("Progressive Skill registry requires at least one root")
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._generation = 0
        self._front_matter: dict[str, SkillFrontMatter] = {}
        self._digests: dict[str, str] = {}
        self._paths: dict[str, Path] = {}
        self._documents: dict[str, SkillDocument] = {}
        self._references: dict[str, SkillReference] = {}

    @property
    def generation(self) -> int:
        self._ensure_loaded()
        return self._generation

    @property
    def loaded_document_ids(self) -> frozenset[str]:
        return frozenset(self._documents)

    @property
    def loaded_reference_ids(self) -> frozenset[str]:
        return frozenset(self._references)

    def refresh(self) -> int:
        discovered = _discover_skill_paths(self.roots)
        paths = {skill_id: item.path for skill_id, item in discovered.items()}
        front_matter: dict[str, SkillFrontMatter] = {}
        digests: dict[str, str] = {}
        for skill_id, path in paths.items():
            try:
                front_matter[skill_id] = _read_front_matter(path)
                digests[skill_id] = _skill_digest(path)
            except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
                raise SkillDocumentError(f"invalid Skill front matter in {path}: {exc}") from exc
            expected_source = discovered[skill_id].expected_source
            if (
                expected_source is not None
                and front_matter[skill_id].source is not expected_source
            ):
                raise SkillDocumentError(
                    f"Skill package in {path.parent} must declare "
                    f"source={expected_source.value}"
                )
        self._paths = paths
        self._front_matter = front_matter
        self._digests = digests
        self._fingerprint = _fingerprint(self.roots)
        self._documents.clear()
        self._references.clear()
        self._generation += 1
        return self._generation

    def reload_if_changed(self) -> int:
        current = _fingerprint(self.roots)
        if self._fingerprint is not None and current == self._fingerprint:
            return self._generation
        return self.refresh()

    def list_summaries(self) -> list[SkillSummary]:
        self.reload_if_changed()
        return [self._summary(skill_id) for skill_id in sorted(self._front_matter)]

    def search(
        self,
        query: str,
        *,
        capability: str | None = None,
        max_results: int = 8,
    ) -> list[SkillSearchResult]:
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        self.reload_if_changed()
        query_terms = _terms(query)
        capability_terms = _terms(capability or "")
        results: list[SkillSearchResult] = []
        for skill_id, metadata in self._front_matter.items():
            capability_corpus = _terms(" ".join(metadata.required_capabilities))
            capability_matches = capability_terms & capability_corpus
            if capability_terms and not capability_matches:
                continue
            corpus = _terms(
                " ".join(
                    [
                        skill_id,
                        metadata.name,
                        metadata.description,
                        *metadata.required_capabilities,
                        *metadata.preferred_tools,
                    ]
                )
            )
            matches = query_terms & corpus
            if query_terms and not matches:
                continue
            score = (
                0.60 * (len(matches) / max(1, len(query_terms)))
                + 0.35 * (len(capability_matches) / max(1, len(capability_terms)))
                + 0.05
            )
            results.append(
                SkillSearchResult(
                    skill=self._summary(skill_id),
                    score=round(min(score, 1.0), 6),
                    matched_terms=sorted(matches | capability_matches),
                )
            )
        results.sort(key=lambda result: (-result.score, result.skill.id))
        return results[:max_results]

    def load_document(self, skill_id: str) -> SkillDocument:
        self.reload_if_changed()
        if skill_id in self._documents:
            return self._documents[skill_id]
        path = self._path(skill_id)
        content = path.read_text(encoding="utf-8")
        metadata, body = _parse_document(content, path)
        indexed = self._front_matter[skill_id]
        if metadata != indexed:
            raise SkillDocumentError(f"Skill front matter changed while loading {path}")
        sections = _parse_sections(body, path)
        input_schema, output_schema = _load_optional_schemas(path.parent)
        document = SkillDocument(
            **self._summary(skill_id).model_dump(),
            preferred_tools=metadata.preferred_tools,
            approval_level=metadata.approval_level,
            content=content,
            sections=sections,
            input_schema=input_schema,
            output_schema=output_schema,
        )
        self._documents[skill_id] = document
        return document

    def load_references(self, skill_id: str) -> SkillReference:
        self.reload_if_changed()
        if skill_id in self._references:
            return self._references[skill_id]
        reference_path = self._path(skill_id).with_name("REFERENCES.md")
        if not reference_path.is_file():
            raise SkillReferenceNotFoundError(
                f"Skill {skill_id!r} does not provide REFERENCES.md"
            )
        reference = SkillReference(
            skill_id=skill_id,
            version=str(self._front_matter[skill_id].version),
            digest=self._digests[skill_id],
            source=self._front_matter[skill_id].source,
            content=reference_path.read_text(encoding="utf-8"),
        )
        self._references[skill_id] = reference
        return reference

    def validate(self) -> list[SkillDocument]:
        self.refresh()
        return [self.load_document(skill_id) for skill_id in sorted(self._paths)]

    def _summary(self, skill_id: str) -> SkillSummary:
        metadata = self._front_matter[skill_id]
        return SkillSummary(
            id=skill_id,
            name=metadata.name,
            description=metadata.description,
            version=str(metadata.version),
            digest=self._digests[skill_id],
            source=metadata.source,
            required_capabilities=metadata.required_capabilities,
        )

    def _path(self, skill_id: str) -> Path:
        try:
            return self._paths[skill_id]
        except KeyError as exc:
            raise KeyError(skill_id) from exc

    def _ensure_loaded(self) -> None:
        if self._fingerprint is None:
            self.refresh()


def _discover_skill_paths(
    roots: Sequence[SkillPackageRoot],
) -> dict[str, _DiscoveredSkill]:
    discovered: dict[str, _DiscoveredSkill] = {}
    for root in sorted(roots, key=lambda item: (item.priority, str(item.path))):
        for skill_id, path in _discover_root_paths(root.path).items():
            existing = discovered.get(skill_id)
            if existing is not None and existing.priority == root.priority:
                raise SkillDocumentError(
                    f"Skill {skill_id!r} is duplicated across equal-priority roots"
                )
            if existing is None or root.priority > existing.priority:
                discovered[skill_id] = _DiscoveredSkill(
                    path=path,
                    expected_source=root.expected_source,
                    priority=root.priority,
                )
    return discovered


def _discover_root_paths(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    if not root.is_dir():
        raise SkillDocumentError(f"Skill root is not a directory: {root}")
    discovered: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        path = child / "SKILL.md"
        if not path.is_file():
            continue
        skill_id = child.name
        if not skill_id or any(character.isspace() for character in skill_id):
            raise SkillDocumentError(f"invalid Skill id: {skill_id!r}")
        discovered[skill_id] = path
    return discovered


def _fingerprint(
    roots: Sequence[SkillPackageRoot],
) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    patterns = (
        "*/SKILL.md",
        "*/REFERENCES.md",
        "*/schemas/input.json",
        "*/schemas/output.json",
    )
    for root in roots:
        source = root.expected_source.value if root.expected_source is not None else "any"
        prefix = f"{root.priority}:{source}:{root.path}"
        if not root.path.exists():
            entries.append((f"{prefix}:missing", 0, 0))
            continue
        if not root.path.is_dir():
            stat = root.path.stat()
            entries.append((f"{prefix}:not-directory", stat.st_mtime_ns, stat.st_size))
            continue
        for pattern in patterns:
            for path in root.path.glob(pattern):
                if path.is_file():
                    stat = path.stat()
                    entries.append(
                        (
                            f"{prefix}:{path.relative_to(root.path)}",
                            stat.st_mtime_ns,
                            stat.st_size,
                        )
                    )
    return tuple(sorted(entries))


def _skill_digest(path: Path) -> str:
    digest = sha256()
    directory = path.parent
    for relative in (
        Path("SKILL.md"),
        Path("REFERENCES.md"),
        Path("schemas/input.json"),
        Path("schemas/output.json"),
    ):
        candidate = directory / relative
        if not candidate.is_file():
            continue
        name = relative.as_posix().encode()
        content = candidate.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_front_matter(path: Path) -> SkillFrontMatter:
    with path.open(encoding="utf-8") as stream:
        if stream.readline().strip() != "---":
            raise SkillDocumentError("SKILL.md must begin with YAML front matter")
        lines: list[str] = []
        for line in stream:
            if line.strip() == "---":
                break
            lines.append(line)
        else:
            raise SkillDocumentError("SKILL.md front matter is missing its closing delimiter")
    raw = yaml.safe_load("".join(lines))
    if not isinstance(raw, dict):
        raise SkillDocumentError("SKILL.md front matter must be a mapping")
    return SkillFrontMatter.model_validate(raw)


def _parse_document(content: str, path: Path) -> tuple[SkillFrontMatter, str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillDocumentError(f"{path} must begin with YAML front matter")
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        ),
        None,
    )
    if closing is None:
        raise SkillDocumentError(f"{path} front matter is missing its closing delimiter")
    raw = yaml.safe_load("".join(lines[1:closing]))
    if not isinstance(raw, dict):
        raise SkillDocumentError(f"{path} front matter must be a mapping")
    return SkillFrontMatter.model_validate(raw), "".join(lines[closing + 1 :])


def _parse_sections(body: str, path: Path) -> dict[str, str]:
    matches = list(_SECTION.finditer(body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[heading.casefold()] = body[match.end() : end].strip()
    missing = [heading for heading in _REQUIRED_SECTIONS if heading.casefold() not in sections]
    empty = [
        heading
        for heading in _REQUIRED_SECTIONS
        if heading.casefold() in sections and not sections[heading.casefold()]
    ]
    if missing:
        raise SkillDocumentError(f"{path} is missing required sections: {', '.join(missing)}")
    if empty:
        raise SkillDocumentError(f"{path} has empty required sections: {', '.join(empty)}")
    return {heading: sections[heading.casefold()] for heading in _REQUIRED_SECTIONS}


def _load_optional_schemas(
    skill_directory: Path,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    input_path = skill_directory / "schemas" / "input.json"
    output_path = skill_directory / "schemas" / "output.json"
    if not input_path.exists() and not output_path.exists():
        return None, None
    if not input_path.is_file() or not output_path.is_file():
        raise SkillDocumentError(
            f"{skill_directory} must provide both schemas/input.json and schemas/output.json"
        )
    return _read_schema(input_path), _read_schema(output_path)


def _read_schema(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillDocumentError(f"invalid JSON schema {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SkillDocumentError(f"JSON schema {path} must be an object")
    return raw


def _terms(text: str) -> set[str]:
    return set(_WORDS.findall(text.lower().replace("_", " ").replace("-", " ")))
