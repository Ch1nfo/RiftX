---
name: Service Enumeration
description: Discover and characterize authorized network services with raw artifacts and explicit uncertainty
version: 1.1.0
source: official
required_capabilities:
  - port_scan
  - registered_tool_execution
  - artifact_read
  - evidence_ledger
preferred_tools:
  - port_scan
  - run_registered_tool
  - read_artifact
  - register_artifact_evidence
  - query_reasoning_graph
  - record_observation
  - propose_hypothesis
  - record_negative_result
approval_level: sensitive
---

## When to use

Use after an authorized target set is known and the objective requires identifying reachable network services, protocol behavior, or service versions.

## Preconditions

Each hostname, address, and port range is inside current Scope. Target interaction has passed the existing approval gate, rate and concurrency limits are known, and raw output can be retained as bounded artifacts.

## Procedure

1. Resolve the exact target identities and re-check Scope after DNS, redirect, proxy, or address changes.
2. Start with the smallest port and protocol set justified by the objective; avoid broad scans by default.
3. Use the native port scan path or an approved registered tool with structured `target`, `ports`, and `service_detection` inputs; never pass raw scanner argv for a Pentest target. Preserve command identity, parameters, timing, exit status, and raw artifact references.
4. Promote an endpoint only from observed reachability. Separate transport state, protocol, product, version, and deployment role by evidence strength.
5. Perform minimal protocol-aware checks only where they materially reduce ambiguity.
6. Record closed, filtered, timed-out, reset, unsupported, and ambiguous results so later tasks do not repeat identical probes.

## Decision points

- Do not scan a resolved or redirected host until its current identity is admitted by Scope.
- Do not infer a service from a default port alone.
- Treat banners, TLS metadata, and tool fingerprints as observations until corroborated when proxies or shared infrastructure are plausible.
- Increase scan breadth or intensity only through the existing approval and budget controls.

## Stop conditions

Stop an endpoint branch when its service state is sufficiently characterized for the objective, repeated probes add no evidence, the target becomes out of scope, approval expires, or rate and time budgets are reached.

## Expected output

A normalized endpoint inventory linked to raw artifacts, confidence-bounded service observations, and durable negative results for unavailable or ambiguous paths.

## Error handling

Record tool availability, parser, timeout, privilege, containment, and artifact failures. Do not silently fall back to an unregistered command or treat tool failure as a closed service.
