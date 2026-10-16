# Boat worker packing benchmark

Measured on 2026-10-16 UTC with one disposable `default` Boat sandbox:
4 vCPU, advertised 8 GB RAM, 2 GB swap, native Claude Code 2.1.301,
tmux, and one detached Git worktree per agent. The checkout was `20aefb2`.

## Observed results

Ten native Claude processes registered with one-conv and remained resident.
The cumulative stages submitted no model prompts; each included ten seconds
of idle sampling. All stages completed and their owned agents were stopped.

| Registered resident agents | Lowest available memory, MiB | OOM kills |
| --- | --- | --- |
| 1 | 6746.0 | 0 |
| 2 | 6534.5 | 0 |
| 4 | 6054.9 | 0 |
| 8 | 5127.4 | 0 |
| 10 | 4681.8 | 0 |

The baseline available memory was 6993.7 MiB. Summed process RSS is also
recorded by the harness, but it counts shared pages more than once and is
not a measure of the pool's physical memory cost. The common readable
`user.slice` cgroup had a 7,789,871,104-byte memory limit; per-command scope
limits were not substituted for the box's shared limit.

A second pool of ten registered idle agents ran alongside ten concurrent
local workloads. Each workload performed 1,500,000 PBKDF2-SHA256 iterations,
slept two seconds, and ran one real repository unit test in its own worktree
with optimized Python. These jobs were launched directly by the test driver.

- All ten jobs passed and wrote independent completion receipts.
- Receipt intervals overlapped at a peak of ten jobs.
- The complete batch took 4.17 seconds; individual jobs took 3.00–3.45 seconds.
- Available memory stayed above 4286.1 MiB, with no OOM kills.
- All ten harness processes remained alive; cleanup left no benchmark tmux sessions.

This establishes idle residency and a small concurrent local-workload baseline.
It does not establish ten simultaneous model turns, large builds, prolonged
context growth, hub topic throughput, or mixed-harness capacity.

## Model readiness

The active Claude test registered successfully but its first model request
reported an individual spend limit. It completed no verified assistant response.
The account limit was not changed.

Native Codex was installed but `codex login status` reported not logged in.
Boat's integrated Codex prompt returned `409 provider_not_configured`.
The default Boat AI profile listed only `claude-subscription`; it listed no
OpenRouter key. Model-turn capacity therefore remains unverified until a
working subscription or API credential is connected.

## Cloud GitHub preparation

A narrow worker preparation helper retrieved the existing GitHub credential
from the cloud vault, authenticated `gh`, and cloned the selected public and
private repositories. A fresh worker process confirmed access to the private
repository and its push permission. The saved credential was mode `0600`, and
temporary bootstrap payloads were removed. No Mac keychain was required.

The repository allowlist does not narrow the existing token's GitHub authority.
This test verifies credentials and selected clones, not the full future worker
image, every skill/tool dependency, or a production credential rotation service.

## Cloud workspace validation

The mobile rollout used a small persistent controller and one default worker
box. Managed Claude and Codex requests shared that worker box, with separate
worktrees, the selected skills and cloud GitHub access. Claude authenticated but
reported its account spend limit; Codex was not logged in. No successful model
answer or active-model packing measurement is claimed for this rollout.

A WebKit phone viewport paired over public HTTPS, sent a human message through
an ordinary Mac node to Oh My Pi, and received its reply in the cloud inbox.
Worker preparation also ran with that Mac bridge disconnected. The phone
controls resumed the tracked worker box and stopped it again; the worker box
was left stopped pending provider access.

The controller recovered after a real stop and resume without a remote command
to start the app. Its paired browser session, message history, rate cards,
worker records, cloud credentials and numeric usage ledger survived. A real
native-history import stored 2,283 events without transcript text; all of their
subscription cash costs remained unknown. These are functional checks, not a
physical-iPhone test or a measure of concurrent model capacity.

## Reproduction

The Python helpers and their tests are committed in
[claudine at b44fc7e](https://github.com/ClementWalter/claudine/commit/b44fc7e):
`scripts/generated/boat_agent_bench.py` and
`scripts/generated/boat_worker_prepare.py`.
Their combined focused suite passed 75 tests; Ruff and the configured Trunk
checks passed. Use each helper's `--help`; both require explicit execution flags.

Boat metered 3603 running seconds at USD 0.03603 for this sandbox before
cleanup. This is machine usage, separate from model billing. Current published
machine rates are described in [Boat pricing](https://docs.boat.dev/pricing).

