"""Generation-aware dynamic Tool discovery and model visibility sets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from riftx.domain import ExecutorType, ToolAvailability

from .models import ExecutionPolicy, ToolDefinition
from .registry import ToolNotFoundError, ToolRegistry

RESIDENT_TOOL_IDS: Final[tuple[str, ...]] = (
    "search_tools",
    "list_tools",
    "get_tool",
    "search_mcp_tools",
    "get_mcp_tool",
    "call_mcp_tool",
    "search_skills",
    "list_skills",
    "load_skill",
    "load_skill_references",
    "unload_skill",
    "list_files",
    "read_file",
    "read_many_files",
    "grep",
    "glob",
    "symbol_search",
    "find_references",
    "call_hierarchy",
    "diagnostics",
    "apply_patch",
    "revert_patch",
    "git_status",
    "git_diff",
    "git_log",
    "open_browser",
    "observe_browser",
    "act_browser",
    "close_browser",
    "web_fetch",
    "web_search",
    "web_research",
    "query_http_traffic",
    "read_http_exchange",
    "target_http_request",
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
    "search_skills",
    "list_skills",
    "load_skill",
    "load_skill_references",
    "unload_skill",
    "list_files",
    "read_file",
    "read_many_files",
    "grep",
    "glob",
    "symbol_search",
    "find_references",
    "call_hierarchy",
    "diagnostics",
    "git_status",
    "git_diff",
    "git_log",
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

    execution_policy: ExecutionPolicy
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
            entries = (item for item in entries if item.availability is ToolAvailability.AVAILABLE)
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

    @property
    def execution_policy(self) -> ExecutionPolicy:
        return self.registry.config.execution_policy

    @property
    def visible_resident_tool_ids(self) -> tuple[str, ...]:
        """Return resident tools authorized by the current Registry policy."""

        if self.execution_policy is ExecutionPolicy.OPEN:
            return self.resident_tool_ids
        return tuple(tool_id for tool_id in self.resident_tool_ids if tool_id != "run_shell")

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

    def list_tools_for_scope(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
        include_unavailable: bool = True,
        max_results: int = 100,
    ) -> list[ToolIndexEntry]:
        """List bounded Tool metadata without leaking outside an Agent allowlist."""

        if max_results < 1 or max_results > 100:
            raise ValueError("max_results must be between 1 and 100")
        scope = self._scope(run_id, session_id, agent_id)
        entries = self.index.list(include_unavailable=include_unavailable)
        if scope.allowed is not None:
            entries = [entry for entry in entries if entry.id in scope.allowed]
        return entries[:max_results]

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
        residents = set(self.visible_resident_tool_ids)
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

    def assert_selected(
        self,
        tool_id: str,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> None:
        """Require an available registered Tool to have been explicitly loaded."""

        self._require_allowed(
            tool_id,
            run_id=run_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        if tool_id not in self._scope(run_id, session_id, agent_id).selected:
            raise ToolNotFoundError(tool_id)
        self.index.schema(tool_id, require_available=True)

    def visibility(
        self,
        *,
        run_id: str,
        session_id: str,
        agent_id: str,
    ) -> ToolVisibilitySnapshot:
        scope = self._scope(run_id, session_id, agent_id)
        snapshot = self.registry.snapshot
        execution_policy = self.execution_policy
        resident = [
            tool_id
            for tool_id in dict.fromkeys(self.visible_resident_tool_ids)
            if scope.allowed is None or tool_id in scope.allowed
        ]
        selected: list[str] = []
        schemas = [
            _resident_schema(tool_id, execution_policy=execution_policy) for tool_id in resident
        ]
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
            execution_policy=execution_policy,
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
        if tool_id == "run_shell" and self.execution_policy is not ExecutionPolicy.OPEN:
            raise ToolNotFoundError(tool_id)
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


def _resident_schema(
    tool_id: str,
    *,
    execution_policy: ExecutionPolicy,
) -> dict[str, object]:
    descriptions = {
        "search_tools": "Search the node Tool Index by task language or capability.",
        "list_tools": "List lightweight Tool Index entries without loading schemas.",
        "get_tool": "Read detail for one registered tool and select its full schema.",
        "search_mcp_tools": "Search bounded metadata for discovered MCP Tools.",
        "get_mcp_tool": "Read the bounded schema for one discovered MCP Tool.",
        "call_mcp_tool": (
            "Invoke one operator-allowlisted MCP Tool after explicit approval and save "
            "the sanitized full result as an immutable Artifact."
        ),
        "search_skills": "Search Progressive Skills available to this Agent Session.",
        "list_skills": "List lightweight Progressive Skill summaries.",
        "load_skill": "Pin and load one versioned Skill procedure for this Session.",
        "load_skill_references": "Load references for an already selected Skill.",
        "unload_skill": "Remove one selected Skill from the active model context.",
        "list_files": "List bounded entries in the current code source without following links.",
        "read_file": "Read a bounded preview from one regular file in the current code source.",
        "read_many_files": "Read bounded previews from several regular code files.",
        "grep": "Search literal text across bounded regular files in the current code source.",
        "glob": "Find bounded regular files by a relative glob pattern.",
        "symbol_search": "Search bounded source definitions using the safe built-in static index.",
        "find_references": (
            "Find bounded exact-name source references with explicit static ambiguity."
        ),
        "call_hierarchy": (
            "Find bounded incoming or outgoing name-level calls using AST or lexical analysis."
        ),
        "diagnostics": (
            "Read bounded built-in syntax and lexical diagnostics without starting project tools."
        ),
        "apply_patch": (
            "Apply one explicitly approved digest-bound code patch and save a revert receipt."
        ),
        "revert_patch": (
            "Revert one explicitly approved patch when the current digest matches its receipt."
        ),
        "git_status": "Read bounded Git worktree and index status without refreshing the index.",
        "git_diff": "Read a bounded working-tree or staged Git diff without external drivers.",
        "git_log": "Read bounded Git commit history without signatures, pager, or hooks.",
        "open_browser": (
            "Open one managed ephemeral browser at an authorized URL after explicit approval."
        ),
        "observe_browser": (
            "Read a bounded untrusted observation from this Agent Session's browser."
        ),
        "act_browser": (
            "Perform one explicitly approved scoped browser action against a fresh observation."
        ),
        "close_browser": "Close this Agent Session's managed browser.",
        "web_fetch": (
            "Fetch one anonymous public HTTP(S) document into canonical untrusted Sources "
            "and Artifacts after explicit approval."
        ),
        "web_search": (
            "Search configured public providers for bounded untrusted discovery candidates "
            "after explicit approval; candidates are not canonical Sources."
        ),
        "web_research": (
            "Search and fetch bounded public evidence into canonical Sources and a "
            "citation-safe untrusted Research packet after explicit approval."
        ),
        "query_http_traffic": (
            "Query bounded redacted metadata for Target HTTP exchanges in this Run."
        ),
        "read_http_exchange": (
            "Read one redacted Target HTTP exchange and its Run-bound Artifact references."
        ),
        "target_http_request": (
            "Send one scoped Target HTTP request through the Runner after explicit approval; "
            "request and response originals are saved as Artifacts."
        ),
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
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "include_unavailable": {"type": "boolean"},
        }
        required = ["query"]
    elif tool_id == "list_tools":
        properties = {
            "include_unavailable": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    elif tool_id == "get_tool":
        properties = {"tool_id": {"type": "string"}}
        required = ["tool_id"]
    elif tool_id == "search_mcp_tools":
        properties = {
            "query": {"type": "string", "maxLength": 2000},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        }
    elif tool_id == "get_mcp_tool":
        properties = {
            "tool_id": {"type": "string", "minLength": 1, "maxLength": 64},
        }
        required = ["tool_id"]
    elif tool_id == "call_mcp_tool":
        properties = {
            "tool_id": {"type": "string", "minLength": 1, "maxLength": 64},
            "arguments": {
                "type": "object",
                "additionalProperties": True,
                "maxProperties": 1024,
            },
        }
        required = ["tool_id", "arguments"]
    elif tool_id == "search_skills":
        properties = {
            "query": {"type": "string"},
            "capability": {"type": ["string", "null"]},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        }
        required = ["query"]
    elif tool_id == "list_skills":
        properties = {
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    elif tool_id == "load_skill":
        properties = {
            "skill_id": {"type": "string"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        }
        required = ["skill_id", "reason"]
    elif tool_id in {"load_skill_references", "unload_skill"}:
        properties = {"skill_id": {"type": "string"}}
        required = ["skill_id"]
    elif tool_id == "list_files":
        properties = {
            "path": {"type": "string", "maxLength": 4096},
            "recursive": {"type": "boolean"},
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    elif tool_id == "read_file":
        properties = {
            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
        }
        required = ["path"]
    elif tool_id == "read_many_files":
        properties = {
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                "minItems": 1,
                "maxItems": 20,
            },
            "max_bytes_per_file": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65536,
            },
            "max_total_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 131072,
            },
        }
        required = ["paths"]
    elif tool_id == "glob":
        properties = {
            "pattern": {"type": "string", "minLength": 1, "maxLength": 4096},
            "path": {"type": "string", "maxLength": 4096},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
        required = ["pattern"]
    elif tool_id == "grep":
        properties = {
            "query": {"type": "string", "minLength": 1, "maxLength": 4096},
            "path": {"type": "string", "maxLength": 4096},
            "file_glob": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "case_sensitive": {"type": "boolean"},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 200},
        }
        required = ["query"]
    elif tool_id == "git_status":
        properties = {
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
        }
    elif tool_id == "symbol_search":
        properties = {
            "query": {"type": "string", "minLength": 1, "maxLength": 1024},
            "path": {"type": "string", "maxLength": 4096},
            "file_glob": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "case_sensitive": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        }
        required = ["query"]
    elif tool_id == "find_references":
        properties = {
            "symbol": {"type": "string", "minLength": 1, "maxLength": 512},
            "path": {"type": "string", "maxLength": 4096},
            "file_glob": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "include_declarations": {"type": "boolean"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        }
        required = ["symbol"]
    elif tool_id == "call_hierarchy":
        properties = {
            "symbol": {"type": "string", "minLength": 1, "maxLength": 512},
            "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"]},
            "path": {"type": "string", "maxLength": 4096},
            "file_glob": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        }
        required = ["symbol"]
    elif tool_id == "diagnostics":
        properties = {
            "path": {"type": "string", "maxLength": 4096},
            "file_glob": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
        }
    elif tool_id == "apply_patch":
        properties = {
            "patch": {
                "type": "string",
                "minLength": 1,
                "maxLength": 262_144,
                "description": (
                    "One-file *** Begin Patch document using Add, Update, or Delete File."
                ),
            },
            "expected_sha256": {
                "type": ["string", "null"],
                "pattern": "^[0-9a-f]{64}$",
                "description": "Required for Update/Delete; null for Add.",
            },
        }
        required = ["patch"]
    elif tool_id == "revert_patch":
        properties = {
            "receipt_artifact_id": {"type": "string", "minLength": 1},
        }
        required = ["receipt_artifact_id"]
    elif tool_id == "open_browser":
        properties = {
            "url": {"type": "string", "minLength": 1, "maxLength": 8192},
            "include_screenshot": {"type": "boolean", "default": True},
        }
        required = ["url"]
    elif tool_id == "observe_browser":
        properties = {
            "browser_session_id": {"type": "string", "minLength": 1},
            "page_id": {"type": ["string", "null"], "minLength": 1},
            "include_screenshot": {"type": "boolean", "default": False},
            "include_network": {"type": "boolean", "default": True},
        }
        required = ["browser_session_id"]
    elif tool_id == "act_browser":
        properties = {
            "browser_session_id": {"type": "string", "minLength": 1},
            "page_id": {"type": "string", "minLength": 1},
            "observation_version": {"type": "integer", "minimum": 1},
            "action": {
                "type": "string",
                "enum": [
                    "navigate",
                    "click",
                    "fill",
                    "type",
                    "select",
                    "press",
                    "scroll",
                    "download",
                    "wait",
                    "go_back",
                    "reload",
                ],
            },
            "element_ref": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 255,
            },
            "value": {"type": ["string", "null"], "maxLength": 16_384},
            "url": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 8192,
            },
            "delay_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1000,
                "default": 0,
            },
            "scroll_delta_x": {
                "type": "integer",
                "minimum": -10_000,
                "maximum": 10_000,
                "default": 0,
            },
            "scroll_delta_y": {
                "type": "integer",
                "minimum": -10_000,
                "maximum": 10_000,
                "default": 700,
            },
            "wait_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 10_000,
                "default": 500,
            },
            "include_screenshot": {"type": "boolean", "default": True},
        }
        required = [
            "browser_session_id",
            "page_id",
            "observation_version",
            "action",
        ]
    elif tool_id == "close_browser":
        properties = {
            "browser_session_id": {"type": "string", "minLength": 1},
        }
        required = ["browser_session_id"]
    elif tool_id == "web_fetch":
        properties = {
            "url": {"type": "string", "minLength": 1, "maxLength": 8192},
            "cache_policy": {
                "type": "string",
                "enum": ["default", "bypass", "refresh", "no_store"],
                "default": "default",
            },
            "redirect_policy": {
                "type": "string",
                "enum": [
                    "none",
                    "same_origin_auto",
                    "all_auto",
                    "return_cross_origin",
                ],
                "default": "same_origin_auto",
            },
            "max_response_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10_000_000,
                "default": 2_000_000,
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 60,
                "default": 30,
            },
            "save_raw": {"type": "boolean", "default": True},
            "use_browser_fallback": {"type": "boolean", "default": True},
        }
        required = ["url"]
    elif tool_id == "web_search":
        properties = {
            "query": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 10,
            },
            "freshness": {
                "type": ["string", "null"],
                "enum": ["day", "week", "month", "year", None],
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string", "maxLength": 253},
                "maxItems": 20,
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string", "maxLength": 253},
                "maxItems": 20,
            },
            "language": {"type": ["string", "null"], "maxLength": 32},
            "region": {"type": ["string", "null"], "maxLength": 32},
            "search_type": {
                "type": "string",
                "enum": [
                    "general",
                    "documentation",
                    "security_advisory",
                    "cve",
                    "exploit",
                    "source_code",
                    "academic",
                    "news",
                ],
                "default": "general",
            },
        }
        required = ["query"]
    elif tool_id == "web_research":
        properties = {
            "question": {"type": "string", "minLength": 1, "maxLength": 4_000},
            "search_type": {
                "type": "string",
                "enum": [
                    "general",
                    "documentation",
                    "security_advisory",
                    "cve",
                    "exploit",
                    "source_code",
                    "academic",
                    "news",
                ],
                "default": "general",
            },
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string", "maxLength": 253},
                "maxItems": 20,
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string", "maxLength": 253},
                "maxItems": 20,
            },
            "max_queries": {
                "type": "integer",
                "minimum": 1,
                "maximum": 4,
                "default": 4,
            },
            "max_results_per_query": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 10,
            },
            "max_total_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "default": 30,
            },
            "max_sources": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6,
                "default": 6,
            },
        }
        required = ["question"]
    elif tool_id == "query_http_traffic":
        properties = {
            "method": {"type": ["string", "null"], "minLength": 1, "maxLength": 32},
            "status_class": {
                "type": ["string", "null"],
                "enum": [
                    "informational",
                    "success",
                    "redirect",
                    "client_error",
                    "server_error",
                    None,
                ],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "cursor": {"type": ["string", "null"], "minLength": 1, "maxLength": 4096},
        }
    elif tool_id == "read_http_exchange":
        properties = {
            "exchange_id": {"type": "string", "minLength": 1, "maxLength": 256},
        }
        required = ["exchange_id"]
    elif tool_id == "target_http_request":
        properties = {
            "method": {"type": "string", "minLength": 1, "maxLength": 32},
            "url": {"type": "string", "minLength": 1, "maxLength": 8192},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "maxProperties": 100,
            },
            "query": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "maxProperties": 100,
            },
            "body": {"type": ["string", "null"], "maxLength": 1_000_000},
            "json_body": {
                "type": ["object", "array", "null"],
            },
            "cookies": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "maxProperties": 100,
            },
            "verify_tls": {"type": "boolean", "default": True},
            "follow_redirects": {"type": "boolean", "default": False},
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 60,
                "default": 30,
            },
            "max_response_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10_000_000,
                "default": 2_000_000,
            },
        }
        required = ["method", "url"]
    elif tool_id == "git_diff":
        properties = {
            "path": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "staged": {"type": "boolean"},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 20},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
        }
    elif tool_id == "git_log":
        properties = {
            "path": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 4096,
            },
            "max_entries": {"type": "integer", "minimum": 1, "maximum": 100},
        }
    elif tool_id == "run_registered_tool":
        properties = {
            "tool_id": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": {"type": ["string", "null"]},
            "environment": {
                "type": "object",
                "additionalProperties": {"type": ["string", "null"]},
            },
            "timeout_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
            "reason": {"type": "string"},
        }
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
    elif tool_id == "get_execution":
        properties = {"execution_id": {"type": "string"}}
        required = ["execution_id"]
    elif tool_id == "wait_execution":
        properties = {
            "execution_id": {"type": "string"},
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 30,
            },
            "stdout_cursor": {"type": "integer", "minimum": 0},
            "stderr_cursor": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
            "next_poll_after_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3600,
            },
        }
        required = ["execution_id"]
    elif tool_id == "cancel_execution":
        properties = {
            "execution_id": {"type": "string"},
            "reason": {"type": ["string", "null"], "maxLength": 1000},
        }
        required = ["execution_id"]
    elif tool_id == "read_artifact":
        properties = {
            "artifact_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
        }
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
            "available_skill_ids": {"type": "array", "items": {"type": "string"}},
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
    elif tool_id == "complete_run":
        properties = {
            "run_summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16_384,
            }
        }
        required = ["run_summary"]
    metadata: dict[str, object] = {
        "resident": True,
        "execution_policy": execution_policy.value,
    }
    if tool_id in {
        "apply_patch",
        "revert_patch",
        "open_browser",
        "act_browser",
        "web_fetch",
        "web_search",
        "web_research",
        "target_http_request",
        "call_mcp_tool",
    }:
        metadata.update(
            {
                "approval_level": "always",
                "approval_policy": "explicit",
            }
        )
    return {
        "type": "function",
        "name": tool_id,
        "description": descriptions[tool_id],
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "x-riftx": metadata,
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
