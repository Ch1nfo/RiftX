"""Generation-aware dynamic Tool discovery and model visibility sets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ExecutorType, ToolAvailability

from .models import ToolDefinition
from .registry import ToolNotFoundError, ToolRegistry

RESIDENT_TOOL_IDS: Final[tuple[str, ...]] = (
    "search_tools",
    "list_tools",
    "get_tool",
    "run_registered_tool",
    "run_shell",
    "get_execution",
    "wait_execution",
    "cancel_execution",
    "read_artifact",
    "delegate",
    "complete_run",
)
SUBAGENT_RESIDENT_TOOL_IDS: Final[tuple[str, ...]] = (
    "search_tools",
    "list_tools",
    "get_tool",
    "run_registered_tool",
    "get_execution",
    "wait_execution",
    "cancel_execution",
    "read_artifact",
)

_WORDS = re.compile(r"[a-z0-9]+")
_SYNONYM_GROUPS: Final[tuple[frozenset[str], ...]] = (
    frozenset({"smb", "cifs", "share", "shares"}),
    frozenset({"enumerate", "enumeration", "discover", "discovery", "recon", "list", "listing"}),
    frozenset({"http", "web", "website"}),
    frozenset({"vulnerability", "vulnerabilities", "vuln", "vulns", "cve"}),
    frozenset({"port", "ports", "service", "services"}),
    frozenset({"directory", "directories", "dir", "content", "path", "paths"}),
    frozenset({"password", "passwords", "credential", "credentials", "creds"}),
)
_SYNONYMS: Final[dict[str, frozenset[str]]] = {
    term: group for group in _SYNONYM_GROUPS for term in group
}


class ToolIndexEntry(BaseModel):
    """Level-0 Tool metadata safe to expose without loading a full schema."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    short_description: str = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    availability: ToolAvailability
    execution_type: ExecutorType


class ToolDetail(ToolIndexEntry):
    """Level-1 Tool detail; command and environment secrets remain hidden."""

    description: str = Field(min_length=1)
    approval_level: str
    timeout_seconds: float = Field(gt=0)
    version: str | None = None
    unavailable_reason: str | None = None


class ToolSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = ""
    capability: str | None = None
    max_results: int = Field(default=8, ge=1, le=100)
    include_unavailable: bool = True


class ToolSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: ToolIndexEntry
    score: float = Field(ge=0, le=1)
    matched_terms: list[str] = Field(default_factory=list)


class ToolSchema(BaseModel):
    """Level-2 full function schema loaded only after explicit selection."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    generation: int = Field(ge=1)
    full_schema: dict[str, object]


class ToolSelection(BaseModel):
    """Explicit selection response returned by the resident get_tool operation."""

    model_config = ConfigDict(extra="forbid")

    detail: ToolDetail
    full_schema: dict[str, object]
    generation: int = Field(ge=1)


class ToolVisibilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_tools: list[dict[str, object]] = Field(default_factory=list)
    always_visible_tools: list[str] = Field(default_factory=list)
    dynamically_loaded_tools: list[str] = Field(default_factory=list)
    hidden_available_tools: list[str] = Field(default_factory=list)
    hidden_unavailable_tools: list[str] = Field(default_factory=list)
    tool_registry_generation: int = Field(ge=1)

    def manifest(self) -> dict[str, object]:
        return self.model_dump(exclude={"available_tools"})


class DynamicToolIndex:
    """A lazy index derived from the current Tool Registry generation."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._generation: int | None = None
        self._entries: dict[str, ToolIndexEntry] = {}

    @property
    def generation(self) -> int:
        self._ensure_current()
        assert self._generation is not None
        return self._generation

    def list(self, *, include_unavailable: bool = True) -> list[ToolIndexEntry]:
        self._ensure_current()
        entries = self._entries.values()
        if not include_unavailable:
            entries = (
                item for item in entries if item.availability is ToolAvailability.AVAILABLE
            )
        return sorted(entries, key=lambda item: item.id)

    def get(self, tool_id: str) -> ToolIndexEntry:
        self._ensure_current()
        try:
            return self._entries[tool_id]
        except KeyError as exc:
            raise ToolNotFoundError(tool_id) from exc

    def detail(self, tool_id: str) -> ToolDetail:
        entry = self.get(tool_id)
        definition = self._registry.get(tool_id)
        state = self._registry.snapshot.states[tool_id]
        return ToolDetail(
            **entry.model_dump(),
            description=_description(definition),
            approval_level=definition.approval_level.value,
            timeout_seconds=definition.timeout_seconds,
            version=state.version,
            unavailable_reason=state.reason,
        )

    def schema(self, tool_id: str, *, require_available: bool = True) -> ToolSchema:
        self._ensure_current()
        definition = (
            self._registry.get_available(tool_id)
            if require_available
            else self._registry.get(tool_id)
        )
        return ToolSchema(
            tool_id=tool_id,
            generation=self.generation,
            full_schema=_function_schema(definition),
        )

    def search(self, request: ToolSearchRequest) -> list[ToolSearchResult]:
        self._ensure_current()
        query_terms = _expanded_terms(request.query)
        capability_terms = _expanded_terms(request.capability or "")
        results: list[ToolSearchResult] = []
        for entry in self._entries.values():
            if (
                not request.include_unavailable
                and entry.availability is not ToolAvailability.AVAILABLE
            ):
                continue
            definition = self._registry.get(entry.id)
            corpus = _tool_terms(definition)
            capability_corpus = _expanded_terms(" ".join(definition.capabilities))
            matched = sorted(query_terms & corpus)
            capability_matched = sorted(capability_terms & capability_corpus)
            if capability_terms and not capability_matched:
                continue
            if (
                query_terms
                and not matched
                and request.query.lower() not in _search_text(definition)
            ):
                continue
            score = _search_score(
                definition,
                query_terms=query_terms,
                matched_terms=set(matched),
                capability_terms=capability_terms,
                capability_matches=set(capability_matched),
                available=entry.availability is ToolAvailability.AVAILABLE,
                raw_query=request.query,
            )
            results.append(
                ToolSearchResult(
                    tool=entry,
                    score=round(min(1.0, score), 6),
                    matched_terms=sorted(set(matched) | set(capability_matched)),
                )
            )
        results.sort(key=lambda item: (-item.score, item.tool.id))
        return results[: request.max_results]

    def _ensure_current(self) -> None:
        snapshot = self._registry.snapshot
        if snapshot.generation == self._generation:
            return
        self._entries = {
            tool_id: ToolIndexEntry(
                id=tool_id,
                short_description=_short_description(definition),
                capabilities=list(definition.capabilities),
                availability=snapshot.states[tool_id].availability,
                execution_type=definition.executor,
            )
            for tool_id, definition in snapshot.definitions.items()
        }
        self._generation = snapshot.generation


@dataclass(slots=True)
class _ScopedToolSet:
    selected: set[str] = field(default_factory=set)
    allowed: set[str] | None = None


class ToolContextManager:
    """Own independent dynamic Tool Sets for each Runtime agent session."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        resident_tool_ids: tuple[str, ...] = RESIDENT_TOOL_IDS,
    ) -> None:
        self.registry = registry
        self.index = DynamicToolIndex(registry)
        self.resident_tool_ids = resident_tool_ids
        self._sets: dict[tuple[str, str, str], _ScopedToolSet] = {}

    def search_tools(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        request: ToolSearchRequest,
    ) -> list[ToolSearchResult]:
        scope = self._scope(run_id, session_id, agent_id)
        results = self.index.search(request)
        if scope.allowed is None:
            return results
        return [result for result in results if result.tool.id in scope.allowed]

    def list_tools(self, *, include_unavailable: bool = True) -> list[ToolIndexEntry]:
        return self.index.list(include_unavailable=include_unavailable)

    def get_tool(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> ToolSelection:
        schema = self.load_tool(
            tool_id,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        return ToolSelection(
            detail=self.index.detail(tool_id),
            full_schema=schema.full_schema,
            generation=schema.generation,
        )

    def describe_tool(self, tool_id: str) -> ToolDetail:
        return self.index.detail(tool_id)

    def load_tool(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> ToolSchema:
        self._require_allowed(
            tool_id,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        schema = self.index.schema(tool_id, require_available=True)
        self._scope(run_id, session_id, agent_id).selected.add(tool_id)
        return schema

    def unload_tool(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        self._scope(run_id, session_id, agent_id).selected.discard(tool_id)

    def restrict_tools(
        self,
        tool_ids: list[str],
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        """Apply an explicit registered-Tool allowlist to one isolated agent scope."""

        allowed = set(tool_ids)
        residents = set(self.resident_tool_ids)
        for tool_id in allowed - residents:
            self.index.schema(tool_id, require_available=True)
        scope = self._scope(run_id, session_id, agent_id)
        scope.allowed = allowed
        scope.selected.intersection_update(allowed)

    def assert_allowed(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        self._require_allowed(
            tool_id,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
        )

    def visibility(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> ToolVisibilitySnapshot:
        scope = self._scope(run_id, session_id, agent_id)
        snapshot = self.registry.snapshot
        resident = [
            tool_id
            for tool_id in dict.fromkeys(self.resident_tool_ids)
            if scope.allowed is None or tool_id in scope.allowed
        ]
        selected: list[str] = []
        schemas = [_resident_schema(tool_id) for tool_id in resident]
        for tool_id in sorted(scope.selected):
            definition = snapshot.definitions.get(tool_id)
            state = snapshot.states.get(tool_id)
            if definition is None or state is None:
                continue
            if state.availability is not ToolAvailability.AVAILABLE:
                continue
            if tool_id not in resident:
                selected.append(tool_id)
                schemas.append(self.index.schema(tool_id).full_schema)
        selected_set = set(selected)
        resident_set = set(resident)
        visible_ids = set(snapshot.states) if scope.allowed is None else scope.allowed
        hidden_available = sorted(
            tool_id
            for tool_id, state in snapshot.states.items()
            if state.availability is ToolAvailability.AVAILABLE
            and tool_id in visible_ids
            and tool_id not in selected_set
            and tool_id not in resident_set
        )
        hidden_unavailable = sorted(
            tool_id
            for tool_id, state in snapshot.states.items()
            if state.availability is not ToolAvailability.AVAILABLE
            and tool_id in visible_ids
            and tool_id not in resident_set
        )
        return ToolVisibilitySnapshot(
            available_tools=schemas,
            always_visible_tools=resident,
            dynamically_loaded_tools=selected,
            hidden_available_tools=hidden_available,
            hidden_unavailable_tools=hidden_unavailable,
            tool_registry_generation=snapshot.generation,
        )

    def _scope(self, run_id: str, session_id: str, agent_id: str) -> _ScopedToolSet:
        return self._sets.setdefault((run_id, session_id, agent_id), _ScopedToolSet())

    def _require_allowed(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        allowed = self._scope(run_id, session_id, agent_id).allowed
        if allowed is not None and tool_id not in allowed:
            raise ToolNotFoundError(tool_id)


def _short_description(definition: ToolDefinition) -> str:
    if definition.short_description:
        return definition.short_description
    if definition.description:
        first_sentence = definition.description.split(".", maxsplit=1)[0].strip()
        if first_sentence:
            return first_sentence
    capabilities = ", ".join(definition.capabilities) or "registered execution"
    return f"{definition.id} tool for {capabilities}"


def _description(definition: ToolDefinition) -> str:
    return definition.description or _short_description(definition)


def _function_schema(definition: ToolDefinition) -> dict[str, object]:
    parameters = definition.input_schema or {
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments appended to the registered command prefix.",
            },
            "environment": {
                "type": "object",
                "additionalProperties": {"type": ["string", "null"]},
            },
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
        },
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "name": definition.id,
        "description": _description(definition),
        "parameters": parameters,
        "x-riftx": {
            "tool_id": definition.id,
            "capabilities": list(definition.capabilities),
            "execution_type": definition.executor.value,
            "approval_level": definition.approval_level.value,
        },
    }


def _resident_schema(tool_id: str) -> dict[str, object]:
    descriptions = {
        "search_tools": "Search the node Tool Index by task language or capability.",
        "list_tools": "List lightweight Tool Index entries without loading schemas.",
        "get_tool": "Read detail for one registered tool and select its full schema.",
        "run_registered_tool": "Run a selected registered tool through the Runner.",
        "run_shell": "Run an authorized shell command through the Runner.",
        "get_execution": "Inspect a durable Execution by ID.",
        "wait_execution": "Wait for an Execution without changing its timeout policy.",
        "cancel_execution": "Cancel a durable Execution and its process group.",
        "read_artifact": "Read bounded content from a persisted Artifact.",
        "delegate": "Delegate one bounded independent task to an isolated Subagent.",
        "complete_run": "Request completion of the current authorized Run.",
    }
    properties: dict[str, object] = {}
    required: list[str] = []
    if tool_id == "search_tools":
        properties = {
            "query": {"type": "string"},
            "capability": {"type": ["string", "null"]},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        }
        required = ["query"]
    elif tool_id in {"get_tool", "run_registered_tool"}:
        properties = {"tool_id": {"type": "string"}}
        required = ["tool_id"]
    elif tool_id == "run_shell":
        properties = {
            "script": {
                "type": "string",
                "minLength": 1,
                "description": "Authorized shell script to execute.",
            },
            "cwd": {"type": ["string", "null"]},
            "environment": {
                "type": "object",
                "additionalProperties": {"type": ["string", "null"]},
            },
            "timeout_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
        }
        required = ["script"]
    elif tool_id in {"get_execution", "wait_execution", "cancel_execution"}:
        properties = {"execution_id": {"type": "string"}}
        required = ["execution_id"]
    elif tool_id == "read_artifact":
        properties = {"artifact_id": {"type": "string"}}
        required = ["artifact_id"]
    elif tool_id == "delegate":
        properties = {
            "task_id": {"type": "string"},
            "subagent_type": {"type": "string"},
            "task": {"type": "string"},
            "expected_output_schema": {"type": "object"},
            "run_contract_summary": {"type": "string"},
            "relevant_scope": {"type": "array", "items": {"type": "string"}},
            "selected_fact_ids": {"type": "array", "items": {"type": "string"}},
            "selected_artifact_refs": {
                "type": "array",
                "items": {"type": "string"},
            },
            "selected_memory_ids": {"type": "array", "items": {"type": "string"}},
            "available_tool_ids": {"type": "array", "items": {"type": "string"}},
            "workspace": {"type": "string"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "stop_conditions": {"type": "array", "items": {"type": "string"}},
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_tool_calls": {"type": "integer", "minimum": 0, "maximum": 1000},
            "token_budget": {"type": "integer", "minimum": 256},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
        }
        required = [
            "subagent_type",
            "task",
            "run_contract_summary",
            "relevant_scope",
            "workspace",
        ]
    return {
        "type": "function",
        "name": tool_id,
        "description": descriptions[tool_id],
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": tool_id != "run_shell",
        },
        "x-riftx": {"resident": True},
    }


def _search_text(definition: ToolDefinition) -> str:
    return " ".join(
        [
            definition.id,
            definition.short_description or "",
            definition.description or "",
            *definition.capabilities,
            *definition.synonyms,
        ]
    ).lower()


def _tool_terms(definition: ToolDefinition) -> set[str]:
    return _expanded_terms(_search_text(definition))


def _expanded_terms(text: str) -> set[str]:
    terms = set(_WORDS.findall(text.lower().replace("_", " ").replace("-", " ")))
    expanded = set(terms)
    for term in terms:
        expanded.update(_SYNONYMS.get(term, ()))
    return expanded


def _search_score(
    definition: ToolDefinition,
    *,
    query_terms: set[str],
    matched_terms: set[str],
    capability_terms: set[str],
    capability_matches: set[str],
    available: bool,
    raw_query: str,
) -> float:
    query_ratio = len(matched_terms) / max(1, len(query_terms))
    capability_ratio = len(capability_matches) / max(1, len(capability_terms))
    raw = raw_query.strip().lower()
    exact_id = raw == definition.id.lower()
    phrase = bool(raw and raw in _search_text(definition))
    return (
        0.40 * query_ratio
        + 0.35 * capability_ratio
        + 0.10 * float(exact_id)
        + 0.10 * float(phrase)
        + 0.05 * float(available)
    )
