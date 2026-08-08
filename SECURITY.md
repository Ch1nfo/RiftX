# Security Policy

RiftX controls security-testing workflows and may handle sensitive target metadata.
Please report suspected vulnerabilities privately.

## Supported version

RiftX is currently alpha software. Security fixes are applied to the latest code on
`main`; older commits, branches, and unpublished artifacts are not separately supported.

## Reporting a vulnerability

Email [ch1nfo@foxmail.com](mailto:ch1nfo@foxmail.com) with the subject
`[RiftX Security] <short summary>`.

Include:

- affected version or commit;
- impact and the violated security boundary;
- minimal reproduction steps using synthetic or project-owned test data;
- relevant logs with credentials, target details, and absolute private paths removed;
- any suggested mitigation, if known.

Do not open a public issue for an unpatched vulnerability. Do not include live
credentials, real captured traffic, third-party target data, or destructive proof of
concepts. You will receive an acknowledgement when the report has been reviewed; public
disclosure should be coordinated after a fix or mitigation is available.

## Scope

Reports should concern RiftX code or its documented deployment boundary. Vulnerabilities
in model providers, Temporal, browsers, operating systems, or third-party tools should be
reported to their respective maintainers unless RiftX integrates them unsafely.

Only test systems you own or are explicitly authorized to assess. Do not test GitHub,
package registries, contributor infrastructure, or unrelated public services as part of
a RiftX report.
