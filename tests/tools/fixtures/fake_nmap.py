"""Machine-output fixture used by structured port-scan tests."""

from __future__ import annotations

import sys

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
    else:
        print(NMAP_XML)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
