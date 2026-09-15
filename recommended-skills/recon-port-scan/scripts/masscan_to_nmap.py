#!/usr/bin/env python3
"""
Convert fast port-scan results (nmap output) to port lists or nmap commands.

The dedicated high-rate scanners are not in the mirror; the local workflow is:
  sudo nmap -T5 --min-rate=2000 -p- -oG fast.gnmap TARGET
  python3 <this-script> fast.gnmap --nmap-cmd            # service-scan commands
  python3 <this-script> fast.gnmap -o ports.txt          # comma port list

Accepts nmap greppable (-oG), normal (-oN) and XML (-oX) output.

Usage:
    python3 <this-script> <nmap_output_file>
    python3 <this-script> scan.gnmap -o ports.txt
    python3 <this-script> scan.gnmap --nmap-cmd -t 192.168.1.100

Output options:
- Plain text: list of open ports (for nmap -p)
- By host: "IP: port,port" lines
- Nmap commands: nmap -sV -sC -p <ports> for each discovered host
"""

import re
import sys
import argparse
from typing import List, Dict, Any


def parse_nmap_output(path: str) -> List[Dict[str, Any]]:
    """Parse nmap -oG / -oN / -oX output into [{ip, port, proto}] entries."""
    try:
        text = open(path, "r", errors="ignore").read()
    except FileNotFoundError:
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)

    results: List[Dict[str, Any]] = []
    if "<nmaprun" in text:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            print(f"Error: invalid XML: {e}", file=sys.stderr)
            sys.exit(1)
        for host in root.iter("host"):
            addr = host.find("address")
            ip = addr.get("addr") if addr is not None else ""
            for p in host.iter("port"):
                st = p.find("state")
                if st is not None and st.get("state") == "open":
                    results.append({"ip": ip, "port": int(p.get("portid", 0)),
                                    "proto": p.get("protocol", "tcp")})
    elif text.startswith("# Nmap") or "\nHost: " in text:
        # Greppable (-oG): Host: IP (name) Ports: 22/open/tcp//ssh//..., ...
        for m in re.finditer(r"^Host:\s+(\S+)", text, re.M):
            ip = m.group(1)
            seg = text[m.end():text.find("\n", m.end())]
            for pm in re.finditer(r"(\d+)/open/(\w+)", seg):
                results.append({"ip": ip, "port": int(pm.group(1)),
                                "proto": pm.group(2)})
    else:
        # Normal (-oN): "Nmap scan report for IP" + "PORT STATE SERVICE" table
        current = None
        for line in text.splitlines():
            hm = re.match(r"Nmap scan report for (\S+)", line)
            if hm:
                current = hm.group(1)
                continue
            pm = re.match(r"(\d+)/(tcp|udp)\s+open\s", line)
            if pm and current:
                results.append({"ip": current, "port": int(pm.group(1)),
                                "proto": pm.group(2)})
    return results


def format_ports(results: List[Dict[str, Any]], target_ip: str = None) -> str:
    """Comma-separated list of open ports (optionally filtered by IP)."""
    if target_ip:
        results = [r for r in results if r["ip"] == target_ip]
    return ",".join(map(str, sorted(set(r["port"] for r in results))))


def format_by_host(results: List[Dict[str, Any]]) -> str:
    """Group open ports by host IP."""
    hosts: Dict[str, List[int]] = {}
    for r in results:
        hosts.setdefault(r["ip"], []).append(r["port"])
    return "\n".join(f"{ip}: {','.join(map(str, sorted(set(ps))))}"
                     for ip, ps in sorted(hosts.items()))


def generate_nmap_commands(results: List[Dict[str, Any]], extra_args: str = "") -> List[str]:
    """Generate one nmap service/version scan command per discovered host."""
    hosts: Dict[str, List[int]] = {}
    for r in results:
        hosts.setdefault(r["ip"], []).append(r["port"])
    cmds = []
    for ip, ports in hosts.items():
        ports_str = ",".join(map(str, sorted(set(ports))))
        cmds.append(f"nmap -sV -sC -p {ports_str} {extra_args} {ip}".strip())
    return cmds


def main():
    parser = argparse.ArgumentParser(
        description="Convert fast port-scan results (nmap -oG/-oN/-oX) "
                    "to port lists or nmap service-scan commands")
    parser.add_argument("json_file", metavar="scan_file",
                        help="nmap output file (-oG, -oN or -oX)")
    parser.add_argument("-o", "--output", help="Output file")
    parser.add_argument("-t", "--target", help="Filter by target IP")
    parser.add_argument("--nmap-cmd", action="store_true",
                        help="Generate nmap commands instead of port list")
    parser.add_argument("--by-host", action="store_true",
                        help="Group output by host IP")
    parser.add_argument("--nmap-args", default="",
                        help="Additional arguments for generated nmap commands")
    args = parser.parse_args()

    results = parse_nmap_output(args.json_file)
    if not results:
        print("No open ports found in scan output.", file=sys.stderr)
        sys.exit(0)

    if args.nmap_cmd:
        output = "\n".join(generate_nmap_commands(results, args.nmap_args))
    elif args.by_host:
        output = format_by_host(results)
    else:
        output = format_ports(results, args.target)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output + "\n")
        print(f"Output written to: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
