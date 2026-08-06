"""Offline, sanitized security demos for a newly onboarded operator."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from riftx.audit import (
    DetectorRunLimits,
    FileInventory,
    LocalDetectorRunner,
    LocalSnapshotViewEntry,
    SnapshotBlobObjectType,
    SourceCaptureDecision,
    SourceCaptureReason,
    SourceClassification,
    SourceManifest,
    SourceManifestEntry,
    SourceManifestObjectType,
    SourceManifestOrigin,
    SourceManifestPath,
    SourceManifestSourceKind,
    build_file_inventory,
    builtin_detector_registry,
)
from riftx.config import RiftXConfig
from riftx.packs import OfficialPackCatalog
from riftx.tools import ToolConfigError, load_tool_config

_PENTEST_PACK_IDS = (
    "pentest-foundation",
    "scope-and-safety",
    "passive-recon",
    "service-enumeration",
    "web-attack-surface",
)
_OPTIONAL_PENTEST_TOOLS = ("nmap", "nuclei")
_CODE_AUDIT_PACK_ID = "code-audit-foundation"
_DEMO_TARGET = "https://portal.demo.invalid"
_DEMO_FILES = {
    "app.py": (
        "python",
        SourceClassification.SOURCE,
        'password = "demo-secret-value"\n\n'
        "def evaluate(user_input: str):\n"
        "    return eval(user_input)\n",
    ),
    "requirements.txt": (
        "text",
        SourceClassification.CONFIGURATION,
        "flask>=3.0\n",
    ),
    "settings.yaml": (
        "yaml",
        SourceClassification.CONFIGURATION,
        "debug: true\n",
    ),
}


class DemoError(RuntimeError):
    """Raised when a bundled authoritative Demo dependency is invalid."""


@dataclass(frozen=True, slots=True)
class PentestDemoStep:
    pack_id: str
    activity: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PentestDemoResult:
    sanitized: bool
    target: str
    pack_ids: tuple[str, ...]
    steps: tuple[PentestDemoStep, ...]
    available_optional_tools: tuple[str, ...]
    unavailable_optional_tools: tuple[str, ...]
    degradation_path: str
    tool_config_issue: str | None = None


@dataclass(frozen=True, slots=True)
class CodeAuditDemoFinding:
    rule_id: str
    relative_path: str
    line: int
    message: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CodeAuditDemoResult:
    sanitized: bool
    pack_id: str
    files_scanned: int
    findings: tuple[CodeAuditDemoFinding, ...]
    registry_digest: str
    run_digest: str
    degradation_path: str


def run_pentest_demo(
    config: RiftXConfig,
    *,
    catalog: OfficialPackCatalog | None = None,
) -> PentestDemoResult:
    """Play a no-network transcript backed by the installed Official Packs."""

    if not isinstance(config, RiftXConfig):
        raise DemoError("Demo configuration is invalid")
    _require_official_packs(_PENTEST_PACK_IDS, catalog or OfficialPackCatalog())
    available, unavailable, issue = _optional_tool_status(config.tools.path)
    steps = (
        PentestDemoStep(
            "scope-and-safety",
            "Admit scope and stop conditions",
            "Authorized scope is portal.demo.invalid only; transcript uses zero network I/O.",
        ),
        PentestDemoStep(
            "passive-recon",
            "Review a public-source lead",
            "Bundled source artifact mentions portal.demo.invalid; retained as an observation.",
        ),
        PentestDemoStep(
            "service-enumeration",
            "Classify a service observation",
            "Bundled artifact records TCP/443 as HTTPS; product and version remain unknown.",
        ),
        PentestDemoStep(
            "web-attack-surface",
            "Map routes and inputs",
            "Bundled HTTP exchange records GET /login and POST /session with one form input.",
        ),
        PentestDemoStep(
            "pentest-foundation",
            "Close with evidence discipline",
            "One observation and one negative result recorded; no vulnerability is claimed.",
        ),
    )
    return PentestDemoResult(
        sanitized=True,
        target=_DEMO_TARGET,
        pack_ids=_PENTEST_PACK_IDS,
        steps=steps,
        available_optional_tools=available,
        unavailable_optional_tools=unavailable,
        degradation_path=(
            "Use the bundled transcript and Official Pack workflow without external scanners; "
            "install and enable Nmap/Nuclei for later authorized live Runs."
        ),
        tool_config_issue=issue,
    )


def run_code_audit_demo(
    *,
    catalog: OfficialPackCatalog | None = None,
) -> CodeAuditDemoResult:
    """Run the production built-in static detectors over a bundled safe fixture."""

    _require_official_packs((_CODE_AUDIT_PACK_ID,), catalog or OfficialPackCatalog())
    inventory = build_file_inventory(
        SourceManifest.create(
            source_kind=SourceManifestSourceKind.DIRECTORY,
            commit_sha=None,
            head_commit_sha=None,
            capture_policy_digest=_digest(b"riftx-demo-policy"),
            entries=tuple(
                _manifest_entry(path, language, classification, content)
                for path, (language, classification, content) in sorted(_DEMO_FILES.items())
            ),
        )
    )
    registry = builtin_detector_registry()
    receipt = LocalDetectorRunner(registry, DetectorRunLimits()).run(
        view=_DemoView(inventory),
        inventory=inventory,
    )
    if receipt.cancelled or any(file.failures or file.reason for file in receipt.files):
        raise DemoError("Built-in code-audit Demo did not complete cleanly")
    return CodeAuditDemoResult(
        sanitized=True,
        pack_id=_CODE_AUDIT_PACK_ID,
        files_scanned=len(receipt.files),
        findings=tuple(
            CodeAuditDemoFinding(
                rule_id=signal.rule_id,
                relative_path=signal.relative_path,
                line=signal.line,
                message=signal.message,
                evidence=signal.evidence,
            )
            for signal in receipt.signals
        ),
        registry_digest=registry.registry_digest,
        run_digest=receipt.run_digest,
        degradation_path=(
            "Built-in static detectors remain available without a model, LSP, Control Plane, "
            "or external scanner."
        ),
    )


def _require_official_packs(pack_ids: tuple[str, ...], catalog: OfficialPackCatalog) -> None:
    try:
        bundles = {bundle.source.pack_id for bundle in catalog.load()}
    except (OSError, TypeError, ValueError) as exc:
        raise DemoError(f"Official Pack catalog is invalid: {exc}") from exc
    missing = tuple(pack_id for pack_id in pack_ids if pack_id not in bundles)
    if missing:
        raise DemoError("Required Official Packs are unavailable: " + ", ".join(missing))


def _optional_tool_status(
    tool_path: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    try:
        tools = load_tool_config(tool_path.expanduser())
    except (OSError, ToolConfigError) as exc:
        return (), _OPTIONAL_PENTEST_TOOLS, str(exc)
    available: list[str] = []
    unavailable: list[str] = []
    for tool_id in _OPTIONAL_PENTEST_TOOLS:
        definition = tools.tools.get(tool_id)
        if (
            definition is not None
            and definition.enabled
            and shutil.which(definition.command[0]) is not None
        ):
            available.append(tool_id)
        else:
            unavailable.append(tool_id)
    return tuple(available), tuple(unavailable), None


def _manifest_entry(
    relative_path: str,
    language: str,
    classification: SourceClassification,
    content: str,
) -> SourceManifestEntry:
    encoded = content.encode("utf-8")
    return SourceManifestEntry(
        path=SourceManifestPath.from_bytes(relative_path.encode("utf-8")),
        object_type=SourceManifestObjectType.REGULAR_FILE,
        origin=SourceManifestOrigin.LOCAL_DIRECTORY,
        mode=0o100644,
        size=len(encoded),
        sha256=_digest(encoded),
        git_blob_id=None,
        language=language,
        classification=classification,
        decision=SourceCaptureDecision.INCLUDED,
        reason=SourceCaptureReason.INCLUDED,
    )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class _DemoView:
    def __init__(self, inventory: FileInventory) -> None:
        included = inventory.included_entries()
        self._entries = tuple(
            LocalSnapshotViewEntry(
                relative_path=entry.relative_path or "invalid",
                object_type=SnapshotBlobObjectType.REGULAR_FILE,
                size=entry.size or 0,
                mode=0o100644,
                content_digest=entry.blob_digest or _digest(b"invalid"),
            )
            for entry in included
        )

    def entries(self) -> tuple[LocalSnapshotViewEntry, ...]:
        return self._entries

    def read_text(
        self,
        relative_path: str,
        *,
        max_bytes: int,
        max_characters: int,
    ) -> str:
        content = _DEMO_FILES[relative_path][2]
        if len(content.encode("utf-8")) > max_bytes or len(content) > max_characters:
            raise ValueError("Demo file exceeds detector limits")
        return content


__all__ = [
    "CodeAuditDemoFinding",
    "CodeAuditDemoResult",
    "DemoError",
    "PentestDemoResult",
    "PentestDemoStep",
    "run_code_audit_demo",
    "run_pentest_demo",
]
