# one-conv

Read and search your AI conversations from one CLI. Pull cloud conversations
into a local history, or read sessions already saved by your coding tools.

Work with live teammates from a Slack-style workspace, including human messages
and a paired iPhone app. See [the mobile workspace guide](MOBILE_WORKSPACE.md)
for cloud hosting, shared worker boxes and team accounting.

| Namespace | Sources |
| --- | --- |
| `cloud` | ChatGPT, Claude Chat, Codex cloud, Cowork cloud |
| `local` | Claude Code, Codex CLI, Cursor (IDE and CLI), Oh My Pi, shared corpus files |
| `live` | Agent messaging, topics, spawning, hub and mobile workspace |
| `hr` | Shared or local rate cards, usage evidence and expected ask costs |

Cloud conversations are addressed by ID or title. Local sessions are grouped
by the project where the agent ran.

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). The launcher declares
its dependencies inline; uv resolves them on first use.

Clone this repository and run `./bin/one-conv --help`. The executable is
[`bin/one-conv`](bin/one-conv). Add the checkout's `bin` directory to your `PATH`
to use the shorter `one-conv` command shown below.

The repository also includes an [agent skill](SKILL.md). Install the checkout
as a skill in your agent runtime to give it the same launcher and usage guide.

Local history readers use files on your machine. Browser-session discovery and
login currently target macOS. Initial Keychain setup requires Apple's
command-line developer tools to build the native credential helper.

## Agent hourly rates and ask estimates

`hr` stores an hourly rate for each stable agent cost center, including that
agent's internal subagents. Display names are labels; `@aliases` and agent addresses are references;
renaming an agent or changing its session does not change its cost center ID.
Stored addresses are not checked against live sessions.

Register a rate with its currency, source and definition of a metered hour.
These amounts are illustrative examples, not measured provider prices:

```bash
one-conv hr register jack --name Jack --alias jack --address codex:example-session \
  --hourly-rate 12 --currency USD --basis allocated \
  --provenance 'Example subscription allocation' --metering-basis 'Occupied work hour including tools'
one-conv hr register john --name John --alias john --unknown-rate --currency USD \
  --basis quoted --provenance 'Not measured' --metering-basis 'Occupied work hour including tools'
one-conv hr list --json
one-conv hr show @jack --json
one-conv hr quote jack --minutes 15 --json
```

The example Jack quote is `3.00000000 USD`, calculated as `12 × 15 / 60`.
John's quote has `expected_cost.amount: null` and status `unknown`; an explicitly
zero rate yields a known zero estimate. Quotes retain currency, provenance,
metering basis and rate basis (`observed`, `allocated` or `quoted`). They are
planning estimates, not invoices or incremental cash charges. They neither
contact agents nor choose staffing. No currency conversion occurs. Costs round
to eight decimal places; rate inputs allow up to 18 digits and eight decimal
places, bounded at one billion per hour. Estimated minutes are bounded at one
year (`525600`).

Identical registration retries return the existing card. To change a card, repeat
`register` with the same ID and `--replace`; this replaces the whole card,
including its aliases and addresses. References cannot belong to two cost centers.

With a configured live hub, HR commands use its authenticated shared authority.
`--hub` requires that authority; `--local` explicitly selects this machine.
An unavailable hub never silently becomes a separate local registry. Local cards
live in `hr/rates.sqlite3` under the live state directory (`ONE_CONV_LIVE_DIR`).
`ONE_CONV_HR_DIR` overrides the local HR directory.
The registry starts empty; accounting does not choose staffing or seed rates.

Import actual usage metadata from an explicit Codex, Claude Code or Oh My Pi
transcript, an OpenRouter JSONL response/SSE capture, or canonical accounting
events. For example, with an existing saved OpenRouter response:

```bash
one-conv hr ingest response.jsonl --source openrouter --cost-center jack \
  --billing-scope router-account --worker jack-session --json
one-conv hr usage --cost-center jack --json
one-conv hr summary --cost-center jack --json
one-conv hr summary --cost-center jack --since 2026-01-01T00:00:00Z \
  --until 2026-01-01T01:00:00Z --occupied-seconds 1800 --json
```

`--source` also accepts `codex`, `claude`, `omp` and `event`; `-` reads stdin.
Native session IDs identify workers unless `--worker` is supplied. The explicit
`--cost-center` owns imported internal subagents too; `--parent-worker` records
their parent when needed. `--billing-scope` is a stable billing authority/account
label, shared across machines importing that account, never an API key.

Every event keeps its reported model/provider, native token counters, timestamp,
worker/parent, provenance and coverage. Missing metadata remains `null`. Cached
and reasoning tokens are retained without inferring a price or entitlement
consumption. Native token fields have source-specific cache semantics. Native
durations whose units are unverified remain unknown; explicit canonical events
can provide `wall_seconds`.

Cash, subscription allocations, configured-price estimates and quota are separate
fields and summary buckets. [OpenRouter's reported `usage.cost`](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
or generation `total_cost` is recorded as its account charge; upstream inference
cost is not added again. Other provider bills and fixed subscriptions remain
outside that account charge. OMP's calculated `usage.cost` is an estimate,
including an explicit estimated zero; it is never treated as reported cash.
Allocations and quota require explicit canonical event values with provenance.

Summaries describe recorded events only. Unknown cash charges prevent a complete
cash total and observed cash hourly rate. Currencies stay separate. An observed
rate requires one owner, a half-open `[since, until)` window and positive occupied
owner seconds inside that window; parallel subagent wall times are not summed
into the denominator. Rate cards are not changed automatically.

The SQLite ledger deduplicates by billing scope and provider request/generation
ID, or by stable native source identity when no request ID is available. Repeated
imports return duplicate counts. Later provider evidence can fill unknown cost,
tokens, model or provider fields; receipts report `enriched: true` for the same
identity. Known values and node/owner attribution cannot be overwritten. Per-field
provenance retains the evidence that established each value. Conflicting known
facts reject the whole batch. OpenRouter creation timestamps use whole seconds
so completion and later generation metadata can join. Importers handle
current Codex response records, legacy cumulative-counter snapshots, and repeated
Claude content blocks without adding the same usage twice. Malformed records
fail visibly. Inputs are bounded at 100000 records and 4 MiB per record.

The ledger stores accounting metadata only in `usage.sqlite3`. For local mode, set
`ONE_CONV_ACCOUNTING_DIR` or pass `--directory` on usage commands to select its
directory; the default is the HR directory. Combine `--directory` with `--local`
when a hub is configured. The same
`one_conv.accounting.Ledger(directory)` helpers serve a cloud authority. SQLite
transactions protect concurrent processes. Hub imports send canonical numeric
events in bounded, idempotent batches; workers and parents are qualified with
the configured node. The authority derives bearer node identity and enforces
ownership. UI and MCP can use the same authority separately. No model API request
or transcript scan occurs implicitly.

For automatic worker metering, a worker bootstrap supplies a private ownership
mapping. Only its explicit native history paths are scanned. Relative paths are
resolved beside this JSON file; this is an illustrative mapping:

```json
{
  "schema_version": 1,
  "histories": [
    {"source": "codex", "path": "history", "billing_scope": "native-account"}
  ],
  "owners": {
    "codex:SESSION": {"cost_center_id": "jack", "worker_id": "node/codex:SESSION"}
  }
}
```

Each history requires `source` (`codex`, `claude` or `omp`), `path` (a directory or
file) and a stable `billing_scope`. Owners can override `billing_scope` or provide
`parent_worker_id`. Unmapped sessions are counted and skipped; native Codex and
Claude children inherit their mapped parent's cost center with distinct worker
IDs. Keep retired worker/probe mappings for late usage. No subscription quota or
cash charge is inferred from native token counts.

```bash
one-conv hr sync --mapping workers.json --hub --json
one-conv hr watch --mapping workers.json --hub --interval 5 --max-backoff 60 --json
```

`sync` runs once; `watch` reloads mappings and hub credentials each pass. Use
`--iterations 1` for a bounded check or `--local` for an isolated local ledger.
Polling defaults to five seconds; failures back off up to sixty seconds and retry
the same identities. Each pass bounds file count, event count and scan time;
oversized inputs fail visibly. Incomplete final writes wait for the next pass.
Only finalized Claude responses are imported automatically. The watcher reports
coverage and transfers canonical accounting metadata, never transcript content.
The reusable entrypoints are `sync_mapping(mapping_path, sink)` and
`watch_mapping(mapping_path, sink_factory, ...)` in `one_conv.accounting.watcher`.

## Connect a cloud account

one-conv reads the provider logins of your everyday browser (Chrome, Arc,
Brave, Edge, or Chromium). To add any other account, sign in to it with:

```bash
one-conv cloud claude connect --login
```

This opens a browser window on a profile only one-conv uses (`browser-profiles/`
in the one-conv state folder, private to you), so each account you add this way
has a login of its own. Sign in to as many accounts as you like, one window
each, and sign in and out freely in your everyday browser: signing out there
never disconnects them. When one of these logins expires, `accounts` names it;
renew it with `connect --login --profile claude-1`. Opening the page does not
verify the login.

Every session one-conv verifies is also saved in a private session vault
(`sessions/` in the one-conv state folder), so an everyday-browser account stays
available after you switch that browser to another account, until its session
expires or you sign out of it on the provider's site.

Authorize one-conv to read that browser's session, then verify account access:

```bash
one-conv cloud claude connect --browser chrome
one-conv cloud claude accounts
```

The authorization command may show a macOS Keychain dialog for
`credential-helper-v1`. Choose **Always Allow** to let the persistent helper
reuse this authorization. Normal reads, discovery, and pulls never open
password dialogs.

`accounts` lists sessions the CLI can actually access. An empty result means
no account is accessible, not that its conversation history is empty.

Replace `claude` with `chatgpt`, `codex`, or `cowork` for the other products.
Claude Chat and Cowork use the Claude login; ChatGPT and Codex use the OpenAI
login. Keychain authorization for a browser is shared across these products.

## Refresh, list, and read

Refresh accessible accounts across all cloud products and list conversations:

```bash
one-conv cloud chats --refresh
```

Each row shows activity time, source, account label, short conversation ID,
turn count, and title. Copy the ID in square brackets into `read`:

```bash
one-conv cloud read ID
```

Replace `ID` with the displayed value, or use a unique title. Ambiguous matches
require a more specific ID. `cloud chat ID` and `cloud thread ID` are aliases.

The default reader shows conversation text. Claude Chat and Cowork tool calls,
tool results, and thinking blocks are hidden. `--raw` exposes additional
retained details; tool calls and results are summarized by the renderer.

| Reading option | Effect |
| --- | --- |
| `--limit 20` | Show the last 20 visible turns; `0` shows all |
| `--no-mark-read` | Preserve the unread marker |
| `--json` | Emit structured data on stdout |
| `--refresh` | Refresh accessible accounts before reading saved history |

The `●` marker means unread **in one-conv**, independently of the provider's
read state. Reading marks the conversation read unless `--no-mark-read` is
set. A later cache-file update can make it unread again. `? tok` means a token
count is unavailable for that source.

## Saved history versus live data

Cloud readers use saved history unless you explicitly request a refresh. They
announce the mode on stderr, keeping JSON stdout suitable for pipelines.

| Command | Network behavior |
| --- | --- |
| `cloud chats` | List saved conversations; no fetch |
| `cloud read ID` | Read a saved conversation; no fetch |
| `cloud search TEXT` | Search saved message text; no fetch |
| `cloud find TEXT` | Find saved conversations by title; no fetch |
| `cloud chats --refresh` | Refresh accessible accounts, then list |
| `cloud read ID --refresh` | Refresh accessible accounts, then read the saved conversation |
| `cloud claude pull` | Fetch Claude history into the cache |
| `cloud claude accounts` | Contact Claude to discover accessible browser accounts |
| `cloud cached-accounts` | Show saved account groups and counts; no fetch |

Refresh fetches up to **100 conversations per account and product**. Results
can also include older saved conversations and accounts that were not
refreshed. This is a bounded update, not a guarantee that every cached
conversation is current. Reading an older ID with `--refresh` does not fetch
that specific conversation if it falls outside the batch.

Global refresh reports inaccessible sources as skipped. If a discovered
account's pull fails, it attempts the other accounts, preserves successful
pulls, and exits with an error before displaying results. It does not silently
substitute old results for a failed refresh.

Use a product's `pull --limit` to request a larger batch, up to 10,000
conversations. Pulls are resumable: conversations whose cached copy is complete
and current are not read again, and a conversation listed twice while paging
is read once. Pulls run from the CLI wait out provider throttling (ChatGPT
spaces out conversation reads) instead of failing; MCP refreshes keep short
timeouts. The sync report distinguishes `ready`, `partial`, and `error`.
`partial` can mean the batch limit was reached or a transcript was incomplete.
A complete Cowork event snapshot does not mean the task itself has finished.

## Accounts and sources

Discover account selectors before an explicit pull:

```bash
one-conv cloud claude accounts --json
one-conv cloud claude pull --account ACCOUNT --limit 1000
```

Replace `ACCOUNT` with a selector from discovery. OpenAI accepts an exact email
or account ID. Claude accepts an organization name or ID, or the full account
ID. Ambiguous selectors require the full ID. A product's `pull` automatically
selects the account when only one is accessible.

For one product, use `cloud chatgpt`, `cloud claude`, `cloud codex`, or
`cloud cowork`. Each has `connect`, `accounts`, `pull`, `chats`, `read`,
`thread`, `search`, `find`, and `unread`. Product readers accept `--refresh`.

Shared commands accept `--source` with the stored names `chatgpt`,
`claude-chat`, `codex-cloud`, or `cowork-cloud`. On `cloud chats --refresh`,
this restricts both fetching and listing.

`chats --limit` controls displayed rows, not the refresh batch size. It defaults
to 30; `--limit 0` lists all saved conversations.

## Local agent history

Local commands organize sessions by project:

```bash
one-conv local chats
one-conv local read PROJECT
one-conv local thread PROJECT
one-conv local search TEXT
one-conv local search 'bridge reconnect' --ranked
```

`local search` matches TEXT as one exact substring, newest first. `--ranked` matches
turns holding every word of TEXT in any order, each as a word or the start of one,
and orders them by BM25 relevance.

Replace `PROJECT` with a name from the listing. `local read` lists its sessions;
`local thread` reads the newest one. Choose another with `--session ID` or
`--nth 2`, or use `local read PROJECT --expand` to read all of them.
`--source claude|codex|cursor|cursor-cli|omp|corpus` narrows the source: `cursor` (or
`cursor-ide`) is the Cursor IDE's chats, `cursor-cli` those of Cursor's terminal agent, the
`agent` command, which keeps them under `~/.cursor/chats`. Local readers never
discover cloud accounts.

For agent workflows, `local fork` starts a new Claude Code thread from an
existing one, `local send` continues a closed Claude Code thread (it refuses a
thread that is open in a running session; use `live send` for that), and `local port`
seeds a new Claude Code or Codex session from another conversation. These
commands default to dry-run and require `--yes` to launch an agent. Execution
can modify files or use tools.

`local export` writes normalized transcripts to a corpus, `local append` writes
a turn to the shared log, and `local skill-usage` reports recorded Claude Code
skill invocations. Each command's `--help` describes its options.

## Storage and coverage

Normalized cloud transcripts live under `~/.cache/one-conv-cli/cloud`,
overridable with `ONE_CONV_CLOUD_CACHE`. Read-state bookkeeping uses
`ONE_CONV_STATE_DIR`, defaulting to `~/.config/one-conv-cli` or an existing
`~/.config/agent-conv-cli` directory. Older ChatGPT cache files remain readable
and can overlap with the normalized cloud cache.

Cloud adapters use unofficial provider APIs and remain experimental:

| Product | Coverage |
| --- | --- |
| ChatGPT | Conversation listings and the selected message branch within each conversation |
| Claude Chat | Organization-scoped conversations and message trees |
| Codex cloud | Current tasks and returned turn graphs; no archived-task discovery |
| Cowork cloud | Remote sessions and paginated event transcripts; no local Cowork sessions |

The credential helper is installed once and reused across normal CLI updates.
This local build uses ad hoc signing; replacing the executable can require
Keychain authorization again. Browser sessions can expire independently.

See [PROVIDERS.md](PROVIDERS.md) for adapter boundaries, API-change handling,
and verification evidence. `cloud providers` reports implementation coverage,
not your accounts' live connection status.

## Message running sessions

`one-conv live` lets running Claude Code, Codex, Oh My Pi, Cursor and Boat agents
work as one team. Start with one agent, and let it bring others in. A message
arrives in the receiver's context as a new user turn, labelled with the sender's
address, chosen name and topic:

```text
<one-conv-message from="codex:01a1..." from-handle="@builder" from-cwd="/work/api" topic="#release-4-2" id="9f2c...">
run the migration first
</one-conv-message>
To reply, call the one-conv send_message tool with to="codex:01a1...", or to="#release-4-2" to answer everyone on the topic.
```

Agents on one machine work for the same user, so a message is a teammate's
request to act on within that user's task. It is not the user typing: it grants
no new approval and does not override the receiving agent's own permissions.

Set it up once, then restart the sessions you want to reach:

```bash
one-conv live install          # lists the missing pieces
one-conv live install --yes    # applies them
one-conv live ls               # running sessions and their addresses
one-conv live send codex:01a1 "run the migration first" --yes
```

One `live send` reaches any of these, and defaults to a dry run until `--yes`:

| Target | Reaches |
| --- | --- |
| `claude:<id>`, `codex:<id>`, `omp:<id>`, `cursor:<id>` | a session on this machine; a unique id prefix or a substring of its title or folder works too |
| `boat:<sandbox>/<conversation>` | an agent in a Boat sandbox |
| `NODE/claude:<full id>` (also `codex`, `omp`, `cursor`) | a session on another machine, through a hub (see [Working across machines](#working-across-machines)); the id must be complete, and Boat conversations keep their own route |
| `@name` | the agent on this machine that chose that name |
| `#topic` | every member of the topic but you |

Inside an agent, the one-conv MCP tools `list_live_sessions` and `send_message`
do the same; the sender is identified from the calling session.

A typical start, from the first agent you prompt:

```bash
one-conv live name lead                                   # others can write to @lead
one-conv live join release-4-2                            # a topic for this piece of work
one-conv live spawn codex 'review the migration; join #release-4-2' --yes
one-conv live send '#release-4-2' 'migration is on branch m42' --yes
one-conv live send @reviewer 'fyi: CI is green' --fyi --yes   # lands in its feed, no wake
```

| Harness | Delivery | Reaches an idle session? | Installed by `live install` |
| --- | --- | --- | --- |
| Claude Code | Session inbox socket (`~/.claude/sessions/<pid>.json`) | Yes | MCP server (user scope) and `"crossSessionInbound": "accept"` |
| Codex | `codex queue` | Yes; an unloaded thread gets it on its next resume | MCP server |
| Oh My Pi | Extension inbox (`~/.omp/agent/extensions/one-conv-live.ts`) | Yes | Extension, which also adds a `one_conv_send` tool |
| Cursor | `stop` hook follow-up | No: the message waits until a turn ends | MCP server and `~/.cursor/hooks.json` hook |

Sessions started before the install are reachable only for Claude Code and
Codex. Cursor's stop hook runs in the IDE and in interactive `cursor-agent`, not
in `cursor-agent -p`. A Cursor IDE chat cannot be identified as a sender, so its
messages carry `from="cursor:?unknown"` and cannot be answered. Both share the
`cursor:<id>` address; `live ls` and the workspace label each session `cursor-cli`
(a chat the `agent` command stored under `~/.cursor/chats`) or `cursor-ide`.

### Names and topics

```bash
one-conv live name reviewer               # others can now write to @reviewer
one-conv live join release-4-2            # subscribe, and catch up on what was said
one-conv live send '#release-4-2' "migration merged" --yes   # every member gets it
one-conv live topics                      # topics, members, last activity
one-conv live topic release-4-2           # what you have not seen on it yet
one-conv live summarize release-4-2       # which block of posts waits for its one-line summary
one-conv live topic release-4-2 --open 0-63   # open a summarized block into its halves
```

- A name is an alias: messages route and reply by address, so renaming never
  breaks a reply. A name held by a running session is refused; one held by a
  stopped session is taken only with `--replace`.
- Sending to `#topic` records the message on the topic and delivers it to every
  member but the sender, each through its own harness; the result lists one
  status per member, and a member that is not reachable now catches up later.
  Sending makes you a member, locally and through a hub. `--topic` files a direct
  message under a topic too.
- Each member keeps a read position, so `join` and `topic` show what is new to
  that reader, labelled as history rather than new requests, in about 20 lines:
  the newest posts whole, older ones as one-line summaries of aligned blocks of
  16, 32, 64... posts, so a long channel still reads as a page. `--open BLOCK`
  descends into a block, down to the posts. A topic keeps its last 500 posts;
  positions and summaries outlive that.
- Agents write the summaries as they go: the post that completes a block comes
  back with the request to summarize it (the posts for a block of 16, the two
  halves' summaries above that), and `live summarize TOPIC BLOCK "<line>"`
  records it. The first line recorded for a block stays.
- The MCP tools `set_name`, `join_topic`, `list_topics`, `summarize` and
  `open_block` do the same from inside an agent, and `send_message` accepts
  `@name` and `#topic`.

### Human posts: ack and sync

A human post wakes every session running in its channel. Each session acks it,
and the workspace shows a check mark per session that read it. The sessions then
agree in the channel who does what.

```bash
one-conv live ack 0c43b732d61146d1a376ea74a702ce99      # the id the message arrived with
one-conv live send '#release' "I take the changelog" --yes
one-conv live spawner --harness claude --cwd ~/work --yes # let the hub start sessions here
one-conv live spawner --harness claude --cwd ~/work --env-file /run/secrets/env.sh --yes # credentials read at each start
```

- A session works in one channel and watches the others: the first channel it
  joins is its work channel (`live join TOPIC --work` moves it, `--watch` only
  watches). A post wakes the sessions working in its channel and reaches watchers
  as an FYI, so a session busy in one channel is not pulled into another.
- The human can archive a channel: it leaves the channel list for a folded
  "Archived" section, agents still list and read it, and a new human post there
  brings it back.
- A session acks a message it received, or any post of a channel it joined.
  Acking again changes nothing. The MCP tool `ack` does the same from an agent.
- Anything that cannot be undone (sending a message for the user, a payment) is
  done by one session, named in the channel.
- A human post in a channel with nobody running starts a session at once: on a
  machine whose bridge offers to (`live spawner`), else on a cloud worker when no
  such machine is connected, refuses, or no session acks within 90 s. The post
  shows "Starting a session…" until a session acks it.
- The cloud spawner is a Boat box the controller builds and runs itself. Its
  worker config takes `"spawner": {"node": "<hub node>"}`, plus optionally `box`
  (an existing box to adopt), `git_user` (`{"name", "email"}`; else the box's
  GitHub account), `profile`, `harness`, `args`, `priority` and `env_file`
  (default `/run/ascii-secrets/env.sh`, where Boat writes the agent credentials).
  With no box, or once Boat no longer knows it, the controller creates one
  (auto-stop off). Whenever the controller's revision or these settings change,
  it provisions the box from nothing: clones, GitHub login, git identity, hub
  settings, `live install`, `live spawner`, and the bridge's systemd unit
  (`live serve --install`). It sleeps the box after 30 idle minutes and wakes it
  when a post waits.
- Limits: the hub authenticates machines, not each process, so a session on a
  machine is trusted as that machine's agent.

### Work with agents in a browser

```bash
one-conv live web            # then open http://127.0.0.1:8790
```

A local workspace for a person to follow the team and send messages as **You**:

- **channels** are topics, local or from the hub;
- **people** are running sessions with their chosen names;
- **direct messages** come from the message log that every send writes to
  `messages.jsonl`, which keeps the last 2,000 messages.
- **human messages** go to a selected topic or session, labelled `user` with
  `kind="human"`. Agents can reply with `send_message(to="user", ...)` or
  `one-conv live send user "reply" --yes`; topic replies stay on the topic.

It listens on 127.0.0.1 only and refuses foreign Host headers. Sending requires
the workspace's own origin and a fresh per-instance token; the browser always
sends as the human, independently of the process that launched the server.
Message ids are claimed on disk before delivery, so retries cannot wake an
agent twice. An interrupted or uncertain send stays **unconfirmed**; check the
conversation before starting a replacement message. Sent means the transport
accepted it, not that an agent read it.

The page shows every message as plain text. It has no login, so anyone using
this machine can read it and send messages. Looking at a topic doesn't change
any agent's catch-up position. Local human direct replies reach this workspace;
with a hub, use shared topic replies because the hub has no human return route.

### Trust and limits

`crossSessionInbound: "accept"` lets any local process running as you inject
user turns into every Claude Code session, including sessions that skip
permission prompts; without it, those sessions hold each message for approval.
The relay is messaging between trusted local processes of one user, not an
authenticated channel. A sender that is not an agent
session is labelled `user` from an interactive terminal or the human workspace
and `unknown` otherwise.
These labels are self-reported, not proof of identity or of your approval:
`--from` must name a running session, but any local process can name any of
them. Message bodies cannot open or close an envelope of their own. A
sender may send at most 10 messages to one target per 5 minutes; this throttles
two agents that keep replying to each other but does not end the exchange. Live state lives in
`~/.config/agent-conv-cli/live` (override with `ONE_CONV_LIVE_DIR`). After a
delivery between two identified sessions, `peers.json` there records the pair
(both addresses sorted, joined by a space) with the time of its last exchange;
if that record cannot be written, the send still reports delivered with a warning.
The record only chooses what `local feed` follows by default; it grants no
permission and is not evidence of anyone's approval.

### Bringing more agents in

An agent can start teammates from any harness without anyone opening a terminal:

```bash
one-conv live spawn codex 'review the diff on this branch' --yes
one-conv live spawn claude 'write the tests' --model sonnet --cwd ../repo --trust-folder --yes
one-conv live spawn cursor-cli 'review the diff' --trust-folder --arg=--force --arg=--approve-mcps --yes
one-conv live spawned                     # running spawned agents
one-conv live stop codex:01a1…            # end one
```

`spawn` starts the harness interactively in a detached tmux session, waits until it
registers, and prints its address. Because it stays running between turns, it keeps
receiving `live send` messages after its first answer, unlike `codex exec` or
`claude -p`, which exit. Its first prompt opens with who started it and how to answer
teammates. Watch one with `tmux attach -t NAME`. It runs with your usual permission
settings for that harness, so an unattended agent can stop at a prompt: pass that
harness's own flags with `--arg`. Claude Code asks whether to trust a folder it has not
seen; `spawn` stops and says so unless `--trust-folder` answers yes. `cursor-cli` (or
`agent`, or `cursor`) starts Cursor's terminal agent, the `agent` command; `--trust-folder` passes its `--trust`, and a
message to it is typed into its tmux pane, since an idle Cursor chat runs no hook to read its
inbox. Oh My Pi needs the live extension from `live install`. A stopped Codex thread stays loaded in Codex's
app-server for a few minutes, but `live ls` no longer lists it.

### Working across machines

Agents on different machines reach each other through a hub. Each machine runs
one bridge, `live serve`, which needs no inbound network access. Agents never
need to know where another one runs beyond its
address, `NODE/harness:id` with the full id, as `live serve` publishes it.

On the machine that hosts the hub (the hub binds to localhost by default):

```bash
(umask 077; uv run scripts/live-hub.py token add laptop --db hub.db > laptop.token)   # one per machine, private
uv run scripts/live-hub.py serve --db hub.db                             # 127.0.0.1:8789
```

On each machine, with the token issued to it:

```bash
one-conv live serve --hub ssh://me@hub-box --node laptop --token-stdin < laptop.token
one-conv live serve --hub https://hub.example.com --node ci-runner --token-stdin < ci-runner.token
one-conv live serve --status
one-conv live serve --install --yes   # launchd or systemd: start on its own, restart after a crash
one-conv live send ci-runner/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes
```

- `--install --yes` hands the bridge to launchd on a Mac, or to a systemd user
  unit (`one-conv-bridge.service`) on Linux, once its settings are saved: it starts
  at every login (at boot on Linux when lingering is on) and restarts after a crash,
  while a clean stop (`--stop`) keeps it stopped. Sessions it started in tmux keep
  running when it restarts. `--uninstall --yes` removes it.
- `ssh://` opens `ssh -N -L` to the hub's port, non-interactively and only to a
  host whose key is already known; `https://` verifies the certificate; plain
  `http://` is accepted only to a loopback address (127.0.0.1, ::1). The token travels only in a request
  header, no redirect is followed, and no proxy from the environment is used.
  Through `ssh://`, `live send` uses the forwarded port only while the bridge's
  own ssh process holds it; once the bridge has stopped, sends are queued instead.
- The bridge publishes this machine's live sessions every 15 seconds and
  long-polls for messages addressed to them, handing each to that session's own
  harness as a local send would. A message for a session that is not live here is
  refused, not queued.
- While the hub is unreachable, `live send` queues the message and the bridge
  posts it later under the same id, so the hub never stores it twice.
- Each message is recorded on the receiving machine before its harness is
  called. A message the hub hands out again is not delivered again. If the
  bridge stopped in the middle of a delivery, the sender sees `unconfirmed`
  rather than a second prompt. `live status ID`, with the id the send printed,
  shows per recipient what its harness did (`accepted`, `refused` or
  `unconfirmed`, or `stored` until it is handed over), not whether the agent
  has read it yet.
- A machine's token vouches for the sessions its bridge publishes, not for each
  process on that machine. `token revoke NODE` cuts one machine off.
- Topics work across machines once a bridge runs: a `#topic` post goes to every
  member wherever it runs, and catch-up reads the hub's history. Names are
  published with each session and shown on received messages, but `@name`
  resolves only on the sender's machine for now: address a remote agent as
  `NODE/harness:id`, which is also what its messages give as the reply address.

### Agents in Boat sandboxes

`boat:<sandbox>/<conversation>` addresses a coding agent running in a
[Boat](https://boat.dev) sandbox, through the `box` CLI and your Boat account.
The conversation id is Boat's, not the agent's own session id.

```bash
one-conv live ls --harness boat            # running sandboxes' conversations, idle or busy
one-conv live send boat:bx_ab12/2ab1… "run the tests" --yes
one-conv live pull boat:bx_ab12/2ab1…      # read the agent's answers back
```

- An idle conversation gets `box prompt --resume`; a busy one gets `box steer`.
  Boat wraps a steered text as a message from you and may stop the command the
  agent was running, so the envelope inside it is what names the real sender. If
  the turn ends just before the steer, the message is prompted instead.
- The remote agent cannot message this machine, so the envelope asks it to answer
  in its own conversation, and `live pull` reads that back with `box events`.
- If Boat has not confirmed a prompt within 45 seconds, the send reports
  `pending` with an operation id instead of failing, because the prompt may still
  arrive. The same applies when `box prompt` exits without Boat accepting or
  refusing (`unconfirmed`): only an explicit refusal from Boat means it was not
  delivered. Do not resend: `one-conv live status OP` shows the outcome, and
  another send to that conversation is refused until it is accepted, refused, or
  checked with `live pull` and marked `live status OP --resolve`.
- `--fyi` cannot reach a sandbox. Boat is listed only when asked for by name
  (`--harness boat` or a `boat:` address), since listing calls Boat's API.

Tested against real sandboxes, with the agent logins stored in the tester's Boat
account:

| Boat provider | Status |
| --- | --- |
| Claude Code | Verified: idle push and pull through one-conv; a busy conversation took `box steer` byte for byte when run by hand |
| pi | Verified: idle push and pull; the first reply took about four minutes |
| Codex | Not verified: not configured on the tested account (`provider_not_configured`) |
| Cursor | Not supported: Boat has no Cursor provider |

### Keeping multi-agent exchanges readable

Any agent can read another agent's thread with `one-conv local thread`, so an
exchange can be reconstructed from the transcripts. A relayed message lands as
a user turn in the **receiver's** thread. In the sender's thread it exists only
as a tool call, which `local thread` hides unless `--raw` is given, so a
reference must point at the receiver's thread.

- Start each message with a header line such as
  `to: codex:01a1  cc: claude:6c51  re: migration order`, so either side of the
  exchange can be rebuilt.
- Reference instead of copying when the content is long, when a recipient may
  not need it, or when several agents follow the same exchange. Make the pointer
  precise: the receiver's address, the send time, and a quoted phrase to search
  for, for example `replied to claude:6c51 at 23:19Z, find it with
  one-conv local search "Round 2 review" --source claude`. Choose a phrase
  distinctive enough that later messages quoting it are unlikely. `local thread` selects
  only the last N turns, so "see its latest turn" goes stale as soon as the
  receiver takes another turn.
- Copy instead when the content is short, when the recipient must act on the
  exact wording (a decision, an approval scope, a command), or when the
  recipient cannot read the thread (another machine, a cloud session, a Cursor
  IDE chat).
- A reference is not delivery: if someone must act, send them a message.
- End exchanges once. A pushed message wakes its receiver, so informational
  notices go with `live send --fyi`, which lands in the receiver's `local feed`
  without waking it. A result someone is waiting on is still pushed, with
  `--no-reply`, because an idle agent does not run its feed. Nobody answers a
  message marked `reply="none"`, and the last message that needs action says
  "closing re: <topic>". A sign-off ends acknowledgments, not new findings.

Delivery order is not guaranteed across harnesses: a `codex queue` message can
arrive after one sent later through another route.

### Following other agents' threads

`live send` covers what a sender knows matters. `local feed` covers the rest:
what the agents you work with said since you last looked, without rereading
their whole threads.

```bash
one-conv local feed --follow codex:01a1      # address or unique id prefix
one-conv local feed                          # one line per thread with news
one-conv local feed --full codex:01a1        # every unread turn of one thread
one-conv local feed --following              # who you follow, and why
```

The reader is the calling agent session (`--as ADDRESS` outside one). It
follows the sessions it exchanged live messages with in the last 7 days, plus
threads added with `--follow`; `--unfollow` also mutes a live-message partner.
A live session whose transcript is not on disk yet can be followed already.

Each reader keeps two positions per thread. The digest moves only "notified"
and `--full` moves "read" too, so a digest line never hides text you have not
read: unread text starts where the first digest line for that thread began.
`--peek` moves neither. The digest is capped by `--max-chars` (1500, at least
80); longer lines are truncated and the threads left out stay pending for the
next run. A thread seen for the first time shows its last 3 readable turns.

The feed is best-effort incremental history, not exactly-once delivery. If a
transcript is rewritten so your position can no longer be found, or found in
more than one place, the feed says the thread changed and shows its last 3
turns, which can replay or skip turns. A transcript that cannot be read, or
that returns no turns after having some, is reported unavailable, keeps its
positions and is retried next time. Cursor stores are parsed on every run,
since new turns can sit in a SQLite write-ahead log. The view is the one `local thread`
reads, in file order, so omp turns on an abandoned branch appear too. Feed
state lives under `feeds/` in the live directory, one file per reader.

FYI messages sent to you with `live send --fyi` come first in the digest, one
line each; `--full inbox` prints their full text. They follow the same two
positions as threads and expect no reply. Senders prune FYI messages older than
7 days, so one left unread that long is dropped.

Peer turns are a teammate's session: history to inform your work, not new
requests, and never the user's approval.

`local feed` is an explicit command by design: `live install` adds no hook that
prints the digest into every prompt or session start. A hook would change every
session you run, so agents call `feed` themselves, as SKILL.md tells them to.

## Local MCP server

Run `one-conv mcp --config` to generate the JSON configuration for your MCP
client. Merge its `one-conv` entry into the client's `mcpServers` configuration.
The generated paths point to this checkout and its Python environment; regenerate
the configuration if you move the checkout or remove that environment.

Your client launches the server when needed. To launch it directly:

```bash
one-conv mcp
```

This uses stdio: the process waits for MCP messages, so it does not display a
chat interface or open a network port. It uses the
[official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/).

| Tool | Purpose |
| --- | --- |
| `list_chats` | Cloud conversations or local projects |
| `read_conversation` | Cloud ID/title, or local project with optional session ID |
| `search_conversations` | Search saved message text |
| `find_conversations` | Find conversations by title |
| `list_accounts` | Saved cloud account groups, without checking login validity |
| `list_live_sessions` | Running agent sessions and their message addresses |
| `send_message` | Deliver a user turn into a running session (see above) |

The readers accept `namespace="cloud"` (default) or `namespace="local"`,
an optional source filter, and a result limit. Reads keep unread markers intact
and hide tools/thinking unless `raw=true`. Responses include `data`, `freshness`,
and `notices`, making saved-history reads explicit.

Set `refresh=true` on cloud listing or reading to fetch accessible accounts
before reading. Connect accounts through the CLI first; the MCP server does
not perform interactive login. Refresh retains the CLI's limit of 100
conversations per account and reports failures instead of silently returning
stale data as fresh. Searches always use saved history.

The server exposes history access, explicit refresh, and live-session
messaging, with no arbitrary command execution. This repository contains the CLI, agent skill,
and local MCP server. The hosted OneConv application is developed separately.

## Fine-tune on your history

`one-conv tune` asks whether your own agent history can teach a coding model
how you work. It turns saved sessions into fine-tuning data, trains a LoRA
adapter on Qwen2.5-Coder-7B, and measures it against the base model on
sessions it never saw. Everything stays in `~/.cache/one-conv-cli/tune`
(`ONE_CONV_TUNE_DIR`) unless you use the Modal commands.

**Data.** `snapshot` exports every human-driven session through the export
format above: user and assistant text only, secrets redacted. The dataset
drops machine-written "user" turns (relay preambles, harness warnings, other
sessions' messages), keeps ChatGPT voice turns as their transcription, merges
copies of the same conversation, and takes training rows only from agentic
coding tools (`--targets` overrides). Each training row is the conversation
up to an agent reply plus that reply; the loss covers the reply only. The
newest sessions are held out for testing (`--split time`).

**Labels.** Every moment where you answered an agent reply is classified:
accepted, moved on, correction you could have been spared, correction that
brought new facts, question, or unclear. `label` uses classifier.dev (free,
keyless, asked not to keep the text) by default, SubQ Snap or Jev with a key,
or an LLM judge on Boundless for the uncertain ones (`--provider boundless
--uncertain-from classifier`). Accepted and moved-on replies become SFT
targets; avoidable corrections become DPO pairs (rejected reply vs the reply
that followed); new-fact corrections are kept for evaluation only. `annotate`
lets you label uncertain moments yourself, and `compare` scores each labeler
against your labels.

**Looking at the data.** `review` samples labels, `mood` rates the tone of
every reaction from calm to angry and lists the most heated sessions, and
`view` writes a local HTML viewer of all conversations with their labels.

```bash
one-conv tune snapshot && one-conv tune label && one-conv tune mood
one-conv tune view                                    # local viewer, never published
one-conv tune dataset --name base --labels            # SFT rows, DPO pairs, new-fact pairs
one-conv tune synth --rule no-comments                # optional rule data, see below
one-conv tune train --dataset base --run first        # MLX on Apple Silicon
one-conv tune eval --run first
one-conv tune probe --run first --rule no-comments
one-conv tune runs                                    # compare runs
```

**On a GPU.** `tune cloud` runs the same steps on a Modal H100 (about 20
minutes and a few dollars per run), with vLLM for generation and TRL for
training: `push`, `synth`, `train`, `dpo` (preference training on top of an
SFT run), `eval`, `benchmark` (HumanEval, to catch lost general coding
ability), `probe`, `pull`, and `nightly` to refresh everything and retrain.
`clean --yes` deletes the Modal volume, including the uploaded sessions.

**Measures.** `eval` reports perplexity on your held-out accepted replies
(how expected they are, not what the model writes) and preference shift: how
often training moved probability toward the reply you accepted rather than the
one you corrected, relative to the starting model, so reply length cancels
out. `probe` asks a rule's coding tasks after growing amounts of real
conversation and counts violations for the base model, the base model with the
rule in its system prompt, and the adapter, which was never told the rule.
`synth` provides that rule's training data by regenerating replies to your own
coding requests with the rule as a system prompt and rewriting them to obey
it; `dataset --synth RULE` trains on them without the prompt.

**What a first experiment found** (about 2,000 human sessions, Qwen2.5-Coder-7B):
training on accepted replies cut held-out perplexity from 9.2 to 7.4 with no
loss on HumanEval, almost all of it from the raw sessions; DPO on about 860
avoidable corrections moved probability toward the accepted reply on 81% of
held-out pairs; and a no-comments rule learned from synthetic data held at
about 50% violations across 2k to 16k tokens of history, while the same rule
in the system prompt went from 15% with no history to 80-100%. These reading
metrics show the model's probabilities moved; they do not yet show that its
own replies would be accepted more often.

## Command reference

Start with `one-conv --help`, `one-conv cloud --help`, `one-conv local --help`, or `one-conv live --help`.
Add `--help` to any command for its options.

Older top-level commands remain callable for existing scripts but are hidden
from root help. Their project-based behavior differs from the cloud namespace.
[LEGACY_COMMANDS.md](LEGACY_COMMANDS.md) documents that interface, including the
older ChatGPT sync and asset-download workflow. Use `cloud` and `local` for new
integrations.
