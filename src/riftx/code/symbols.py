"""Bounded, non-executing symbol extraction used when no trusted LSP is active."""

# Regular-expression definitions are kept on one line so each supported declaration
# remains auditable as a single pattern.
# ruff: noqa: E501

from __future__ import annotations

import ast
import io
import re
import tokenize
from bisect import bisect_right
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


@dataclass(frozen=True, slots=True)
class StaticCall:
    caller: str | None
    callee: str
    line_number: int
    column: int
    confidence: Literal["python_ast", "lexical"]


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
_CALL_PATTERN = re.compile(rf"(?P<name>{_IDENTIFIER})\s*\(")

_PATTERNS: dict[str, tuple[_Pattern, ...]] = {
    "javascript": (
        _Pattern("class", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?class\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("function", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("method", re.compile(rf"^\s*(?:(?:async|static|get|set)\s+)*(?P<name>{_IDENTIFIER})\s*\([^;{{}}]*\)\s*(?:\{{|=>)")),
    ),
    "typescript": (
        _Pattern("class", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("interface", re.compile(rf"^\s*(?:export\s+)?interface\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("enum", re.compile(rf"^\s*(?:export\s+)?(?:const\s+)?enum\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("type", re.compile(rf"^\s*(?:export\s+)?type\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("function", re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("constant", re.compile(rf"^\s*(?:export\s+)?const\s+(?P<name>{_IDENTIFIER})\b")),
        _Pattern("method", re.compile(rf"^\s*(?:(?:public|protected|private|abstract|async|static|override|readonly|get|set)\s+)*(?P<name>{_IDENTIFIER})\s*\([^;{{}}]*\)\s*(?::\s*[^={{;]+)?(?:\{{|=>)")),
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
_NON_CALL_NAMES = _NON_SYMBOL_NAMES | frozenset({"def", "fn", "func", "function"})
_NON_DECLARATION_PREFIXES = ("return ", "throw ", "yield ", "await ")
_SLASH_COMMENT_LANGUAGES = frozenset(
    {
        "c",
        "cpp",
        "csharp",
        "go",
        "java",
        "javascript",
        "kotlin",
        "php",
        "rust",
        "swift",
        "typescript",
    }
)
_HASH_COMMENT_LANGUAGES = frozenset({"php", "ruby", "shell"})
_BACKTICK_STRING_LANGUAGES = frozenset(
    {"go", "javascript", "php", "ruby", "shell", "typescript"}
)


def language_for_path(path: str) -> str | None:
    return _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())


def is_identifier(value: str) -> bool:
    return bool(value) and (
        value.isidentifier() or re.fullmatch(_IDENTIFIER, value) is not None
    )


def find_identifier_occurrences(
    path: str,
    text: str,
    *,
    identifier: str,
    max_occurrences: int,
) -> tuple[list[tuple[int, int]], bool, bool]:
    """Return exact identifier positions, truncation, and lexical-error state."""

    language = language_for_path(path)
    if language is None:
        return [], False, False
    if language == "python":
        return _python_identifier_occurrences(
            text,
            identifier=identifier,
            max_occurrences=max_occurrences,
        )
    return _lexical_identifier_occurrences(
        text,
        language=language,
        identifier=identifier,
        max_occurrences=max_occurrences,
    )


def extract_call_graph(
    path: str,
    text: str,
    *,
    max_symbols: int,
    max_calls: int,
) -> tuple[
    list[CodeSymbol],
    list[StaticCall],
    bool,
    bool,
    Literal["python_ast", "lexical"],
]:
    """Return declarations and bounded name-level call edges for one source file."""

    symbols, symbol_truncated, symbol_parse_error = extract_symbols(
        path,
        text,
        max_symbols=max_symbols,
    )
    language = language_for_path(path)
    if language == "python":
        calls, call_truncated, call_parse_error = _python_calls(
            text,
            max_calls=max_calls,
        )
        return (
            symbols,
            calls,
            symbol_truncated or call_truncated,
            symbol_parse_error or call_parse_error,
            "python_ast",
        )
    calls, call_truncated, call_parse_error = _lexical_calls(
        text,
        language=language or "",
        symbols=symbols,
        max_calls=max_calls,
    )
    return (
        symbols,
        calls,
        symbol_truncated or call_truncated,
        symbol_parse_error or call_parse_error,
        "lexical",
    )


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


def _python_identifier_occurrences(
    text: str,
    *,
    identifier: str,
    max_occurrences: int,
) -> tuple[list[tuple[int, int]], bool, bool]:
    positions: list[tuple[int, int]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.NAME or token.string != identifier:
                continue
            positions.append(token.start)
            if len(positions) >= max_occurrences:
                return positions, True, False
    except (IndentationError, SyntaxError, tokenize.TokenError):
        return positions, False, True
    return positions, False, False


def _lexical_identifier_occurrences(
    text: str,
    *,
    language: str,
    identifier: str,
    max_occurrences: int,
) -> tuple[list[tuple[int, int]], bool, bool]:
    scrubbed, parse_error = _scrub_non_code(text, language=language)
    line_starts = _line_starts(scrubbed)
    positions: list[tuple[int, int]] = []
    expression = re.compile(
        rf"(?<![\w$]){re.escape(identifier)}(?![\w$])",
        re.UNICODE,
    )
    for match in expression.finditer(scrubbed):
        positions.append(_line_column(line_starts, match.start()))
        if len(positions) >= max_occurrences:
            return positions, True, parse_error
    return positions, False, parse_error


def _scrub_non_code(text: str, *, language: str) -> tuple[str, bool]:
    characters = list(text)
    length = len(text)
    index = 0

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if characters[position] != "\n":
                characters[position] = " "

    while index < length:
        if language in _SLASH_COMMENT_LANGUAGES and text.startswith("//", index):
            end = text.find("\n", index + 2)
            end = length if end < 0 else end
            blank(index, end)
            index = end
            continue
        if language in _SLASH_COMMENT_LANGUAGES and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                blank(index, length)
                return "".join(characters), True
            end += 2
            blank(index, end)
            index = end
            continue
        if language in _HASH_COMMENT_LANGUAGES and text[index] == "#":
            end = text.find("\n", index + 1)
            end = length if end < 0 else end
            blank(index, end)
            index = end
            continue

        quote = text[index]
        rust_lifetime = (
            language == "rust"
            and quote == "'"
            and index + 1 < length
            and (text[index + 1].isalpha() or text[index + 1] == "_")
            and (index + 2 >= length or text[index + 2] != "'")
        )
        if rust_lifetime:
            index += 1
            continue
        if quote not in {"'", '"'} and not (
            quote == "`" and language in _BACKTICK_STRING_LANGUAGES
        ):
            index += 1
            continue

        start = index
        index += 1
        closed = False
        while index < length:
            current = text[index]
            index += 1
            if (
                current == "\\"
                and index < length
                and not (quote == "`" and language == "go")
            ):
                index += 1
            elif current == quote:
                closed = True
                break
        blank(start, index)
        if not closed:
            return "".join(characters), True
    return "".join(characters), False


def _line_starts(text: str) -> list[int]:
    return [0, *(index + 1 for index, character in enumerate(text) if character == "\n")]


def _line_column(line_starts: list[int], index: int) -> tuple[int, int]:
    line_index = bisect_right(line_starts, index) - 1
    return line_index + 1, index - line_starts[line_index]


def _absolute_index(line_starts: list[int], line_number: int, column: int) -> int:
    return line_starts[line_number - 1] + column


def _lexical_calls(
    text: str,
    *,
    language: str,
    symbols: list[CodeSymbol],
    max_calls: int,
) -> tuple[list[StaticCall], bool, bool]:
    scrubbed, parse_error = _scrub_non_code(text, language=language)
    line_starts = _line_starts(scrubbed)
    declaration_positions = {
        _absolute_index(line_starts, symbol.line_number, symbol.column)
        for symbol in symbols
    }
    callable_definitions = sorted(
        (
            _absolute_index(line_starts, symbol.line_number, symbol.column),
            symbol.qualified_name,
        )
        for symbol in symbols
        if symbol.kind in {"function", "method"}
    )
    calls: list[StaticCall] = []
    caller: str | None = None
    definition_index = 0
    for match in _CALL_PATTERN.finditer(scrubbed):
        position = match.start("name")
        while (
            definition_index < len(callable_definitions)
            and callable_definitions[definition_index][0] <= position
        ):
            caller = callable_definitions[definition_index][1]
            definition_index += 1
        name = match.group("name")
        if position in declaration_positions or name in _NON_CALL_NAMES:
            continue
        line_number, column = _line_column(line_starts, position)
        calls.append(
            StaticCall(
                caller=caller,
                callee=name,
                line_number=line_number,
                column=column,
                confidence="lexical",
            )
        )
        if len(calls) >= max_calls:
            return calls, True, parse_error
    return calls, False, parse_error


def _python_calls(
    text: str,
    *,
    max_calls: int,
) -> tuple[list[StaticCall], bool, bool]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], False, True
    visitor = _PythonCallVisitor(text=text, max_calls=max_calls)
    try:
        visitor.visit(tree)
    except RecursionError:
        return [], False, True
    return visitor.calls, visitor.truncated, False


class _PythonCallVisitor(ast.NodeVisitor):
    def __init__(self, *, text: str, max_calls: int) -> None:
        self._lines = text.splitlines()
        self._max_calls = max_calls
        self._containers: list[str] = []
        self._callers: list[str] = []
        self.calls: list[StaticCall] = []
        self.truncated = False

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        self._containers.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._containers.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        qualified = _bounded_text(
            ".".join([*self._containers, node.name]),
            _MAX_QUALIFIED_NAME_CHARS,
        )
        self._containers.append(node.name)
        self._callers.append(qualified)
        for statement in node.body:
            self.visit(statement)
        self._callers.pop()
        self._containers.pop()

    def visit_Call(self, node: ast.Call) -> None:
        target = _python_call_target(node.func, self._lines)
        if target is not None:
            if len(self.calls) >= self._max_calls:
                self.truncated = True
                return
            callee, line_number, column = target
            self.calls.append(
                StaticCall(
                    caller=self._callers[-1] if self._callers else None,
                    callee=_bounded_text(callee, _MAX_QUALIFIED_NAME_CHARS),
                    line_number=line_number,
                    column=column,
                    confidence="python_ast",
                )
            )
        self.generic_visit(node)


def _python_call_target(
    node: ast.expr,
    lines: list[str],
) -> tuple[str, int, int] | None:
    if isinstance(node, ast.Name):
        return node.id, node.lineno, node.col_offset
    if not isinstance(node, ast.Attribute):
        return None
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    parts.reverse()
    line_number = int(getattr(node, "end_lineno", node.lineno))
    column = max(0, int(getattr(node, "end_col_offset", node.col_offset)) - len(node.attr))
    if 0 < line_number <= len(lines):
        located = lines[line_number - 1].rfind(node.attr, 0, column + len(node.attr) + 1)
        if located >= 0:
            column = located
    return ".".join(parts), line_number, column


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
        if 0 < line_number <= len(self._lines):
            located = self._lines[line_number - 1].find(name, column)
            if located >= 0:
                column = located
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


__all__ = [
    "StaticCall",
    "extract_call_graph",
    "extract_symbols",
    "find_identifier_occurrences",
    "is_identifier",
    "language_for_path",
]
