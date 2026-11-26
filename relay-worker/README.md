# one-conv relay on Cloudflare

This folder is the default one-conv relay: a Cloudflare Worker that links your
machines' bridges (`one-conv live serve`), keeps the conversation archive they
sync, and serves the remote MCP server for the Claude app. It has the same API
as the self-hosted Python relay (`scripts/live-hub.py`).

| Piece | Cloudflare resource |
| --- | --- |
| Live relay: node tokens, sessions, messages, invites, session shares | Durable Object `Relay` (SQLite) |
| Guest page of a shared session (`/s/<id>`, `live share`) | `src/share-page.html`, bundled as text |
| Transcript of a shared session, for a share made with `--transcript` (`/v1/shares/<id>/transcript`, `read_session`, `search_session`) | The archive (`ArchiveIndex` + R2) copy of that one session, rendered by `src/transcript.ts` (`src/share-transcript.ts`) |
| MCP server of a shared session for a guest's agent (`/s/<id>/mcp`, share secret as bearer token, no OAuth) | Durable Object `Relay` (`src/share-mcp.ts`); `ask` waits on an in-memory promise, so a wait costs no CPU |
| Archive index | Durable Object `ArchiveIndex` (SQLite) |
| Board: topics and posts (`/v1/board`, `one-conv board`, `src/board.ts`) | Durable Object `Relay` (SQLite tables `board_topics`, `board_posts`, `board_files`) |
| Archive chunks, message images and board files (`board/<topic>/<file id>`) | R2 bucket, binding `ARCHIVE` |
| OAuth grants of the remote MCP connector | KV namespace, binding `OAUTH_KV` |

## Deploy from scratch

You need a Cloudflare account on the Workers Paid plan (the raised CPU limit in
`wrangler.jsonc` needs it) with R2 enabled, Node.js 24, and
[uv](https://docs.astral.sh/uv/). Run every command from this folder.

1. Install the pinned tools and log in:

   ```sh
   npm ci
   npx wrangler login
   ```

2. Create the bucket and the KV namespace:

   ```sh
   npx wrangler r2 bucket create one-conv-archive
   npx wrangler kv namespace create OAUTH_KV
   ```

   `wrangler.jsonc` holds the ids of the owner's deployment. In it, replace the
   `OAUTH_KV` id with the one the second command printed. If you named the
   bucket differently, replace `bucket_name` too. Keep `name` or choose your own:
   it becomes `https://<name>.<your-subdomain>.workers.dev`.

3. Create the admin secret in a private file and give it to the Worker. It
   mints node tokens and invites; never paste it into a command line or a chat.

   ```sh
   mkdir -p ~/.config/one-conv
   (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/one-conv/relay-admin.token)
   npx wrangler secret put RELAY_ADMIN_TOKEN < ~/.config/one-conv/relay-admin.token
   ```

4. Set the passphrase you will type to connect the Claude app (step 7), then
   deploy:

   ```sh
   npx wrangler secret put MCP_OWNER_PASSPHRASE   # prompts; use a long random passphrase
   npx wrangler deploy
   ```

   `deploy` prints the Worker's URL, `https://<your-relay>.workers.dev` below.

5. Connect the first machine. The admin script reads the secret from the
   environment only and prints one `one-conv live join` command:

   ```sh
   RELAY_ADMIN_TOKEN="$(cat ~/.config/one-conv/relay-admin.token)" \
     uv run scripts/relay_admin.py --url https://<your-relay>.workers.dev invite laptop
   ```

   Run the printed `one-conv live join '...' --yes` on that machine. It saves the
   node token, applies `one-conv live install`, and installs and starts the
   bridge. `token add NODE` instead prints a raw token for
   `one-conv live serve --hub URL --node NODE --token-stdin`.

6. Connect every next machine from one already connected, without the admin
   secret:

   ```sh
   one-conv live invite studio            # on a connected machine
   one-conv live join '<printed invite>' --yes   # on the new one
   ```

   An invite works once and expires after 15 minutes (`--ttl`, at most 24h). A
   node that already has a token needs `--replace`; joining then revokes its
   old token.

7. Connect the Claude app to the remote MCP server: Settings → Connectors → Add
   custom connector, URL `https://<your-relay>.workers.dev/mcp`, then type the
   passphrase from step 4 when the page asks.

Check a machine with `one-conv live serve --status`. Its settings, including its
token (mode 600), are in `<live dir>/bridge/serve.json`, by default
`~/.config/agent-conv-cli/live/bridge/serve.json`.

## Admin script

```sh
export RELAY_ADMIN_TOKEN="$(cat ~/.config/one-conv/relay-admin.token)"
uv run scripts/relay_admin.py --url https://<your-relay>.workers.dev token list
uv run scripts/relay_admin.py --url https://<your-relay>.workers.dev token revoke studio
uv run scripts/relay_admin.py --url https://<your-relay>.workers.dev invite studio --ttl 1h
```

`scripts/import_archive.py` copies a Python relay's `archive.db` into this relay;
it is idempotent and safe to rerun.

The admin token also deletes any board post. Only the node that posted a post
can delete it with its own token:

```sh
ONE_CONV_HUB_URL=https://<your-relay>.workers.dev ONE_CONV_HUB_TOKEN="$RELAY_ADMIN_TOKEN" \
  one-conv board rm TOPIC POST_ID --yes
```

## Board API

All routes take a node token.

| Route | What it does |
| --- | --- |
| `GET /v1/board?read=<topic>:<seq>...` | Topics, newest activity first, with post counts, `last` seq and `unread` past each given position |
| `GET /v1/board/<topic>?after=&limit=&tag=` | Live posts after a seq, oldest first, and the topic's `last` seq |
| `POST /v1/board/<topic>/files` | Raw file bytes, named by the percent-encoded `X-File-Name` header; returns its id and sha256 |
| `POST /v1/board/<topic>` | `{id, from, name, text, tags, files, title}`; idempotent by `id` |
| `GET /v1/board/<topic>/files/<id>` | A file that a live post carries, served as an attachment |
| `DELETE /v1/board/<topic>/posts/<id>` | Soft delete, by the posting node or the admin token |
| `GET /v1/board/search?q=&topic=&words=1` | Substring search (or all words with `words=1`) over post text, newest first |

The board's tables are created when the Relay starts, so a deploy needs no migration.

## Develop and test

```sh
npm ci
npx tsc --noEmit
npx vitest run                     # inside workerd; nothing reaches Cloudflare
npx wrangler dev --local --var RELAY_ADMIN_TOKEN:<secret>
```

From the repository root, `ONE_CONV_TEST_RELAY_WORKER=1` also runs the Python
relay tests against `wrangler dev --local`.

## Production gotchas

- Cloudflare answers 403 (error 1010) to Python's default `Python-urllib` user
  agent. one-conv and the scripts here send their own user agent; a new client
  must set one too.
- Durable Object SQLite binds at most 100 parameters per statement in
  production, while local workerd allows more. Batch queries stay under that.
- `limits.cpu_ms` in `wrangler.jsonc` is raised because a chunk upload
  decompresses and hashes up to 16 MB. Without it, large uploads fail.
- After `npx wrangler deploy`, Durable Object code can take about 5 minutes to
  reach every instance. A route that answers 404 right after a deploy usually
  needs that time, not a fix.
