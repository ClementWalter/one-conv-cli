# OneConv on your phone

The cloud workspace combines the mobile web app, live hub, human mailbox,
worker jobs and shared accounting. A laptop is an ordinary connected node.
The workspace continues to run when it disconnects.

Open a pairing link in Safari, choose **Connect this device**, then use Safari's
Share menu and **Add to Home Screen**. Links are single-use and expire after ten
minutes by default. The resulting session lasts up to thirty days. Disconnecting
the device revokes its session. Offline navigation shows a reconnect page;
the public app shell contains no message history.

From a connected device, open **Device settings → Connect another device** to
generate a private one-use link for your other device. It expires after ten
minutes and grants full owner access, including messaging and staffing. Creating
a new link invalidates the previous unused link while keeping connected devices
signed in. Share the plain workspace URL for navigation; keep `#pair=…` links private.

The human is **You**, with return address `workspace/user`. Browser messages
cannot select an agent identity. An agent's acceptance means the delivery was
accepted, not that it has read or answered the task. The conversation held in a
session's terminal shows in that session's conversation too: the machine's
bridge reports new prompts and text replies every 20 seconds, as `one-conv local
thread` renders them, skipping relayed messages, command output and the order a
spawned session started with.

A message can carry up to four images (PNG, JPEG, GIF or WebP, 10 MB each):
paste one into the composer, drop it, or pick it with the paperclip. The image
is uploaded at once and travels with the message; the receiving machine's
bridge saves it under its live directory and tells the session where, so the
agent reads the file to see it.

Agent names and short IDs stay stable; a native conversation title is context.
The roster separates the bridge's connection from the agent's reported idle or
busy status. A loaded native thread with no activity report is **Unverified**,
and an expired bridge or session publication is **Offline**. Neither a connected
process nor an accepted message proves provider credits or a reply. Last-message
context shows recorded workspace activity, not an inferred current assignment.
Offline conversations keep their history; a new direct message requires the
agent to be connected again. Reopening the app keeps the selected conversation.

## Channels

Every agent joins **#general** the first time its bridge publishes it, and so do
you. An agent that leaves #general stays out. Choose **+** next to Channels to
create one: give it a name, a kickoff message and the connected agents to add.
They join at once and receive the kickoff; #general gets a one-line announcement
without asking for replies, and anyone else can join with the `join_topic` tool or
`one-conv live join NAME`. Retrying the same form finishes the same channel.

The archive button in a channel's header hides it from your list once you are
done with it; it folds under **Archived**, where the same button brings it back.
Agents still read and post in an archived channel, and your next post there
brings it back on its own.

While an agent in the open channel or direct message is working on a turn,
"@name is working…" shows above the composer. It follows the busy status the
agent's bridge reports every 15 seconds, so it can trail a reply by a few seconds.

Direct messages between agents on the same machine bypass the hub; that
machine's bridge reports them afterwards from its local message log, with their
original time, target and local outcome. They appear under Direct messages marked
"Synced history" and never notify anyone again. A direct message tagged with a channel also appears in that channel,
marked "To @name only".

## Routines

A routine is a prompt the workspace posts in a channel on a schedule, as you.
Choose **+** next to Routines: give it a name, a channel, when (every day,
weekdays, a day of the week at a time, every hour, or a cron line) and the
prompt. Times are in the time zone of the device that created it. At each time
the cloud workspace posts the prompt, marked with the routine's name, so agents
working in the channel pick it up and a channel nobody works in gets a session
started, the same as for your own posts. A fire missed while the workspace was
down runs once when it is back. Open a routine to run it now, pause or resume
it, open its channel or delete it; its posts stay in the channel.

Agents manage routines with `one-conv live routine ls|add|run|pause|resume|rm`.
Reading works for any node; changing a routine needs the staffing grant, since
a routine posts as you.

## Unread

A channel or direct message with agent messages you have not seen turns bold
with a count, and the tab title shows the total. Opening it while the tab is
visible clears it. The workspace keeps one read position per conversation, so
reading on your phone clears it on your laptop too; a position only moves
forward. Your own messages never count, and a topic-tagged direct message counts
in its direct thread, not in the channel. History from before unread tracking
started counts as read. An agent that has ended stays listed under
its direct messages while they are unread, so every count in the title can be
opened. An archived channel keeps its count on its own row but leaves the title. `one-conv live web` keeps the same read positions in
`web-read.sqlite3` beside its message journal.

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
one-conv live serve --install --yes   # on a Mac: keep the bridge running across restarts
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
held for reconciliation. Further requests for that profile can use an existing
box with free slots, but wait before allocating another box. Canceling a waiting
request does not cancel the original uncertain creation. Inspect Boat and use
the original request's **Use this existing box** form to attach its sandbox.

Ordinary node credentials can inspect jobs but cannot change staffing. The
operator can delegate staffing to a CFO or another node using the service's
existing control state directory:

```console
one-conv live control node add cfo --output cfo.token --staffing
```

Use the service's `live control --directory` setting when it uses a custom state
directory. To delegate an existing key instead, run
`one-conv live control node grant-staffing cfo`. The grant covers every process
sharing that node key; replacing the key requires a new grant. Later,
`one-conv live control node revoke-staffing cfo` removes staffing authority while
keeping messaging and accounting available. Revoking the node key also removes
its grant. Paired human browsers retain their staffing authority.

An explicitly delegated agent can use the same managed pool:

```console
one-conv live spawn codex 'Review the current change.' --cloud --name jack --topic release --yes --json
one-conv live workers --json
```

The receipt includes the durable request ID. Use it with `--request-id` after an
unconfirmed response. `live workers --stop ID --yes` stops one teammate while
leaving others in its box running. The phone also offers **Stop box** and
**Resume box**. Resumed agents must pass readiness again.
After the box resumes, choose **Check again** on each preserved teammate to
restart it under the same request and box. A teammate stopped individually
stays stopped; add a new teammate to replace it.

Profiles select GitHub repositories, pinned revisions, skills, provider
credentials, harness approval settings and resident slots. Built-in default
harness settings run unattended; custom profiles can change approval and
sandbox settings. The phone describes the selected profile and harness's
configured access before **Bring teammate online**. Each agent gets its own git
worktree. A box shares its OS user and credentials across those agents;
this is a personal trusted team, with no credential isolation between teammates.
Selected skills are linked into Claude Code, Codex and Oh My Pi discovery paths.
GitHub access comes from the cloud vault, independently of any laptop keychain.

Native subscriptions require an environment that permits Boat's agent
credentials. `--no-env` strips those logins. The subscription profile uses a
dedicated environment that withholds personal secrets, GitHub injection and
Boat administration credentials while allowing agent credentials. An explicit
vault-key profile can instead use `--no-env`. Missing login, model access or
quota leaves the job needing attention on the same box.

Task dispatch waits for an authenticated model response, hub registration and
channel membership, and a working numeric usage collector; a collector failure
leaves the outcome needing checking rather than **Online**.

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
