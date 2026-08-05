from __future__ import annotations

import pytest

from riftx.code.symbols import extract_symbols, language_for_path


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
