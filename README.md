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

Finish signing in in the browser. If it already has your personal account
signed in, switch to your work account there, or use separate browser profiles
to keep both accounts available. Opening the page does not verify the login.

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
conversations. The sync report distinguishes `ready`, `partial`, and `error`.
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
existing one, `local send` continues a Claude Code thread, and `local port`
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

The readers accept `namespace="cloud"` (default) or `namespace="local"`,
an optional source filter, and a result limit. Reads keep unread markers intact
and hide tools/thinking unless `raw=true`. Responses include `data`, `freshness`,
and `notices`, making saved-history reads explicit.

Set `refresh=true` on cloud listing or reading to fetch accessible accounts
before reading. Connect accounts through the CLI first; the MCP server does
not perform interactive login. Refresh retains the CLI's limit of 100
conversations per account and reports failures instead of silently returning
stale data as fresh. Searches always use saved history.

The server exposes history access and explicit refresh, with no message sending
or arbitrary command execution. This repository contains the CLI, agent skill,
and local MCP server. The hosted OneConv application is developed separately.

## Command reference

Start with `one-conv --help`, `one-conv cloud --help`, or `one-conv local --help`.
Add `--help` to any command for its options.

Older top-level commands remain callable for existing scripts but are hidden
from root help. Their project-based behavior differs from the cloud namespace.
[LEGACY_COMMANDS.md](LEGACY_COMMANDS.md) documents that interface, including the
older ChatGPT sync and asset-download workflow. Use `cloud` and `local` for new
integrations.
