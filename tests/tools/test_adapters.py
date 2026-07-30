from __future__ import annotations

import json
from pathlib import Path

import pytest

from riftx.tools import ToolOutputParseError, parse_tool_output

GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize(
    ("format_name", "input_name", "expected_name"),
    [
        ("nmap_xml", "nmap.xml", "nmap.json"),
        ("nuclei_jsonl", "nuclei.jsonl", "nuclei.json"),
        ("masscan_json", "masscan.input.json", "masscan.json"),
    ],
)
def test_tool_adapter_matches_golden_file(
    format_name: str,
    input_name: str,
    expected_name: str,
) -> None:
    actual = parse_tool_output(format_name, (GOLDEN / input_name).read_bytes())
    expected = json.loads((GOLDEN / expected_name).read_text())
    assert actual == expected


def test_tool_adapter_rejects_invalid_machine_output() -> None:
    with pytest.raises(ToolOutputParseError, match="invalid nmap XML"):
        parse_tool_output("nmap_xml", b"not xml")
