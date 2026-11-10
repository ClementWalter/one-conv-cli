# Session-based cloud adapters

The experimental cloud path separates three responsibilities:

1. The authentication broker supplies an authorized HTTP session. Adapters never
   inspect browser stores or own user login.
2. `one_conv/providers/` contains private routes, response schemas, pagination,
   auth exchange and normalization. `base.py` supplies bounded GET transport
   and explicit authentication, rate-limit, unavailable and schema-change errors.
3. `one_conv/cloud.py` consumes normalized records, preserves branches and source
   attribution, writes private atomic cache files and records each sync outcome.
   Normal CLI search and reading consume these records without private API URLs.

## Coverage

| Product | Adapter | Current coverage | Live verification in this change |
| --- | --- | --- | --- |
| ChatGPT | `chatgpt.py` | Paginated current conversation branch; separate legacy graph reader | Browser listing and two transcript responses verified; older-message pagination observed |
| Claude Chat | `anthropic.py` | Explicit organization, v2 conversation listing and message tree | Four conversations listed; two real transcripts parsed |
| Codex cloud | `codex.py` | Current tasks and full returned turn graphs | Three tasks listed; two task graphs parsed |
| Cowork cloud | `cowork.py` | Remote sessions with paginated event snapshots | Zama task listed and its transcript pulled through the CLI on 2026-09-08 |

Codex reads the returned turn graph, including its active branch, for current
tasks. Archived discovery is not implemented; the browser's archived listing
was empty. Claude Code and Cowork are not inferred from Claude Chat access.
Provider capabilities remain distinct from account login. `one-conv cloud
providers` reports these limits. Cowork uses browser authentication; managed
session-file authentication is not implemented for it.

Live evidence was collected on 2026-09-08 from authenticated Chrome interface
requests through the supported browser connection. Response bodies were fed
through the real adapters; only synthetic regression fixtures enter Git.
This verifies network routes, actual response shapes and normalization. It does
not verify the separate Python cookie-to-bearer exchange, hosted login, session
renewal or operation with the user's browser closed. No credentials were copied.

The current ChatGPT UI uses `/backend-api/conversations/{id}` and earlier-page
`/messages?before=...` requests. This exposes the selected branch, not every
regenerated alternative. `coverage` and `complete` are persisted explicitly.
A seven-message conversation was complete; three captured pages of a longer
conversation totaled 115 messages and still indicated older history. Bounded
reads report partial, and never overwrite an existing complete cached snapshot.
The legacy singular graph endpoint remains a separate compatibility path and
was not live-verified in this browser pass.

The observed search UI uses a read-only POST to `/backend-api/global/search`.
`global_search` matches that request and normalizes conversation hits while
retaining source-status and partial-result metadata. The first captured page
contained ten hits and a continuation cursor; only that first-page contract is
verified here. This POST is narrowly allowlisted in the transport. Legacy CLI
search keeps its separate GET compatibility route, not live-verified this pass.

Claude's observed listing is `chat_conversations_v2` with `data` and `has_more`,
not the earlier array response. The adapter normalizes its synthetic root UUID
before the core traverses the active branch. Message content blocks are `text`,
`thinking`, `tool_use`, `tool_result`, `image` and `document`; any other type
fails the read and names the type.

`cloud claude send` uses two of one-conv's three provider writes (upload and completion). It POSTs to
`/api/organizations/{org}/chat_conversations/{id}/completion` with `prompt`,
`parent_message_uuid` (the chat's `current_leaf_message_uuid`, or the synthetic
root for an empty chat), `timezone`, empty `attachments` and `files`, and
`rendering_mode: messages`; no Origin header is required. The response is
server-sent events: `message_start` carries the reply `uuid` and its
`parent_uuid` (the posted turn), `content_block_delta` carries `text_delta`
pieces, then `message_delta`, `message_limit` and `message_stop`. Verified live
on 2026-11-10 against a chat-capable organization; an organization whose
capabilities lack `chat` (API-only individual orgs) answers 403. The transport
allowlists only this route and never retries it, since a retry could post twice.
`cloud cowork send` POSTs `{"events": [{"payload": {"type": "user", "uuid",
"session_id", "parent_tool_use_id": null, "message": {"role": "user",
"content"}}}]}` to `/v1/code/sessions/{id}/events` with the ccr headers, the
route the web app's `sendUrl` uses. The answer's `results[0].sequence_num` marks
the posted event; the turn's reply is the `assistant` events after it, up to the
`result` event (its `subtype`, e.g. `success`, is the stop reason). The events
listing ignores `from_sequence_num`, so the poll pages the whole log and filters
by sequence. Device attestation applies to tool-permission answers, not to user
messages. Verified live on 2026-11-10 on a session created in a chat-capable
organization (`POST /v1/code/sessions` with `environment_id`, `config`, a first
user event and the `cowork-remote` tag). This route is allowlisted and never
retried, like the chat completion.

Images: a chat turn's images are uploaded first, one multipart `file` field per
POST to `/api/{org}/upload`, whose answer carries `file_uuid`; the completion's
`files` lists those IDs. A Cowork user event carries them inline: `content` becomes
`[{"type": "image", "source": {"type": "base64", "media_type", "data"}}, ...,
{"type": "text", "text"}]`, as the web app's composer builds it. Both verified
live on 2026-11-10 with a PNG each receiver named correctly. The upload route is
allowlisted and never retried. Codex's observed `/tasks/{id}/turns`
contains `turn_mapping` and `current_turn_id`; details alone omit historical turns.

## Session integration

`one-conv cloud sync PRODUCT` reads `ONE_CONV_SESSION_FILE` or `--session-file`.
This is an internal broker integration seam, not the end-user onboarding flow.
The private regular file must have mode 600 and contain `provider` (`openai` or
`anthropic`), a `cookies` map and an optional allowlisted `headers` map. Only
Authorization and ChatGPT-Account-ID headers are accepted. Do not commit these
files, include secrets in command arguments, or ask customers to copy tokens.
Claude requires explicit `--organization`. OpenAI session validation exchanges
the supplied sign-in cookie for an API bearer token before Codex task access.

Cloud login, a hosted session broker, hosted MCP and tenant storage are still
separate implementation work in CLOUD_PLAN.md. The cache here belongs to one
OS user; it is not a multi-tenant server database. No secrets are persisted in
the conversation cache. No automatic local browser fallback is attempted.

For explicit development verification, `one-conv cloud PRODUCT pull --account SELECTOR`
reuses browser authentication for ChatGPT, Claude Chat and Codex cloud. It is mutually
exclusive with `--session-file`, and is not a hosted login implementation.
The authenticated account identity is retained while actual history access is
checked; an additional token exchange is not required. Live CLI pulls on
2026-09-08 retrieved two personal ChatGPT conversations and three personal Codex
tasks. Claude personal browser login and a conversation read also succeeded.
Claude CLI session acquisition uses the same guarded browser cookie reader,
scoped to claude.ai, then validates organizations through `/api/organizations`.
It selects by organization name, organization ID or profile-qualified account ID.
The live CLI attempt found no session accessible without Keychain interaction;
its HTTP ingestion is not yet live-verified. Missing access never becomes an
empty successful sync. `cloud claude accounts --json` lists accessible selectors.

`cloud PRODUCT connect --browser chrome` explicitly authorizes a stable native
helper for one browser. Normal discovery uses its noninteractive read mode.
The helper is installed once, optimized and ad hoc signed; ordinary CLI source
updates do not replace its executable or Keychain trust identity. The local
installation needs developer tools for its first build. A distributed release
must use a stable signing identity across helper updates. No credentials are
written into arguments, logs or files by this helper.

The public hierarchy is `cloud chatgpt|claude|codex|cowork pull`. Internal source
IDs (`claude-chat`, `codex-cloud`, `cowork-cloud`) remain unchanged in stored data.
The old `cloud sync PRODUCT` and top-level commands remain compatibility aliases.

The bounded sync refetches up to `--limit` conversations per invocation; it is
not yet a background incremental scheduler. A capped scan is partial. Absence
from a partial or failed scan never deletes cached conversations. Cached search
is available offline under `claude-chat`, `codex-cloud` and `chatgpt` sources.
The full graph and content blocks are retained, while default text search follows
the active branch when supplied. Session and cache identities include product
and account, avoiding collisions between providers and accounts.

## Handling upstream changes

- Each adapter declares an evidence/version fingerprint. Keep route and payload
  fixes within that provider, with a redacted or synthetic regression fixture.
- Required identity/list/message fields fail with `SchemaChanged`; a changed
  payload is never silently treated as an empty successful account.
- Unknown content remains available in source blocks where supported; parsers
  reject unsupported shapes that they cannot represent safely.
- 401/403 are authorization failures. 429 respects Retry-After, including HTTP
  dates; long delays return to the caller instead of blocking indefinitely.
  GET retries and pagination are bounded. Redirects in the new transport do
  not forward account credentials to other origins.
- Cache files replace atomically after successful parsing. Schema/network
  failures retain older content and record an error outcome. Status files are
  observations of a particular sync, not a promise the session is still live.
- Test route requests, parsing and normalization separately from cache/search.
  Integration fixtures exercise provider -> cache -> public CLI. Use a known
  account and read-only list/detail canary before declaring a private endpoint
  live; never use successful fixture tests as live-access evidence.

Existing ChatGPT commands share retry, listing, search and conversation schema
validation with the provider layer. Legacy asset download and compact transcript
rendering still live in the compatibility CLI and are not the hosted adapter
contract. They can migrate independently without changing the normalized API.

## Evidence

- Codex private routes and fixture: official
  [backend client](https://github.com/openai/codex/blob/74d3a5bf1046f004ee33a200ee497dc7593a5687/codex-rs/backend-client/src/client.rs)
  and [task detail fixture](https://github.com/openai/codex/blob/74d3a5bf1046f004ee33a200ee497dc7593a5687/codex-rs/backend-client/tests/fixtures/task_details_with_diff.json).
- Claude Chat routes are based on public reverse-engineering implementations,
  not an official contract: [exporter route notes](https://github.com/glebmish/claude-exporter/blob/main/docs/claude-ai-api.md)
  and [export implementation](https://gist.github.com/jas-ho/f95abd89d4e007eac9ee821d7c2a3d0b).
- Cowork routes were observed on a populated Zama task: `/v1/code/sessions`
  filtered by `tags=cowork-remote`, session detail, and `/events`. The web client
  uses `cursor`/`next_cursor` pagination, distinct from its streaming resume
  cursor. Snapshots retain message content blocks; operational control events
  are excluded. Local Cowork sessions and hosted login are outside this adapter.
