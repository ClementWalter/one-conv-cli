# one-conv

Read and search your AI conversations from one CLI. Pull cloud conversations
into a local history, or read sessions already saved by your coding tools.

| Namespace | Sources |
| --- | --- |
| `cloud` | ChatGPT, Claude Chat, Codex cloud, Cowork cloud |
| `local` | Claude Code, Codex CLI, Cursor (IDE and CLI), Oh My Pi, shared corpus files |

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

## Connect a cloud account

Sign in to the provider in Chrome, Arc, Brave, Edge, or Chromium. For example,
open Claude's sign-in page with:

```bash
one-conv cloud claude connect --login --browser chrome
```

Finish signing in in the browser. Opening the page does not verify the login.

Every session one-conv verifies is saved in a private session vault
(`sessions/` in the one-conv state folder under `~/.config`, files readable only by you). To add a
second account, sign in to it in the same browser and run `accounts` again:
the first account stays available from the vault until its session expires,
even though the browser now shows the other one. Signing out on claude.ai
revokes that session for good, so add a second Claude account from another
browser or browser profile instead of signing out of the first.

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
```

Replace `PROJECT` with a name from the listing. `local read` lists its sessions;
`local thread` reads the newest one. Choose another with `--session ID` or
`--nth 2`, or use `local read PROJECT --expand` to read all of them.
`--source claude|codex|cursor|cursor-cli|omp|corpus` narrows the source. Local readers never
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

`one-conv live` relays messages between running Claude Code, Codex, Oh My Pi
and Cursor sessions. A message arrives as a new user turn, wrapped with the
sender's address so the receiver can answer:

```text
<one-conv-message from="codex:01a1..." from-name="Fix tests" from-cwd="/work/api">
run the migration first
</one-conv-message>
To reply, call the one-conv send_message tool with to="codex:01a1...".
```

Set it up once, then restart the sessions you want to reach:

```bash
one-conv live install          # lists the missing pieces
one-conv live install --yes    # applies them
one-conv live ls               # running sessions and their addresses
one-conv live send codex:01a1 "run the migration first" --yes
```

`live send` takes an address, an id prefix, or a substring of a session's title
or working directory, and defaults to a dry run. Inside an agent, the one-conv
MCP tools `list_live_sessions` and `send_message` do the same; the sender is
identified from the calling session.

| Harness | Delivery | Reaches an idle session? | Installed by `live install` |
| --- | --- | --- | --- |
| Claude Code | Session inbox socket (`~/.claude/sessions/<pid>.json`) | Yes | MCP server (user scope) and `"crossSessionInbound": "accept"` |
| Codex | `codex queue` | Yes; an unloaded thread gets it on its next resume | MCP server |
| Oh My Pi | Extension inbox (`~/.omp/agent/extensions/one-conv-live.ts`) | Yes | Extension, which also adds a `one_conv_send` tool |
| Cursor | `stop` hook follow-up | No: the message waits until a turn ends | MCP server and `~/.cursor/hooks.json` hook |

Sessions started before the install are reachable only for Claude Code and
Codex. Cursor's stop hook runs in the IDE and in interactive `cursor-agent`, not
in `cursor-agent -p`. A Cursor IDE chat cannot be identified as a sender, so its
messages carry `from="cursor:?unknown"` and cannot be answered.

`crossSessionInbound: "accept"` lets any local process running as you inject
user turns into every Claude Code session, including sessions that skip
permission prompts; without it, those sessions hold each message for approval.
The relay is messaging between trusted local processes of one user, not an
authenticated channel. A sender that is not an agent
session is labelled `user` from an interactive terminal and `unknown` otherwise.
These labels are self-reported, not proof of identity or of your approval:
`--from` must name a running session, but any local process can name any of
them. Message bodies cannot open or close an envelope of their own. A
sender may send at most 10 messages to one target per 5 minutes; this throttles
two agents that keep replying to each other but does not end the exchange. Live state lives in
`~/.config/agent-conv-cli/live` (override with `ONE_CONV_LIVE_DIR`).

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
  one-conv local search "Round 2 review" --source claude`. `local thread` selects
  only the last N turns, so "see its latest turn" goes stale as soon as the
  receiver takes another turn.
- Copy instead when the content is short, when the recipient must act on the
  exact wording (a decision, an approval scope, a command), or when the
  recipient cannot read the thread (another machine, a cloud session, a Cursor
  IDE chat).
- A reference is not delivery: if someone must act, send them a message.

Delivery order is not guaranteed across harnesses: a `codex queue` message can
arrive after one sent later through another route.

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
