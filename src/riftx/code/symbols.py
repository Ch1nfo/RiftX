"""Bounded, non-executing symbol extraction used when no trusted LSP is active."""

# Regular-expression definitions are kept on one line so each supported declaration
# remains auditable as a single pattern.
# ruff: noqa: E501

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .models import CodeSymbol

_MAX_SIGNATURE_CHARS = 500
_MAX_LINE_CHARS = 4096
_MAX_LINES = 50_000
_MAX_NAME_CHARS = 512
_MAX_QUALIFIED_NAME_CHARS = 2048

type _SymbolKind = Literal[
    "function",
    "method",
    "class",
    "interface",
    "struct",
    "enum",
    "trait",
    "module",
    "namespace",
    "type",
    "constant",
    "variable",
]


@dataclass(frozen=True, slots=True)
class _Pattern:
    kind: _SymbolKind
    expression: re.Pattern[str]


_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".mjs": "javascript",
    ".php": "php",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}

_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_WORD_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"

_PATTERNS: dict[str, tuple[_Pattern, ...]] = {
    "javascript": (
        _Pattern("class", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("function", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\b")),
    ),
    "typescript": (
        _Pattern("class", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("interface", re.compile(rf"^\s*(?:export\s+)?interface\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("enum", re.compile(rf"^\s*(?:export\s+)?(?:const\s+)?enum\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("type", re.compile(rf"^\s*(?:export\s+)?type\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("function", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\b")),
    ),
    "go": (
        _Pattern("method", re.compile(rf"^\s*func\s*\([^)]*\)\s*(?P<name>{_WORD_IDENTIFIER})\s*\(")),
        _Pattern("function", re.compile(rf"^\s*func\s+(?P<name>{_WORD_IDENTIFIER})\s*\(")),
        _Pattern("struct", re.compile(rf"^\s*type\s+(?P<name>{_WORD_IDENTIFIER})\s+struct\b")),
        _Pattern("interface", re.compile(rf"^\s*type\s+(?P<name>{_WORD_IDENTIFIER})\s+interface\b")),
        _Pattern("type", re.compile(rf"^\s*type\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*const\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("variable", re.compile(rf"^\s*var\s+(?P<name>{_WORD_IDENTIFIER})\b")),
    ),
    "rust": (
        _Pattern("function", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("struct", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("enum", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("trait", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("module", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("type", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?type\s+(?P<name>{_WORD_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(?:mut\s+)?(?P<name>{_WORD_IDENTIFIER})\b")),
    ),
    "java": (),
    "kotlin": (),
    "csharp": (),
    "swift": (),
    "c": (),
    "cpp": (),
    "php": (),
    "ruby": (),
    "shell": (),
}

_CLASS_FAMILY = (
    _Pattern("class", re.compile(rf"^\s*(?:(?:public|protected|private|internal|open|abstract|final|sealed|static|data)\s+)*class\s+(?P<name>{_WORD_IDENTIFIER})\b")),
    _Pattern("interface", re.compile(rf"^\s*(?:(?:public|protected|private|internal|sealed)\s+)*interface\s+(?P<name>{_WORD_IDENTIFIER})\b")),
    _Pattern("enum", re.compile(rf"^\s*(?:(?:public|protected|private|internal)\s+)*enum(?:\s+class)?\s+(?P<name>{_WORD_IDENTIFIER})\b")),
    _Pattern("struct", re.compile(rf"^\s*(?:(?:public|protected|private|internal)\s+)*struct\s+(?P<name>{_WORD_IDENTIFIER})\b")),
)
for _language in ("java", "kotlin", "csharp", "swift", "c", "cpp"):
    _PATTERNS[_language] = _CLASS_FAMILY

_PATTERNS["java"] += (
    _Pattern("method", re.compile(rf"^\s*(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*(?:[A-Za-z_$][A-Za-z0-9_$.<>?,\[\]]*\s+)+(?P<name>{_IDENTIFIER})\s*\([^;{{}}]*\)\s*(?:throws\s+[^{{;]+\s*)?(?:\{{|;)")),
)
_PATTERNS["kotlin"] += (
    _Pattern("function", re.compile(rf"^\s*(?:(?:public|protected|private|internal|open|final|abstract|override|suspend|inline|tailrec|operator|infix|external)\s+)*fun\s+(?:<[^>]+>\s*)?(?P<name>{_WORD_IDENTIFIER})\s*\(")),
)
_PATTERNS["csharp"] += (
    _Pattern("method", re.compile(rf"^\s*(?:(?:public|protected|private|internal|static|virtual|abstract|sealed|override|async|extern|unsafe|new|partial)\s+)*(?:[A-Za-z_][A-Za-z0-9_.<>?,\[\]]*\s+)+(?P<name>{_WORD_IDENTIFIER})\s*\([^;{{}}]*\)\s*(?:where\s+[^{{;]+\s*)?(?:=>|\{{|;)")),
)
_PATTERNS["swift"] += (
    _Pattern("interface", re.compile(rf"^\s*(?:(?:public|private|fileprivate|internal|open)\s+)*protocol\s+(?P<name>{_WORD_IDENTIFIER})\b")),
    _Pattern("function", re.compile(rf"^\s*(?:(?:public|private|fileprivate|internal|open|static|class|mutating|nonmutating|override|final)\s+)*func\s+(?P<name>{_WORD_IDENTIFIER})\s*\(")),
)
_C_FUNCTION = _Pattern(
    "function",
    re.compile(
        rf"^\s*(?:(?:static|extern|inline|constexpr|consteval|virtual|friend|explicit|unsigned|signed|long|short|const|volatile)\s+)*(?:[A-Za-z_][A-Za-z0-9_:<>]*[\s*&]+)+(?P<name>{_WORD_IDENTIFIER})\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{{"
    ),
)
_PATTERNS["c"] += (_C_FUNCTION,)
_PATTERNS["cpp"] += (_C_FUNCTION,)

_PATTERNS["cpp"] += (
    _Pattern("namespace", re.compile(rf"^\s*namespace\s+(?P<name>{_WORD_IDENTIFIER})\b")),
)
_PATTERNS["php"] = (
    _Pattern("class", re.compile(rf"^\s*(?:abstract\s+|final\s+)?class\s+(?P<name>{_WORD_IDENTIFIER})\b", re.IGNORECASE)),
    _Pattern("interface", re.compile(rf"^\s*interface\s+(?P<name>{_WORD_IDENTIFIER})\b", re.IGNORECASE)),
    _Pattern("trait", re.compile(rf"^\s*trait\s+(?P<name>{_WORD_IDENTIFIER})\b", re.IGNORECASE)),
    _Pattern("function", re.compile(rf"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*function\s+&?\s*(?P<name>{_WORD_IDENTIFIER})\b", re.IGNORECASE)),
)
_PATTERNS["ruby"] = (
    _Pattern("class", re.compile(rf"^\s*class\s+(?P<name>{_WORD_IDENTIFIER}(?:::{_WORD_IDENTIFIER})*)\b")),
    _Pattern("module", re.compile(rf"^\s*module\s+(?P<name>{_WORD_IDENTIFIER}(?:::{_WORD_IDENTIFIER})*)\b")),
    _Pattern("function", re.compile(rf"^\s*def\s+(?:self\.)?(?P<name>{_WORD_IDENTIFIER}[!?=]?)\b")),
)
_PATTERNS["shell"] = (
    _Pattern("function", re.compile(rf"^\s*(?:function\s+)?(?P<name>{_WORD_IDENTIFIER})\s*\(\s*\)\s*\{{")),
)

_NON_SYMBOL_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof"})
_NON_DECLARATION_PREFIXES = ("return ", "throw ", "yield ", "await ")


def language_for_path(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def extract_symbols(
    path: str,
    text: str,
    *,
    max_symbols: int,
) -> tuple[list[CodeSymbol], bool, bool]:
    """Return symbols, truncation, and parse-error state."""

    language = language_for_path(path)
    if language is None:
        return [], False, False
    if language == "python":
        return _python_symbols(path, text, max_symbols=max_symbols)
    symbols: list[CodeSymbol] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if line_number > _MAX_LINES:
            return symbols, True, False
        line = raw_line[:_MAX_LINE_CHARS]
        if line.lstrip().startswith(_NON_DECLARATION_PREFIXES):
            continue
        for pattern in _PATTERNS[language]:
            match = pattern.expression.match(line)
            if match is None:
                continue
            name = match.group("name")
            if name in _NON_SYMBOL_NAMES:
                continue
            bounded_name = _bounded_text(name, _MAX_NAME_CHARS)
            symbols.append(
                CodeSymbol(
                    name=bounded_name,
                    qualified_name=bounded_name,
                    kind=pattern.kind,
                    language=language,
                    path=path,
                    line_number=line_number,
                    column=match.start("name"),
                    signature=line.strip()[:_MAX_SIGNATURE_CHARS],
                )
            )
            if len(symbols) >= max_symbols:
                return symbols, True, False
            break
    return symbols, False, False


def _python_symbols(
    path: str,
    text: str,
    *,
    max_symbols: int,
) -> tuple[list[CodeSymbol], bool, bool]:
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return [], False, True
    visitor = _PythonSymbolVisitor(path=path, text=text, max_symbols=max_symbols)
    try:
        visitor.visit(tree)
    except RecursionError:
        return [], False, True
    return visitor.symbols, visitor.truncated, False


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str, text: str, max_symbols: int) -> None:
        self._path = path
        self._lines = text.splitlines()
        self._max_symbols = max_symbols
        self._containers: list[tuple[str, _SymbolKind]] = []
        self.symbols: list[CodeSymbol] = []
        self.truncated = False

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not self._append(node, node.name, "class"):
            return
        self._containers.append((node.name, "class"))
        self.generic_visit(node)
        self._containers.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind: _SymbolKind = (
            "method" if self._containers and self._containers[-1][1] == "class" else "function"
        )
        if not self._append(node, node.name, kind):
            return
        self._containers.append((node.name, kind))
        self.generic_visit(node)
        self._containers.pop()

    def _append(self, node: ast.AST, name: str, kind: _SymbolKind) -> bool:
        if len(self.symbols) >= self._max_symbols:
            self.truncated = True
            return False
        line_number = int(getattr(node, "lineno", 1))
        column = int(getattr(node, "col_offset", 0))
        qualified = ".".join([*(container[0] for container in self._containers), name])
        signature = (
            self._lines[line_number - 1].strip()[:_MAX_SIGNATURE_CHARS]
            if 0 < line_number <= len(self._lines)
            else ""
        )
        self.symbols.append(
            CodeSymbol(
                name=_bounded_text(name, _MAX_NAME_CHARS),
                qualified_name=_bounded_text(qualified, _MAX_QUALIFIED_NAME_CHARS),
                kind=kind,
                language="python",
                path=self._path,
                line_number=line_number,
                column=column,
                signature=signature,
            )
        )
        return True


def _bounded_text(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else f"{value[: maximum - 3]}..."


__all__ = ["extract_symbols", "language_for_path"]
