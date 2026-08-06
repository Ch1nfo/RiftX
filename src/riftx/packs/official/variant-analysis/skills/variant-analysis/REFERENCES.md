# Variant Analysis References

- Start from `finding-verification` output or an equivalently evidence-complete seed, never from severity or a vulnerability name alone.
- Root-cause invariants include attacker capability, source, path, security boundary, sink or decision, missing defense, preconditions, and impact.
- Search with syntax, symbols, references, callers, configuration, types, data or control flow, and defense structure, then verify independently.
- Seed Evidence can guide the search but cannot be reused as proof for another source location.
- Report searched, excluded, truncated, unsupported, negative, duplicate, unresolved, and verified surfaces explicitly.
