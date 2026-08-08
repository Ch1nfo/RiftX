"""Machine-output fixture used by structured port-scan tests."""

from __future__ import annotations

import socket
import sys
from xml.sax.saxutils import escape

NMAP_XML = """<?xml version="1.0"?>
<nmaprun scanner="nmap">
  <host>
    <status state="up" />
    <address addr="192.0.2.10" addrtype="ipv4" />
    <ports>
      <port protocol="tcp" portid="80">
        <state state="open" />
        <service name="http" product="fixture" version="1.0" />
      </port>
    </ports>
  </host>
</nmaprun>
"""


def main() -> int:
    if "--version" in sys.argv:
        print("Nmap fixture 1.0")
    elif "--invalid" in sys.argv:
        print("not xml")
    elif "--fixture-probe" in sys.argv:
        print(_probe_xml(sys.argv[1:]))
    else:
        print(NMAP_XML)
    return 0


def _probe_xml(arguments: list[str]) -> str:
    try:
        port_index = arguments.index("-p")
        port = int(arguments[port_index + 1])
        target = next(
            item
            for item in reversed(arguments)
            if not item.startswith("-") and item != str(port)
        )
    except (StopIteration, ValueError, IndexError) as exc:
        raise SystemExit(f"fixture probe requires one target and numeric -p port: {exc}") from exc

    state = "closed"
    service = ""
    try:
        with socket.create_connection((target, port), timeout=1.0) as connection:
            connection.sendall(b"HEAD / HTTP/1.0\r\nHost: fixture\r\n\r\n")
            connection.recv(4096)
        state = "open"
        service = '<service name="http" product="RiftX Fixture" version="1.0" />'
    except OSError:
        pass

    return f'''<?xml version="1.0"?>
<nmaprun scanner="nmap">
  <host>
    <status state="up" />
    <address addr="{escape(target)}" addrtype="ipv4" />
    <ports>
      <port protocol="tcp" portid="{port}">
        <state state="{state}" />
        {service}
      </port>
    </ports>
  </host>
</nmaprun>'''


if __name__ == "__main__":
    raise SystemExit(main())
