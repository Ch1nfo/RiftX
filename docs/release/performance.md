# RiftX 1.0 performance and resource acceptance

M8 performance evidence is a release gate, not an informal observation. Record the worst passing
value from the RC artifacts in the `performanceGate.metrics` object of the protected acceptance
record. Use release builds on otherwise idle, supported systems; identify hardware, OS, sample
command, raw result Artifact, tag, and commit in the linked evidence.

## Fixed thresholds

| Metric | RiftX 1.0 threshold | Measurement contract |
| --- | ---: | --- |
| `sampleSeconds` | at least 60 s | Measure after a 30 s startup/settling interval. |
| `desktopIdleCpuP95Percent` | at most 5% | Supported macOS and Windows Desktop, no active turn. Record the worse platform. |
| `daemonIdleCpuP95Percent` | at most 2% | Daemon with 16 configured, enabled Profiles and no active engagement. |
| `configuredProfileCount` | at least 16 | Use valid, secret-referenced Profiles without invoking them. |
| `eagerRuntimeCount` | exactly 0 | No Profile Runtime directory/process may exist before first use. |
| `timelineEntryCount` | at least 10,000 | Seed conversation/timeline history, then page through the public API/Desktop loader. |
| `timelinePageP95Ms` | at most 250 ms | At least 100 page requests on local IPC; exclude fixture creation time. |
| `killStartP95Ms` | at most 2,000 ms | Time from local Kill request receipt to the first process-tree termination attempt. |
| `duplicateEventCountAfterReconnect` | exactly 0 | Disconnect/reconnect the event consumer at least 20 times during a deterministic fixture. |
| `reportArtifactPayloadBytesRead` | exactly 0 | Generate JSON and Markdown reports with a large Artifact; report generation may read metadata but not Artifact payload bytes. |

CPU percentages are one logical core percentages, not percentages normalized across the whole
machine. P95 values must be computed from the raw samples and retained with the acceptance bundle.
A faster developer machine cannot waive the supported-OS run.

## Failure handling

A threshold miss is a release failure. Do not average macOS and Windows together to hide one
failure, discard retries, or replace RC binaries with locally rebuilt binaries. Link the issue and
rerun evidence for the same tag/commit after the fix; if the fix changes the commit, all evidence
must move to the new tag/commit.
