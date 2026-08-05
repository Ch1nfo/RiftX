"""Built-in local-static security detectors for the simplified Code Audit."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .detectors import (
    DetectorInput,
    DetectorMatch,
    DetectorRegistry,
    DetectorRuleMetadata,
)
from .source_manifest import SourceClassification

BUILTIN_DETECTOR_SET_VERSION = "riftx.builtin-detectors/v1"


def _metadata(
    rule_id: str,
    title: str,
    contract: str,
    *,
    languages: tuple[str, ...] = (),
    categories: tuple[SourceClassification, ...] = (),
) -> DetectorRuleMetadata:
    return DetectorRuleMetadata(
        rule_id=rule_id,
        version="1.0.0",
        implementation_digest=hashlib.sha256(
            f"{BUILTIN_DETECTOR_SET_VERSION}\0{rule_id}\0{contract}".encode()
        ).hexdigest(),
        title=title,
        supported_languages=languages,
        supported_categories=categories,
    )


def _line_match(line_number: int, line: str, start: int, message: str, evidence: str):
    return DetectorMatch(
        line=line_number,
        column=start + 1,
        end_line=line_number,
        end_column=max(start + 2, len(line) + 1),
        message=message,
        evidence=evidence.strip() or "[redacted]",
    )


@dataclass(frozen=True, slots=True)
class SecretDetector:
    metadata = _metadata(
        "secret.hardcoded_credential",
        "Hard-coded credential",
        "private-key/aws/github/generic-assignment-redacted-v1",
    )

    _specific = re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
        r"\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{36,255}\b"
    )
    _assignment = re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*([\"'])([^\"'\r\n]{8,})\2"
    )

    def detect(self, detector_input: DetectorInput):
        matches = []
        for number, line in enumerate(detector_input.content.splitlines(), 1):
            specific = self._specific.search(line)
            if specific is not None:
                matches.append(
                    _line_match(
                        number,
                        line,
                        specific.start(),
                        "Credential material is hard-coded",
                        "[credential redacted]",
                    )
                )
                continue
            assignment = self._assignment.search(line)
            if assignment is None or _placeholder(assignment.group(3)):
                continue
            evidence = f"{line[: assignment.start(3)]}[REDACTED]{line[assignment.end(3) :]}"
            matches.append(
                _line_match(
                    number, line, assignment.start(1), "Credential value is hard-coded", evidence
                )
            )
        return tuple(matches)


@dataclass(frozen=True, slots=True)
class DependencyDetector:
    metadata = _metadata(
        "dependency.unpinned",
        "Unpinned dependency",
        "requirements-package-json-pyproject-mutable-version-v1",
    )

    def detect(self, detector_input: DetectorInput):
        name = PurePosixPath(detector_input.relative_path).name.lower()
        if name in {"requirements.txt", "requirements-dev.txt", "constraints.txt"}:
            return _requirements_matches(detector_input.content)
        if name == "package.json":
            return _package_json_matches(detector_input.content)
        if name == "pyproject.toml":
            return _pyproject_matches(detector_input.content)
        return ()


@dataclass(frozen=True, slots=True)
class ConfigurationDetector:
    metadata = _metadata(
        "configuration.insecure_setting",
        "Insecure configuration",
        "debug-tls-cors-root-container-v1",
        categories=(SourceClassification.CONFIGURATION, SourceClassification.DATA),
    )
    _patterns = (
        (re.compile(r"(?i)\bdebug\s*[:=]\s*true\b"), "Debug mode is enabled"),
        (
            re.compile(
                r"(?i)\b(?:verify_ssl|ssl_verify|tls_verify|rejectUnauthorized)\s*[:=]\s*false\b"
            ),
            "TLS certificate verification is disabled",
        ),
        (
            re.compile(r"(?i)\binsecure_skip_verify\s*[:=]\s*true\b"),
            "TLS certificate verification is disabled",
        ),
        (
            re.compile(r"(?i)\b(?:allow_origins|cors_origins?)\s*[:=].*[\"']\*[\"']"),
            "CORS allows every origin",
        ),
        (re.compile(r"(?i)^\s*USER\s+root\s*$"), "Container explicitly runs as root"),
    )

    def detect(self, detector_input: DetectorInput):
        matches = []
        for number, line in enumerate(detector_input.content.splitlines(), 1):
            for pattern, message in self._patterns:
                found = pattern.search(line)
                if found is not None:
                    matches.append(_line_match(number, line, found.start(), message, line))
                    break
        return tuple(matches)


@dataclass(frozen=True, slots=True)
class PythonDetector:
    metadata = _metadata(
        "python.dangerous_api",
        "Dangerous Python API",
        "ast-eval-exec-os-system-subprocess-shell-pickle-yaml-v1",
        languages=("python",),
    )

    def detect(self, detector_input: DetectorInput):
        try:
            tree = ast.parse(detector_input.content)
        except SyntaxError:
            return ()
        lines = detector_input.content.split("\n")
        matches = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            message = None
            if name in {"eval", "exec", "os.system", "pickle.load", "pickle.loads"}:
                message = f"Dangerous API {name} is used"
            elif name == "yaml.load" and not _safe_yaml_loader(node):
                message = "yaml.load is used without SafeLoader"
            elif name.startswith("subprocess.") and _keyword_true(node, "shell"):
                message = "Subprocess is invoked with shell=True"
            if message is None:
                continue
            line_number = getattr(node, "lineno", 1)
            line = lines[line_number - 1] if line_number <= len(lines) else name
            matches.append(
                _line_match(line_number, line, getattr(node, "col_offset", 0), message, line)
            )
        return tuple(matches)


@dataclass(frozen=True, slots=True)
class JavaScriptDetector:
    metadata = _metadata(
        "javascript.dangerous_api",
        "Dangerous JavaScript API",
        "eval-function-child-process-dom-tls-v1",
        languages=("javascript", "typescript"),
    )
    _patterns = (
        (re.compile(r"\beval\s*\("), "eval executes dynamically constructed code"),
        (
            re.compile(r"\bnew\s+Function\s*\("),
            "Function constructor executes dynamically constructed code",
        ),
        (
            re.compile(r"\b(?:child_process\.)?exec(?:Sync)?\s*\("),
            "child_process exec invokes a command shell",
        ),
        (re.compile(r"\.innerHTML\s*="), "innerHTML assignment can enable DOM injection"),
        (
            re.compile(r"\bdangerouslySetInnerHTML\b"),
            "dangerouslySetInnerHTML can enable DOM injection",
        ),
        (
            re.compile(r"\brejectUnauthorized\s*:\s*false\b"),
            "TLS certificate verification is disabled",
        ),
    )

    def detect(self, detector_input: DetectorInput):
        matches = []
        for number, line in enumerate(detector_input.content.splitlines(), 1):
            for pattern, message in self._patterns:
                for found in pattern.finditer(line):
                    matches.append(_line_match(number, line, found.start(), message, line))
        return tuple(matches)


def builtin_detectors():
    return (
        ConfigurationDetector(),
        DependencyDetector(),
        JavaScriptDetector(),
        PythonDetector(),
        SecretDetector(),
    )


def builtin_detector_registry() -> DetectorRegistry:
    return DetectorRegistry(builtin_detectors())


def _placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(
        token in lowered
        for token in (
            "${",
            "{{",
            "example",
            "changeme",
            "placeholder",
            "process.env",
            "os.environ",
            "getenv",
        )
    )


def _requirements_matches(content: str):
    matches = []
    for number, line in enumerate(content.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith(("#", "-")):
            continue
        if re.fullmatch(r"[A-Za-z0-9_.-]+==[^*\s]+", value) is None:
            matches.append(
                _line_match(number, line, 0, "Dependency is not pinned to an exact version", line)
            )
    return tuple(matches)


def _package_json_matches(content: str):
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ()
    matches = []
    lines = content.splitlines()
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        values = payload.get(section, {}) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            continue
        for name, version in sorted(values.items()):
            if isinstance(version, str) and _exact_version(version):
                continue
            number, line = _find_line(lines, f'"{name}"')
            matches.append(
                _line_match(
                    number,
                    line,
                    max(0, line.find(f'"{name}"')),
                    "Dependency is not pinned to an exact version",
                    line,
                )
            )
    return tuple(matches)


def _pyproject_matches(content: str):
    try:
        payload = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return ()
    dependencies = payload.get("project", {}).get("dependencies", [])
    if not isinstance(dependencies, list):
        return ()
    lines = content.splitlines()
    matches = []
    for value in dependencies:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]+==[^*\s]+", value):
            continue
        number, line = _find_line(lines, value)
        matches.append(
            _line_match(
                number,
                line,
                max(0, line.find(value)),
                "Dependency is not pinned to an exact version",
                line,
            )
        )
    return tuple(matches)


def _exact_version(value: str) -> bool:
    return re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value.strip()) is not None


def _find_line(lines: list[str], token: str):
    for number, line in enumerate(lines, 1):
        if token in line:
            return number, line
    return 1, token


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _keyword_true(node: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _safe_yaml_loader(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "Loader" and _call_name(keyword.value).endswith("SafeLoader"):
            return True
    return False


__all__ = [
    "BUILTIN_DETECTOR_SET_VERSION",
    "ConfigurationDetector",
    "DependencyDetector",
    "JavaScriptDetector",
    "PythonDetector",
    "SecretDetector",
    "builtin_detector_registry",
    "builtin_detectors",
]
