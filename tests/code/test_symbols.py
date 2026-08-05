from __future__ import annotations

import pytest

from riftx.code.symbols import (
    extract_call_graph,
    extract_diagnostics,
    extract_symbols,
    find_identifier_occurrences,
    language_for_path,
)


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        (
            "app.py",
            (
                "class Service:\n"
                "    async def handle(self):\n"
                "        def nested():\n"
                "            pass\n"
            ),
            [
                ("Service", "Service", "class"),
                ("handle", "Service.handle", "method"),
                ("nested", "Service.handle.nested", "function"),
            ],
        ),
        (
            "api.ts",
            "export interface Request {}\nexport async function handleRequest() {}\n",
            [("Request", "Request", "interface"), ("handleRequest", "handleRequest", "function")],
        ),
        (
            "main.go",
            "func (s *Server) Serve() {}\nfunc helper() {}\ntype Config struct {}\n",
            [
                ("Serve", "Serve", "method"),
                ("helper", "helper", "function"),
                ("Config", "Config", "struct"),
            ],
        ),
        (
            "lib.rs",
            "pub struct Engine {}\npub trait Runner {}\npub async fn execute() {}\n",
            [
                ("Engine", "Engine", "struct"),
                ("Runner", "Runner", "trait"),
                ("execute", "execute", "function"),
            ],
        ),
        (
            "Main.java",
            (
                "public class Main {\n"
                "  public String execute(int value) { return \"\"; }\n"
                "  public String caller() {\n"
                "    return execute(1);\n"
                "  }\n"
                "}\n"
            ),
            [
                ("Main", "Main", "class"),
                ("execute", "execute", "method"),
                ("caller", "caller", "method"),
            ],
        ),
        (
            "main.c",
            "static int execute(const char *value) { return value != 0; }\n",
            [("execute", "execute", "function")],
        ),
    ],
)
def test_builtin_symbol_extraction_covers_common_languages(
    path: str,
    source: str,
    expected: list[tuple[str, str, str]],
) -> None:
    symbols, truncated, parse_error = extract_symbols(path, source, max_symbols=100)

    assert [(item.name, item.qualified_name, item.kind) for item in symbols] == expected
    assert truncated is False
    assert parse_error is False


def test_python_parse_error_and_unsupported_extension_are_explicit() -> None:
    symbols, truncated, parse_error = extract_symbols(
        "broken.py",
        "def broken(:\n",
        max_symbols=100,
    )

    assert symbols == []
    assert truncated is False
    assert parse_error is True
    assert language_for_path("README.md") is None

    long_name = "x" * 600
    symbols, _, parse_error = extract_symbols(
        "long.py",
        f"def {long_name}():\n    pass\n",
        max_symbols=100,
    )
    assert parse_error is False
    assert len(symbols[0].name) == 512
    assert symbols[0].name.endswith("...")


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        ("app.py", 'handle()\ntext = "handle"\n# handle\n', [(1, 0)]),
        (
            "api.ts",
            'handle(); // handle\nconst text = "handle";\nconst raw = `handle`;\n',
            [(1, 0)],
        ),
        ("main.go", "handle()\nvar raw = `handle`\n", [(1, 0)]),
        (
            "lib.rs",
            'fn f<\'a>(value: &\'a str) { handle(); let c = \'h\'; let s = "handle"; }\n',
            [(1, 27)],
        ),
    ],
)
def test_identifier_scanner_skips_comments_and_string_literals(
    path: str,
    source: str,
    expected: list[tuple[int, int]],
) -> None:
    positions, truncated, parse_error = find_identifier_occurrences(
        path,
        source,
        identifier="handle",
        max_occurrences=10,
    )

    assert positions == expected
    assert truncated is False
    assert parse_error is False


@pytest.mark.parametrize(
    "source",
    ['const value = "unterminated\nhandle();\n', "/* unterminated\nhandle();\n"],
)
def test_identifier_scanner_reports_incomplete_lexical_regions(source: str) -> None:
    positions, truncated, parse_error = find_identifier_occurrences(
        "api.ts",
        source,
        identifier="handle",
        max_occurrences=10,
    )

    assert positions == []
    assert truncated is False
    assert parse_error is True


def test_python_call_graph_tracks_qualified_callers_and_module_calls() -> None:
    source = (
        "def target():\n"
        "    pass\n"
        "\n"
        "class Service:\n"
        "    def helper(self):\n"
        "        return target()\n"
        "\n"
        "    def caller(self):\n"
        "        self.helper()\n"
        "        target()\n"
        "\n"
        "def configured(value=target()):\n"
        "    return target()\n"
        "\n"
        "target()\n"
    )

    symbols, calls, truncated, parse_error, mode = extract_call_graph(
        "app.py",
        source,
        max_symbols=100,
        max_calls=100,
    )

    assert [symbol.qualified_name for symbol in symbols] == [
        "target",
        "Service",
        "Service.helper",
        "Service.caller",
        "configured",
    ]
    assert [(call.caller, call.callee, call.confidence) for call in calls] == [
        ("Service.helper", "target", "python_ast"),
        ("Service.caller", "self.helper", "python_ast"),
        ("Service.caller", "target", "python_ast"),
        (None, "target", "python_ast"),
        ("configured", "target", "python_ast"),
        (None, "target", "python_ast"),
    ]
    assert mode == "python_ast"
    assert truncated is False
    assert parse_error is False


def test_lexical_call_graph_skips_declarations_comments_and_strings() -> None:
    source = (
        "class Service {\n"
        "  target() {}\n"
        "  caller() {\n"
        "    target();\n"
        "    helper();\n"
        '    const text = "ignored()"; // ignored()\n'
        "  }\n"
        "}\n"
    )

    symbols, calls, truncated, parse_error, mode = extract_call_graph(
        "api.ts",
        source,
        max_symbols=100,
        max_calls=100,
    )

    assert [symbol.name for symbol in symbols] == ["Service", "target", "caller", "text"]
    assert [(call.caller, call.callee, call.confidence) for call in calls] == [
        ("caller", "target", "lexical"),
        ("caller", "helper", "lexical"),
    ]
    assert mode == "lexical"
    assert truncated is False
    assert parse_error is False


def test_static_diagnostics_report_python_and_lexical_structure_errors() -> None:
    python_diagnostics, truncated, parse_error, mode = extract_diagnostics(
        "broken.py",
        "def broken(:\n",
        max_diagnostics=10,
    )
    lexical_diagnostics, lexical_truncated, lexical_error, lexical_mode = (
        extract_diagnostics(
            "broken.ts",
            "function broken( {\n",
            max_diagnostics=10,
        )
    )
    string_diagnostics, _, string_error, _ = extract_diagnostics(
        "string.ts",
        'const value = "unterminated\n',
        max_diagnostics=10,
    )

    assert [(item.code, item.severity, item.line_number) for item in python_diagnostics] == [
        ("python_syntax_error", "error", 1)
    ]
    assert mode == "python_ast"
    assert truncated is False
    assert parse_error is True
    assert {item.code for item in lexical_diagnostics} == {"unclosed_delimiter"}
    assert lexical_mode == "lexical"
    assert lexical_truncated is False
    assert lexical_error is True
    assert string_diagnostics[0].code == "unclosed_string"
    assert string_diagnostics[0].severity == "error"
    assert string_error is True
