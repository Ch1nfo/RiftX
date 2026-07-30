from __future__ import annotations

import json
from pathlib import Path

import pytest

from riftx.tools import ToolOutputParseError, parse_generic_json, parse_tool_output

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


def test_generic_json_adapter_preserves_value_and_reports_shape() -> None:
    result = parse_generic_json(b'{"ports":[443,80],"target":"authorized.example"}')

    assert result == {
        "adapter": "generic_json",
        "value": {"ports": [443, 80], "target": "authorized.example"},
        "top_level_type": "object",
        "item_count": 2,
        "top_level_keys": ["ports", "target"],
    }


def test_generic_json_adapter_rejects_invalid_json() -> None:
    with pytest.raises(ToolOutputParseError, match="invalid generic JSON"):
        parse_generic_json(b"not json")
