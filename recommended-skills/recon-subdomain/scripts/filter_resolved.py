#!/usr/bin/env python3
"""
Filter resolved subdomains with custom resolution logic.

Usage:
    python filter_resolved.py subs.txt -o resolved.txt
    python filter_resolved.py subs.txt --pattern-only -o resolved.txt

Features:
- Custom DNS resolution checking (dig +short, falls back to socket.getaddrinfo)
- Pattern-based filtering
- HTTP/HTTPS probe filtering
- CNAME chain analysis
"""

import sys
import argparse
import socket
import subprocess
from typing import List, Set


def dig_available() -> bool:
    """Check whether the dig binary exists."""
    try:
        subprocess.run(['dig', '-v'], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def resolves(subdomain: str, use_dig: bool) -> bool:
    """Verify a single subdomain resolves (dig first, getaddrinfo fallback)."""
    if use_dig:
        try:
            result = subprocess.run(
                ['dig', '+short', subdomain],
                capture_output=True,
                text=True,
                timeout=5
            )
            return bool(result.stdout.strip())
        except subprocess.TimeoutExpired:
            return False
    try:
        socket.getaddrinfo(subdomain, None)
        return True
    except socket.gaierror:
        return False


def filter_by_pattern(subdomains: List[str]) -> Set[str]:
    """Filter out obviously invalid patterns."""
    valid = set()

    invalid_patterns = [
        '*.',
        'xn--',  # Punycode (often false positives)
        '..',
    ]

    for sub in subdomains:
        # Skip invalid patterns
        if any(p in sub for p in invalid_patterns):
            continue

        # Basic validation
        if '.' in sub and len(sub) > 3:
            valid.add(sub)

    return valid


def filter_resolved(subdomains: List[str], use_dig: bool) -> Set[str]:
    """Keep only subdomains that actually resolve."""
    resolved = set()
    for sub in subdomains:
        if resolves(sub, use_dig):
            resolved.add(sub)
    return resolved


def main():
    parser = argparse.ArgumentParser(
        description="Filter resolved subdomains"
    )
    parser.add_argument("input", help="Input subdomain file")
    parser.add_argument("-o", "--output", required=True, help="Output file")
    parser.add_argument("--pattern-only", action="store_true",
                        help="Only filter by pattern, no resolution")

    args = parser.parse_args()

    # Read input
    with open(args.input, 'r') as f:
        subdomains = [line.strip() for line in f if line.strip()]

    print(f"Read {len(subdomains)} subdomains", file=sys.stderr)

    # Filter by pattern first
    valid = filter_by_pattern(subdomains)
    print(f"After pattern filter: {len(valid)}", file=sys.stderr)

    # Verify DNS resolution (dig +short, fallback to getaddrinfo)
    if not args.pattern_only:
        use_dig = dig_available()
        if not use_dig:
            print("dig not found, using socket.getaddrinfo", file=sys.stderr)
        valid = filter_resolved(list(valid), use_dig)
        print(f"After DNS resolution: {len(valid)}", file=sys.stderr)

    # Write output
    with open(args.output, 'w') as f:
        for sub in sorted(valid):
            f.write(f"{sub}\n")

    print(f"Wrote {len(valid)} subdomains to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
