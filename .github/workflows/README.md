# GitHub Actions (maintainer reference)

Internal notes for repository automation under `.github/workflows/`. Not published on the docs site.

## Workflows

| Workflow | Purpose |
| -------- | ------- |
| [`ci.yml`](ci.yml) | PR/push quality gates and sharded pytest |
| [`ci-labels-windows.yml`](ci-labels-windows.yml) | Optional Windows CI (`ci:windows` label) |
| [`codeql.yml`](codeql.yml) | Full post-merge CodeQL and manual PR-profile benchmarks |
| [`greptile-pr-reminder.yml`](greptile-pr-reminder.yml) | Greptile review nudge on PR open |
| [`celebrate-merged-pr.yml`](celebrate-merged-pr.yml) | Post-merge celebration comment |
| [`good-first-issue-assign.yml`](good-first-issue-assign.yml) | Auto-assign good first issues |
| [`release-safety-scheduled.yml`](release-safety-scheduled.yml) | Hourly release safety regression check |
| [`release.yml`](release.yml) | Release builds and artifacts |
| [`installer-canary.yml`](installer-canary.yml) | Post-publish canaries for the public install paths (CDN, GitHub release resolution, platform installers) on Linux/macOS/Windows |
| [`telemetry-integrity.yml`](telemetry-integrity.yml) | Post-merge telemetry identity/login probes in fresh processes on Linux/macOS/Windows and a Linux container (see [`tests/analytics/INTEGRITY.md`](../../tests/analytics/INTEGRITY.md)) |

See [CI.md](../../CI.md) for local parity commands before push.

## Canary concurrency policy

`installer-canary.yml` serializes runs **per channel** — the pinned tag, or
`latest` for scheduled and empty-tag runs — with `cancel-in-progress: false`:
runs queue, a running canary or report is never interrupted, and a newer
same-channel arrival supersedes an older pending same-channel run. That
supersession is scope-equivalent (the newest completed cycle for a channel is
that channel's verdict), and different channels never block each other.

The `report` job keeps **one tracking issue per channel**
(`Installer canary failing (<channel>)`): same-channel cycles are serialized by
the group, cross-channel cycles touch disjoint issues, and a stale success can
therefore never close another channel's failure. Issues created before channel
scoping carry the bare `Installer canary failing` title and are adopted as the
`latest` channel by the next cycle.

## CodeQL ownership

The checked-in workflow owns CodeQL for this repository. Full Python and
JavaScript/TypeScript `security-and-quality` scans run on `main` and weekly. The
manual `pr-fast` profile uses default queries and shipped Python paths only;
select a larger runner by passing its label through `runner_label`.

Keep repository-level GitHub Code Quality disabled after this workflow lands.
Otherwise GitHub starts a second dynamic Python/JavaScript analysis on every PR
and push, restoring the four-minute critical path and duplicating scan cost.
