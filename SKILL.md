---
name: one-conv-cli
description: >-
  Read the user's AI conversation history with one-conv. Use cloud for ChatGPT,
  Claude Chat, Codex cloud and Cowork; use local for Claude Code, Codex CLI,
  Cursor, Oh My Pi and the shared corpus on disk. Discover accounts, pull cloud
  history, list conversations, search messages and read threads. Local agent
  continuation commands require explicit --yes. Cloud authentication reuses
  authorized browser sessions without Keychain password prompts.
---

# One conversation reader

Use the bundled `bin/one-conv` launcher, or `one-conv` when installed on PATH.
The Python launcher resolves its dependencies through `uv`.

## Choose the namespace first

```bash
one-conv cloud --help
one-conv local --help
```

- `cloud chatgpt`: ChatGPT web conversations.
- `cloud claude`: Claude web chat, distinct from local Claude Code.
- `cloud codex`: Codex cloud tasks, distinct from local Codex CLI.
- `cloud cowork`: remote Cowork sessions and paginated event transcripts.
- `local`: local Claude Code, Codex CLI, Cursor (IDE and CLI), Oh My Pi and corpus files.

Old top-level commands remain compatibility aliases. Prefer namespaces for new
invocations so local work cannot implicitly scan cloud accounts. For legacy
asset sync, online ChatGPT search and existing automation, see
[LEGACY_COMMANDS.md](LEGACY_COMMANDS.md).

## Cloud history

Every product exposes `connect`, `accounts`, `pull`, `chats`, `read`, `thread`, `search`,
`find` and `unread`. Pull contacts the provider. Readers and searches inspect
saved history without contacting authentication services.

`cloud chats` lists individual saved conversations across products; product
`chats` commands restrict that list to one product. `--limit 0` lists all saved
conversations. `cloud cached-accounts` lists account groups and cached counts;
`cloud read ID` opens a conversation using the displayed ID prefix or a unique
title; `cloud chat ID` and `cloud thread ID` are aliases. Product readers use the
same conversation lookup. Ambiguous matches require a longer ID. `--no-mark-read`
preserves unread status. Cloud read no longer takes an account/project query.
product `accounts` discovers accessible browser logins. Listing never pulls.
Every cloud reader identifies saved history on stderr, keeping JSON stdout clean.
Product readers accept `--refresh` to pull up to 100 conversations before reading;
failed refreshes return an error instead of silently displaying old results.
The result still includes older saved conversations, not just the refreshed batch.
`cloud chats --refresh` refreshes all accessible supported accounts across
products (up to 100 conversations per account/product). It reports skipped
sources explicitly and attempts all accounts before returning any refresh
errors. `--source` restricts both refresh and listing. JSON stays on stdout;
progress, skips, and sync reports go to stderr.

`cloud claude connect --login --browser chrome` opens Claude sign-in. If Chrome
already has another Claude account, switch accounts there or use a separate
browser profile to keep both live. Verify with `cloud claude accounts` after
sign-in. Opening a login page does not verify a connection. Plain `connect`
authorizes Keychain access; it does not sign in to the provider.

```bash
one-conv cloud claude connect --browser chrome
one-conv cloud claude accounts --json
one-conv cloud claude pull --account Zama --limit 10
one-conv cloud claude chats --json
one-conv cloud claude read QUERY --json
one-conv cloud claude thread QUERY --no-mark-read --json
one-conv cloud chatgpt pull --account EMAIL --limit 10
one-conv cloud codex pull --account EMAIL --limit 10
one-conv cloud search QUERY --json
```

Replace `QUERY`, `EMAIL` and account selectors with values from discovery.
Claude selectors include organization names and IDs; ambiguous names require
the exact account ID. Claude account identity includes browser profile and
organization. Unknown account email is left unset.

Claude and OpenAI reuse provider-scoped browser sessions. Keychain access is
strictly noninteractive: being signed into a browser does not prove the CLI
can decrypt its session. Empty discovery or an authorization error is not an
empty conversation history. Never disable prompt suppression to work around
background authentication failures. When the user explicitly authorizes setup,
`connect --browser chrome` permits one Keychain dialog through a stable native
helper. Ask the user to choose Always Allow; never run connect from a background
check. This browser authorization is shared by Claude and OpenAI.

The helper is compiled with optimization and installed once in the user's
Application Support directory. Passive reads never rebuild it. This local
development installation uses ad hoc signing; replacing the helper binary can
require authorization again. Distributable app updates need a stable signing
identity. Browser sessions themselves can still expire independently.

`--session-file` remains an internal broker integration option. Do not ask users
to copy tokens or treat that file as customer onboarding. No hosted login or
session renewal service is implemented. See [PROVIDERS.md](PROVIDERS.md) for
provider coverage, live verification and API-change handling.

## Local history

```bash
one-conv local chats --json
one-conv local read PROJECT --source claude --json
one-conv local thread PROJECT --source codex --no-mark-read --json
one-conv local search QUERY --json
one-conv local find QUERY --json
one-conv local unread --json
one-conv local skill-usage --json
```

`local chats` lists projects/accounts. `read QUERY` lists a project's conversations;
`read QUERY --expand` includes their messages. `thread QUERY` reads one
conversation, selecting the latest unless `--nth` or `--session` is supplied.
`search` finds message text; `find` matches conversation titles.

Local `--source` accepts `claude`, `codex`, `cursor`, `cursor-cli`, `omp`, or `corpus`.
Queries naming the same local project merge matching agent histories.
`--raw` includes tool details. Reading updates local unread bookkeeping unless
`--no-mark-read` is supplied. Consult each command's `--help` before less common
operations.

## Continuing local agent conversations

`local fork`, `local send` and `local port` launch actual agents. They default
to dry-run and require `--yes` to act. `fork` and `send` target Claude Code;
`port --into claude|codex` starts a new conversation with rendered context.
These agents can modify files or use tools, so preserve the user's action scope.

`local export` writes normalized history into a corpus checkout. `local append`
writes one turn to the shared corpus log. These are writes, not history reads.

## Verification

Distinguish current provider access, cached history and parser tests. A browser
page read is not proof of a successful CLI pull. A successful bounded pull is
not a complete account backfill. Report authentication and schema failures
explicitly; do not represent them as a connected account with no messages.
