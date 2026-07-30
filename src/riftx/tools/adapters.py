"""Machine-readable output adapters for common security tools."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any


class ToolOutputParseError(ValueError):
    """Raised when a configured machine output cannot be parsed."""


def parse_tool_output(format_name: str, content: bytes) -> dict[str, object]:
    """Parse one supported machine format into a stable RiftX structure."""

    normalized = format_name.strip().lower()
    parsers: dict[str, Callable[[bytes], dict[str, object]]] = {
        "nmap_xml": parse_nmap_xml,
        "xml": parse_nmap_xml,
        "nuclei_jsonl": parse_nuclei_jsonl,
        "jsonl": parse_nuclei_jsonl,
        "masscan_json": parse_masscan_json,
        "json": parse_masscan_json,
    }
    try:
        parser = parsers[normalized]
    except KeyError as exc:
        raise ToolOutputParseError(f"unsupported tool output format {format_name!r}") from exc
    try:
        return parser(content)
    except ToolOutputParseError:
        raise
    except Exception as exc:
        raise ToolOutputParseError(f"unable to parse {format_name!r} tool output: {exc}") from exc


def parse_nmap_xml(content: bytes) -> dict[str, object]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ToolOutputParseError(f"invalid nmap XML: {exc}") from exc
    if root.tag != "nmaprun":
        raise ToolOutputParseError(f"unexpected nmap XML root {root.tag!r}")

    hosts: list[dict[str, object]] = []
    for host in root.findall("host"):
        status = host.find("status")
        addresses = [
            {
                "address": item.attrib.get("addr", ""),
                "type": item.attrib.get("addrtype", ""),
            }
            for item in host.findall("address")
            if item.attrib.get("addr")
        ]
        hostnames = [
            item.attrib["name"]
            for item in host.findall("./hostnames/hostname")
            if item.attrib.get("name")
        ]
        ports: list[dict[str, object]] = []
        for port in host.findall("./ports/port"):
            state = port.find("state")
            service = port.find("service")
            ports.append(
                {
                    "protocol": port.attrib.get("protocol", ""),
                    "port": int(port.attrib.get("portid", "0")),
                    "state": (
                        state.attrib.get("state", "unknown") if state is not None else "unknown"
                    ),
                    "service": service.attrib.get("name") if service is not None else None,
                    "product": service.attrib.get("product") if service is not None else None,
                    "version": service.attrib.get("version") if service is not None else None,
                }
            )
        hosts.append(
            {
                "status": (
                    status.attrib.get("state", "unknown") if status is not None else "unknown"
                ),
                "addresses": addresses,
                "hostnames": hostnames,
                "ports": ports,
            }
        )
    return {
        "adapter": "nmap_xml",
        "hosts": hosts,
        "host_count": len(hosts),
        "open_port_count": sum(
            1
            for host in hosts
            for port in host["ports"]
            if port["state"] == "open"  # type: ignore[index]
        ),
    }


def parse_nuclei_jsonl(content: bytes) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(content.decode("utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ToolOutputParseError(
                f"invalid nuclei JSONL at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(item, dict):
            raise ToolOutputParseError(
                f"invalid nuclei JSONL at line {line_number}: object expected"
            )
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        classification = (
            info.get("classification") if isinstance(info.get("classification"), dict) else {}
        )
        findings.append(
            {
                "template_id": item.get("template-id") or item.get("template_id"),
                "name": info.get("name"),
                "severity": info.get("severity"),
                "matched_at": item.get("matched-at") or item.get("matched_at"),
                "host": item.get("host"),
                "type": item.get("type"),
                "matcher_name": item.get("matcher-name") or item.get("matcher_name"),
                "cve_ids": classification.get("cve-id", []),
            }
        )
    return {
        "adapter": "nuclei_jsonl",
        "findings": findings,
        "finding_count": len(findings),
    }


def parse_masscan_json(content: bytes) -> dict[str, object]:
    text = content.decode("utf-8").strip()
    text = re.sub(r",\s*]$", "]", text)
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolOutputParseError(f"invalid masscan JSON: {exc.msg}") from exc
    if not isinstance(payload, list):
        raise ToolOutputParseError("invalid masscan JSON: top-level array expected")

    hosts: list[dict[str, object]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ToolOutputParseError(f"invalid masscan JSON item {index}: object expected")
        ports = item.get("ports", [])
        if not isinstance(ports, list):
            raise ToolOutputParseError(f"invalid masscan JSON item {index}: ports array expected")
        hosts.append(
            {
                "ip": item.get("ip"),
                "timestamp": item.get("timestamp"),
                "ports": [
                    {
                        "port": port.get("port"),
                        "protocol": port.get("proto"),
                        "status": port.get("status"),
                        "reason": port.get("reason"),
                        "ttl": port.get("ttl"),
                    }
                    for port in ports
                    if isinstance(port, dict)
                ],
            }
        )
    return {
        "adapter": "masscan_json",
        "hosts": hosts,
        "host_count": len(hosts),
        "open_port_count": sum(len(host["ports"]) for host in hosts),  # type: ignore[arg-type]
    }
