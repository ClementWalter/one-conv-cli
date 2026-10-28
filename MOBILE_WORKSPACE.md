# OneConv on your phone

The cloud workspace combines the mobile web app, live hub, human mailbox,
the cloud spawner box and shared accounting. A laptop is an ordinary connected node.
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
one-conv live serve --install --yes   # keep the bridge running across restarts (launchd or systemd)
```

The operator CLI writes pairing links and node credentials into new private
files. `live control revoke-devices --yes` disconnects every browser while
preserving node connections. The service state directory contains SQLite
databases and the spawner box's private state; keep it on persistent storage and
back it up. The installer creates a systemd service that starts again after a
host restart.

## Bringing in teammates

Choose **+ Teammate**, then a name, a channel and a first task. The request shows
in the channel, and a connected machine that offers to start sessions starts the
teammate there: your own machine first, the cloud spawner box otherwise. The
phone says which machine took it. A lost response is retried under the same
request ID, which the hub keeps to one teammate. The teammate runs the harness
that machine's spawner starts (`live spawner --harness`).

Ordinary node credentials cannot ask for teammates. The operator can delegate
staffing to a CFO or another node using the service's existing control state
directory:

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

An explicitly delegated agent asks the same way:

```console
one-conv live spawn claude 'Review the current change.' --name jack --topic release --yes --json
```

The receipt includes the durable request ID. Use it with `--request-id` after an
unconfirmed response.

The cloud spawner box is built from code by the controller (see the README's
`spawner` block): it clones the profile's repositories on their own branches,
follows one-conv's main, runs a self-updating Claude Code, and sleeps when no
session on it is busy. A profile names the box's repositories, Boat environment
and machine type.

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
