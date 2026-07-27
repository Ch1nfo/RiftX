# RiftX Report: Authorized lab

- Schema: `riftx.report/v1`
- Generated at: `80`
- Engagement: `eng-1`
- Objective: Validate an authorized attack path
- Mode: `Auto`
- Environment: `Lab`
- Authorization expires: `2000000000`
- Policy revision: `revision-1`
- Status: `Active`
- LLM Profile: `default`
- LLM Protocol: `ChatCompletions`

## Success Criteria

- Preserve reproducible evidence
- `artifact-evidence`: Preserve one artifact-backed evidence item

## Operator-declared Authorized Scope

This scope is an application-level operator declaration. Local shell execution is not an OS-enforced network isolation boundary.

- CIDRs: 10.10.20.0/24
- Domains: none
- Ports: 445
- Capabilities: attack_path.analysis

## Assets and Services

No assets recorded.

## Asset Relationships

No asset relationships recorded.

## Identities

No identities recorded.

## Observations

- [local:nuclei / potentialFinding] Potential issue requires validation token: [REDACTED] on `dc-1` (confidence 7000/10000)

## Hypotheses and Test Cases

- `Proposed` Credential reuse may reach domain control (confidence 6000/10000)
  - Test `test-1` using `credentialValidation` against `dc-1`

## Executions

- `execution-1` via `native-tool`: `Completed`

## Findings

### Validated credential reuse (High)

- Finding ID: `finding-1`
- Evidence: `evidence-1`

The authorized test reproduced credential reuse


## Attack Paths

- `domainController` -> `domainAdminEquivalent` (confidence 9000/10000, reproducible=true)
  - `identity-1` --credentialReuse--> `dc-1` (evidence=`evidence-1`)

## Coverage

- `authorizedAssets`: 3/4 (evidence=`evidence-1`)

## Evidence

- `evidence-1`: Tool output reproduced the authorized finding (finding=finding-1, execution=execution-1, artifact=id=`artifact-1`, sha256=`dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd`, bytes=42, captured=55, reproducible=true)

## Approvals

- `approval-1`: `Command` -> `Cancelled` (actor=System, requested=30, decided=31, policy=`revision-1`, binding=`binding-sha256`)

## Auto Run

- State: `Succeeded`
- Stop reason: `SuccessCriteriaMet`
- Turns: 3/20
- Tool calls: 100/100
- Wall-clock budget: 3600 seconds
- Single-command budget: 300 seconds
- Goal assessment: succeeded=true, evaluated=70, evidence=`evidence-1`
  - Criterion `artifact-evidence`: satisfied=true, evidence=`evidence-1`

## Known Limitations

- RiftX executes tools on the local machine; review local tool and Artifact handling accordingly.
- The declared target Scope is checked by RiftX policy but is not an OS-enforced network isolation boundary.
- User-provided tools may create Artifacts containing sensitive data; review them before export.

## Tool Snapshot

- Inventory SHA-256: `tool-inventory-sha256`
- `nmap`: `nmap-sha256` (metadataSchema=1, managed=true, shadowed=false)

## Skill Snapshot

- Catalog SHA-256: `skill-catalog-sha256`
- `authorized-recon`: `skill-sha256` (User, enabled=true)

## Artifacts

- `artifact-1`: path=`artifacts/reproduction.txt`, sha256=`dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd` (42 bytes)

## Artifact Hash Manifest

dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd  artifacts/reproduction.txt
