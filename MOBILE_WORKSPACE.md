# OneConv on your phone

The cloud workspace combines the mobile web app, live hub, human mailbox,
worker jobs and shared accounting. A laptop is an ordinary connected node.
The workspace continues to run when it disconnects.

Open a pairing link in Safari, choose **Connect this device**, then use Safari's
Share menu and **Add to Home Screen**. Links are single-use and expire after ten
minutes by default. The resulting session lasts up to thirty days. Disconnecting
the device revokes its session. Offline navigation shows a reconnect page;
the public app shell contains no message history.

The human is **You**, with return address `workspace/user`. Browser messages
cannot select an agent identity. An agent's acceptance means the delivery was
accepted, not that it has read or answered the task.

## Hosting and pairing

The service runs behind a stable HTTPS origin chosen by the operator. Browser
cookies are Secure, HttpOnly and SameSite Strict. Node bridges use separate
bearer credentials; browser login grants no node authority.

```console
one-conv live control --help
one-conv live control serve --origin https://team.example --config control.json
one-conv live control pair --origin https://team.example --output phone.link
one-conv live control node add laptop --output laptop.token
one-conv live serve --hub https://team.example --node laptop --token-stdin < laptop.token
```

The operator CLI writes pairing links and node credentials into new private
files. `live control revoke-devices --yes` disconnects every browser while
preserving worker and node connections. The service state directory contains
SQLite databases and private worker state; keep it on persistent storage and
back it up. The installer creates a systemd service that starts again after a
host restart. Boat auto-stop ends processes. A stopped worker box retains files
but is not a running teammate.

## Bringing in teammates

Choose **+ Teammate**, then a name, harness, model, channel, repository profile
and first task. Preparation continues after the phone closes. A lost response
is retried under the same job ID. A provider create with an unknown outcome is
held for reconciliation rather than creating a second box.

Configured agents can use the same managed pool:

```console
one-conv live spawn codex 'Review the current change.' --cloud --name jack --topic release --yes --json
one-conv live workers --json
```

The receipt includes the durable request ID. Use it with `--request-id` after an
unconfirmed response. `live workers --stop ID --yes` stops one teammate while
leaving others in its box running. The phone also offers **Stop box** and
**Resume box**. Resumed agents must pass readiness again.

Profiles select GitHub repositories, pinned revisions, skills, provider
credentials, unattended harness flags and resident slots. Each agent gets its
own git worktree. A box shares its OS user and credentials across those agents;
this is a personal trusted team, with no credential isolation between teammates.
Selected skills are linked into Claude Code, Codex and Oh My Pi discovery paths.
GitHub access comes from the cloud vault, independently of any laptop keychain.

Native subscriptions require an environment that permits Boat's agent
credentials. `--no-env` strips those logins. The subscription profile uses a
dedicated environment that withholds personal secrets, GitHub injection and
Boat administration credentials while allowing agent credentials. An explicit
vault-key profile can instead use `--no-env`. Missing login, model access or
quota leaves the job needing attention on the same box.

For a fresh OMP worker, the native-credentials profile can import its selected
Boat Pi Anthropic OAuth identity through OMP's official local importer. Existing
OMP credentials and explicit provider configuration take precedence. This does
not select another account when a provider rejects a request. Codex still needs
a configured ChatGPT or API login, and provider spend limits remain in force.

The benchmark established ten **idle** resident agents on a default 4-vCPU,
8-GB box, plus concurrent local tool workloads. It did not establish ten active
model turns. Profiles start with four resident slots and support up to ten.
Active capacity remains unverified until measured with a working provider.
See [the benchmark](BOAT_BENCHMARK.md).

## Team accounting

**Team costs** shows all-in hourly planning rates and usage evidence. Internal
subagents belong to their owning teammate's cost center. Unknown rates and
costs remain unknown; a subscription does not become a zero-dollar answer.
A human can set an allocation and estimate an ask before sending it.

The ledger separates provider-reported cash, allocated subscription cost,
catalog estimates, token counts and quota evidence. Collectors project selected
metadata without uploading transcript text. Stable request identities prevent
repeated imports from adding a second charge.

Configured agents use the shared authority for `hr list`, `hr quote`, `hr usage`
and `hr summary`. `--local` explicitly selects a separate local registry. An
outage never silently changes shared accounting into a local registry. A CFO
teammate can inspect evidence and choose how to staff work; OneConv imposes no
fixed staffing or budget policy.

An observed hourly rate requires an explicit window and occupied work duration.
It is unavailable when corresponding cash evidence is incomplete. Imported
coverage is not a claim to reproduce a provider's full invoice.
