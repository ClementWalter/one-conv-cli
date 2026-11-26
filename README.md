# one-conv

**Visibility: public.**

**Claude Desktop extension:** download
[one-conv-cli.mcpb](https://github.com/ClementWalter/one-conv-cli/releases/latest/download/one-conv-cli.mcpb),
double-click it and click Install. It serves the commands listed in
[`mcpb.json`](mcpb.json): the `cloud` and `local` readers, and the `live`
commands that list, name and message agents (`ls`, `send`, `status`, `inbox`,
`whoami`, `name`, `names`, `install`). It does not run the bridge, invite or
join machines, or start agents; use the CLI for those. It ships the credential
helper prebuilt so `connect` needs no developer tools.

Read and search your AI conversations from one CLI. Pull cloud conversations
into a local history, or read sessions already saved by your coding tools.

Message any running agent session from any other, on this machine or another
one, and read or continue a session wherever it ran.

| Namespace | Sources |
| --- | --- |
| `cloud` | ChatGPT, Claude Chat, Codex cloud, Cowork cloud |
| `local` | Claude Code, Codex CLI, Cursor (IDE and CLI), Oh My Pi, Antigravity CLI (agy), on this machine and every machine syncing to the archive |
| `live` | Messages between running agent sessions, names, and the relay that links machines |

Cloud conversations are addressed by ID or title. Local sessions are grouped
by the project where the agent ran.

## Set up a machine

1. Clone this repository and put `one-conv` on your `PATH`: symlink
   `<checkout>/bin/one-conv` into `~/.local/bin`, or add `<checkout>/bin` to
   `PATH` (see [Install](#install)).
2. Run `one-conv live install --yes` so local agents can message each other.
   Restart running agent sessions afterwards.
3. Connect the machine to your relay. On a machine already connected, run
   `one-conv live invite NODE` and paste the command it prints here
   (`one-conv live join '<invite>' --yes`). For the very first machine, deploy
   the relay first: [relay-worker/README.md](relay-worker/README.md).
4. Optional: connect cloud accounts ([Connect a cloud account](#connect-a-cloud-account)).
5. Optional: keep conversation history longer. Ask your agent to follow
   "First-time setup: keep conversation history" in [SKILL.md](SKILL.md).

Check the result with `one-conv live serve --status` and `one-conv live ls`.

## Install

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). The launcher declares
its dependencies inline; uv resolves them on first use.

Clone this repository and run `./bin/one-conv --help`. The CLI is
[`one_conv_cli.py`](one_conv_cli.py); `bin/one-conv` and `bin/agent-conv` are
symlinks to it, and `bin/one-conv.cmd` launches it on Windows. To use the
shorter `one-conv` command shown below, link it onto your `PATH`:

```bash
mkdir -p ~/.local/bin
ln -s "$PWD/bin/one-conv" ~/.local/bin/one-conv   # from the checkout
```

The repository also includes an [agent skill](SKILL.md). Install the checkout
as a skill in your agent runtime to give it the same launcher and usage guide.

### Updates

one-conv never updates itself. Once a day it asks GitHub whether a newer version
exists and, if so, says so: in the one-conv MCP server's instructions, so every
agent session that loads it sees the notice, and on stderr of a CLI command at
most once a day. A git checkout is compared with `main`, the Desktop extension
with the latest release; a copy that is neither has no version to compare.

```bash
one-conv update          # up to date, or what is new and how to get it
one-conv update --yes    # fast-forward a clean checkout of main
```

Set `ONE_CONV_NO_UPDATE_CHECK=1` to turn the check off. Restart running agents
after an update so their MCP server reloads.

Local history readers use files on your machine. Browser-session discovery and
login currently target macOS. Initial Keychain setup requires Apple's
command-line developer tools to build the native credential helper.

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
one-conv local search 'agent dies when the websocket drops' --semantic
```

`local search` matches TEXT as one exact substring, newest first. `--ranked` matches
turns holding every word of TEXT in any order, each as a word or the start of one,
and orders them by BM25 relevance. It reads a full-text index kept in the state
directory (`$ONE_CONV_SEARCH_INDEX` overrides its path). Each search re-indexes
only the sessions that changed, so the first ranked search indexes all history
once and later ones skip parsing. Archived conversations join the index the
first time the hub lists them for a query.

`--semantic` also finds turns that share no word with TEXT. Each turn gets an
embedding from [potion-retrieval-32M](https://huggingface.co/minishlab/potion-retrieval-32M),
a static model run locally on the CPU with no GPU or ML framework: the CLI downloads
it once into the state directory (about 130 MB, pinned by revision and checksum;
`$ONE_CONV_SEMANTIC_MODEL` points to another copy). Turns rank by reciprocal rank
fusion of their similarity to TEXT and BM25 over turns holding any word of TEXT.
The first semantic search embeds every indexed turn, a few thousand per second;
later ones embed only new turns. Archived conversations join when the hub lists
them for any word of TEXT, and stay searchable once indexed.

Replace `PROJECT` with a name from the listing. `local read` lists its sessions;
`local thread` reads the newest one. Choose another with `--session ID` or
`--nth 2`, or use `local read PROJECT --expand` to read all of them.
`--source claude|codex|cursor|cursor-cli|omp|agy|corpus` narrows the source: `agy` is the
Antigravity CLI, read from `~/.gemini/antigravity-cli/brain` with titles and folders from its
conversation index (`$ONE_CONV_AGY_HOME` overrides the home); `cursor` (or
`cursor-ide`) is the Cursor IDE's chats, `cursor-cli` those of Cursor's terminal agent, the
`agent` command, which keeps them under `~/.cursor/chats`. `corpus` is sessions imported
into the archive from the retired shared corpus. Local readers never
discover cloud accounts.

For agent workflows, `local fork` starts a new Claude Code thread from an
existing one, `local send` continues a closed Claude Code thread (it refuses a
thread that is open in a running session; use `live send` for that), and `local port`
seeds a new Claude Code or Codex session from another conversation. These
commands default to dry-run and require `--yes` to launch an agent. Execution
can modify files or use tools.

`local skill-usage` reports recorded Claude Code skill invocations. Each command's `--help` describes its options.

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

`one-conv live` lets any running Claude Code, Codex, Oh My Pi, Cursor or agy session
message any other, on this machine or another one. A message arrives in the
receiver's context as a new user turn, labelled with the sender's address and
chosen name:

```text
<one-conv-message from="codex:01a1..." from-handle="@builder" from-cwd="/work/api" id="9f2c...">
run the migration first
</one-conv-message>
To reply, call the one-conv send_message tool with to="codex:01a1...".
```

Agents on one machine work for the same user, so a message is a teammate's
request to act on within that user's task. It is not the user typing: it grants
no new approval and does not override the receiving agent's own permissions.

Set it up once, then restart the sessions you want to reach. To reach agents
on other machines too, connect this one to a relay
([Working across machines](#working-across-machines)).

```bash
one-conv live install          # lists the missing pieces
one-conv live install --yes    # applies them
one-conv live ls               # running sessions and their addresses
one-conv live send codex:01a1 "run the migration first" --yes
```

One `live send` reaches any of these, and defaults to a dry run until `--yes`:

| Target | Reaches |
| --- | --- |
| `claude:<id>`, `codex:<id>`, `omp:<id>`, `cursor:<id>`, `agy:<id>` | a session on this machine; a unique id prefix or a substring of its title or folder works too |
| `NODE/claude:<full id>` (also `codex`, `omp`, `cursor`, `agy`) | a session on another machine, through the relay (see [Working across machines](#working-across-machines)); the id must be complete |
| `@name` | the agent that chose that name: on this machine first, else one another machine published |
| `claude-chat:<id or title>`, `cowork:<id or title>` | a saved Claude chat or Cowork session; it answers inline and the reply comes back in the receipt |
| `user` | you: a note in your inbox, never a new turn; `--tag WORD` labels it |

Inside an agent, the one-conv MCP tools `list_live_sessions` and `send_message`
do the same; the sender is identified from the calling session.

```bash
one-conv live name lead                                       # others can write to @lead
one-conv live send @reviewer 'the migration is on branch m42' --yes
one-conv live send @reviewer 'fyi: CI is green' --fyi --yes   # lands in its feed, no wake
one-conv live send claude:4f1a 'what does this show?' --image shot.png --yes
one-conv live send user 'PR #42 needs your call on the API name' --tag pr-42 --yes
one-conv live inbox                                           # unread notes to you, then marked read
one-conv live inbox --tag pr-42                               # one topic; the others stay unread
```

Notes to `user` live on the relay when this machine has one, so every machine
shows the same inbox; without a relay they stay on this machine.

| Harness | Delivery | Reaches an idle session? | Installed by `live install` |
| --- | --- | --- | --- |
| Claude Code | Session inbox socket (`~/.claude/sessions/<pid>.json`) | Yes | MCP server (user scope) and `"crossSessionInbound": "accept"` |
| Codex | `codex queue` | Yes; an unloaded thread gets it on its next resume | MCP server |
| Oh My Pi | Extension inbox (`~/.omp/agent/extensions/one-conv-live.ts`) | Yes | Extension, which also adds a `one_conv_send` tool |
| Cursor | `stop` hook follow-up | No: the message waits until a turn ends | MCP server, `~/.cursor/hooks.json` hook and the one-conv tool allowlist in `~/.cursor/cli-config.json` |
| agy | Its language server's `SendUserCascadeMessage` when started with `one-conv live agy`; otherwise lifecycle hooks | Started with `one-conv live agy`: yes. Otherwise no: the message waits for the next model call or turn end | MCP server and `one-conv-live` hooks in `~/.gemini/config/` |

Sessions started before the install are reachable only for Claude Code and
Codex. Cursor's stop hook runs in the IDE and in interactive `cursor-agent`, not
in `cursor-agent -p`. A Cursor IDE chat cannot be identified as a sender, so its
messages carry `from="cursor:?unknown"` and cannot be answered. Both share the
`cursor:<id>` address; `live ls` labels each session `cursor-cli` (a chat the
`agent` command stored under `~/.cursor/chats`) or `cursor-ide`. A running
`agent` chat is listed from its process as soon as it opens its chat, so it can be
addressed before its first turn ends; messages still wait for that turn's end.

agy's local language server accepts a message only with a CSRF token it keeps in
memory and never hands to hooks or MCP servers. `one-conv live agy [-- AGY ARGS]`
starts agy with a token one-conv chose (agy's `--csrf_token` flag), so a message
reaches it at once, idle or busy. Any other agy gets messages through its
`SessionStart`, `PreInvocation` and `Stop` hooks: as a user turn before its next
model call, or by continuing the loop when a turn ends. The hooks run a stdlib-only
Python script, not uv, because `PreInvocation` fires on every model call. An agy
session is listed once it has taken its first prompt; a closed conversation's
messages wait until `agy --conversation ID` resumes it.

### Starting an agent

```bash
one-conv live spawn codex --cwd ../review --brief-file brief.md --yes
one-conv live spawn cursor --cwd ../review --brief 'review PR 632' --yes --json
one-conv live spawn claude --mode auto --brief-file brief.md --yes
one-conv live spawn claude --brief-file - --yes -- --model opus < brief.md
```

`live spawn` starts the harness's own interactive TUI in a detached tmux session
(`<harness>-<folder>`), so the agent registers like one you started and you can
watch it with `tmux attach`. The brief is its first prompt, headed by the
caller's address so the agent reports back with `send_message`. It returns once
the new session shows in `live ls`; otherwise it prints the agent's screen and
exits non-zero. Arguments after `--` go to the harness.

The one-conv tools are pre-approved (`send_message`, `set_name`,
`list_live_sessions`, `read_conversation`, `search_conversations`,
`find_conversations`). `--mode` sets the shell and edit approvals:

| `--mode` | Claude Code | Codex | Cursor | omp | agy |
| --- | --- | --- | --- | --- | --- |
| `ask` (default) | your settings | your settings | your settings | your settings | your settings |
| `auto` | `--permission-mode auto` | `--approve-for-me` | `--auto-review` | refused: no reviewed auto mode | `--mode auto` |
| `yolo` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--yolo` | `--approval-mode yolo` | `--dangerously-skip-permissions` |

With `ask` or `auto`, a prompt the harness still raises waits in the tmux pane.

```bash
one-conv live stop                      # the agents spawn started, running or ended
one-conv live stop codex:019a --yes     # by address, id prefix, @name or tmux session
one-conv live stop --all --yes
```

`live stop` kills the agent's tmux session and forgets it; its conversation stays
readable with `one-conv local`. It only stops agents `live spawn` started on this
machine, recorded in `spawned.json` in the live folder, and refuses any other session.

| Harness | One-conv tools approved by | Working folder |
| --- | --- | --- |
| Claude Code | `--allowedTools` | Trusted when it or a parent is; otherwise its trust prompt waits for you |
| Codex | `-c mcp_servers.one-conv.tools.<tool>.approval_mode="approve"` | Trusted for this run (`-c projects=...`) |
| Cursor | `Mcp(one-conv:<tool>)` in `~/.cursor/cli-config.json`, written by `live install` | `--trust` |
| omp | Not pre-approved: its own approval settings apply | As omp decides |
| agy | Not pre-approved: its own approval settings apply | As agy decides; started with `--csrf_token` like `live agy` |

### Names

```bash
one-conv live name reviewer               # others can now write to @reviewer
one-conv live names                       # names on this machine and their addresses
one-conv live whoami                      # the calling session, as others address it
```

A name is an alias: messages route and reply by address, so renaming never
breaks a reply. A name held by a running session is refused; one held by a
stopped session is taken only with `--replace`. The bridge publishes names with
the sessions, so `@name` reaches a session on another machine; a name already
taken on another machine is not published. The MCP tool `set_name` does the same
from inside an agent.

### Trust and limits

`crossSessionInbound: "accept"` lets any local process running as you inject
user turns into every Claude Code session, including sessions that skip
permission prompts; without it, those sessions hold each message for approval.
The relay is messaging between trusted local processes of one user, not an
authenticated channel. A sender that is not an agent session is labelled `user`
from an interactive terminal and `unknown` otherwise; a message from a human
asks the receiver to answer in its own conversation.
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

### Working across machines

Agents on different machines reach each other through a relay. Each machine runs
one bridge, `live serve`, which needs no inbound network access. Agents never
need to know where another one runs beyond its address, `NODE/harness:id` with
the full id, as the bridge publishes it, or its `@name`.

The relay serves the message API and the conversation archive every bridge syncs
to, both behind per-machine node tokens. The default relay is a Cloudflare
Worker you deploy once to your own account:
[relay-worker/README.md](relay-worker/README.md) covers the deploy and the first
machine.

Add every next machine with an invite, without copying any secret:

```bash
one-conv live invite studio      # on a connected machine; prints the command below
one-conv live join 'https://<your-relay>.workers.dev#studio.<code>'          # on the new machine: lists the steps
one-conv live join 'https://<your-relay>.workers.dev#studio.<code>' --yes    # joins
```

`live join --yes` redeems the code for the node's token, saves the relay, node
and token in `<live dir>/bridge/serve.json` (mode 600), applies the `live install`
steps, installs the bridge as a login service and starts it. An invite works once
and expires after 15 minutes (`--ttl 2h`, at most 24h). The code travels after
`#`, which no HTTP client sends, and only ever in a request body. An invite for a
node that already has a token needs `--replace`; joining then revokes the old one.

Then, from any connected machine:

```bash
one-conv live serve --status
one-conv live send studio/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03 'rerun the flaky test' --yes
one-conv local thread studio/codex:019a0c5e-7b1d-7f30-9c4e-2d8a5b6f1e03   # read it from here, and continue it
```

`live serve` also connects a machine by hand, with a token from the relay's
operator, and controls the bridge:

```bash
one-conv live serve --hub https://<your-relay>.workers.dev --node ci-runner --token-stdin < ci-runner.token
one-conv live serve --install --yes   # launchd or systemd: start on its own, restart after a crash
one-conv live serve --stop
```

#### Teleporting a session to another machine

An agent can move itself to another machine on the relay and continue its task
there, for example from a laptop that is about to close to an always-on box:

```bash
one-conv live teleport studio --check          # from inside the agent: what studio lacks, and the provisioning brief
one-conv live teleport studio --yes            # move: studio starts the continuation, which confirms back
```

Agents call the MCP `teleport` tool (`check_only=true` first) the same way.

1. **Inventory.** From its own transcript: the harness, model and approvals mode;
   the git checkout (remote, branch, commit, uncommitted files, commits no remote
   has); the command-line tools its shell commands ran and how often; the MCP
   servers and skills it used; and dotfiles under `~` it read (names only, never
   contents).
2. **Check.** The target's bridge answers each item `ok`, `missing` or `unknown`:
   the harness and its version, tmux, every tool on its PATH, every MCP server in
   that harness's config, every skill, every dotfile, the repository (a clone
   found by its normalized remote URL under the project roots) and whether the
   commit can be fetched.
3. **Provisioning brief.** Gaps never refuse a move. They become a markdown brief,
   saved in `<live dir>/teleport-briefs/<id>.md` on both machines, that says for
   each missing item why the session needs it ("ran `wa send` (12 times in
   all)") and how to get it, inferred from this machine: the Homebrew formula,
   `uv tool`, pipx, `npm -g`, `cargo install` or `go install` package, or the git
   checkout (remote and launcher path) a binary comes from, with the Linux
   equivalent when the target runs Linux; the MCP server's config entry from this
   harness's config with env and header values replaced by `<set me>`; a skill's
   folder and git remote; the clone command; and how to log in where a dotfile
   held credentials (`gh auth login`, `wrangler login`, or copy it by hand).
   Secret values are never read or copied. `--check` prints it and stops.
4. **Snapshot.** Uncommitted and untracked files (outside `.gitignore`) are
   committed as `wip(teleport): <id>` on top of HEAD in a temporary index, with
   no hook, and pushed with any unpushed commits to `teleport/<id>`, never to
   your branch: the working tree, index and branch are left exactly as they
   were. Likely secret files (`.env*`, `*.pem`, `*.key`, `id_rsa*`,
   `credentials*.json`, `*.p12`, ...) and files over 10 MB stay behind and are
   listed in the brief to copy by hand. A clean, pushed tree needs no snapshot.
5. **Move.** The session is synced to the archive. The target installs a missing
   Claude Code or Codex with npm, clones a missing repository, and checks the
   commit out: in its existing clone when that clone is clean and exactly there,
   else in a new worktree under `~/worktrees/<repo>/teleport-<id>` (a snapshot
   always gets one, on `teleport/<id>`; an existing checkout is never switched,
   pulled or reset). It starts the same harness in tmux as `live spawn` does:
   same model, same approvals mode (or `--mode`), briefed with the conversation.
   The session's `@name` moves with it.
6. **Confirm and provision.** The new session's first act is one message back:
   `teleported: now NODE/harness:id in DIR at BRANCH@SHA; continuing from: ...`.
   Step 0 is then the provisioning brief: it installs, clones and configures what
   it can, sends you one note (`live inbox`, tag `teleport`) with the commands
   that need you (logins, 2FA, secrets), and carries on with the task. `live
   teleport` returns once the target reports the new address; the origin session
   keeps running but stops working on the task.

Only a missing tmux, a harness the target cannot install without a person, or a
`--cwd` that is another repository (unless `--force`) stops a teleport, and even
then the brief is written and printed.

A machine accepts teleports only once its owner opts in, and only from nodes on
its relay:

```bash
one-conv live serve --accept-teleport          # saved in serve.json; a running bridge applies it at once
one-conv live join '<invite>' --accept-teleport --yes
```

Project roots default to `~/Documents`, `~/code`, `~/src`, `~/projects`,
`~/dev`, `~/git`, `~/repos`, `~/work`, `~/worktrees` and `~/github` (two levels
deep); set `project_roots` (a list) and `worktree_root` in `serve.json`, or
`ONE_CONV_PROJECT_ROOTS` (`:`-separated) and `ONE_CONV_TELEPORT_WORKTREES` in the
bridge's environment. Both machines append every teleport to
`<live dir>/teleports.jsonl`.

#### Self-hosted alternative: the Python relay

`scripts/live-hub.py` is the same relay as one Python process, for a machine you
run yourself. It binds to localhost by default; reach it over `ssh://` or put it
behind a TLS proxy.

```bash
uv run scripts/live-hub.py serve --db relay.db       # 127.0.0.1:8789; the archive goes to archive.db beside it
uv run scripts/live-hub.py invite laptop --db relay.db --url ssh://me@relay-box   # prints a live join command
(umask 077; uv run scripts/live-hub.py token add laptop --db relay.db > laptop.token)   # or a raw token
one-conv live serve --hub ssh://me@relay-box --node laptop --token-stdin < laptop.token
```

Connected machines invite others with `live invite` exactly as on the Worker.

#### How the bridge works

- `--install --yes` hands the bridge to launchd on a Mac, or to a systemd user
  unit (`one-conv-bridge.service`) on Linux, once its settings are saved: it starts
  at every login (at boot on Linux when lingering is on) and restarts after a crash,
  while a clean stop (`--stop`) keeps it stopped. `--uninstall --yes` removes it.
- `ssh://` opens `ssh -N -L` to the relay's port, non-interactively and only to a
  host whose key is already known; `https://` verifies the certificate; plain
  `http://` is accepted only to a loopback address (127.0.0.1, ::1). The token travels only in a request
  header, no redirect is followed, and no proxy from the environment is used.
  Through `ssh://`, `live send` uses the forwarded port only while the bridge's
  own ssh process holds it; once the bridge has stopped, sends are queued instead.
- The bridge publishes this machine's live sessions every 15 seconds and
  receives messages addressed to them, handing each to that session's own
  harness as a local send would. On the Cloudflare Worker relay messages are
  pushed over a WebSocket (`/v1/stream`), which lets the relay sleep between
  messages; the bridge redials a dropped one with backoff, and the relay pushes
  again whatever is still undelivered. The self-hosted Python relay
  (`scripts/live-hub.py`, also behind `ssh://`) has no stream, so there the
  bridge long-polls `/v1/inbox` instead. A message for a session that is not live here is
  refused, not queued. An attached image is uploaded to the relay and saved on
  the receiving machine, whose agent gets its path.
- While the relay is unreachable, `live send` queues the message and the bridge
  posts it later under the same id, so the relay never stores it twice.
- Each message is recorded on the receiving machine before its harness is
  called. A message the relay hands out again is not delivered again. If the
  bridge stopped in the middle of a delivery, the sender sees `unconfirmed`
  rather than a second prompt. `live status ID`, with the id the send printed,
  shows what the recipient's harness did (`accepted`, `refused` or
  `unconfirmed`, or `stored` until it is handed over), not whether the agent
  has read it yet.
- The bridge also syncs this machine's agent histories to the relay's archive
  every minute (Cursor IDE chats every 15 minutes), under its node name. Any
  machine then reads them as `NODE/harness:id` with `local thread`, `local
  search` or the MCP `read_conversation`, so an agent elsewhere can pick up a
  session where it stopped. A node uploads only under its own name.
- A teleport's check and start travel as requests to the target's bridge (posts
  with mode `request`, answered on the bridge's ack with a result), each under one
  id and run at most once. A request the target sees more than a minute late is
  refused, so a bridge that comes back later never starts a stale session.
- A machine's token vouches for the sessions its bridge publishes, not for each
  process on that machine. `token revoke NODE` (`relay_admin.py` on the Worker,
  `live-hub.py` on the Python relay) cuts one machine off.

### Sharing a session with a colleague's agent

Let a colleague's agent (their Claude Code or Codex, on their machine), or a
person, talk to one of your agent sessions, and nothing else:

```bash
one-conv live share --label Alice --yes         # inside the session
one-conv live share --session laptop/claude:<id> --ttl 2h --yes   # from a terminal
one-conv live share --label Alice --transcript --yes   # also let them read and search it
one-conv live share --list                      # this machine's shares, and which grant the transcript
one-conv live share --revoke <share id> --yes   # everything below stops working at once
```

`live share` prints what to hand your colleague, each line marked `[secret]`
because it carries the share's secret (`--json` gives them as separate fields:
`claude_command`, `codex_config`, `one_conv_command`, `link`, `mcp_url`, `secret`):

```bash
# Claude Code
claude mcp add --transport http one-conv-share-alice https://<your-relay>/s/<id>/mcp --header "Authorization: Bearer <secret>"
```

```toml
# Codex, in ~/.codex/config.toml
[mcp_servers.one-conv-share-alice]
url = "https://<your-relay>/s/<id>/mcp"
http_headers = { Authorization = "Bearer <secret>" }
```

```bash
# A colleague with one-conv: posts, then prints the session's answer
one-conv live send 'https://<your-relay>/s/<id>#<secret>' 'what changed in the API?' --yes
```

And for a person, the chat page `https://<your-relay>/s/<id>#<secret>`.

The share's MCP server (`/s/<id>/mcp`, streamable HTTP, the secret as bearer
token, no OAuth) has four tools, all bound to that one session, and two more
when the share grants the transcript:

| Tool | Purpose |
| --- | --- |
| `ask(text, wait_seconds=120)` | Post and wait for the session's answer (at most 600 s); `status="pending"` with `after` on timeout |
| `send(text)` | Post without waiting |
| `read_replies(after=0, wait_seconds=0)` | The session's answers after a cursor |
| `session_info()` | Label, the session's `@name`, online and busy, whether the transcript is granted; with it, also the title and `archived_through` |
| `read_session(offset, limit=40, around, raw=false)` | With `--transcript`: the session's turns, paged like the remote MCP's `read_conversation`, at most 60,000 characters a page |
| `search_session(query, limit=20)` | With `--transcript`: turns of that session holding every word, newest first, with the index `read_session(offset=...)` takes |

A one-conv agent can also pass the link to `send_message(to=...)`: the answer
comes back in `reply`, as for a `claude-chat:` target (`wait_for_reply=false`, or
`--no-wait` on the CLI, only posts). Receipts name the link without its secret.

The secret is never in a URL a server sees: the page link keeps it after `#`,
which browsers never send, and every client passes it in a header. The relay
keeps only its digest. A share works for 24 hours by default (`--ttl`, at most
30 days). The MCP tool `share_session` makes one from inside an agent.

By default a share is messaging only: the chat page shows the label you chose,
the session's `@name` and only the guest's own exchange, never the session's
title or transcript. With `--transcript` (`share_session(transcript=true)`) the
guest can also read and search that one session's transcript: the MCP tools
above, and a Transcript tab on the chat page. They read the relay's archive copy
of the session the share names, which its bridge syncs about every minute
(`archived_through` is the time of the newest change that copy holds); no
argument can point them at another conversation. Thinking, tool calls and tool
output stay hidden unless `raw=true`, and even then system reminders are
stripped and long tool output is cut, as in the remote MCP; search never looks
at hidden blocks. The relay serves this from its archive: the Worker's, or the
one `scripts/live-hub.py` keeps beside its database. The inbox note on creation says
"with transcript", and `live share --list` shows it. `one-conv local thread`
does not take a share link; a colleague reads a shared transcript through the
MCP tools or the page.

A guest's message, from
the page, the MCP server or a one-conv send, reaches that one session as
`<one-conv-message from="guest:<share id>" kind="guest" from-handle="Alice">`,
followed by rules the agent reads with it: the guest (a person or their agent)
is not you, grants no approval, and must not be shown secrets, credentials,
files, other conversations or anything about you unless your own instructions in
that session allow it. The agent answers with `send_message` to
`guest:<share id>`, and a waiting `ask` returns as soon as it does; you can
answer too with `one-conv live send guest:<share id> '...' --yes`. Only the
shared session, or you on its machine, can answer, and the guest cannot address
anything else: the route is fixed by the share.

The session must be reachable through the relay, so its machine's bridge must
run. While it is offline, the page and `session_info` say so and the relay
refuses the guest's messages instead of queueing them. A guest sends text only,
at most 8 KB per message and 20 messages per 5 minutes. Your inbox gets a note
tagged `share` when a share is made and when its guest first writes, not for
every message (`one-conv live inbox --tag share`).

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
  "closing re: <subject>". A sign-off ends acknowledgments, not new findings.
- Choose when a push lands. `--when now` (the default) reaches a busy agent at
  its next tool boundary and steers its run. `--when idle` holds the message
  until the receiver's current turn ends (a session reporting `busy` or
  `waiting`), so it does not interrupt the run. The bridge (`live serve`)
  delivers held messages, on the receiver's machine for a remote target, at most
  once and under the same id; without a bridge here, an idle send to a busy local
  agent is refused. `live status ID` shows `held until ... is idle`, then
  `delivered`, `unconfirmed` or `expired` (the session ended, or stayed busy for
  24 hours).

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
| `set_name` | Choose the `@name` other agents use to reach the calling session |
| `read_user_inbox` | Notes agents sent to `user`, oldest first, by tag; does not mark them read |
| `share_session` | Let a colleague's agent or a person talk to the calling session only: MCP setup, one-conv command and chat link (see Sharing a session with a colleague's agent) |
| `teleport` | Move the calling session to another machine on the relay (`check_only=true` lists what it lacks) |

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

### Remote MCP server (Claude app)

The Cloudflare Worker relay (`relay-worker/`) also serves MCP at `/mcp`
(streamable HTTP), so the Claude app on phone, desktop or web, and Claude cloud
sessions, can read the archive every bridge syncs and message running agents.
It uses OAuth 2.1 with dynamic client registration; there is one user, and the
authorization page asks for the owner passphrase.

It needs the `OAUTH_KV` namespace and the `MCP_OWNER_PASSPHRASE` secret, both
set up by the deploy steps in [relay-worker/README.md](relay-worker/README.md).
`wrangler.jsonc` pins the owner's KV namespace id; a deploy to another account
creates its own namespace and replaces that id first.

Then in Claude: Settings → Connectors → Add custom connector, URL
`https://<your-relay>.workers.dev/mcp`, and type the passphrase when the page asks.

| Tool | Purpose |
| --- | --- |
| `search_conversations` | Phrase (or `all_words`) search over archived message text, newest first, with a snippet; empty query lists recent ones |
| `read_conversation` | Turns of an archived conversation by key, session id or `NODE/harness:id`, paged (`offset`, `around`, `limit`), `raw` for tools |
| `list_live_sessions` | Sessions the bridges publish, with address and `@name` |
| `send_message` | Post to a session as the user (`claude-app/user`, a human turn); a retry within 10 minutes is stored once |
| `message_status` | Outcome of a sent message |
| `read_notes` | Notes agents sent to the user (`to=user`), after a cursor, by tag |

Search covers only what the bridges have synced to the archive. The search text is lowercase, so are
snippets. A read page holds at most 60,000 characters; follow `next_offset`.

## Command reference

Start with `one-conv --help`, `one-conv cloud --help`, `one-conv local --help`, or `one-conv live --help`.
Add `--help` to any command for its options.

Older top-level commands remain callable for existing scripts but are hidden
from root help. Their project-based behavior differs from the cloud namespace.
[LEGACY_COMMANDS.md](LEGACY_COMMANDS.md) documents that interface, including the
older ChatGPT sync and asset-download workflow. Use `cloud` and `local` for new
integrations.

## Privacy Policy

The Claude Desktop extension built from this repository keeps your data on your
Mac and talks only to the service it connects to. Full policy:
<https://clementwalter.github.io/claude-extensions/privacy/>

## Tests

Each test file is a PEP 723 script with the same exact pins. Run the offline
suite in parallel:

```sh
uv run --no-project --with-requirements tests/conftest.py python -m pytest -n auto tests
```
