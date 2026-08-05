from __future__ import annotations

import pytest

from riftx.code.symbols import (
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
