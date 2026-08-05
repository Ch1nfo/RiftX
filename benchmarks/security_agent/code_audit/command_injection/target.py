"""Intentionally vulnerable static fixture. Never import or execute this file."""

import subprocess


def lookup_host(user_supplied_host: str) -> str:
    completed = subprocess.run(
        f"nslookup {user_supplied_host}",
        capture_output=True,
        check=False,
        shell=True,
        text=True,
    )
    return completed.stdout
