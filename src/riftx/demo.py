"""Offline, sanitized security demos for a newly onboarded operator."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

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
_DEMO_TARGET = "https://portal.demo.invalid"


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


__all__ = [
    "DemoError",
    "PentestDemoResult",
    "PentestDemoStep",
    "run_pentest_demo",
]
