# RiftX 1.0 release process

RiftX releases are built by `.github/workflows/release.yml` from an existing tag. The workflow accepts `v$(cat VERSION)` or a numbered `v$(cat VERSION)-rc.N` tag, resolves it to one immutable commit, and checks out that commit in every platform job. Only the exact final tag can pass the separate publication workflow.

## Repository setup

Create a protected GitHub Environment named `riftx-release` with required reviewers. Store signing material only in that environment:

- macOS: `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`.
- Windows: `WINDOWS_CERTIFICATE_BASE64`, `WINDOWS_CERTIFICATE_PASSWORD`.

The release workflow is `workflow_dispatch` only. Ordinary pull requests and push CI cannot read these secrets. Rotate signing secrets immediately if a release job exposes unexpected output; secret values must never be echoed.

## Release sequence

1. Complete required CI and protected Responses/Chat live smoke checks on the intended commit.
2. Fill the matching `CHANGELOG.md` release section; placeholder notes make the workflow fail.
3. Set `VERSION` and all synchronized product versions. Create a protected `v<version>-rc.N` tag for RC evidence; after M8 passes, the exact `v<version>` final tag may point to the same immutable commit.
4. Dispatch **RiftX Release** for that tag. The workflow always creates a draft and cannot publish directly.
5. Review per-platform signature checks, payload scans, checksums, CycloneDX SBOM, and the source commit in `RELEASE-MANIFEST.json`.
6. Verify macOS and Windows installers on clean systems and the Linux tarball on clean Ubuntu 22.04 and 24.04.
7. Complete `docs/release/acceptance-template.json`, validate it locally, then upload the protected record to the draft release as `riftx-<version>-acceptance.json`.
8. Dispatch **RiftX Publish Accepted Release** for the same tag. The protected workflow revalidates all M8 evidence against the tag commit, deletes the raw record, attaches only its SHA-256-bound sanitized summary, and then removes draft status.

The workflow does not configure automatic updates and does not enable product telemetry. Linux remains a tarball release; no deb/rpm repository is promised for 1.0.

Before testing an upgrade from v0.8 data, read [the upgrade and rollback procedure](upgrade.md). The
1.0 binary performs forward-only migrations and refuses to write a State DB whose schema is newer
than it supports.
