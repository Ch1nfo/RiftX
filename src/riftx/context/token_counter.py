"""Deterministic, provider-neutral token estimates for Context observability."""

from __future__ import annotations

import json
from math import ceil


def estimate_context_tokens(value: object) -> int:
    """Estimate tokens from the canonical rendered payload without provider coupling."""

    if value is None:
        return 0
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    if not rendered:
        return 0
    return max(1, ceil(len(rendered) / 4))
