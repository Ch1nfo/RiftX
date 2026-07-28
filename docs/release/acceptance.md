# RiftX 1.0 release acceptance evidence

RiftX 1.0 must remain a draft release until the M8 evidence record passes
`scripts/riftx/verify-release-acceptance.py`. Repository implementation, a successful local build,
or a provider capability probe is not release evidence by itself.

Start from `docs/release/acceptance-template.json` and keep the completed record in the protected
release evidence store. Do not commit provider transcripts, API keys, raw audit logs, signing
certificates, or private target data. Evidence links must be durable HTTPS URLs or protected
`artifact://` references.

## Required record

Every evidence entry has these common fields:

- `status`: exactly `passed`;
- `tag` and `sourceCommit`: the same immutable release source as the record;
- `tester`: a named human or CI identity;
- `os`: the tested OS/runtime, or an explicit applicable-platform description;
- `checkedAt`: timezone-aware RFC 3339 timestamp;
- `evidence`: protected HTTPS or Artifact URI;
- `artifactSha256`: optional SHA-256 when the evidence is a downloaded artifact.

The verifier requires:

1. **44 automated cells** from the M8 matrix: mock plus macOS/Windows/Linux for every row, and a
   protected live lane for Responses/Chat text and tool loops.
2. **18 human cells**: scenarios A-F on macOS, Windows, and Linux.
3. **14 release gates**: protected environments; tag/commit identity; Apple signing,
   notarization, and staple; Windows Authenticode; clean installs; Ubuntu 22.04/24.04; upgrade and
   rollback; checksum/SBOM/manifest identity; live-secret scan; the fixed performance contract in `performance.md`; final notes; and a structured zero-P0/P1/no-flake defect gate in which every remaining P2 has a workaround, risk assessment, and 1.0.x milestone.
4. A separate Go/No-Go reviewer decision with outcome `go`.

Run:

```bash
conda run --no-capture-output -n agent \
  python3 scripts/riftx/verify-release-acceptance.py \
  /secure/path/riftx-1.0.0-acceptance.json \
  --output /secure/path/riftx-1.0.0-acceptance-summary.json
```

Only the sanitized summary and the acceptance-record SHA-256 should be attached to the release.
The draft may be published only after this command succeeds and reviewers confirm the referenced
artifacts still exist.

## Human scenarios

The scenario identifiers map directly to `1.0 计划.md`:

- `A`: first install and restart recovery;
- `B`: Pentest approval, denial, continuation, and report;
- `C`: RedTeam tool-risk approval and invalidation after modification;
- `D`: authorized Auto multi-asset success with evidence;
- `E`: no-progress, pause/resume, kill, and deadline protection;
- `F`: Desktop/daemon interruption, recovery, Profile isolation, and audit failure handling.

A screenshot alone is insufficient when the scenario also requires a report, event timeline,
signature, checksum, or process-tree result. Link those machine artifacts in the same protected
evidence bundle.
